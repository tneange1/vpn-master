"""Бэкап самого бота: pg_dump всей БД + master.key → tar.gz. Восстановление обратно."""
import asyncio
import io
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, crypto


def _pg_url() -> str:
    return config.DATABASE_URL.replace("+asyncpg", "")


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def make() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="vmbak-"))
    dump = tmp / "dump.sql"
    code, out = await _run("pg_dump", "--clean", "--if-exists", "--no-owner", "--no-privileges",
                           "-f", str(dump), _pg_url())
    if code != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"pg_dump: {out[-500:]}")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = config.DATA_DIR / "backups" / f"vpn-master-{ts}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(dump, arcname="dump.sql")
        tar.add(crypto.key_path(), arcname="master.key")
        info = tarfile.TarInfo("VERSION")
        data = b"vpn-master-backup 1\n"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    shutil.rmtree(tmp, ignore_errors=True)
    # храним последние 10 локально
    olds = sorted(archive.parent.glob("vpn-master-*.tar.gz"))[:-10]
    for o in olds:
        o.unlink(missing_ok=True)
    return archive


async def restore(archive: Path) -> str:
    tmp = Path(tempfile.mkdtemp(prefix="vmres-"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
            if not {"dump.sql", "master.key"} <= names:
                raise RuntimeError("это не бэкап vpn-master (нет dump.sql / master.key)")
            for n in ("dump.sql", "master.key"):
                tar.extract(n, tmp, filter="data")
        code, out = await _run("psql", "-v", "ON_ERROR_STOP=1", "-q", "-f", str(tmp / "dump.sql"), _pg_url())
        if code != 0:
            raise RuntimeError(f"psql: {out[-600:]}")
        shutil.copyfile(tmp / "master.key", crypto.key_path())
        os.chmod(crypto.key_path(), 0o600)
        crypto.load()
        return out[-300:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
