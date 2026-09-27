"""Offline tests for the Git memory plugin.

Nothing here touches the network: the "remote" is a local bare repository in a
temporary directory, so the whole commit → pull → push path runs against a real
Git installation.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# 注意：market CI、`neko-plugin publish` 与本地检查都以插件仓库根为工作目录
# 运行 ruff（--isolated / --config ruff.toml），那里不存在 `plugin/` 包，
# 所以 `plugin.*` 按第三方导入排序——必须与 pytest 同块连续、且排在 pytest
# 之前（字母序 plugin < pytest）。不要在 N.E.K.O 仓库根目录下用 src 探测
# 重排这里的导入（那里 `plugin` 会被识别为一方导入，排出来反而过不了 CI）。
import plugin.plugins.git_memory.git_backend as git_backend_module
import pytest
from plugin.plugins.git_memory.git_backend import (
    GitError,
    GitRepository,
    SyncOptions,
    _map_git_error_text,
    build_git_env,
    detect_git,
    proxy_appears_in,
    redact,
    redact_proxy,
    resolve_git_proxy,
    resolve_remote_newer,
    sync_repository,
    web_url_from_remote,
)
from plugin.plugins.git_memory.platform_info import detect_platform
from plugin.plugins.git_memory.providers import (
    PROVIDERS,
    _normalize_repository,
    provider_options,
)
from plugin.plugins.git_memory.settings import (
    GITIGNORE_PRESETS,
    INTERVAL_OPTIONS,
    GitMemorySettings,
    build_gitignore,
    render_commit_message,
    validate_settings,
)

GIT_INFO = detect_git()
needs_git = pytest.mark.skipif(
    not GIT_INFO.get("available"),
    reason=f"Git / GitPython unavailable: {GIT_INFO.get('error')}",
)


def make_options(**overrides: object) -> SyncOptions:
    values: dict[str, object] = {
        "remote_name": "origin",
        "branch": "main",
        "commit_message": "chore(memory): sync test",
        "author_name": "NEKO Test",
        "author_email": "test@example.com",
    }
    values.update(overrides)
    return SyncOptions(**values)  # type: ignore[arg-type]


def make_remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


def remote_log(bare: Path, branch: str = "main") -> list[str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(bare), "log", "--oneline", branch],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


@needs_git
def test_local_sync_roundtrip(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "persona.txt").write_text("hello memory", encoding="utf-8")
    bare = make_remote(tmp_path)

    repo = GitRepository.initialize(
        memory,
        branch="main",
        author_name="NEKO Test",
        author_email="test@example.com",
    )
    assert repo.write_gitignore(build_gitignore("default", "*.secret")) is True
    repo.set_remote("origin", bare.as_uri())

    assert GitRepository(memory).is_repo() is True
    assert GitRepository(memory).remote_url("origin") == bare.as_uri()

    first = sync_repository(GitRepository(memory), make_options())
    assert first["status"] == "ok"
    assert first["pushed"] is True
    assert len(remote_log(bare)) == 1

    (memory / "persona.txt").write_text("hello memory v2", encoding="utf-8")
    (memory / "notes.txt").write_text("second file", encoding="utf-8")
    second = sync_repository(GitRepository(memory), make_options())
    assert second["changed_files"] == 2
    assert len(remote_log(bare)) == 2

    third = sync_repository(GitRepository(memory), make_options())
    assert third["status"] == "up_to_date"
    assert third["pushed"] is True
    assert len(remote_log(bare)) == 2

    snapshot = GitRepository(memory).status_snapshot()
    assert snapshot["initialized"] is True
    assert snapshot["dirty"] is False
    assert snapshot["branch"] == "main"


@needs_git
def test_sync_without_remote_reports_remote_missing(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "a.txt").write_text("a", encoding="utf-8")
    GitRepository.initialize(memory, branch="main")
    with pytest.raises(GitError) as excinfo:
        sync_repository(GitRepository(memory), make_options())
    assert excinfo.value.code == "remote_missing"


def publish_other_version(bare: Path, tmp_path: Path, *, text: str = "from remote") -> None:
    """Push a commit into ``bare`` from a second clone, like another machine."""

    other = tmp_path / "other-worktree"
    subprocess.run(["git", "clone", bare.as_uri(), str(other)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(other), "checkout", "-B", "main", "origin/main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(other), "config", "user.email", "other@example.com"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "Other"], check=True)
    (other / "remote.txt").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "add", "--all"], check=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-m", "remote change"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(other), "push", "origin", "main"], check=True, capture_output=True)


def prepare_diverged_repo(tmp_path: Path) -> Path:
    """A memory repo with one local edit while the remote got another commit."""

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "a.txt").write_text("local v1", encoding="utf-8")
    bare = make_remote(tmp_path)
    repo = GitRepository.initialize(
        memory,
        branch="main",
        author_name="NEKO Test",
        author_email="test@example.com",
    )
    repo.set_remote("origin", bare.as_uri())
    first = sync_repository(GitRepository(memory), make_options())
    assert first["status"] == "ok"
    publish_other_version(bare, tmp_path)
    (memory / "a.txt").write_text("local v2", encoding="utf-8")
    return bare


@needs_git
def test_remote_newer_asks_before_touching_either_side(tmp_path: Path) -> None:
    bare = prepare_diverged_repo(tmp_path)
    memory = tmp_path / "memory"

    report = sync_repository(GitRepository(memory), make_options())

    assert report["status"] == "pending_choice"
    assert report["code"] == "remote_newer"
    assert report["relation"]["behind"] == 1
    assert report["relation"]["ahead"] == 1
    assert report["remote_head"]["short"]
    assert report["remote_head"]["subject"] == "remote change"
    # 询问模式下两边都不能被改动：本地没有远端文件，远端也还是两条提交。
    assert not (memory / "remote.txt").exists()
    assert len(remote_log(bare)) == 2


@needs_git
def test_keep_remote_version_overwrites_local_copy(tmp_path: Path) -> None:
    bare = prepare_diverged_repo(tmp_path)
    memory = tmp_path / "memory"
    assert sync_repository(GitRepository(memory), make_options())["status"] == "pending_choice"

    resolved = resolve_remote_newer(GitRepository(memory), make_options(), "keep_remote")

    assert resolved["status"] == "ok"
    assert resolved["resolution"] == "keep_remote"
    assert resolved["forced"] is False
    assert (memory / "remote.txt").read_text(encoding="utf-8") == "from remote"
    assert (memory / "a.txt").read_text(encoding="utf-8") == "local v1"
    assert len(remote_log(bare)) == 2

    after = sync_repository(GitRepository(memory), make_options())
    assert after["status"] == "up_to_date"


@needs_git
def test_keep_local_version_force_pushes_and_clears_pending(tmp_path: Path) -> None:
    bare = prepare_diverged_repo(tmp_path)
    memory = tmp_path / "memory"
    assert sync_repository(GitRepository(memory), make_options())["status"] == "pending_choice"

    resolved = resolve_remote_newer(GitRepository(memory), make_options(), "keep_local")

    assert resolved["resolution"] == "keep_local"
    assert resolved["forced"] is True
    log = remote_log(bare)
    assert len(log) == 2
    assert "remote change" not in log[0]
    # 强制推送后远端跟踪引用也要跟着走，否则下一次同步又会误判「远端更新」。
    after = sync_repository(GitRepository(memory), make_options())
    assert after["status"] == "up_to_date"
    assert after.get("code") != "remote_newer"


@needs_git
def test_keep_remote_policy_resolves_without_asking(tmp_path: Path) -> None:
    prepare_diverged_repo(tmp_path)
    memory = tmp_path / "memory"

    report = sync_repository(GitRepository(memory), make_options(remote_update_policy="keep_remote"))

    assert report["status"] == "ok"
    assert (memory / "remote.txt").read_text(encoding="utf-8") == "from remote"


@needs_git
def test_sync_falls_back_to_current_branch_when_configured_branch_missing(tmp_path: Path) -> None:
    """配置分支在本地不存在时必须退回当前分支，而不是报 src refspec 错误。"""

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "a.txt").write_text("local", encoding="utf-8")
    bare = make_remote(tmp_path)
    # 旧仓库停在 master；设置里仍是默认的 main。
    GitRepository.initialize(
        memory,
        branch="master",
        author_name="NEKO Test",
        author_email="test@example.com",
    )
    GitRepository(memory).set_remote("origin", bare.as_uri())

    report = sync_repository(GitRepository(memory), make_options(branch="main"))

    assert report["status"] == "ok"
    assert report["branch"] == "master"
    assert remote_log(bare, "master")


def test_git_command_timeout_kills_the_blocking_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时必须真的把卡住的进程结束掉。

    不要用「跑一条很快的命令 + 极短超时」来测这个：在 Linux 的 CI 上
    ``git --version`` 能在 1 毫秒内跑完，用例就会偶发地不抛异常（正是它把
    release 流水线挂掉的那次）。这里改成让一个必定还在运行的进程充当 git 子进程，
    超时该杀就杀，结果与机器快慢无关。
    """

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    fake_repo = SimpleNamespace(
        git=SimpleNamespace(
            execute=lambda command, **kwargs: SimpleNamespace(proc=process),
        )
    )
    monkeypatch.setattr(GitRepository, "repository", lambda self: fake_repo)

    try:
        with pytest.raises(GitError) as excinfo:
            GitRepository(tmp_path).git("--version", timeout=0.5)

        assert excinfo.value.code == "timeout"
        assert process.poll() is not None, "超时后子进程必须已经被结束"
    finally:
        if process.poll() is None:  # pragma: no cover - only on a broken timeout path
            process.kill()
            process.wait(timeout=5)


