"""GitPython based Git operations for the N.E.K.O memory directory.

Every network command runs with ``GIT_TERMINAL_PROMPT=0`` and an in-memory
``http.extraheader`` credential, so access tokens never end up in the
repository's ``.git/config`` and a missing credential fails fast instead of
opening an interactive prompt that no one can answer.
"""

from __future__ import annotations

import base64
import importlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
LOCAL_COMMAND_TIMEOUT = 60.0
NETWORK_COMMAND_TIMEOUT = 180.0

_SECRET_IN_URL = re.compile(r"//[^/\s@]+:[^/\s@]+@")
_AUTH_ERROR_HINTS = (
    "authentication failed",
    "invalid username or password",
    "could not read username",
    "terminal prompts disabled",
    "403 forbidden",
    "401 unauthorized",
    "http 401",
    "http 403",
    "access denied",
    "permission denied (publickey)",
    "remote: you do not have permission",
)
_NETWORK_ERROR_HINTS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "connection refused",
    "connection reset",
    "the remote end hung up",
    "early eof",
    "rpc failed",
    "transfer closed",
    "ssl",
    "tls",
    "proxy",
    "unable to access",
)

ERROR_MESSAGES = {
    "git_missing": "未检测到 Git，请先安装 Git。",
    "gitpython_missing": "插件依赖 GitPython 尚未安装，请重新安装或更新插件。",
    "not_initialized": "memory 目录还没有初始化 Git 仓库。",
    "remote_missing": "还没有关联远端仓库。",
    "identity_missing": "Git 提交身份未配置，请在面板中填写用户名与邮箱。",
    "auth_failed": "远端拒绝了本次认证，请重新检查访问令牌与仓库权限。",
    "network_error": "网络请求失败，请检查网络或代理设置。",
    "merge_conflict": "远端与本地改动冲突，同步已中止，请手动处理后重试。",
    "diverged": "本地与远端历史已经分叉，请先手动合并后再同步。",
    "push_rejected": "远端拒绝了本次推送，请先拉取远端改动。",
    "remote_changed": "远端仓库在我们读取之后又有了新的提交，请刷新后重新选择保留哪一版。",
    "timeout": "Git 命令超时仍未返回，已强制中止（通常是网络被挂起，或在等待凭据窗口）。",
    "dirty_worktree": "工作区还有未提交的改动。",
    "unknown": "Git 操作失败。",
}


class GitError(RuntimeError):
    """A Git failure carrying a stable code plus a redacted detail string."""

    def __init__(self, code: str = "unknown", *, message: str = "", detail: str = "") -> None:
        self.code = code if code in ERROR_MESSAGES else "unknown"
        self.detail = redact(detail)
        super().__init__(message or ERROR_MESSAGES[self.code])

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": str(self),
            "detail": self.detail,
        }


def redact(value: object) -> str:
    """Strip ``user:token@`` credentials out of any Git output."""

    text = str(value or "")
    text = _SECRET_IN_URL.sub("//", text)
    return text.strip()


def gitpython_module() -> Any | None:
    try:
        return importlib.import_module("git")
    except Exception:
        return None


def gitpython_available() -> bool:
    return gitpython_module() is not None


def detect_git() -> dict[str, object]:
    """Report whether Git (and GitPython) can actually be used."""

    git_module = gitpython_module()
    info: dict[str, object] = {
        "available": False,
        "git_found": False,
        "gitpython_found": git_module is not None,
        "gitpython_version": getattr(git_module, "__version__", ""),
        "version": "",
        "path": "",
        "error": "",
        "error_code": "",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    executable = shutil.which("git") or ""
    info["path"] = executable
    if not executable:
        info["error_code"] = "git_missing"
        info["error"] = ERROR_MESSAGES["git_missing"]
        return info

    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - platform dependent
        info["error_code"] = "git_missing"
        info["error"] = f"{ERROR_MESSAGES['git_missing']} ({type(exc).__name__})"
        return info

    output = (completed.stdout or completed.stderr or "").strip()
    info["version"] = output
    info["git_found"] = completed.returncode == 0
    if completed.returncode != 0:
        info["error_code"] = "git_missing"
        info["error"] = redact(output) or ERROR_MESSAGES["git_missing"]
        return info

    if git_module is None:
        info["error_code"] = "gitpython_missing"
        info["error"] = ERROR_MESSAGES["gitpython_missing"]
        return info

    info["available"] = True
    return info


PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_PROXY_CREDENTIALS = re.compile(r"//[^/\s@]+@")


def redact_proxy(url: object) -> str:
    """Drop ``user:password@`` from a proxy URL before it is shown or logged."""

    return _PROXY_CREDENTIALS.sub("//", str(url or "").strip())


def proxy_appears_in(detail: object, proxy_display: str) -> bool:
    """True when git's own output names the configured proxy host/port.

    That is the difference between "the proxy is the problem" and "the network
    is the problem", and git writes it two ways: ``127.0.0.1:7890`` and
    ``Failed to connect to 127.0.0.1 port 7890``.
    """

    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(proxy_display or "").strip())
    except Exception:  # pragma: no cover - defensive
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    text = str(detail or "").lower()
    candidates = [host]
    if parsed.port:
        candidates.append(f"{host}:{parsed.port}")
        candidates.append(f"{host} port {parsed.port}")
    return any(candidate in text for candidate in candidates)


