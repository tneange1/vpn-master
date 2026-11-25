"""Главное меню, помощь, бэкап бота, кнопки терминала."""
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import backup, config, db, selfupdate, terminal
from ..ui import h, kb, say, show

router = Router(name="common")


async def main_text() -> str:
    return ("🛰 <b>VPN Master</b>\n\n"
            f"Серверов: {await db.count(db.Server)} · Панелей: {await db.count(db.Panel)} · "
            f"Нод: {await db.count(db.Node)} · Ботов: {await db.count(db.Bedolaga)}")


MAIN_KB = kb([("📦 Установка проектов", "inst")],
             [("🛠 Управление", "mng")],
             [("🔐 Доступы и серверы", "acc"), ("🔑 SSH-ключи", "key:list")],
             [("⚙️ Бот", "bot"), ("❓ Помощь", "help")])


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await main_text(), reply_markup=MAIN_KB)


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, await main_text(), MAIN_KB)


@router.callback_query(F.data == "help")
async def help_(c: CallbackQuery):
    await show(c, "❓ <b>Как пользоваться</b>\n\n"
                  "<b>📦 Установка</b> — Remnawave (панель + подписка + Caddy), Bedolaga Bot, Bedolaga Cabinet. "
                  "В первом шаге выбираешь сервер и способ входа: пароль или SSH-ключ.\n\n"
                  "<b>🛠 Управление</b>\n"
                  "• Панели: ноды (self-steal Caddy + remnanode), профили Reality/Hysteria2, сквады\n"
                  "• Модификации: Link Extractor, Backup-Restore (на панель, свой бот), WARP (на ноду)\n"
                  "• Безопасность: UFW, вход по паролю вкл/выкл, ключи, секретная ссылка панели\n"
                  "• Обновления: всё по отдельности\n\n"
                  "<b>🔐 Доступы</b> — серверы, пароли, токены. <b>🔑 SSH-ключи</b> — загрузка из файла, "
                  "раздача по серверам. Всё в PostgreSQL, секреты зашифрованы.\n\n"
                  "<b>⚙️ Бот</b> — бэкап бота и автообновление из git.\n\n"
                  "<b>💻 Терминал</b> — живая SSH-сессия на сервере прямо в чате: пишешь команду сообщением, "
                  "бот отправляет её на сервер и показывает экран. Нужен для установщиков с вопросами "
                  "(backup-restore, WARP) и ручной работы. Открыть: /term или Доступы → сервер → 💻 Терминал.\n\n"
                  "Команды: /menu — меню, /cancel — отменить ввод, /term — открыть/закрыть терминал.",
               kb([("⬅️ Назад", "menu")]))


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=MAIN_KB)


# ------------------------------------------------------------------ бэкап бота
class Restore(StatesGroup):
    waiting = State()
    confirm = State()


BACKUP_HOURS = ["0", "6", "12", "24", "72"]


@router.callback_query(F.data == "bot")
async def bot_menu(c: CallbackQuery):
    st = selfupdate.status()
    ver = f"<code>{h(st.get('commit', '?'))}</code> {h(st.get('subject', ''))}" if st else "updater не запущен"
    await show(c, f"⚙️ <b>Бот</b>\nВерсия: {ver}",
               kb([("💾 Бэкап бота", "bk"), ("🔄 Обновление", "su")], [("⬅️ Меню", "menu")]))


RESULT = {"idle": "ещё не проверял", "uptodate": "✅ последняя версия", "available": "🆕 есть обновление",
          "updating": "⏳ обновляется…", "updated": "✅ обновлён", "failed": "❌ сборка упала, откатился",
          "error": "⚠️ ошибка"}


