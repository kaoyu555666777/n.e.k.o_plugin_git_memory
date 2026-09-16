"""GitPython based Git operations for the N.E.K.O memory directory.

Every network command runs with ``GIT_TERMINAL_PROMPT=0`` and an in-memory
``http.extraheader`` credential, so access tokens never end up in the
repository's ``.git/config`` and a missing credential fails fast instead of
opening an interactive prompt that no one can answer.
"""

from __future__ import annotations

import base64
import importlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def build_git_env(*, token: str = "", username: str = "", proxy_url: str = "") -> dict[str, str]:
    """Environment for one Git invocation (credentials never touch the disk)."""

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
    if token:
        account = username or "oauth2"
        encoded = base64.b64encode(f"{account}:{token}".encode("utf-8")).decode("ascii")
        config_pairs.append(("http.extraheader", f"Authorization: Basic {encoded}"))
    for index, (key, value) in enumerate(config_pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(len(config_pairs))

    if proxy_url.strip():
        proxy = proxy_url.strip()
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = env["http_proxy"] = env["https_proxy"] = proxy
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
    merge_strategy: str = "ours"
    proxy_url: str = ""
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
        repo = self.repository()
        command = ["git", *[str(item) for item in args]]
        kwargs: dict[str, Any] = {}
        if env is not None:
            kwargs["env"] = env
        if timeout is not None and not _is_windows():
            kwargs["kill_after_timeout"] = timeout
        try:
            output = repo.git.execute(command, **kwargs)
        except Exception as exc:
            raise _map_git_error(exc) from exc
        return output if isinstance(output, str) else str(output)

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


def _map_git_error(exc: Exception) -> GitError:
    detail = redact(getattr(exc, "stderr", "") or str(exc))
    lowered = detail.lower()
    if any(hint in lowered for hint in _AUTH_ERROR_HINTS):
        return GitError("auth_failed", detail=detail)
    if "non-fast-forward" in lowered or "fetch first" in lowered or "rejected" in lowered:
        return GitError("push_rejected", detail=detail)
    if any(hint in lowered for hint in _NETWORK_ERROR_HINTS):
        return GitError("network_error", detail=detail)
    return GitError("unknown", detail=detail)


def sync_repository(repo: GitRepository, options: SyncOptions) -> dict[str, object]:
    """Commit local changes, optionally pull, then push to the remote."""

    if not repo.is_repo():
        raise GitError("not_initialized")

    branch = options.branch.strip() or repo.current_branch() or DEFAULT_BRANCH
    remote_name = options.remote_name.strip() or DEFAULT_REMOTE
    remote_url = repo.remote_url(remote_name)
    if not remote_url:
        raise GitError("remote_missing")

    env = build_git_env(token=options.token, username=options.username, proxy_url=options.proxy_url)
    steps: list[dict[str, object]] = []
    committed = ""
    pulled = False
    pushed = False

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
            if relation["behind"]:
                repo.merge(remote_ref, strategy=options.merge_strategy, env=env, timeout=options.timeout)
                pulled = True
                steps.append({"id": "pull", "status": "ok", "detail": f"已合并远端 {remote_ref}"})
            else:
                steps.append({"id": "pull", "status": "skipped", "detail": "远端没有新的提交"})
        else:
            steps.append({"id": "pull", "status": "skipped", "detail": "远端分支尚不存在"})

    if repo.has_commits():
        set_upstream = not repo.has_upstream(branch)
        repo.push(remote_name, branch, env=env, set_upstream=set_upstream, timeout=options.timeout)
        pushed = True
        steps.append({"id": "push", "status": "ok", "detail": f"已推送到 {remote_name}/{branch}"})
    else:
        steps.append({"id": "push", "status": "skipped", "detail": "没有任何提交可以推送"})

    # ``pushed`` 只表示"执行过推送"，空提交的推送同样会成功；只有真的产生了
    # 新提交（本地改动）或合并了远端提交时，才算是完成了实际同步。
    synced = bool(committed or pulled)
    return {
        "status": "ok" if synced else "up_to_date",
        "steps": steps,
        "commit": committed[:12],
        "pushed": pushed,
        "branch": branch,
        "remote": remote_name,
        "remote_url": redact(remote_url),
        "changed_files": changes,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "message": "同步完成" if synced else "没有需要同步的改动",
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
    "redact",
    "sync_repository",
    "timestamp",
    "web_url_from_remote",
]
