"""«Настройка безопасности»: UFW, вход по SSH (пароль вкл/выкл), SSH-ключи, защита панели в reverse proxy."""
import re
import secrets

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import crypto, db, scripts, ssh
from ..forms import Field, form, start_form
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show

router = Router(name="security")

UFW_RULE_RE = re.compile(r"^(\d{1,5}(:\d{1,5})?(/(tcp|udp))?|from [0-9a-fA-F.:/]+( to any port \d{1,5}( proto (tcp|udp))?)?)$")


@router.callback_query(F.data == "sec")
async def sec_menu(c: CallbackQuery):
    servers = await db.all_(db.Server)
    panels = await db.all_(db.Panel)
    rows = [[(f"🖥 {s.name} · {s.host}", f"sec:s:{s.id}")] for s in servers]
    rows += [[(f"🔒 Защита панели {p.name}", f"sec:p:{p.id}")] for p in panels]
    rows.append([("⬅️ Назад", "mng")])
    await show(c, "🛡 <b>Безопасность</b>\n\nСервер: UFW, вход по паролю, SSH-ключи.\n"
                  "Панель: секретная ссылка в Caddy (docs.rw/install/panel-security).", kb(*rows))


async def _roles(s: db.Server):
    panels = await db.all_(db.Panel, db.Panel.server_id == s.id)
    nodes = await db.all_(db.Node, db.Node.server_id == s.id)
    bots = await db.all_(db.Bedolaga, db.Bedolaga.server_id == s.id)
    return panels, nodes, bots


@router.callback_query(F.data.startswith("sec:s:"))
async def sec_server(c: CallbackQuery):
    s = await db.get(db.Server, c.data.split(":")[2])
    if not s:
        return await c.answer("Сервер не привязан", show_alert=True)
    i = s.id
    await show(c, f"🛡 <b>{h(s.name)}</b> · <code>{h(s.host)}</code>\n"
                  f"Бот входит: {'🔑 по ключу' if s.auth == 'key' else '🔒 по паролю'}\n\n"
                  "Отключать вход по паролю можно только когда бот уже входит по ключу.",
               kb([("🧱 Включить UFW (правила по ролям)", f"sec:ufw:{i}")],
                  [("➕ Открыть порт", f"sec:allow:{i}"), ("📊 Статус UFW", f"sec:ufws:{i}")],
                  [("⛔️ Выключить UFW", f"sec:ufwoff:{i}")],
                  [("🔑 Поставить SSH-ключ", f"srv:auth:{i}"), ("📊 Статус SSH", f"sec:sshs:{i}")],
                  [("🔒 Вход по паролю: ВЫКЛ", f"sec:pw:{i}:0"), ("🔓 ВКЛ", f"sec:pw:{i}:1")],
                  [("⬅️ Безопасность", "sec"), ("🖥 Сервер", f"srv:v:{i}")]))