@router.callback_query(F.data == "su")
async def su_menu(c: CallbackQuery):
    st = selfupdate.status()
    minutes = int(await db.setting("autoupdate_interval", "60") or 0)
    if not st:
        text = ("🔄 <b>Автообновление бота</b>\n\n⚠️ Контейнер <code>vpn-master-updater</code> не отвечает.\n"
                "Обнови один раз вручную на сервере, дальше бот будет обновляться сам:\n"
                "<code>cd /opt/vpn-master && git pull && docker compose up -d --build</code>")
    else:
        checked = st.get("checked")
        when = datetime.fromtimestamp(float(checked), timezone.utc).strftime("%d.%m %H:%M UTC") if checked else "—"
        msg = st.get("message", "")
        if st.get("result") == "available":
            msg = "\n" + "\n".join("• " + x for x in msg.split(";") if x)
        text = (f"🔄 <b>Автообновление бота</b> (git → пересборка)\n\n"
                f"Версия: <code>{h(st.get('commit'))}</code> {h(st.get('subject', ''))}\n"
                f"Проверка: {h(when)} — {RESULT.get(st.get('result'), h(st.get('result')))} {h(msg)}\n"
                f"Ветка: <code>{h(await db.setting('autoupdate_branch', 'main'))}</code>")
    rows = [[("🔍 Проверить", "su:check"), ("⬆️ Обновить сейчас", "su:now")],
            [(("✅ " if minutes == m else "") + label, f"su:i:{m}") for label, m in selfupdate.INTERVALS],
            [("📜 Лог", "su:log"), ("⬅️ Назад", "bot")]]
    await show(c, text + f"\n\nАвтоматически: {'выкл' if not minutes else f'каждые {minutes} мин'}", kb(*rows))


@router.callback_query(F.data.startswith("su:i:"))
async def su_interval(c: CallbackQuery):
    await db.set_setting("autoupdate_interval", c.data.split(":")[2])
    await selfupdate.sync_files()
    await su_menu(c)


@router.callback_query(F.data.in_({"su:check", "su:now"}))
async def su_go(c: CallbackQuery):
    selfupdate.request("check" if c.data == "su:check" else "update")
    await c.answer("Запрос отправлен updater-у — через ~20 сек обнови экран" +
                   (". Если есть новое, бот перезапустится и напишет." if c.data == "su:now" else ""), show_alert=True)


@router.callback_query(F.data == "su:log")
async def su_log(c: CallbackQuery):
    await say(c, f"<pre>{h(selfupdate.log_tail()[-3500:]) or 'лог пуст'}</pre>", kb([("⬅️ Назад", "su")]))


async def notify_if_updated(bot: Bot) -> None:
    await selfupdate.sync_files()
    st = selfupdate.status()
    commit = st.get("commit")
    last = await db.setting("last_commit")
    if commit and commit != last:
        await db.set_setting("last_commit", commit)
        if last:
            try:
                await bot.send_message(config.OWNER_ID, f"🔄 Бот обновлён: <code>{h(last)}</code> → "
                                                        f"<code>{h(commit)}</code>\n{h(st.get('subject', ''))}")
            except Exception:  # noqa: BLE001
                pass


@router.callback_query(F.data == "bk")
async def bk_menu(c: CallbackQuery):
    hours = await db.setting("backup_hours", "24")
    last = await db.setting("backup_last")
    await show(c, "💾 <b>Бэкап бота</b>\n\n"
                  "Архив: полный дамп PostgreSQL (серверы, ключи, панели, настройки) + master.key.\n"
                  "⚠️ В архиве ключ расшифровки — храни его как пароли.\n\n"
                  f"Автобэкап: {'выкл' if hours == '0' else f'каждые {hours} ч'}\n"
                  f"Последний: {h(last or '—')}",
               kb([("💾 Сделать сейчас", "bk:now"), ("♻️ Восстановить", "bk:res")],
                  [(("✅ " if hours == x else "") + ("выкл" if x == "0" else f"{x}ч"), f"bk:h:{x}") for x in BACKUP_HOURS],
                  [("⬅️ Назад", "bot")]))


async def send_backup(bot: Bot) -> None:
    path = await backup.make()
    await bot.send_document(config.OWNER_ID, FSInputFile(path),
                            caption=f"💾 Бэкап vpn-master · {path.stat().st_size // 1024} КБ\n"
                                    "Внутри ключ шифрования — не пересылай.")
    await db.set_setting("backup_last", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))