def _direct_mode_requested(environ: Mapping[str, str] | None = None) -> bool:
    """True when N.E.K.O's direct-connection switch marked the environment."""

    source: Mapping[str, str] = environ if environ is not None else os.environ
    raw = str(source.get("NO_PROXY") or source.get("no_proxy") or "")
    return any(item.strip() == "*" for item in raw.replace(";", ",").split(","))


def detect_system_proxy() -> str:
    """Best-effort read of the OS proxy (Windows registry, macOS network setup)."""

    try:
        import urllib.request

        proxies = urllib.request.getproxies()
    except Exception:  # pragma: no cover - platform dependent
        return ""
    if isinstance(proxies, Mapping):
        for key in ("https", "http", "all"):
            value = str(proxies.get(key) or "").strip()
            if value:
                return value
    return ""


def resolve_git_proxy(proxy_url: str = "", proxy_mode: str = "auto") -> dict[str, str]:
    """Decide which proxy git should use, and where that choice came from.

    git reads neither the Windows registry nor the macOS network settings — only
    its own config and the process environment. A user whose browser and the
    rest of N.E.K.O work fine can therefore still get "network error" from every
    push. ``auto`` mirrors what the rest of the app sees: the plugin setting
    first, then the inherited environment, then the OS proxy. ``off`` forces a
    direct connection; ``manual`` trusts only the configured address.
    """

    mode = str(proxy_mode or "auto").strip().lower()
    configured = str(proxy_url or "").strip()
    if mode == "off" or (mode != "manual" and _direct_mode_requested()):
        return {"url": "", "display": "", "source": "direct"}
    if configured:
        return {"url": configured, "display": redact_proxy(configured), "source": "setting"}
    if mode == "manual":
        return {"url": "", "display": "", "source": "none"}
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return {"url": value, "display": redact_proxy(value), "source": "environment"}
    detected = detect_system_proxy()
    if detected:
        return {"url": detected, "display": redact_proxy(detected), "source": "system"}
    return {"url": "", "display": "", "source": "none"}


