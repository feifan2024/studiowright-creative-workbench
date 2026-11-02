"""Preserve Chrome's encrypted account tokens, without copying autofill/passwords."""
import hashlib
import shutil
import sqlite3
from pathlib import Path


def copy_login_tokens(source, target):
    source, target = Path(source), Path(target)
    if not source.is_file():
        return 0
    scratch = target.parent / ".login-database-snapshot"
    scratch.mkdir(mode=0o700)
    try:
        # Chrome can hold Web Data exclusively. Snapshot database and rollback
        # journal only when both are stable; recover on our own copy, never source.
        names = [source, source.with_name(source.name+"-journal"), source.with_name(source.name+"-wal")]
        def fingerprints():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in names if p.is_file()}
        before=fingerprints()
        for path in names:
            if path.is_file():
                shutil.copyfile(path,scratch/path.name)
        copied={p.name:hashlib.sha256((scratch/p.name).read_bytes()).hexdigest() for p in names if (scratch/p.name).is_file()}
        if before != copied or before != fingerprints():
            raise ValueError("浏览器正在修改登录资料，本次未覆盖账号。")
        with sqlite3.connect(scratch/source.name,timeout=3) as src:
            if src.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError("登录资料快照未通过完整性检查。")
            schema=src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='token_service'").fetchone()
            if not schema:
                return 0
            records=src.execute('SELECT * FROM token_service').fetchall()
            with sqlite3.connect(target) as out:
                for table in ('meta','token_service'):
                    sql=src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
                    if not sql:
                        continue
                    out.execute(sql[0])
                    rows=records if table=='token_service' else src.execute('SELECT * FROM meta').fetchall()
                    if rows:
                        out.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
        target.chmod(0o600)
        return len(records)
    finally:
        shutil.rmtree(scratch)
