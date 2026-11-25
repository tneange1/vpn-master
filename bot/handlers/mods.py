"""«Модификации»: Link Extractor и Backup-Restore — на каждую панель отдельно (свой бот),
WARP-native — на каждую ноду отдельно."""
import json

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import crypto, db, scripts, terminal, xray
from ..forms import Field, form, start_form
from ..rw_api import RwError, for_panel
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show

router = Router(name="mods")


async def _mod(kind: str, panel_id=None, node_id=None) -> db.Mod | None:
    where = [db.Mod.kind == kind]
    if panel_id:
        where.append(db.Mod.panel_id == int(panel_id))
    if node_id:
        where.append(db.Mod.node_id == int(node_id))
    rows = await db.all_(db.Mod, *where)
    return rows[0] if rows else None


@router.callback_query(F.data == "mod")
async def mods_menu(c: CallbackQuery):
    panels = await db.all_(db.Panel)
    rows = [[(f"🖥 {p.name}", f"mod:p:{p.id}")] for p in panels] or [[("Панелей нет", "noop")]]
    rows.append([("⬅️ Назад", "mng")])
    await show(c, "🧩 <b>Модификации</b>\n\nНа панель: 🔗 Link Extractor, 💾 Backup-Restore (у каждой панели свой бот).\n"
                  "На ноду: 🌐 WARP-native.\n\nВыбери панель:", kb(*rows))