def build_git_env(
    *,
    token: str = "",
    username: str = "",
    proxy_url: str = "",
    proxy_mode: str = "auto",
    proxy: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for one Git invocation (credentials never touch the disk)."""

    resolved = dict(proxy) if isinstance(proxy, Mapping) else resolve_git_proxy(proxy_url, proxy_mode)
    env: dict[str, str] = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }

    config_pairs: list[tuple[str, str]] = [
        ("http.lowSpeedLimit", "1000"),
        ("http.lowSpeedTime", "60"),
        ("credential.helper", ""),
    ]
    selected_proxy = str(resolved.get("url") or "").strip()
    if selected_proxy:
        config_pairs.append(("http.proxy", selected_proxy))
        config_pairs.append(("https.proxy", selected_proxy))
    elif resolved.get("source") == "direct":
        # 明确直连：连继承来的代理配置一起关掉，避免死代理把同步拖住。
        config_pairs.append(("http.proxy", ""))
        config_pairs.append(("https.proxy", ""))
    if token:
        account = username or "oauth2"
        encoded = base64.b64encode(f"{account}:{token}".encode("utf-8")).decode("ascii")
        config_pairs.append(("http.extraheader", f"Authorization: Basic {encoded}"))
    for index, (key, value) in enumerate(config_pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(len(config_pairs))

    if selected_proxy:
        for key in PROXY_ENV_KEYS:
            env[key] = selected_proxy
    elif resolved.get("source") == "direct":
        for key in PROXY_ENV_KEYS:
            env[key] = ""
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
    return env


@dataclass
class SyncOptions:
    """Everything the sync engine needs to know, already resolved by the plugin."""

    remote_name: str = DEFAULT_REMOTE
    branch: str = DEFAULT_BRANCH
    commit_message: str = "chore(memory): sync"
    author_name: str = ""
    author_email: str = ""
    pull_before_push: bool = True
    # ask / keep_local / keep_remote：远端有新提交时如何取舍。
    remote_update_policy: str = "ask"
    proxy_url: str = ""
    # auto / manual / off：git 用哪个代理（auto 会跟随应用与系统代理）。
    proxy_mode: str = "auto"
    token: str = ""
    username: str = ""
    timeout: float = NETWORK_COMMAND_TIMEOUT
    extra: dict[str, Any] = field(default_factory=dict)


class GitRepository:
    """Thin, testable wrapper around :class:`git.Repo`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ basics
    @property
    def git_dir(self) -> Path:
        return self.path / ".git"

    def exists(self) -> bool:
        return self.path.exists()

    def is_repo(self) -> bool:
        if not self.git_dir.exists():
            return False
        return self._open() is not None

    def _open(self) -> Any | None:
        module = gitpython_module()
        if module is None:
            return None
        try:
            return module.Repo(str(self.path))
        except Exception:
            return None

    def repository(self) -> Any:
        module = gitpython_module()
        if module is None:
            raise GitError("gitpython_missing")
        if not self.git_dir.exists():
            raise GitError("not_initialized")
        try:
            return module.Repo(str(self.path))
        except Exception as exc:
            raise GitError("not_initialized", detail=str(exc)) from exc

    @classmethod
    def initialize(
        cls,
        path: str | Path,
        *,
        branch: str = DEFAULT_BRANCH,
        author_name: str = "",
        author_email: str = "",
    ) -> "GitRepository":
        module = gitpython_module()
        if module is None:
            raise GitError("gitpython_missing")
        target = Path(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GitError("unknown", message=f"无法创建目录：{target}", detail=str(exc)) from exc

        repo = cls(target)
        if repo.is_repo():
            return repo
        try:
            module.Repo.init(str(target), mkdir=False, initial_branch=branch)
        except TypeError:
            module.Repo.init(str(target), mkdir=False)
            repo.git("branch", "-M", branch)
        except Exception as exc:
            raise GitError("unknown", message="初始化 Git 仓库失败。", detail=str(exc)) from exc
        repo.ensure_identity(author_name, author_email)
        return repo

    def git(self, *args: str, env: dict[str, str] | None = None, timeout: float | None = None) -> str:
        """Run one ``git`` command, killing it if it blocks past ``timeout``.

        GitPython refuses ``kill_after_timeout`` on Windows (its child-process
        lookup shells out to ``ps``), so a command that waits forever — a
        stalled proxy, a credential helper, a locked index — used to freeze the
        sync action with no way back. Drive the process ourselves instead and
        kill the whole tree on expiry, on every platform.
        """

        repo = self.repository()
        command = ["git", *[str(item) for item in args]]
        resolved_env = dict(env) if env is not None else build_git_env()
        limit = float(timeout) if timeout else LOCAL_COMMAND_TIMEOUT
        try:
            handle = repo.git.execute(command, env=resolved_env, as_process=True)
        except Exception as exc:
            raise _map_git_error(exc) from exc
        process = handle.proc
        try:
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=limit)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_tree(process)
                label = " ".join(command[:2])
                raise GitError("timeout", detail=f"{label} 超过 {limit:g} 秒仍未返回。") from exc
        finally:
            # AutoInterrupt.__del__ 会在 GC 时关流并再 poll 一次；进程早已结束时
            # 这在 Windows 上会抛 OSError(9) 并往日志里塞一行噪音。提前断开引用。
            handle.proc = None
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        if process.returncode:
            raise _map_git_error_text(stderr or stdout)
        return stdout.strip()

    # -------------------------------------------------------------- inspection
    def current_branch(self) -> str:
        repo = self.repository()
        try:
            return repo.active_branch.name
        except Exception:
            head = repo.head
            return head.ref.name if getattr(head, "is_detached", False) is False else ""

    def status_snapshot(self) -> dict[str, object]:
        """Return branch / cleanliness / remote information without touching the network."""

        snapshot: dict[str, object] = {
            "exists": self.exists(),
            "initialized": False,
            "branch": "",
            "dirty": False,
            "changed_files": 0,
            "untracked_files": 0,
            "head_commit": "",
            "head_subject": "",
            "head_committed_at": "",
            "remote_name": "",
            "remote_url": "",
            "remote_web_url": "",
            "remotes": [],
            "last_commit_message": "",
            "last_commit_at": "",
        }
        if not self.is_repo():
            return snapshot

        repo = self.repository()
        snapshot["initialized"] = True
        snapshot["branch"] = self.current_branch()
        try:
            porcelain = self.git("status", "--porcelain", timeout=LOCAL_COMMAND_TIMEOUT)
        except GitError:
            porcelain = ""
        lines = [line for line in porcelain.splitlines() if line.strip()]
        snapshot["changed_files"] = len(lines)
        snapshot["dirty"] = bool(lines)
        snapshot["untracked_files"] = len([line for line in lines if line.startswith("??")])

        try:
            head = repo.head.commit
            snapshot["head_commit"] = head.hexsha[:12]
            snapshot["head_subject"] = head.summary or ""
            snapshot["last_commit_message"] = (head.message or "").strip().splitlines()[0] if head.message else ""
            committed_at = datetime.fromtimestamp(head.committed_date, tz=timezone.utc)
            snapshot["head_committed_at"] = committed_at.isoformat()
            snapshot["last_commit_at"] = snapshot["head_committed_at"]
        except Exception:
            pass

        remotes: list[dict[str, str]] = []
        for remote in getattr(repo, "remotes", []):
            url = ""
            try:
                url = remote.url or ""
            except Exception:
                url = ""
            remotes.append({"name": remote.name, "url": url, "web_url": web_url_from_remote(url)})
        snapshot["remotes"] = remotes
        preferred = self.remote_url(DEFAULT_REMOTE) or (remotes[0]["url"] if remotes else "")
        preferred_name = DEFAULT_REMOTE if self.remote_url(DEFAULT_REMOTE) else (remotes[0]["name"] if remotes else "")
        snapshot["remote_name"] = preferred_name
        snapshot["remote_url"] = preferred
        snapshot["remote_web_url"] = web_url_from_remote(preferred)
        return snapshot

    def change_count(self) -> int:
        porcelain = self.git("status", "--porcelain", timeout=LOCAL_COMMAND_TIMEOUT)
        return len([line for line in porcelain.splitlines() if line.strip()])

    def has_commits(self) -> bool:
        repo = self.repository()
        try:
            repo.head.commit
        except Exception:
            return False
        return True

    def local_config(self) -> dict[str, str]:
        return {
            "user.name": self.config_get("user.name"),
            "user.email": self.config_get("user.email"),
        }

    def config_get(self, key: str) -> str:
        try:
            value = self.git("config", "--local", "--get", key, timeout=LOCAL_COMMAND_TIMEOUT)
        except GitError:
            return ""
        return value.strip()

    def config_set(self, key: str, value: str) -> None:
        self.git("config", "--local", key, value, timeout=LOCAL_COMMAND_TIMEOUT)

    # ------------------------------------------------------------------ mutation
    def ensure_identity(self, name: str, email: str) -> None:
        if name.strip():
            self.config_set("user.name", name.strip())
        if email.strip():
            self.config_set("user.email", email.strip())

    def write_gitignore(self, text: str) -> bool:
        path = self.path / ".gitignore"
        if path.exists():
            return False
        path.write_text(text, encoding="utf-8")
        return True

    def stage_all(self) -> None:
        self.git("add", "--all", timeout=LOCAL_COMMAND_TIMEOUT)

    def commit(self, message: str, *, author_name: str = "", author_email: str = "") -> str:
        module = gitpython_module()
        repo = self.repository()
        if module is None:  # pragma: no cover - guarded by repository()
            raise GitError("gitpython_missing")

        resolved_name = author_name.strip() or self.config_get("user.name")
        resolved_email = author_email.strip() or self.config_get("user.email")
        if not resolved_name or not resolved_email:
            raise GitError("identity_missing")
        self.ensure_identity(resolved_name, resolved_email)
        actor = module.Actor(resolved_name, resolved_email)
        try:
            commit = repo.index.commit(message, author=actor, committer=actor)
        except Exception as exc:
            raise _map_git_error(exc) from exc
        return commit.hexsha

    def remote_url(self, name: str) -> str:
        if not name:
            return ""
        try:
            value = self.git("remote", "get-url", name, timeout=LOCAL_COMMAND_TIMEOUT)
        except GitError:
            return ""
        return value.strip()

    def set_remote(self, name: str, url: str) -> None:
        repo = self.repository()
        try:
            repo.create_remote(name, url)
        except Exception:
            self.git("remote", "set-url", name, url, timeout=LOCAL_COMMAND_TIMEOUT)

    def remove_remote(self, name: str) -> bool:
        repo = self.repository()
        try:
            remote = repo.remote(name)
        except Exception:
            return False
        repo.delete_remote(remote)
        return True

    def ref_exists(self, ref: str) -> bool:
        try:
            self.git("rev-parse", "--verify", "--quiet", ref, timeout=LOCAL_COMMAND_TIMEOUT)
        except GitError:
            return False
        return True

    def relation(self, local_ref: str, remote_ref: str) -> dict[str, int]:
        try:
            output = self.git(
                "rev-list",
                "--left-right",
                "--count",
                f"{local_ref}...{remote_ref}",
                timeout=LOCAL_COMMAND_TIMEOUT,
            )
        except GitError:
            return {"ahead": 0, "behind": 0}
        parts = output.replace("\t", " ").split()
        ahead = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
        behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return {"ahead": ahead, "behind": behind}

    def fetch(self, remote_name: str, *, env: dict[str, str], timeout: float = NETWORK_COMMAND_TIMEOUT) -> str:
        return self.git("fetch", "--prune", remote_name, env=env, timeout=timeout)

    def merge(self, ref: str, *, strategy: str, env: dict[str, str], timeout: float = NETWORK_COMMAND_TIMEOUT) -> str:
        try:
            return self.git("merge", "--ff-only", ref, env=env, timeout=timeout)
        except GitError as exc:
            if strategy == "abort":
                self.abort_merge()
                raise GitError("diverged", detail=exc.detail) from exc
        try:
            return self.git(
                "merge",
                "--no-edit",
                "--allow-unrelated-histories",
                "-X",
                "ours",
                ref,
                env=env,
                timeout=timeout,
            )
        except GitError as exc:
            self.abort_merge()
            raise GitError("merge_conflict", detail=exc.detail) from exc

    def abort_merge(self) -> None:
        try:
            self.git("merge", "--abort", timeout=LOCAL_COMMAND_TIMEOUT)
        except GitError:
            pass

    def has_upstream(self, branch: str) -> bool:
        try:
            self.git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}", timeout=LOCAL_COMMAND_TIMEOUT)
        except GitError:
            return False
        return True

    def push(
        self,
        remote_name: str,
        branch: str,
        *,
        env: dict[str, str],
        set_upstream: bool,
        timeout: float = NETWORK_COMMAND_TIMEOUT,
    ) -> str:
        args = ["push", "--porcelain"]
        if set_upstream:
            args.append("--set-upstream")
        args.extend([remote_name, f"refs/heads/{branch}:refs/heads/{branch}"])
        return self.git(*args, env=env, timeout=timeout)

    def remote_head(self, remote_name: str, branch: str) -> dict[str, str]:
        """Latest commit of the already-fetched ``<remote>/<branch>`` ref."""

        if not branch:
            return {}
        ref = f"{remote_name}/{branch}"
        if not self.ref_exists(ref):
            return {}
        try:
            output = self.git(
                "log",
                "-1",
                "--format=%H%x1f%an%x1f%cI%x1f%s",
                ref,
                timeout=LOCAL_COMMAND_TIMEOUT,
            )
        except GitError:
            return {}
        parts = output.split("\x1f")
        if len(parts) < 4 or not parts[0]:
            return {}
        return {
            "ref": ref,
            "sha": parts[0],
            "short": parts[0][:12],
            "author": parts[1],
            "committed_at": parts[2],
            "subject": parts[3],
        }

    def reset_hard(self, ref: str) -> None:
        """Take ``ref`` as-is, discarding local commits and file edits."""

        self.git("reset", "--hard", ref, timeout=LOCAL_COMMAND_TIMEOUT)

    def update_remote_tracking(self, remote_name: str, branch: str, source_ref: str = "HEAD") -> None:
        """Point ``<remote>/<branch>`` at ``source_ref`` after a forced push.

        Without this the remote-tracking ref still holds the version we just
        overwrote, so the next sync would report a phantom "remote is newer".
        """

        if not branch:
            return
        self.git(
            "update-ref",
            f"refs/remotes/{remote_name}/{branch}",
            source_ref,
            timeout=LOCAL_COMMAND_TIMEOUT,
        )

    def force_push(
        self,
        remote_name: str,
        branch: str,
        *,
        env: dict[str, str] | None = None,
        timeout: float = NETWORK_COMMAND_TIMEOUT,
        expected_sha: str = "",
    ) -> str:
        """Overwrite the remote branch with the local one (user approved)."""

        target = f"refs/heads/{branch}:refs/heads/{branch}"
        args = ["push", "--porcelain"]
        args.append(f"--force-with-lease=refs/heads/{branch}:{expected_sha}" if expected_sha else "--force")
        args.extend([remote_name, target])
        try:
            return self.git(*args, env=env, timeout=timeout)
        except GitError as exc:
            if expected_sha and exc.code in {"remote_changed", "push_rejected"}:
                # 远端在被读取之后又动过。用户已经明确选择覆盖远端，这里退回到
                # 无条件 --force，但调用方仍会记录这是一次强制覆盖。
                return self.git("push", "--porcelain", "--force", remote_name, target, env=env, timeout=timeout)
            raise


