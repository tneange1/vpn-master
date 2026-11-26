"""Раздел 3: «Доступы» — серверы, SSH-ключи, пароли, токены панелей и ботов."""
import asyncio

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from .. import crypto, db, scripts, ssh, terminal
from ..forms import Field, form, start_form
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show

router = Router(name="servers")


async def mark_deploy(key_id: int, server_id: int, on: bool) -> None:
    from sqlalchemy import delete as sa_delete
    async with db.Session() as ses:
        if on:
            await ses.merge(db.KeyDeploy(key_id=key_id, server_id=server_id))
        else:
            await ses.execute(sa_delete(db.KeyDeploy).where(db.KeyDeploy.key_id == key_id,
                                                           db.KeyDeploy.server_id == server_id))
        await ses.commit()


async def server_label(s: db.Server) -> str:
    """«🛰 NL-1 · нода MikaelaVPN» — сервер с его ролями."""
    panels = await db.all_(db.Panel, db.Panel.server_id == s.id)
    nodes = await db.all_(db.Node, db.Node.server_id == s.id)
    bots = await db.all_(db.Bedolaga, db.Bedolaga.server_id == s.id)
    roles = [f"панель {p.name}" for p in panels] + [f"нода {n.name}" for n in nodes] + [f"бот {b.name}" for b in bots]
    icon = "🖥" if panels else "🛰" if nodes else "🤖" if bots else "💻"
    return f"{icon} {s.name}" + (f" · {', '.join(roles)}" if roles else "")

AUTH_LABEL = {"password": "🔒 пароль", "key": "🔑 ключ"}


# ---------------------------------------------------------------- общие поля «какой сервер»
async def server_choices(values, ctx, allow_none=False):
    rows = [(f"{await server_label(s)} · {s.host}", s.id) for s in await db.all_(db.Server)]
    rows.append(("➕ Новый сервер", "new"))
    if allow_none or ctx.get("allow_none"):
        rows.append(("— без SSH (только API)", "none"))
    return rows


async def key_choices(values, ctx):
    return [(f"🔑 {k.name}", k.id) for k in await db.all_(db.SshKey) if k.private_enc]


def _new(v):
    return v.get("server") == "new"


AUTH_CHOICES = [
    ("🔑 Сгенерировать ключ и поставить (пароль нужен 1 раз)", "genkey"),
    ("🔒 Вход по паролю", "pass"),
    ("🗝 Ключ из хранилища бота", "key"),
    ("📋 Вставить свой приватный ключ", "paste"),
]

SERVER_FIELDS = [
    Field("server", "🖥 <b>Сервер</b>: выбери из списка или добавь новый.", "choice", choices=server_choices),
    Field("s_name", "Название сервера (для себя), например <code>nl-1</code>:", when=_new),
    Field("s_host", "IP или домен сервера:", when=_new),
    Field("s_port", "SSH-порт:", "int", optional=True, default=22, when=_new),
    Field("s_user", "Пользователь SSH:", optional=True, default="root", when=_new),
    Field("s_auth", "Как входить по SSH?", "choice", choices=AUTH_CHOICES, when=_new),
    Field("s_pass", "Пароль от сервера (сообщение сразу удалю):", secret=True,
          when=lambda v: _new(v) and v.get("s_auth") in ("pass", "genkey")),
    Field("s_keep", "Хранить пароль в боте после установки ключа?", "choice",
          choices=[("Нет, только ключ", False), ("Да, сохранить", True)],
          when=lambda v: _new(v) and v.get("s_auth") == "genkey"),
    Field("s_key", "Какой ключ? (он уже должен быть в authorized_keys сервера)", "choice", choices=key_choices,
          when=lambda v: _new(v) and v.get("s_auth") == "key"),
    Field("s_paste", "Пришли приватный ключ <b>файлом</b> (например <code>id_ed25519</code>) или текстом. Сообщение удалю.", "raw", secret=True,
          when=lambda v: _new(v) and v.get("s_auth") == "paste"),
]


