"""Offline tests for the Git memory plugin.

Nothing here touches the network: the "remote" is a local bare repository in a
temporary directory, so the whole commit → pull → push path runs against a real
Git installation.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest
from plugin.plugins.git_memory.git_backend import (
    GitError,
    GitRepository,
    SyncOptions,
    build_git_env,
    detect_git,
    redact,
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
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
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
                "merge_strategy": "wrong",
                "gitignore_preset": "wrong",
            }
        )
    )
    assert len(warnings) == 5
    assert settings.branch == "main"
    assert settings.remote_name == "origin"
    assert settings.auto_sync_interval_minutes == 30
    assert settings.merge_strategy == "ours"
    assert settings.gitignore_preset == "default"

    accepted = validate_settings(GitMemorySettings.from_mapping({"auto_sync_interval_minutes": 60}))[0]
    assert accepted.auto_sync_interval_minutes == 60


def test_platform_and_git_environment_helpers() -> None:
    info = detect_platform()
    assert info["platform"] in {"windows", "macos", "linux"}
    assert info["install"]["commands"] or info["install"]["links"]
    assert info["download_page"].startswith("https://")
    assert GIT_INFO["gitpython_found"] is True

    env = build_git_env(token="secret-token", username="tester", proxy_url="http://127.0.0.1:7890")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "4"
    values = [env[f"GIT_CONFIG_VALUE_{index}"] for index in range(4)]
    credentials = base64.b64encode(b"tester:secret-token").decode("ascii")
    assert f"Authorization: Basic {credentials}" in values
    assert "secret-token" not in values
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert build_git_env()["GIT_CONFIG_COUNT"] == "3"


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