def test_gitignore_presets_and_commit_template() -> None:
    assert set(GITIGNORE_PRESETS) == {"default", "minimal", "none"}
    assert build_gitignore("none", "") == ""
    assert "*.tmp" in build_gitignore("default")
    assert "*.secret" in build_gitignore("minimal", "*.secret")

    message = render_commit_message("sync {date} {count}", changed_files=3)
    assert message.startswith("sync ")
    assert message.endswith(" 3")
    assert render_commit_message("keep {unknown}") == "keep {unknown}"


def test_settings_validation_restores_defaults() -> None:
    assert INTERVAL_OPTIONS == (5, 10, 30, 60)
    settings, warnings = validate_settings(
        GitMemorySettings.from_mapping(
            {
                "branch": "bad branch",
                "remote_name": "bad name",
                "auto_sync_interval_minutes": 9999,
                "remote_update_policy": "wrong",
                "proxy_mode": "wrong",
                "gitignore_preset": "wrong",
            }
        )
    )
    assert len(warnings) == 6
    assert settings.branch == "main"
    assert settings.remote_name == "origin"
    assert settings.auto_sync_interval_minutes == 30
    assert settings.remote_update_policy == "ask"
    assert settings.proxy_mode == "auto"
    assert settings.gitignore_preset == "default"

    accepted = validate_settings(GitMemorySettings.from_mapping({"auto_sync_interval_minutes": 60}))[0]
    assert accepted.auto_sync_interval_minutes == 60


