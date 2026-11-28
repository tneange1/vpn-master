"""🚚 Умный переезд бота на другой сервер.

1. Свежий бэкап (БД + master.key + бэкапы экстракторов) — копия уходит владельцу.
2. Новый сервер: Docker, код, .env (BOT_TOKEN, OWNER_ID), сборка, PostgreSQL.
3. Бэкап по SFTP → восстановление базы и ключа на новом сервере.
4. Переключение: этот бот перестаёт брать обновления → запускается новый → проверка «Start polling».
   Новый не поднялся — останавливаем его, этот бот снова работает (откат).
5. Успех: этот экземпляр засыпает навсегда (флаг migrated.flag), сайдкар updater останавливает его контейнеры.
"""
import asyncio
import logging

from aiogram import Bot
from aiogram.types import FSInputFile

from . import backup, config, db, runtime, scripts, ssh
from .runner import run_task
from .ui import h, post

log = logging.getLogger("migrate")


async def migrate(bot: Bot, chat: int, server: db.Server) -> None:
    if runtime.busy:
        await post(bot, chat, "⏳ Переезд уже идёт.")
        return
    runtime.busy = True
    try:
        await _migrate(bot, chat, server)
    except Exception as e:  # noqa: BLE001
        log.exception("migrate")
        await bot.send_message(chat, f"❌ Переезд прерван: <code>{h(type(e).__name__)}: {h(e)}</code>\n"
                                     "Этот бот продолжает работать.")
        if runtime.mode == "switching":
            runtime.clear_migrated()
            runtime.mode = "rollback"
            runtime.switch_done.set()
    finally:
        runtime.busy = False


async def _migrate(bot: Bot, chat: int, server: db.Server) -> None:
    # 1. бэкап
    from .handlers.linkext import backup_all
    await post(bot, chat, "💾 Делаю свежий бэкап бота…")
    path = await backup.make(await backup_all())
    await bot.send_document(chat, FSInputFile(path),
                            caption="💾 Бэкап перед переездом (на всякий случай). Внутри ключ шифрования — не пересылай.")
    # 2. подготовка нового сервера
    branch = await db.setting("autoupdate_branch", "main") or "main"
    r = await run_task(bot, chat, server, f"Переезд: подготовка {server.host}",
                       scripts.migrate_prepare(config.BOT_TOKEN, config.OWNER_ID, branch), timeout=3600)
    if not r.ok:
        await bot.send_message(chat, "❌ Новый сервер не подготовился — лог выше. Этот бот работает как раньше.")
        return
    # 3. бэкап → новый сервер → восстановление
    await ssh.upload(server, str(path), scripts.MIGRATE_UPLOAD)
    r = await run_task(bot, chat, server, "Переезд: восстановление базы", scripts.MIGRATE_RESTORE)
    if not (r.ok and r.marks.get("RESTORED")):
        await bot.send_message(chat, "❌ База не восстановилась — лог выше. Этот бот работает как раньше.")
        return
    # 4. переключение (два бота с одним токеном конфликтуют — сначала гасим polling здесь)
    await post(bot, chat, f"🔀 Переключаюсь на <code>{h(server.host)}</code>: этот бот перестаёт принимать команды, "
                          "запускаю новый. Минутку…")
    runtime.mode = "switching"
    runtime.mark_migrated(server.host)
    await runtime.dp.stop_polling()
    await asyncio.sleep(2)
    r = await run_task(bot, chat, server, "Переезд: запуск нового бота", scripts.MIGRATE_START)
    if not (r.ok and r.marks.get("STARTED")):
        await run_task(bot, chat, server, "Переезд: откат — останавливаю новый", scripts.MIGRATE_STOP)
        runtime.clear_migrated()
        runtime.mode = "rollback"
        runtime.switch_done.set()
        await bot.send_message(chat, "❌ Новый бот не запустился — откатился, <b>этот бот снова работает</b>. Лог выше.")
        return
    # 5. успех
    runtime.mode = "migrated"
    runtime.request_shutdown()
    runtime.switch_done.set()
    await bot.send_message(chat, f"✅ <b>Бот переехал на {h(server.host)}</b> со всей базой и ключами.\n"
                                 "Новый экземпляр уже отвечает — открой /menu.\n\n"
                                 "Старый экземпляр уснул и не будет мешать (даже после перезагрузки); его контейнеры "
                                 "останавливаются. Когда убедишься, что всё ок, на старом сервере можно удалить "
                                 "<code>/opt/vpn-master</code> (<code>docker compose down -v</code> в этой папке).")
