"""Интерактивный терминал через Telegram: для установщиков с вопросами
(backup-restore, warp-native) и ручной работы на сервере."""
import asyncio
import shlex
from dataclasses import dataclass, field

import asyncssh
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from . import db, ssh
from .runner import clean
from .ui import h, kb

TERM_KB = kb(
    [("⏎ Enter", "term:enter"), ("Ctrl+C", "term:ctrlc"), ("↑", "term:up"), ("↓", "term:down")],
    [("1", "term:k:1"), ("2", "term:k:2"), ("3", "term:k:3"), ("y", "term:k:y"), ("n", "term:k:n")],
    [("❌ Закрыть терминал", "term:close")],
)


@dataclass
class TermSession:
    conn: asyncssh.SSHClientConnection
    proc: asyncssh.SSHClientProcess
    msg: Message
    title: str
    buf: list = field(default_factory=list)
    closed: bool = False
    task: asyncio.Task | None = None


SESSIONS: dict[int, TermSession] = {}


def _screen(s: TermSession, status: str = "🟢") -> str:
    body = clean("".join(s.buf))
    # схлопываем пустые строки, берём хвост
    lines = [ln for ln in body.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    tail = "\n".join(lines)[-3300:]
    return f"{status} <b>Терминал</b> · {h(s.title)}\n<pre>{h(tail) or '…'}</pre>\n<i>Пиши текст сообщением — уйдёт в терминал.</i>"


async def start(bot: Bot, chat_id: int, user_id: int, server: db.Server, command: str | None, title: str):
    await stop(user_id)
    conn = await ssh.open_server(server)
    if command:
        command = ("" if server.username == "root" else "sudo ") + "bash -c " + shlex.quote(command)
    proc = await conn.create_process(command, term_type="xterm", term_size=(110, 40), stderr=asyncssh.STDOUT)
    msg = await bot.send_message(chat_id, f"🟢 <b>Терминал</b> · {h(title)}\n<pre>…</pre>", reply_markup=TERM_KB)
    s = TermSession(conn, proc, msg, title)
    SESSIONS[user_id] = s
    s.task = asyncio.create_task(_pump(user_id, s))


async def _pump(user_id: int, s: TermSession):
    async def reader():
        while True:
            chunk = await s.proc.stdout.read(4096)
            if not chunk:
                break
            s.buf.append(chunk)
            if len(s.buf) > 400:
                s.buf[:] = ["".join(s.buf)[-20000:]]

    rtask = asyncio.create_task(reader())
    last = ""
    while not rtask.done():
        await asyncio.sleep(2)
        text = _screen(s)
        if text != last:
            try:
                await s.msg.edit_text(text, reply_markup=TERM_KB)
                last = text
            except TelegramBadRequest:
                pass
    s.closed = True
    try:
        await s.msg.edit_text(_screen(s, "⚪️ завершён ·"), reply_markup=None)
    except TelegramBadRequest:
        pass
    s.conn.close()
    if SESSIONS.get(user_id) is s:
        SESSIONS.pop(user_id, None)


def active(user_id: int) -> bool:
    s = SESSIONS.get(user_id)
    return bool(s and not s.closed)


def send(user_id: int, data: str) -> bool:
    s = SESSIONS.get(user_id)
    if not s or s.closed:
        return False
    s.proc.stdin.write(data)
    return True


async def stop(user_id: int):
    s = SESSIONS.pop(user_id, None)
    if s and not s.closed:
        try:
            s.proc.stdin.write("\x03")
            s.proc.close()
        except Exception:  # noqa: BLE001
            pass
        s.conn.close()