def web_url_from_remote(url: str) -> str:
    """Convert a clone URL into the browser URL of the repository."""

    value = (url or "").strip()
    if not value:
        return ""
    value = _SECRET_IN_URL.sub("//", value)
    if value.startswith("git@") and ":" in value:
        host_path = value[4:].replace(":", "/", 1)
        return f"https://{host_path}".removesuffix(".git")
    if value.startswith("http://") or value.startswith("https://"):
        return value.removesuffix(".git")
    return ""


def _is_windows() -> bool:
    import sys

    return sys.platform.startswith("win")


def _terminate_process_tree(process: Any) -> None:
    """Kill a git process plus anything it spawned (ssh, credential helpers)."""

    pid = int(getattr(process, "pid", 0) or 0)
    if _is_windows() and pid:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
            process.wait(timeout=5)
            return
        except Exception:  # pragma: no cover - fall back to killing the child
            pass
    try:
        process.kill()
    except Exception:  # pragma: no cover - already gone
        pass
    try:
        process.wait(timeout=5)
    except Exception:  # pragma: no cover - already reaped
        pass


def _map_git_error_text(raw: str) -> GitError:
    detail = redact(raw)
    lowered = detail.lower()
    if any(hint in lowered for hint in _AUTH_ERROR_HINTS):
        return GitError("auth_failed", detail=detail)
    if "stale info" in lowered:
        return GitError("remote_changed", detail=detail)
    if "non-fast-forward" in lowered or "fetch first" in lowered or "rejected" in lowered:
        return GitError("push_rejected", detail=detail)
    if any(hint in lowered for hint in _NETWORK_ERROR_HINTS):
        return GitError("network_error", message=_describe_network_failure(lowered), detail=detail)
    return GitError("unknown", detail=detail)


