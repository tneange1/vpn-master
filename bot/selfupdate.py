"""Связь с сайдкаром updater (updater.sh) через файлы в data/. Источник правды — настройки в БД,
файлы — их зеркало для updater-контейнера."""
from . import config, db

INTERVALS = [("выкл", 0), ("15 мин", 15), ("1 ч", 60), ("6 ч", 360), ("24 ч", 1440)]


def _f(name: str):
    return config.DATA_DIR / name


async def sync_files() -> None:
    """Пишем настройки из БД в файлы для updater."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _f("autoupdate.interval").write_text(await db.setting("autoupdate_interval", "60") or "0")
    _f("autoupdate.branch").write_text(await db.setting("autoupdate_branch", "main") or "main")


def request(mode: str = "update") -> None:
    """mode: update — обновить, если есть новое; check — только проверить."""
    _f("update.request").write_text(mode)


def status() -> dict:
    p = _f("update.status")
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def log_tail(n: int = 25) -> str:
    p = _f("updater.log")
    return "\n".join(p.read_text(errors="replace").splitlines()[-n:]) if p.exists() else ""