async def ensure_server(event, values) -> db.Server | None:
    """Возвращает сервер из values (существующий или создаёт новый). None — если «без SSH».
    Бросает RuntimeError, если подключиться не удалось."""
    sv = values.get("server")
    if sv in (None, "none"):
        return None
    if sv != "new":
        s = await db.get(db.Server, sv)
        if not s:
            raise RuntimeError("сервер не найден")
        return s

    auth = values["s_auth"]
    s = db.Server(name=values["s_name"], host=values["s_host"], port=values.get("s_port") or 22,
                  username=values.get("s_user") or "root", auth="password")
    if auth == "pass":
        s.password_enc = crypto.enc(values["s_pass"])
    elif auth == "key":
        s.key_id, s.auth = values["s_key"], "key"
    elif auth == "paste":
        pem = values["s_paste"].strip() + "\n"
        try:
            pub = ssh.public_from_private(pem)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"не смог прочитать приватный ключ: {e}") from e
        k = await db.save(db.SshKey(name=f"{s.name}-imported", private_enc=crypto.enc(pem), public=pub))
        s.key_id, s.auth = k.id, "key"
    elif auth == "genkey":
        s.password_enc = crypto.enc(values["s_pass"])

    s = await db.save(s)
    ok, info = await ssh.check(s)
    if not ok:
        await db.delete(s)
        raise RuntimeError(f"не удалось подключиться к {s.host}: {info}")
    if s.key_id:
        await mark_deploy(s.key_id, s.id, True)

    if auth == "genkey":
        priv, pub = ssh.generate_keypair(f"vpn-master@{s.name}")
        k = await db.save(db.SshKey(name=f"{s.name}-key", private_enc=crypto.enc(priv), public=pub))
        code, out = await ssh.run(s, scripts.ssh_add_key(s.username, pub), timeout=60)
        if code != 0:
            raise RuntimeError(f"не удалось поставить ключ: {out[-500:]}")
        s.key_id, s.auth = k.id, "key"
        s = await db.save(s)
        await mark_deploy(k.id, s.id, True)
        ok_key, info_key = await ssh.check(s, use_password=False)
        if not ok_key:
            await say(event, f"⚠️ Ключ поставлен, но вход по ключу не прошёл: <code>{h(info_key)}</code>\n"
                             "Оставляю вход по паролю.")
            s.auth = "password"
        elif not values.get("s_keep"):
            s.password_enc = None
        s = await db.save(s)
    await say(event, f"✅ Сервер <b>{h(s.name)}</b> добавлен ({AUTH_LABEL[s.auth]}).\n<code>{h(info)}</code>")
    return s


# ---------------------------------------------------------------- меню «Доступы»
@router.callback_query(F.data == "acc")
async def access_menu(c: CallbackQuery):
    await show(c, "📇 <b>Доступы</b>\n\nЛогины, пароли и токены панелей и ботов.",
               kb([("📇 Панели: логины и токены", "acc:panels")],
                  [("🤖 Боты: токены", "acc:bots")],
                  [("⬅️ Назад", "menu")]))


@router.callback_query(F.data == "srv:list")
async def srv_list(c: CallbackQuery):
    servers = await db.all_(db.Server)
    rows = [[(f"{await server_label(s)} · {AUTH_LABEL.get(s.auth, s.auth)}", f"srv:v:{s.id}")] for s in servers]
    rows.append([("➕ Добавить сервер", "srv:add")])
    rows.append([("⬅️ Меню", "menu")])
    await show(c, f"🖥 <b>Серверы</b> ({len(servers)})\nВ карточке: состояние, терминал, команды, пароль, "
                  "способ входа, безопасность (UFW, IPv6, ping, BBR, SSH-порт), обслуживание.", kb(*rows))


@form("srv_add", "➕ Новый сервер", SERVER_FIELDS)
async def srv_add_done(event, values, ctx):
    try:
        s = await ensure_server(event, values)
    except RuntimeError as e:
        await say(event, f"❌ {h(e)}", kb([("🔁 Ещё раз", "srv:add"), ("🏠 Меню", "menu")]))
        return
    await say(event, "Готово.", kb([("Открыть сервер", f"srv:v:{s.id}"), ("🏠 Меню", "menu")]))


@router.callback_query(F.data == "srv:add")
async def srv_add(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "srv_add", preset={"server": "new"})


async def _server_text(s: db.Server) -> str:
    key = await db.get(db.SshKey, s.key_id) if s.key_id else None
    panels = await db.all_(db.Panel, db.Panel.server_id == s.id)
    nodes = await db.all_(db.Node, db.Node.server_id == s.id)
    bots = await db.all_(db.Bedolaga, db.Bedolaga.server_id == s.id)
    roles = [f"панель {p.name}" for p in panels] + [f"нода {n.name}" for n in nodes] + [f"бот {b.name}" for b in bots]
    return (f"🖥 <b>{h(s.name)}</b>\n"
            f"Адрес: <code>{h(s.username)}@{h(s.host)}:{s.port}</code>\n"
            f"Вход: {AUTH_LABEL.get(s.auth, s.auth)}"
            f"{' · ключ ' + h(key.name) if key else ''}"
            f"{' · пароль сохранён' if s.password_enc else ''}\n"
            f"Host key: {'запомнен' if s.host_key else '—'}\n"
            f"Роли: {h(', '.join(roles)) or '—'}")


