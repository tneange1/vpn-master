"""🔗 Link Extractor прямо в vpn-master (без отдельного бота): установка на сервер панели и всё управление
через API экстрактора — ключи и подписки, источники, сквады, группы стран, проверка подписки,
бэкапы, настройки, обновления. Перенесено из bot/src/bot.js репозитория link-extractor."""
import json
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from .. import config, db, le_api, proxy, scripts
from ..forms import Field, form, start_form
from ..le_api import LeError
from ..rw_api import RwError
from ..runner import run_task
from ..ui import chat_id, h, kb, say, show
from .panels import ensure_api_token

router = Router(name="linkext")


def plural(n: int, a: str, b: str, c: str) -> str:
    m, hh = n % 10, n % 100
    return a if m == 1 and hh != 11 else b if 2 <= m <= 4 and not 10 <= hh < 20 else c


def keys_word(n: int) -> str:
    return f"{n} {plural(n, 'ключ', 'ключа', 'ключей')}"


def _p(data: str, i: int = 2) -> str:
    return data.split(":")[i]


async def _err(c, e, back: str):
    await show(c, f"⚠️ {h(e)}", kb([("⬅️ Назад", back)]))


# ================================================================== главный экран
@router.callback_query(F.data.startswith("lx:v:"))
async def lx_main(c: CallbackQuery):
    pid = _p(c.data)
    p = await db.get(db.Panel, pid)
    conf = le_api.conf_of(await le_api.mod_for(p.id), p)
    if not conf:
        srv = await db.get(db.Server, p.server_id)
        note = ("Прокси ведёт бот — домен подписки направлю в экстрактор сам."
                if p.managed_proxy else "Прокси не от бота — после установки нажми «🩹 Фикс reverse proxy» в карточке панели.")
        return await show(c, "🔗 <b>Link Extractor</b> · " + h(p.name) + "\n\n"
                             "Ставится на сервер панели (нужны контейнеры remnawave и remnawave-subscription-page). "
                             "Отдельный бот не нужен: всё управление здесь, уведомления экстрактора придут сюда.\n\n"
                             + h(note) + ("" if srv else "\n\n⚠️ У панели не привязан SSH-сервер."),
                          kb([("🚀 Установить", f"lx:inst:{pid}")] if srv else [],
                             [("🔗 Подключить уже установленный", f"lx:conn:{pid}")],
                             [("⬅️ Модификации", f"mod:p:{pid}")]))
    try:
        async with await le_api.for_panel(pid) as le:
            st = await le.state()
    except LeError as e:
        return await show(c, f"🔗 <b>Link Extractor · {h(p.name)}</b>\n<code>{h(conf['url'])}</code>\n\n⚠️ {h(e)}",
                          kb([("🔄 Ещё раз", f"lx:v:{pid}"), ("🔗 Переподключить", f"lx:conn:{pid}")],
                             [("🗑 Убрать", f"lx:rm:{pid}"), ("⬅️ Назад", f"mod:p:{pid}")]))
    srcs = st.get("sources", [])
    subs = sum(1 for s in srcs if s.get("kind") == "sub")
    errs = sum(1 for s in srcs if s.get("error") or s.get("hostError") or s.get("squadError"))
    groups = st.get("groups") or []
    all_g = next((g for g in groups if g.get("code") == "ALL"), None)
    sett = st.get("settings", {})
    stub = sett.get("stubInbound")
    lines = [f"🔗 <b>Link Extractor · {h(p.name)}</b>", f"<code>{h(conf['url'])}</code>", "",
             f"📚 Источников: <b>{len(srcs)}</b> (подписок: {subs})" + (f" · ⚠️ с ошибками: {errs}" if errs else ""),
             f"🔑 Ключей в работе: <b>{all_g['count'] if all_g else st.get('activeCount', 0)}</b>",
             f"🌍 Группы: {'вкл, стран: ' + str(len([g for g in groups if g.get('code') != 'ALL'])) if sett.get('groupsEnabled') else 'выкл'}",
             f"🧩 Заглушка: {h((stub.get('profileName') or '') + ' → ' + stub.get('tag', '')) if stub else '—'}",
             f"🔔 Уведомления: {'да' if st.get('telegram') else 'нет'} · проверка ACTIVE: {'да' if st.get('activeCheck') else 'нет'}",
             f"🏷 Версия: {h(st.get('version') or '—')}"]
    if st.get("squadsError"):
        lines.append(f"\n⚠️ {h(st['squadsError'])}")
    await show(c, "\n".join(lines), kb(
        [("📥 Добавить ключи", f"lx:add:{pid}"), ("📚 Источники", f"lx:src:{pid}")],
        [("🌍 Группы", f"lx:grp:{pid}"), ("🔎 Проверить подписку", f"lx:chk:{pid}")],
        [("💾 Бэкапы", f"lx:bk:{pid}"), ("⚙️ Настройки", f"lx:set:{pid}")],
        [("🔄 Обновить все подписки", f"lx:ref:{pid}")],
        [("⬆️ Обновить экстрактор", f"lx:upd:{pid}"), ("⬆️ Обновить Remnawave", f"lx:updrw:{pid}")],
        [("🩹 Фикс маршрутов (экстрактор на домене подписки)", f"lx:fix:{pid}")],
        [("🗑 Убрать", f"lx:rm:{pid}"), ("⬅️ Модификации", f"mod:p:{pid}")]))


@router.callback_query(F.data.startswith("lx:fix:"))
async def lx_fix(c: CallbackQuery, bot: Bot):
    """Экстрактор — только на домене подписки: чиним прокси сервера панели."""
    from ..discover import fix_proxy
    p = await db.get(db.Panel, _p(c.data))
    server = await db.get(db.Server, p.server_id)
    if not server:
        return await c.answer("У панели нет SSH-сервера", show_alert=True)
    await c.answer("Чиню прокси…")
    await fix_proxy(bot, c.message.chat.id, server)
    p = await db.get(db.Panel, p.id)
    if p.sub_domain and p.sub_local:
        m = await le_api.mod_for(p.id)
        conf = le_api.conf_of(m, p)
        if conf:
            await le_api.save_conf(p, le_api.default_url(p), conf["token"], server.id)
    await say(c, f"🔗 Экстрактор: https://{h(p.sub_domain or '—')}/link-extractor/", kb([("⬅️ Экстрактор", f"lx:v:{p.id}")]))


