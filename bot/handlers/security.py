"""«Настройка безопасности»: UFW (правила, IPv6, ping, сброс), SSH (порт, пароль, root), защита панели."""
import re
import secrets

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import crypto, db, proxy, scripts, ssh
from ..forms import Field, form, start_form
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show
from .servers import server_label

router = Router(name="security")

UFW_RULE_RE = re.compile(r"^(\d{1,5}(:\d{1,5})?(/(tcp|udp))?|from [0-9a-fA-F.:/]+( to any port \d{1,5}(:\d{1,5})?( proto (tcp|udp))?)?)$")


@router.callback_query(F.data == "sec")
async def sec_menu(c: CallbackQuery):
    servers = await db.all_(db.Server)
    panels = await db.all_(db.Panel)
    rows = [[(await server_label(s), f"sec:s:{s.id}")] for s in servers]
    rows += [[(f"🔒 Защита панели {p.name}", f"sec:p:{p.id}")] for p in panels]
    rows.append([("⬅️ Назад", "mng")])
    await show(c, "🛡 <b>Безопасность</b>\n\nСервер: UFW, IPv6, ping, SSH-порт, вход по паролю, root.\n"
                  "Панель: секретный путь в Caddy/Nginx (docs.rw/install/panel-security).", kb(*rows))


async def _srv(c: CallbackQuery, idx: int = 2) -> db.Server | None:
    s = await db.get(db.Server, c.data.split(":")[idx])
    if not s:
        await c.answer("Сервер не привязан", show_alert=True)
    return s


@router.callback_query(F.data.startswith("sec:s:"))
async def sec_server(c: CallbackQuery):
    if not (s := await _srv(c)):
        return
    i = s.id
    await show(c, f"🛡 <b>{h(s.name)}</b> · <code>{h(s.host)}:{s.port}</code>\n"
                  f"Бот входит: {'🔑 по ключу' if s.auth == 'key' else '🔒 по паролю'}",
               kb([("🧱 Firewall (UFW)", f"ufw:m:{i}"), ("🔐 SSH", f"ssh:m:{i}")],
                  [("🚀 BBR: включить", f"bbr:{i}:1"), ("выключить", f"bbr:{i}:0"), ("статус", f"bbr:{i}:s")],
                  [("📊 Статус всего", f"sec:st:{i}")],
                  [("⬅️ Безопасность", "sec"), ("🖥 Сервер", f"srv:v:{i}")]))


@router.callback_query(F.data.startswith("bbr:"))
async def bbr_toggle(c: CallbackQuery, bot: Bot):
    _, i, mode = c.data.split(":")
    s = await db.get(db.Server, i)
    script = scripts.BBR_STATUS if mode == "s" else scripts.bbr(mode == "1")
    await c.answer()
    await run_task(bot, c.message.chat.id, s, "BBR " + {"1": "вкл", "0": "выкл", "s": "статус"}[mode], script)


@router.callback_query(F.data.startswith("sec:st:"))
async def sec_status(c: CallbackQuery, bot: Bot):
    if s := await _srv(c):
        await c.answer()
        await run_task(bot, c.message.chat.id, s, "Статус безопасности",
                       scripts.UFW_STATUS + scripts.SSH_STATUS.split("\n", 1)[1] + scripts.BBR_STATUS.split("\n", 1)[1])


# ------------------------------------------------------------------ UFW
@router.callback_query(F.data.startswith("ufw:m:"))
async def ufw_menu(c: CallbackQuery):
    if not (s := await _srv(c)):
        return
    i = s.id
    await show(c, f"🧱 <b>UFW · {h(s.name)}</b>\n\n"
                  "«Включить» ставит deny incoming / allow outgoing, открывает SSH-порт и порты по ролям сервера "
                  "(панель/бот/нода: 80, 443 tcp+udp; нода: 2222 только с IP панели).",
               kb([("✅ Включить (по ролям)", f"ufw:on:{i}"), ("⛔️ Выключить", f"ufw:off:{i}")],
                  [("➕ Открыть порт", f"ufw:allow:{i}"), ("➖ Удалить правило", f"ufw:rules:{i}")],
                  [("🌐 IPv6 в UFW: ВЫКЛ", f"ufw:v6:{i}:0"), ("ВКЛ", f"ufw:v6:{i}:1")],
                  [("🚫 IPv6 в системе: ВЫКЛ", f"sys:v6:{i}:0"), ("ВКЛ", f"sys:v6:{i}:1")],
                  [("🏓 Ping: блокировать", f"ufw:ping:{i}:0"), ("разрешить", f"ufw:ping:{i}:1")],
                  [("📊 Статус", f"ufw:st:{i}"), ("♻️ Сбросить правила", f"ufw:reset:{i}")],
                  [("⬅️ Назад", f"sec:s:{i}")]))


