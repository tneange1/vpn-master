"""Клиент Remnawave API (https://docs.rw/api). Авторизация: Bearer API-токен из панели."""
import httpx

from . import crypto, db


class RwError(Exception):
    pass


class RW:
    def __init__(self, base_url: str, token: str | None, cookie: tuple[str, str] | None = None):
        if not token:
            raise RwError("У панели не задан API-токен (Доступы → Панели → API-токен)")
        headers = {
            "Authorization": f"Bearer {token}",
            # нужно, если запрос идёт мимо reverse proxy
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "127.0.0.1",
        }
        cookies = {cookie[0]: cookie[1]} if cookie else None
        self.c = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, cookies=cookies, timeout=30)

    async def _req(self, method: str, path: str, **kw):
        try:
            r = await self.c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise RwError(f"{method} {path}: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise RwError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
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

    # --- система
    async def keygen(self) -> str:
        r = await self._req("GET", "/api/keygen")
        return r["pubKey"]

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
    async def create_host(self, remark: str, address: str, port: int, profile_uuid: str,
                          inbound_uuid: str, sni: str | None = None):
        body = {"remark": remark[:40], "address": address, "port": port,
                "inbound": {"configProfileUuid": profile_uuid, "configProfileInboundUuid": inbound_uuid}}
        if sni:
            body["sni"] = sni
        return await self._req("POST", "/api/hosts", json=body)


def for_panel(panel: db.Panel) -> RW:
    cookie = None
    if panel.protected and panel.cookie_enc:
        cookie = ("vmkey", crypto.dec(panel.cookie_enc))
    return RW(panel.api_url, crypto.dec(panel.api_token_enc), cookie)
