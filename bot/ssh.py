"""SSH: подключение к серверам из БД, запуск bash-скриптов, генерация ключей."""
import asyncio
from dataclasses import dataclass

import asyncssh

from . import crypto, db


@dataclass
class Creds:
    host: str
    port: int
    username: str
    password: str | None = None
    private_key: str | None = None
    host_key: str | None = None


async def creds_for(server: db.Server, use_password: bool = True) -> Creds:
    key = None
    if server.key_id:
        k = await db.get(db.SshKey, server.key_id)
        key = crypto.dec(k.private_enc) if k else None
    password = crypto.dec(server.password_enc) if use_password else None
    return Creds(server.host, server.port, server.username, password, key, server.host_key)


def generate_keypair(comment: str = "vpn-master") -> tuple[str, str]:
    k = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    return k.export_private_key().decode(), k.export_public_key().decode().strip()


def normalize_public(line: str) -> str:
    """Проверяет публичный ключ OpenSSH и возвращает его одной строкой (с комментарием)."""
    line = " ".join(line.split())
    asyncssh.import_public_key(line)
    return line


def public_from_private(pem: str) -> str:
    return asyncssh.import_private_key(pem).export_public_key().decode().strip()


async def _connect(c: Creds) -> asyncssh.SSHClientConnection:
    kw = dict(host=c.host, port=c.port, username=c.username, connect_timeout=25,
              keepalive_interval=30, agent_path=None, client_keys=[])
    if c.private_key:
        kw["client_keys"] = [asyncssh.import_private_key(c.private_key)]
    if c.password:
        kw["password"] = c.password
    # Trust-on-first-use: первый раз запоминаем ключ хоста, дальше сверяем.
    kw["known_hosts"] = ([asyncssh.import_public_key(c.host_key)], [], []) if c.host_key else None
    return await asyncssh.connect(**kw)


async def open_server(server: db.Server, use_password: bool = True) -> asyncssh.SSHClientConnection:
    c = await creds_for(server, use_password)
    conn = await _connect(c)
    if not server.host_key:
        server.host_key = conn.get_server_host_key().export_public_key().decode().strip()
        await db.save(server)
    return conn


def _wrap(c_user: str, password: str | None, script: str) -> tuple[str, str]:
    if c_user == "root":
        return "bash -s", script
    if password:
        return "sudo -S -p '' bash -s", password + "\n" + script
    return "sudo -n bash -s", script


async def run(server: db.Server, script: str, timeout: int = 1800, on_chunk=None,
              use_password: bool = True) -> tuple[int, str]:
    """Выполняет bash-скрипт на сервере (через stdin), stdout+stderr вместе."""
    conn = await open_server(server, use_password)
    async with conn:
        password = crypto.dec(server.password_enc) if use_password else None
        cmd, data = _wrap(server.username, password, script)
        proc = await conn.create_process(cmd, stderr=asyncssh.STDOUT)
        proc.stdin.write(data)
        proc.stdin.write_eof()
        out: list[str] = []

        async def reader():
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                out.append(chunk)
                if on_chunk:
                    on_chunk(chunk)

        try:
            await asyncio.wait_for(reader(), timeout)
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            out.append("\n[vpn-master] таймаут, процесс остановлен\n")
            return 124, "".join(out)
        code = proc.exit_status if proc.exit_status is not None else -1
        return code, "".join(out)


async def check(server: db.Server, use_password: bool = True) -> tuple[bool, str]:
    try:
        code, out = await run(server, "echo VM_OK; hostname; uname -sr; (. /etc/os-release && echo $PRETTY_NAME)",
                              timeout=40, use_password=use_password)
        return ("VM_OK" in out and code == 0), out.replace("VM_OK\n", "").strip()
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def upload(server: db.Server, local_path: str, remote_path: str) -> None:
    """Загрузить файл на сервер по SFTP."""
    conn = await open_server(server)
    async with conn:
        async with conn.start_sftp_client() as sftp:
            await sftp.put(local_path, remote_path)
