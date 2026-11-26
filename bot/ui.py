"""Интерфейс: цветные кнопки (Bot API style), карточки-таблицы (rich message), одно «живое» сообщение.

• kb(): цвет кнопки выбирается по эмодзи в начале (⬅️/🗑/✖️ — красные, ✅/➕/🚀/⬆️ — зелёные,
  🩹/🏠/🔁 — синие) или явно третьим элементом: ("Текст", "data", "success"|"danger"|"primary"|"").
• show(..., card=...) рисует таблицу (rich message); если Telegram её не примет — обычный HTML.
• Контекст CTX: сообщение, в котором нажали кнопку, и экран, на который вернёт «⬅️ Назад».
  run_task и формы пишут прогресс прямо в это сообщение, а не присылают новые.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendRichMessage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputRichBlockDivider,
                           InputRichBlockFooter, InputRichBlockParagraph, InputRichBlockSectionHeading,
                           InputRichBlockTable, InputRichMessage, Message, RichBlockTableCell, RichTextBold,
                           RichTextCode, RichTextSpoiler, RichTextUrl)


def h(text) -> str:
    return escape(str(text if text is not None else ""))


# ------------------------------------------------------------------ кнопки
DANGER = ("⬅️", "🗑", "⛔", "✖️", "❌", "🛑")
SUCCESS = ("✅", "➕", "🚀", "✨", "▶️", "⬆️", "📥", "🔓", "💾 Сделать", "♻️ Да", "🗑 Да")
PRIMARY = ("🩹", "🏠", "🔁", "🔍", "📦 Установка", "🛠 Управление")


def _style(text: str) -> str | None:
    t = text.strip()
    if t.startswith(("🗑 Да", "♻️ Да")):
        return "danger"
    if t.startswith(DANGER):
        return "danger"
    if t.startswith(SUCCESS):
        return "success"
    if t.startswith(PRIMARY):
        return "primary"
    return None


def kb(*rows) -> InlineKeyboardMarkup:
    """kb([("Текст", "data"[, "style"]), ...], ...). data 'url:https://...' — кнопка-ссылка."""
    out = []
    for row in rows:
        if not row:
            continue
        line = []
        for item in row:
            text, data = item[0], item[1]
            style = item[2] if len(item) > 2 else _style(text)
            extra = {"style": style} if style else {}
            if data.startswith("url:"):
                line.append(InlineKeyboardButton(text=text, url=data[4:], **extra))
            else:
                line.append(InlineKeyboardButton(text=text, callback_data=data[:64], **extra))
        out.append(line)
    return InlineKeyboardMarkup(inline_keyboard=out)


def back_kb(back: str | None, *extra_rows) -> InlineKeyboardMarkup:
    nav = [("⬅️ Назад", back or "menu")] + ([("🏠 Меню", "menu")] if back and back != "menu" else [])
    return kb(*extra_rows, nav)


def yes_no(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return kb([("✅ Да", yes_data), ("✖️ Нет", no_data)])


# ------------------------------------------------------------------ карточки (rich message)
@dataclass
class Card:
    title: str
    rows: list = field(default_factory=list)  # (label, value[, "code"|"spoiler"|"url"]) или ("—",) разделитель
    note: str | None = None
    footer: str | None = None


def _val(v: str, kind: str | None):
    v = str(v if v not in (None, "") else "—")
    if kind == "spoiler":
        return RichTextSpoiler(text=v)
    if kind == "code":
        return RichTextCode(text=v)
    if kind == "url" or (kind is None and v.startswith("https://")):
        return RichTextUrl(text=v.removeprefix("https://"), url=v if v.startswith("http") else f"https://{v}")
    return v


def _cell(text, header=False):
    return RichBlockTableCell(align="left", valign="middle", text=text, is_header=header or None)


def card_rich(c: Card) -> InputRichMessage:
    blocks = [InputRichBlockSectionHeading(text=c.title, size=3)]
    table = []
    for r in c.rows:
        if len(r) == 1:  # разделитель-подзаголовок
            if table:
                blocks.append(InputRichBlockTable(cells=table, is_bordered=True, is_compact=True))
                table = []
            blocks.append(InputRichBlockSectionHeading(text=r[0], size=5))
            continue
        table.append([_cell(RichTextBold(text=str(r[0]))), _cell(_val(r[1], r[2] if len(r) > 2 else None))])
    if table:
        blocks.append(InputRichBlockTable(cells=table, is_bordered=True, is_compact=True))
    if c.note:
        blocks += [InputRichBlockDivider(), InputRichBlockParagraph(text=c.note)]
    if c.footer:
        blocks.append(InputRichBlockFooter(text=c.footer))
    return InputRichMessage(blocks=blocks)


def card_html(c: Card) -> str:
    lines = [f"<b>{h(c.title)}</b>"]
    for r in c.rows:
        if len(r) == 1:
            lines.append(f"\n<b>{h(r[0])}</b>")
            continue
        kind = r[2] if len(r) > 2 else None
        v = h(r[1] if r[1] not in (None, "") else "—")
        v = f"<tg-spoiler>{v}</tg-spoiler>" if kind == "spoiler" else f"<code>{v}</code>" if kind == "code" else v
        lines.append(f"{h(r[0])}: {v}")
    if c.note:
        lines.append("\n" + h(c.note))
    if c.footer:
        lines.append(f"<i>{h(c.footer)}</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ контекст «живого» сообщения
SCREENS: dict[tuple[int, int], str] = {}  # (chat, message_id) → callback data экрана в этом сообщении
CTX: ContextVar[dict | None] = ContextVar("vm_ctx", default=None)


def remember(chat: int, mid: int, data: str | None) -> None:
    if data:
        SCREENS[(chat, mid)] = data
        if len(SCREENS) > 2000:
            for k in list(SCREENS)[:500]:
                SCREENS.pop(k, None)


def chat_id(event) -> int:
    return event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id


async def render(bot: Bot, chat: int, mid: int | None, text: str, markup: InlineKeyboardMarkup | None = None,
                 card: Card | None = None) -> int:
    """Показать экран в сообщении mid (правка). Не вышло — новое сообщение, старое удалить. Возвращает id."""
    if mid:
        try:
            if card:
                try:
                    await bot(EditMessageText(chat_id=chat, message_id=mid, rich_message=card_rich(card),
                                              reply_markup=markup))
                    return mid
                except TelegramBadRequest as e:
                    if "not modified" in str(e):
                        return mid
            await bot.edit_message_text(text, chat_id=chat, message_id=mid, reply_markup=markup,
                                        disable_web_page_preview=True)
            return mid
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return mid
    msg = await _send(bot, chat, text, markup, card)
    if mid:
        try:
            await bot.delete_message(chat, mid)
        except TelegramBadRequest:
            pass
    return msg.message_id


async def _send(bot: Bot, chat: int, text: str, markup=None, card: Card | None = None) -> Message:
    if card:
        try:
            return await bot(SendRichMessage(chat_id=chat, rich_message=card_rich(card), reply_markup=markup))
        except TelegramBadRequest:
            pass
    return await bot.send_message(chat, text, reply_markup=markup, disable_web_page_preview=True)


async def show(event, text: str, markup: InlineKeyboardMarkup | None = None, card: Card | None = None):
    """Экран: для кнопки — правит это же сообщение, для текста — новое сообщение."""
    if card and not text:
        text = card_html(card)
    if isinstance(event, CallbackQuery):
        chat = event.message.chat.id
        mid = await render(event.bot, chat, event.message.message_id, text, markup, card)
        remember(chat, mid, event.data)
        ctx = CTX.get()
        if ctx is not None:
            ctx["mid"] = mid
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
        return mid
    msg = await _send(event.bot, event.chat.id, text, markup, card)
    return msg.message_id


def _with_back(markup: InlineKeyboardMarkup | None, back: str | None) -> InlineKeyboardMarkup:
    rows = list(markup.inline_keyboard) if markup else []
    has_nav = any(b.text.startswith(("⬅️", "🏠")) for r in rows for b in r)
    return InlineKeyboardMarkup(inline_keyboard=rows if has_nav else rows + back_kb(back).inline_keyboard)


async def post(bot: Bot, chat: int, text: str, markup: InlineKeyboardMarkup | None = None) -> Message | None:
    """Итог действия — в то же «живое» сообщение (шаги сверху, «⬅️ Назад» снизу).
    Если задача упала — отдельным сообщением, чтобы её лог остался на экране."""
    ctx = CTX.get()
    if ctx is not None and ctx.get("chat") == chat and ctx.get("mid") and all(ok for ok, _ in ctx.get("steps", [])):
        head = "".join(f"✅ {h(t)}\n" for _, t in ctx["steps"])
        ctx["mid"] = await render(bot, chat, ctx.get("mid"), f"{head}\n{text}" if head else text,
                                  _with_back(markup, ctx.get("back")))
        return None
    return await bot.send_message(chat, text, reply_markup=markup, disable_web_page_preview=True)


async def say(event, text: str, markup: InlineKeyboardMarkup | None = None) -> Message | None:
    """Итоги, секреты: в «живое» сообщение после успешных задач, иначе — отдельным сообщением."""
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
    return await post(event.bot, chat_id(event), text, markup)


async def live(event, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Обновить «живое» сообщение (кнопки/формы) — без новых сообщений; вне контекста — новое."""
    ctx = CTX.get()
    chat = chat_id(event)
    if ctx is not None and ctx.get("chat") == chat:
        ctx["mid"] = await render(event.bot, chat, ctx.get("mid"), text, _with_back(markup, ctx.get("back")))
    else:
        await event.bot.send_message(chat, text, reply_markup=markup, disable_web_page_preview=True)
