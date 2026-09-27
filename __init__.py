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
    proxy_appears_in,
    resolve_git_proxy,
    resolve_remote_newer,
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
# 同步线程的硬上限：入口超时是 300 秒，留出余量后即使 Git 被挂起也能把
# 结果回给面板，而不是让「同步中…」一直转下去。
SYNC_DEADLINE_SECONDS = 240.0


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
        # 远端有新版本时已经停下等用户决定，不必每分钟再问一次远端。
        if self._pending_choice():
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
                "remote_update_policy": str(settings.remote_update_policy),
                "proxy": self._proxy_info(settings),
                "running": self._sync_lock.locked(),
                "last_sync": last_sync if isinstance(last_sync, dict) else {},
                "next_sync_at": self._next_sync_at(settings) if settings.auto_sync_enabled else "",
                "pending_choice": self._pending_choice(),
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
        # 远端校验必须只看仓库里真实的 remote：settings.repository 只是上次
        # 选择过的仓库名，重新初始化或解除关联后它会留下来，用它放行就会把
        # 同步带进「没有远端却要 fetch/push」的死路。
        remote_url = await asyncio.to_thread(repo.remote_url, settings.remote_name)
        if not remote_url:
            return Err(
                SdkError(
                    "还没有关联远端仓库：请先在「账号与仓库」里选择或创建一个私有仓库，再点击同步。",
                    code="remote_missing",
                )
            )

        options = await self._build_sync_options(settings, repo)
        try:
            report = await asyncio.wait_for(
                asyncio.to_thread(sync_repository, repo, options),
                timeout=SYNC_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._record_sync(
                trigger,
                {
                    "status": "error",
                    "code": "timeout",
                    "message": "同步超过等待上限仍未完成，已放弃等待。",
                    "detail": "",
                },
            )
            if trigger == "auto" and settings.notify_on_error:
                self._notify_user("记忆自动同步超时，已放弃本次同步。")
            return Err(SdkError("同步超过等待上限仍未完成，已放弃等待。", code="timeout"))
        except GitError as exc:
            message = self._network_hint(exc, settings)
            proxy_info = self._proxy_info(settings)
            await self._record_sync(
                trigger,
                {
                    "status": "error",
                    "code": exc.code,
                    "message": message,
                    "detail": exc.detail,
                    "proxy": proxy_info["url"],
                    "proxy_source": proxy_info["source"],
                },
            )
            if trigger == "auto" and settings.notify_on_error:
                self._notify_user(f"记忆自动同步失败：{message}")
            return Err(SdkError(message, code=exc.code))
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.exception("[Git Memory] 同步出现未预期错误")
            await self._record_sync(trigger, {"status": "error", "code": "unknown", "message": str(exc), "detail": ""})
            return Err(SdkError(f"同步失败：{exc}", code="unknown"))

        if str(report.get("status") or "") == "pending_choice":
            newly_detected = await self._set_pending_choice(report, trigger=trigger)
            if trigger == "auto" and settings.notify_on_error and newly_detected:
                self._notify_user(str(report.get("message") or "远端仓库有更新的版本，等待你选择保留哪一版。"))
            return Ok(report)

        await self._clear_pending_choice()
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

    async def _build_sync_options(self, settings: GitMemorySettings, repo: GitRepository) -> SyncOptions:
        auth = self._auth_for(settings)
        return SyncOptions(
            remote_name=settings.remote_name,
            branch=settings.branch,
            commit_message=render_commit_message(
                settings.commit_message,
                changed_files=await asyncio.to_thread(repo.change_count),
            ),
            author_name=settings.author_name,
            author_email=settings.author_email,
            pull_before_push=settings.pull_before_push,
            remote_update_policy=settings.remote_update_policy,
            proxy_url=settings.proxy_url,
            proxy_mode=settings.proxy_mode,
            token=auth["token"],
            username=auth["username"],
        )

    # ------------------------------------------------------- 远端版本待决状态
    def _proxy_info(self, settings: GitMemorySettings) -> dict[str, str]:
        info = resolve_git_proxy(settings.proxy_url, settings.proxy_mode)
        return {
            "mode": str(settings.proxy_mode),
            "url": str(info.get("display") or ""),
            "source": str(info.get("source") or ""),
        }

    def _network_hint(self, exc: GitError, settings: GitMemorySettings) -> str:
        """Say which proxy the failing command actually used."""

        message = str(exc)
        if getattr(exc, "code", "") != "network_error":
            return message
        proxy = resolve_git_proxy(settings.proxy_url, settings.proxy_mode)
        display = str(proxy.get("display") or "")
        if not display:
            return f"{message}（当前未使用代理：可在「Git 设置」里填写代理地址）"
        if proxy_appears_in(getattr(exc, "detail", ""), display):
            return (
                f"代理连接失败（{display}）：git 连不上这个代理，"
                f"请确认地址与端口，或在「Git 设置 → 代理模式」里改成直连后重试。原始信息：{message}"
            )
        return f"{message}（本次使用的代理：{display}；若代理不可用，可在「Git 设置 → 代理模式」改为直连）"

    def _pending_choice(self) -> dict[str, Any]:
        value = self._runtime_state.get("pending_choice") if isinstance(self._runtime_state, dict) else None
        return dict(value) if isinstance(value, dict) else {}

    async def _set_pending_choice(self, report: dict[str, Any], *, trigger: str = "") -> bool:
        """Persist the "remote is newer, waiting for a decision" state.

        Returns ``True`` when this is a *new* remote head, so the auto-sync path
        notifies once per new version instead of once per tick.
        """

        head = report.get("remote_head") if isinstance(report.get("remote_head"), dict) else {}
        relation = report.get("relation") if isinstance(report.get("relation"), dict) else {}
        entry: dict[str, Any] = {
            "code": str(report.get("code") or "remote_newer"),
            "branch": str(report.get("branch") or ""),
            "remote": str(report.get("remote") or ""),
            "message": str(report.get("message") or ""),
            "relation": relation,
            "remote_head": head,
            "detected_at": str(report.get("finished_at") or datetime.now(timezone.utc).isoformat()),
            "trigger": trigger,
        }
        with self._settings_lock:
            previous = self._pending_choice()
            self._runtime_state["pending_choice"] = entry
        await self._state_store.update({"pending_choice": entry})
        previous_head = previous.get("remote_head") if isinstance(previous.get("remote_head"), dict) else {}
        return bool(previous_head.get("sha") != head.get("sha") or previous.get("branch") != entry["branch"])

    async def _clear_pending_choice(self) -> None:
        with self._settings_lock:
            removed = (
                bool(self._runtime_state.pop("pending_choice", None))
                if isinstance(self._runtime_state, dict)
                else False
            )
        if removed:
            await self._state_store.update({"pending_choice": {}})

    async def _resolve_remote_newer_choice(self, choice: str):
        """Run the user's keep_remote / keep_local decision."""

        await self._ensure_loaded()
        settings = self._settings
        if not self._sync_lock.acquire(blocking=False):
            return Err(SdkError("已有同步任务正在进行，请稍后再试。", code="sync_in_progress"))
        try:
            git_info = await self._git_info_cached()
            if not git_info.get("available"):
                return Err(
                    SdkError(
                        str(git_info.get("error") or "Git 不可用。"),
                        code=str(git_info.get("error_code") or "git_missing"),
                    )
                )
            repo = GitRepository(self.memory_dir())
            if not await asyncio.to_thread(repo.is_repo):
                return Err(SdkError("memory 目录还没有初始化 Git 仓库。", code="not_initialized"))
            if not await asyncio.to_thread(repo.remote_url, settings.remote_name):
                return Err(SdkError("还没有关联远端仓库。", code="remote_missing"))
            options = await self._build_sync_options(settings, repo)
            try:
                report = await asyncio.wait_for(
                    asyncio.to_thread(resolve_remote_newer, repo, options, choice),
                    timeout=SYNC_DEADLINE_SECONDS,
                )
            except asyncio.TimeoutError:
                return Err(SdkError("处理远端版本超过等待上限，请稍后重试。", code="timeout"))
            except GitError as exc:
                message = self._network_hint(exc, settings)
                await self._record_sync(
                    "resolve",
                    {"status": "error", "code": exc.code, "message": message, "detail": exc.detail},
                )
                return Err(SdkError(message, code=exc.code))
            await self._clear_pending_choice()
            report["trigger"] = "resolve"
            await self._record_sync("resolve", report)
            self.logger.info("[Git Memory] 远端版本处理完成：{}", report.get("message"))
            return Ok(report)
        finally:
            self._sync_lock.release()

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
            # 只比较 name 跳不过 .git 的内容：目录项本身不是文件不会计数，
            # 而 HEAD / objects / pack 的 name 都不是 ".git"，会把成千上万个
            # Git 内部文件全部算进去。必须按相对路径把整棵 .git 子树剪掉。
            if ".git" in path.relative_to(root).parts:
                continue
            if path.is_file():
                total += 1
                if total >= MAX_SCANNED_FILES:
                    break
    except OSError:
        return total
    return total


__all__ = ["GitMemoryPlugin"]