def test_platform_and_git_environment_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 结果只应该由参数决定，不受跑测试的机器上代理环境变量的影响。
    for key in ("NO_PROXY", "no_proxy", *git_backend_module.PROXY_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)

    info = detect_platform()
    assert info["platform"] in {"windows", "macos", "linux"}
    assert info["install"]["commands"] or info["install"]["links"]
    assert info["download_page"].startswith("https://")
    assert GIT_INFO["gitpython_found"] is True

    env = build_git_env(token="secret-token", username="tester", proxy_url="http://127.0.0.1:7890")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    count = int(env["GIT_CONFIG_COUNT"])
    keys = [env[f"GIT_CONFIG_KEY_{index}"] for index in range(count)]
    values = [env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)]
    credentials = base64.b64encode(b"tester:secret-token").decode("ascii")
    assert f"Authorization: Basic {credentials}" in values
    assert "secret-token" not in values
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "http.proxy" in keys and "https.proxy" in keys and "http.extraheader" in keys

    # 直连模式：代理配置显式清空，继承来的代理环境变量也一起关掉。
    direct = build_git_env(proxy_mode="off")
    assert direct["NO_PROXY"] == "*"
    assert direct["HTTPS_PROXY"] == ""
    assert "" in [direct[f"GIT_CONFIG_VALUE_{index}"] for index in range(int(direct["GIT_CONFIG_COUNT"]))]