@router.callback_query(F.data.startswith("srv:v:"))
async def srv_view(c: CallbackQuery):
    s = await db.get(db.Server, c.data.split(":")[2])
    if not s:
        return await c.answer("Нет такого", show_alert=True)
    i = s.id
    await show(c, await _server_text(s), kb(
        [("📊 Состояние", f"srv:state:{i}"), ("🔌 Проверить", f"srv:chk:{i}")],
        [("💻 Терминал", f"srv:term:{i}"), ("⌨️ Команда", f"srv:cmd:{i}")],
        [("👁 Показать пароль", f"srv:pw:{i}"), ("✏️ Сменить пароль", f"srv:setpw:{i}")],
        [("🔁 Способ входа", f"srv:auth:{i}"), ("🛡 Безопасность", f"sec:s:{i}")],
        [("🩹 Фикс reverse proxy", f"srv:fix:{i}"), ("🤖 Bedolaga", f"srv:bd:{i}")],
        [("🧹 Обслуживание", f"srv:mnt:{i}"), ("🔄 Перезагрузить", f"srv:rb:{i}")],
        [("♻️ Сбросить host key", f"srv:hk:{i}"), ("🗑 Удалить из бота", f"srv:del:{i}")],
        [("⬅️ Серверы", "srv:list")]))


