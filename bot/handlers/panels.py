"""Раздел 2 «Управление» → «Панели»: ноды, хосты, профили, сквады, доступ к API, сервер панели."""
import asyncio
import json
import socket

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from .. import crypto, db, scripts, xray
from ..forms import Field, form, start_form
from ..rw_api import RwError, for_panel, has_api
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


def _api_label(p: db.Panel) -> str:
    parts = []
    if p.api_token_enc:
        parts.append("токен")
    if p.admin_user and p.admin_pass_enc:
        parts.append("логин/пароль")
    return " + ".join(parts) + " ✅" if parts else "❌ не задан"


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
            f"Сервер (SSH): {h(server.name + ' · ' + server.host) if server else '— не привязан'}\n"
            f"Доступ к API: {_api_label(p)}\n"
            f"Прокси: {(server.proxy or 'caddy').capitalize() + ' от vpn-master' if p.managed_proxy and server else 'не наш'}"
            f" · Секретный путь: {'🔒' if p.protected else 'выкл'}\n"
            f"Нод в боте: {len(nodes)}")
    await show(c, text, kb(
        [("🛰 Ноды", f"pn:nodes:{i}"), ("🌐 Хосты", f"hs:list:{i}:0")],
        [("🧬 Профили", f"pn:prof:{i}"), ("👥 Сквады", f"pn:sq:{i}")],
        [("🔗 Link Extractor", f"lx:v:{i}"), ("🧩 Модификации", f"mod:p:{i}")],
        [("🔑 Доступ к API", f"pn:api:{i}"), ("🖥 Сервер панели", f"pn:srv:{i}")],
        [("🧭 Reverse proxy", f"pn:px:{i}"), ("⬆️ Обновления", f"upd:p:{i}")],
        [("🗑 Убрать из бота", f"pn:del:{i}")],
        [("⬅️ Панели", "pn:list")]))


# ------------------------------------------------------------------ доступ к API
@router.callback_query(F.data.startswith("pn:api:"))
async def pn_api(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    i = p.id
    await show(c, f"🔑 <b>Доступ к API · {h(p.name)}</b>\n\n"
                  f"API-токен: {'✅' if p.api_token_enc else '—'}\n"
                  f"Логин/пароль: {'✅ ' + h(p.admin_user) if p.admin_user and p.admin_pass_enc else '—'}\n\n"
                  "Можно задать одно или оба. С логином/паролем бот сам получает JWT через /api/auth/login "
                  "и может выпустить API-токен (он нужен Bedolaga и странице подписки).",
               kb([("🔑 API-токен", f"pn:tok:{i}"), ("👤 Логин и пароль", f"pn:cred:{i}")],
                  [("🔌 Проверить", f"pn:test:{i}"), ("✨ Выпустить API-токен", f"pn:mktok:{i}")],
                  [("⬅️ К панели", f"pn:v:{i}")]))


async def _test(p: db.Panel) -> str:
    try:
        async with for_panel(p) as rw:
            n = len(await rw.nodes())
        return f"✅ API отвечает, нод в панели: {n}"
    except RwError as e:
        return f"⚠️ API не ответил: <code>{h(e)}</code>"


@router.callback_query(F.data.startswith("pn:test:"))
async def pn_test(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    await c.answer("Проверяю…")
    await say(c, await _test(p), kb([("⬅️ Доступ к API", f"pn:api:{p.id}")]))


@form("pn_tok", "🔑 API-токен панели", [
    Field("token", "Пришли API-токен (Панель → Настройки → API-токены). Сообщение удалю:", secret=True)])
async def pn_tok_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["id"])
    p.api_token_enc = crypto.enc(values["token"])
    p = await db.save(p)
    server = await db.get(db.Server, p.server_id)
    if server and p.managed_proxy and p.sub_local:
        await run_task(event.bot, chat_id(event), server, "Токен для страницы подписки",
                       scripts.set_subpage_token(values["token"]))
    await say(event, await _test(p), kb([("⬅️ Доступ к API", f"pn:api:{p.id}")]))


@router.callback_query(F.data.startswith("pn:tok:"))
async def pn_tok(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "pn_tok", ctx={"id": int(c.data.split(":")[2])})


@form("pn_cred", "👤 Логин и пароль панели", [
    Field("user", "Логин админа панели:"),
    Field("pw", "Пароль (сообщение удалю):", secret=True)])
async def pn_cred_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["id"])
    p.admin_user, p.admin_pass_enc = values["user"], crypto.enc(values["pw"])
    p = await db.save(p)
    await say(event, await _test(p), kb([("✨ Выпустить API-токен", f"pn:mktok:{p.id}")],
                                        [("⬅️ Доступ к API", f"pn:api:{p.id}")]))


@router.callback_query(F.data.startswith("pn:cred:"))
async def pn_cred(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "pn_cred", ctx={"id": int(c.data.split(":")[2])})


