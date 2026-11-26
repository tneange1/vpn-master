"""Мини-движок пошаговых форм (визардов): поле за полем, кнопки-варианты, пропуск, отмена."""
import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .ui import CTX, SCREENS, back_kb, chat_id, h, kb, render, show

router = Router(name="forms")


class FormSt(StatesGroup):
    filling = State()


@dataclass
class Field:
    key: str
    prompt: str
    kind: str = "text"  # text | int | domain | ids | choice | raw
    choices: Any = None  # list[(label, value)] | (values, ctx) -> list | async
    optional: bool = False
    default: Any = None
    secret: bool = False  # удалить сообщение пользователя после чтения
    when: Callable | None = None


@dataclass
class Form:
    title: str
    fields: list
    done: Callable  # async (event, values, ctx)


FORMS: dict[str, Form] = {}


def form(name: str, title: str, fields: list):
    def deco(fn):
        FORMS[name] = Form(title, fields, fn)
        return fn
    return deco


async def start_form(event, state: FSMContext, name: str, ctx: dict | None = None, preset: dict | None = None):
    """Форма живёт в одном сообщении: вопросы правят его, ответы пользователя удаляются."""
    pmid, back = None, None
    if isinstance(event, CallbackQuery):
        pmid = event.message.message_id
        back = SCREENS.get((event.message.chat.id, pmid))
    await state.set_state(FormSt.filling)
    await state.update_data(form=name, idx=-1, values=dict(preset or {}), ctx=ctx or {}, opts=[], pmid=pmid, back=back)
    await _next(event, state)


async def _screen(event, state: FSMContext, text: str, markup) -> None:
    data = await state.get_data()
    chat = chat_id(event)
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
    mid = await render(event.bot, chat, data.get("pmid"), text, markup)
    await state.update_data(pmid=mid)


async def _choices(f: Field, values, ctx):
    ch = f.choices
    if callable(ch):
        ch = ch(values, ctx)
        if inspect.isawaitable(ch):
            ch = await ch
    return list(ch or [])


async def _reprompt(event, state: FSMContext, warn: str) -> None:
    """Повторить текущий вопрос с предупреждением (в том же сообщении)."""
    data = await state.get_data()
    await state.update_data(idx=data["idx"] - 1)
    await _next(event, state, warn)


async def _next(event, state: FSMContext, warn: str = ""):
    data = await state.get_data()
    form_ = FORMS[data["form"]]
    values, ctx = data["values"], data["ctx"]
    idx = data["idx"] + 1
    while idx < len(form_.fields):
        f = form_.fields[idx]
        if f.key not in values and (f.when is None or f.when(values)):
            break
        idx += 1
    if idx >= len(form_.fields):
        pmid, back = data.get("pmid"), data.get("back")
        await state.clear()
        # результат формы (логи задач) — в то же сообщение, с кнопкой «Назад» на исходный экран
        token = CTX.set({"chat": chat_id(event), "mid": pmid, "back": back, "steps": []})
        try:
            await form_.done(event, values, ctx)
        finally:
            CTX.reset(token)
        return
    f = form_.fields[idx]
    rows, opts = [], []
    if f.kind == "choice":
        try:
            choices = await _choices(f, values, ctx)
        except Exception as e:  # noqa: BLE001
            back = data.get("back")
            await _screen(event, state, f"❌ Не удалось получить варианты: <code>{h(e)}</code>", back_kb(back))
            await state.clear()
            return
        for i, (label, val) in enumerate(choices):
            rows.append([(label, f"f:c:{i}")])
            opts.append(val)
        if not choices:
            rows.append([("(вариантов нет)", "noop")])
    nav = []
    if f.optional:
        nav.append(("⏭ Пропустить" + (f" ({f.default})" if f.default not in (None, "") else ""), "f:skip"))
    nav.append(("✖️ Отмена", "f:x"))
    rows.append(nav)
    await state.update_data(idx=idx, opts=opts)
    await _screen(event, state, f"<b>{h(form_.title)}</b>\n\n{f.prompt}" + (f"\n\n⚠️ {warn}" if warn else ""), kb(*rows))


DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z0-9-]{2,63}$")