@router.callback_query(F.data.startswith("sec:ufw:"))
async def sec_ufw(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    panels, nodes, bots = await _roles(s)
    rules = []
    if panels or bots:
        rules += ["80/tcp", "443/tcp", "443/udp"]
    if nodes:
        rules += ["80/tcp", "443/tcp", "443/udp", "8443/udp"]
        for n in nodes:
            p = await db.get(db.Panel, n.panel_id)
            psrv = await db.get(db.Server, p.server_id) if p else None
            if psrv:
                rules.append(f"from {psrv.host} to any port 2222 proto tcp")
    rules = list(dict.fromkeys(rules))
    await c.answer()
    await run_task(bot, c.message.chat.id, s, "UFW: включение", scripts.ufw_enable(s.port, rules))


@router.callback_query(F.data.startswith(("sec:ufws:", "sec:ufwoff:", "sec:sshs:")))
async def sec_simple(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    kind = c.data.split(":")[1]
    script, title = {"ufws": (scripts.UFW_STATUS, "UFW статус"), "ufwoff": (scripts.UFW_DISABLE, "UFW выкл"),
                     "sshs": (scripts.SSH_STATUS, "SSH статус")}[kind]
    await c.answer()
    await run_task(bot, c.message.chat.id, s, title, script)


@form("sec_allow", "➕ Открыть порт в UFW", [
    Field("rule", "Правило, например <code>8080/tcp</code>, <code>2053</code> или "
                  "<code>from 1.2.3.4 to any port 2222 proto tcp</code>:")])
async def sec_allow_done(event, values, ctx):
    s = await db.get(db.Server, ctx["id"])
    rule = values["rule"].strip()
    if not UFW_RULE_RE.match(rule):
        return await say(event, "❌ Не понял правило.", kb([("🔁 Ещё раз", f"sec:allow:{s.id}")]))
    await run_task(event.bot, chat_id(event), s, f"UFW allow {rule}", scripts.ufw_allow(rule))


@router.callback_query(F.data.startswith("sec:allow:"))
async def sec_allow(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "sec_allow", ctx={"id": int(c.data.split(":")[2])})


@router.callback_query(F.data.startswith("sec:pw:"))
async def sec_password(c: CallbackQuery, bot: Bot):
    _, _, i, on = c.data.split(":")
    s = await db.get(db.Server, i)
    enable = on == "1"
    if not enable:
        if s.auth != "key":
            return await c.answer("Сначала переведи бота на вход по ключу (🔑 Поставить SSH-ключ)", show_alert=True)
        await c.answer("Проверяю вход по ключу…")
        ok, info = await ssh.check(s, use_password=False)
        if not ok:
            return await say(c, f"❌ Вход по ключу не работает — пароль не отключаю.\n<code>{h(info)}</code>")
    else:
        await c.answer()
    await run_task(bot, c.message.chat.id, s, f"SSH: вход по паролю {'ВКЛ' if enable else 'ВЫКЛ'}",
                   scripts.ssh_password_auth(enable))


# ------------------------------------------------------------------ защита панели
@router.callback_query(F.data.startswith("sec:p:"))
async def sec_panel(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    i = p.id
    if not p.managed_proxy:
        return await show(c, "🔒 Эта панель подключена, а не установлена ботом — её Caddy я не трогаю.\n"
                             "Гайды: https://docs.rw/install/panel-security/", kb([("⬅️ Назад", "sec")]))
    status = "🔒 включена" if p.protected else "выключена"
    text = (f"🔒 <b>Защита панели {h(p.name)}</b>: {status}\n\n"
            "Caddy пускает в панель только с cookie. Cookie выдаётся по секретной ссылке "
            "<code>https://панель/?vmkey=…</code>; без неё соединение сбрасывается. Страница подписки "
            "на своём домене остаётся открытой.")
    if p.protected:
        text += f"\n\nСсылка для входа: <tg-spoiler>https://{h(p.domain)}/?vmkey={h(crypto.dec(p.cookie_enc))}</tg-spoiler>"
    await show(c, text, kb([("✅ Включить / новый ключ", f"sec:pon:{i}"), ("⛔️ Выключить", f"sec:poff:{i}")],
                           [("⬅️ Назад", "sec")]))


@router.callback_query(F.data.startswith(("sec:pon:", "sec:poff:")))
async def sec_panel_toggle(c: CallbackQuery, bot: Bot):
    p = await db.get(db.Panel, c.data.split(":")[2])
    server = await db.get(db.Server, p.server_id)
    if not server:
        return await c.answer("У панели нет SSH-сервера", show_alert=True)
    on = c.data.startswith("sec:pon:")
    cookie = secrets.token_urlsafe(24).replace("-", "x").replace("_", "y") if on else None
    await c.answer()
    res = await run_task(bot, c.message.chat.id, server, f"Защита панели {'ВКЛ' if on else 'ВЫКЛ'}",
                         scripts.apply_panel_site(p.domain, p.sub_domain, cookie, p.link_extractor))
    if res.ok:
        p.protected, p.cookie_enc = on, crypto.enc(cookie) if cookie else None
        await db.save(p)
        msg = (f"✅ Включено. Вход: <tg-spoiler>https://{h(p.domain)}/?vmkey={h(cookie)}</tg-spoiler>\n"
               "Bedolaga на другом сервере: передай ему ключ (переустанови бота или пропиши REMNAWAVE_SECRET_KEY).") \
            if on else "✅ Защита выключена."
        await say(c, msg, kb([("⬅️ Назад", f"sec:p:{p.id}")]))
