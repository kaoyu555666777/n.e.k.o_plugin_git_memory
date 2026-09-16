"""Plugin-private persistence.

* :class:`StateStore` keeps the JSON snapshot shown in the panel (detection
  results, last sync report).
* :class:`SecretStore` keeps access tokens Fernet-encrypted inside the plugin
  data directory, so they never land in the shared config file or in Git.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

MAX_TOKEN_LENGTH = 2048


class StorageError(RuntimeError):
    """Raised when plugin-private state could not be read or written."""


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class StateStore:
    """Small JSON document with atomic writes and a blocking-safe lock."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / "state.json"
        self._lock = threading.Lock()

    def load_sync(self) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

    def save_sync(self, state: Mapping[str, Any]) -> dict[str, Any]:
        document = dict(state)
        with self._lock:
            try:
                _atomic_write_bytes(
                    self.path,
                    json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            except OSError as exc:
                raise StorageError(f"无法写入插件状态文件：{exc}") from exc
        return document

    async def load(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.load_sync)

    async def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.save_sync, state)

    async def update(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        current = await self.load()
        current.update(dict(patch))
        return await self.save(current)


class SecretStore:
    """Fernet-encrypted per-provider access tokens."""

    def __init__(self, data_dir: str | Path, *, file_name: str = "git_accounts.bin") -> None:
        self._directory = Path(data_dir) / "secrets"
        self._payload_path = self._directory / file_name
        self._key_path = self._directory / f"{Path(file_name).stem}.key"
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def normalize_token(value: object) -> str:
        if not isinstance(value, str):
            return ""
        token = value.strip()
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return ""
        if any(character.isspace() or ord(character) < 32 for character in token):
            return ""
        return token

    # ------------------------------------------------------------------ reading
    def _load_sync(self) -> dict[str, dict[str, str]]:
        with self._lock:
            if not self._payload_path.is_file() or not self._key_path.is_file():
                return {}
            try:
                key = self._key_path.read_bytes()
                encrypted = self._payload_path.read_bytes()
                payload = json.loads(Fernet(key).decrypt(encrypted).decode("utf-8"))
            except (OSError, ValueError, UnicodeError, InvalidToken, json.JSONDecodeError):
                return {}
            if not isinstance(payload, dict):
                return {}
            accounts: dict[str, dict[str, str]] = {}
            for provider, entry in payload.items():
                if isinstance(provider, str) and isinstance(entry, dict):
                    accounts[provider] = {str(k): str(v) for k, v in entry.items() if isinstance(v, str)}
            return accounts

    def _save_sync(self, accounts: Mapping[str, Mapping[str, str]]) -> None:
        with self._lock:
            try:
                key = self._key_path.read_bytes() if self._key_path.is_file() else Fernet.generate_key()
                try:
                    fernet = Fernet(key)
                except ValueError:
                    key = Fernet.generate_key()
                    fernet = Fernet(key)
                payload = {provider: dict(entry) for provider, entry in accounts.items()}
                encrypted = fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                _atomic_write_bytes(self._key_path, key)
                _atomic_write_bytes(self._payload_path, encrypted)
            except OSError as exc:
                raise StorageError(f"无法保存访问令牌：{exc}") from exc

    async def load(self) -> dict[str, dict[str, str]]:
        return await asyncio.to_thread(self._load_sync)

    async def get(self, provider: str) -> dict[str, str]:
        accounts = await self.load()
        return accounts.get(str(provider or "").strip().lower(), {})

    async def save(
        self,
        provider: str,
        token: str,
        *,
        username: str = "",
        base_url: str = "",
    ) -> None:
        key = str(provider or "").strip().lower()
        normalized = self.normalize_token(token)
        if not key or not normalized:
            raise StorageError("访问令牌无效。")
        accounts = await self.load()
        accounts[key] = {
            "token": normalized,
            "username": str(username or "").strip(),
            "base_url": str(base_url or "").strip(),
        }
        await asyncio.to_thread(self._save_sync, accounts)

    async def clear(self, provider: str | None = None) -> None:
        accounts = await self.load()
        if provider is None:
            accounts = {}
        else:
            accounts.pop(str(provider).strip().lower(), None)
        if not accounts and not self._payload_path.exists():
            return
        await asyncio.to_thread(self._save_sync, accounts)


__all__ = ["MAX_TOKEN_LENGTH", "SecretStore", "StateStore", "StorageError"]
