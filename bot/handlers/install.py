"""Раздел 1: «Установка» — Remnawave, Bedolaga Bot, Bedolaga Cabinet, подключение существующей панели."""
import dataclasses
import json
import secrets
import string

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import crypto, db, proxy, scripts
from ..forms import Field, form, start_form
from ..rw_api import RwError, for_panel
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show
from .panels import ensure_api_token
from .servers import SERVER_FIELDS, ensure_server

router = Router(name="install")


def strong_password(n: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(ch.islower() for ch in p) and any(ch.isupper() for ch in p) and any(ch.isdigit() for ch in p):
            return p


def new_cookie() -> str:
    return secrets.token_urlsafe(24).replace("-", "x").replace("_", "y")


def fields_when(fields: list, cond) -> list:
    """Копия полей, которые показываются только при cond(values)."""
    out = []
    for f in fields:
        old = f.when
        out.append(dataclasses.replace(f, when=lambda v, old=old: cond(v) and (old is None or old(v))))
    return out


PROXY_CHOICES = [("Caddy — сертификаты сам (рекомендую)", "caddy"), ("Nginx + certbot", "nginx")]


def _login_url(domain: str, cookie: str | None) -> str:
    return f"https://{domain}/?vmkey={cookie}" if cookie else f"https://{domain}"


async def _pick_proxy(event, server: db.Server, wanted: str | None) -> str:
    """На сервере может быть только один наш прокси — если он уже есть, используем его."""
    kind = server.proxy or wanted or "caddy"
    if server.proxy and wanted and wanted != server.proxy:
        await say(event, f"ℹ️ На сервере уже стоит {server.proxy.capitalize()} от бота — использую его.")
    if server.proxy != kind:
        server.proxy = kind
        await db.save(server)
    return kind


@router.callback_query(F.data == "inst")
async def install_menu(c: CallbackQuery):
    await show(c, "📦 <b>Установка</b>\n\n"
                  "Перед установкой направь A-записи доменов на нужный сервер.\n"
                  "Сервер выбираешь в первом шаге (или добавляешь новый: IP, пользователь, пароль или SSH-ключ).",
               kb([("🌊 Remnawave (панель + подписка)", "inst:rw")],
                  [("🤖 Bedolaga Bot", "inst:bb")],
                  [("🗂 Bedolaga Cabinet", "inst:cab")],
                  [("🔗 Подключить существующую панель", "inst:rwx")],
                  [("⬅️ Назад", "menu")]))


# ------------------------------------------------------------------ Remnawave
@form("inst_rw", "🌊 Установка Remnawave", SERVER_FIELDS + [
    Field("name", "Название панели (для себя):"),
    Field("domain", "Домен панели, например <code>panel.example.com</code>:", "domain"),
    Field("sub", "Домен страницы подписки, например <code>sub.example.com</code>:", "domain"),
    Field("admin", "Логин супер-админа (пароль сгенерирую сам):", optional=True, default="admin"),
    Field("proxy", "Reverse proxy:", "choice", choices=PROXY_CHOICES),
    Field("protect", "Закрыть панель секретным путём (docs.rw/install/panel-security)?\n"
                     "Вход будет только по ссылке <code>https://панель/?vmkey=…</code>, остальным — обрыв.",
          "choice", choices=[("🔒 Да, закрыть", True), ("Нет", False)]),
    Field("email", "E-mail для Let's Encrypt:", optional=True, default="", when=lambda v: v.get("proxy") == "nginx"),
])
async def inst_rw_done(event, values, ctx):
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    admin_pass = strong_password()
    cookie = new_cookie() if values.get("protect") else None
    panel = await db.save(db.Panel(
        name=values["name"], server_id=server.id, domain=values["domain"], sub_domain=values["sub"],
        api_url=f"https://{values['domain']}", admin_user=values["admin"] or "admin",
        admin_pass_enc=crypto.enc(admin_pass), managed_proxy=True, sub_local=True))
    res = await run_task(event.bot, chat_id(event), server, f"Установка Remnawave {panel.domain}",
                         scripts.remnawave_install(panel.domain, panel.sub_domain, panel.admin_user, admin_pass))
    token = res.marks.get("RW_TOKEN")
    if token:
        panel.api_token_enc = crypto.enc(token)
    kind = await _pick_proxy(event, server, values.get("proxy"))
    pres = await run_task(event.bot, chat_id(event), server, f"{kind.capitalize()}: панель и подписка",
                          proxy.apply_script(kind, "panel", proxy.panel_sites(panel, cookie), values.get("email") or None))
    if pres.ok and cookie:
        panel.protected, panel.cookie_enc = True, crypto.enc(cookie)
    panel = await db.save(panel)
    text = (f"{'✅' if res.ok and pres.ok else '⚠️'} <b>Remnawave: {h(panel.name)}</b>\n"
            f"Вход: <tg-spoiler>{h(_login_url(panel.domain, cookie if panel.protected else None))}</tg-spoiler>\n"
            f"Подписка: https://{h(panel.sub_domain)}\n"
            f"Логин: <code>{h(panel.admin_user)}</code>\n"
            f"Пароль: <tg-spoiler><code>{h(admin_pass)}</code></tg-spoiler>\n"
            f"API-токен: {'<tg-spoiler><code>' + h(token) + '</code></tg-spoiler>' if token else '❌ не получен — задай вручную'}\n"
            f"Прокси: {kind.capitalize()} · секретный путь: {'🔒 вкл' if panel.protected else 'выкл'}\n\n"
            "Всё сохранено в БД (📇 Доступы → Панели).")
    await say(event, text, kb([("🖥 К панели", f"pn:v:{panel.id}")], [("🏠 Меню", "menu")]))


@router.callback_query(F.data == "inst:rw")
async def inst_rw(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "inst_rw")


# ------------------------------------------------------------------ подключение существующей панели
def _has_srv(v):
    return v.get("server") not in (None, "none")


@form("inst_rwx", "🔗 Подключить существующую панель", [
    Field("name", "Название панели (для себя):"),
    Field("domain", "Домен панели:", "domain"),
    Field("auth", "Как бот будет ходить в API панели?", "choice",
          choices=[("🔑 API-токен", "token"), ("👤 Логин и пароль админа", "cred"), ("Оба", "both")]),
    Field("token", "API-токен (Панель → Настройки → API-токены). Сообщение удалю:", secret=True,
          when=lambda v: v.get("auth") in ("token", "both")),
    Field("user", "Логин админа панели:", when=lambda v: v.get("auth") in ("cred", "both")),
    Field("pw", "Пароль админа (сообщение удалю):", secret=True, when=lambda v: v.get("auth") in ("cred", "both")),
] + SERVER_FIELDS + [
    Field("stack", "Что стоит на сервере панели?", "choice",
          choices=[("Только панель", "panel"), ("Панель + бот Bedolaga", "panel_bot")], when=_has_srv),
    Field("sub_where", "Где страница подписки?", "choice",
          choices=[("На этом же сервере", "here"), ("На отдельном сервере", "other"), ("Её нет", "none")]),
    Field("sub", "Домен страницы подписки:", "domain", when=lambda v: v.get("sub_where") in ("here", "other")),
    Field("le", "Стоит ли на сервере Link Extractor?", "choice", choices=[("Да", True), ("Нет", False)], when=_has_srv),
    Field("hooks", "Домен вебхуков/API бота Bedolaga (чтобы прокси его не потерял):", "domain", optional=True,
          default="", when=lambda v: v.get("stack") == "panel_bot"),
    Field("cabinet", "Домен кабинета Bedolaga (если есть):", "domain", optional=True, default="",
          when=lambda v: v.get("stack") == "panel_bot"),
    Field("proxy", "Настроить reverse proxy на сервере?\n"
                   "Бот забэкапит текущий прокси, поставит свой, проверит панель и при ошибке откатит.", "choice",
          choices=[("🔒 Caddy + секретный путь", "caddy:1"), ("🔒 Nginx + секретный путь", "nginx:1"),
                   ("Caddy без защиты", "caddy:0"), ("Nginx без защиты", "nginx:0"),
                   ("Не трогать прокси", "skip")], when=_has_srv),
    Field("email", "E-mail для Let's Encrypt:", optional=True, default="",
          when=lambda v: str(v.get("proxy", "")).startswith("nginx")),
])
async def inst_rwx_done(event, values, ctx):
    try:
        server = await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    panel = db.Panel(name=values["name"], server_id=server.id if server else None, domain=values["domain"],
                     sub_domain=values.get("sub"), sub_local=values.get("sub_where") == "here",
                     api_url=f"https://{values['domain']}", api_token_enc=crypto.enc(values.get("token")),
                     admin_user=values.get("user"), admin_pass_enc=crypto.enc(values.get("pw")),
                     link_extractor=bool(values.get("le")), managed_proxy=False)
    try:
        async with for_panel(panel) as rw:
            nodes = await rw.nodes()
        status = f"✅ API отвечает, нод в панели: {len(nodes)}"
    except RwError as e:
        status = f"⚠️ API не ответил: <code>{h(e)}</code>"
    panel = await db.save(panel)
    plan = values.get("proxy")
    if not server or not plan or plan == "skip":
        return await say(event, f"Панель <b>{h(panel.name)}</b> добавлена.\n{status}",
                         kb([("🖥 К панели", f"pn:v:{panel.id}"), ("🏠 Меню", "menu")]))
    kind, protect = plan.split(":")
    await db.set_setting(f"rwx:{panel.id}", json.dumps({
        "kind": kind, "protect": protect == "1", "hooks": values.get("hooks") or None,
        "cabinet": values.get("cabinet") or None, "email": values.get("email") or None}))
    sites = [f"• панель {panel.domain}" + (" 🔒 секретный путь" if protect == "1" else "")]
    if panel.sub_domain and panel.sub_local:
        sites.append(f"• подписка {panel.sub_domain} → " + ("Link Extractor" if panel.link_extractor else "subscription-page"))
    if panel.link_extractor:
        sites.append("• /link-extractor/ → Link Extractor")
    if values.get("hooks"):
        sites.append(f"• бот {values['hooks']}")
    if values.get("cabinet"):
        sites.append(f"• кабинет {values['cabinet']}")
    await say(event, f"Панель <b>{h(panel.name)}</b> добавлена.\n{status}\n\n"
                     f"<b>План для {h(server.name)}</b> ({kind.capitalize()}):\n" + h("\n".join(sites)) + "\n\n"
                     "1. Текущий прокси на 80/443 бэкаплю в <code>/opt/vpn-master-backup/</code> и останавлю.\n"
                     "2. Ставлю свой и проверяю, что панель открывается.\n"
                     "3. Если не открылась — возвращаю старый прокси.\n"
                     "Панель может быть недоступна около минуты.",
              kb([("✅ Настроить прокси", f"rwx:go:{panel.id}")], [("Позже", f"pn:v:{panel.id}")]))


@router.callback_query(F.data == "inst:rwx")
async def inst_rwx(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "inst_rwx", ctx={"allow_none": True})


@router.callback_query(F.data.startswith("rwx:go:"))
async def rwx_go(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    server = await db.get(db.Server, p.server_id)
    raw = await db.setting(f"rwx:{p.id}")
    if not (server and raw):
        return await c.answer("План устарел — подключи панель заново", show_alert=True)
    plan = json.loads(raw)
    kind = plan["kind"]
    if server.proxy and server.proxy != kind:
        kind = server.proxy
    cookie = new_cookie() if plan["protect"] else None
    extra = []
    if plan.get("hooks"):
        extra.append(("bedolaga", proxy.bedolaga_sites(plan["hooks"])))
    if plan.get("cabinet"):
        extra.append(("cabinet", proxy.cabinet_sites(plan["cabinet"], "remnawave_bot:8080")))
    await c.answer()
    res = await run_task(c.bot, c.message.chat.id, server, f"Перенастройка прокси → {kind.capitalize()}",
                         proxy.takeover_script(kind, proxy.panel_sites(p, cookie), extra, p.domain, cookie,
                                               plan.get("email")))
    if not res.ok:
        return await say(c, "❌ Не получилось — старый прокси возвращён. Лог выше.",
                         kb([("🔁 Ещё раз", f"rwx:go:{p.id}"), ("🖥 К панели", f"pn:v:{p.id}")]))
    server.proxy = kind
    await db.save(server)
    p.managed_proxy = True
    p.protected, p.cookie_enc = bool(cookie), crypto.enc(cookie) if cookie else None
    await db.save(p)
    await db.set_setting(f"rwx:{p.id}", None)
    await say(c, f"✅ Прокси {kind.capitalize()} настроен.\n"
                 f"Вход в панель: <tg-spoiler>{h(_login_url(p.domain, cookie))}</tg-spoiler>\n"
                 f"Бэкап старого прокси: <code>{h(res.marks.get('BACKUP', '—'))}</code>",
              kb([("🖥 К панели", f"pn:v:{p.id}")]))


# ------------------------------------------------------------------ Bedolaga Bot
async def panel_choices(values, ctx):
    return [(f"🖥 {p.name} ({p.domain})", p.id) for p in await db.all_(db.Panel)]


async def bot_where_choices(values, ctx):
    p = await db.get(db.Panel, values["panel"])
    srv = await db.get(db.Server, p.server_id) if p else None
    rows = [(f"🖥 На сервер панели ({srv.name}) — по мануалу, в одной сети с панелью", "same")] if srv else []
    return rows + [("📦 На отдельный сервер", "other")]


def _other(v):
    return v.get("where") == "other"


@form("inst_bb", "🤖 Установка Bedolaga Bot", [
    Field("panel", "К какой панели Remnawave подключить бота?", "choice", choices=panel_choices),
    Field("where", "Куда ставить бота?", "choice", choices=bot_where_choices),
] + fields_when(SERVER_FIELDS, _other) + [
    Field("proxy", "Reverse proxy на сервере бота:", "choice", choices=PROXY_CHOICES, when=_other),
    Field("name", "Название бота (для себя):"),
    Field("token", "Токен бота от @BotFather (сообщение удалю):", secret=True),
    Field("admins", "Telegram ID админов через запятую:", "ids"),
    Field("hooks", "Домен для вебхуков/API бота, например <code>hooks.example.com</code>:", "domain"),
])
async def inst_bb_done(event, values, ctx):
    panel = await db.get(db.Panel, values["panel"])
    try:
        server = (await db.get(db.Server, panel.server_id)) if values["where"] == "same" \
            else await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}", kb([("🏠 Меню", "menu")]))
    if not server:
        return await say(event, "❌ Нет сервера для установки.")
    try:
        rw_token = await ensure_api_token(panel)
    except RwError as e:
        return await say(event, f"❌ Нужен API-токен панели, а выпустить его не вышло: <code>{h(e)}</code>",
                         kb([("🔑 Доступ к API", f"pn:api:{panel.id}")]))
    local = panel.server_id == server.id
    web_token = secrets.token_hex(32)
    script = scripts.bedolaga_install(
        bot_token=values["token"], admin_ids=values["admins"], hooks_domain=values["hooks"],
        rw_url="http://remnawave:3000" if local else panel.api_url, rw_key=rw_token, local=local,
        web_api_token=web_token, webhook_secret=secrets.token_hex(32), pg_pass=secrets.token_hex(16),
        rw_cookie=proxy.panel_cookie(panel) if not local else None)
    b = await db.save(db.Bedolaga(name=values["name"], server_id=server.id, panel_id=panel.id,
                                  bot_token_enc=crypto.enc(values["token"]), admin_ids=values["admins"],
                                  hooks_domain=values["hooks"], web_api_token_enc=crypto.enc(web_token)))
    res = await run_task(event.bot, chat_id(event), server, f"Установка Bedolaga {b.name}", script)
    if res.marks.get("BEDOLAGA_COMPOSE"):
        b.compose_file = res.marks["BEDOLAGA_COMPOSE"]
        b = await db.save(b)
    kind = await _pick_proxy(event, server, values.get("proxy"))
    pres = await run_task(event.bot, chat_id(event), server, f"{kind.capitalize()}: {b.hooks_domain}",
                          proxy.apply_script(kind, "bedolaga", proxy.bedolaga_sites(b.hooks_domain)))
    await say(event, f"{'✅' if res.ok and pres.ok else '⚠️'} Bedolaga <b>{h(b.name)}</b>\n"
                     f"Вебхуки и API: https://{h(b.hooks_domain)}\n"
                     f"Сервер: {h(server.name)} ({'рядом с панелью, docker-compose.local' if local else 'отдельный'})\n"
                     "Web API токен сохранён (📇 Доступы → Боты).",
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
    Field("bot", "Для какого бота Bedolaga?", "choice", choices=bedolaga_choices),
    Field("where", "Где ставить кабинет?", "choice",
          choices=[("🤖 Там же, где бот (рекомендую)", "same"), ("📦 На отдельный сервер", "other")]),
] + fields_when(SERVER_FIELDS, _other) + [
    Field("proxy", "Reverse proxy на сервере кабинета:", "choice", choices=PROXY_CHOICES, when=_other),
    Field("domain", "Домен кабинета, например <code>cabinet.example.com</code>:", "domain"),
])
async def inst_cab_done(event, values, ctx):
    b = await db.get(db.Bedolaga, values["bot"])
    bot_srv = await db.get(db.Server, b.server_id)
    if not bot_srv:
        return await say(event, "❌ У бота не привязан сервер.")
    same = values["where"] == "same"
    try:
        cab_srv = bot_srv if same else await ensure_server(event, values)
    except RuntimeError as e:
        return await say(event, f"❌ {h(e)}")
    if not same and not b.hooks_domain:
        return await say(event, "❌ Для кабинета на отдельном сервере нужен домен API бота (hooks).")
    domain = values["domain"]
    r1 = await run_task(event.bot, chat_id(event), bot_srv, "Кабинет: настройки бота",
                        scripts.cabinet_bot_env(compose_file=b.compose_file, cabinet_domain=domain,
                                                hooks_domain=b.hooks_domain))
    r2 = await run_task(event.bot, chat_id(event), cab_srv, "Кабинет: фронтенд", scripts.cabinet_front(local=same))
    kind = await _pick_proxy(event, cab_srv, values.get("proxy"))
    api = "remnawave_bot:8080" if same else f"https://{b.hooks_domain}"
    r3 = await run_task(event.bot, chat_id(event), cab_srv, f"{kind.capitalize()}: {domain}",
                        proxy.apply_script(kind, "cabinet", proxy.cabinet_sites(domain, api)))
    ok = r1.ok and r2.ok and r3.ok
    if r2.ok:
        b.cabinet, b.cabinet_domain = True, domain
        await db.save(b)
        await db.set_setting(f"cabinet_srv:{b.id}", str(cab_srv.id))
    await say(event, f"{'✅' if ok else '⚠️'} Кабинет: https://{h(domain)} (сервер {h(cab_srv.name)})\n"
                     "Должен открыться вход через Telegram. Укажи домен в @BotFather → Bot Settings → Domain.",
              kb([("🏠 Меню", "menu")]))


@router.callback_query(F.data == "inst:cab")
async def inst_cab(c: CallbackQuery, state: FSMContext):
    if not await db.all_(db.Bedolaga):
        return await show(c, "Сначала установи Bedolaga Bot.", kb([("⬅️ Назад", "inst")]))
    await start_form(c, state, "inst_cab")