async def ensure_api_token(p: db.Panel) -> str:
    """Возвращает API-токен панели; если его нет, но есть логин/пароль — выпускает и сохраняет."""
    tok = crypto.dec(p.api_token_enc)
    if tok:
        return tok
    async with for_panel(p) as rw:
        tok = await rw.create_api_token("vpn-master")
    p.api_token_enc = crypto.enc(tok)
    await db.save(p)
    return tok


@router.callback_query(F.data.startswith("pn:mktok:"))
async def pn_mktok(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    if not (p.admin_user and p.admin_pass_enc):
        return await c.answer("Сначала задай логин и пароль", show_alert=True)
    try:
        async with for_panel(p) as rw:
            tok = await rw.create_api_token("vpn-master")
    except RwError as e:
        return await say(c, f"❌ <code>{h(e)}</code>")
    p.api_token_enc = crypto.enc(tok)
    await db.save(p)
    await say(c, "✅ API-токен выпущен и сохранён.", kb([("⬅️ Доступ к API", f"pn:api:{p.id}")]))


# ------------------------------------------------------------------ reverse proxy панели
@router.callback_query(F.data.startswith("pn:px:"))
async def pn_proxy(c: CallbackQuery):
    from .. import proxy
    p = await db.get(db.Panel, c.data.split(":")[2])
    server = await db.get(db.Server, p.server_id)
    i = p.id
    if not server:
        return await show(c, "🧭 Сначала привяжи SSH-сервер панели.", kb([("🖥 Сервер панели", f"pn:srv:{i}")],
                                                                         [("⬅️ К панели", f"pn:v:{i}")]))
    groups = await proxy.server_groups(server)
    sites = "\n".join(f"• {x.domain}" + (" 🔒" if x.cookie else "") + (" (+меню экстрактора)" if x.html_upstream else "")
                      for _, ss in groups for x in ss)
    if p.managed_proxy:
        kind = proxy.kind_of(server)
        return await show(c, f"🧭 <b>Reverse proxy · {h(server.name)}</b>\n{kind.capitalize()} от vpn-master\n\n"
                             f"Сайты на сервере:\n{h(sites)}\n\n"
                             "«Пересобрать» заново сгенерирует конфиг всех сайтов сервера (панель, подписка, "
                             "экстрактор, Bedolaga, кабинет), проверит и применит; при ошибке вернёт прежний.",
                          kb([("🔁 Пересобрать конфиг", f"pn:pxr:{i}")],
                             [("🔒 Секретный путь", f"sec:p:{i}"), ("🩹 Сети (502)", f"srv:nets:{server.id}")],
                             [("⬅️ К панели", f"pn:v:{i}")]))
    await show(c, f"🧭 <b>Reverse proxy · {h(server.name)}</b>\nСейчас прокси ставил не бот.\n\n"
                  f"Бот может взять его на себя: бэкап текущего (80/443) → свой Caddy/Nginx со всеми сайтами:\n{h(sites)}\n"
                  "→ проверка панели → при ошибке откат.",
               kb([("🔒 Caddy + секретный путь", f"pn:pxt:{i}:caddy:1"), ("🔒 Nginx + секретный путь", f"pn:pxt:{i}:nginx:1")],
                  [("Caddy", f"pn:pxt:{i}:caddy:0"), ("Nginx", f"pn:pxt:{i}:nginx:0")],
                  [("⬅️ К панели", f"pn:v:{i}")]))


@router.callback_query(F.data.startswith("pn:pxt:"))
async def pn_proxy_takeover(c: CallbackQuery):
    import json as _json
    _, _, pid, kind, protect = c.data.split(":")
    await db.set_setting(f"rwx:{pid}", _json.dumps({"kind": kind, "protect": protect == "1", "hooks": None,
                                                    "cabinet": None, "email": await db.setting("acme_email")}))
    await show(c, f"Поставить {kind.capitalize()}{' с секретным путём' if protect == '1' else ''}?\n"
                  "Панель может быть недоступна около минуты.",
               kb([("✅ Да, настроить", f"rwx:go:{pid}")], [("✖️ Отмена", f"pn:px:{pid}")]))


@router.callback_query(F.data.startswith("pn:pxr:"))
async def pn_proxy_rebuild(c: CallbackQuery):
    from .. import proxy
    p = await db.get(db.Panel, c.data.split(":")[2])
    server = await db.get(db.Server, p.server_id)
    await c.answer()
    groups = await proxy.server_groups(server)
    res = await run_task(c.bot, c.message.chat.id, server, f"{proxy.kind_of(server).capitalize()}: пересборка",
                         proxy.rebuild_script(proxy.kind_of(server), groups, await db.setting("acme_email")))
    await say(c, "✅ Прокси пересобран." if res.ok else "❌ Не вышло — прежний конфиг возвращён. Лог выше.",
              kb([("⬅️ Reverse proxy", f"pn:px:{p.id}")]))


# ------------------------------------------------------------------ сервер панели
@form("pn_srv", "🖥 Сервер панели", SERVER_FIELDS)
async def pn_srv_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["id"])
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("⬅️ К панели", f"pn:v:{p.id}")]))
    p.server_id = server.id
    await db.save(p)
    await say(event, f"✅ Панель привязана к серверу <b>{h(server.name)}</b>. Теперь доступны модификации, "
                     "безопасность и обновления по SSH.", kb([("⬅️ К панели", f"pn:v:{p.id}")]))


