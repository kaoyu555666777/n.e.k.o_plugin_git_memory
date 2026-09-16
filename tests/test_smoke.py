"""Manifest / surfaces / i18n consistency checks for the Git memory plugin."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "ui" / "panel.tsx"


def manifest() -> dict:
    return tomllib.loads((PLUGIN_ROOT / "plugin.toml").read_text(encoding="utf-8"))


def panel_source() -> str:
    return PANEL.read_text(encoding="utf-8")


def declared_entry_ids() -> set[str]:
    from plugin.plugins.git_memory import GitMemoryPlugin

    found: set[str] = set()
    for name in dir(GitMemoryPlugin):
        member = getattr(GitMemoryPlugin, name, None)
        meta = getattr(member, "__neko_event_meta__", None)
        if meta is not None and getattr(meta, "event_type", None) == "plugin_entry":
            entry_id = str(getattr(meta, "id", "") or name)
            found.add(entry_id)
    return found


def declared_ui_action_ids() -> set[str]:
    from plugin.plugins.git_memory import GitMemoryPlugin

    found: set[str] = set()
    for name in dir(GitMemoryPlugin):
        member = getattr(GitMemoryPlugin, name, None)
        meta = getattr(member, "__neko_ui_action__", None)
        action_id = meta.get("id") if isinstance(meta, dict) else getattr(meta, "id", None)
        if action_id:
            found.add(str(action_id))
    return found


def panel_action_references() -> set[str]:
    ids: set[str] = set()
    for match in re.finditer(r'(?:api\.call|can|busyLabel)\(\s*"([a-z0-9_]+)"', panel_source()):
        ids.add(match.group(1))
    return ids


def panel_i18n_keys() -> set[str]:
    return set(re.findall(r't\(\s*"((?:panel|plugin)\.[^"]+)"', panel_source()))


def test_plugin_manifest_declares_entry_and_surfaces() -> None:
    data = manifest()
    plugin = data["plugin"]
    assert plugin["id"] == "git_memory"
    assert plugin["entry"] == "plugin.plugins.git_memory:GitMemoryPlugin"
    assert plugin["i18n"]["default_locale"] == "zh-CN"
    assert plugin["i18n"]["locales_dir"] == "i18n"
    assert plugin["ui"]["enabled"] is True

    panels = plugin["ui"]["panel"]
    assert [panel["id"] for panel in panels] == ["main"]
    assert panels[0]["mode"] == "hosted-tsx"
    assert panels[0]["context"] == "dashboard"
    assert "state:read" in panels[0]["permissions"]
    assert "action:call" in panels[0]["permissions"]

    guides = plugin["ui"]["guide"]
    assert [guide["id"] for guide in guides] == ["quickstart"]

    runtime = data["plugin_runtime"]
    assert runtime["enabled"] is True and runtime["auto_start"] is True

    section = data["git_memory"]
    assert section["branch"] in {"main", "master"}
    assert 5 <= int(section["auto_sync_interval_minutes"]) <= 60


def test_surface_files_exist() -> None:
    data = manifest()
    for panel in data["plugin"]["ui"]["panel"]:
        assert (PLUGIN_ROOT / panel["entry"]).is_file(), panel["entry"]
    for guide in data["plugin"]["ui"]["guide"]:
        assert (PLUGIN_ROOT / guide["entry"]).is_file(), guide["entry"]
    assert PANEL.is_file()


def test_panel_actions_match_declared_entry_points() -> None:
    referenced = panel_action_references()
    declared = declared_entry_ids()
    assert referenced, "panel.tsx should call plugin entries through props.api.call"
    missing = sorted(referenced - declared)
    assert missing == [], f"panel references undeclared entries: {missing}"

    ui_actions = declared_ui_action_ids()
    assert ui_actions, "the plugin should expose hosted UI actions"
    assert ui_actions <= declared, f"UI actions without a plugin entry: {sorted(ui_actions - declared)}"


def test_panel_i18n_keys_are_translated() -> None:
    keys = panel_i18n_keys()
    assert len(keys) > 50
    for locale in ("zh-CN", "en"):
        bundle = json.loads((PLUGIN_ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        missing = sorted(key for key in keys if key not in bundle)
        assert missing == [], f"{locale}.json is missing panel keys: {missing}"


def test_i18n_locales_share_the_same_keys() -> None:
    zh = json.loads((PLUGIN_ROOT / "i18n" / "zh-CN.json").read_text(encoding="utf-8"))
    en = json.loads((PLUGIN_ROOT / "i18n" / "en.json").read_text(encoding="utf-8"))
    assert sorted(zh) == sorted(en)
    assert zh["plugin.name"] and en["plugin.name"]
