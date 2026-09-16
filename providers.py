"""Git hosting providers (GitHub / GitLab / Gitee) used by the setup wizard.

The module owns three things:

* the static description of each provider (token page, scopes, API roots),
* an aiohttp client for validating tokens, listing and creating repositories,
* the mapping from a repository identifier to its clone / web URL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

from .git_backend import redact

USER_AGENT = "NEKO-GitMemory/0.1 (+https://project-neko.online)"
DEFAULT_TIMEOUT = 20.0
MAX_REPOSITORIES = 200


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    site_url: str
    api_base: str
    token_url: str
    token_scopes: tuple[str, ...]
    token_note: str
    signup_url: str
    docs_url: str
    supports_self_hosted: bool = False
    create_endpoint: str = ""
    list_endpoint: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "site_url": self.site_url,
            "token_url": self.token_url,
            "token_scopes": list(self.token_scopes),
            "token_note": self.token_note,
            "signup_url": self.signup_url,
            "docs_url": self.docs_url,
            "supports_self_hosted": self.supports_self_hosted,
        }


PROVIDERS: dict[str, ProviderSpec] = {
    "github": ProviderSpec(
        id="github",
        label="GitHub",
        site_url="https://github.com",
        api_base="https://api.github.com",
        token_url="https://github.com/settings/tokens/new?scopes=repo&description=NEKO%20Git%20Memory",
        token_scopes=("repo",),
        token_note="经典令牌需要勾选 repo；细粒度令牌需要 Contents 读写权限，创建仓库还需要 Administration 写权限。",
        signup_url="https://github.com/signup",
        docs_url="https://docs.github.com/zh/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens",
        create_endpoint="/user/repos",
        list_endpoint="/user/repos",
    ),
    "gitlab": ProviderSpec(
        id="gitlab",
        label="GitLab",
        site_url="https://gitlab.com",
        api_base="https://gitlab.com/api/v4",
        token_url="https://gitlab.com/-/user_settings/personal_access_tokens?name=NEKO%20Git%20Memory&scopes=api",
        token_scopes=("api",),
        token_note="个人访问令牌需要 api 范围（只勾 write_repository 时无法创建新仓库）。",
        signup_url="https://gitlab.com/users/sign_up",
        docs_url="https://docs.gitlab.com/user/profile/personal_access_tokens/",
        supports_self_hosted=True,
        create_endpoint="/projects",
        list_endpoint="/projects",
    ),
    "gitee": ProviderSpec(
        id="gitee",
        label="Gitee 码云",
        site_url="https://gitee.com",
        api_base="https://gitee.com/api/v5",
        token_url="https://gitee.com/profile/personal_access_tokens",
        token_scopes=("projects",),
        token_note="私人令牌需要勾选 projects（仓库读写）范围，否则无法创建或推送私有仓库。",
        signup_url="https://gitee.com/signup",
        docs_url="https://help.gitee.com/account/personal-access-token",
        supports_self_hosted=True,
        create_endpoint="/user/repos",
        list_endpoint="/user/repos",
    ),
}

ERROR_MESSAGES = {
    "invalid_token": "访问令牌无效或已过期。",
    "insufficient_scope": "访问令牌权限不足，请重新生成并勾选所需范围。",
    "not_found": "找不到对应的仓库或账号。",
    "conflict": "同名仓库已经存在。",
    "rate_limited": "触发平台限流，请稍后再试。",
    "network": "无法连接到代码托管平台，请检查网络或代理。",
    "invalid_response": "平台返回了无法解析的数据。",
    "unknown": "调用代码托管平台失败。",
}


class ProviderError(RuntimeError):
    def __init__(self, code: str = "unknown", *, message: str = "", detail: str = "") -> None:
        self.code = code if code in ERROR_MESSAGES else "unknown"
        self.detail = redact(detail)
        super().__init__(message or ERROR_MESSAGES[self.code])

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self), "detail": self.detail}


def provider_options() -> list[dict[str, object]]:
    return [spec.as_dict() for spec in PROVIDERS.values()]


def get_provider(provider_id: str) -> ProviderSpec:
    key = str(provider_id or "").strip().lower()
    if key not in PROVIDERS:
        raise ProviderError("unknown", message=f"不支持的平台：{provider_id}")
    return PROVIDERS[key]


def normalize_base_url(provider_id: str, base_url: str = "") -> str:
    spec = get_provider(provider_id)
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return spec.site_url
    if not value.startswith("http://") and not value.startswith("https://"):
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.netloc:
        raise ProviderError("unknown", message=f"站点地址无效：{base_url}")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _session_kwargs(url: str) -> dict[str, object]:
    try:
        from utils.aiohttp_proxy_utils import aiohttp_session_kwargs_for_url
    except Exception:  # pragma: no cover - host helper is optional
        return {"trust_env": True}
    try:
        return dict(aiohttp_session_kwargs_for_url(url))
    except Exception:  # pragma: no cover - defensive
        return {"trust_env": True}


@dataclass
class ProviderClient:
    provider_id: str
    token: str
    base_url: str = ""
    username: str = ""
    proxy_url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    spec: ProviderSpec = field(init=False)
    site_url: str = field(init=False)
    api_base: str = field(init=False)

    def __post_init__(self) -> None:
        self.spec = get_provider(self.provider_id)
        self.site_url = normalize_base_url(self.spec.id, self.base_url)
        if self.site_url == self.spec.site_url:
            self.api_base = self.spec.api_base
        else:
            # Self-hosted GitLab / Gitee expose the same API below the site root.
            suffix = "/api/v4" if self.spec.id == "gitlab" else "/api/v5"
            self.api_base = f"{self.site_url}{suffix}"

    # ----------------------------------------------------------------- requests
    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.spec.id == "github":
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        elif self.spec.id == "gitlab":
            headers["PRIVATE-TOKEN"] = self.token
        return headers

    def _params(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        merged: dict[str, Any] = dict(params or {})
        if self.spec.id == "gitee":
            merged["access_token"] = self.token
        return merged

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        url = f"{self.api_base}{path}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        kwargs = _session_kwargs(url)
        proxy = self.proxy_url.strip() or None
        try:
            async with aiohttp.ClientSession(timeout=timeout, **kwargs) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=self._params(params),
                    json=dict(json_body) if json_body is not None else None,
                    proxy=proxy,
                ) as response:
                    text = await response.text()
                    headers = dict(response.headers)
                    if response.status >= 400:
                        raise _map_status(response.status, text, headers)
                    if not text.strip():
                        return None, headers
                    try:
                        payload = await response.json(content_type=None)
                    except Exception as exc:
                        raise ProviderError("invalid_response", detail=str(exc)) from exc
                    return payload, headers
        except ProviderError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderError("network", detail=f"请求超时：{path}") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("network", detail=f"{type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            raise ProviderError("network", detail=str(exc)) from exc

    # ------------------------------------------------------------------ account
    async def current_user(self) -> dict[str, object]:
        payload, headers = await self._request("GET", "/user")
        if not isinstance(payload, Mapping):
            raise ProviderError("invalid_response")
        account, scopes = _normalize_account(self.spec.id, payload, headers, self.site_url)
        return {"account": account, "scopes": scopes, "warnings": _scope_warnings(self.spec, scopes)}

    async def list_repositories(self, *, query: str = "", limit: int = MAX_REPOSITORIES) -> list[dict[str, object]]:
        keyword = query.strip().lower()
        repositories: list[dict[str, object]] = []
        page = 1
        per_page = 100
        while len(repositories) < limit and page <= 5:
            path, params = _list_request(self.spec, page=page, per_page=per_page, query=query)
            payload, _headers = await self._request("GET", path, params=params)
            if not isinstance(payload, list):
                raise ProviderError("invalid_response")
            if not payload:
                break
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                normalized = _normalize_repository(self.spec.id, item, self.site_url)
                if normalized is None:
                    continue
                if keyword and keyword not in str(normalized.get("full_name", "")).lower():
                    continue
                repositories.append(normalized)
            if len(payload) < per_page:
                break
            page += 1
        repositories.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return repositories[:limit]

    async def create_repository(
        self,
        name: str,
        *,
        private: bool = True,
        description: str = "",
    ) -> dict[str, object]:
        repository_name = name.strip()
        if not repository_name:
            raise ProviderError("unknown", message="仓库名称不能为空。")
        body: dict[str, Any] = {"name": repository_name}
        if self.spec.id == "gitlab":
            body["path"] = repository_name
            body["visibility"] = "private" if private else "public"
        else:
            body["private"] = bool(private)
        if description.strip():
            body["description"] = description.strip()
        if self.spec.id != "gitlab":
            body["auto_init"] = False

        payload, _headers = await self._request("POST", self.spec.create_endpoint, json_body=body)
        if not isinstance(payload, Mapping):
            raise ProviderError("invalid_response")
        normalized = _normalize_repository(self.spec.id, payload, self.site_url)
        if normalized is None:
            raise ProviderError("invalid_response")
        return normalized

    # -------------------------------------------------------------- url helpers
    def remote_url(self, full_name: str) -> str:
        identifier = _normalize_identifier(self.spec.id, full_name, self.username)
        return f"{self.site_url}/{identifier}.git"

    def web_url(self, full_name: str) -> str:
        identifier = _normalize_identifier(self.spec.id, full_name, self.username)
        return f"{self.site_url}/{identifier}"


def _normalize_identifier(provider_id: str, full_name: str, username: str) -> str:
    value = str(full_name or "").strip().strip("/")
    if value.endswith(".git"):
        value = value[: -len(".git")]
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        value = parsed.path.strip("/")
    if value.startswith("git@"):
        value = value.split(":", 1)[-1]
    if provider_id == "gitlab" and value and "/" not in value and username:
        value = f"{username}/{value}"
    return value


def _list_request(
    spec: ProviderSpec,
    *,
    page: int,
    per_page: int,
    query: str,
) -> tuple[str, dict[str, Any]]:
    keyword = query.strip()
    if spec.id == "github":
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "sort": "updated",
            "affiliation": "owner,collaborator,organization_member",
        }
        if keyword:
            return "/user/repos", params
        return "/user/repos", params
    if spec.id == "gitlab":
        params = {
            "membership": "true",
            "simple": "true",
            "per_page": per_page,
            "page": page,
            "order_by": "last_activity_at",
            "min_access_level": 30,
        }
        if keyword:
            params["search"] = keyword
        return "/projects", params
    params = {
        "per_page": per_page,
        "page": page,
        "sort": "updated",
        "affiliation": "owner",
    }
    if keyword:
        params["q"] = keyword
    return "/user/repos", params


def _normalize_account(
    provider_id: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    site_url: str,
) -> tuple[dict[str, object], list[str]]:
    scopes: list[str] = []
    if provider_id == "github":
        raw_scope = headers.get("X-OAuth-Scopes") or headers.get("x-oauth-scopes") or ""
        scopes = [item.strip() for item in raw_scope.split(",") if item.strip()]
        account = {
            "login": str(payload.get("login") or ""),
            "name": str(payload.get("name") or payload.get("login") or ""),
            "avatar_url": str(payload.get("avatar_url") or ""),
            "html_url": str(payload.get("html_url") or site_url),
            "type": "classic" if scopes else "fine-grained",
        }
    elif provider_id == "gitlab":
        username = str(payload.get("username") or "")
        account = {
            "login": username,
            "name": str(payload.get("name") or username),
            "avatar_url": str(payload.get("avatar_url") or ""),
            "html_url": str(payload.get("web_url") or site_url),
            "type": "personal_access_token",
        }
    else:
        login = str(payload.get("login") or payload.get("name") or "")
        account = {
            "login": login,
            "name": str(payload.get("name") or login),
            "avatar_url": str(payload.get("avatar_url") or ""),
            "html_url": str(payload.get("html_url") or site_url),
            "type": "personal_access_token",
        }
    return account, scopes


def _scope_warnings(spec: ProviderSpec, scopes: list[str]) -> list[str]:
    if spec.id != "github" or not scopes:
        return []
    lowered = {item.lower() for item in scopes}
    if "repo" not in lowered and "*" not in lowered:
        return ["令牌缺少 repo 范围，可能无法创建或推送私有仓库。"]
    return []


def _normalize_repository(
    provider_id: str,
    payload: Mapping[str, Any],
    site_url: str,
) -> dict[str, object] | None:
    if provider_id == "github":
        full_name = str(payload.get("full_name") or "")
        clone_url = str(payload.get("clone_url") or (f"{site_url}/{full_name}.git" if full_name else ""))
        permissions = payload.get("permissions")
        can_push = bool(permissions.get("push")) if isinstance(permissions, Mapping) else True
        return {
            "full_name": full_name,
            "name": str(payload.get("name") or full_name),
            "private": bool(payload.get("private", False)),
            "clone_url": clone_url,
            "web_url": str(payload.get("html_url") or (f"{site_url}/{full_name}" if full_name else "")),
            "default_branch": str(payload.get("default_branch") or ""),
            "updated_at": str(payload.get("updated_at") or payload.get("pushed_at") or ""),
            "can_push": can_push,
        }
    if provider_id == "gitlab":
        path = str(payload.get("path_with_namespace") or "")
        return {
            "full_name": path,
            "name": str(payload.get("name") or path),
            "private": str(payload.get("visibility") or "private") != "public",
            "clone_url": str(payload.get("http_url_to_repo") or (f"{site_url}/{path}.git" if path else "")),
            "web_url": str(payload.get("web_url") or (f"{site_url}/{path}" if path else "")),
            "default_branch": str(payload.get("default_branch") or ""),
            "updated_at": str(payload.get("last_activity_at") or ""),
            "can_push": True,
        }
    full_name = str(payload.get("full_name") or payload.get("path") or "")
    permission = payload.get("permission")
    can_push = True
    if isinstance(permission, Mapping):
        can_push = bool(permission.get("push"))
    # Gitee 的 API 只返回 html_url（网页地址），把它当成 clone_url 会让 git push
    # 打到网页路径上，因此这里优先使用接口返回的克隆地址，其次按仓库名拼 https 地址。
    clone_url = str(payload.get("clone_url") or "")
    if not clone_url:
        clone_url = f"{site_url}/{full_name}.git" if full_name else str(payload.get("html_url") or "")
    return {
        "full_name": full_name,
        "name": str(payload.get("name") or full_name),
        "private": bool(payload.get("private", False)),
        "clone_url": clone_url,
        "web_url": str(payload.get("html_url") or (f"{site_url}/{full_name}" if full_name else "")),
        "default_branch": str(payload.get("default_branch") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "can_push": can_push,
    }


def _map_status(status: int, text: str, headers: Mapping[str, str]) -> ProviderError:
    detail = redact(text)[:500]
    if status in (401,):
        return ProviderError("invalid_token", detail=detail)
    if status in (403, 404) and "rate limit" in detail.lower():
        return ProviderError("rate_limited", detail=detail)
    if status == 403:
        return ProviderError("insufficient_scope", detail=detail)
    if status == 404:
        return ProviderError("not_found", detail=detail)
    if status == 429 or headers.get("Retry-After"):
        return ProviderError("rate_limited", detail=detail)
    if status in (400, 409, 422):
        lowered = detail.lower()
        if "already exist" in lowered or "has already been taken" in lowered or "已存在" in lowered:
            return ProviderError("conflict", detail=detail)
        return ProviderError("unknown", message=ERROR_MESSAGES["unknown"], detail=detail)
    return ProviderError("unknown", detail=f"HTTP {status}: {detail}")


def repository_clone_url(provider_id: str, full_name: str, base_url: str = "", username: str = "") -> str:
    client = ProviderClient(provider_id=provider_id, token="", base_url=base_url, username=username)
    return client.remote_url(full_name)


__all__ = [
    "PROVIDERS",
    "ProviderClient",
    "ProviderError",
    "ProviderSpec",
    "get_provider",
    "normalize_base_url",
    "provider_options",
    "repository_clone_url",
]
