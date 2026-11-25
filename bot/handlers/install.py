"""Раздел 1: «Установка проектов» — Remnawave, Bedolaga Bot, Bedolaga Cabinet."""
import secrets
import string

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import crypto, db, scripts
from ..forms import Field, form, start_form
from ..rw_api import RwError, for_panel
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show
from .servers import SERVER_FIELDS, ensure_server

router = Router(name="install")


def strong_password(n: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(ch.islower() for ch in p) and any(ch.isupper() for ch in p) and any(ch.isdigit() for ch in p):
            return p


@router.callback_query(F.data == "inst")
async def install_menu(c: CallbackQuery):
    await show(c, "📦 <b>Установка проектов</b>\n\n"
                  "Перед установкой направь A-записи доменов на нужный сервер — Caddy сам выпустит сертификаты.\n"
                  "SSH-доступ к серверу выберешь в первом шаге: по паролю или по ключу.",
               kb([("🌊 Remnawave (панель + подписка)", "inst:rw")],
                  [("🤖 Bedolaga Bot", "inst:bb")],
                  [("🗂 Bedolaga Cabinet", "inst:cab")],
                  [("🔗 Подключить уже установленную панель", "inst:rwx")],
                  [("⬅️ Назад", "menu")]))


# ------------------------------------------------------------------ Remnawave
@form("inst_rw", "🌊 Установка Remnawave", SERVER_FIELDS + [
    Field("name", "Название панели (для себя):"),
    Field("domain", "Домен панели, например <code>panel.example.com</code>:", "domain"),
    Field("sub", "Домен страницы подписки, например <code>sub.example.com</code>:", "domain"),
    Field("admin", "Логин супер-админа:", optional=True, default="admin"),
])
async def inst_rw_done(event, values, ctx):
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    admin_pass = strong_password()
    panel = await db.save(db.Panel(
        name=values["name"], server_id=server.id, domain=values["domain"], sub_domain=values["sub"],
        api_url=f"https://{values['domain']}", admin_user=values["admin"] or "admin",
        admin_pass_enc=crypto.enc(admin_pass), managed_proxy=True))
    res = await run_task(event.bot, chat_id(event), server, f"Установка Remnawave {panel.domain}",
                         scripts.remnawave_install(panel.domain, panel.sub_domain, panel.admin_user, admin_pass))
    token = res.marks.get("RW_TOKEN")
    if token:
        panel.api_token_enc = crypto.enc(token)
        await db.save(panel)
    text = (f"{'✅' if res.ok else '⚠️'} <b>Remnawave: {h(panel.name)}</b>\n"
            f"Панель: https://{h(panel.domain)}\nПодписка: https://{h(panel.sub_domain)}\n"
            f"Логин: <code>{h(panel.admin_user)}</code>\n"
            f"Пароль: <tg-spoiler><code>{h(admin_pass)}</code></tg-spoiler>\n"
            f"API-токен: {'сохранён ✅' if token else '❌ не получен — задай вручную'}\n\n"
            "Пароль и токен лежат в БД (🔐 Доступы → Панели).")
    await say(event, text, kb([("🖥 К панели", f"pn:v:{panel.id}")],
                              [("🔑 Задать API-токен", f"pn:tok:{panel.id}")] if not token else [],
                              [("🏠 Меню", "menu")]))


@router.callback_query(F.data == "inst:rw")
async def inst_rw(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "inst_rw")


# ------------------------------------------------------------------ существующая панель
@form("inst_rwx", "🔗 Подключить панель", [
    Field("name", "Название панели (для себя):"),
    Field("domain", "Домен панели:", "domain"),
    Field("token", "API-токен (Панель → Настройки → API-токены). Сообщение удалю:", secret=True),
    Field("sub", "Домен подписки (если есть):", "domain", optional=True),
] + SERVER_FIELDS)
async def inst_rwx_done(event, values, ctx):
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    panel = db.Panel(name=values["name"], server_id=server.id if server else None, domain=values["domain"],
                     sub_domain=values.get("sub"), api_url=f"https://{values['domain']}",
                     api_token_enc=crypto.enc(values["token"]), managed_proxy=False)
    try:
        rw = for_panel(panel)
        nodes = await rw.nodes()
        await rw.close()
        status = f"✅ API отвечает, нод в панели: {len(nodes)}"
    except RwError as e:
        status = f"⚠️ API не ответил: <code>{h(e)}</code>"
    panel = await db.save(panel)
    await say(event, f"Панель <b>{h(panel.name)}</b> добавлена.\n{status}",
              kb([("🖥 К панели", f"pn:v:{panel.id}"), ("🏠 Меню", "menu")]))


@router.callback_query(F.data == "inst:rwx")
async def inst_rwx(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "inst_rwx", ctx={"allow_none": True})


# ------------------------------------------------------------------ Bedolaga Bot
async def panel_choices(values, ctx):
    return [(f"🖥 {p.name} ({p.domain})", p.id) for p in await db.all_(db.Panel)]


@form("inst_bb", "🤖 Установка Bedolaga Bot", SERVER_FIELDS + [
    Field("panel", "К какой панели Remnawave подключить бота?", "choice", choices=panel_choices),
    Field("name", "Название бота (для себя):"),
    Field("token", "Токен бота от @BotFather (сообщение удалю):", secret=True),
    Field("admins", "Telegram ID админов через запятую:", "ids"),
    Field("hooks", "Домен для вебхуков/API бота, например <code>hooks.example.com</code>:", "domain"),
])
async def inst_bb_done(event, values, ctx):
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    panel = await db.get(db.Panel, values["panel"])
    rw_token = crypto.dec(panel.api_token_enc)
    if not rw_token:
        return await say(event, "❌ У панели нет API-токена. Задай его и повтори.",
                         kb([("🔑 Задать токен", f"pn:tok:{panel.id}")]))
    local = panel.server_id == server.id
    web_token = secrets.token_hex(32)
    script = scripts.bedolaga_install(
        bot_token=values["token"], admin_ids=values["admins"], hooks_domain=values["hooks"],
        rw_url="http://remnawave:3000" if local else panel.api_url, rw_key=rw_token, local=local,
        web_api_token=web_token, webhook_secret=secrets.token_hex(32), pg_pass=secrets.token_hex(16),
        rw_cookie=crypto.dec(panel.cookie_enc) if (panel.protected and not local) else None)
    b = await db.save(db.Bedolaga(name=values["name"], server_id=server.id, panel_id=panel.id,
                                  bot_token_enc=crypto.enc(values["token"]), admin_ids=values["admins"],
                                  hooks_domain=values["hooks"], web_api_token_enc=crypto.enc(web_token)))
    res = await run_task(event.bot, chat_id(event), server, f"Установка Bedolaga {b.name}", script)
    if res.marks.get("BEDOLAGA_COMPOSE"):
        b.compose_file = res.marks["BEDOLAGA_COMPOSE"]
        await db.save(b)
    await say(event, f"{'✅' if res.ok else '⚠️'} Bedolaga <b>{h(b.name)}</b>\n"
                     f"Вебхуки и API: https://{h(b.hooks_domain)}\n"
                     f"Режим: {'локально рядом с панелью' if local else 'отдельный сервер'} · compose: <code>{h(b.compose_file)}</code>\n"
                     "Web API токен сохранён (🔐 Доступы → Боты).",
              kb([("🗂 Поставить кабинет", "inst:cab")], [("🏠 Меню", "menu")]))


@router.callback_query(F.data == "inst:bb")
async def inst_bb(c: CallbackQuery, state: FSMContext):
    if not await db.all_(db.Panel):
        return await show(c, "Сначала установи или подключи панель Remnawave.", kb([("⬅️ Назад", "inst")]))
    await start_form(c, state, "inst_bb")


# ------------------------------------------------------------------ Bedolaga Cabinet
async def bedolaga_choices(values, ctx):
    return [(f"🤖 {b.name} ({b.hooks_domain})", b.id) for b in await db.all_(db.Bedolaga)]


@form("inst_cab", "🗂 Установка Bedolaga Cabinet", [
    Field("bot", "Для какого бота Bedolaga? (кабинет ставится на тот же сервер)", "choice", choices=bedolaga_choices),
    Field("domain", "Домен кабинета, например <code>cabinet.example.com</code>:", "domain"),
])
async def inst_cab_done(event, values, ctx):
    b = await db.get(db.Bedolaga, values["bot"])
    server = await db.get(db.Server, b.server_id)
    if not server:
        return await say(event, "❌ У бота не привязан сервер.")
    res = await run_task(event.bot, chat_id(event), server, f"Установка кабинета {values['domain']}",
                         scripts.cabinet_install(compose_file=b.compose_file, cabinet_domain=values["domain"],
                                                 hooks_domain=b.hooks_domain))
    if res.ok:
        b.cabinet, b.cabinet_domain = True, values["domain"]
        await db.save(b)
    await say(event, f"{'✅' if res.ok else '⚠️'} Кабинет: https://{h(values['domain'])}\n"
                     "Должен открыться экран входа через Telegram. Не забудь указать домен в @BotFather → "
                     "Bot Settings → Domain (для Telegram Login).", kb([("🏠 Меню", "menu")]))


@router.callback_query(F.data == "inst:cab")
async def inst_cab(c: CallbackQuery, state: FSMContext):
    if not await db.all_(db.Bedolaga):
        return await show(c, "Сначала установи Bedolaga Bot.", kb([("⬅️ Назад", "inst")]))
    await start_form(c, state, "inst_cab")
