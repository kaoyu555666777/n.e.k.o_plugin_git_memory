"""Operating-system detection and Git installation guidance.

The hosted UI needs to explain how to install Git on the machine that runs the
plugin process, so this module keeps every platform-specific string — download
links, package managers, distribution commands — in one place.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

GIT_DOWNLOAD_PAGE = "https://git-scm.com/downloads"
GIT_LINUX_PAGE = "https://git-scm.com/download/linux"
GIT_WINDOWS_PAGE = "https://git-scm.com/download/win"
GIT_MACOS_PAGE = "https://git-scm.com/download/mac"
OS_RELEASE_PATH = "/etc/os-release"

# (distro ids or id_like entries, package manager, install command)
_LINUX_DISTRIBUTIONS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        (
            "debian",
            "ubuntu",
            "linuxmint",
            "pop",
            "elementary",
            "zorin",
            "kali",
            "raspbian",
            "deepin",
            "uos",
            "devuan",
            "mx",
        ),
        "apt",
        "sudo apt update && sudo apt install -y git",
    ),
    (
        ("rhel", "centos", "rocky", "almalinux", "ol", "oracle", "scientific"),
        "dnf",
        "sudo dnf install -y git",
    ),
    (("fedora",), "dnf", "sudo dnf install -y git"),
    (
        ("opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles", "sled"),
        "zypper",
        "sudo zypper --non-interactive install git",
    ),
    (
        ("arch", "manjaro", "endeavouros", "garuda", "cachyos", "artix"),
        "pacman",
        "sudo pacman -S --needed --noconfirm git",
    ),
    (("alpine",), "apk", "sudo apk add --no-cache git"),
    (("gentoo", "funtoo"), "emerge", "sudo emerge --ask dev-vcs/git"),
    (("nixos",), "nix", "nix-env -iA nixos.git"),
    (("void",), "xbps", "sudo xbps-install -Sy git"),
    (("solus",), "eopkg", "sudo eopkg install git"),
)

_LINUX_FALLBACK_COMMANDS: tuple[dict[str, str], ...] = (
    {"label": "Debian / Ubuntu", "command": "sudo apt update && sudo apt install -y git"},
    {"label": "Fedora / RHEL", "command": "sudo dnf install -y git"},
    {"label": "Arch Linux", "command": "sudo pacman -S --needed git"},
    {"label": "openSUSE", "command": "sudo zypper install git"},
    {"label": "Alpine", "command": "sudo apk add --no-cache git"},
)


def read_os_release(path: str | Path = OS_RELEASE_PATH) -> dict[str, str]:
    """Parse ``/etc/os-release`` into a plain dict (empty when unavailable)."""

    values: dict[str, str] = {}
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip():
            values[key.strip()] = value
    return values


def _linux_distro() -> dict[str, str]:
    release = read_os_release()
    distro_id = (release.get("ID") or "").strip().lower()
    id_like = [item.strip().lower() for item in (release.get("ID_LIKE") or "").split() if item.strip()]
    return {
        "id": distro_id,
        "id_like": " ".join(id_like),
        "name": (release.get("NAME") or "").strip(),
        "pretty_name": (release.get("PRETTY_NAME") or "").strip(),
        "version": (release.get("VERSION_ID") or "").strip(),
        "codename": (release.get("VERSION_CODENAME") or "").strip(),
        "home_url": (release.get("HOME_URL") or "").strip(),
    }


def _match_linux_install(distro: dict[str, str]) -> tuple[str, str] | None:
    tokens = [distro.get("id", ""), *distro.get("id_like", "").split()]
    tokens = [token for token in tokens if token]
    for ids, manager, command in _LINUX_DISTRIBUTIONS:
        for token in tokens:
            if token in ids:
                return manager, command
    return None


def _windows_platform() -> dict[str, object]:
    release = platform.release() or ""
    version = platform.version() or ""
    return {
        "platform": "windows",
        "label": f"Windows {release}".strip(),
        "detail": version,
        "install": {
            "kind": "download",
            "links": [
                {"label": "Git for Windows 官方下载", "url": GIT_WINDOWS_PAGE},
                {
                    "label": "git-for-windows 最新版安装包",
                    "url": "https://github.com/git-for-windows/git/releases/latest",
                },
            ],
            "commands": [
                {"label": "winget", "command": "winget install --id Git.Git -e --source winget"},
                {"label": "scoop", "command": "scoop install git"},
            ],
            "notes": [
                "安装完成后需要重新打开 N.E.K.O，或重启插件，让新的 PATH 生效。",
                "安装时保持默认的 PATH 选项（Git from the command line）。",
            ],
        },
    }


def _macos_platform() -> dict[str, object]:
    mac_ver = platform.mac_ver()[0]
    return {
        "platform": "macos",
        "label": f"macOS {mac_ver}".strip(),
        "detail": platform.machine(),
        "install": {
            "kind": "download",
            "links": [
                {"label": "macOS 官方下载说明", "url": GIT_MACOS_PAGE},
                {"label": "Homebrew git 公式", "url": "https://formulae.brew.sh/formula/git"},
            ],
            "commands": [
                {"label": "Xcode 命令行工具", "command": "xcode-select --install"},
                {"label": "Homebrew", "command": "brew install git"},
            ],
            "notes": [
                "执行 xcode-select --install 后会弹出系统安装窗口，按提示完成即可。",
                "没有 Homebrew 时可以先安装它：https://brew.sh/",
            ],
        },
    }


def _linux_platform() -> dict[str, object]:
    distro = _linux_distro()
    match = _match_linux_install(distro)
    commands: list[dict[str, str]] = []
    manager = ""
    if match is not None:
        manager, command = match
        commands.append({"label": distro.get("pretty_name") or distro.get("id") or "当前发行版", "command": command})
    commands.extend(_LINUX_FALLBACK_COMMANDS)

    notes: list[str] = []
    if manager == "":
        notes.append("未识别出具体发行版，请按你的发行版选择上面任意一条命令。")
    if "microsoft" in (platform.release() or "").lower() or "wsl" in (platform.release() or "").lower():
        notes.append("检测到 WSL：这里的 memory 目录位于 Linux 子系统内，需要在这个发行版里安装 Git。")
    notes.append("卸载或升级 Git 前请先确认没有正在运行的同步任务。")

    label = distro.get("pretty_name") or distro.get("name") or "Linux"
    return {
        "platform": "linux",
        "label": label,
        "detail": f"{platform.machine()} · {distro.get('id', '')} {distro.get('version', '')}".strip(),
        "distro": distro,
        "manager": manager,
        "install": {
            "kind": "command",
            "links": [{"label": "Linux 安装说明", "url": GIT_LINUX_PAGE}],
            "commands": commands,
            "notes": notes,
        },
    }


def detect_platform() -> dict[str, object]:
    """Return a JSON-friendly description of the host platform."""

    system = sys.platform
    if system.startswith("win"):
        info = _windows_platform()
    elif system == "darwin":
        info = _macos_platform()
    else:
        info = _linux_platform()

    info.update(
        {
            "system": system,
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "download_page": GIT_DOWNLOAD_PAGE,
        }
    )
    return info


def install_summary(info: dict[str, object] | None = None) -> str:
    """Build a one-line, human readable install hint."""

    data = info if isinstance(info, dict) else detect_platform()
    install = data.get("install") if isinstance(data.get("install"), dict) else {}
    commands = install.get("commands") if isinstance(install.get("commands"), list) else []
    if commands and isinstance(commands[0], dict):
        return str(commands[0].get("command") or "")
    links = install.get("links") if isinstance(install.get("links"), list) else []
    if links and isinstance(links[0], dict):
        return str(links[0].get("url") or "")
    return GIT_DOWNLOAD_PAGE


__all__ = [
    "GIT_DOWNLOAD_PAGE",
    "detect_platform",
    "install_summary",
    "read_os_release",
]
