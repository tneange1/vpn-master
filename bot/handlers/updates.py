"""«Обновления»: панель, подписка, Caddy, ноды, Bedolaga, кабинет, link-extractor — по отдельности."""
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from .. import db, scripts
from ..runner import run_task
from ..ui import h, kb, say, show

router = Router(name="updates")

LABEL = {"panel": "Панель Remnawave", "subpage": "Страница подписки", "caddy": "Caddy", "nginx": "Nginx",
         "node": "Нода", "selfsteal": "Self-steal Nginx", "bedolaga": "Bedolaga Bot",
         "cabinet": "Bedolaga Cabinet", "link_extractor": "Link Extractor", "backup_restore": "rw-backup"}


@router.callback_query(F.data == "upd")
async def upd_menu(c: CallbackQuery):
    rows = [[(f"🖥 {p.name}", f"upd:p:{p.id}")] for p in await db.all_(db.Panel)]
    rows += [[(f"🤖 {b.name}", f"upd:b:{b.id}")] for b in await db.all_(db.Bedolaga)]
    if not rows:
        rows = [[("Пока нечего обновлять", "noop")]]
    rows.append([("⬅️ Назад", "mng")])
    await show(c, "⬆️ <b>Обновления</b>\nПорядок по docs.rw: сначала панель, потом ноды.", kb(*rows))


@router.callback_query(F.data.startswith("upd:p:"))
async def upd_panel(c: CallbackQuery):
    p = await db.get(db.Panel, c.data.split(":")[2])
    nodes = await db.all_(db.Node, db.Node.panel_id == p.id)
    s = p.server_id
    rows = []
    if s:
        rows += [[("🌊 Панель", f"upd:run:panel:{s}"), ("📄 Подписка", f"upd:run:subpage:{s}")]]
        if p.managed_proxy:
            srv = await db.get(db.Server, s)
            kind = srv.proxy if srv and srv.proxy else "caddy"
            rows += [[(f"🧭 {kind.capitalize()}", f"upd:run:{kind}:{s}")]]
        if p.link_extractor:
            rows += [[("🔗 Link Extractor", f"upd:run:link_extractor:{s}")]]
    rows += [[(f"🛰 Нода {n.name}", f"upd:run:node:{n.id}"), ("nginx self-steal", f"upd:run:selfsteal:{n.id}")] for n in nodes]
    if nodes:
        rows.append([("🛰 Все ноды панели по очереди", f"upd:allnodes:{p.id}")])
    rows.append([("⬅️ Обновления", "upd")])
    await show(c, f"⬆️ <b>{h(p.name)}</b>" + ("" if s else "\n⚠️ у панели нет SSH-сервера"), kb(*rows))


@router.callback_query(F.data.startswith("upd:b:"))
async def upd_bot(c: CallbackQuery):
    b = await db.get(db.Bedolaga, c.data.split(":")[2])
    rows = [[("🤖 Bedolaga Bot", f"upd:run:bedolaga:{b.id}")]]
    if b.cabinet:
        rows.append([("🗂 Кабинет", f"upd:run:cabinet:{b.id}")])
        cab_srv = await db.setting(f"cabinet_srv:{b.id}") or b.server_id
        rows.append([("🩹 Кабинет отдаёт 502/белый экран — фикс reverse proxy", f"srv:fix:{cab_srv}")])
    rows.append([("⬅️ Обновления", "upd")])
    await show(c, f"⬆️ <b>{h(b.name)}</b>", kb(*rows))


async def _target(kind: str, obj_id: str):
    """Возвращает (server, script, title)."""
    if kind in ("node", "selfsteal"):
        n = await db.get(db.Node, obj_id)
        return await db.get(db.Server, n.server_id), scripts.UPDATES[kind], f"{LABEL[kind]} {n.name}"
    if kind in ("bedolaga", "cabinet"):
        b = await db.get(db.Bedolaga, obj_id)
        srv_id = b.server_id
        if kind == "cabinet":
            srv_id = await db.setting(f"cabinet_srv:{b.id}") or b.server_id
        script = scripts.bedolaga_update(b.compose_file, b.install_dir) if kind == "bedolaga" \
            else scripts.UPDATES["cabinet"] + scripts.FIX_PROXY_NETS.split("\n", 1)[1]
        return await db.get(db.Server, srv_id), script, f"{LABEL[kind]} {b.name}"
    s = await db.get(db.Server, obj_id)  # panel/subpage/caddy/link_extractor/backup_restore — по id сервера
    return s, scripts.UPDATES[kind], LABEL[kind]


@router.callback_query(F.data.startswith("upd:run:"))
async def upd_run(c: CallbackQuery, bot: Bot):
    _, _, kind, obj_id = c.data.split(":")
    server, script, title = await _target(kind, obj_id)
    if not server:
        return await c.answer("Нет SSH-сервера", show_alert=True)
    await c.answer()
    await run_task(bot, c.message.chat.id, server, "Обновление: " + title, script)


@router.callback_query(F.data.startswith("upd:allnodes:"))
async def upd_all_nodes(c: CallbackQuery, bot: Bot):
    p = await db.get(db.Panel, c.data.split(":")[2])
    await c.answer()
    results = []
    for n in await db.all_(db.Node, db.Node.panel_id == p.id):
        server = await db.get(db.Server, n.server_id)
        if not server:
            results.append(f"⚪️ {h(n.name)}: нет сервера")
            continue
        r = await run_task(bot, c.message.chat.id, server, f"Обновление ноды {n.name}", scripts.UPDATES["node"])
        results.append(f"{'✅' if r.ok else '❌'} {h(n.name)}")
    await say(c, "Итог:\n" + "\n".join(results), kb([("⬅️ Назад", f"upd:p:{p.id}")]))
