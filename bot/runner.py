"""Запуск скрипта на сервере с живым логом в Telegram.

Строки вида `##VM KEY=VALUE` — служебные маркеры: в чат и в лог не попадают,
возвращаются в result.marks (так скрипт отдаёт боту токены и т.п.).
"""
import asyncio
import re
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

from . import db, ssh
from .ui import CTX, back_kb, h, render

MARK_RE = re.compile(r"^##VM (\w+)=(.*)$", re.M)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")


@dataclass
class Result:
    ok: bool
    code: int
    output: str
    marks: dict = field(default_factory=dict)


def clean(text: str) -> str:
    text = ANSI_RE.sub("", text)
    lines = []
    for raw in text.split("\n"):
        # \r внутри строки — прогресс-бары: берём последний кусок
        part = raw.split("\r")[-1] if "\r" in raw.rstrip("\r") else raw.rstrip("\r")
        if not part.startswith("##VM "):
            lines.append(part)
    return "\n".join(lines)


def _render(title: str, server: str, body: str, status: str, steps: list | None = None) -> str:
    tail = body[-3000:]
    if len(body) > 3000:
        tail = "…" + tail[tail.find("\n") + 1:]
    done = "".join(f"{'✅' if ok else '❌'} {h(t)}\n" for ok, t in (steps or []))
    return f"{done}{status} <b>{h(title)}</b> · <code>{h(server)}</code>\n<pre>{h(tail) or '…'}</pre>"


async def run_task(bot: Bot, chat_id: int, server: db.Server, title: str, script: str,
                   timeout: int = 2400) -> Result:
    """Выполнить скрипт с живым логом. Если действие запущено кнопкой/формой — лог идёт в то же
    сообщение (а не новое), в конце — «⬅️ Назад» на прежний экран и «🏠 Меню»."""
    ctx = CTX.get()
    steps = ctx["steps"] if ctx is not None else None
    if ctx is not None and ctx.get("chat") == chat_id:
        mid = await render(bot, chat_id, ctx.get("mid"), _render(title, server.name, "подключаюсь…", "⏳", steps))
        ctx["mid"] = mid
    else:
        ctx = None
        mid = (await bot.send_message(chat_id, _render(title, server.name, "подключаюсь…", "⏳"))).message_id
    buf: list[str] = []
    done = asyncio.Event()

    async def ticker():
        last = ""
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), 4)
            except asyncio.TimeoutError:
                pass
            if done.is_set():
                return
            text = _render(title, server.name, clean("".join(buf)), "⏳", steps)
            if text != last:
                try:
                    await bot.edit_message_text(text, chat_id=chat_id, message_id=mid)
                    last = text
                except TelegramBadRequest:
                    pass

    tick = asyncio.create_task(ticker())
    try:
        code, out = await ssh.run(server, script, timeout=timeout, on_chunk=buf.append)
    except Exception as e:  # noqa: BLE001
        code, out = -1, "".join(buf) + f"\n[vpn-master] ошибка SSH: {type(e).__name__}: {e}"
    finally:
        done.set()
        await tick

    marks = {m.group(1): m.group(2).strip() for m in MARK_RE.finditer(out)}
    body = clean(out)
    ok = code == 0
    markup = back_kb(ctx.get("back")) if ctx is not None else None
    try:
        await bot.edit_message_text(_render(title, server.name, body + f"\n\n[exit {code}]", "✅" if ok else "❌", steps),
                                    chat_id=chat_id, message_id=mid, reply_markup=markup)
    except TelegramBadRequest:
        pass
    if steps is not None:
        steps.append((ok, title))
    if len(body) > 3000:
        await bot.send_document(chat_id, BufferedInputFile(body.encode(), filename="log.txt"),
                                caption=f"Полный лог: {h(title)}")
    await db.save(db.Log(server_id=server.id, title=title, ok=ok, tail=body[-4000:]))
    return Result(ok, code, body, marks)
