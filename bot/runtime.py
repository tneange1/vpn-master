"""Состояние процесса бота: умный переезд (флаг «переехал», переключение без двойного polling)."""
import asyncio

from . import config

dp = None  # Dispatcher — ставит __main__
mode: str | None = None  # None | "switching" | "rollback" | "migrated"
switch_done = asyncio.Event()
busy = False  # идёт переезд


def flag_path():
    return config.DATA_DIR / "migrated.flag"


def is_migrated() -> bool:
    return flag_path().exists()


def mark_migrated(where: str) -> None:
    flag_path().write_text(where)


def clear_migrated() -> None:
    flag_path().unlink(missing_ok=True)


def request_shutdown() -> None:
    """Попросить сайдкар updater остановить контейнеры этого (старого) экземпляра."""
    (config.DATA_DIR / "shutdown.request").write_text("migrated")