def _map_git_error(exc: Exception) -> GitError:
    return _map_git_error_text(getattr(exc, "stderr", "") or str(exc))


def _describe_network_failure(lowered: str) -> str:
    """Turn a transport failure into a message naming the actual culprit.

    One blanket "check your network or proxy" for DNS, TLS, proxy and timeout
    failures leaves the user nothing to act on — they all look alike from there.
    """

    if (
        "could not resolve host" in lowered
        or "name or service not known" in lowered
        or "temporary failure in name resolution" in lowered
    ):
        return "无法解析远端域名（DNS 失败）：请检查网络，或在「Git 设置 → 网络代理」里填写可用代理。"
    if "407" in lowered or "proxy" in lowered or "connect tunnel failed" in lowered:
        return "代理连接失败：请确认「Git 设置」里的代理地址与端口，或把代理模式改为直连后重试。"
    if "ssl" in lowered or "tls" in lowered or "certificate" in lowered or "schannel" in lowered:
        return "TLS/证书校验失败：公司网络或代理比较常见，请确认代理证书，或换一个网络重试。"
    if "timed out" in lowered:
        return "连接远端超时：网络被阻断或过慢，建议配置代理后重试。"
    if (
        "connection refused" in lowered
        or "connection reset" in lowered
        or "network is unreachable" in lowered
        or "failed to connect" in lowered
    ):
        return "无法连接远端：连接被拒绝或中断，请检查网络或代理设置。"
    if "early eof" in lowered or "rpc failed" in lowered or "remote end hung up" in lowered:
        return "传输中断：网络不稳定，可以稍后重试或改用代理。"
    return ERROR_MESSAGES["network_error"]


