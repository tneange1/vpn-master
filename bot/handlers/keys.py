"""🔑 SSH-ключи (главное меню): генерация, импорт из файла/текста (приватный или только публичный),
и раздача ключей по конкретным серверам — панели, ноды, боты."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from .. import crypto, db, scripts, ssh
from ..forms import Field, form, start_form
from ..runner import run_task
from ..ui import h, kb, say, show
from .servers import install_key, mark_deploy, server_label

router = Router(name="keys")

PUB_PREFIXES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-ssh-ed25519@", "sk-ecdsa-sha2-", "ssh-dss ")


def _icon(k: db.SshKey) -> str:
    return "🔐" if k.private_enc else "📄"


async def _deployed(key_id: int) -> set[int]:
    async with db.Session() as s:
        from sqlalchemy import select
        return set((await s.scalars(select(db.KeyDeploy.server_id).where(db.KeyDeploy.key_id == key_id))).all())


@router.callback_query(F.data == "key:list")
async def key_list(c: CallbackQuery):
    keys = await db.all_(db.SshKey)
    rows = [[(f"{_icon(k)} {k.name}", f"key:v:{k.id}")] for k in keys]
    rows.append([("✨ Сгенерировать", "key:gen"), ("📥 Загрузить ключ", "key:imp")])
    rows.append([("⬅️ Меню", "menu")])
    await show(c, f"🔑 <b>SSH-ключи</b> ({len(keys)})\n\n"
                  "🔐 — пара (бот может входить этим ключом)\n📄 — только публичный (например, ключ твоего ПК)\n\n"
                  "Ключ можно раздать на конкретные серверы: панели, ноды, ботов. "
                  "Приватные части хранятся в БД зашифрованными.", kb(*rows))


@form("key_gen", "✨ Новый ключ ed25519", [Field("name", "Название ключа:")])
async def key_gen_done(event, values, ctx):
    priv, pub = ssh.generate_keypair(f"vpn-master-{values['name']}")
    k = await db.save(db.SshKey(name=values["name"], private_enc=crypto.enc(priv), public=pub))
    await say(event, f"✅ Ключ <b>{h(k.name)}</b> создан.\n<code>{h(pub)}</code>",
              kb([("📤 Раздать на серверы", f"key:dep:{k.id}")], [("Открыть", f"key:v:{k.id}"), ("⬅️ Ключи", "key:list")]))


@router.callback_query(F.data == "key:gen")
async def key_gen(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "key_gen")


@form("key_imp", "📥 Загрузка ключа", [
    Field("key", "Пришли ключ <b>файлом</b> или текстом:\n"
                 "• приватный (<code>id_ed25519</code>, <code>id_rsa</code>) — бот сможет им входить\n"
                 "• публичный (<code>*.pub</code>) — только раздавать на серверы\n\n"
                 "Сообщение удалю.", "raw", secret=True),
    Field("name", "Название ключа:", optional=True, default=""),
])
async def key_imp_done(event, values, ctx):
    raw = values["key"].strip()
    name = values.get("name") or (values.get("_filename") or "key").removesuffix(".pub")
    if raw.startswith(PUB_PREFIXES) and "\n" not in raw:
        try:
            ssh_pub = ssh.normalize_public(raw)
        except Exception as e:  # noqa: BLE001
            return await say(event, f"❌ Не похоже на публичный ключ: <code>{h(e)}</code>", kb([("⬅️ Ключи", "key:list")]))
        k = await db.save(db.SshKey(name=name, private_enc=None, public=ssh_pub))
        kind = "публичный 📄"
    else:
        pem = raw + "\n"
        try:
            pub = ssh.public_from_private(pem)
        except Exception as e:  # noqa: BLE001
            msg = "ключ защищён паролем — сними passphrase (<code>ssh-keygen -p -f ключ</code>)" \
                if "passphrase" in str(e).lower() else h(e)
            return await say(event, f"❌ Не смог прочитать приватный ключ: {msg}", kb([("⬅️ Ключи", "key:list")]))
        k = await db.save(db.SshKey(name=name, private_enc=crypto.enc(pem), public=pub))
        kind = "пара 🔐"
    await say(event, f"✅ Ключ <b>{h(k.name)}</b> загружен ({kind}).\n<code>{h(k.public)}</code>",
              kb([("📤 Раздать на серверы", f"key:dep:{k.id}")], [("Открыть", f"key:v:{k.id}"), ("⬅️ Ключи", "key:list")]))


@router.callback_query(F.data == "key:imp")
async def key_imp(c: CallbackQuery, state: FSMContext):
    await start_form(c, state, "key_imp")


@router.callback_query(F.data.startswith("key:v:"))
async def key_view(c: CallbackQuery):
    k = await db.get(db.SshKey, c.data.split(":")[2])
    if not k:
        return await c.answer("Ключа нет", show_alert=True)
    login = await db.all_(db.Server, db.Server.key_id == k.id, db.Server.auth == "key")
    dep = await _deployed(k.id)
    dep_names = [s.name for s in await db.all_(db.Server) if s.id in dep]
    i = k.id
    rows = [[("📤 Где стоит ключ (раздать/снять)", f"key:dep:{i}")]]
    if k.private_enc:
        rows.append([("🤖 Бот входит этим ключом на…", f"key:login:{i}")])
        rows.append([("📄 Приватный", f"key:dl:{i}:priv"), ("📄 Публичный", f"key:dl:{i}:pub")])
    else:
        rows.append([("📄 Публичный", f"key:dl:{i}:pub")])
    rows.append([("🗑 Удалить", f"key:del:{i}"), ("⬅️ Ключи", "key:list")])
    await show(c, f"{_icon(k)} <b>{h(k.name)}</b> — {'пара' if k.private_enc else 'только публичный'}\n"
                  f"Стоит на: {h(', '.join(dep_names)) or '—'}\n"
                  f"Бот входит им на: {h(', '.join(s.name for s in login)) or '—'}\n\n"
                  f"<code>{h(k.public)}</code>", kb(*rows))


@router.callback_query(F.data.startswith("key:dl:"))
async def key_dl(c: CallbackQuery):
    _, _, i, part = c.data.split(":")
    k = await db.get(db.SshKey, i)
    await c.answer()
    if part == "priv" and k.private_enc:
        await c.message.answer_document(BufferedInputFile(crypto.dec(k.private_enc).encode(), filename=k.name),
                                        caption="Приватный ключ. Никому не пересылай.")
    else:
        await c.message.answer_document(BufferedInputFile((k.public + "\n").encode(), filename=f"{k.name}.pub"))


# ------------------------------------------------------------------ раздача по серверам
@router.callback_query(F.data.startswith("key:dep:"))
async def key_deploy_list(c: CallbackQuery):
    k = await db.get(db.SshKey, c.data.split(":")[2])
    dep = await _deployed(k.id)
    rows = []
    for s in await db.all_(db.Server):
        on = s.id in dep
        rows.append([(f"{'✅' if on else '➕'} {await server_label(s)}", f"key:tg:{k.id}:{s.id}")])
    for p in await db.all_(db.Panel):
        if await db.all_(db.Node, db.Node.panel_id == p.id):
            rows.append([(f"📤 На все ноды панели {p.name}", f"key:allnodes:{k.id}:{p.id}")])
    if not rows:
        rows.append([("Серверов пока нет", "noop")])
    rows.append([("⬅️ К ключу", f"key:v:{k.id}")])
    await show(c, f"📤 <b>{h(k.name)}</b>: на каких серверах стоит\n\n"
                  "✅ — ключ в authorized_keys (нажми, чтобы снять)\n➕ — нажми, чтобы поставить", kb(*rows))


async def _toggle(c: CallbackQuery, bot: Bot, k: db.SshKey, s: db.Server, want: bool | None = None) -> bool:
    on = s.id in await _deployed(k.id)
    target = (not on) if want is None else want
    if target == on:
        return True
    if not target and s.key_id == k.id and s.auth == "key":
        await say(c, f"⛔️ Бот входит на <b>{h(s.name)}</b> этим ключом — сначала смени способ входа "
                     "(Доступы → сервер → 🔁 Способ входа).")
        return False
    script = scripts.ssh_add_key(s.username, k.public) if target else scripts.ssh_remove_key(s.username, k.public)
    res = await run_task(bot, c.message.chat.id, s, f"{'Ставлю' if target else 'Снимаю'} ключ {k.name}", script)
    if res.ok:
        await mark_deploy(k.id, s.id, target)
    return res.ok


@router.callback_query(F.data.startswith("key:tg:"))
async def key_toggle(c: CallbackQuery, bot: Bot):
    _, _, kid, sid = c.data.split(":")
    k, s = await db.get(db.SshKey, kid), await db.get(db.Server, sid)
    await c.answer()
    await _toggle(c, bot, k, s)
    await say(c, "Готово.", kb([("📤 К списку серверов", f"key:dep:{k.id}")]))


@router.callback_query(F.data.startswith("key:allnodes:"))
async def key_all_nodes(c: CallbackQuery, bot: Bot):
    _, _, kid, pid = c.data.split(":")
    k = await db.get(db.SshKey, kid)
    await c.answer()
    done = []
    for n in await db.all_(db.Node, db.Node.panel_id == int(pid)):
        s = await db.get(db.Server, n.server_id)
        if s:
            ok = await _toggle(c, bot, k, s, want=True)
            done.append(f"{'✅' if ok else '❌'} {h(n.name)}")
    await say(c, "Итог:\n" + ("\n".join(done) or "нод с серверами нет"), kb([("📤 К списку", f"key:dep:{k.id}")]))


@router.callback_query(F.data.startswith("key:login:"))
async def key_login_list(c: CallbackQuery):
    k = await db.get(db.SshKey, c.data.split(":")[2])
    rows = [[(f"{'✅' if (s.key_id == k.id and s.auth == 'key') else '▫️'} {await server_label(s)}",
              f"key:lg:{k.id}:{s.id}")] for s in await db.all_(db.Server)] or [[("Серверов нет", "noop")]]
    rows.append([("⬅️ К ключу", f"key:v:{k.id}")])
    await show(c, f"🤖 На каких серверах <b>бот</b> входит ключом <b>{h(k.name)}</b>?\n"
                  "Нажми сервер — поставлю ключ, проверю вход и переключу бота на него.", kb(*rows))


@router.callback_query(F.data.startswith("key:lg:"))
async def key_login_set(c: CallbackQuery, bot: Bot):
    _, _, kid, sid = c.data.split(":")
    await c.answer()
    await install_key(c, bot, await db.get(db.Server, sid), await db.get(db.SshKey, kid))


@router.callback_query(F.data.startswith("key:del:"))
async def key_del(c: CallbackQuery):
    k = await db.get(db.SshKey, c.data.split(":")[2])
    used = await db.all_(db.Server, db.Server.key_id == k.id)
    if used:
        return await c.answer("Бот входит этим ключом на: " + ", ".join(s.name for s in used), show_alert=True)
    await db.delete(k)
    await say(c, "🗑 Ключ удалён из бота (с серверов не снимал — сделай это в «📤 Где стоит» до удаления, если нужно).",
              kb([("⬅️ Ключи", "key:list")]))