@router.callback_query(F.data == "bk:now")
async def bk_now(c: CallbackQuery, bot: Bot):
    await c.answer("Делаю бэкап…")
    try:
        await send_backup(bot)
    except Exception as e:  # noqa: BLE001
        await say(c, f"❌ {h(e)}")


@router.callback_query(F.data.startswith("bk:h:"))
async def bk_hours(c: CallbackQuery):
    await db.set_setting("backup_hours", c.data.split(":")[2])
    await bk_menu(c)


@router.callback_query(F.data == "bk:res")
async def bk_restore(c: CallbackQuery, state: FSMContext):
    await state.set_state(Restore.waiting)
    await show(c, "♻️ Пришли файл бэкапа <code>vpn-master-*.tar.gz</code> одним сообщением.",
               kb([("✖️ Отмена", "f:x")]))


@router.message(Restore.waiting, F.document)
async def bk_got_file(m: Message, state: FSMContext, bot: Bot):
    path = config.DATA_DIR / "restore-upload.tar.gz"
    await bot.download(m.document, destination=path)
    await state.set_state(Restore.confirm)
    await m.answer("⚠️ Текущие данные бота будут <b>заменены</b> данными из бэкапа. Продолжить?",
                   reply_markup=kb([("♻️ Да, восстановить", "bk:resok"), ("✖️ Отмена", "f:x")]))


@router.callback_query(Restore.confirm, F.data == "bk:resok")
async def bk_restore_ok(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer("Восстанавливаю…")
    try:
        await backup.restore(config.DATA_DIR / "restore-upload.tar.gz")
    except Exception as e:  # noqa: BLE001
        return await say(c, f"❌ Не вышло: <code>{h(e)}</code>")
    finally:
        (config.DATA_DIR / "restore-upload.tar.gz").unlink(missing_ok=True)
    await say(c, "✅ Бэкап восстановлен.", MAIN_KB)


async def autobackup_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(600)
        try:
            hours = int(await db.setting("backup_hours", "24") or 0)
            if hours <= 0:
                continue
            last = await db.setting("backup_ts")
            now = datetime.now(timezone.utc).timestamp()
            if last and now - float(last) < hours * 3600:
                continue
            await db.set_setting("backup_ts", str(now))
            await send_backup(bot)
        except Exception as e:  # noqa: BLE001
            try:
                await bot.send_message(config.OWNER_ID, f"⚠️ Автобэкап не удался: <code>{h(e)}</code>")
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ терминал
KEYS = {"enter": "\r", "ctrlc": "\x03", "up": "\x1b[A", "down": "\x1b[B"}


@router.callback_query(F.data.startswith("term:"))
async def term_key(c: CallbackQuery):
    parts = c.data.split(":")
    if parts[1] == "close":
        await terminal.stop(c.from_user.id)
        return await c.answer("Терминал закрыт")
    data = parts[2] if parts[1] == "k" else KEYS.get(parts[1], "")
    ok = terminal.send(c.from_user.id, data)
    await c.answer("" if ok else "Терминал уже закрыт")


@router.message(Command("term"))
async def term_cmd(m: Message):
    if terminal.active(m.from_user.id):
        await terminal.stop(m.from_user.id)
        return await m.answer("💻 Терминал закрыт.")
    servers = await db.all_(db.Server)
    if not servers:
        return await m.answer("💻 Терминал — SSH-сессия на сервере прямо в чате. Сначала добавь сервер.",
                              reply_markup=kb([("➕ Добавить сервер", "srv:add")]))
    await m.answer("💻 <b>Терминал</b>\nЖивая SSH-сессия на сервере прямо в чате: пишешь команды сообщениями, "
                   "бот показывает экран. Кнопки снизу — Enter, Ctrl+C, стрелки.\nНа какой сервер?",
                   reply_markup=kb(*[[(f"💻 {s.name} · {s.host}", f"srv:term:{s.id}")] for s in servers]))


@router.message(F.text, lambda m: terminal.active(m.from_user.id))
async def term_input(m: Message):
    terminal.send(m.from_user.id, m.text + "\r")
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