async def _role_rules(s: db.Server) -> list[str]:
    panels = await db.all_(db.Panel, db.Panel.server_id == s.id)
    nodes = await db.all_(db.Node, db.Node.server_id == s.id)
    bots = await db.all_(db.Bedolaga, db.Bedolaga.server_id == s.id)
    rules = []
    if panels or bots or nodes:
        rules += ["80/tcp", "443/tcp", "443/udp"]
    for n in nodes:
        p = await db.get(db.Panel, n.panel_id)
        psrv = await db.get(db.Server, p.server_id) if p else None
        if psrv:
            rules.append(f"from {psrv.host} to any port 2222 proto tcp")
    return list(dict.fromkeys(rules))


@router.callback_query(F.data.startswith("ufw:on:"))
async def ufw_on(c: CallbackQuery, bot: Bot):
    if s := await _srv(c):
        await c.answer()
        await run_task(bot, c.message.chat.id, s, "UFW: включение", scripts.ufw_enable(s.port, await _role_rules(s)))


@router.callback_query(F.data.startswith(("ufw:off:", "ufw:st:")))
async def ufw_simple(c: CallbackQuery, bot: Bot):
    if s := await _srv(c):
        off = c.data.startswith("ufw:off:")
        await c.answer()
        await run_task(bot, c.message.chat.id, s, "UFW выкл" if off else "UFW статус",
                       scripts.UFW_DISABLE if off else scripts.UFW_STATUS)


@router.callback_query(F.data.startswith("ufw:reset:"))
async def ufw_reset(c: CallbackQuery):
    i = c.data.split(":")[2]
    await show(c, "♻️ Сбросить все правила UFW? Он выключится, потом включи заново «по ролям».",
               kb([("♻️ Да, сбросить", f"ufw:resetok:{i}"), ("✖️ Нет", f"ufw:m:{i}")]))


@router.callback_query(F.data.startswith("ufw:resetok:"))
async def ufw_reset_ok(c: CallbackQuery, bot: Bot):
    if s := await _srv(c):
        await c.answer()
        await run_task(bot, c.message.chat.id, s, "UFW: сброс", scripts.UFW_RESET)


@router.callback_query(F.data.startswith(("ufw:v6:", "sys:v6:", "ufw:ping:")))
async def ufw_toggles(c: CallbackQuery, bot: Bot):
    a, b, i, on = c.data.split(":")
    s = await db.get(db.Server, i)
    on = on == "1"
    if (a, b) == ("ufw", "v6"):
        script, title = scripts.ufw_ipv6(on), f"IPv6 в UFW {'ВКЛ' if on else 'ВЫКЛ'}"
    elif a == "sys":
        script, title = scripts.sys_ipv6(on), f"IPv6 в системе {'ВКЛ' if on else 'ВЫКЛ'}"
    else:
        script, title = scripts.ufw_ping(on), f"Ping {'разрешён' if on else 'блок'}"
    await c.answer()
    await run_task(bot, c.message.chat.id, s, title, script)
    if a == "sys" and not on:
        await say(c, "ℹ️ WARP-native проверяет IPv6 при установке — если ставишь WARP, IPv6 в системе не выключай.")


@form("ufw_allow", "➕ Открыть порт в UFW", [
    Field("rule", "Правило, например <code>8080/tcp</code>, <code>2053</code>, <code>10000:10100/udp</code> или "
                  "<code>from 1.2.3.4 to any port 2222 proto tcp</code>:")])
async def ufw_allow_done(event, values, ctx):
    s = await db.get(db.Server, ctx["id"])
    rule = values["rule"].strip()
    if not UFW_RULE_RE.match(rule):
        return await say(event, "❌ Не понял правило.", kb([("🔁 Ещё раз", f"ufw:allow:{s.id}")]))
    await run_task(event.bot, chat_id(event), s, f"UFW allow {rule}", scripts.ufw_allow(rule))
    await say(event, "—", kb([("⬅️ UFW", f"ufw:m:{s.id}")]))


