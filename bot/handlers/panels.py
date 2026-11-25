"""Раздел 2 «Управление» → «Панели»: ноды, профили (Reality self-steal / Hysteria2), внутренние сквады."""
import asyncio
import json
import socket

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from .. import crypto, db, scripts, xray
from ..forms import Field, form, start_form
from ..rw_api import RwError, for_panel
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show
from .servers import SERVER_FIELDS, ensure_server

router = Router(name="panels")


@router.callback_query(F.data == "mng")
async def manage_menu(c: CallbackQuery):
    await show(c, "🛠 <b>Управление</b>",
               kb([("🖥 Панели", "pn:list"), ("🧩 Модификации", "mod")],
                  [("🛡 Безопасность", "sec"), ("⬆️ Обновления", "upd")],
                  [("⬅️ Назад", "menu")]))


@router.callback_query(F.data == "pn:list")
async def panel_list(c: CallbackQuery):
    panels = await db.all_(db.Panel)
    rows = [[(f"🖥 {p.name} · {p.domain}", f"pn:v:{p.id}")] for p in panels]
    rows.append([("➕ Установить", "inst:rw"), ("🔗 Подключить", "inst:rwx")])
    rows.append([("⬅️ Назад", "mng")])
    await show(c, f"🖥 <b>Панели</b> ({len(panels)})", kb(*rows))


@router.callback_query(F.data.startswith("pn:v:"))
async def panel_view(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    if not p:
        return await c.answer("Нет такой панели", show_alert=True)
    server = await db.get(db.Server, p.server_id)
    nodes = await db.all_(db.Node, db.Node.panel_id == p.id)
    i = p.id
    text = (f"🖥 <b>{h(p.name)}</b>\n"
            f"Панель: https://{h(p.domain)}\n"
            f"Подписка: {('https://' + h(p.sub_domain)) if p.sub_domain else '—'}\n"
            f"Сервер: {h(server.name) if server else '— (только API)'}\n"
            f"API-токен: {'✅' if p.api_token_enc else '❌'} · Caddy от vpn-master: {'✅' if p.managed_proxy else '—'}\n"
            f"Защита ссылкой: {'🔒 вкл' if p.protected else 'выкл'} · Link Extractor: {'✅' if p.link_extractor else '—'}\n"
            f"Нод в боте: {len(nodes)}")
    await show(c, text, kb(
        [("➕ Нода", f"pn:addnode:{i}"), ("📋 Ноды", f"pn:nodes:{i}")],
        [("🧬 Профили", f"pn:prof:{i}"), ("👥 Сквады", f"pn:sq:{i}")],
        [("🧩 Модификации", f"mod:p:{i}"), ("⬆️ Обновления", f"upd:p:{i}")],
        [("🔑 API-токен", f"pn:tok:{i}"), ("🗑 Убрать из бота", f"pn:del:{i}")],
        [("⬅️ Панели", "pn:list")]))


@form("pn_tok", "🔑 API-токен панели", [
    Field("token", "Пришли API-токен (Панель → Настройки → API-токены). Сообщение удалю:", secret=True)])
async def pn_tok_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["id"])
    p.api_token_enc = crypto.enc(values["token"])
    await db.save(p)
    try:
        rw = for_panel(p)
        n = len(await rw.nodes())
        await rw.close()
        msg = f"✅ Токен сохранён, API отвечает (нод: {n})."
    except RwError as e:
        msg = f"⚠️ Токен сохранён, но API не ответил: <code>{h(e)}</code>"
    server = await db.get(db.Server, p.server_id)
    if server and p.managed_proxy:
        await run_task(event.bot, chat_id(event), server, "Токен для страницы подписки",
                       scripts.set_subpage_token(values["token"]))
    await say(event, msg, kb([("⬅️ К панели", f"pn:v:{p.id}")]))


@router.callback_query(F.data.startswith("pn:tok:"))
async def pn_tok(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "pn_tok", ctx={"id": int(c.data.split(":")[2])})


@router.callback_query(F.data.startswith("pn:del:"))
async def pn_del(c: CallbackQuery):
    i = c.data.split(":")[2]
    await show(c, "🗑 Убрать панель из бота? Сама панель и сервер не пострадают. Ноды панели тоже уберутся из бота.",
               kb([("🗑 Да", f"pn:delok:{i}"), ("✖️ Нет", f"pn:v:{i}")]))


