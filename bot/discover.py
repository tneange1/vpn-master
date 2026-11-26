"""Обнаружение того, что реально запущено на сервере, и «🩹 Фикс reverse proxy» одной кнопкой.

1. DISCOVER по SSH: контейнеры, домены из .env панели и Bedolaga, кабинет, текущий прокси и его ключ X-Api-Key.
2. Подхват в БД: панель на сервере (домен подписки, Link Extractor), бот Bedolaga и кабинет.
3. Пересборка прокси со всеми сайтами сервера: бэкап текущего → свой Caddy/Nginx → проверка → откат при ошибке.
4. Bedolaga получает доступ к панели через X-Api-Key (REMNAWAVE_AUTH_TYPE=caddy).
"""
import json
import re

from . import crypto, db, proxy, scripts
from .runner import run_task
from .ui import h


def _host(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"^(?:https?://)?([^/:\s]+)", url.strip())
    return m.group(1).lower() if m and "." in m.group(1) else None


def parse(marks: dict) -> dict:
    cts = []
    for k, v in marks.items():
        if re.fullmatch(r"CT\d+", k):
            name, img, wd, cf, nets, ports = (v.split("|") + [""] * 6)[:6]
            cts.append({"name": name, "image": img, "dir": wd, "compose": cf, "nets": [n for n in nets.split(",") if n],
                        "ports": ports})
    names = {c["name"] for c in cts}

    def find(pred):
        return next((c["name"] for c in cts if pred(c)), None)

    info = {
        "containers": cts,
        "panel": "remnawave" if "remnawave" in names else None,
        "subpage": find(lambda c: "subscription-page" in c["image"] or c["name"] == "remnawave-subscription-page"),
        "le": find(lambda c: c["name"] == "link-extractor"),
        "bot": marks.get("BD_CT") or None,
        "cabinet": find(lambda c: "bedolaga-cabinet" in c["image"] or "cabinet" in c["name"].lower()),
        "rw_front": _host(marks.get("RW_FRONT_END_DOMAIN")),
        "rw_sub": None,
        "bd": {k[3:]: v for k, v in marks.items() if k.startswith("BD_")},
        "proxy": marks.get("PROXY"),
        "foreign_key": marks.get("FOREIGN_KEY") or None,
    }
    sub = (marks.get("RW_SUB_PUBLIC_DOMAIN") or "").strip()
    if sub and "/" not in re.sub(r"^https?://", "", sub).rstrip("/"):  # «panel/api/sub» — отдельного домена нет
        info["rw_sub"] = _host(sub)
    return info


async def run_discover(bot, chat_id: int, server: db.Server) -> dict | None:
    res = await run_task(bot, chat_id, server, "Смотрю, что запущено на сервере", scripts.DISCOVER)
    return parse(res.marks) if res.ok else None


