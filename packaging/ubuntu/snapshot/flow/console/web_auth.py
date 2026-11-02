"""One local administrator; salted password hashes and bounded login attempts."""
import hashlib
import hmac
import re
import secrets
import time


class WebAuth:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS web_admin(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT, salt TEXT, password_hash TEXT);
            CREATE TABLE IF NOT EXISTS web_login_failures(at REAL);
            ''')

    def configured(self):
        return bool(self.store.rows('SELECT id FROM web_admin WHERE id=1'))

    @staticmethod
    def derive(password, salt):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()

    def verify(self, username, password):
        rows = self.store.rows('SELECT * FROM web_admin WHERE id=1')
        row = rows[0] if rows else {'username': '', 'salt': '00'*16, 'password_hash': '00'*32}
        actual = self.derive(password, row['salt'])
        return hmac.compare_digest(actual, row['password_hash']) and hmac.compare_digest(username.encode(), row['username'].encode())

    def set_credentials(self, username, password, current_password=None):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{3,64}', username):
            raise ValueError('账号需为 3～64 位字母、数字或 . _ @ -。')
        if not isinstance(password, str) or not 10 <= len(password) <= 200:
            raise ValueError('密码长度应为 10～200 个字符。')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM web_admin WHERE id=1').fetchone()
            if old and (not isinstance(current_password, str) or len(current_password)>200 or
                        not hmac.compare_digest(self.derive(current_password, old['salt']), old['password_hash'])):
                raise ValueError('当前密码不正确，未修改。')
            salt = secrets.token_hex(16)
            db.execute('INSERT OR REPLACE INTO web_admin VALUES(1,?,?,?)', (username, salt, self.derive(password, salt)))
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM web_login_failures')
            db.execute('DELETE FROM tickets')

    def new_session(self, remember):
        token = secrets.token_urlsafe(32)
        age = 30*86400 if remember else 12*3600
        self.store.execute('INSERT INTO sessions VALUES(?,?)', (self.store.digest(token), time.time()+age))
        return token, age

    def login(self, username, password, remember=False):
        if not isinstance(username, str) or not isinstance(password, str) or len(username)>64 or len(password)>200 or type(remember) is not bool:
            raise ValueError('登录信息格式不正确。')
        now = time.time()
        self.store.execute('DELETE FROM web_login_failures WHERE at<?', (now-300,))
        if len(self.store.rows('SELECT at FROM web_login_failures')) >= 10:
            raise ValueError('登录失败次数过多，请在 5 分钟后重试。')
        if not self.verify(username, password):
            self.store.execute('INSERT INTO web_login_failures VALUES(?)', (now,))
            raise ValueError('账号或密码不正确。')
        self.store.execute('DELETE FROM web_login_failures')
        return self.new_session(remember)
