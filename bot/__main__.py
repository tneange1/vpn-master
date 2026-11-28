"""Точка входа: python -m bot"""
import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import config, crypto, db, forms, runtime
from .ui import CTX, SCREENS
from .handlers import bedolaga, common, install, keys, linkext, mods, panels, security, servers, updates


class LiveMessage(BaseMiddleware):
    """Для нажатой кнопки: прогресс/результат пишем в это же сообщение, «Назад» — на прежний экран."""

    async def __call__(self, handler, event, data):
        msg = getattr(event, "message", None)
        if msg is None:
            return await handler(event, data)
        token = CTX.set({"chat": msg.chat.id, "mid": msg.message_id,
                         "back": SCREENS.get((msg.chat.id, msg.message_id)), "steps": []})
        try:
            return await handler(event, data)
        finally:
            CTX.reset(token)


class OwnerOnly(BaseMiddleware):
    """Бот личный: всё, что не от владельца, молча игнорируется."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user or user.id != config.OWNER_ID:
            return None
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    crypto.load()
    for attempt in range(30):
        try:
            await db.init()
            break
        except Exception as e:  # noqa: BLE001
            logging.warning("БД недоступна (%s), жду… %s", attempt, e)
            await asyncio.sleep(2)
    else:
        raise SystemExit("PostgreSQL так и не поднялся")

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(OwnerOnly())
    dp.callback_query.middleware(LiveMessage())
    # порядок важен: терминал и формы ловят текст раньше остальных
    dp.include_routers(common.router, forms.router, install.router, panels.router, mods.router,
                       security.router, updates.router, bedolaga.router, servers.router, keys.router,
                       linkext.router)
    await bot.set_my_commands([BotCommand(command="menu", description="Главное меню"),
                               BotCommand(command="cancel", description="Отменить ввод"),
                               BotCommand(command="term", description="Терминал на сервере (открыть/закрыть)")])
    if runtime.is_migrated():
        # бот переехал на другой сервер: этот экземпляр не должен забирать обновления у нового
        logging.warning("Бот переехал (%s) — этот экземпляр спит. Удалите data/migrated.flag, чтобы вернуть.",
                        runtime.flag_path().read_text().strip())
        await asyncio.Event().wait()
    runtime.dp = dp
    asyncio.create_task(common.autobackup_loop(bot))
    await common.notify_if_updated(bot)
    try:
        await bot.send_message(config.OWNER_ID, "🛰 VPN Master запущен. /menu")
    except Exception:  # noqa: BLE001
        pass
    while True:
        await dp.start_polling(bot, close_bot_session=False)
        if runtime.mode == "switching":  # умный переезд: ждём, чем кончится переключение
            await runtime.switch_done.wait()
        if runtime.mode == "rollback":  # новый не поднялся — работаем дальше здесь
            runtime.mode = None
            runtime.switch_done.clear()
            logging.warning("Переезд откатан — возобновляю polling")
            continue
        if runtime.mode == "migrated":
            logging.warning("Бот переехал — этот экземпляр спит")
            await asyncio.Event().wait()
        break
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
