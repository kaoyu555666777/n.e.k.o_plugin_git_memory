"""Settings model, defaults and text helpers for the Git memory plugin."""

from __future__ import annotations

import platform
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

INTERVAL_OPTIONS: tuple[int, ...] = (5, 10, 30, 60)
DEFAULT_INTERVAL = 30
MIN_INTERVAL = 1
MAX_INTERVAL = 1440
MERGE_STRATEGIES: tuple[str, ...] = ("ours", "abort")
GITIGNORE_PRESETS: tuple[str, ...] = ("default", "minimal", "none")

GITIGNORE_TEMPLATES: dict[str, str] = {
    "default": (
        "# N.E.K.O 记忆目录的临时文件\n"
        "*.tmp\n"
        "*.log\n"
        "*.bak\n"
        "*~\n"
        "__pycache__/\n"
        ".DS_Store\n"
        "Thumbs.db\n"
        ".neko-sync/\n"
    ),
    "minimal": "# 只忽略同步过程中的临时文件\n*.tmp\n*.lock\n",
    "none": "",
}

_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,120}$")
_REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,60}$")


@dataclass
class GitMemorySettings:
    """User editable settings, mirrored by the ``[git_memory]`` config section."""

    memory_dir: str = ""
    remote_name: str = "origin"
    branch: str = "main"
    commit_message: str = "chore(memory): sync {timestamp}"
    author_name: str = ""
    author_email: str = ""
    provider: str = ""
    repository: str = ""
    provider_base_url: str = ""
    auto_sync_enabled: bool = False
    auto_sync_interval_minutes: int = DEFAULT_INTERVAL
    pull_before_push: bool = True
    merge_strategy: str = "ours"
    proxy_url: str = ""
    notify_on_error: bool = True
    gitignore_preset: str = "default"
    gitignore_extra: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any] | None) -> "GitMemorySettings":
        data = dict(section) if isinstance(section, Mapping) else {}
        defaults = cls()
        values: dict[str, Any] = {}
        for name, default in defaults.as_dict().items():
            raw = data.get(name, default)
            values[name] = _coerce(name, raw, default)
        return cls(**values)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "开启", "是"}:
            return True
        if lowered in {"0", "false", "no", "off", "关闭", "否"}:
            return False
    return default


def _as_text(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _coerce(name: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return _as_bool(value, default)
    if isinstance(default, int):
        return _as_int(value, default)
    return _as_text(value, default)


def validate_settings(settings: GitMemorySettings) -> tuple[GitMemorySettings, list[str]]:
    """Normalize a settings object and return ``(settings, warnings)``."""

    warnings: list[str] = []
    data = settings.as_dict()

    if not data["remote_name"] or not _REMOTE_PATTERN.match(data["remote_name"]):
        warnings.append("远端名称不合法，已恢复为 origin。")
        data["remote_name"] = "origin"
    if not data["branch"] or not _BRANCH_PATTERN.match(data["branch"]):
        warnings.append("分支名称不合法，已恢复为 main。")
        data["branch"] = "main"
    if not data["commit_message"]:
        warnings.append("提交信息模板为空，已恢复默认模板。")
        data["commit_message"] = GitMemorySettings().commit_message

    interval = data["auto_sync_interval_minutes"]
    if interval < MIN_INTERVAL or interval > MAX_INTERVAL:
        warnings.append("自动同步间隔超出范围，已恢复为 30 分钟。")
        data["auto_sync_interval_minutes"] = DEFAULT_INTERVAL

    if data["merge_strategy"] not in MERGE_STRATEGIES:
        warnings.append("冲突处理方式不支持，已恢复为 ours。")
        data["merge_strategy"] = "ours"
    if data["gitignore_preset"] not in GITIGNORE_PRESETS:
        warnings.append("gitignore 预设不支持，已恢复为 default。")
        data["gitignore_preset"] = "default"

    return GitMemorySettings(**data), warnings


def render_commit_message(template: str, *, changed_files: int = 0, extra: Mapping[str, Any] | None = None) -> str:
    """Expand the supported placeholders in the commit message template."""

    now = datetime.now()
    values: dict[str, Any] = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "hostname": platform.node() or "unknown-host",
        "count": changed_files,
    }
    if isinstance(extra, Mapping):
        values.update({str(key): value for key, value in extra.items()})

    text = template or GitMemorySettings().commit_message
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        # Unknown placeholder: keep the template as-is instead of failing the sync.
        return text


def build_gitignore(preset: str, extra: str = "") -> str:
    base = GITIGNORE_TEMPLATES.get(preset, GITIGNORE_TEMPLATES["default"])
    extra_text = extra.strip()
    if not extra_text:
        return base
    if not base:
        return f"{extra_text}\n"
    separator = "" if base.endswith("\n") else "\n"
    return f"{base}{separator}{extra_text}\n"


__all__ = [
    "DEFAULT_INTERVAL",
    "GITIGNORE_PRESETS",
    "GITIGNORE_TEMPLATES",
    "INTERVAL_OPTIONS",
    "MERGE_STRATEGIES",
    "GitMemorySettings",
    "build_gitignore",
    "render_commit_message",
    "validate_settings",
]
