"""🤖 Bedolaga Bot и Cabinet на серверах: подхват уже установленных, статус, логи, перезапуск, обновление,
настройки .env, доступ к панели через секретный путь (X-Api-Key), кабинет."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import crypto, db, discover, proxy, scripts
from ..forms import Field, form, start_form
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show
from .servers import server_label

router = Router(name="bedolaga")

ENV_KEYS = ["ADMIN_IDS", "BOT_TOKEN", "WEBHOOK_URL", "REMNAWAVE_API_URL", "REMNAWAVE_API_KEY",
            "SUPPORT_USERNAME", "TRIAL_DURATION_DAYS", "CABINET_ALLOWED_ORIGINS"]


def _id(data: str, i: int = 2) -> str:
    return data.split(":")[i]


async def _bot(c: CallbackQuery):
    b = await db.get(db.Bedolaga, _id(c.data))
    if not b:
        await c.answer("Бот не найден", show_alert=True)
        return None, None
    s = await db.get(db.Server, b.server_id)
    if not s:
        await c.answer("У бота не привязан сервер", show_alert=True)
    return b, s


# ------------------------------------------------------------------ список / подхват
@router.callback_query(F.data == "bd:list")
async def bd_list(c: CallbackQuery):
    rows = [[(f"🤖 {b.name} · {b.hooks_domain or '—'}", f"bd:v:{b.id}")] for b in await db.all_(db.Bedolaga)]
    rows.append([("🔍 Найти на сервере", "bd:find"), ("📦 Установить", "inst:bb")])
    rows.append([("⬅️ Управление", "mng")])
    await show(c, "🤖 <b>Bedolaga Bot и Cabinet</b>\n\n«Найти на сервере» подхватит уже установленного бота и кабинет "
                  "(папка, compose, домены, токены из .env).", kb(*rows))


@router.callback_query(F.data == "bd:find")
async def bd_find(c: CallbackQuery):
    rows = [[(await server_label(s), f"srv:find:{s.id}")] for s in await db.all_(db.Server)] or [[("Серверов нет", "noop")]]
    rows.append([("⬅️ Назад", "bd:list")])
    await show(c, "🔍 На каком сервере искать Bedolaga?", kb(*rows))


@router.callback_query(F.data.startswith("srv:find:"))
async def srv_find(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, _id(c.data))
    await c.answer("Смотрю сервер…")
    info = await discover.run_discover(bot, c.message.chat.id, s)
    if info is None:
        return await say(c, "❌ Не смог осмотреть сервер.")
    notes = await discover.adopt(s, info)
    bots = [b for b in await db.all_(db.Bedolaga) if b.server_id == s.id]
    rows = [[(f"🤖 {b.name}", f"bd:v:{b.id}")] for b in bots]
    rows.append([("🩹 Фикс reverse proxy", f"srv:fix:{s.id}"), ("🖥 Сервер", f"srv:v:{s.id}")])
    await say(c, "🔍 <b>Найдено</b>\n" + ("\n".join(notes) or "ничего знакомого"), kb(*rows))


@router.callback_query(F.data.startswith("srv:bd:"))
async def srv_bots(c: CallbackQuery):
    s = await db.get(db.Server, _id(c.data))
    bots = [b for b in await db.all_(db.Bedolaga) if b.server_id == s.id]
    cabs, bot_ids = [], {b.id for b in bots}
    for b in await db.all_(db.Bedolaga):
        if b.cabinet and str(await db.setting(f"cabinet_srv:{b.id}") or b.server_id) == str(s.id) and b.id not in bot_ids:
            cabs.append(b)
    rows = [[(f"🤖 {b.name}", f"bd:v:{b.id}")] for b in bots] + [[(f"🗂 Кабинет {b.cabinet_domain}", f"bd:cab:{b.id}")] for b in cabs]
    rows.append([("🔍 Найти на сервере", f"srv:find:{s.id}"), ("📦 Установить бота", "inst:bb")])
    rows.append([("⬅️ К серверу", f"srv:v:{s.id}")])
    await show(c, f"🤖 <b>Bedolaga на {h(s.name)}</b>" + ("" if bots or cabs else "\nПока не найден."), kb(*rows))


@router.callback_query(F.data.startswith("srv:fix:"))
async def srv_fix(c: CallbackQuery, bot: Bot):
    s = await db.get(db.Server, _id(c.data))
    await c.answer("Чиню прокси…")
    await discover.fix_proxy(bot, c.message.chat.id, s)
    await say(c, "—", kb([("🖥 К серверу", f"srv:v:{s.id}")]))


# ------------------------------------------------------------------ карточка бота
@router.callback_query(F.data.startswith("bd:v:"))
async def bd_view(c: CallbackQuery):
    b = await db.get(db.Bedolaga, _id(c.data))
    if not b:
        return await c.answer("Бот не найден", show_alert=True)
    s = await db.get(db.Server, b.server_id)
    p = await db.get(db.Panel, b.panel_id) if b.panel_id else None
    i = b.id
    text = (f"🤖 <b>{h(b.name)}</b>\n"
            f"Сервер: {h(s.name + ' · ' + s.host) if s else '—'}\n"
            f"Папка: <code>{h(b.install_dir)}</code> · <code>{h(b.compose_file)}</code>\n"
            f"Вебхуки/API: {('https://' + h(b.hooks_domain)) if b.hooks_domain else '—'}\n"
            f"Панель: {h(p.domain) if p else '—'}"
            + (" · 🔒 через X-Api-Key" if p and p.protected else "") + "\n"
            f"Кабинет: {('https://' + h(b.cabinet_domain)) if b.cabinet else '—'}\n"
            f"Админы: <code>{h(b.admin_ids or '—')}</code>")
    rows = [[("📊 Статус", f"bd:st:{i}"), ("📜 Логи", f"bd:lg:{i}")],
            [("🔄 Перезапуск", f"bd:rs:{i}"), ("⬆️ Обновить", f"upd:run:bedolaga:{i}")],
            [("⚙️ Настройки (.env)", f"bd:env:{i}"), ("🔑 Доступ к панели", f"bd:acc:{i}")],
            [("🗂 Кабинет", f"bd:cab:{i}")] if b.cabinet else [("➕ Поставить кабинет", "inst:cab")],
            [("🩹 Фикс reverse proxy", f"srv:fix:{b.server_id}"), ("🔗 Панель", f"bd:pn:{i}")],
            [("🗑 Убрать из бота", f"bd:del:{i}"), ("⬅️ Боты", "bd:list")]]
    await show(c, text, kb(*rows))


@router.callback_query(F.data.startswith(("bd:st:", "bd:lg:", "bd:rs:")))
async def bd_action(c: CallbackQuery, bot: Bot):
    b, s = await _bot(c)
    if not s:
        return
    kind = c.data.split(":")[1]
    await c.answer()
    script = scripts.BEDOLAGA_STATUS if kind == "st" else \
        scripts.bedolaga_cmd(b.install_dir, b.compose_file, "logs" if kind == "lg" else "restart")
    await run_task(bot, c.message.chat.id, s, {"st": "Статус", "lg": "Логи", "rs": "Перезапуск"}[kind] + f" · {b.name}", script)


@router.callback_query(F.data.startswith("bd:acc:"))
async def bd_access(c: CallbackQuery, bot: Bot):
    b, s = await _bot(c)
    if not s:
        return
    p = await db.get(db.Panel, b.panel_id) if b.panel_id else None
    if not p:
        return await c.answer("Сначала выбери панель (🔗 Панель)", show_alert=True)
    await c.answer()
    await proxy.sync_bedolaga_token(bot, c.message.chat.id, p)
    await say(c, "✅ Bedolaga получил доступ: " + ("REMNAWAVE_AUTH_TYPE=caddy, REMNAWAVE_CADDY_TOKEN = ключ "
                                                  "секретного пути (уходит в X-Api-Key)." if p.protected else
                                                  "секретного пути нет — AUTH_TYPE=api_key."),
              kb([("⬅️ К боту", f"bd:v:{b.id}")]))


@router.callback_query(F.data.startswith("bd:pn:"))
async def bd_panel_pick(c: CallbackQuery):
    i = _id(c.data)
    rows = [[(f"🖥 {p.name} · {p.domain}", f"bd:pns:{i}:{p.id}")] for p in await db.all_(db.Panel)] or [[("Панелей нет", "noop")]]
    rows.append([("⬅️ К боту", f"bd:v:{i}")])
    await show(c, "К какой панели относится бот?", kb(*rows))


@router.callback_query(F.data.startswith("bd:pns:"))
async def bd_panel_set(c: CallbackQuery):
    _, _, i, pid = c.data.split(":")
    b = await db.get(db.Bedolaga, i)
    b.panel_id = int(pid)
    await db.save(b)
    await c.answer("Сохранено")
    await bd_view(c)


@router.callback_query(F.data.startswith("bd:del:"))
async def bd_del(c: CallbackQuery):
    i = _id(c.data)
    await show(c, "🗑 Убрать бота из vpn-master? На сервере ничего не изменится.",
               kb([("🗑 Да", f"bd:delok:{i}"), ("✖️ Нет", f"bd:v:{i}")]))


@router.callback_query(F.data.startswith("bd:delok:"))
async def bd_del_ok(c: CallbackQuery):
    b = await db.get(db.Bedolaga, _id(c.data))
    if b:
        await db.delete(b)
    await bd_list(c)


# ------------------------------------------------------------------ настройки .env
@router.callback_query(F.data.startswith("bd:env:"))
async def bd_env(c: CallbackQuery, bot: Bot):
    b, s = await _bot(c)
    if not s:
        return
    await c.answer()
    await run_task(bot, c.message.chat.id, s, f".env · {b.name} (секреты скрыты)",
                   scripts.bedolaga_cmd(b.install_dir, b.compose_file, "env"))
    rows = [[(f"✏️ {k}", f"bd:ek:{b.id}:{k}")] for k in ENV_KEYS]
    rows.append([("✏️ Другая переменная", f"bd:ek:{b.id}:*")])
    rows.append([("⬅️ К боту", f"bd:v:{b.id}")])
    await say(c, "⚙️ Что поменять? После сохранения бот перезапустится.", kb(*rows))


@form("bd_env", "⚙️ Переменная Bedolaga", [
    Field("key", "Имя переменной (A-Z, 0-9, _):"),
    Field("value", "Новое значение (пусто — «-»). Сообщение удалю:", secret=True),
])
async def bd_env_done(event, values, ctx):
    import re
    key = values["key"].strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", key):
        return await say(event, "❌ Плохое имя переменной.")
    val = "" if values["value"] == "-" else values["value"]
    b = await db.get(db.Bedolaga, ctx["id"])
    s = await db.get(db.Server, b.server_id)
    res = await run_task(event.bot, chat_id(event), s, f"{b.name}: {key}",
                         scripts.bedolaga_env(b.compose_file, {key: val}, b.install_dir))
    if res.ok:
        if key == "BOT_TOKEN":
            b.bot_token_enc = crypto.enc(val)
        elif key == "ADMIN_IDS":
            b.admin_ids = val
        elif key == "WEBHOOK_URL":
            b.hooks_domain = discover._host(val) or b.hooks_domain
        await db.save(b)
    await say(event, "✅ Сохранено, бот перезапущен." if res.ok else "❌ Не вышло, лог выше.",
              kb([("⚙️ Ещё", f"bd:env:{b.id}"), ("⬅️ К боту", f"bd:v:{b.id}")]))


@router.callback_query(F.data.startswith("bd:ek:"))
async def bd_env_key(c: CallbackQuery, state: FSMContext):
    _, _, i, key = c.data.split(":")
    await start_form(c, state, "bd_env", ctx={"id": int(i)}, preset={} if key == "*" else {"key": key})


# ------------------------------------------------------------------ кабинет
async def _cab(b: db.Bedolaga):
    sid = await db.setting(f"cabinet_srv:{b.id}") or b.server_id
    s = await db.get(db.Server, sid)
    ct = (await proxy.containers(s)).get("cabinet", "cabinet_frontend") if s else "cabinet_frontend"
    return s, ct


@router.callback_query(F.data.startswith("bd:cab:"))
async def bd_cabinet(c: CallbackQuery):
    b = await db.get(db.Bedolaga, _id(c.data))
    s, ct = await _cab(b)
    i = b.id
    await show(c, f"🗂 <b>Кабинет · {h(b.name)}</b>\n"
                  f"Домен: {('https://' + h(b.cabinet_domain)) if b.cabinet_domain else '—'}\n"
                  f"Сервер: {h(s.name) if s else '—'} · контейнер <code>{h(ct)}</code>\n"
                  f"API кабинета: /api → бот Bedolaga",
               kb([("📜 Логи", f"bd:cl:{i}"), ("🔄 Перезапуск", f"bd:cr:{i}")],
                  [("⬆️ Обновить", f"bd:cu:{i}"), ("🩹 Фикс reverse proxy", f"srv:fix:{s.id}" if s else "noop")],
                  [("⬅️ К боту", f"bd:v:{i}")]))


@router.callback_query(F.data.startswith(("bd:cl:", "bd:cr:", "bd:cu:")))
async def bd_cabinet_action(c: CallbackQuery, bot: Bot):
    b = await db.get(db.Bedolaga, _id(c.data))
    s, ct = await _cab(b)
    if not s:
        return await c.answer("Нет сервера кабинета", show_alert=True)
    kind = {"cl": "logs", "cr": "restart", "cu": "update"}[c.data.split(":")[1]]
    await c.answer()
    script = scripts.cabinet_cmd(ct, kind)
    if kind != "logs":
        script += scripts.FIX_PROXY_NETS.split("\n", 1)[1]
    await run_task(bot, c.message.chat.id, s, f"Кабинет: {kind}", script)
