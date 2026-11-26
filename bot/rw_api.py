"""Клиент Remnawave API (https://docs.rw/api).

Авторизация: API-токен (Bearer) или логин/пароль админа → JWT через /api/auth/login
(кешируется, при 401 перелогин).
"""
import httpx

from . import crypto, db


class RwError(Exception):
    pass


_JWT_CACHE: dict[str, str] = {}


class RW:
    def __init__(self, base_url: str, token: str | None, cookie: tuple[str, str] | None = None,
                 username: str | None = None, password: str | None = None):
        if not token and not (username and password):
            raise RwError("У панели не задан доступ к API: ни токена, ни логина/пароля (🔑 Доступ к API)")
        self.base = base_url.rstrip("/")
        self.token, self.username, self.password = token, username, password
        self.cache_key = f"{self.base}|{username}"
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"}
        cookies = {cookie[0]: cookie[1]} if cookie else None
        if cookie:  # секретный путь пропускает и по заголовку
            headers["X-Api-Key"] = cookie[1]
        self.c = httpx.AsyncClient(base_url=self.base, headers=headers, cookies=cookies, timeout=30)

    async def login(self) -> str:
        try:
            r = await self.c.post("/api/auth/login", json={"username": self.username, "password": self.password})
        except httpx.HTTPError as e:
            raise RwError(f"login: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise RwError(f"login → {r.status_code}: {r.text[:300]}")
        jwt = (r.json().get("response") or {}).get("accessToken")
        if not jwt:
            raise RwError(f"login: нет accessToken в ответе ({r.text[:200]})")
        _JWT_CACHE[self.cache_key] = jwt
        return jwt

    async def _auth(self, relogin: bool = False) -> str:
        if self.token and not relogin:
            return self.token
        if not (self.username and self.password):
            return self.token
        if relogin or self.cache_key not in _JWT_CACHE:
            return await self.login()
        return _JWT_CACHE[self.cache_key]

    async def _req(self, method: str, path: str, **kw):
        for attempt in (0, 1):
            bearer = await self._auth(relogin=attempt == 1)
            try:
                r = await self.c.request(method, path, headers={"Authorization": f"Bearer {bearer}"}, **kw)
            except httpx.HTTPError as e:
                raise RwError(f"{method} {path}: {type(e).__name__}: {e}") from e
            # токен протух/неверный, но есть логин — пробуем через JWT
            if r.status_code in (401, 403) and attempt == 0 and self.username and self.password:
                self.token = None
                continue
            break
        if r.status_code >= 400:
            raise RwError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        if not r.content:
            return {}
        try:
            data = r.json()
        except ValueError as e:
            raise RwError(f"{method} {path}: не JSON ({r.text[:200]})") from e
        return data.get("response", data) if isinstance(data, dict) else data

    @staticmethod
    def _list(resp, key: str):
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            if isinstance(resp.get(key), list):
                return resp[key]
            for v in resp.values():
                if isinstance(v, list):
                    return v
        return []

    async def close(self):
        await self.c.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # --- система / токены
    async def keygen(self) -> str:
        return (await self._req("GET", "/api/keygen"))["pubKey"]

    async def create_api_token(self, name: str = "vpn-master") -> str:
        """Создать API-токен (нужен вход по логину/паролю)."""
        if not (self.username and self.password):
            raise RwError("для создания API-токена нужен логин/пароль админа")
        self.token = None
        r = await self._req("POST", "/api/tokens", json={"tokenName": name})
        return r["token"]

    # --- профили
    async def profiles(self):
        return self._list(await self._req("GET", "/api/config-profiles"), "configProfiles")

    async def profile(self, uuid: str):
        return await self._req("GET", f"/api/config-profiles/{uuid}")

    async def create_profile(self, name: str, config: dict):
        return await self._req("POST", "/api/config-profiles", json={"name": name, "config": config})

    async def update_profile(self, uuid: str, config: dict):
        return await self._req("PATCH", "/api/config-profiles", json={"uuid": uuid, "config": config})

    # --- сквады
    async def squads(self):
        return self._list(await self._req("GET", "/api/internal-squads"), "internalSquads")

    async def create_squad(self, name: str, inbounds: list[str]):
        return await self._req("POST", "/api/internal-squads", json={"name": name, "inbounds": inbounds})

    async def update_squad(self, uuid: str, inbounds: list[str]):
        return await self._req("PATCH", "/api/internal-squads", json={"uuid": uuid, "inbounds": inbounds})

    # --- ноды
    async def nodes(self):
        return self._list(await self._req("GET", "/api/nodes"), "nodes")

    async def create_node(self, name: str, address: str, port: int, profile_uuid: str,
                          inbounds: list[str], country: str = "XX"):
        body = {"name": name, "address": address, "port": port, "countryCode": country,
                "configProfile": {"activeConfigProfileUuid": profile_uuid, "activeInbounds": inbounds}}
        return await self._req("POST", "/api/nodes", json=body)

    async def restart_node(self, uuid: str):
        return await self._req("POST", f"/api/nodes/{uuid}/actions/restart")

    async def delete_node(self, uuid: str):
        return await self._req("DELETE", f"/api/nodes/{uuid}")

    # --- хосты
    async def hosts(self):
        return self._list(await self._req("GET", "/api/hosts"), "hosts")

    async def create_host(self, remark: str, address: str, port: int, profile_uuid: str,
                          inbound_uuid: str, sni: str | None = None):
        body = {"remark": remark[:40], "address": address, "port": port,
                "inbound": {"configProfileUuid": profile_uuid, "configProfileInboundUuid": inbound_uuid}}
        if sni:
            body["sni"] = sni
        return await self._req("POST", "/api/hosts", json=body)

    async def update_host(self, uuid: str, **fields):
        return await self._req("PATCH", "/api/hosts", json={"uuid": uuid, **fields})

    async def delete_host(self, uuid: str):
        return await self._req("DELETE", f"/api/hosts/{uuid}")

    async def set_hosts_enabled(self, uuids: list[str], enabled: bool):
        action = "enable" if enabled else "disable"
        return await self._req("POST", f"/api/hosts/bulk/{action}", json={"uuids": uuids})


def for_panel(panel: db.Panel) -> RW:
    cookie = None
    if panel.protected and panel.cookie_enc:
        cookie = ("vmkey", crypto.dec(panel.cookie_enc))
    return RW(panel.api_url, crypto.dec(panel.api_token_enc), cookie,
              panel.admin_user, crypto.dec(panel.admin_pass_enc))


def has_api(panel: db.Panel) -> bool:
    return bool(panel.api_token_enc or (panel.admin_user and panel.admin_pass_enc))