@router.callback_query(F.data.startswith("mod:p:"))
async def mods_panel(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    le = await _mod("link_extractor", panel_id=p.id)
    br = await _mod("backup_restore", panel_id=p.id)
    nodes = await db.all_(db.Node, db.Node.panel_id == p.id)
    i = p.id
    rows = [
        [(f"🔗 Link Extractor {'✅' if le else '➕'}", f"le:v:{i}")],
        [(f"💾 Backup-Restore {'✅' if br else '➕'}", f"br:v:{i}")],
    ]
    rows += [[(f"🌐 WARP · {n.name} {'✅' if n.warp else ''}", f"mod:n:{n.id}")] for n in nodes]
    rows.append([("⬅️ Модификации", "mod"), ("🖥 Панель", f"pn:v:{i}")])
    await show(c, f"🧩 <b>Модификации · {h(p.name)}</b>", kb(*rows))


def _panel_server_required(p, server):
    if not server:
        return "У панели не привязан сервер с SSH — модификации ставятся по SSH."
    return None


# ------------------------------------------------------------------ Link Extractor
@router.callback_query(F.data.startswith("le:v:"))
async def le_view(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    m = await _mod("link_extractor", panel_id=p.id)
    i = p.id
    if not m:
        return await show(c, "🔗 <b>Link Extractor</b> (github.com/tneange1/link-extractor)\n\n"
                             "Ставится на сервер панели вместе со своим Telegram-ботом. "
                             "Нужен токен отдельного бота от @BotFather.\n"
                             + ("Caddy управляется ботом — маршруты пропишу сам." if p.managed_proxy else
                                "⚠️ Caddy ставил не vpn-master: маршруты в reverse proxy настрой сам "
                                "(подписка → link-extractor:3020, /link-extractor/ → link-extractor:3020)."),
                          kb([("➕ Установить", f"le:inst:{i}")], [("⬅️ Назад", f"mod:p:{i}")]))
    await show(c, f"🔗 <b>Link Extractor · {h(p.name)}</b> ✅\nАдмины бота: <code>{h(m.admin_ids)}</code>\n"
                  f"Страница: https://{h(p.domain)}/link-extractor/",
               kb([("🔐 Пароль страницы и API_TOKEN", f"le:sec:{i}")],
                  [("⬆️ Обновить", f"upd:run:link_extractor:{p.server_id}"), ("🔁 Переустановить", f"le:inst:{i}")],
                  [("🗑 Остановить", f"le:rm:{i}")],
                  [("⬅️ Назад", f"mod:p:{i}")]))


@form("le_inst", "🔗 Установка Link Extractor", [
    Field("token", "Токен отдельного бота для Link Extractor (сообщение удалю):", secret=True),
    Field("admins", "Telegram ID админов этого бота через запятую:", "ids"),
])
async def le_inst_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    server = await db.get(db.Server, p.server_id)
    if err := _panel_server_required(p, server):
        return await say(event, "❌ " + err)
    res = await run_task(event.bot, chat_id(event), server, "Установка Link Extractor",
                         scripts.link_extractor_install(values["token"], values["admins"]))
    m = await _mod("link_extractor", panel_id=p.id) or db.Mod(kind="link_extractor", panel_id=p.id)
    m.server_id, m.bot_token_enc, m.admin_ids = server.id, crypto.enc(values["token"]), values["admins"]
    if res.marks.get("LE_SECRETS"):
        m.secrets_enc = crypto.enc(res.marks["LE_SECRETS"])
    await db.save(m)
    if res.ok and p.managed_proxy:
        p.link_extractor = True
        await db.save(p)
        await run_task(event.bot, chat_id(event), server, "Маршруты Caddy для Link Extractor",
                       scripts.apply_panel_site(p.domain, p.sub_domain, crypto.dec(p.cookie_enc) if p.protected else None, True))
    await say(event, f"{'✅' if res.ok else '⚠️'} Link Extractor установлен. API-токен Remnawave задай в его боте "
                     "(⚙️ Настройки).", kb([("🔐 Секреты", f"le:sec:{p.id}"), ("⬅️ Назад", f"mod:p:{p.id}")]))


@router.callback_query(F.data.startswith("le:inst:"))
async def le_inst(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "le_inst", ctx={"panel": int(c.data.split(":")[2])})


@router.callback_query(F.data.startswith("le:sec:"))
async def le_secrets(c: CallbackQuery, bot: Bot):
    p = await db.get(db.Panel, c.data.split(":")[2])
    m = await _mod("link_extractor", panel_id=p.id)
    server = await db.get(db.Server, p.server_id)
    res = await run_task(bot, c.message.chat.id, server, "Секреты Link Extractor", scripts.LINK_EXTRACTOR_SECRETS)
    raw = res.marks.get("LE_SECRETS") or (crypto.dec(m.secrets_enc) if m else None)
    if res.marks.get("LE_SECRETS") and m:
        m.secrets_enc = crypto.enc(res.marks["LE_SECRETS"])
        await db.save(m)
    try:
        pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=1) if raw else "—"
    except ValueError:
        pretty = raw
    await say(c, f"🔐 <b>Link Extractor</b>\n<tg-spoiler><code>{h(pretty)}</code></tg-spoiler>",
              kb([("⬅️ Назад", f"le:v:{p.id}")]))


@router.callback_query(F.data.startswith("le:rm:"))
async def le_remove(c: CallbackQuery, bot: Bot):
    p = await db.get(db.Panel, c.data.split(":")[2])
    server = await db.get(db.Server, p.server_id)
    await run_task(bot, c.message.chat.id, server, "Остановка Link Extractor", scripts.LINK_EXTRACTOR_REMOVE)
    m = await _mod("link_extractor", panel_id=p.id)
    if m:
        await db.delete(m)
    if p.link_extractor and p.managed_proxy:
        p.link_extractor = False
        await db.save(p)
        await run_task(bot, c.message.chat.id, server, "Маршруты Caddy",
                       scripts.apply_panel_site(p.domain, p.sub_domain, crypto.dec(p.cookie_enc) if p.protected else None, False))
    await say(c, "🗑 Link Extractor остановлен.", kb([("⬅️ Назад", f"mod:p:{p.id}")]))


# ------------------------------------------------------------------ Backup-Restore (distillium)
@router.callback_query(F.data.startswith("br:v:"))
async def br_view(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    m = await _mod("backup_restore", panel_id=p.id)
    i = p.id
    text = ("💾 <b>Backup-Restore</b> (github.com/distillium/remnawave-backup-restore)\n\n"
            "Установщик интерактивный — открою терминал прямо тут. Для каждой панели заведи <b>отдельного бота</b>: "
            "сохрани его токен в боте, чтобы потом вставить в настройки скрипта (пункт «Настройки» → Telegram).\n")
    if m:
        text += f"\nТокен бота сохранён ✅, chat ID: <code>{h(m.admin_ids or '—')}</code>"
    rows = [[("💾 Сохранить токен/chat ID", f"br:tok:{i}")],
            [("🧰 Установить (терминал)", f"br:inst:{i}"), ("📋 Меню rw-backup", f"br:menu:{i}")],
            [("▶️ Бэкап сейчас", f"br:now:{i}"), ("⬆️ Обновить", f"br:upd:{i}")],
            [("⬅️ Назад", f"mod:p:{i}")]]
    if m and m.bot_token_enc:
        rows.insert(1, [("👁 Показать токен", f"br:show:{i}")])
    await show(c, text, kb(*rows))


@form("br_tok", "💾 Бот для бэкапов панели", [
    Field("token", "Токен отдельного бота для бэкапов (сообщение удалю):", secret=True),
    Field("chat", "Chat ID, куда слать бэкапы (твой ID или ID группы):"),
])
async def br_tok_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    m = await _mod("backup_restore", panel_id=p.id) or db.Mod(kind="backup_restore", panel_id=p.id)
    m.server_id, m.bot_token_enc, m.admin_ids = p.server_id, crypto.enc(values["token"]), values["chat"]
    await db.save(m)
    await say(event, "✅ Сохранено. Теперь «🧰 Установить» — и вставь эти данные, когда скрипт спросит.",
              kb([("⬅️ Назад", f"br:v:{p.id}")]))


@router.callback_query(F.data.startswith("br:tok:"))
async def br_tok(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "br_tok", ctx={"panel": int(c.data.split(":")[2])})


@router.callback_query(F.data.startswith("br:show:"))
async def br_show(c: CallbackQuery):
    m = await _mod("backup_restore", panel_id=c.data.split(":")[2])
    await say(c, f"Токен: <tg-spoiler><code>{h(crypto.dec(m.bot_token_enc))}</code></tg-spoiler>\n"
                 f"Chat ID: <code>{h(m.admin_ids)}</code>")


async def _panel_term(c: CallbackQuery, bot: Bot, cmd: str, title: str):
    p = await db.get(db.Panel, c.data.split(":")[2])
    server = await db.get(db.Server, p.server_id)
    if not server:
        return await c.answer("У панели нет SSH-сервера", show_alert=True)
    await c.answer()
    try:
        await terminal.start(bot, c.message.chat.id, c.from_user.id, server, cmd, title)
    except Exception as e:  # noqa: BLE001
        await say(c, f"❌ {h(type(e).__name__)}: {h(e)}")
    m = await _mod("backup_restore", panel_id=p.id) or db.Mod(kind="backup_restore", panel_id=p.id)
    m.server_id = server.id
    await db.save(m)


@router.callback_query(F.data.startswith("br:inst:"))
async def br_install(c: CallbackQuery, bot: Bot):
    await _panel_term(c, bot, scripts.BACKUP_RESTORE_INSTALL_CMD, "Установка backup-restore")


@router.callback_query(F.data.startswith("br:menu:"))
async def br_menu(c: CallbackQuery, bot: Bot):
    await _panel_term(c, bot, scripts.BACKUP_RESTORE_MENU_CMD, "rw-backup")


@router.callback_query(F.data.startswith(("br:now:", "br:upd:")))
async def br_run(c: CallbackQuery, bot: Bot):
    p = await db.get(db.Panel, c.data.split(":")[2])
    server = await db.get(db.Server, p.server_id)
    now = c.data.startswith("br:now:")
    await c.answer()
    await run_task(bot, c.message.chat.id, server, "rw-backup " + ("backup" if now else "update"),
                   scripts.BACKUP_RESTORE_NOW if now else scripts.BACKUP_RESTORE_UPDATE)


# ------------------------------------------------------------------ WARP-native (на ноду)
@router.callback_query(F.data.startswith("mod:n:"))
async def warp_view(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    i = n.id
    await show(c, f"🌐 <b>WARP-native · {h(n.name)}</b> {'✅' if n.warp else ''}\n\n"
                  "1) «🧰 Установить» — откроется терминал (скрипт спросит язык).\n"
                  "2) «➕ warp-out в профиль» — добавлю outbound и правило в профиль ноды через API.\n"
                  "Это касается только этой ноды и её профиля.",
               kb([("🧰 Установить (терминал)", f"wp:inst:{i}"), ("📊 Статус", f"wp:st:{i}")],
                  [("➕ warp-out: AI/стриминг", f"wp:add:{i}:list"), ("➕ warp-out: весь трафик", f"wp:add:{i}:all")],
                  [("➖ Убрать warp-out из профиля", f"wp:rmout:{i}")],
                  [("🗑 Удалить WARP (терминал)", f"wp:uninst:{i}")],
                  [("⬅️ К ноде", f"nd:v:{i}")]))


@router.callback_query(F.data.startswith(("wp:inst:", "wp:uninst:")))
async def warp_term(c: CallbackQuery, bot: Bot):
    n = await db.get(db.Node, c.data.split(":")[2])
    server = await db.get(db.Server, n.server_id)
    install = c.data.startswith("wp:inst:")
    await c.answer()
    try:
        await terminal.start(bot, c.message.chat.id, c.from_user.id, server,
                             scripts.WARP_INSTALL_CMD if install else scripts.WARP_UNINSTALL_CMD,
                             f"WARP {'установка' if install else 'удаление'} · {n.name}")
    except Exception as e:  # noqa: BLE001
        return await say(c, f"❌ {h(type(e).__name__)}: {h(e)}")
    n.warp = install
    await db.save(n)
    m = await _mod("warp", node_id=n.id)
    if install and not m:
        await db.save(db.Mod(kind="warp", node_id=n.id, panel_id=n.panel_id, server_id=n.server_id))
    elif not install and m:
        await db.delete(m)


@router.callback_query(F.data.startswith("wp:st:"))
async def warp_status(c: CallbackQuery, bot: Bot):
    n = await db.get(db.Node, c.data.split(":")[2])
    await c.answer()
    await run_task(bot, c.message.chat.id, await db.get(db.Server, n.server_id), f"WARP статус · {n.name}",
                   scripts.WARP_STATUS)


async def _patch_profile(c: CallbackQuery, n: db.Node, fn):
    p = await db.get(db.Panel, n.panel_id)
    if not n.profile_uuid:
        return await c.answer("У ноды не сохранён профиль", show_alert=True)
    try:
        rw = for_panel(p)
        pr = await rw.profile(n.profile_uuid)
        await rw.update_profile(n.profile_uuid, fn(pr["config"]))
        await rw.close()
    except (RwError, KeyError) as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Профиль обновлён ✅", show_alert=True)


@router.callback_query(F.data.startswith("wp:add:"))
async def warp_add(c: CallbackQuery):
    _, _, i, mode = c.data.split(":")
    n = await db.get(db.Node, i)
    await _patch_profile(c, n, lambda cfg: xray.add_warp(cfg, everything=(mode == "all")))


@router.callback_query(F.data.startswith("wp:rmout:"))
async def warp_rm_out(c: CallbackQuery):
    n = await db.get(db.Node, c.data.split(":")[2])
    await _patch_profile(c, n, xray.remove_warp)
