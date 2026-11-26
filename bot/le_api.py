"""Клиент HTTP-API Link Extractor (https://github.com/tneange1/link-extractor):
<url>/api/... с заголовком Authorization: Bearer <apiToken>. Всё, что умел его бот, делаем через это API."""
import json

import httpx

from . import crypto, db


class LeError(Exception):
    pass


async def mod_for(panel_id: int) -> db.Mod | None:
    rows = await db.all_(db.Mod, db.Mod.kind == "link_extractor", db.Mod.panel_id == int(panel_id))
    return rows[0] if rows else None


def conf_of(mod: db.Mod | None, panel: db.Panel) -> dict:
    """{'url': ..., 'token': ...}. Понимает и старый формат (секреты store.json от прошлых версий)."""
    if not mod or not mod.secrets_enc:
        return {}
    try:
        data = json.loads(crypto.dec(mod.secrets_enc))
    except (ValueError, TypeError):
        return {}
    token = data.get("token") or data.get("apiToken")
    url = (data.get("url") or default_url(panel)).rstrip("/")
    # экстрактор работает с домена подписки, а не панели
    if panel.sub_domain and panel.sub_local and url.split("/")[2:3] == [panel.domain]:
        url = default_url(panel)
    return {"url": url, "token": token} if token else {}


def page_url(panel: db.Panel) -> str:
    """Где открывать страницу экстрактора в браузере."""
    if panel.le_on_panel is not False or not (panel.sub_domain and panel.sub_local):
        return f"https://{panel.domain}/link-extractor/"
    return f"https://{panel.sub_domain}/link-extractor/"


async def set_panel_domain(panel: db.Panel) -> str | None:
    """Экстрактору — домен панели (пункт меню, страница в панели) или пусто (страница на домене подписки)."""
    try:
        async with await for_panel(panel.id) as le:
            await le.call("POST", "/settings", {"panelDomain": panel.domain if panel.le_on_panel is not False else ""})
        return None
    except LeError as e:
        return str(e)


def default_url(panel: db.Panel) -> str:
    host = panel.sub_domain if panel.sub_domain and panel.sub_local else panel.domain
    return f"https://{host}/link-extractor"


async def save_conf(panel: db.Panel, url: str, token: str, server_id: int | None = None) -> db.Mod:
    m = await mod_for(panel.id) or db.Mod(kind="link_extractor", panel_id=panel.id)
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    if not url.endswith("/link-extractor"):
        url += "/link-extractor"
    m.secrets_enc = crypto.enc(json.dumps({"url": url, "token": token}))
    m.bot_token_enc = None
    if server_id:
        m.server_id = server_id
    return await db.save(m)


class LE:
    def __init__(self, panel: db.Panel, conf: dict):
        if not conf:
            raise LeError("Link Extractor на этой панели не подключён")
        self.url, self.name = conf["url"], panel.name
        headers = {"Authorization": f"Bearer {conf['token']}"}
        if panel.protected and panel.cookie_enc:  # если идём через домен панели с секретным путём
            headers["X-Api-Key"] = crypto.dec(panel.cookie_enc)
        self.c = httpx.AsyncClient(headers=headers, timeout=60)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.c.aclose()

    async def call(self, method: str, path: str, body=None, *, raw: bool = False, timeout: float = 60):
        try:
            r = await self.c.request(method, f"{self.url}/api{path}", json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise LeError(f"Экстрактор «{self.name}» недоступен: {type(e).__name__}: {e}") from e
        if raw:
            if r.status_code >= 400:
                raise LeError(f"HTTP {r.status_code}")
            return r.content
        try:
            j = r.json()
        except ValueError:
            j = {}
        if r.status_code == 401:
            raise LeError(f"Экстрактор «{self.name}»: неверный API-токен")
        if r.status_code >= 400:
            raise LeError(j.get("error") or f"HTTP {r.status_code}")
        return j

    # короткие обёртки
    async def state(self):
        return await self.call("GET", "/state")


async def for_panel(panel_id) -> LE:
    panel = await db.get(db.Panel, panel_id)
    if not panel:
        raise LeError("Панель не найдена")
    return LE(panel, conf_of(await mod_for(panel.id), panel))


async def all_panels() -> list[db.Panel]:
    out = []
    for p in await db.all_(db.Panel):
        if conf_of(await mod_for(p.id), p):
            out.append(p)
    return out