@router.callback_query(F.data.startswith("pn:srv:"))
async def pn_srv(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "pn_srv", ctx={"id": int(c.data.split(":")[2])})


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
async def _api_nodes(p: db.Panel) -> tuple[dict, str | None]:
    try:
        async with for_panel(p) as rw:
            return {n.get("uuid"): n for n in await rw.nodes()}, None
    except RwError as e:
        return {}, str(e)


@router.callback_query(F.data.startswith("pn:nodes:"))
async def node_list(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    nodes = await db.all_(db.Node, db.Node.panel_id == p.id)
    status, err = await _api_nodes(p)
    rows = []
    for n in nodes:
        st = status.get(n.rw_uuid, {})
        icon = "🟢" if st.get("isConnected") else ("🔴" if st else "⚪️")
        rows.append([(f"{icon} {n.name} · {(n.domain or '').split(',')[0]}", f"nd:v:{n.id}")])
    known = {n.rw_uuid for n in nodes}
    extra = [s for u, s in status.items() if u not in known]
    rows.append([("➕ Добавить ноду", f"pn:addnode:{p.id}"), ("🗑 Удалить ноду", f"nd:pick:{p.id}")])
    if extra:
        rows.append([("📥 Взять ноды из панели в бота", f"nd:adopt:{p.id}")])
    rows.append([("⬅️ К панели", f"pn:v:{p.id}")])
    txt = f"🛰 <b>Ноды · {h(p.name)}</b> ({len(nodes)})\n🟢 подключена · 🔴 нет связи · ⚪️ нет данных"
    if extra:
        txt += "\n\nВ панели есть ноды, которых нет в боте: " + h(", ".join(s.get("name", "?") for s in extra))
    if err:
        txt += f"\n\n⚠️ API: <code>{h(err[:200])}</code>"
    await show(c, txt, kb(*rows))


@router.callback_query(F.data.startswith("nd:adopt:"))
async def node_adopt(c: CallbackQuery):
    """Ноды, созданные в панели вручную, добавляем в бота (сервер можно привязать потом)."""
    p = await db.get(db.Panel, c.data.split(":")[2])
    status, err = await _api_nodes(p)
    known = {n.rw_uuid for n in await db.all_(db.Node, db.Node.panel_id == p.id)}
    servers = {s.host: s for s in await db.all_(db.Server)}
    added = []
    for u, n in status.items():
        if u in known:
            continue
        srv = servers.get(n.get("address"))
        prof = (n.get("configProfile") or {}).get("activeConfigProfileUuid")
        await db.save(db.Node(panel_id=p.id, server_id=srv.id if srv else None, name=n.get("name", "node"),
                              rw_uuid=u, profile_uuid=prof))
        added.append(n.get("name", "?") + ("" if srv else " (без SSH)"))
    await say(c, "📥 Добавлены: " + (h(", ".join(added)) or "—") +
              "\n\nУ нод «без SSH» привяжи сервер в карточке ноды.", kb([("🛰 Ноды", f"pn:nodes:{p.id}")]))


@router.callback_query(F.data.startswith("nd:pick:"))
async def node_pick_delete(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    rows = [[(f"🗑 {n.name}", f"nd:del:{n.id}")] for n in await db.all_(db.Node, db.Node.panel_id == p.id)]
    rows = rows or [[("Нод нет", "noop")]]
    rows.append([("⬅️ Ноды", f"pn:nodes:{p.id}")])
    await show(c, "Какую ноду удалить?", kb(*rows))


@router.callback_query(F.data.startswith("nd:v:"))
async def node_view(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    if not n:
        return await c.answer("Ноды нет", show_alert=True)
    s = await db.get(db.Server, n.server_id)
    i = n.id
    await show(c, f"🛰 <b>{h(n.name)}</b>\nСервер: {h(s.name + ' · ' + s.host) if s else '— не привязан'}\n"
                  f"Домены self-steal: {h(n.domain or '—')}\nUUID: <code>{h(n.rw_uuid or '—')}</code>\n"
                  f"WARP: {'✅' if n.warp else '—'}",
               kb([("🔄 Рестарт (API)", f"nd:rs:{i}"), ("🩺 Проверка", f"nd:chk:{i}")],
                  [("🔐 Сертификат", f"crt:v:{i}"), ("🌐 WARP", f"mod:n:{i}")],
                  [("⬆️ Обновить ноду", f"upd:run:node:{i}"), ("🛡 Безопасность", f"sec:s:{n.server_id}")],
                  [("🖥 Привязать сервер", f"nd:srv:{i}"), ("🗑 Удалить", f"nd:del:{i}")],
                  [("⬅️ Ноды", f"pn:nodes:{n.panel_id}")]))


@form("nd_srv", "🖥 Сервер ноды", SERVER_FIELDS)
async def nd_srv_done(event, values, ctx):
    n = await db.get(db.Node, ctx["id"])
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}")
    n.server_id = server.id
    await db.save(n)
    await say(event, "✅ Сервер привязан.", kb([("⬅️ К ноде", f"nd:v:{n.id}")]))


@router.callback_query(F.data.startswith("nd:srv:"))
async def nd_srv(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "nd_srv", ctx={"id": int(c.data.split(":")[2])})


async def _node_server(c: CallbackQuery, n: db.Node):
    s = await db.get(db.Server, n.server_id)
    if not s:
        await c.answer("У ноды не привязан сервер", show_alert=True)
    return s


@router.callback_query(F.data.startswith("nd:chk:"))
async def node_check(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    if s := await _node_server(c, n):
        await c.answer()
        await run_task(c.bot, c.message.chat.id, s, f"Проверка ноды {n.name}", scripts.NODE_CHECK)


@router.callback_query(F.data.startswith("nd:rs:"))
async def node_restart(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    p = await db.get(db.Panel, n.panel_id)
    try:
        async with for_panel(p) as rw:
            await rw.restart_node(n.rw_uuid)
        await c.answer("Команда на рестарт отправлена", show_alert=True)
    except RwError as e:
        await c.answer(str(e)[:190], show_alert=True)


@router.callback_query(F.data.startswith("nd:del:"))
async def node_del(c: CallbackQuery):
    i = c.data.split(":")[2]
    n = await db.get(db.Node, i)
    await show(c, f"🗑 Удалить ноду <b>{h(n.name)}</b>?",
               kb([("Из бота и из панели", f"nd:delok:{i}:1")], [("Только из бота", f"nd:delok:{i}:0")],
                  [("✖️ Отмена", f"nd:v:{i}")]))


@router.callback_query(F.data.startswith("nd:delok:"))
async def node_del_ok(c: CallbackQuery):
    _, _, i, also_panel = c.data.split(":")
    n = await db.get(db.Node, i)
    p = await db.get(db.Panel, n.panel_id)
    if also_panel == "1" and n.rw_uuid:
        try:
            async with for_panel(p) as rw:
                await rw.delete_node(n.rw_uuid)
        except RwError as e:
            await say(c, f"⚠️ В панели удалить не вышло: <code>{h(e)}</code>")
    await db.delete(n)
    await say(c, "🗑 Нода удалена. Контейнеры на сервере не трогал.", kb([("🛰 Ноды", f"pn:nodes:{p.id}")]))


# ------------------------------------------------------------------ добавление ноды
async def profile_choices(values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    rows = [(f"✨ Новый: {label}", f"new:{k}") for k, label in xray.KINDS.items()]
    async with for_panel(p) as rw:
        for pr in await rw.profiles():
            rows.append((f"🧬 {pr['name']} ({len(pr.get('inbounds') or [])} inb.)", f"p:{pr['uuid']}"))
    return rows


async def squad_choices(values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    async with for_panel(p) as rw:
        rows = [(f"👥 {s['name']}", s["uuid"]) for s in await rw.squads()]
    return rows + [("➕ Новый сквад", "new"), ("⏭ Не добавлять в сквад", "skip")]


def _is_new(v):
    return str(v.get("profile", "")).startswith("new:")


def _needs_domains(v):
    return _is_new(v) or v.get("selfsteal")


async def _email_default(values, ctx):
    return await db.setting("acme_email", "")


@form("node_add", "➕ Новая нода", SERVER_FIELDS + [
    Field("name", "Имя ноды, например <code>DE-1</code>:"),
    Field("profile", "Профиль конфигурации:", "choice", choices=profile_choices),
    Field("selfsteal", "Поставить self-steal (certbot + Nginx на 127.0.0.1:8443)?", "choice",
          choices=[("✅ Да", True), ("Нет", False)], when=lambda v: str(v.get("profile", "")).startswith("p:")),
    Field("domains", "Домены ноды через запятую — все в один сертификат (<code>--cert-name key</code>) и в "
                     "serverNames Reality.\nНапример: <code>de.example.com, cdn.example.space</code>\n"
                     "A-записи → IP ноды, порт 80 должен быть свободен.", "domains", when=_needs_domains),
    Field("email", "E-mail для Let's Encrypt (уведомления о сертификате):", optional=True, default="",
          when=_needs_domains),
    Field("bbr", "Hysteria2: включить BBR (<code>finalmask</code>)? Нужна свежая версия ноды.", "choice",
          choices=[("✅ Да, BBR", True), ("Нет", False)],
          when=lambda v: v.get("profile") in ("new:hy2", "new:both")),
    Field("squad", "В какой внутренний сквад добавить инбаунды?", "choice", choices=squad_choices),
    Field("squad_name", "Название нового сквада:", when=lambda v: v.get("squad") == "new"),
    Field("country", "Код страны (2 буквы, напр. DE):", optional=True, default="XX"),
])
async def node_add_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    if not server:
        return await say(event, "❌ Для ноды нужен SSH-доступ к серверу.")
    name = values["name"]
    domains = [d for d in (values.get("domains") or "").split(",") if d]
    if values.get("email"):
        await db.set_setting("acme_email", values["email"])
    email = values.get("email") or await db.setting("acme_email") or None
    prof_val = values["profile"]
    selfsteal = _is_new(values) or bool(values.get("selfsteal"))
    report = []
    try:
        async with for_panel(p) as rw:
            secret = await rw.keygen()
            if prof_val.startswith("new:"):
                kind = prof_val[4:]
                cfg = xray.build_profile(kind, domains, prefix=name, bbr=values.get("bbr", True))
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
                    await rw.create_host(f"{name} {inb.get('tag')}", domains[0] if domains else server.host,
                                         inb.get("port") or 443, prof["uuid"], inb["uuid"],
                                         sni=domains[0] if domains else None)
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
        return await say(event, "❌ Ошибка API панели:\n<code>" + h(e) + "</code>\n\n" + "\n".join(report))

    n = await db.save(db.Node(panel_id=p.id, server_id=server.id, name=name, domain=",".join(domains) or None,
                              rw_uuid=node.get("uuid"), profile_uuid=prof["uuid"]))
    panel_srv = await db.get(db.Server, p.server_id)
    panel_ip = await _resolve(panel_srv.host if panel_srv else p.domain)
    await say(event, "\n".join(report))
    res = await run_task(event.bot, chat_id(event), server, f"Установка ноды {name}",
                         scripts.node_install(secret, domains, selfsteal, panel_ip, email))
    await say(event, f"{'✅' if res.ok else '⚠️'} Нода <b>{h(name)}</b> готова. Через минуту она должна стать 🟢.\n"
                     "Проверь в подписке, что у Reality стоит <code>flow=xtls-rprx-vision</code>.",
              kb([("🛰 К ноде", f"nd:v:{n.id}"), ("🛰 Ноды", f"pn:nodes:{p.id}")]))


async def _resolve(host: str) -> str | None:
    try:
        return await asyncio.get_running_loop().run_in_executor(None, socket.gethostbyname, host)
    except OSError:
        return None


@router.callback_query(F.data.startswith("pn:addnode:"))
async def node_add(c: CallbackQuery, state: FSMContext):
    p = await db.get(db.Panel, c.data.split(":")[2])
    if not has_api(p):
        return await c.answer("Сначала задай доступ к API панели (токен или логин/пароль)", show_alert=True)
    await start_form(c, state, "node_add", ctx={"panel": p.id})


# ------------------------------------------------------------------ сертификат ноды (certbot, cert-name key)
@router.callback_query(F.data.startswith("crt:v:"))
async def cert_view(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    i = n.id
    await show(c, f"🔐 <b>Сертификат · {h(n.name)}</b>\n"
                  f"Домены: {h(n.domain or '—')}\n"
                  "Файлы: <code>/opt/certbot/certs/live/key/</code> (в ноде — <code>/etc/letsencrypt/live/key/</code>)\n"
                  "Автопродление: cron 28 числа → renew → reload nginx → restart remnanode.",
               kb([("📊 Статус", f"crt:st:{i}"), ("🧪 Проверка продления", f"crt:dry:{i}")],
                  [("🔁 Продлить сейчас", f"crt:renew:{i}"), ("➕ Добавить домены", f"crt:add:{i}")],
                  [("⬅️ К ноде", f"nd:v:{i}")]))


@router.callback_query(F.data.startswith(("crt:st:", "crt:dry:", "crt:renew:")))
async def cert_action(c: CallbackQuery):
    kind, i = c.data.split(":")[1], c.data.split(":")[2]
    n = await db.get(db.Node, i)
    if s := await _node_server(c, n):
        script, title = {"st": (scripts.CERT_STATUS, "статус"), "dry": (scripts.CERT_DRYRUN, "dry-run"),
                         "renew": (scripts.CERT_RENEW, "продление")}[kind]
        await c.answer()
        await run_task(c.bot, c.message.chat.id, s, f"Сертификат {n.name}: {title}", script)


@form("crt_add", "➕ Домены в сертификат", [
    Field("domains", "Какие домены добавить (через запятую)? Старые останутся.", "domains"),
    Field("profile", "Добавить их и в serverNames Reality профиля ноды?", "choice",
          choices=[("✅ Да", True), ("Нет", False)]),
])
async def crt_add_done(event, values, ctx):
    n = await db.get(db.Node, ctx["id"])
    s = await db.get(db.Server, n.server_id)
    if not s:
        return await say(event, "❌ У ноды нет сервера.")
    old = [d for d in (n.domain or "").split(",") if d]
    new = [d for d in values["domains"].split(",") if d]
    allds = list(dict.fromkeys(old + new))
    res = await run_task(event.bot, chat_id(event), s, f"Сертификат {n.name}: {', '.join(allds)}",
                         scripts.cert_issue(allds, await db.setting("acme_email") or None, expand=True))
    if not res.ok:
        return await say(event, "⚠️ Сертификат не выпущен — домены не сохраняю.")
    n.domain = ",".join(allds)
    await db.save(n)
    msg = "✅ Сертификат обновлён."
    if values["profile"] and n.profile_uuid:
        p = await db.get(db.Panel, n.panel_id)
        try:
            async with for_panel(p) as rw:
                pr = await rw.profile(n.profile_uuid)
                cfg = pr["config"]
                for inb in cfg.get("inbounds", []):
                    rs = (inb.get("streamSettings") or {}).get("realitySettings")
                    if rs is not None:
                        rs["serverNames"] = list(dict.fromkeys((rs.get("serverNames") or []) + new))
                await rw.update_profile(n.profile_uuid, cfg)
            msg += " serverNames в профиле обновлены."
        except (RwError, KeyError) as e:
            msg += f"\n⚠️ Профиль не обновил: <code>{h(e)}</code>"
    await say(event, msg, kb([("⬅️ Сертификат", f"crt:v:{n.id}")]))


@router.callback_query(F.data.startswith("crt:add:"))
async def crt_add(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "crt_add", ctx={"id": int(c.data.split(":")[2])})


# ------------------------------------------------------------------ хосты
def _hidden_profiles(nodes: list) -> callable:
    """Хосты профилей, на которых нет ни одной ноды (балансер link-extractor и т.п.), скрываем."""
    used = {(n.get("configProfile") or {}).get("activeConfigProfileUuid") for n in nodes}

    def hidden(host: dict) -> bool:
        prof = (host.get("inbound") or {}).get("configProfileUuid")
        remark = (host.get("remark") or "").lower()
        return "extractor" in remark or (prof is not None and prof not in used)
    return hidden


@router.callback_query(F.data.startswith("hs:list:"))
async def hosts_list(c: CallbackQuery):
    _, _, pid, show_all = c.data.split(":")
    await _hosts_screen(c, pid, show_all)


async def _hosts_screen(c: CallbackQuery, pid, show_all: str):
    p = await db.get(db.Panel, pid)
    try:
        async with for_panel(p) as rw:
            hosts, nodes = await rw.hosts(), await rw.nodes()
    except RwError as e:
        return await show(c, f"❌ <code>{h(e)}</code>", kb([("⬅️ К панели", f"pn:v:{p.id}")]))
    hidden = _hidden_profiles(nodes)
    visible = [x for x in hosts if show_all == "1" or not hidden(x)]
    n_hidden = len(hosts) - len([x for x in hosts if not hidden(x)])
    rows = [[(f"{'⏸' if x.get('isDisabled') else '🌐'} {x.get('remark')} · {x.get('address')}:{x.get('port')}",
              f"hs:v:{p.id}:{x['uuid']}")] for x in visible]
    rows.append([("➕ Добавить хост", f"hs:new:{p.id}")])
    if n_hidden:
        rows.append([("👁 Показать скрытые" if show_all == "0" else "🙈 Скрыть служебные",
                      f"hs:list:{p.id}:{'1' if show_all == '0' else '0'}")])
    rows.append([("⬅️ К панели", f"pn:v:{p.id}")])
    await show(c, f"🌐 <b>Хосты · {h(p.name)}</b> ({len(visible)})"
                  + (f"\nСкрыто служебных (экстрактор/балансер): {n_hidden}" if n_hidden and show_all == "0" else ""),
               kb(*rows))


async def _host(p: db.Panel, uuid: str) -> dict | None:
    async with for_panel(p) as rw:
        return next((x for x in await rw.hosts() if x.get("uuid") == uuid), None)


@router.callback_query(F.data.startswith("hs:v:"))
async def host_view(c: CallbackQuery):
    _, _, pid, uuid = c.data.split(":", 3)
    await _host_screen(c, pid, uuid)


async def _host_screen(c: CallbackQuery, pid, uuid: str):
    p = await db.get(db.Panel, pid)
    try:
        x = await _host(p, uuid)
    except RwError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    if not x:
        return await c.answer("Хост не найден", show_alert=True)
    ref = f"{p.id}:{uuid}"
    await show(c, f"🌐 <b>{h(x.get('remark'))}</b> {'⏸ выключен' if x.get('isDisabled') else ''}\n"
                  f"Адрес: <code>{h(x.get('address'))}:{x.get('port')}</code>\n"
                  f"SNI: <code>{h(x.get('sni') or '—')}</code> · Host: <code>{h(x.get('host') or '—')}</code>\n"
                  f"Инбаунд: <code>{h((x.get('inbound') or {}).get('configProfileInboundUuid', '—'))}</code>",
               kb([("✏️ Адрес", f"hs:ed:{ref}:address"), ("✏️ Порт", f"hs:ed:{ref}:port")],
                  [("✏️ Название", f"hs:ed:{ref}:remark"), ("✏️ SNI", f"hs:ed:{ref}:sni")],
                  [("▶️ Включить" if x.get("isDisabled") else "⏸ Выключить", f"hs:tg:{ref}"),
                   ("🗑 Удалить", f"hs:del:{ref}")],
                  [("⬅️ Хосты", f"hs:list:{p.id}:0")]))


@form("hs_ed", "✏️ Хост", [Field("value", "Новое значение:")])
async def hs_ed_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    val = values["value"]
    if ctx["field"] == "port":
        if not val.isdigit():
            return await say(event, "❌ Порт — число.")
        val = int(val)
    try:
        async with for_panel(p) as rw:
            await rw.update_host(ctx["uuid"], **{ctx["field"]: val})
    except RwError as e:
        return await say(event, f"❌ <code>{h(e)}</code>")
    await say(event, "✅ Сохранено.", kb([("⬅️ К хосту", f"hs:v:{p.id}:{ctx['uuid']}")]))


@router.callback_query(F.data.startswith("hs:ed:"))
async def host_edit(c: CallbackQuery, state: FSMContext):
    _, _, pid, uuid, field = c.data.split(":")
    await start_form(c, state, "hs_ed", ctx={"panel": int(pid), "uuid": uuid, "field": field})


@router.callback_query(F.data.startswith("hs:tg:"))
async def host_toggle(c: CallbackQuery):
    _, _, pid, uuid = c.data.split(":", 3)
    p = await db.get(db.Panel, pid)
    try:
        x = await _host(p, uuid)
        async with for_panel(p) as rw:
            await rw.set_hosts_enabled([uuid], bool(x.get("isDisabled")))
    except RwError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await _host_screen(c, pid, uuid)


@router.callback_query(F.data.startswith("hs:del:"))
async def host_del(c: CallbackQuery):
    _, _, pid, uuid = c.data.split(":", 3)
    await show(c, "🗑 Удалить хост?", kb([("🗑 Да", f"hs:delok:{pid}:{uuid}"), ("✖️ Нет", f"hs:v:{pid}:{uuid}")]))


@router.callback_query(F.data.startswith("hs:delok:"))
async def host_del_ok(c: CallbackQuery):
    _, _, pid, uuid = c.data.split(":", 3)
    p = await db.get(db.Panel, pid)
    try:
        async with for_panel(p) as rw:
            await rw.delete_host(uuid)
    except RwError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await _hosts_screen(c, pid, "0")


async def _profiles_only(values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    async with for_panel(p) as rw:
        return [(f"🧬 {pr['name']}", pr["uuid"]) for pr in await rw.profiles()]


async def _inbounds_of(values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    async with for_panel(p) as rw:
        pr = await rw.profile(values["profile"])
    return [(f"{i.get('tag')} · {i.get('type', '')} :{i.get('port', '')}", f"{i['uuid']}|{i.get('port') or 443}")
            for i in pr.get("inbounds") or []]


@form("hs_new", "➕ Новый хост", [
    Field("profile", "Профиль:", "choice", choices=_profiles_only),
    Field("inbound", "Инбаунд:", "choice", choices=_inbounds_of),
    Field("remark", "Название хоста (видно в клиенте):"),
    Field("address", "Адрес (домен или IP ноды):"),
    Field("port", "Порт:", "int", optional=True, default=None),
    Field("sni", "SNI (для Reality — один из serverNames):", optional=True, default=""),
])
async def hs_new_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    inb_uuid, inb_port = values["inbound"].split("|")
    try:
        async with for_panel(p) as rw:
            await rw.create_host(values["remark"], values["address"], values.get("port") or int(inb_port),
                                 values["profile"], inb_uuid, sni=values.get("sni") or None)
    except RwError as e:
        return await say(event, f"❌ <code>{h(e)}</code>", kb([("⬅️ Хосты", f"hs:list:{p.id}:0")]))
    await say(event, "✅ Хост создан.", kb([("⬅️ Хосты", f"hs:list:{p.id}:0")]))


@router.callback_query(F.data.startswith("hs:new:"))
async def host_new(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "hs_new", ctx={"panel": int(c.data.split(":")[2])})


# ------------------------------------------------------------------ профили
@router.callback_query(F.data.startswith("pn:prof:"))
async def prof_list(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    try:
        async with for_panel(p) as rw:
            profiles = await rw.profiles()
    except RwError as e:
        return await show(c, f"❌ <code>{h(e)}</code>", kb([("⬅️ К панели", f"pn:v:{p.id}")]))
    lines = [f"🧬 <b>Профили {h(p.name)}</b>\n"]
    rows = []
    for pr in profiles:
        tags = ", ".join(i.get("tag", "?") for i in pr.get("inbounds") or [])
        lines.append(f"• <b>{h(pr['name'])}</b>: {h(tags) or '—'}")
        rows.append([(f"📄 {pr['name']}", f"pf:dl:{p.id}:{pr['uuid']}")])
    rows.append([("✨ Создать из шаблона", f"pf:new:{p.id}"), ("📥 Загрузить JSON", f"pf:up:{p.id}")])
    rows.append([("⬅️ К панели", f"pn:v:{p.id}")])
    await show(c, "\n".join(lines) + "\n\n📄 — скачать JSON профиля\n"
                  "Шаблоны: VLESS Reality self-steal и Hysteria2 по мануалу.", kb(*rows))


@form("pf_up", "📥 Загрузить профиль", [
    Field("json", "Пришли JSON профиля Xray <b>файлом</b> или текстом (как в редакторе профиля панели):", "raw"),
    Field("name", "Название профиля:", optional=True, default=""),
])
async def pf_up_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    try:
        cfg = json.loads(values["json"])
        if not isinstance(cfg, dict) or "inbounds" not in cfg:
            raise ValueError("нет поля inbounds")
    except ValueError as e:
        return await say(event, f"❌ Это не JSON профиля: <code>{h(e)}</code>", kb([("⬅️ Профили", f"pn:prof:{p.id}")]))
    name = values.get("name") or (values.get("_filename") or "profile").removesuffix(".json")
    try:
        async with for_panel(p) as rw:
            pr = await rw.create_profile(name, cfg)
    except RwError as e:
        return await say(event, f"❌ <code>{h(e)}</code>", kb([("⬅️ Профили", f"pn:prof:{p.id}")]))
    await say(event, f"✅ Профиль <b>{h(pr.get('name', name))}</b> загружен, инбаундов: {len(pr.get('inbounds') or [])}.",
              kb([("⬅️ Профили", f"pn:prof:{p.id}")]))


@router.callback_query(F.data.startswith("pf:up:"))
async def pf_up(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "pf_up", ctx={"panel": int(c.data.split(":")[2])})


@router.callback_query(F.data.startswith("pf:dl:"))
async def prof_download(c: CallbackQuery):
    _, _, pid, uuid = c.data.split(":", 3)
    p = await db.get(db.Panel, pid)
    try:
        async with for_panel(p) as rw:
            pr = await rw.profile(uuid)
    except RwError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer()
    await c.message.answer_document(BufferedInputFile(
        json.dumps(pr.get("config", pr), ensure_ascii=False, indent=2).encode(),
        filename=f"{pr.get('name', 'profile')}.json"))


@form("pf_new", "✨ Новый профиль", [
    Field("kind", "Тип профиля:", "choice", choices=[(v, k) for k, v in xray.KINDS.items()]),
    Field("name", "Название профиля:"),
    Field("domains", "Домены self-steal через запятую (serverNames Reality):", "domains",
          when=lambda v: v.get("kind") != "hy2"),
    Field("bbr", "Hysteria2: BBR через finalmask?", "choice", choices=[("✅ Да", True), ("Нет", False)],
          when=lambda v: v.get("kind") in ("hy2", "both")),
])
async def pf_new_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    try:
        async with for_panel(p) as rw:
            pr = await rw.create_profile(values["name"], xray.build_profile(
                values["kind"], values.get("domains") or "", prefix=values["name"], bbr=values.get("bbr", True)))
    except RwError as e:
        return await say(event, f"❌ <code>{h(e)}</code>", kb([("⬅️ Профили", f"pn:prof:{p.id}")]))
    await say(event, f"✅ Профиль <b>{h(pr.get('name'))}</b> создан, инбаундов: {len(pr.get('inbounds') or [])}.\n"
                     "На ноде нужен сертификат в /etc/letsencrypt/live/key/ и Nginx на 127.0.0.1:8443 "
                     "(ставятся при «➕ Добавить ноду»).",
              kb([("⬅️ Профили", f"pn:prof:{p.id}")]))


@router.callback_query(F.data.startswith("pf:new:"))
async def pf_new(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "pf_new", ctx={"panel": int(c.data.split(":")[2])})


# ------------------------------------------------------------------ сквады
@router.callback_query(F.data.startswith("pn:sq:"))
async def squad_list(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    try:
        async with for_panel(p) as rw:
            squads = await rw.squads()
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
        async with for_panel(p) as rw:
            inb = []
            if values["profile"] != "none":
                inb = [i["uuid"] for i in (await rw.profile(values["profile"][2:])).get("inbounds") or []]
            await rw.create_squad(values["name"], inb)
    except RwError as e:
        return await say(event, f"❌ <code>{h(e)}</code>", kb([("⬅️ Сквады", f"pn:sq:{p.id}")]))
    await say(event, f"✅ Сквад <b>{h(values['name'])}</b> создан ({len(inb)} инбаундов).",
              kb([("⬅️ Сквады", f"pn:sq:{p.id}")]))


@router.callback_query(F.data.startswith("sq:new:"))
async def sq_new(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "sq_new", ctx={"panel": int(c.data.split(":")[2])})