def _remote_update_decision(policy: str) -> str:
    """Normalize a policy value into ask / keep_local / keep_remote."""

    value = str(policy or "").strip().lower()
    return value if value in {"keep_local", "keep_remote"} else "ask"


def _remote_newer_report(
    *,
    steps: list[dict[str, object]],
    branch: str,
    remote_name: str,
    remote_url: str,
    committed: str,
    changes: int,
    relation: dict[str, int],
    remote_head: dict[str, str],
    proxy: Mapping[str, str],
) -> dict[str, object]:
    """The "the remote moved on, you decide" answer of :func:`sync_repository`."""

    return {
        "status": "pending_choice",
        "code": "remote_newer",
        "steps": steps,
        "commit": committed[:12],
        "pushed": False,
        "forced": False,
        "branch": branch,
        "remote": remote_name,
        "remote_url": redact(remote_url),
        "proxy": str(proxy.get("display") or ""),
        "proxy_source": str(proxy.get("source") or ""),
        "changed_files": changes,
        "relation": relation,
        "remote_head": remote_head,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "message": (
            f"远端 {remote_name}/{branch} 有 {relation['behind']} 个本地没有的提交，"
            "请选择保留远端版本还是本地版本。"
        ),
    }


def _resolve_working_branch(repo: GitRepository, configured: str) -> str:
    """配置的分支在本地不存在时退回仓库当前分支。

    ``plugin.toml`` 默认 main，而插件初始化之前的旧仓库可能停在 master 或
    其他分支上；这时仍按配置推送 ``refs/heads/main`` 只会得到一条 "src
    refspec does not match any" 的未知错误，退回当前分支才能真的完成同步。
    """

    branch = configured.strip() or repo.current_branch() or DEFAULT_BRANCH
    if branch and not repo.ref_exists(f"refs/heads/{branch}"):
        existing = repo.current_branch()
        if existing and existing != branch:
            return existing
    return branch