def _convert(f: Field, v: str):
    if f.kind == "int":
        if not v.isdigit():
            raise ValueError("нужно число")
        return int(v)
    if f.kind == "domain":
        v = re.sub(r"^https?://", "", v.strip().lower()).strip("/")
        if not DOMAIN_RE.match(v):
            raise ValueError("это не похоже на домен, пример: <code>panel.example.com</code>")
        return v
    if f.kind == "domains":
        out = []
        for d in re.split(r"[,\s]+", v.strip().lower()):
            if not d:
                continue
            d = re.sub(r"^https?://", "", d).strip("/")
            if not DOMAIN_RE.match(d):
                raise ValueError(f"<code>{h(d)}</code> не похоже на домен")
            out.append(d)
        if not out:
            raise ValueError("нужен хотя бы один домен")
        return ",".join(dict.fromkeys(out))
    if f.kind == "ids":
        ids = [x for x in re.split(r"[,\s]+", v) if x]
        if not ids or not all(x.lstrip("-").isdigit() for x in ids):
            raise ValueError("нужны числовые ID через запятую")
        return ",".join(ids)
    if f.kind == "raw":
        return v
    return v.strip()


@router.message(FormSt.filling, F.text)
async def on_text(m: Message, state: FSMContext):
    data = await state.get_data()
    f: Field = FORMS[data["form"]].fields[data["idx"]]
    raw = m.text if f.kind == "raw" else m.text.strip()
    try:  # ответы не копятся в чате — всё в одном сообщении формы
        await m.delete()
    except TelegramBadRequest:
        pass
    if f.kind == "choice":
        return await _reprompt(m, state, "выбери вариант кнопкой")
    try:
        value = _convert(f, raw)
    except ValueError as e:
        return await _reprompt(m, state, f"{e}. Попробуй ещё раз.")
    values = data["values"]
    values[f.key] = value
    await state.update_data(values=values)
    await _next(m, state)


@router.message(FormSt.filling, F.document)
async def on_document(m: Message, state: FSMContext):
    """Файл вместо текста (например, приватный/публичный ключ)."""
    data = await state.get_data()
    f: Field = FORMS[data["form"]].fields[data["idx"]]
    if f.kind != "raw":
        await m.delete()
        return await _reprompt(m, state, "тут нужен текст, а не файл")
    if m.document.file_size and m.document.file_size > 64 * 1024:
        await m.delete()
        return await _reprompt(m, state, "файл больше 64 КБ — это точно ключ?")
    buf = await m.bot.download(m.document)
    try:
        text = buf.read().decode("utf-8")
    except UnicodeDecodeError:
        await m.delete()
        return await _reprompt(m, state, "файл не текстовый, нужен ключ OpenSSH/PEM")
    try:
        await m.delete()
    except TelegramBadRequest:
        pass
    values = data["values"]
    values[f.key] = text
    values.setdefault("_filename", m.document.file_name)
    await state.update_data(values=values)
    await _next(m, state)


@router.message(FormSt.filling)
async def on_other(m: Message, state: FSMContext):
    try:
        await m.delete()
    except TelegramBadRequest:
        pass
    await _reprompt(m, state, "жду текст (или файл, если просят ключ)")


@router.callback_query(FormSt.filling, F.data.startswith("f:c:"))
async def on_choice(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    i = int(c.data.split(":")[2])
    opts = data.get("opts") or []
    if i >= len(opts):
        await c.answer("Устарело", show_alert=True)
        return
    f: Field = FORMS[data["form"]].fields[data["idx"]]
    values = data["values"]
    values[f.key] = opts[i]
    await state.update_data(values=values)
    await _next(c, state)


@router.callback_query(FormSt.filling, F.data == "f:skip")
async def on_skip(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    f: Field = FORMS[data["form"]].fields[data["idx"]]
    values = data["values"]
    values[f.key] = f.default
    await state.update_data(values=values)
    await _next(c, state)


@router.callback_query(F.data == "f:x")
async def on_cancel(c: CallbackQuery, state: FSMContext):
    back = (await state.get_data()).get("back")
    await state.clear()
    await show(c, "✖️ Отменено.", back_kb(back))


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()
