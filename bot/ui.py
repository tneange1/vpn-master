"""Клавиатуры и отправка сообщений."""
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


def h(text) -> str:
    return escape(str(text if text is not None else ""))


def kb(*rows) -> InlineKeyboardMarkup:
    """kb([("Текст", "data"), ...], ...). data вида 'url:https://...' даёт кнопку-ссылку."""
    out = []
    for row in rows:
        if not row:
            continue
        line = []
        for text, data in row:
            if data.startswith("url:"):
                line.append(InlineKeyboardButton(text=text, url=data[4:]))
            else:
                line.append(InlineKeyboardButton(text=text, callback_data=data[:64]))
        out.append(line)
    return InlineKeyboardMarkup(inline_keyboard=out)


def chat_id(event) -> int:
    return event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id


async def show(event, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    """Для callback — редактирует сообщение, для message — отправляет новое."""
    if isinstance(event, CallbackQuery):
        try:
            msg = await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                msg = event.message
            else:
                msg = await event.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
        return msg
    return await event.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def say(event, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    """Всегда новое сообщение."""
    target = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
    return await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


def yes_no(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return kb([("✅ Да", yes_data), ("✖️ Нет", no_data)])