# ================================================================== установка / подключение
@router.callback_query(F.data.startswith("lx:inst:"))
async def lx_install(c: CallbackQuery):
    pid = _p(c.data)
    p = await db.get(db.Panel, pid)
    await show(c, f"🚀 <b>Установка Link Extractor · {h(p.name)}</b>\n\n"
                  f"• режим: только экстрактор, без отдельного бота\n"
                  f"• подписка: {h(p.sub_domain or '—')} · панель: {h(p.domain)}\n"
                  f"• API-токен Remnawave возьму из панели (или выпущу по логину)\n"
                  f"• уведомления — сюда, автообновление экстрактора — вкл\n"
                  f"• прокси: {'пропишу маршруты в прокси бота' if p.managed_proxy else 'install.sh сам настроит Caddy/nginx'}",
               kb([("🚀 Поехали", f"lx:instgo:{pid}")], [("⬅️ Назад", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:instgo:"))
async def lx_install_go(c: CallbackQuery, bot: Bot):
    p = await db.get(db.Panel, _p(c.data))
    server = await db.get(db.Server, p.server_id)
    if not server:
        return await c.answer("У панели нет SSH-сервера", show_alert=True)
    await c.answer()
    try:
        rw_token = await ensure_api_token(p)
    except RwError as e:
        return await say(c, f"❌ Нужен API-токен панели: <code>{h(e)}</code>", kb([("🔑 Доступ к API", f"pn:api:{p.id}")]))
    res = await run_task(bot, c.message.chat.id, server, "Установка Link Extractor", scripts.link_extractor_install(
        panel_name=p.name, panel_domain=p.domain, sub_domain=p.sub_domain if p.sub_local else None,
        rw_token=rw_token, tg_token=config.BOT_TOKEN, tg_chat=config.OWNER_ID, proxy_auto=False))
    m = re.search(r"url=(\S*) token=(\S*)", res.marks.get("LE_RESULT", ""))
    if not (res.ok and m and m.group(2)):
        return await say(c, "❌ Установка не удалась — лог выше.", kb([("🔁 Ещё раз", f"lx:instgo:{p.id}"), ("⬅️ Назад", f"lx:v:{p.id}")]))
    await le_api.save_conf(p, le_api.default_url(p), m.group(2), server.id)
    p.link_extractor = True
    p = await db.save(p)
    from ..discover import fix_proxy
    await fix_proxy(bot, c.message.chat.id, server)  # домен подписки → экстрактор
    try:
        async with await le_api.for_panel(p.id) as le:
            await le.call("GET", "/info", timeout=20)
        ok = "✅ отвечает"
    except LeError as e:
        ok = f"⚠️ пока не отвечает ({h(e)}) — проверь DNS/прокси"
    await say(c, f"🎉 Link Extractor установлен · {ok}", kb([("🔗 Открыть", f"lx:v:{p.id}")]))


@form("lx_conn", "🔗 Подключить Link Extractor", [
    Field("url", "Адрес экстрактора — обычно домен подписки, например <code>sub.example.com</code>:"),
    Field("token", "<code>apiToken</code> экстрактора (в /opt/link-extractor/data/store.json → secrets, "
                   "или старый API_TOKEN/ADMIN_PASSWORD). Сообщение удалю:", secret=True),
])
async def lx_conn_done(event, values, ctx):
    p = await db.get(db.Panel, ctx["panel"])
    await le_api.save_conf(p, values["url"], values["token"], p.server_id)
    try:
        async with await le_api.for_panel(p.id) as le:
            info = await le.call("GET", "/info", timeout=20)
        msg = f"✅ Подключено: {h(info.get('panel') or '')} · версия {h(info.get('version') or '?')}"
    except LeError as e:
        msg = f"⚠️ Сохранил, но экстрактор не ответил: {h(e)}"
    await say(event, msg, kb([("🔗 Открыть", f"lx:v:{p.id}")]))


@router.callback_query(F.data.startswith("lx:conn:"))
async def lx_conn(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "lx_conn", ctx={"panel": int(_p(c.data))})


@router.callback_query(F.data.startswith("lx:rm:"))
async def lx_rm(c: CallbackQuery):
    pid = _p(c.data)
    await show(c, "🗑 Убрать Link Extractor?", kb([("Только из бота", f"lx:rmok:{pid}:0")],
                                                 [("Из бота + остановить контейнеры", f"lx:rmok:{pid}:1")],
                                                 [("✖️ Отмена", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:rmok:"))
async def lx_rm_ok(c: CallbackQuery, bot: Bot):
    _, _, pid, stop = c.data.split(":")
    p = await db.get(db.Panel, pid)
    server = await db.get(db.Server, p.server_id)
    await c.answer()
    if stop == "1" and server:
        await run_task(bot, c.message.chat.id, server, "Остановка Link Extractor", scripts.LINK_EXTRACTOR_REMOVE)
        if p.link_extractor and p.managed_proxy:
            p.link_extractor = False
            p = await db.save(p)
            await run_task(bot, c.message.chat.id, server, "Маршруты прокси",
                           proxy.apply_script(proxy.kind_of(server), proxy.sname("panel", p.id),
                                          proxy.panel_sites(p, proxy.panel_cookie(p))))
    m = await le_api.mod_for(p.id)
    if m:
        await db.delete(m)
    await say(c, "🗑 Убрано.", kb([("⬅️ Модификации", f"mod:p:{p.id}")]))


# ================================================================== источники
@router.callback_query(F.data.startswith("lx:src:"))
async def lx_sources(c: CallbackQuery):
    pid = _p(c.data)
    try:
        async with await le_api.for_panel(pid) as le:
            st = await le.state()
    except LeError as e:
        return await _err(c, e, f"lx:v:{pid}")
    rows = []
    for s in st.get("sources", []):
        title = s.get("title") or s.get("squadName") or ("Подписка" if s.get("kind") == "sub" else "Ключи")
        flag = ("✅" if s.get("enabled") else "⛔") + ("⚠️" if s.get("error") or s.get("hostError") else "")
        rows.append([(f"{flag} {title[:30]} · {len(s.get('nodes', []))}", f"lx:s:{pid}:{s['id']}")])
    rows.append([("📥 Добавить ключи", f"lx:add:{pid}")])
    rows.append([("⬅️ Назад", f"lx:v:{pid}")])
    await show(c, "📚 <b>Источники</b>" if st.get("sources") else "📚 Источников пока нет.", kb(*rows))


async def _source(pid, sid):
    async with await le_api.for_panel(pid) as le:
        st = await le.state()
    src = next((x for x in st.get("sources", []) if x["id"] == sid), None)
    if not src:
        raise LeError("Источник не найден (его удалили?)")
    return st, src


async def _source_screen(c: CallbackQuery, pid, sid):
    try:
        st, src = await _source(pid, sid)
    except LeError as e:
        return await _err(c, e, f"lx:src:{pid}")
    nodes = src.get("nodes", [])
    on = sum(1 for n in nodes if not n.get("off"))
    ua = src.get("userAgent")
    lines = [f"{'✅' if src.get('enabled') else '⛔'} <b>{h(src.get('title') or src.get('squadName') or 'Источник')}</b>",
             f"🔗 <code>{h(src.get('input'))}</code>" if src.get("kind") == "sub" else "🔑 Ключи вставлены вручную",
             "👥 " + ("Всем пользователям" if src.get("scope") == "all" else f"Сквад: <b>{h(src.get('squadName') or '—')}</b>")]
    if src.get("hostMode"):
        lines.append(f"🏷 Тег хостов: <b>{h(src.get('hostTag') or '')}</b> · хостов: {len(src.get('hosts') or {})}")
    if src.get("kind") == "sub":
        lines.append(f"🕵️ User-Agent: {h('Авто' if ua in (None, '', 'auto') else ua)}"
                     + (f" (сработал: {h(src['usedUaLabel'])})" if src.get("usedUaLabel") else ""))
    lines.append(f"🔑 Ключей: {on} из {len(nodes)}")
    if src.get("updatedAt"):
        lines.append(f"🕒 Обновлено: {h(src['updatedAt'][:16].replace('T', ' '))} UTC")
    for k, label in (("error", "Ошибка обновления"), ("hostError", "Хосты"), ("squadError", "Сквад")):
        if src.get(k):
            lines.append(f"⚠️ {label}: {h(src[k])}")
    ref = f"{pid}:{sid}"
    rows = [[("⛔ Выключить" if src.get("enabled") else "✅ Включить", f"lx:st:{ref}"), ("🔑 Ключи", f"lx:sk:{ref}")]]
    if src.get("kind") == "sub":
        rows.append([("🔄 Обновить", f"lx:sr:{ref}"), ("🕵️ User-Agent", f"lx:ua:{ref}")])
    rows.append([("👥 Кому выдавать", f"lx:sq:{ref}"), ("🗑 Удалить", f"lx:sd:{ref}")])
    if len(await le_api.all_panels()) > 1:
        rows.append([("🌐 На всех панелях", f"lx:sa:{ref}")])
    rows.append([("⬅️ Источники", f"lx:src:{pid}")])
    await show(c, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("lx:s:"))
async def lx_source(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    await _source_screen(c, pid, sid)


async def _patch(pid, sid, body):
    async with await le_api.for_panel(pid) as le:
        return await le.call("PATCH", f"/sources/{sid}", body)


@router.callback_query(F.data.startswith("lx:st:"))
async def lx_src_toggle(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    try:
        _, src = await _source(pid, sid)
        await _patch(pid, sid, {"enabled": not src.get("enabled")})
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await _source_screen(c, pid, sid)


@router.callback_query(F.data.startswith("lx:sr:"))
async def lx_src_refresh(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    await c.answer("Обновляю…")
    try:
        async with await le_api.for_panel(pid) as le:
            await le.call("POST", "/refresh", {"id": sid}, timeout=180)
    except LeError as e:
        return await say(c, f"⚠️ {h(e)}")
    await _source_screen(c, pid, sid)


# --- ключи источника
async def _keys_screen(c, pid, sid):
    try:
        _, src = await _source(pid, sid)
    except LeError as e:
        return await _err(c, e, f"lx:src:{pid}")
    nodes = src.get("nodes", [])
    rows = [[(f"{'⛔' if n.get('off') else '✅'} {(n.get('name') or n.get('server') or '?')[:40]} · {n.get('protocol', '')}",
              f"lx:kt:{pid}:{sid}:{i}")] for i, n in enumerate(nodes[:60])]
    rows.append([("⬅️ К источнику", f"lx:s:{pid}:{sid}")])
    more = f"\n(показаны первые 60 из {len(nodes)})" if len(nodes) > 60 else ""
    await show(c, f"🔑 <b>Ключи</b> · нажми, чтобы включить/выключить{more}", kb(*rows))


@router.callback_query(F.data.startswith("lx:sk:"))
async def lx_keys(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    await _keys_screen(c, pid, sid)


@router.callback_query(F.data.startswith("lx:kt:"))
async def lx_key_toggle(c: CallbackQuery):
    _, _, pid, rest = c.data.split(":", 3)
    sid, i = rest.rsplit(":", 1)
    try:
        _, src = await _source(pid, sid)
        raw = src["nodes"][int(i)]["raw"]
        off = set(src.get("disabledLinks") or [])
        off.symmetric_difference_update({raw})
        await _patch(pid, sid, {"disabledLinks": sorted(off)})
    except (LeError, IndexError, KeyError) as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await _keys_screen(c, pid, sid)


# --- кому выдавать (сквад)
@router.callback_query(F.data.startswith("lx:sq:"))
async def lx_squad_pick(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    try:
        st, _ = await _source(pid, sid)
    except LeError as e:
        return await _err(c, e, f"lx:src:{pid}")
    rows = [[(f"👥 {q['name']} ({q.get('members', '?')})", f"lx:sqs:{pid}:{sid}:{i + 1}")] for i, q in enumerate(st.get("squads", []))]
    rows.append([("🌐 Всем пользователям", f"lx:sqs:{pid}:{sid}:0")])
    rows.append([("⬅️ Назад", f"lx:s:{pid}:{sid}")])
    await show(c, "👥 <b>Кому выдавать ключи?</b>", kb(*rows))


@router.callback_query(F.data.startswith("lx:sqs:"))
async def lx_squad_set(c: CallbackQuery):
    _, _, pid, rest = c.data.split(":", 3)
    sid, i = rest.rsplit(":", 1)
    try:
        st, _ = await _source(pid, sid)
        scope = "all" if i == "0" else st["squads"][int(i) - 1]["uuid"]
        await _patch(pid, sid, {"scope": scope, "enabled": True})
    except (LeError, IndexError) as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Сохранено")
    await _source_screen(c, pid, sid)


# --- User-Agent
def _ua_choices(st) -> list:
    return [("🤖 Авто (перебор)", "auto")] + [(u["label"], u["id"]) for u in st.get("uaPresets", [])]


@router.callback_query(F.data.startswith("lx:ua:"))
async def lx_ua_pick(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    try:
        st, _ = await _source(pid, sid)
    except LeError as e:
        return await _err(c, e, f"lx:src:{pid}")
    rows = [[(label, f"lx:uas:{pid}:{sid}:{i}")] for i, (label, _v) in enumerate(_ua_choices(st))]
    rows.append([("✏️ Свой…", f"lx:uac:{pid}:{sid}")])
    rows.append([("⬅️ Назад", f"lx:s:{pid}:{sid}")])
    await show(c, "🕵️ <b>User-Agent для загрузки подписки</b>", kb(*rows))


@router.callback_query(F.data.startswith("lx:uas:"))
async def lx_ua_set(c: CallbackQuery):
    _, _, pid, rest = c.data.split(":", 3)
    sid, i = rest.rsplit(":", 1)
    await c.answer("Перезагружаю подписку…")
    try:
        st, _ = await _source(pid, sid)
        await _patch(pid, sid, {"userAgent": _ua_choices(st)[int(i)][1]})
    except (LeError, IndexError) as e:
        return await say(c, f"⚠️ {h(e)}")
    await _source_screen(c, pid, sid)


@form("lx_uac", "🕵️ Свой User-Agent", [Field("ua", "Пришли User-Agent, например <code>Happ/4.13.0/ios/2606221801679</code>:")])
async def lx_uac_done(event, values, ctx):
    try:
        await _patch(ctx["panel"], ctx["sid"], {"userAgent": values["ua"]})
    except LeError as e:
        return await say(event, f"⚠️ {h(e)}")
    await say(event, "✅ Сохранено.", kb([("⬅️ К источнику", f"lx:s:{ctx['panel']}:{ctx['sid']}")]))


@router.callback_query(F.data.startswith("lx:uac:"))
async def lx_uac(c: CallbackQuery, state: FSMContext):
    _, _, pid, sid = c.data.split(":", 3)
    await start_form(c, state, "lx_uac", ctx={"panel": pid, "sid": sid})


# --- удаление
@router.callback_query(F.data.startswith("lx:sd:"))
async def lx_src_del(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    try:
        _, src = await _source(pid, sid)
    except LeError as e:
        return await _err(c, e, f"lx:src:{pid}")
    rows = [[("🗑 Удалить источник", f"lx:sdk:{pid}:{sid}")]]
    if src.get("squadCreated") and src.get("squadUuid"):
        rows.insert(0, [(f"🗑 Удалить + сквад «{src.get('squadName')}»", f"lx:sds:{pid}:{sid}")])
    rows.append([("⬅️ Назад", f"lx:s:{pid}:{sid}")])
    await show(c, f"Удалить «{h(src.get('title') or src.get('squadName') or 'источник')}»?\nЕго хосты удалятся из панели.", kb(*rows))


@router.callback_query(F.data.startswith(("lx:sdk:", "lx:sds:")))
async def lx_src_del_ok(c: CallbackQuery):
    _, kind, pid, sid = c.data.split(":", 3)
    try:
        async with await le_api.for_panel(pid) as le:
            await le.call("DELETE", f"/sources/{sid}" + ("?deleteSquad=1" if kind == "sds" else ""))
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Удалено")
    await show(c, "🗑 Источник удалён.", kb([("📚 Источники", f"lx:src:{pid}")]))


# --- один источник на всех панелях
def _src_key(x: dict) -> str:
    return f"sub:{x.get('input')}" if x.get("kind") == "sub" else "keys:" + "\n".join(sorted(x.get("links") or []))


async def _twins(src: dict):
    out = []
    for p in await le_api.all_panels():
        try:
            async with await le_api.for_panel(p.id) as le:
                st = await le.call("GET", "/state", timeout=20)
            out.append((p, next((x for x in st.get("sources", []) if _src_key(x) == _src_key(src)), None), None))
        except LeError as e:
            out.append((p, None, str(e)))
    return out


@router.callback_query(F.data.startswith("lx:sa:"))
async def lx_all(c: CallbackQuery):
    _, _, pid, sid = c.data.split(":", 3)
    await c.answer("Смотрю все панели…")
    try:
        _, src = await _source(pid, sid)
    except LeError as e:
        return await _err(c, e, f"lx:src:{pid}")
    tw = await _twins(src)
    lines = [f"🌐 <b>{h(src.get('title') or src.get('squadName') or 'Источник')} на всех панелях</b>", ""]
    for p, s, err in tw:
        icon = "⚠️" if err else "➖" if not s else "✅" if s.get("enabled") else "⛔"
        tail = f" · {keys_word(len(s.get('nodes', [])))}" + (f" · {h(s['squadName'])}" if s.get("squadName") else "") if s \
            else (" · недоступна" if err else " · нет")
        lines.append(f"{icon} {h(p.name)}{tail}")
    missing = sum(1 for _, s, err in tw if not s and not err)
    ref = f"{pid}:{sid}"
    rows = [[("✅ Включить везде", f"lx:saa:{ref}:on"), ("⛔ Выключить везде", f"lx:saa:{ref}:off")]]
    if src.get("kind") == "sub":
        rows.append([("🔄 Обновить везде", f"lx:saa:{ref}:refresh")])
    if missing:
        rows.append([(f"➕ Добавить в остальные ({missing})", f"lx:saa:{ref}:copy")])
    rows.append([("🗑 Удалить везде + сквады", f"lx:saa:{ref}:delsq"), ("🗑 Удалить везде", f"lx:saa:{ref}:del")])
    rows.append([("⬅️ К источнику", f"lx:s:{ref}")])
    await show(c, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("lx:saa:"))
async def lx_all_do(c: CallbackQuery):
    _, _, pid, rest = c.data.split(":", 3)
    sid, action = rest.rsplit(":", 1)
    await c.answer("Выполняю на всех панелях…")
    try:
        _, src = await _source(pid, sid)
    except LeError as e:
        return await say(c, f"⚠️ {h(e)}")
    out = []
    for p, s, err in await _twins(src):
        try:
            async with await le_api.for_panel(p.id) as le:
                if action == "copy":
                    if s or err:
                        continue
                    text = src.get("input") if src.get("kind") == "sub" else "\n".join(src.get("links") or [])
                    r = await le.call("POST", "/sources", {"text": text, "title": src.get("title") or src.get("squadName") or "",
                                                           "userAgent": src.get("userAgent") or "auto",
                                                           "scope": "all" if src.get("scope") == "all" else "new"}, timeout=300)
                    out.append(f"➕ {h(p.name)}: " + (", ".join(x.get("squadName") or "всем" for x in r.get("created", [])) or "уже есть"))
                    continue
                if not s:
                    continue
                if action in ("on", "off"):
                    await le.call("PATCH", f"/sources/{s['id']}", {"enabled": action == "on"})
                elif action == "refresh":
                    await le.call("POST", "/refresh", {"id": s["id"]}, timeout=120)
                elif action in ("del", "delsq"):
                    await le.call("DELETE", f"/sources/{s['id']}" + ("?deleteSquad=1" if action == "delsq" else ""))
                out.append(f"✅ {h(p.name)}")
        except LeError as e:
            out.append(f"❌ {h(p.name)}: {h(e)}")
    await say(c, "\n".join(out) or "Нечего делать", kb([("📚 Источники", f"lx:src:{pid}")]))


# ================================================================== добавить ключи
async def _ua_field(values, ctx):
    async with await le_api.for_panel(ctx["panel"]) as le:
        return _ua_choices(await le.state())


@form("lx_add", "📥 Добавить ключи", [
    Field("text", "Пришли ключи (<code>vless://</code>, <code>vmess://</code>, <code>trojan://</code>, <code>ss://</code>, "
                  "<code>hysteria2://</code>, <code>tuic://</code>) и/или ссылки на подписки — по одной на строку. "
                  "Можно прислать .txt-файлом.", "raw"),
    Field("ua", "Каким клиентом скачивать подписки?", "choice", choices=_ua_field),
])
async def lx_add_done(event, values, ctx):
    pid = ctx["panel"]
    msg = await say(event, "⏳ Расшифровываю…")
    try:
        async with await le_api.for_panel(pid) as le:
            r = await le.call("POST", "/preview", {"text": values["text"], "userAgent": values["ua"]}, timeout=180)
    except LeError as e:
        return await msg.edit_text(f"⚠️ {h(e)}")
    lines, total = ["🔍 <b>Что нашёл</b>", ""], 0
    for s in r.get("sources", []):
        nodes = s.get("nodes", [])
        total += len(nodes)
        lines.append(f"{'🔗' if s.get('kind') == 'sub' else '🔑'} <code>{h((s.get('input') or '')[:60])}</code> — "
                     f"{keys_word(len(nodes))}" + (f" (UA: {h(s['usedUa'])})" if s.get("usedUa") else ""))
        lines += [f"   • {h(n.get('name') or n.get('server'))} <i>{h(n.get('protocol'))}</i>" for n in nodes[:12]]
        if len(nodes) > 12:
            lines.append(f"   … ещё {len(nodes) - 12}")
        lines += [f"   ✖️ {h(e.get('error'))}" for e in (s.get("errors") or [])[:3]]
    if not total:
        return await msg.edit_text("\n".join(lines) + "\nКлючей не найдено.", reply_markup=kb([("🔁 Ещё раз", f"lx:add:{pid}")]))
    await db.set_setting(f"lx_add:{pid}", json.dumps({"text": values["text"], "ua": values["ua"]}))
    await msg.edit_text("\n".join(lines)[:4000], reply_markup=kb([("➡️ Далее", f"lx:addok:{pid}")], [("✖️ Отмена", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:add:"))
async def lx_add(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "lx_add", ctx={"panel": int(_p(c.data))})


async def _target_choices(values, ctx):
    n = len(await le_api.all_panels())
    return [("🖥 Только эта панель", "this")] + ([(f"🌐 Во все панели с экстрактором ({n})", "all")] if n > 1 else [])


async def _scope_choices(values, ctx):
    rows = [("🆕 Создать сквад из названия", "new")]
    if values.get("target") != "all":
        async with await le_api.for_panel(ctx["panel"]) as le:
            st = await le.state()
        rows += [(f"👥 {q['name']} ({q.get('members', '?')})", q["uuid"]) for q in st.get("squads", [])]
    return rows + [("🌐 Всем пользователям", "all")]


@form("lx_add2", "📥 Добавить ключи", [
    Field("title", "✏️ <b>Название</b> (из него имя сквада и тег хостов):", optional=True, default=""),
    Field("target", "🖥 В какие панели добавить? На каждой будет свой сквад и свои хосты.", "choice",
          choices=_target_choices),
    Field("scope", "👥 <b>Кому выдавать ключи?</b>\nНовый сквад создаётся с инбаундом-заглушкой — потом добавь в него юзеров.",
          "choice", choices=_scope_choices),
])
async def lx_add2_done(event, values, ctx):
    pid = ctx["panel"]
    raw = await db.setting(f"lx_add:{pid}")
    if not raw:
        return await say(event, "Начни заново: 📥 Добавить ключи")
    flow = json.loads(raw)
    targets = await le_api.all_panels() if values["target"] == "all" else [await db.get(db.Panel, pid)]
    msg = await say(event, f"⏳ Добавляю{f' в {len(targets)} панели' if len(targets) > 1 else ''}: создаю сквады и хосты…")
    lines, any_squad = ["✅ <b>Добавлено</b>", ""], False
    for t in targets:
        if len(targets) > 1:
            lines.append(f"🖥 <b>{h(t.name)}</b>")
        try:
            async with await le_api.for_panel(t.id) as le:
                r = await le.call("POST", "/sources", {"text": flow["text"], "title": values.get("title") or "",
                                                       "userAgent": flow["ua"], "scope": values["scope"]}, timeout=300)
        except LeError as e:
            lines.append(f"  ❌ {h(e)}")
            continue
        for cr in r.get("created", []):
            any_squad |= bool(cr.get("squadCreated"))
            who = f" · сквад <b>{h(cr['squadName'])}</b>" if cr.get("squadName") else (" · всем" if cr.get("scope") == "all" else "")
            lines.append(f"  • {h(cr.get('title') or cr.get('squadName') or 'источник')}: {keys_word(len(cr.get('links') or []))}{who}")
            lines += [f"    ⚠️ {h(x)}" for x in (cr.get("error"), cr.get("squadError"), cr.get("hostError")) if x]
        if not r.get("created"):
            lines.append("  Ничего нового (такая подписка уже добавлена?)")
    if any_squad:
        lines += ["", "👉 Добавь юзеров в новые сквады: панель → Внутренние сквады."]
    await db.set_setting(f"lx_add:{pid}", None)
    await msg.edit_text("\n".join(lines)[:4000], reply_markup=kb([("📚 Источники", f"lx:src:{pid}"), ("⬅️ Экстрактор", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:addok:"))
async def lx_add2(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "lx_add2", ctx={"panel": int(_p(c.data))})


# ================================================================== группы стран
@router.callback_query(F.data.startswith("lx:grp:"))
async def lx_groups(c: CallbackQuery):
    pid = _p(c.data)
    try:
        async with await le_api.for_panel(pid) as le:
            st = await le.state()
    except LeError as e:
        return await _err(c, e, f"lx:v:{pid}")
    on = st.get("settings", {}).get("groupsEnabled")
    lines = [f"🌍 <b>Группы стран</b> · {'включены' if on else 'выключены'}", ""]
    if on:
        lines += ["Одна точка входа на страну с автовыбором по пингу + общий автовыбор. Ключи — скрытые хосты.", ""]
        for g in st.get("groups") or []:
            lines.append(f"{'' if g.get('code') == 'ALL' else '• '}{h(g.get('name'))} — {keys_word(g.get('count', 0))}"
                         + ("" if g.get("inPanel") else " ⏳"))
    else:
        lines.append("Каждый ключ — отдельный видимый хост.")
    await show(c, "\n".join(lines), kb([("⛔ Выключить группы" if on else "✅ Включить группы", f"lx:grpt:{pid}")],
                                       [("✏️ Название «все страны»", f"lx:grpn:{pid}")], [("⬅️ Назад", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:grpt:"))
async def lx_groups_toggle(c: CallbackQuery):
    pid = _p(c.data)
    await c.answer("Применяю… это может занять до минуты")
    try:
        async with await le_api.for_panel(pid) as le:
            st = await le.state()
            await le.call("POST", "/settings", {"groupsEnabled": not st["settings"].get("groupsEnabled")}, timeout=180)
    except LeError as e:
        return await say(c, f"⚠️ {h(e)}")
    await lx_groups(c)


async def _settings_post(pid, body: dict, timeout: float = 60):
    async with await le_api.for_panel(pid) as le:
        return await le.call("POST", "/settings", body, timeout=timeout)


def _setting_form(name: str, title: str, prompt: str, key: str, kind: str = "text", lo: int = 0, hi: int = 0,
                  back: str = "set", secret: bool = False):
    @form(name, title, [Field("v", prompt, "int" if kind == "int" else "text", secret=secret)])
    async def done(event, values, ctx):
        v = values["v"]
        if kind == "int" and not lo <= v <= hi:
            return await say(event, f"Число от {lo} до {hi}.", kb([("🔁 Ещё раз", f"lx:f:{name}:{ctx['panel']}")]))
        try:
            await _settings_post(ctx["panel"], {key: v})
        except LeError as e:
            return await say(event, f"⚠️ {h(e)}")
        await say(event, "✅ Сохранено.", kb([("⬅️ Назад", f"lx:{back}:{ctx['panel']}")]))
    return done


_setting_form("lx_grpn", "✏️ Общий автовыбор", "Название, например <code>⚡ Авто · все страны</code>:", "allGroupName", back="grp")
_setting_form("lx_refresh", "🔄 Интервал", "Как часто обновлять подписки (в минутах, 1–1440)?", "refreshMinutes", "int", 1, 1440)
_setting_form("lx_bkh", "💾 Автобэкап", "Как часто делать автобэкап экстрактора (в часах, 1–720)?", "backupHours", "int", 1, 720)
_setting_form("lx_domain", "🌐 Домен панели", "Домен панели Remnawave (для пункта «Экстрактор ссылок» в меню панели):", "panelDomain")
_setting_form("lx_rwtok", "🔑 Токен Remnawave", "API-токен Remnawave (сообщение удалю):", "remnawaveToken", secret=True)


@router.callback_query(F.data.startswith("lx:f:"))
async def lx_form(c: CallbackQuery, state: FSMContext):
    _, _, name, pid = c.data.split(":")
    await start_form(c, state, name, ctx={"panel": int(pid)})


@router.callback_query(F.data.startswith("lx:grpn:"))
async def lx_groups_rename(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "lx_grpn", ctx={"panel": int(_p(c.data))})


# ================================================================== проверка подписки
@form("lx_chk", "🔎 Проверить подписку", [
    Field("target", "Пришли ссылку подписки юзера (или его shortUuid):"),
    Field("ua", "📱 Как какой клиент проверить?", "choice", choices=_ua_field),
])
async def lx_chk_done(event, values, ctx):
    pid = ctx["panel"]
    try:
        async with await le_api.for_panel(pid) as le:
            r = await le.call("POST", "/debug", {"target": values["target"], "userAgent": values["ua"]}, timeout=120)
    except LeError as e:
        return await say(event, f"⚠️ {h(e)}", kb([("⬅️ Назад", f"lx:v:{pid}")]))
    rw = r.get("remnawave") or {}
    L = ["🔎 <b>Проверка подписки</b>", ""]
    L.append(f"Юзер: ошибка API — {h(rw['error'])}" if rw.get("error") else
             f"👤 {h(rw.get('username'))} · {h(rw.get('status'))}\n👥 Сквады: {h(', '.join(rw.get('squads') or []) or '—')}")
    if r.get("upstreamError"):
        L.append(f"Remnawave не ответил: {h(r['upstreamError'])}")
    if r.get("upstream"):
        L.append(f"📦 Ответ: HTTP {r['upstream'].get('status')}, {h(r['upstream'].get('contentType') or '?')}")
    res = r.get("result")
    if res:
        if res.get("groups") or res.get("hostsReplaced") or res.get("hostsDropped"):
            L.append(f"🌍 Автовыборов: {res.get('groups') or 0} · ключей подменено: {res.get('hostsReplaced') or 0} · "
                     f"убрано: {res.get('hostsDropped') or 0}")
        L.append(f"Формат: {h(res.get('format'))} · добавлено: {res.get('added')}")
        L.append("✅ Ключи выдаются" if res.get("added") else f"❌ Не выдаются: {h(res.get('reason') or '')}")
    await say(event, "\n".join(L), kb([("🔎 Ещё раз", f"lx:chk:{pid}"), ("⬅️ Назад", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:chk:"))
async def lx_chk(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "lx_chk", ctx={"panel": int(_p(c.data))})


# ================================================================== бэкапы экстрактора
@router.callback_query(F.data.startswith("lx:bk:"))
async def lx_backups(c: CallbackQuery):
    pid = _p(c.data)
    try:
        async with await le_api.for_panel(pid) as le:
            r = await le.call("GET", "/backups")
    except LeError as e:
        return await _err(c, e, f"lx:v:{pid}")
    bks = r.get("backups", [])[:10]
    rows = [[("💾 Сделать бэкап сейчас", f"lx:bkm:{pid}")]]
    rows += [[(f"💾 {b['name'].removesuffix('.json')} · {max(1, round(b.get('size', 0) / 1024))} КБ", f"lx:bkg:{pid}:{i}")]
             for i, b in enumerate(bks)]
    rows += [[("♻️ Восстановить из файла", f"lx:bkr:{pid}")], [("⬅️ Назад", f"lx:v:{pid}")]]
    await show(c, "💾 <b>Бэкапы экстрактора</b>\n\nИсточники, ключи, связи со сквадами и хостами, настройки. "
                  "Автоматически по расписанию (⚙️ Настройки) с отправкой сюда.\n\n"
                  + ("Последние:" if bks else "Бэкапов пока нет."), kb(*rows))


async def _send_backup(c: CallbackQuery, pid, name: str, caption: str):
    async with await le_api.for_panel(pid) as le:
        data = await le.call("GET", f"/backups/{name}", raw=True, timeout=300)
    await c.message.answer_document(BufferedInputFile(data, filename=name), caption=caption)


@router.callback_query(F.data.startswith("lx:bkm:"))
async def lx_backup_make(c: CallbackQuery):
    pid = _p(c.data)
    p = await db.get(db.Panel, pid)
    await c.answer("Делаю бэкап…")
    try:
        async with await le_api.for_panel(pid) as le:
            b = await le.call("POST", "/backups", {"sendTelegram": False})
        await _send_backup(c, pid, b["name"], f"💾 Бэкап экстрактора · <b>{h(p.name)}</b>")
    except LeError as e:
        await say(c, f"⚠️ {h(e)}")


@router.callback_query(F.data.startswith("lx:bkg:"))
async def lx_backup_get(c: CallbackQuery):
    _, _, pid, i = c.data.split(":")
    await c.answer("Отправляю…")
    try:
        async with await le_api.for_panel(pid) as le:
            name = (await le.call("GET", "/backups"))["backups"][int(i)]["name"]
        await _send_backup(c, pid, name, h(name))
    except (LeError, IndexError) as e:
        await say(c, f"⚠️ {h(e)}")


@form("lx_restore", "♻️ Восстановить экстрактор", [
    Field("file", "Пришли файл бэкапа экстрактора (<code>store-….json</code>). "
                  "Текущие данные перед восстановлением сохранятся отдельным бэкапом.", "raw")])
async def lx_restore_done(event, values, ctx):
    pid = ctx["panel"]
    try:
        j = json.loads(values["file"])
    except ValueError:
        return await say(event, "❌ Это не JSON.")
    data = j.get("data") if j.get("app") == "link-extractor" else j
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        return await say(event, "❌ Это не бэкап Link Extractor.")
    await db.set_setting(f"lx_restore:{pid}", values["file"])
    p = await db.get(db.Panel, pid)
    await say(event, f"♻️ В бэкапе {len(data['sources'])} источников"
                     + (f" от {h(j['createdAt'][:16])}" if j.get("createdAt") else "")
                     + f".\nВосстановить в «{h(p.name)}»? Текущие данные заменятся.",
              kb([("♻️ Восстановить", f"lx:bky:{pid}")], [("✖️ Отмена", f"lx:bk:{pid}")]))


@router.callback_query(F.data.startswith("lx:bkr:"))
async def lx_restore(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "lx_restore", ctx={"panel": int(_p(c.data))})


@router.callback_query(F.data.startswith("lx:bky:"))
async def lx_restore_yes(c: CallbackQuery):
    pid = _p(c.data)
    raw = await db.setting(f"lx_restore:{pid}")
    if not raw:
        return await c.answer("Пришли файл заново", show_alert=True)
    await c.answer("Восстанавливаю…")
    try:
        async with await le_api.for_panel(pid) as le:
            r = await le.call("POST", "/restore", {"backup": json.loads(raw)}, timeout=300)
    except LeError as e:
        return await say(c, f"⚠️ {h(e)}")
    await db.set_setting(f"lx_restore:{pid}", None)
    await say(c, f"✅ Восстановлено: источников {r.get('sources')}. Хосты и группы синхронизированы.",
              kb([("⬅️ Экстрактор", f"lx:v:{pid}")]))


# ================================================================== настройки
@router.callback_query(F.data.startswith("lx:set:"))
async def lx_settings(c: CallbackQuery):
    pid = _p(c.data)
    try:
        async with await le_api.for_panel(pid) as le:
            st = await le.state()
    except LeError as e:
        return await _err(c, e, f"lx:v:{pid}")
    s = st.get("settings", {})
    stub = s.get("stubInbound")
    n = st.get("notify") or {}
    notify = (f"{'личка' if str(n.get('chatId', '')).lstrip('-').isdigit() and int(n['chatId']) > 0 else 'чат'} "
              f"<code>{h(n['chatId'])}</code>" + (f", топик {h(n['threadId'])}" if n.get("threadId") else "")) \
        if n.get("chatId") else "не настроены"
    text = "\n".join(["⚙️ <b>Настройки экстрактора</b>", "",
                      f"🔄 Обновление подписок: каждые <b>{s.get('refreshMinutes')}</b> мин",
                      f"🧩 Инбаунд-заглушка: {h((stub.get('profileName') or '') + ' → ' + stub.get('tag', '')) if stub else '—'}",
                      f"🔑 Токен Remnawave: {'⚠️ ' + h(st['squadsError']) if st.get('squadsError') else 'работает'}",
                      f"🔔 Уведомления: {notify}",
                      f"🌐 Домен панели: {h(s.get('panelDomain') or '—')}"])
    au, rau = s.get("autoUpdate") is not False, bool(s.get("remnawaveAutoUpdate"))
    await show(c, text, kb(
        [("🔄 Интервал обновления", f"lx:f:lx_refresh:{pid}"), ("🧩 Заглушка", f"lx:stub:{pid}")],
        [("🔑 Токен из панели", f"lx:rwt:{pid}"), ("✏️ Токен вручную", f"lx:f:lx_rwtok:{pid}")],
        [("🔔 Слать мне", f"lx:ntf:{pid}:me"), ("↩️ Как в .env", f"lx:ntf:{pid}:reset"), ("🔔 Тест", f"lx:test:{pid}")],
        [("🌐 Домен панели", f"lx:f:lx_domain:{pid}"), ("🔐 Пароль страницы", f"lx:pass:{pid}")],
        [(("✅" if au else "⬜") + " Автообновление экстрактора", f"lx:tg:{pid}:autoUpdate")],
        [(("✅" if rau else "⬜") + " Автообновление Remnawave", f"lx:tg:{pid}:remnawaveAutoUpdate")],
        [(f"💾 Бэкап каждые {s.get('backupHours') or 24} ч", f"lx:f:lx_bkh:{pid}")],
        [("⬅️ Назад", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:stub:"))
async def lx_stub(c: CallbackQuery):
    pid = _p(c.data)
    try:
        async with await le_api.for_panel(pid) as le:
            st = await le.state()
    except LeError as e:
        return await _err(c, e, f"lx:set:{pid}")
    cur = (st.get("settings", {}).get("stubInbound") or {}).get("uuid")
    rows = [[(f"{'✅ ' if i.get('uuid') == cur else ''}{i.get('profileName')} → {i.get('tag')}"
              + (f" ⚠️ нод: {i['profileNodes']}" if i.get("profileNodes") else ""), f"lx:stubs:{pid}:{n}")]
            for n, i in enumerate(st.get("inbounds", []))]
    rows.append([("⬅️ Назад", f"lx:set:{pid}")])
    await show(c, "🧩 <b>Инбаунд-заглушка</b>\nБери профиль без нод (⚠️ — профиль стоит на нодах).", kb(*rows))


@router.callback_query(F.data.startswith("lx:stubs:"))
async def lx_stub_set(c: CallbackQuery):
    _, _, pid, n = c.data.split(":")
    try:
        async with await le_api.for_panel(pid) as le:
            st = await le.state()
            await le.call("POST", "/settings", {"stubInbound": st["inbounds"][int(n)]["uuid"]})
    except (LeError, IndexError) as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Сохранено")
    await lx_settings(c)


@router.callback_query(F.data.startswith("lx:rwt:"))
async def lx_rw_token(c: CallbackQuery):
    pid = _p(c.data)
    p = await db.get(db.Panel, pid)
    try:
        tok = await ensure_api_token(p)
        await _settings_post(pid, {"remnawaveToken": tok})
    except (RwError, LeError) as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("✅ Токен передан. Синхронизирую сквады, хосты и группы…", show_alert=True)


@router.callback_query(F.data.startswith("lx:ntf:"))
async def lx_notify(c: CallbackQuery):
    _, _, pid, mode = c.data.split(":")
    body = {"notifyChatId": str(config.OWNER_ID), "notifyThreadId": ""} if mode == "me" else {"notifyChatId": ""}
    try:
        await _settings_post(pid, body)
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Сохранено")
    await lx_settings(c)


@router.callback_query(F.data.startswith("lx:test:"))
async def lx_test(c: CallbackQuery):
    try:
        async with await le_api.for_panel(_p(c.data)) as le:
            await le.call("POST", "/notify-test")
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Отправлено")


@router.callback_query(F.data.startswith("lx:tg:"))
async def lx_toggle(c: CallbackQuery):
    _, _, pid, key = c.data.split(":")
    try:
        async with await le_api.for_panel(pid) as le:
            s = (await le.state()).get("settings", {})
            cur = s.get("autoUpdate") is not False if key == "autoUpdate" else bool(s.get(key))
            await le.call("POST", "/settings", {key: not cur})
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Сохранено" + (" — панель будет обновляться и перезапускаться сама" if key == "remnawaveAutoUpdate" and not cur else ""),
                   show_alert=key == "remnawaveAutoUpdate" and not cur)
    await lx_settings(c)


@router.callback_query(F.data.startswith("lx:pass:"))
async def lx_pass(c: CallbackQuery):
    pid = _p(c.data)
    p = await db.get(db.Panel, pid)
    try:
        async with await le_api.for_panel(pid) as le:
            sec = await le.call("GET", "/secrets")
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    url = le_api.conf_of(await le_api.mod_for(p.id), p).get("url", "")
    await show(c, f"🔐 <b>Пароль страницы экстрактора</b>\n\n<tg-spoiler>{h(sec.get('adminPassword'))}</tg-spoiler>\n\n"
                  f"Страница: <code>{h(url)}/</code>",
               kb([("🔄 Сменить пароль", f"lx:passn:{pid}")], [("⬅️ Назад", f"lx:set:{pid}")]))


@router.callback_query(F.data.startswith("lx:passn:"))
async def lx_pass_new(c: CallbackQuery):
    pid = _p(c.data)
    try:
        await _settings_post(pid, {"regeneratePassword": True})
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await show(c, "✅ Пароль сменён.", kb([("🔐 Показать", f"lx:pass:{pid}")], [("⬅️ Назад", f"lx:set:{pid}")]))


# ================================================================== обновления
@router.callback_query(F.data.startswith("lx:ref:"))
async def lx_refresh_all(c: CallbackQuery):
    pid = _p(c.data)
    await c.answer("Обновляю все подписки…")
    try:
        async with await le_api.for_panel(pid) as le:
            await le.call("POST", "/refresh", {}, timeout=180)
    except LeError as e:
        return await say(c, f"⚠️ {h(e)}")
    await lx_main(c)


@router.callback_query(F.data.startswith("lx:upd:"))
async def lx_update(c: CallbackQuery):
    try:
        async with await le_api.for_panel(_p(c.data)) as le:
            r = await le.call("POST", "/update")
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer(r.get("note") or "Запрошено", show_alert=True)


@router.callback_query(F.data.startswith("lx:updrw:"))
async def lx_update_rw(c: CallbackQuery):
    pid = _p(c.data)
    await show(c, "⬆️ Обновить панель Remnawave?\nБудут скачаны новые образы и панель перезапустится (перерыв ~10–30 секунд).",
               kb([("⬆️ Да, обновить", f"lx:updrwy:{pid}")], [("⬅️ Назад", f"lx:v:{pid}")]))


@router.callback_query(F.data.startswith("lx:updrwy:"))
async def lx_update_rw_yes(c: CallbackQuery):
    pid = _p(c.data)
    try:
        async with await le_api.for_panel(pid) as le:
            r = await le.call("POST", "/update-remnawave")
    except LeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer(r.get("note") or "Запрошено", show_alert=True)
    await lx_main(c)


async def backup_all() -> dict[str, bytes]:
    """Для бэкапа vpn-master: свежий бэкап каждого экстрактора."""
    out = {}
    for p in await le_api.all_panels():
        try:
            async with await le_api.for_panel(p.id) as le:
                b = await le.call("POST", "/backups", {"sendTelegram": False})
                out[f"{p.id}-{b['name']}"] = await le.call("GET", f"/backups/{b['name']}", raw=True, timeout=300)
        except (LeError, KeyError):
            continue
    return out

