"""Шифрование секретов (пароли, ключи, токены) перед записью в БД.

Мастер-ключ лежит в data/master.key (создаётся при первом запуске) и попадает в бэкап бота.
"""
import os

from cryptography.fernet import Fernet

from . import config

_fernet: Fernet | None = None


def key_path():
    return config.DATA_DIR / "master.key"


def load() -> None:
    global _fernet
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = key_path()
    if not p.exists():
        p.write_bytes(Fernet.generate_key())
        os.chmod(p, 0o600)
    _fernet = Fernet(p.read_bytes().strip())


def enc(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet.encrypt(value.encode()).decode()


def dec(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet.decrypt(value.encode()).decode()
