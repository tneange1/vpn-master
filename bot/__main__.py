"""Точка входа: python -m bot"""
import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import config, crypto, db, forms
from .handlers import common, install, keys, mods, panels, security, servers, updates


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
    # порядок важен: терминал и формы ловят текст раньше остальных
    dp.include_routers(common.router, forms.router, install.router, panels.router, mods.router,
                       security.router, updates.router, servers.router, keys.router)
    await bot.set_my_commands([BotCommand(command="menu", description="Главное меню"),
                               BotCommand(command="cancel", description="Отменить ввод"),
                               BotCommand(command="term", description="Терминал на сервере (открыть/закрыть)")])
    asyncio.create_task(common.autobackup_loop(bot))
    await common.notify_if_updated(bot)
    try:
        await bot.send_message(config.OWNER_ID, "🛰 VPN Master запущен. /menu")
    except Exception:  # noqa: BLE001
        pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
