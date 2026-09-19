"""Hosted-UI actions for the Git memory plugin.

Every action that can touch tokens, remotes or settings requires the
``ui_token`` that only the hosted panel receives, so the LLM and other plugins
cannot drive the setup wizard.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr, ui

from .git_backend import GitRepository
from .platform_info import detect_platform
from .providers import PROVIDERS, ProviderClient, ProviderError, get_provider, normalize_base_url
from .settings import GITIGNORE_PRESETS, GitMemorySettings, build_gitignore, validate_settings
from .storage import StorageError

UI_ONLY_MESSAGE = "该操作只能从插件面板调用。"
SETTINGS_KEYS = frozenset(GitMemorySettings().as_dict().keys())


class GitMemoryUiMixin:
    """Setup-wizard actions mixed into :class:`GitMemoryPlugin`."""

    def _init_ui_actions(self) -> None:
        self._ui_actions_ready = True

    # ------------------------------------------------------------------ guard
    def _valid_ui_token(self, candidate: object) -> bool:
        if not isinstance(candidate, str):
            return False
        try:
            return secrets.compare_digest(candidate, self._ui_token)
        except TypeError:  # pragma: no cover - defensive
            return False

    def _ui_guard(self, ui_token: object):
        if not self._valid_ui_token(ui_token):
            return Err(SdkError(UI_ONLY_MESSAGE, code="ui_only"))
        return None

    async def _snapshot(self) -> dict[str, Any]:
        return await self._build_snapshot()

    async def _apply_settings_patch(self, patch: dict[str, Any]):
        merged = {**self._settings.as_dict(), **patch}
        settings, warnings = validate_settings(GitMemorySettings.from_mapping(merged))
        await self._save_settings(settings, settings.as_dict())
        return settings, warnings

    async def _client_for(self, provider: str, base_url: str = "", timeout: float = 20.0) -> ProviderClient:
        provider_id = str(provider or self._settings.provider or "").strip().lower()
        if provider_id not in PROVIDERS:
            raise ProviderError("unknown", message="请先选择代码托管平台。")
        entry = self._accounts.get(provider_id, {})
        token = str(entry.get("token") or "")
        if not token:
            raise ProviderError("invalid_token", message="请先填写并校验访问令牌。")
        resolved_base = base_url.strip() or str(entry.get("base_url") or "") or self._settings.provider_base_url
        return ProviderClient(
            provider_id=provider_id,
            token=token,
            base_url=resolved_base,
            username=str(entry.get("username") or ""),
            proxy_url=self._settings.proxy_url,
            timeout=timeout,
        )

    # ------------------------------------------------------------- environment
    @ui.action(
        id="ui_check_environment",
        label=tr("actions.check.label", default="重新检测"),
        icon="🧪",
        tone="info",
        group="setup",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_check_environment",
        name=tr("entry.check.name", default="重新检测 Git 环境"),
        description=tr(
            "entry.check.description",
            default="重新检测 Git、操作系统与 memory 目录的仓库状态。",
        ),
        kind="service",
        timeout=60.0,
        input_schema={
            "type": "object",
            "properties": {"ui_token": {"type": "string", "writeOnly": True, "minLength": 1}},
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_check_environment(self, ui_token: str = "", **_: object):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        git_info = await self._git_info_cached(force=True)
        platform_info = detect_platform()
        self._platform_info = platform_info
        await self._state_store.update({"git": git_info, "platform": platform_info})
        return Ok({"git": git_info, "os": platform_info})

    # ------------------------------------------------------------------- setup
    @ui.action(
        id="ui_init_repository",
        label=tr("actions.init.label", default="初始化仓库"),
        icon="🌱",
        tone="primary",
        group="setup",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_init_repository",
        name=tr("entry.init.name", default="初始化 memory 仓库"),
        description=tr(
            "entry.init.description",
            default="在 memory 目录中执行 git init，并写入 .gitignore 与提交身份。",
        ),
        kind="service",
        timeout=120.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "branch": {"type": "string"},
                "author_name": {"type": "string"},
                "author_email": {"type": "string"},
                "gitignore_preset": {"type": "string", "enum": list(GITIGNORE_PRESETS)},
            },
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_init_repository(
        self,
        ui_token: str = "",
        branch: str = "",
        author_name: str = "",
        author_email: str = "",
        gitignore_preset: str = "",
        **_,
    ):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        git_info = await self._git_info_cached()
        if not git_info.get("available"):
            return Err(SdkError(str(git_info.get("error") or "Git 不可用。"), code=str(git_info.get("error_code") or "git_missing")))
        settings = self._settings
        target_branch = (branch.strip() or settings.branch or "main")
        author = author_name.strip() or settings.author_name
        email = author_email.strip() or settings.author_email
        preset = gitignore_preset.strip() or settings.gitignore_preset
        try:
            repo = await asyncio.to_thread(
                GitRepository.initialize,
                self.memory_dir(),
                branch=target_branch,
                author_name=author,
                author_email=email,
            )
        except Exception as exc:
            return Err(SdkError(f"初始化 Git 仓库失败：{exc}", code="init_failed"))

        ignore_written = False
        ignore_text = build_gitignore(preset, settings.gitignore_extra)
        if ignore_text.strip():
            ignore_written = await asyncio.to_thread(repo.write_gitignore, ignore_text)

        settings, _warnings = await self._apply_settings_patch(
            {
                "branch": target_branch,
                "author_name": author,
                "author_email": email,
                "gitignore_preset": preset,
            }
        )
        return Ok(
            {
                "status": "initialized",
                "memory_dir": str(self.memory_dir()),
                "branch": target_branch,
                "gitignore_written": ignore_written,
                "settings": settings.as_dict(),
                "snapshot": await self._snapshot(),
            }
        )

    # ------------------------------------------------------------------- token
    @ui.action(
        id="ui_save_token",
        label=tr("actions.token.label", default="校验并保存令牌"),
        icon="🔑",
        tone="primary",
        group="auth",
        order=30,
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_save_token",
        name=tr("entry.token.name", default="校验并保存访问令牌"),
        description=tr(
            "entry.token.description",
            default="使用访问令牌调用代码托管平台接口校验账号，并加密保存到插件私有目录。",
        ),
        kind="service",
        timeout=90.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "provider": {"type": "string", "enum": list(PROVIDERS)},
                "token": {"type": "string", "writeOnly": True, "minLength": 8},
                "base_url": {"type": "string"},
            },
            "required": ["ui_token", "provider", "token"],
            "additionalProperties": False,
        },
    )
    async def ui_save_token(
        self,
        ui_token: str = "",
        provider: str = "",
        token: str = "",
        base_url: str = "",
        **_,
    ):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        provider_id = str(provider or "").strip().lower()
        try:
            spec = get_provider(provider_id)
            resolved_base = normalize_base_url(provider_id, base_url)
        except ProviderError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        client = ProviderClient(
            provider_id=provider_id,
            token=token,
            base_url=resolved_base,
            proxy_url=self._settings.proxy_url,
            timeout=25.0,
        )
        try:
            result = await client.current_user()
        except ProviderError as exc:
            return Err(SdkError(str(exc), code=exc.code))

        account = result.get("account") if isinstance(result.get("account"), dict) else {}
        try:
            await self._secret_store.save(
                provider_id,
                token,
                username=str(account.get("login") or ""),
                base_url="" if resolved_base == spec.site_url else resolved_base,
            )
        except StorageError as exc:
            return Err(SdkError(str(exc), code="storage_error"))
        await self._reload_accounts()
        await self._apply_settings_patch(
            {
                "provider": provider_id,
                "provider_base_url": "" if resolved_base == spec.site_url else resolved_base,
            }
        )
        return Ok(
            {
                "status": "validated",
                "provider": provider_id,
                "account": account,
                "scopes": result.get("scopes") or [],
                "warnings": result.get("warnings") or [],
            }
        )

    @ui.action(
        id="ui_clear_token",
        label=tr("actions.clearToken.label", default="清除令牌"),
        icon="🧹",
        tone="danger",
        group="auth",
        order=90,
        confirm=tr("actions.clearToken.confirm", default="确定要删除本插件保存的访问令牌吗？"),
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_clear_token",
        name=tr("entry.clearToken.name", default="清除访问令牌"),
        description=tr("entry.clearToken.description", default="删除插件私有目录中保存的访问令牌。"),
        kind="service",
        timeout=30.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "provider": {"type": "string"},
            },
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_clear_token(self, ui_token: str = "", provider: str = "", **_):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        target = str(provider or self._settings.provider or "").strip().lower() or None
        try:
            await self._secret_store.clear(target)
        except StorageError as exc:
            return Err(SdkError(str(exc), code="storage_error"))
        await self._reload_accounts()
        return Ok({"status": "cleared", "provider": target or "all"})

    # -------------------------------------------------------------- repository
    @ui.action(
        id="ui_list_repositories",
        label=tr("actions.list.label", default="读取仓库列表"),
        icon="📚",
        tone="info",
        group="repository",
        order=40,
        refresh_context=False,
    )
    @plugin_entry(
        id="ui_list_repositories",
        name=tr("entry.list.name", default="列出账号仓库"),
        description=tr("entry.list.description", default="列出访问令牌可写的仓库，供用户选择关联。"),
        kind="service",
        timeout=90.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "provider": {"type": "string"},
                "base_url": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_list_repositories(
        self,
        ui_token: str = "",
        provider: str = "",
        base_url: str = "",
        query: str = "",
        **_,
    ):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        try:
            client = await self._client_for(provider, base_url, timeout=30.0)
        except ProviderError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        try:
            repositories = await client.list_repositories(query=query)
        except ProviderError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        return Ok({"provider": client.provider_id, "count": len(repositories), "repositories": repositories})

    @ui.action(
        id="ui_connect_repository",
        label=tr("actions.connect.label", default="关联并同步"),
        icon="🔗",
        tone="success",
        group="repository",
        order=50,
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_connect_repository",
        name=tr("entry.connect.name", default="关联已有仓库"),
        description=tr(
            "entry.connect.description",
            default="把 memory 目录关联到选定的已有仓库，并立即执行一次同步。",
        ),
        kind="service",
        timeout=300.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "repository": {"type": "string", "minLength": 1},
                "clone_url": {"type": "string"},
                "branch": {"type": "string"},
            },
            "required": ["ui_token", "repository"],
            "additionalProperties": False,
        },
    )
    async def ui_connect_repository(
        self,
        ui_token: str = "",
        repository: str = "",
        clone_url: str = "",
        branch: str = "",
        **_,
    ):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        if not await asyncio.to_thread(self._is_repo_ready):
            return Err(SdkError("请先在 memory 目录初始化 Git 仓库。", code="not_initialized"))
        try:
            client = await self._client_for("", "")
        except ProviderError as exc:
            return Err(SdkError(str(exc), code=exc.code))

        settings = self._settings
        remote_url = clone_url.strip() or client.remote_url(repository)
        repo = GitRepository(self.memory_dir())
        try:
            await asyncio.to_thread(repo.set_remote, settings.remote_name, remote_url)
        except Exception as exc:
            return Err(SdkError(f"设置远端失败：{exc}", code="remote_failed"))

        patch: dict[str, Any] = {"repository": repository.strip()}
        if branch.strip():
            patch["branch"] = branch.strip()
        await self._apply_settings_patch(patch)
        result = await self._run_sync(trigger="manual")
        if isinstance(result, Ok):
            payload = dict(result.value) if isinstance(result.value, dict) else {"status": "ok"}
            payload["remote_url"] = remote_url
            payload["web_url"] = client.web_url(repository)
            return Ok(payload)
        return result

    @ui.action(
        id="ui_create_repository",
        label=tr("actions.create.label", default="创建私有仓库"),
        icon="✨",
        tone="success",
        group="repository",
        order=60,
        confirm=tr("actions.create.confirm", default="将在你的账号下创建一个私有仓库并关联当前 memory 目录，继续吗？"),
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_create_repository",
        name=tr("entry.create.name", default="创建私有仓库并关联"),
        description=tr(
            "entry.create.description",
            default="在选定的代码托管平台上创建私有仓库，关联 memory 目录并立即同步。",
        ),
        kind="service",
        timeout=300.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "private": {"type": "boolean", "default": True},
            },
            "required": ["ui_token", "name"],
            "additionalProperties": False,
        },
    )
    async def ui_create_repository(
        self,
        ui_token: str = "",
        name: str = "",
        description: str = "",
        private: bool = True,
        **_,
    ):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        if not await asyncio.to_thread(self._is_repo_ready):
            return Err(SdkError("请先在 memory 目录初始化 Git 仓库。", code="not_initialized"))
        try:
            client = await self._client_for("", "", timeout=45.0)
        except ProviderError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        try:
            created = await client.create_repository(name, private=bool(private), description=description)
        except ProviderError as exc:
            return Err(SdkError(str(exc), code=exc.code))

        full_name = str(created.get("full_name") or name)
        remote_url = str(created.get("clone_url") or client.remote_url(full_name))
        repo = GitRepository(self.memory_dir())
        try:
            await asyncio.to_thread(repo.set_remote, self._settings.remote_name, remote_url)
        except Exception as exc:
            return Err(SdkError(f"设置远端失败：{exc}", code="remote_failed"))

        await self._apply_settings_patch({"repository": full_name})
        result = await self._run_sync(trigger="manual")
        if isinstance(result, Ok):
            payload = dict(result.value) if isinstance(result.value, dict) else {"status": "ok"}
            payload["repository"] = created
            return Ok(payload)
        return result

    @ui.action(
        id="ui_disconnect_repository",
        label=tr("actions.disconnect.label", default="解除关联"),
        icon="⛓️‍💥",
        tone="warning",
        group="repository",
        order=70,
        confirm=tr("actions.disconnect.confirm", default="将删除 memory 仓库上的远端关联（本地文件与提交历史保留），继续吗？"),
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_disconnect_repository",
        name=tr("entry.disconnect.name", default="解除远端关联"),
        description=tr(
            "entry.disconnect.description",
            default="移除 memory 仓库的远端配置，本地提交历史与文件保持不变。",
        ),
        kind="service",
        timeout=60.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "remove_token": {"type": "boolean", "default": False},
            },
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_disconnect_repository(self, ui_token: str = "", remove_token: bool = False, **_):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        settings = self._settings
        removed = False
        repo = GitRepository(self.memory_dir())
        if await asyncio.to_thread(repo.is_repo):
            removed = await asyncio.to_thread(repo.remove_remote, settings.remote_name)
        await self._apply_settings_patch({"repository": ""})
        cleared = False
        if remove_token and settings.provider:
            try:
                await self._secret_store.clear(settings.provider)
                await self._reload_accounts()
                cleared = True
            except StorageError as exc:
                return Err(SdkError(str(exc), code="storage_error"))
        return Ok({"status": "disconnected", "remote_removed": removed, "token_cleared": cleared})

    # ---------------------------------------------------------------- settings
    @ui.action(
        id="ui_save_settings",
        label=tr("actions.saveSettings.label", default="保存设置"),
        icon="💾",
        tone="primary",
        group="settings",
        order=80,
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_save_settings",
        name=tr("entry.saveSettings.name", default="保存 Git 同步设置"),
        description=tr(
            "entry.saveSettings.description",
            default="保存分支、提交信息模板、作者、代理、冲突策略与自动同步间隔等设置。",
        ),
        kind="service",
        timeout=60.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "settings": {"type": "object"},
            },
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_save_settings(self, ui_token: str = "", settings: dict[str, Any] | None = None, **_):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        patch = {key: value for key, value in (settings or {}).items() if key in SETTINGS_KEYS}
        if not patch:
            return Err(SdkError("没有需要保存的设置项。", code="empty_patch"))
        new_settings, warnings = await self._apply_settings_patch(patch)
        return Ok({"status": "saved", "warnings": warnings, "settings": new_settings.as_dict()})

    @ui.action(
        id="ui_set_auto_sync",
        label=tr("actions.autoSync.label", default="自动同步开关"),
        icon="⏱️",
        tone="info",
        group="sync",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_set_auto_sync",
        name=tr("entry.autoSync.name", default="设置自动同步"),
        description=tr(
            "entry.autoSync.description",
            default="开启或关闭自动同步，并设置 5 / 10 / 30 / 60 分钟的同步间隔。",
        ),
        kind="service",
        timeout=60.0,
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
                "enabled": {"type": "boolean"},
                "interval_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            },
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_set_auto_sync(
        self,
        ui_token: str = "",
        enabled: bool | None = None,
        interval_minutes: int | None = None,
        **_,
    ):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        await self._ensure_loaded()
        patch: dict[str, Any] = {}
        if enabled is not None:
            patch["auto_sync_enabled"] = bool(enabled)
        if interval_minutes is not None:
            patch["auto_sync_interval_minutes"] = int(interval_minutes)
        if not patch:
            return Err(SdkError("没有需要保存的自动同步设置。", code="empty_patch"))
        new_settings, warnings = await self._apply_settings_patch(patch)
        return Ok(
            {
                "status": "saved",
                "warnings": warnings,
                "auto_sync_enabled": bool(new_settings.auto_sync_enabled),
                "interval_minutes": int(new_settings.auto_sync_interval_minutes),
            }
        )

    # --------------------------------------------------------- 远端版本决策
    @ui.action(
        id="ui_keep_remote_version",
        label=tr("actions.keepRemote.label", default="保留远端版本"),
        icon="⬇️",
        tone="warning",
        group="sync",
        order=30,
        confirm=tr(
            "actions.keepRemote.confirm",
            default="将用远端仓库的版本覆盖本地 memory 目录，本地尚未推送的提交会被丢弃，继续吗？",
        ),
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_keep_remote_version",
        name=tr("entry.keepRemote.name", default="保留远端记忆版本"),
        description=tr(
            "entry.keepRemote.description",
            default="用远端仓库的版本覆盖本地 memory 目录。",
        ),
        kind="service",
        timeout=300.0,
        input_schema={
            "type": "object",
            "properties": {"ui_token": {"type": "string", "writeOnly": True, "minLength": 1}},
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_keep_remote_version(self, ui_token: str = "", **_: object):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        return await self._resolve_remote_newer_choice("keep_remote")

    @ui.action(
        id="ui_keep_local_version",
        label=tr("actions.keepLocal.label", default="保留本地版本"),
        icon="⬆️",
        tone="warning",
        group="sync",
        order=40,
        confirm=tr(
            "actions.keepLocal.confirm",
            default="将用本地 memory 目录的版本覆盖远端仓库，远端上别人推送的提交会被丢弃，继续吗？",
        ),
        refresh_context=True,
    )
    @plugin_entry(
        id="ui_keep_local_version",
        name=tr("entry.keepLocal.name", default="保留本地记忆版本"),
        description=tr(
            "entry.keepLocal.description",
            default="用本地 memory 目录的版本覆盖远端仓库。",
        ),
        kind="service",
        timeout=300.0,
        input_schema={
            "type": "object",
            "properties": {"ui_token": {"type": "string", "writeOnly": True, "minLength": 1}},
            "required": ["ui_token"],
            "additionalProperties": False,
        },
    )
    async def ui_keep_local_version(self, ui_token: str = "", **_: object):
        guard = self._ui_guard(ui_token)
        if guard is not None:
            return guard
        return await self._resolve_remote_newer_choice("keep_local")


__all__ = ["GitMemoryUiMixin", "SETTINGS_KEYS"]
