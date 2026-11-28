"""Восстановление бэкапа из командной строки (для умного переезда):
docker compose run --rm --no-deps -T bot python -m bot.restore_cli /app/data/migrate.tar.gz"""
import asyncio
import sys
from pathlib import Path

from . import backup, crypto, db


async def main(path: str) -> None:
    crypto.load()
    for _ in range(30):
        try:
            async with db.engine.connect():
                break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(2)
    await backup.restore(Path(path))
    print("##VM RESTORED=1")
    print("==> База и ключ шифрования восстановлены")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
