"""Конфиг из окружения. В .env только BOT_TOKEN и OWNER_ID — всё остальное живёт в БД."""
import os
from pathlib import Path

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

# Подключение к Postgres из docker-compose (служебное, наружу не торчит).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://vpnmaster:{pw}@db:5432/vpnmaster".format(
        pw=os.getenv("POSTGRES_PASSWORD", "vpnmaster")
    ),
)
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