async def adopt(server: db.Server, info: dict) -> list[str]:
    """Обновляет БД по найденному на сервере. Возвращает строки отчёта."""
    notes = []
    ct = {k: info[k] for k in ("panel", "subpage", "le", "bot", "cabinet") if info.get(k)}
    await db.set_setting(f"ct:{server.id}", json.dumps(ct))

    # --- панель
    panels = await db.all_(db.Panel, db.Panel.server_id == server.id)
    if info["panel"] and not panels and info["rw_front"]:
        same = [p for p in await db.all_(db.Panel) if p.domain == info["rw_front"]]
        if same:
            p = same[0]
            p.server_id = server.id
        else:
            p = db.Panel(name=info["rw_front"], server_id=server.id, domain=info["rw_front"],
                         api_url=f"https://{info['rw_front']}")
        panels = [await db.save(p)]
        notes.append(f"🖥 Панель {h(p.domain)} привязана к серверу")
    for p in panels:
        changed = False
        if info["rw_sub"] and not p.sub_domain:
            p.sub_domain, changed = info["rw_sub"], True
        sub_here = bool(info["subpage"] or info["le"])
        if p.sub_local != sub_here:
            p.sub_local, changed = sub_here, True
        if p.link_extractor != bool(info["le"]):
            p.link_extractor, changed = bool(info["le"]), True
        if info["foreign_key"] and not p.protected and re.match(proxy.KEY_RE, info["foreign_key"]):
            p.protected, p.cookie_enc, changed = True, crypto.enc(info["foreign_key"]), True
            notes.append("🔒 Ключ секретного пути (X-Api-Key) взят из текущего Caddyfile")
        if changed:
            await db.save(p)
        notes.append(f"🖥 {h(p.domain)}: подписка {h(p.sub_domain or '—')}"
                     + (" (здесь)" if p.sub_local else " (на другом сервере)")
                     + (", Link Extractor ✅" if p.link_extractor else ""))

    # --- Bedolaga
    bd = info["bd"]
    if info["bot"] and bd.get("DIR"):
        hooks = _host(bd.get("WEBHOOK_URL"))
        bots = [b for b in await db.all_(db.Bedolaga) if b.server_id == server.id]
        b = bots[0] if bots else db.Bedolaga(name=f"Bedolaga · {server.name}", server_id=server.id)
        b.install_dir = bd["DIR"]
        b.compose_file = bd.get("COMPOSE") or b.compose_file or "docker-compose.yml"
        b.hooks_domain = hooks or b.hooks_domain
        if bd.get("BOT_TOKEN"):
            b.bot_token_enc = crypto.enc(bd["BOT_TOKEN"])
        if bd.get("WEB_API_DEFAULT_TOKEN"):
            b.web_api_token_enc = crypto.enc(bd["WEB_API_DEFAULT_TOKEN"])
        b.admin_ids = bd.get("ADMIN_IDS") or b.admin_ids
        if not b.panel_id:
            api_host = _host(bd.get("REMNAWAVE_API_URL"))
            pan = [p for p in await db.all_(db.Panel) if p.domain == api_host] or panels
            b.panel_id = pan[0].id if pan else None
        if info["cabinet"]:
            origins = [_host(x) for x in (bd.get("CABINET_ALLOWED_ORIGINS") or "").split(",")]
            cab = next((x for x in origins if x), None) or b.cabinet_domain
            if cab:
                b.cabinet, b.cabinet_domain = True, cab
        b = await db.save(b)
        if b.cabinet:
            await db.set_setting(f"cabinet_srv:{b.id}", str(server.id))
        notes.append(f"🤖 Bedolaga: {h(info['bot'])} · вебхуки {h(b.hooks_domain or '—')}"
                     + (f" · кабинет {h(b.cabinet_domain)}" if b.cabinet else ""))
    elif info["cabinet"]:
        notes.append(f"🗂 Найден кабинет {h(info['cabinet'])}, но без бота Bedolaga на этом сервере")
    if info["proxy"]:
        notes.append(f"🧭 Сейчас на 443: {h(info['proxy'].split('|')[0])}")
    return notes


async def fix_proxy(bot, chat_id: int, server: db.Server) -> bool:
    """🩹 Одна кнопка: обнаружить → подхватить → пересобрать прокси → синхронизировать Bedolaga."""
    info = await run_discover(bot, chat_id, server)
    if info is None:
        await bot.send_message(chat_id, "❌ Не смог осмотреть сервер.")
        return False
    notes = await adopt(server, info)
    groups = await proxy.server_groups(server)
    if not groups:
        await bot.send_message(chat_id, "\n".join(notes + ["", "Нечего проксировать: панели/бота/кабинета на сервере нет."]))
        return False
    kind = server.proxy
    if not kind:
        img = (info["proxy"] or "").lower()
        kind = "nginx" if "nginx" in img and "caddy" not in img else "caddy"
    panels = await db.all_(db.Panel, db.Panel.server_id == server.id)
    check = panels[0].domain if panels else groups[0][1][0].domain
    key = proxy.panel_cookie(panels[0]) if panels else None
    site_list = "\n".join(f"• {x.domain} → {x.upstream}" + (" 🔒" if x.cookie else "") for _, ss in groups for x in ss)
    await bot.send_message(chat_id, "\n".join(notes) + f"\n\n<b>Собираю {kind.capitalize()}:</b>\n{h(site_list)}")
    res = await run_task(bot, chat_id, server, f"🩹 Фикс reverse proxy ({kind})",
                         proxy.takeover_script(kind, groups, check, key, await db.setting("acme_email")))
    if not res.ok:
        await bot.send_message(chat_id, "❌ Прокси не поднялся — прежний возвращён (лог выше).")
        return False
    server.proxy = kind
    await db.save(server)
    for p in panels:
        p.managed_proxy = True
        p = await db.save(p)
        await proxy.sync_bedolaga_token(bot, chat_id, p)
    lines = [f"✅ Reverse proxy исправлен ({kind.capitalize()})."]
    for p in panels:
        k = proxy.panel_cookie(p)
        lines.append(f"🖥 {h(p.domain)}: " + (f"вход <tg-spoiler>{h(proxy.login_url(p.domain, k))}</tg-spoiler>, "
                                               f"API — заголовок <code>X-Api-Key</code>" if k else "без секретного пути"))
        if p.link_extractor and p.sub_domain and p.sub_local:
            lines.append(f"🔗 Link Extractor: https://{h(p.sub_domain)}/link-extractor/")
    if res.marks.get("BACKUP"):
        lines.append(f"💾 Бэкап прежнего прокси: <code>{h(res.marks['BACKUP'])}</code>")
    await bot.send_message(chat_id, "\n".join(lines))
    return True