@router.callback_query(F.data.startswith("ufw:allow:"))
async def ufw_allow(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "ufw_allow", ctx={"id": int(c.data.split(":")[2])})


@router.callback_query(F.data.startswith("ufw:rules:"))
async def ufw_rules(c: CallbackQuery):
    if not (s := await _srv(c)):
        return
    await c.answer("Читаю правила…")
    try:
        _code, out = await ssh.run(s, scripts.UFW_RULES, timeout=60)
    except Exception as e:  # noqa: BLE001
        return await say(c, f"❌ {h(e)}")
    rules = re.findall(r"^##VM R(\d+)=(.*)$", out, re.M)
    rows = [[(f"🗑 [{n}] {r.strip()}"[:60], f"ufw:del:{s.id}:{n}")] for n, r in rules] or [[("Правил нет", "noop")]]
    rows.append([("⬅️ UFW", f"ufw:m:{s.id}")])
    warn = f"\n⚠️ Правило SSH-порта {s.port} не удаляй — потеряешь доступ." if rules else ""
    await show(c, f"➖ <b>Удалить правило UFW</b> · {h(s.name)}{warn}", kb(*rows))


@router.callback_query(F.data.startswith("ufw:del:"))
async def ufw_del(c: CallbackQuery, bot: Bot):
    _, _, i, n = c.data.split(":")
    s = await db.get(db.Server, i)
    await c.answer()
    await run_task(bot, c.message.chat.id, s, f"UFW delete [{n}]", scripts.ufw_delete(int(n)))
    await say(c, "Номера правил сдвинулись — открой список заново.", kb([("➖ Ещё удалить", f"ufw:rules:{s.id}")]))


# ------------------------------------------------------------------ SSH
@router.callback_query(F.data.startswith("ssh:m:"))
async def ssh_menu(c: CallbackQuery):
    if not (s := await _srv(c)):
        return
    i = s.id
    await show(c, f"🔐 <b>SSH · {h(s.name)}</b>\nПорт: <code>{s.port}</code> · бот входит "
                  f"{'по ключу 🔑' if s.auth == 'key' else 'по паролю 🔒'}\n\n"
                  "Вход по паролю отключается только если бот уже входит по ключу.\n"
                  "Смена порта безопасная: sshd сначала слушает старый+новый, бот проверяет вход на новом, "
                  "и только потом старый закрывается.",
               kb([("🔢 Сменить SSH-порт", f"ssh:port:{i}"), ("📊 Статус SSH", f"ssh:st:{i}")],
                  [("🔒 Пароль: ВЫКЛ", f"ssh:pw:{i}:0"), ("🔓 Пароль: ВКЛ", f"ssh:pw:{i}:1")],
                  [("👑 root: только по ключу", f"ssh:root:{i}:prohibit-password")],
                  [("👑 root: можно", f"ssh:root:{i}:yes"), ("👑 root: нельзя", f"ssh:root:{i}:no")],
                  [("🔑 Вход бота / ключи", f"srv:auth:{i}")],
                  [("⬅️ Назад", f"sec:s:{i}")]))


@router.callback_query(F.data.startswith("ssh:st:"))
async def ssh_status(c: CallbackQuery, bot: Bot):
    if s := await _srv(c):
        await c.answer()
        await run_task(bot, c.message.chat.id, s, "SSH статус", scripts.SSH_STATUS)


@router.callback_query(F.data.startswith("ssh:pw:"))
async def ssh_password(c: CallbackQuery, bot: Bot):
    _, _, i, on = c.data.split(":")
    s = await db.get(db.Server, i)
    enable = on == "1"
    if not enable:
        if s.auth != "key":
            return await c.answer("Сначала переведи бота на вход по ключу (🔑 Вход бота / ключи)", show_alert=True)
        await c.answer("Проверяю вход по ключу…")
        ok, info = await ssh.check(s, use_password=False)
        if not ok:
            return await say(c, f"❌ Вход по ключу не работает — пароль не отключаю.\n<code>{h(info)}</code>")
    else:
        await c.answer()
    await run_task(bot, c.message.chat.id, s, f"SSH: вход по паролю {'ВКЛ' if enable else 'ВЫКЛ'}",
                   scripts.ssh_password_auth(enable))


