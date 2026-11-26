"""PostgreSQL: модели и мелкие хелперы. Все секреты хранятся в полях *_enc (Fernet)."""
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer,
                        String, Text, func, select)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import config

engine = create_async_engine(config.DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Stamp:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SshKey(Stamp, Base):
    __tablename__ = "ssh_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    private_enc: Mapped[str | None] = mapped_column(Text)  # None — только публичный ключ
    public: Mapped[str] = mapped_column(Text)


class KeyDeploy(Base):
    """На каких серверах публичный ключ стоит в authorized_keys."""
    __tablename__ = "key_deploys"
    key_id: Mapped[int] = mapped_column(ForeignKey("ssh_keys.id", ondelete="CASCADE"), primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)


class Server(Stamp, Base):
    __tablename__ = "servers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(64), default="root")
    auth: Mapped[str] = mapped_column(String(16), default="password")  # password | key
    password_enc: Mapped[str | None] = mapped_column(Text)
    key_id: Mapped[int | None] = mapped_column(ForeignKey("ssh_keys.id", ondelete="SET NULL"))
    host_key: Mapped[str | None] = mapped_column(Text)  # TOFU-отпечаток
    proxy: Mapped[str | None] = mapped_column(String(16))  # caddy | nginx — наш reverse proxy на сервере
    notes: Mapped[str | None] = mapped_column(Text)


class Panel(Stamp, Base):
    __tablename__ = "panels"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"))
    domain: Mapped[str] = mapped_column(String(255))
    sub_domain: Mapped[str | None] = mapped_column(String(255))
    api_url: Mapped[str] = mapped_column(String(255))
    api_token_enc: Mapped[str | None] = mapped_column(Text)
    admin_user: Mapped[str | None] = mapped_column(String(64))
    admin_pass_enc: Mapped[str | None] = mapped_column(Text)
    managed_proxy: Mapped[bool] = mapped_column(Boolean, default=False)  # Caddy ставил vpn-master
    protected: Mapped[bool] = mapped_column(Boolean, default=False)  # секретная ссылка/cookie
    cookie_enc: Mapped[str | None] = mapped_column(Text)
    link_extractor: Mapped[bool] = mapped_column(Boolean, default=False)
    sub_local: Mapped[bool] = mapped_column(Boolean, default=True)  # страница подписки на сервере панели


class Node(Stamp, Base):
    __tablename__ = "nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str | None] = mapped_column(String(255))
    rw_uuid: Mapped[str | None] = mapped_column(String(64))
    profile_uuid: Mapped[str | None] = mapped_column(String(64))
    warp: Mapped[bool] = mapped_column(Boolean, default=False)


class Bedolaga(Stamp, Base):
    __tablename__ = "bedolaga"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"))
    panel_id: Mapped[int | None] = mapped_column(ForeignKey("panels.id", ondelete="SET NULL"))
    bot_token_enc: Mapped[str | None] = mapped_column(Text)
    admin_ids: Mapped[str | None] = mapped_column(String(255))
    hooks_domain: Mapped[str | None] = mapped_column(String(255))
    web_api_token_enc: Mapped[str | None] = mapped_column(Text)
    install_dir: Mapped[str] = mapped_column(String(255), default="/opt/remnawave-bedolaga-telegram-bot")
    compose_file: Mapped[str] = mapped_column(String(64), default="docker-compose.yml")
    cabinet_domain: Mapped[str | None] = mapped_column(String(255))
    cabinet: Mapped[bool] = mapped_column(Boolean, default=False)


class Mod(Stamp, Base):
    """Установленные модификации: link_extractor / backup_restore (на панель), warp (на ноду)."""
    __tablename__ = "mods"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    panel_id: Mapped[int | None] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"))
    bot_token_enc: Mapped[str | None] = mapped_column(Text)
    admin_ids: Mapped[str | None] = mapped_column(String(255))
    secrets_enc: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class Log(Stamp, Base):
    __tablename__ = "logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(255))
    ok: Mapped[bool] = mapped_column(Boolean)
    tail: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)


# Простые миграции для уже работающих баз (create_all не добавляет колонки).
MIGRATIONS = [
    "ALTER TABLE ssh_keys ALTER COLUMN private_enc DROP NOT NULL",
    "ALTER TABLE servers ADD COLUMN IF NOT EXISTS proxy VARCHAR(16)",
    "ALTER TABLE panels ADD COLUMN IF NOT EXISTS sub_local BOOLEAN DEFAULT TRUE",
]


async def init() -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for sql in MIGRATIONS:
            await conn.execute(text(sql))


# ---------- хелперы ----------
async def get(model, id_):
    try:
        id_ = int(id_)
    except (TypeError, ValueError):
        return None
    async with Session() as s:
        return await s.get(model, id_)


async def all_(model, *where, order=None):
    async with Session() as s:
        q = select(model)
        for w in where:
            q = q.where(w)
        q = q.order_by(order if order is not None else model.id)
        return list((await s.scalars(q)).all())


async def save(obj):
    async with Session() as s:
        obj = await s.merge(obj)
        await s.commit()
        return obj


async def delete(obj) -> None:
    async with Session() as s:
        o = await s.get(type(obj), obj.id)
        if o:
            await s.delete(o)
            await s.commit()


async def count(model) -> int:
    async with Session() as s:
        return await s.scalar(select(func.count()).select_from(model)) or 0


async def setting(key: str, default: str | None = None) -> str | None:
    async with Session() as s:
        row = await s.get(Setting, key)
        return row.value if row else default


async def set_setting(key: str, value: str | None) -> None:
    await save(Setting(key=key, value=value))