@router.callback_query(F.data.startswith("srv:state:"))
async def srv_state(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    await c.answer()
    await run_task(bot, c.message.chat.id, s, "Состояние сервера", scripts.SERVER_STATE)


@router.callback_query(F.data.startswith("srv:mnt:"))
async def srv_maint(c: CallbackQuery):
    i = c.data.split(":")[2]
    await show(c, "🧹 <b>Обслуживание</b>",
               kb([("⬆️ Обновить пакеты (apt upgrade)", f"srv:apt:{i}")],
                  [("🐳 Почистить Docker (старше 7 дней)", f"srv:prune:{i}")],
                  [("🩹 Фикс reverse proxy (в т.ч. 502)", f"srv:fix:{i}")],
                  [("⬅️ К серверу", f"srv:v:{i}")]))


@router.callback_query(F.data.startswith("srv:nets:"))
async def srv_nets(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    await c.answer()
    await run_task(bot, c.message.chat.id, s, "Сети прокси", scripts.FIX_PROXY_NETS)


@router.callback_query(F.data.startswith(("srv:apt:", "srv:prune:")))
async def srv_maint_run(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    apt = c.data.startswith("srv:apt:")
    await c.answer()
    await run_task(bot, c.message.chat.id, s, "apt upgrade" if apt else "docker prune",
                   scripts.APT_UPGRADE if apt else scripts.DOCKER_PRUNE)


@router.callback_query(F.data.startswith("srv:rb:"))
async def srv_reboot(c: CallbackQuery):
    i = c.data.split(":")[2]
    await show(c, "🔄 Перезагрузить сервер? Контейнеры с restart: always поднимутся сами.",
               kb([("🔄 Да, перезагрузить", f"srv:rbok:{i}"), ("✖️ Нет", f"srv:v:{i}")]))


@router.callback_query(F.data.startswith("srv:rbok:"))
async def srv_reboot_ok(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    await c.answer()
    await run_task(bot, c.message.chat.id, s, "Перезагрузка", scripts.SERVER_REBOOT)


@router.callback_query(F.data.startswith("srv:chk:"))
async def srv_check(c: CallbackQuery):
    s = await db.get(db.Server, c.data.split(":")[2])
    await c.answer("Проверяю…")
    ok, info = await ssh.check(s)
    await say(c, f"{'✅' if ok else '❌'} <b>{h(s.name)}</b>\n<pre>{h(info)}</pre>",
              kb([("⬅️ К серверу", f"srv:v:{s.id}")]))


@router.callback_query(F.data.startswith("srv:pw:"))
async def srv_show_pw(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    pw = crypto.dec(s.password_enc)
    if not pw:
        return await c.answer("Пароль не сохранён", show_alert=True)
    m = await say(c, f"🔒 <b>{h(s.name)}</b>: <tg-spoiler><code>{h(pw)}</code></tg-spoiler>\n<i>Удалю через 60 сек.</i>")
    asyncio.create_task(_delete_later(bot, m.chat.id, m.message_id, 60))


async def _delete_later(bot: Bot, chat: int, mid: int, sec: int):
    await asyncio.sleep(sec)
    try:
        await bot.delete_message(chat, mid)
    except Exception:  # noqa: BLE001
        pass


@form("srv_setpw", "✏️ Пароль сервера", [Field("pw", "Новый пароль (сохраню в боте, на сервере не меняю):", secret=True)])
async def srv_setpw_done(event, values, ctx):
    s = await db.get(db.Server, ctx["id"])
    s.password_enc = crypto.enc(values["pw"])
    await db.save(s)
    await say(event, "✅ Пароль сохранён.", kb([("⬅️ К серверу", f"srv:v:{s.id}")]))


@router.callback_query(F.data.startswith("srv:setpw:"))
async def srv_setpw(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "srv_setpw", ctx={"id": int(c.data.split(":")[2])})


@router.callback_query(F.data.startswith("srv:auth:"))
async def srv_auth(c: CallbackQuery):
    i = c.data.split(":")[2]
    await show(c, "🔁 <b>Способ входа бота на сервер</b>\n\n"
                  "Это то, как <i>бот</i> подключается. Включить/выключить вход по паролю на самом сервере — "
                  "в 🛡 Безопасность.",
               kb([("🔑 Сгенерировать новый ключ и поставить", f"srv:gk:{i}")],
                  [("🗝 Выбрать ключ из хранилища", f"srv:pk:{i}")],
                  [("🔒 Входить по паролю", f"srv:usepw:{i}")],
                  [("⬅️ Назад", f"srv:v:{i}")]))


async def install_key(event, bot: Bot, s: db.Server, key: db.SshKey) -> bool:
    res = await run_task(bot, chat_id(event), s, "Установка SSH-ключа", scripts.ssh_add_key(s.username, key.public))
    if not res.ok:
        return False
    await mark_deploy(key.id, s.id, True)
    old_key, old_auth = s.key_id, s.auth
    s.key_id, s.auth = key.id, "key"
    s = await db.save(s)
    ok, info = await ssh.check(s, use_password=False)
    if not ok:
        s.key_id, s.auth = old_key, old_auth
        await db.save(s)
        await say(event, f"❌ Вход по ключу не прошёл: <code>{h(info)}</code>. Настройки входа не менял.")
        return False
    await say(event, f"✅ Бот теперь входит на <b>{h(s.name)}</b> по ключу <b>{h(key.name)}</b>.",
              kb([("⬅️ К серверу", f"srv:v:{s.id}")]))
    return True


@router.callback_query(F.data.startswith("srv:gk:"))
async def srv_genkey(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    priv, pub = ssh.generate_keypair(f"vpn-master@{s.name}")
    k = await db.save(db.SshKey(name=f"{s.name}-key", private_enc=crypto.enc(priv), public=pub))
    await install_key(c, bot, s, k)


@router.callback_query(F.data.startswith("srv:pk:"))
async def srv_pickkey(c: CallbackQuery):
    i = c.data.split(":")[2]
    keys = await db.all_(db.SshKey)
    rows = [[(f"🔑 {k.name}", f"srv:uk:{i}:{k.id}")] for k in keys if k.private_enc] or [[("Ключей с приватной частью нет", "noop")]]
    rows.append([("⬅️ Назад", f"srv:auth:{i}")])
    await show(c, "Какой ключ поставить на сервер?", kb(*rows))


@router.callback_query(F.data.startswith("srv:uk:"))
async def srv_usekey(c: CallbackQuery, bot: Bot):
    _, _, sid, kid = c.data.split(":")
    await install_key(c, bot, await db.get(db.Server, sid), await db.get(db.SshKey, kid))


@router.callback_query(F.data.startswith("srv:usepw:"))
async def srv_usepw(c: CallbackQuery):
    s = await db.get(db.Server, c.data.split(":")[2])
    if not s.password_enc:
        return await c.answer("Сначала сохрани пароль (✏️ Сменить пароль)", show_alert=True)
    s.auth = "password"
    await db.save(s)
    await show(c, "✅ Бот будет входить по паролю.", kb([("⬅️ К серверу", f"srv:v:{s.id}")]))


@router.callback_query(F.data.startswith("srv:hk:"))
async def srv_reset_hk(c: CallbackQuery):
    s = await db.get(db.Server, c.data.split(":")[2])
    s.host_key = None
    await db.save(s)
    await c.answer("Отпечаток сброшен — запомню новый при следующем подключении", show_alert=True)


@router.callback_query(F.data.startswith("srv:del:"))
async def srv_del(c: CallbackQuery):
    i = c.data.split(":")[2]
    await show(c, "🗑 Удалить сервер из бота? На самом сервере ничего не изменится.",
               kb([("🗑 Да, удалить", f"srv:delok:{i}"), ("✖️ Нет", f"srv:v:{i}")]))


@router.callback_query(F.data.startswith("srv:delok:"))
async def srv_del_ok(c: CallbackQuery):
    s = await db.get(db.Server, c.data.split(":")[2])
    if s:
        await db.delete(s)
    await srv_list(c)


@router.callback_query(F.data.startswith("srv:term:"))
async def srv_term(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, c.data.split(":")[2])
    await c.answer()
    try:
        await terminal.start(bot, c.message.chat.id, c.from_user.id, s, None, s.name)
    except Exception as e:  # noqa: BLE001
        await say(c, f"❌ {h(type(e).__name__)}: {h(e)}")


@form("srv_cmd", "⌨️ Команда", [Field("cmd", "Команда bash (выполнится от root, вывод пришлю):", "raw")])
async def srv_cmd_done(event, values, ctx):
    s = await db.get(db.Server, ctx["id"])
    await run_task(event.bot, chat_id(event), s, values["cmd"][:60], scripts.custom(values["cmd"]), timeout=600)
    await say(event, "—", kb([("⌨️ Ещё команда", f"srv:cmd:{s.id}"), ("⬅️ К серверу", f"srv:v:{s.id}")]))


@router.callback_query(F.data.startswith("srv:cmd:"))
async def srv_cmd(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "srv_cmd", ctx={"id": int(c.data.split(":")[2])})


# ---------------------------------------------------------------- логины/токены панелей и ботов
@router.callback_query(F.data == "acc:panels")
async def acc_panels(c: CallbackQuery):
    panels = await db.all_(db.Panel)
    rows = [[(f"🖥 {p.name}", f"acc:p:{p.id}")] for p in panels] or [[("Панелей нет", "noop")]]
    rows.append([("⬅️ Назад", "acc")])
    await show(c, "📇 <b>Доступы панелей</b>", kb(*rows))


@router.callback_query(F.data.startswith("acc:p:"))
async def acc_panel(c: CallbackQuery, bot: Bot):
    p = await db.get(db.Panel, c.data.split(":")[2])
    cookie = crypto.dec(p.cookie_enc) if p.protected else None
    login_url = f"https://{p.domain}/{cookie}" if cookie else f"https://{p.domain}"
    text = (f"🖥 <b>{h(p.name)}</b>\n"
            f"Вход: <tg-spoiler>{h(login_url)}</tg-spoiler>\n"
            + (f"X-Api-Key: <tg-spoiler><code>{h(cookie)}</code></tg-spoiler>\n" if cookie else "") +
            f"Логин: <code>{h(p.admin_user or '—')}</code>\n"
            f"Пароль: <tg-spoiler><code>{h(crypto.dec(p.admin_pass_enc) or '—')}</code></tg-spoiler>\n"
            f"API: <code>{h(p.api_url)}</code>\n"
            f"API-токен: <tg-spoiler><code>{h(crypto.dec(p.api_token_enc) or '—')}</code></tg-spoiler>\n\n"
            f"<i>Сообщение удалю через 2 минуты.</i>")
    await c.answer()
    m = await c.message.answer(text, disable_web_page_preview=True,
                               reply_markup=kb([("🔑 Задать API-токен", f"pn:tok:{p.id}")]))
    asyncio.create_task(_delete_later(bot, m.chat.id, m.message_id, 120))


@router.callback_query(F.data == "acc:bots")
async def acc_bots(c: CallbackQuery, bot: Bot):
    bots = await db.all_(db.Bedolaga)
    mods = await db.all_(db.Mod, db.Mod.bot_token_enc.is_not(None))
    lines = ["🤖 <b>Токены ботов</b>\n"]
    for b in bots:
        lines.append(f"Bedolaga <b>{h(b.name)}</b>: <tg-spoiler><code>{h(crypto.dec(b.bot_token_enc))}</code></tg-spoiler>"
                     f"\n  Web API: <tg-spoiler><code>{h(crypto.dec(b.web_api_token_enc) or '—')}</code></tg-spoiler>")
    for m in mods:
        panel = await db.get(db.Panel, m.panel_id) if m.panel_id else None
        lines.append(f"{h(m.kind)} ({h(panel.name if panel else '—')}): "
                     f"<tg-spoiler><code>{h(crypto.dec(m.bot_token_enc))}</code></tg-spoiler>")
    if len(lines) == 1:
        lines.append("Пока пусто.")
    lines.append("\n<i>Удалю через 2 минуты.</i>")
    await c.answer()
    m = await c.message.answer("\n".join(lines))
    asyncio.create_task(_delete_later(bot, m.chat.id, m.message_id, 120))
