"""Local API-key verification and separately protected recoverable copies."""
import hashlib
import hmac
from contextlib import closing
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time


class UnifiedKeys:
    def __init__(self, path):
        self.path = Path(path)
        self.vault = self.path.with_name(self.path.name + '.vault')

    def connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('密钥文件不可用。')
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('密钥文件不可用。')
        if os.name != 'nt':
            os.chmod(self.path, 0o600)
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        db.execute('CREATE TABLE IF NOT EXISTS access_keys (id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, suffix TEXT NOT NULL, created REAL NOT NULL, revoked REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS access_key_secrets (id TEXT PRIMARY KEY, ciphertext BLOB NOT NULL)')
        db.commit()
        return db

    def _cipher(self, db, create=False):
        # A separate per-installation key is readable only by the OS user.
        # Never silently create a new key if encrypted copies already exist.
        from cryptography.fernet import Fernet
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(self.vault, flags)
        except FileNotFoundError:
            if not create or db.execute('SELECT 1 FROM access_key_secrets LIMIT 1').fetchone():
                raise ValueError('密钥保护文件不可用，无法复制。') from None
            material = Fernet.generate_key()
            try:
                fd = os.open(self.vault, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            except FileExistsError:
                fd = os.open(self.vault, flags)
            else:
                with os.fdopen(fd, 'wb') as out:
                    out.write(material)
                    out.flush()
                    os.fsync(out.fileno())
                return Fernet(material)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                raise ValueError('密钥保护文件权限不正确。')
            material = source.read(128)
        try:
            return Fernet(material)
        except (ValueError, TypeError):
            raise ValueError('密钥保护文件不可用，无法复制。') from None

    def list(self):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT a.id,a.name,a.suffix,a.created, '
                              'CASE WHEN s.id IS NOT NULL THEN 1 ELSE 0 END AS copyable '
                              'FROM access_keys a LEFT JOIN access_key_secrets s ON s.id=a.id '
                              'WHERE a.revoked IS NULL ORDER BY a.created DESC')
            return [dict(r) for r in rows]

    def create(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48 or re.search(r'[\x00-\x1f\x7f]', name):
            raise ValueError('请输入 1 至 48 个字的密钥名称。')
        name = name.strip()
        token = 'swu_' + secrets.token_urlsafe(32)
        ident = secrets.token_hex(16)
        created = time.time()
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            ciphertext = self._cipher(db, create=True).encrypt(token.encode('ascii'))
            db.execute('INSERT INTO access_keys(id,name,digest,suffix,created) VALUES(?,?,?,?,?)', (ident, name, digest, token[-4:], created))
            db.execute('INSERT INTO access_key_secrets(id,ciphertext) VALUES(?,?)', (ident, ciphertext))
            db.commit()
        return {'id': ident, 'name': name, 'suffix': token[-4:], 'created': created, 'key': token}

    def reveal(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise ValueError('密钥编号不正确。')
        from cryptography.fernet import InvalidToken
        with closing(self.connect()) as db:
            row = db.execute('SELECT a.digest,s.ciphertext FROM access_keys a '
                             'LEFT JOIN access_key_secrets s ON s.id=a.id '
                             'WHERE a.id=? AND a.revoked IS NULL', (ident,)).fetchone()
            if not row:
                raise ValueError('密钥不存在或已撤销。')
            if row['ciphertext'] is None:
                raise ValueError('旧密钥未保存原文，无法复制，请新建密钥。')
            try:
                token = self._cipher(db).decrypt(row['ciphertext']).decode('ascii')
            except (InvalidToken, UnicodeError):
                raise ValueError('密钥保护文件不可用，无法复制。') from None
            if not re.fullmatch(r'swu_[A-Za-z0-9_-]{43}', token) or not hmac.compare_digest(
                    hashlib.sha256(token.encode('ascii')).hexdigest(), row['digest']):
                raise ValueError('密钥内容校验失败，无法复制。')
            return token

    def revoke(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise ValueError('密钥编号不正确。')
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM access_key_secrets WHERE id=?', (ident,))
            result = db.execute('DELETE FROM access_keys WHERE id=?', (ident,))
            db.commit()
        return result.rowcount == 1

    def purge_revoked(self):
        """Remove previously revoked records; active keys are untouched."""
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM access_key_secrets WHERE id IN (SELECT id FROM access_keys WHERE revoked IS NOT NULL)')
            result = db.execute('DELETE FROM access_keys WHERE revoked IS NOT NULL')
            db.commit()
        return result.rowcount

    def allows(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'swu_[A-Za-z0-9_-]{43}', token):
            return False
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with closing(self.connect()) as db:
            return db.execute('SELECT 1 FROM access_keys WHERE digest=? AND revoked IS NULL', (digest,)).fetchone() is not None
