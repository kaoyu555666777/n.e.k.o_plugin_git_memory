"""Git Memory —— 用 GitPython 把 N.E.K.O 的 memory 目录同步到私有仓库。

功能概览：

* 插件启动时检测 Git / GitPython 是否可用，并识别操作系统与发行版。
* 检测 ``<软件存储目录>/memory`` 下是否已有 ``.git``，没有时引导初始化。
* 通过 GitHub / GitLab / Gitee 的访问令牌校验账号，选择或创建私有仓库并关联。
* 支持每 5 / 10 / 30 / 60 分钟自动同步，以及随时手动同步。
* 所有 Git 设置（分支、提交信息、作者、代理、.gitignore 等）都可以在面板中调整。
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
    ui,
)

from .git_backend import (
    GitError,
    GitRepository,
    SyncOptions,
    detect_git,
    sync_repository,
)
from .platform_info import detect_platform
from .providers import provider_options
from .settings import (
    GITIGNORE_PRESETS,
    INTERVAL_OPTIONS,
    GitMemorySettings,
    build_gitignore,
    render_commit_message,
    validate_settings,
)
from .storage import SecretStore, StateStore
from .ui_actions import GitMemoryUiMixin

SOURCE = "git_memory"
GIT_CHECK_TTL_SECONDS = 30.0
MAX_SCANNED_FILES = 5000
STATUS_ENTRY_TIMEOUT = 60.0
SYNC_ENTRY_TIMEOUT = 300.0
NETWORK_ENTRY_TIMEOUT = 150.0


@neko_plugin
class GitMemoryPlugin(GitMemoryUiMixin, NekoPluginBase):
    """Keep the memory directory in Git and push it to a private remote."""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._init_git_memory_state()

    # --------------------------------------------------------------- lifecycle
    def _init_git_memory_state(self) -> None:
        self._settings_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._settings = GitMemorySettings()
        self._settings_loaded = False
        self._accounts: dict[str, dict[str, str]] = {}
        self._accounts_loaded = False
        self._runtime_state: dict[str, Any] = {}
        self._git_info: dict[str, Any] = {}
        self._git_checked_monotonic = 0.0
        self._platform_info: dict[str, Any] = {}
        self._ui_token = secrets.token_urlsafe(32)
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._state_store = StateStore(self.data_path())
        self._secret_store = SecretStore(self.data_path())
        self._init_ui_actions()

    @lifecycle(id="startup")
    async def on_startup(self, **_: object):
        await self._ensure_loaded()
        git_info = await asyncio.to_thread(detect_git)
        platform_info = detect_platform()
        self._git_info = git_info
        self._git_checked_monotonic = _monotonic()
        self._platform_info = platform_info
        await self._state_store.update({"git": git_info, "platform": platform_info})

        status = "ready" if git_info.get("available") else str(git_info.get("error_code") or "git_missing")
        if git_info.get("available"):
            self.logger.info("[Git Memory] Git 可用：{}", git_info.get("version"))
        else:
            self.logger.warning("[Git Memory] Git 不可用：{}", git_info.get("error"))
        return Ok({"status": status, "git": git_info, "platform": platform_info.get("platform")})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_: object):
        return Ok({"status": "stopped"})

    # 自动同步的最小检查粒度固定为 60 秒：每分钟判断一次是否到点，
    # 真正的同步间隔（5 / 10 / 30 / 60 分钟）存在设置里。
    @timer_interval(id="git_memory_auto_sync", seconds=60, auto_start=True)
    async def auto_sync_tick(self):
        """每分钟检查一次是否到了自动同步的时间。"""

        try:
            await self._ensure_loaded()
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("[Git Memory] 自动同步读取配置失败：{}", exc)
            return

        settings = self._settings
        if not settings.auto_sync_enabled:
            return
        git_info = await self._git_info_cached()
        if not git_info.get("available"):
            return
        if not await asyncio.to_thread(self._is_repo_ready):
            return
        if not self._sync_due(settings):
            return
        await self._run_sync(trigger="auto")

    # ------------------------------------------------------------------ helpers
    def _storage_root(self) -> Path:
        """``<存储目录>/plugins/<plugin_id>`` 的上两级就是存储目录根。"""

        try:
            return Path(self.storage_dir).parents[1]
        except Exception:  # pragma: no cover - defensive
            return Path(self.data_path()).parents[2]

    def default_memory_dir(self) -> Path:
        return self._storage_root() / "memory"

    def memory_dir(self) -> Path:
        configured = self._settings.memory_dir.strip()
        if not configured:
            return self.default_memory_dir()
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = self._storage_root() / path
        return path

    async def _ensure_loaded(self) -> None:
        if self._settings_loaded and self._accounts_loaded and self._runtime_state:
            return
        config_section: dict[str, Any] = {}
        try:
            config = await self.config.dump()
        except Exception as exc:
            self.logger.warning("[Git Memory] 读取配置失败，使用默认值：{}", exc)
            config = {}
        if isinstance(config, dict):
            section = config.get("git_memory")
            if isinstance(section, dict):
                config_section = section
        settings, warnings = validate_settings(GitMemorySettings.from_mapping(config_section))
        for warning in warnings:
            self.logger.warning("[Git Memory] 配置警告：{}", warning)
        accounts = await self._secret_store.load()
        runtime_state = await self._state_store.load()
        with self._settings_lock:
            self._settings = settings
            self._settings_loaded = True
            self._accounts = accounts
            self._accounts_loaded = True
            self._runtime_state = runtime_state

    async def _reload_accounts(self) -> None:
        accounts = await self._secret_store.load()
        with self._settings_lock:
            self._accounts = accounts
            self._accounts_loaded = True

    async def _save_settings(self, settings: GitMemorySettings, patch: dict[str, Any]) -> None:
        validated, warnings = validate_settings(settings)
        for warning in warnings:
            self.logger.warning("[Git Memory] 配置警告：{}", warning)
        try:
            await self.config.update({"git_memory": patch})
        except Exception as exc:
            raise GitError("unknown", message=f"保存插件配置失败：{exc}") from exc
        with self._settings_lock:
            self._settings = validated
            self._settings_loaded = True

    async def _git_info_cached(self, *, force: bool = False) -> dict[str, Any]:
        now = _monotonic()
        cached = self._git_info
        if not force and cached and (now - self._git_checked_monotonic) < GIT_CHECK_TTL_SECONDS:
            return cached
        info = await asyncio.to_thread(detect_git)
        self._git_info = info
        self._git_checked_monotonic = now
        return info

    def _platform(self) -> dict[str, Any]:
        if not self._platform_info:
            self._platform_info = detect_platform()
        return self._platform_info

    def _is_repo_ready(self) -> bool:
        return GitRepository(self.memory_dir()).is_repo()

    def _auth_for(self, settings: GitMemorySettings) -> dict[str, str]:
        entry = self._accounts.get(settings.provider, {}) if settings.provider else {}
        return {
            "token": str(entry.get("token") or ""),
            "username": str(entry.get("username") or ""),
        }

    def _sync_due(self, settings: GitMemorySettings) -> bool:
        last_sync = self._runtime_state.get("last_sync") if isinstance(self._runtime_state, dict) else None
        finished_at = ""
        if isinstance(last_sync, dict):
            finished_at = str(last_sync.get("finished_at") or last_sync.get("at") or "")
        interval = max(1, int(settings.auto_sync_interval_minutes))
        if not finished_at:
            return True
        try:
            last = datetime.fromisoformat(finished_at)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last >= timedelta(minutes=interval)

    def _next_sync_at(self, settings: GitMemorySettings) -> str:
        last_sync = self._runtime_state.get("last_sync") if isinstance(self._runtime_state, dict) else None
        finished_at = str(last_sync.get("finished_at") or "") if isinstance(last_sync, dict) else ""
        if not finished_at:
            return ""
        try:
            last = datetime.fromisoformat(finished_at)
        except ValueError:
            return ""
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (last + timedelta(minutes=max(1, int(settings.auto_sync_interval_minutes)))).isoformat()

    # ------------------------------------------------------------ git snapshot
    def _repo_snapshot_sync(self, path: Path) -> dict[str, Any]:
        repo = GitRepository(path)
        snapshot = repo.status_snapshot()
        snapshot["memory_dir"] = str(path)
        snapshot["memory_exists"] = path.exists()
        snapshot["file_count"] = _count_files(path) if path.exists() else 0
        if snapshot.get("initialized"):
            snapshot["local_config"] = repo.local_config()
        return snapshot

    async def _build_snapshot(self) -> dict[str, Any]:
        await self._ensure_loaded()
        settings = self._settings
        git_info = await self._git_info_cached()
        platform_info = self._platform()
        memory_dir = self.memory_dir()
        repo_snapshot = await asyncio.to_thread(self._repo_snapshot_sync, memory_dir)
        auth_entry = self._accounts.get(settings.provider, {}) if settings.provider else {}
        last_sync = self._runtime_state.get("last_sync") if isinstance(self._runtime_state, dict) else None
        return {
            "ui_token": self._ui_token,
            "plugin": {
                "id": self.plugin_id,
                "started_at": self._started_at,
            },
            "paths": {
                "storage_root": str(self._storage_root()),
                "memory_dir": str(memory_dir),
                "default_memory_dir": str(self.default_memory_dir()),
                "memory_dir_custom": bool(settings.memory_dir.strip()),
            },
            "git": git_info,
            "os": platform_info,
            "repo": repo_snapshot,
            "auth": {
                "provider": settings.provider,
                "token_configured": bool(auth_entry.get("token")),
                "username": str(auth_entry.get("username") or ""),
                "base_url": str(auth_entry.get("base_url") or ""),
                "providers": provider_options(),
            },
            "sync": {
                "auto_sync_enabled": bool(settings.auto_sync_enabled),
                "interval_minutes": int(settings.auto_sync_interval_minutes),
                "interval_options": list(INTERVAL_OPTIONS),
                "running": self._sync_lock.locked(),
                "last_sync": last_sync if isinstance(last_sync, dict) else {},
                "next_sync_at": self._next_sync_at(settings) if settings.auto_sync_enabled else "",
            },
            "settings": settings.as_dict(),
            "gitignore_presets": list(GITIGNORE_PRESETS),
            "gitignore_preview": build_gitignore(settings.gitignore_preset, settings.gitignore_extra),
        }

    # ------------------------------------------------------------------ syncing
    async def _run_sync(self, *, trigger: str = "manual"):
        await self._ensure_loaded()
        settings = self._settings
        if not self._sync_lock.acquire(blocking=False):
            return Err(SdkError("已有同步任务正在进行，请稍后再试。", code="sync_in_progress"))
        try:
            return await self._perform_sync(settings, trigger=trigger)
        finally:
            self._sync_lock.release()

    async def _perform_sync(self, settings: GitMemorySettings, *, trigger: str):
        git_info = await self._git_info_cached()
        if not git_info.get("available"):
            return Err(
                SdkError(
                    str(git_info.get("error") or "Git 不可用。"),
                    code=str(git_info.get("error_code") or "git_missing"),
                )
            )
        memory_dir = self.memory_dir()
        repo = GitRepository(memory_dir)
        if not await asyncio.to_thread(repo.is_repo):
            return Err(SdkError("memory 目录还没有初始化 Git 仓库。", code="not_initialized"))
        if not settings.repository.strip() and not await asyncio.to_thread(repo.remote_url, settings.remote_name):
            return Err(SdkError("还没有关联远端仓库。", code="remote_missing"))

        auth = self._auth_for(settings)
        options = SyncOptions(
            remote_name=settings.remote_name,
            branch=settings.branch,
            commit_message=render_commit_message(settings.commit_message, changed_files=await asyncio.to_thread(repo.change_count)),
            author_name=settings.author_name,
            author_email=settings.author_email,
            pull_before_push=settings.pull_before_push,
            merge_strategy=settings.merge_strategy,
            proxy_url=settings.proxy_url,
            token=auth["token"],
            username=auth["username"],
        )
        try:
            report = await asyncio.to_thread(sync_repository, repo, options)
        except GitError as exc:
            await self._record_sync(trigger, {"status": "error", "code": exc.code, "message": str(exc), "detail": exc.detail})
            if trigger == "auto" and settings.notify_on_error:
                self._notify_user(f"记忆自动同步失败：{exc}")
            return Err(SdkError(str(exc), code=exc.code))
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.exception("[Git Memory] 同步出现未预期错误")
            await self._record_sync(trigger, {"status": "error", "code": "unknown", "message": str(exc), "detail": ""})
            return Err(SdkError(f"同步失败：{exc}", code="unknown"))

        report["trigger"] = trigger
        await self._record_sync(trigger, report)
        self.logger.info("[Git Memory] 同步完成：{}", report.get("message"))
        return Ok(report)

    async def _record_sync(self, trigger: str, report: dict[str, Any]) -> None:
        entry = dict(report)
        entry["trigger"] = trigger
        entry["finished_at"] = str(report.get("finished_at") or datetime.now(timezone.utc).isoformat())
        with self._settings_lock:
            self._runtime_state["last_sync"] = entry
        await self._state_store.update({"last_sync": entry})

    def _notify_user(self, text: str) -> None:
        try:
            self.push_message(
                source=SOURCE,
                visibility=["hud"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": text}],
            )
        except Exception as exc:  # pragma: no cover - notification is best effort
            self.logger.debug("[Git Memory] 推送提醒失败：{}", exc)

    # -------------------------------------------------------------- ui context
    @ui.context(id="dashboard", title=tr("panel.title", default="Git 记忆同步"))
    async def get_dashboard_context(self) -> dict[str, Any]:
        try:
            return await self._build_snapshot()
        except Exception as exc:  # pragma: no cover - keep the panel usable
            self.logger.exception("[Git Memory] 读取面板状态失败")
            return {
                "ui_token": self._ui_token,
                "error": f"读取插件状态失败：{exc}",
                "git": {"available": False, "error": str(exc), "error_code": "unknown"},
                "os": detect_platform(),
                "repo": {},
                "auth": {"providers": provider_options(), "provider": "", "token_configured": False},
                "sync": {"running": False, "interval_options": list(INTERVAL_OPTIONS), "last_sync": {}},
                "settings": GitMemorySettings().as_dict(),
                "paths": {"memory_dir": str(self.memory_dir())},
                "gitignore_presets": list(GITIGNORE_PRESETS),
            }

    # -------------------------------------------------------- model-facing API
    @ui.action(
        id="sync_memory_now",
        label=tr("actions.sync.label", default="立即同步"),
        icon="🔄",
        tone="primary",
        group="sync",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="sync_memory_now",
        name=tr("entry.sync.name", default="同步记忆到 Git"),
        description=tr(
            "entry.sync.description",
            default="把 memory 目录的改动提交并推送到已关联的 Git 私有仓库。",
        ),
        timeout=SYNC_ENTRY_TIMEOUT,
    )
    async def sync_memory_now(self, **_: object):
        return await self._run_sync(trigger="manual")

    @plugin_entry(
        id="memory_sync_status",
        name=tr("entry.status.name", default="查看记忆同步状态"),
        description=tr(
            "entry.status.description",
            default="返回 memory 目录当前的 Git 状态、远端配置与最近一次同步结果。",
        ),
        kind="service",
        timeout=STATUS_ENTRY_TIMEOUT,
    )
    async def memory_sync_status(self, **_: object):
        await self._ensure_loaded()
        settings = self._settings
        git_info = await self._git_info_cached()
        snapshot = await asyncio.to_thread(self._repo_snapshot_sync, self.memory_dir())
        last_sync = self._runtime_state.get("last_sync") if isinstance(self._runtime_state, dict) else None
        return Ok(
            {
                "git_available": bool(git_info.get("available")),
                "git_version": git_info.get("version", ""),
                "initialized": bool(snapshot.get("initialized")),
                "dirty": bool(snapshot.get("dirty")),
                "changed_files": int(snapshot.get("changed_files") or 0),
                "branch": snapshot.get("branch", ""),
                "remote_url": snapshot.get("remote_url", ""),
                "auto_sync_enabled": bool(settings.auto_sync_enabled),
                "interval_minutes": int(settings.auto_sync_interval_minutes),
                "last_sync": last_sync if isinstance(last_sync, dict) else {},
            }
        )


def _monotonic() -> float:
    import time

    return time.monotonic()


def _count_files(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.name == ".git":
                continue
            if path.is_file():
                total += 1
                if total >= MAX_SCANNED_FILES:
                    break
    except OSError:
        return total
    return total


__all__ = ["GitMemoryPlugin"]