def sync_repository(repo: GitRepository, options: SyncOptions) -> dict[str, object]:
    """Commit local changes, compare with the remote, then push.

    When the remote carries commits we do not have, this stops and returns
    ``status="pending_choice"`` instead of merging on its own: which side wins
    is the user's call (see :func:`resolve_remote_newer`). Only the
    ``keep_local`` / ``keep_remote`` settings resolve it without asking.
    """

    if not repo.is_repo():
        raise GitError("not_initialized")

    branch = _resolve_working_branch(repo, options.branch)
    remote_name = options.remote_name.strip() or DEFAULT_REMOTE
    remote_url = repo.remote_url(remote_name)
    if not remote_url:
        raise GitError("remote_missing")

    proxy = resolve_git_proxy(options.proxy_url, options.proxy_mode)
    env = build_git_env(token=options.token, username=options.username, proxy=proxy)
    steps: list[dict[str, object]] = []
    committed = ""
    pulled = False
    pushed = False
    forced = False
    decision = ""
    relation: dict[str, int] = {"ahead": 0, "behind": 0}
    remote_head: dict[str, str] = {}

    changes = repo.change_count()
    if changes:
        repo.stage_all()
        committed = repo.commit(options.commit_message, author_name=options.author_name, author_email=options.author_email)
        steps.append({"id": "commit", "status": "ok", "detail": options.commit_message, "sha": committed[:12]})
    else:
        steps.append({"id": "commit", "status": "skipped", "detail": "没有需要提交的改动"})

    if options.pull_before_push:
        repo.fetch(remote_name, env=env, timeout=options.timeout)
        remote_ref = f"{remote_name}/{branch}"
        if repo.ref_exists(remote_ref):
            relation = repo.relation("HEAD", remote_ref)
            remote_head = repo.remote_head(remote_name, branch)
            if relation["behind"]:
                decision = _remote_update_decision(options.remote_update_policy)
                if decision == "ask":
                    steps.append(
                        {
                            "id": "remote",
                            "status": "pending",
                            "detail": f"远端 {remote_ref} 领先 {relation['behind']} 个提交",
                        }
                    )
                    return _remote_newer_report(
                        steps=steps,
                        branch=branch,
                        remote_name=remote_name,
                        remote_url=remote_url,
                        committed=committed,
                        changes=changes,
                        relation=relation,
                        remote_head=remote_head,
                        proxy=proxy,
                    )
                if decision == "keep_remote":
                    repo.reset_hard(remote_ref)
                    pulled = True
                    steps.append({"id": "resolve", "status": "ok", "detail": f"已用远端 {remote_ref} 覆盖本地"})
                else:
                    steps.append({"id": "resolve", "status": "ok", "detail": "按设置保留本地版本，随后覆盖远端"})
            else:
                steps.append({"id": "pull", "status": "skipped", "detail": "远端没有新的提交"})
        else:
            steps.append({"id": "pull", "status": "skipped", "detail": "远端分支尚不存在"})

    if repo.has_commits() and decision == "keep_local":
        repo.force_push(
            remote_name,
            branch,
            env=env,
            timeout=options.timeout,
            expected_sha=str(remote_head.get("sha") or ""),
        )
        repo.update_remote_tracking(remote_name, branch)
        pushed = True
        forced = True
        steps.append({"id": "push", "status": "ok", "detail": f"已强制覆盖 {remote_name}/{branch}"})
    elif repo.has_commits():
        set_upstream = not repo.has_upstream(branch)
        repo.push(remote_name, branch, env=env, set_upstream=set_upstream, timeout=options.timeout)
        pushed = True
        steps.append({"id": "push", "status": "ok", "detail": f"已推送到 {remote_name}/{branch}"})
    else:
        steps.append({"id": "push", "status": "skipped", "detail": "没有任何提交可以推送"})

    # ``pushed`` 只表示"执行过推送"，空提交的推送同样会成功；只有真的产生了
    # 新提交（本地改动）、接受了远端版本，或按设置覆盖了远端时，才算实际同步。
    synced = bool(committed or pulled or forced)
    return {
        "status": "ok" if synced else "up_to_date",
        "steps": steps,
        "commit": committed[:12],
        "pushed": pushed,
        "forced": forced,
        "branch": branch,
        "remote": remote_name,
        "remote_url": redact(remote_url),
        "proxy": str(proxy.get("display") or ""),
        "proxy_source": str(proxy.get("source") or ""),
        "changed_files": changes,
        "relation": relation,
        "remote_head": remote_head,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "message": "同步完成" if synced else "没有需要同步的改动",
    }


