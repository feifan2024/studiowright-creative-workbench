"""Revocable client keys. Only a digest is persisted; admin sessions manage keys."""
import secrets
import time
import re


class APIKeys:
    def __init__(self, store):
        self.store = store
        store.execute("""CREATE TABLE IF NOT EXISTS api_keys(
          id TEXT PRIMARY KEY,hash TEXT UNIQUE,name TEXT,scope TEXT,
          created REAL,last_used REAL,revoked REAL)""")

    def list(self):
        return self.store.rows("SELECT id,name,scope,created,last_used,revoked FROM api_keys ORDER BY created DESC")

    def create(self, name, scope):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or scope not in ("read", "generate"):
            raise ValueError("填写密钥名称（1～80 字），并选择读取或生成权限。")
        ident, secret = secrets.token_hex(12), "swk_" + secrets.token_urlsafe(40)
        self.store.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,NULL,NULL)", (ident, self.store.digest(secret), name.strip(), scope, time.time()))
        return {"id": ident, "name": name.strip(), "scope": scope, "key": secret, "message": "密钥仅完整显示一次，请保存在调用项目的私密配置中。"}

    def verify(self, secret):
        if not secret.startswith("swk_") or len(secret) > 100:
            return None
        rows = self.store.rows("SELECT id,scope FROM api_keys WHERE hash=? AND revoked IS NULL", (self.store.digest(secret),))
        if not rows:
            return None
        row = rows[0]
        self.store.execute("UPDATE api_keys SET last_used=? WHERE id=?", (time.time(), row["id"]))
        return row

    def revoke(self, ident):
        return self.store.execute("UPDATE api_keys SET revoked=? WHERE id=? AND revoked IS NULL", (time.time(), ident))

    @staticmethod
    def permits(key, method, path):
        if method in ("GET", "HEAD"):
            return path == "/api/state" or path.startswith("/api/library") and not path.startswith("/api/library/config") or path.startswith("/api/material-library/assets") or path == "/api/material-library/facets" or bool(re.fullmatch(r"/api/tasks/[^/]+(?:/files/\d+)?", path))
        return key["scope"] == "generate" and method == "POST" and (path == "/api/tasks" or bool(re.fullmatch(r"/api/tasks/[^/]+/(?:cancel|retry)", path)))