def test_proxy_resolution_prefers_setting_then_environment_then_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("NO_PROXY", "no_proxy", *git_backend_module.PROXY_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(git_backend_module, "detect_system_proxy", lambda: "http://127.0.0.1:1080")

    assert resolve_git_proxy("http://127.0.0.1:7890", "auto")["source"] == "setting"
    assert resolve_git_proxy("", "manual")["source"] == "none"
    assert resolve_git_proxy("", "auto")["source"] == "system"

    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:8080")
    assert resolve_git_proxy("", "auto")["source"] == "environment"
    # 用户在设置里填了地址时，环境变量不该抢走它。
    assert resolve_git_proxy("http://127.0.0.1:7890", "auto")["source"] == "setting"

    # N.E.K.O 的直连模式（NO_PROXY=*）优先于一切自动探测。
    monkeypatch.setenv("NO_PROXY", "*")
    assert resolve_git_proxy("", "auto")["source"] == "direct"
    assert resolve_git_proxy("http://127.0.0.1:7890", "auto")["source"] == "direct"

    assert resolve_git_proxy("http://127.0.0.1:7890", "off")["source"] == "direct"
    assert redact_proxy("http://user:secret@127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_network_failures_are_reported_specifically() -> None:
    cases = {
        "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com": "DNS",
        "fatal: unable to access 'https://github.com/': Received HTTP code 407 from proxy after CONNECT": "代理",
        "fatal: unable to access 'https://github.com/': SSL certificate problem: unable to get local issuer certificate": "证书",
        "fatal: unable to access 'https://github.com/': Failed to connect to github.com port 443: Timed out": "超时",
        "fatal: the remote end hung up unexpectedly": "传输中断",
    }
    for raw, expected in cases.items():
        error = _map_git_error_text(raw)
        assert error.code == "network_error", raw
        assert expected in str(error), (raw, str(error))

    unknown = _map_git_error_text("fatal: something nobody has seen before")
    assert unknown.code == "unknown"


def test_proxy_appears_in_matches_both_git_phrasings() -> None:
    assert proxy_appears_in("Failed to connect to 127.0.0.1 port 9: refused", "http://127.0.0.1:9")
    assert proxy_appears_in("could not connect to 127.0.0.1:9", "http://user:pw@127.0.0.1:9")
    assert not proxy_appears_in("Could not resolve host: github.com", "http://127.0.0.1:7890")
    assert not proxy_appears_in("anything", "")


def test_credentials_are_redacted() -> None:
    assert "secret" not in redact("fatal: https://user:secret@github.com/o/r.git rejected")
    assert web_url_from_remote("https://github.com/owner/name.git") == "https://github.com/owner/name"
    assert web_url_from_remote("git@gitee.com:owner/name.git") == "https://gitee.com/owner/name"


def test_provider_catalogue_covers_three_sites() -> None:
    options = {item["id"]: item for item in provider_options()}
    assert set(options) == {"github", "gitlab", "gitee"}
    for spec in PROVIDERS.values():
        assert spec.token_url.startswith("https://")
        assert spec.docs_url.startswith("https://")
        assert spec.signup_url.startswith("https://")

    # Gitee 撤掉了帮助中心的「私人令牌」文档（该路径现在 404），注册页也从
    # /signup 换成了 /register（前者返回 405）。这两个都实测过。
    gitee_spec = PROVIDERS["gitee"]
    assert "help.gitee.com/account/personal-access-token" not in gitee_spec.docs_url
    assert gitee_spec.docs_url == gitee_spec.token_url
    assert gitee_spec.signup_url == "https://gitee.com/register"

    gitee = _normalize_repository(
        "gitee",
        {"full_name": "owner/name", "html_url": "https://gitee.com/owner/name"},
        "https://gitee.com",
    )
    assert gitee is not None
    assert gitee["clone_url"] == "https://gitee.com/owner/name.git"
    assert gitee["web_url"] == "https://gitee.com/owner/name"

    github = _normalize_repository(
        "github",
        {"full_name": "owner/repo", "clone_url": "https://github.com/owner/repo.git", "private": True},
        "https://github.com",
    )
    assert github is not None
    assert github["clone_url"] == "https://github.com/owner/repo.git"
    assert github["private"] is True