def resolve_remote_newer(repo: GitRepository, options: SyncOptions, choice: str) -> dict[str, object]:
    """Apply the user's answer to a ``pending_choice`` sync report.

    ``keep_remote`` throws the local version away (``reset --hard``), while
    ``keep_local`` overwrites the remote branch with the local history.
    """

    decision = _remote_update_decision(choice)
    if decision == "ask":
        raise GitError("unknown", message="请选择保留远端版本还是保留本地版本。")
    if not repo.is_repo():
        raise GitError("not_initialized")

    branch = _resolve_working_branch(repo, options.branch)
    remote_name = options.remote_name.strip() or DEFAULT_REMOTE
    remote_url = repo.remote_url(remote_name)
    if not remote_url:
        raise GitError("remote_missing")

    proxy = resolve_git_proxy(options.proxy_url, options.proxy_mode)
    env = build_git_env(token=options.token, username=options.username, proxy=proxy)
    remote_ref = f"{remote_name}/{branch}"
    repo.fetch(remote_name, env=env, timeout=options.timeout)
    if not repo.ref_exists(remote_ref):
        raise GitError("remote_missing", message=f"远端分支 {remote_ref} 不存在，请重新关联仓库。")

    remote_head = repo.remote_head(remote_name, branch)
    steps: list[dict[str, object]] = []
    forced = False
    if decision == "keep_remote":
        repo.reset_hard(remote_ref)
        steps.append({"id": "resolve", "status": "ok", "detail": f"本地已覆盖为 {remote_ref} 的版本"})
        message = "已用远端版本覆盖本地记忆。"
    else:
        if not repo.has_commits():
            raise GitError("unknown", message="本地还没有任何提交，无法覆盖远端。")
        repo.force_push(
            remote_name,
            branch,
            env=env,
            timeout=options.timeout,
            expected_sha=str(remote_head.get("sha") or ""),
        )
        repo.update_remote_tracking(remote_name, branch)
        forced = True
        steps.append({"id": "resolve", "status": "ok", "detail": f"已用本地版本覆盖 {remote_name}/{branch}"})
        message = "已用本地版本覆盖远端仓库。"

    return {
        "status": "ok",
        "resolution": decision,
        "steps": steps,
        "commit": "",
        "pushed": decision == "keep_local",
        "forced": forced,
        "branch": branch,
        "remote": remote_name,
        "remote_url": redact(remote_url),
        "proxy": str(proxy.get("display") or ""),
        "proxy_source": str(proxy.get("source") or ""),
        "changed_files": 0,
        "relation": repo.relation("HEAD", remote_ref),
        "remote_head": repo.remote_head(remote_name, branch),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
    }


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "DEFAULT_BRANCH",
    "DEFAULT_REMOTE",
    "GitError",
    "GitRepository",
    "SyncOptions",
    "build_git_env",
    "detect_git",
    "gitpython_available",
    "proxy_appears_in",
    "redact",
    "redact_proxy",
    "resolve_git_proxy",
    "resolve_remote_newer",
    "sync_repository",
    "timestamp",
    "web_url_from_remote",
]