@router.callback_query(F.data.startswith("ssh:root:"))
async def ssh_root(c: CallbackQuery, bot: Bot):
    _, _, i, mode = c.data.split(":")
    s = await db.get(db.Server, i)
    if s.username == "root" and mode == "no":
        return await c.answer("Бот входит как root — так ты отрежешь ему доступ.", show_alert=True)
    if s.username == "root" and mode == "prohibit-password" and s.auth != "key":
        return await c.answer("Бот входит под root по паролю — сначала переведи его на ключ.", show_alert=True)
    await c.answer()
    await run_task(bot, c.message.chat.id, s, f"SSH: PermitRootLogin {mode}", scripts.ssh_root_login(mode))


@form("ssh_port", "🔢 Новый SSH-порт", [
    Field("port", "Новый порт (1024–65535, не занятый: не 443, 2222, 8443…):", "int")])
async def ssh_port_done(event, values, ctx):
    s = await db.get(db.Server, ctx["id"])
    new, old = int(values["port"]), s.port
    if not (1024 <= new <= 65535) or new in (2222, 3000, 3010, 8080, 8443, old):
        return await say(event, "❌ Выбери другой порт.", kb([("🔁 Ещё раз", f"ssh:port:{s.id}")]))
    # шаг 1: sshd слушает оба порта
    r1 = await run_task(event.bot, chat_id(event), s, f"SSH: порт {old} + {new}", scripts.ssh_ports([old, new]))
    if not r1.ok:
        return await say(event, "❌ Не получилось, порт не меняю.")
    # шаг 2: бот входит на новом порту
    s.port = new
    ok, info = await ssh.check(s)
    if not ok:
        s.port = old
        await run_task(event.bot, chat_id(event), s, "SSH: откат порта", scripts.ssh_ports([old]))
        return await say(event, f"❌ На порт {new} войти не удалось (<code>{h(info)}</code>). Вернул {old}.\n"
                                "Проверь фаервол у хостера (security group).")
    s = await db.save(s)
    # шаг 3: оставляем только новый
    r3 = await run_task(event.bot, chat_id(event), s, f"SSH: только порт {new}",
                        scripts.ssh_ports([new], close_port=old))
    await say(event, f"{'✅' if r3.ok else '⚠️'} SSH теперь на порту <b>{new}</b>, в боте порт обновлён.\n"
                     f"Себе в ~/.ssh/config: <code>Port {new}</code>", kb([("⬅️ SSH", f"ssh:m:{s.id}")]))


@router.callback_query(F.data.startswith("ssh:port:"))
async def ssh_port(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "ssh_port", ctx={"id": int(c.data.split(":")[2])})


# ------------------------------------------------------------------ защита панели
@router.callback_query(F.data.startswith("sec:p:"))
async def sec_panel(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    i = p.id
    if not p.managed_proxy:
        return await show(c, "🔒 Reverse proxy этой панели ставил не бот — его я не трогаю.\n"
                             "Чтобы бот взял прокси под себя: Установка → 🔗 Подключить панель → «настроить прокси» "
                             "(старый прокси будет забэкаплен, при ошибке — откат).", kb([("⬅️ Назад", "sec")]))
    status = "🔒 включена" if p.protected else "выключена"
    text = (f"🔒 <b>Защита панели {h(p.name)}</b>: {status}\n\n"
            f"{proxy.KINDS.get(proxy.kind_of(await db.get(db.Server, p.server_id)), '')} пускает в панель только с cookie. Cookie выдаётся по секретной ссылке "
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
                         proxy.apply_script(proxy.kind_of(server), "panel", proxy.panel_sites(p, cookie)))
    if res.ok:
        p.protected, p.cookie_enc = on, crypto.enc(cookie) if cookie else None
        await db.save(p)
        msg = (f"✅ Включено. Вход: <tg-spoiler>https://{h(p.domain)}/?vmkey={h(cookie)}</tg-spoiler>\n"
               "Bedolaga на другом сервере: передай ему ключ (переустанови бота или пропиши REMNAWAVE_SECRET_KEY).") \
            if on else "✅ Защита выключена."
        await say(c, msg, kb([("⬅️ Назад", f"sec:p:{p.id}")]))