@router.callback_query(F.data.startswith("pn:delok:"))
async def pn_del_ok(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    if p:
        await db.delete(p)
    await panel_list(c)


# ------------------------------------------------------------------ ноды
@router.callback_query(F.data.startswith("pn:nodes:"))
async def node_list(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    nodes = await db.all_(db.Node, db.Node.panel_id == p.id)
    status = {}
    try:
        rw = for_panel(p)
        for n in await rw.nodes():
            status[n.get("uuid")] = n
        await rw.close()
    except RwError as e:
        await c.answer(f"API: {e}"[:190], show_alert=True)
    rows = []
    for n in nodes:
        st = status.get(n.rw_uuid, {})
        icon = "🟢" if st.get("isConnected") else ("🔴" if st else "⚪️")
        rows.append([(f"{icon} {n.name} · {n.domain or ''}", f"nd:v:{n.id}")])
    known = {n.rw_uuid for n in nodes}
    extra = [s for u, s in status.items() if u not in known]
    rows.append([("➕ Добавить ноду", f"pn:addnode:{p.id}")])
    rows.append([("⬅️ К панели", f"pn:v:{p.id}")])
    extra_txt = ("\n\nВ панели есть ещё ноды, не созданные ботом: " +
                 h(", ".join(s.get("name", "?") for s in extra))) if extra else ""
    await show(c, f"📋 <b>Ноды панели {h(p.name)}</b>{extra_txt}", kb(*rows))


@router.callback_query(F.data.startswith("nd:v:"))
async def node_view(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    s = await db.get(db.Server, n.server_id)
    i = n.id
    await show(c, f"🛰 <b>{h(n.name)}</b>\nСервер: {h(s.name + ' · ' + s.host) if s else '—'}\n"
                  f"Домен self-steal: {h(n.domain or '—')}\nUUID: <code>{h(n.rw_uuid or '—')}</code>\n"
                  f"WARP: {'✅' if n.warp else '—'}",
               kb([("🔄 Рестарт (API)", f"nd:rs:{i}"), ("🌐 WARP", f"mod:n:{i}")],
                  [("⬆️ Обновить ноду", f"upd:run:node:{i}"), ("🛡 Безопасность", f"sec:s:{n.server_id}")],
                  [("🗑 Удалить", f"nd:del:{i}")],
                  [("⬅️ Ноды", f"pn:nodes:{n.panel_id}")]))


@router.callback_query(F.data.startswith("nd:rs:"))
async def node_restart(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    p = await db.get(db.Panel, n.panel_id)
    try:
        rw = for_panel(p)
        await rw.restart_node(n.rw_uuid)
        await rw.close()
        await c.answer("Команда на рестарт отправлена", show_alert=True)
    except RwError as e:
        await c.answer(str(e)[:190], show_alert=True)


@router.callback_query(F.data.startswith("nd:del:"))
async def node_del(c: CallbackQuery):
    i = c.data.split(":")[2]
    await show(c, "🗑 Удалить ноду?",
               kb([("Из бота и из панели", f"nd:delok:{i}:1")], [("Только из бота", f"nd:delok:{i}:0")],
                  [("✖️ Отмена", f"nd:v:{i}")]))


@router.callback_query(F.data.startswith("nd:delok:"))
async def node_del_ok(c: CallbackQuery):
    _, _, i, also_panel = c.data.split(":")
    n = await db.get(db.Node, i)
    p = await db.get(db.Panel, n.panel_id)
    if also_panel == "1" and n.rw_uuid:
        try:
            rw = for_panel(p)
            await rw.delete_node(n.rw_uuid)
            await rw.close()
        except RwError as e:
            await say(c, f"⚠️ В панели удалить не вышло: <code>{h(e)}</code>")
    await db.delete(n)
    await say(c, "🗑 Нода удалена.", kb([("⬅️ Ноды", f"pn:nodes:{p.id}")]))


async def profile_choices(values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    rows = [(f"✨ Новый: {label}", f"new:{k}") for k, label in xray.KINDS.items()]
    rw = for_panel(p)
    try:
        for pr in await rw.profiles():
            rows.append((f"🧬 {pr['name']} ({len(pr.get('inbounds') or [])} inb.)", f"p:{pr['uuid']}"))
    finally:
        await rw.close()
    return rows


async def squad_choices(values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    rw = for_panel(p)
    try:
        rows = [(f"👥 {s['name']}", s["uuid"]) for s in await rw.squads()]
    finally:
        await rw.close()
    return rows + [("➕ Новый сквад", "new"), ("⏭ Не добавлять в сквад", "skip")]


def _needs_domain(v):
    return str(v.get("profile", "")).startswith("new:") or v.get("selfsteal")


@form("node_add", "➕ Новая нода", SERVER_FIELDS + [
    Field("name", "Имя ноды, например <code>NL-1</code>:"),
    Field("profile", "Профиль конфигурации:", "choice", choices=profile_choices),
    Field("selfsteal", "Поставить self-steal Caddy (заглушка-сайт на 127.0.0.1:9443)?", "choice",
          choices=[("✅ Да", True), ("Нет", False)], when=lambda v: str(v.get("profile", "")).startswith("p:")),
    Field("domain", "Домен ноды для self-steal (A-запись → IP ноды), например <code>nl1.example.com</code>:",
          "domain", when=_needs_domain),
    Field("squad", "В какой внутренний сквад добавить инбаунды?", "choice", choices=squad_choices),
    Field("squad_name", "Название нового сквада:", when=lambda v: v.get("squad") == "new"),
    Field("country", "Код страны (2 буквы, напр. NL):", optional=True, default="XX"),
])
async def node_add_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    if not server:
        return await say(event, "❌ Для ноды нужен SSH-доступ к серверу.")
    name, domain = values["name"], values.get("domain")
    prof_val = values["profile"]
    selfsteal = True if prof_val.startswith("new:") else bool(values.get("selfsteal"))
    rw = for_panel(p)
    report = []
    try:
        secret = await rw.keygen()
        if prof_val.startswith("new:"):
            kind = prof_val[4:]
            cfg = xray.build_profile(kind, domain, prefix=name)
            prof = await rw.create_profile(name, cfg)
            report.append(f"🧬 Профиль <b>{h(name)}</b> создан ({h(xray.KINDS[kind])})")
        else:
            prof = await rw.profile(prof_val[2:])
            report.append(f"🧬 Профиль: {h(prof.get('name'))}")
        inbounds = prof.get("inbounds") or []
        inb_uuids = [i["uuid"] for i in inbounds]
        node = await rw.create_node(name, server.host, 2222, prof["uuid"], inb_uuids,
                                    (values.get("country") or "XX").upper()[:2])
        report.append(f"🛰 Нода создана в панели: <code>{h(node.get('uuid'))}</code>")
        for inb in inbounds:
            try:
                await rw.create_host(f"{name} {inb.get('tag')}", domain or server.host, inb.get("port") or 443,
                                     prof["uuid"], inb["uuid"], sni=domain)
                report.append(f"🌐 Хост {h(inb.get('tag'))}")
            except RwError as e:
                report.append(f"⚠️ Хост {h(inb.get('tag'))}: {h(str(e)[:150])}")
        sq = values["squad"]
        if sq == "new":
            await rw.create_squad(values["squad_name"], inb_uuids)
            report.append(f"👥 Сквад <b>{h(values['squad_name'])}</b> создан")
        elif sq != "skip":
            cur = next((s for s in await rw.squads() if s["uuid"] == sq), {})
            have = [i["uuid"] if isinstance(i, dict) else i for i in cur.get("inbounds") or []]
            await rw.update_squad(sq, list(dict.fromkeys(have + inb_uuids)))
            report.append(f"👥 Инбаунды добавлены в сквад {h(cur.get('name', ''))}")
    except RwError as e:
        await rw.close()
        return await say(event, "❌ Ошибка API панели:\n<code>" + h(e) + "</code>\n\n" + "\n".join(report))
    await rw.close()

    n = await db.save(db.Node(panel_id=p.id, server_id=server.id, name=name, domain=domain,
                              rw_uuid=node.get("uuid"), profile_uuid=prof["uuid"]))
    panel_srv = await db.get(db.Server, p.server_id)
    panel_ip = await _resolve(panel_srv.host if panel_srv else p.domain)
    await say(event, "\n".join(report))
    res = await run_task(event.bot, chat_id(event), server, f"Установка ноды {name}",
                         scripts.node_install(secret, domain, selfsteal, panel_ip))
    await say(event, f"{'✅' if res.ok else '⚠️'} Нода <b>{h(name)}</b> готова. Через минуту она должна стать 🟢 в панели.",
              kb([("🛰 К ноде", f"nd:v:{n.id}"), ("📋 Ноды", f"pn:nodes:{p.id}")]))


async def _resolve(host: str) -> str | None:
    try:
        return await asyncio.get_running_loop().run_in_executor(None, socket.gethostbyname, host)
    except OSError:
        return None


@router.callback_query(F.data.startswith("pn:addnode:"))
async def node_add(c: CallbackQuery, state: FSMContext):
    p = await db.get(db.Panel, c.data.split(":")[2])
    if not p.api_token_enc:
        return await c.answer("Сначала задай API-токен панели", show_alert=True)
    await start_form(c, state, "node_add", ctx={"panel": p.id})


# ------------------------------------------------------------------ профили
@router.callback_query(F.data.startswith("pn:prof:"))
async def prof_list(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    try:
        rw = for_panel(p)
        profiles = await rw.profiles()
        await rw.close()
    except RwError as e:
        return await show(c, f"❌ <code>{h(e)}</code>", kb([("⬅️ К панели", f"pn:v:{p.id}")]))
    lines = [f"🧬 <b>Профили {h(p.name)}</b>\n"]
    rows = []
    for pr in profiles:
        tags = ", ".join(i.get("tag", "?") for i in pr.get("inbounds") or [])
        lines.append(f"• <b>{h(pr['name'])}</b>: {h(tags) or '—'}")
        rows.append([(f"📄 {pr['name']}", f"pf:dl:{p.id}:{pr['uuid']}")])
    rows.append([("✨ Создать профиль", f"pf:new:{p.id}")])
    rows.append([("⬅️ К панели", f"pn:v:{p.id}")])
    await show(c, "\n".join(lines) + "\n\n📄 — скачать JSON профиля", kb(*rows))


@router.callback_query(F.data.startswith("pf:dl:"))
async def prof_download(c: CallbackQuery):
    _, _, pid, uuid = c.data.split(":", 3)
    p = await db.get(db.Panel, pid)
    try:
        rw = for_panel(p)
        pr = await rw.profile(uuid)
        await rw.close()
    except RwError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer()
    await c.message.answer_document(BufferedInputFile(
        json.dumps(pr.get("config", pr), ensure_ascii=False, indent=2).encode(), filename=f"{pr.get('name', 'profile')}.json"))


@form("pf_new", "✨ Новый профиль", [
    Field("kind", "Тип профиля:", "choice", choices=[(v, k) for k, v in xray.KINDS.items()]),
    Field("name", "Название профиля:"),
    Field("domain", "Домен self-steal (SNI для Reality / сертификат для Hysteria2):", "domain"),
])
async def pf_new_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    try:
        rw = for_panel(p)
        pr = await rw.create_profile(values["name"], xray.build_profile(values["kind"], values["domain"],
                                                                         prefix=values["name"]))
        await rw.close()
    except RwError as e:
        return await say(event, f"❌ <code>{h(e)}</code>", kb([("⬅️ Профили", f"pn:prof:{p.id}")]))
    await say(event, f"✅ Профиль <b>{h(pr.get('name'))}</b> создан, инбаундов: {len(pr.get('inbounds') or [])}.\n"
                     "Для работы нужен self-steal Caddy на ноде с этим доменом (ставится при «➕ Нода»).",
              kb([("⬅️ Профили", f"pn:prof:{p.id}")]))


@router.callback_query(F.data.startswith("pf:new:"))
async def pf_new(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "pf_new", ctx={"panel": int(c.data.split(":")[2])})


# ------------------------------------------------------------------ сквады
@router.callback_query(F.data.startswith("pn:sq:"))
async def squad_list(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    try:
        rw = for_panel(p)
        squads = await rw.squads()
        await rw.close()
    except RwError as e:
        return await show(c, f"❌ <code>{h(e)}</code>", kb([("⬅️ К панели", f"pn:v:{p.id}")]))
    lines = [f"👥 <b>Внутренние сквады {h(p.name)}</b>\n"]
    for s in squads:
        inb = s.get("inbounds") or []
        tags = ", ".join(i.get("tag", "?") if isinstance(i, dict) else str(i)[:8] for i in inb)
        info = s.get("info") or {}
        lines.append(f"• <b>{h(s['name'])}</b> — юзеров: {info.get('membersCount', '?')}, инбаунды: {h(tags) or '—'}")
    await show(c, "\n".join(lines), kb([("➕ Создать сквад", f"sq:new:{p.id}")], [("⬅️ К панели", f"pn:v:{p.id}")]))


async def profile_only_choices(values, ctx):
    rows = await profile_choices(values, ctx)
    return [r for r in rows if str(r[1]).startswith("p:")] + [("Пустой сквад", "none")]


@form("sq_new", "👥 Новый сквад", [
    Field("name", "Название сквада:"),
    Field("profile", "Включить все инбаунды профиля:", "choice", choices=profile_only_choices),
])
async def sq_new_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    try:
        rw = for_panel(p)
        inb = []
        if values["profile"] != "none":
            inb = [i["uuid"] for i in (await rw.profile(values["profile"][2:])).get("inbounds") or []]
        await rw.create_squad(values["name"], inb)
        await rw.close()
    except RwError as e:
        return await say(event, f"❌ <code>{h(e)}</code>", kb([("⬅️ Сквады", f"pn:sq:{p.id}")]))
    await say(event, f"✅ Сквад <b>{h(values['name'])}</b> создан ({len(inb)} инбаундов).",
              kb([("⬅️ Сквады", f"pn:sq:{p.id}")]))


@router.callback_query(F.data.startswith("sq:new:"))
async def sq_new(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "sq_new", ctx={"panel": int(c.data.split(":")[2])})
