"""Sequential, login-only activation of enrolled Gemini accounts."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from official_runtime import DENY


class GeminiAuthorization:
    def __init__(self, reauth):
        self.reauth = reauth
        self.pool = reauth.pool
        self.runtime = reauth.runtime
        self.lock = threading.Lock()
        self.progress = {'state': 'idle', 'completed': 0, 'total': 0}

    def status(self):
        with self.lock:
            return dict(self.progress)

    def update(self, **values):
        with self.lock:
            self.progress.update(values)

    def allowed(self, key, *, login_only=False):
        from official_pool import busy_official, unresolved_official
        with self.pool.connection() as db:
            pref = db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?', (key,)).fetchone()
            if self.pool.official.reason(db, key, self.runtime.inventory()) is None:
                return False
            return (not self.pool.maintenance() and not (pref and not pref[0])
                    and not busy_official(db, key) and (login_only or not unresolved_official(db, key)))

    def candidates(self):
        from official_reauth import TOKEN
        config = self.runtime.settings()
        if not config:
            return []
        probes = {row['account_key']: row['request_id'] for row in self.reauth.dashboard()['items']
                  if row.get('mode') == 'login_only' and row.get('can_start')}
        result = []
        for key, entry in config['accounts'].items():
            origin = Path(entry['root']) / TOKEN
            missing = not origin.exists() and not origin.is_symlink()
            if (missing or key in probes) and self.allowed(key, login_only=True):
                result.append((key, entry, config['binary'], None if missing else probes[key]))
        return result

    def start(self):
        with self.lock:
            if self.progress['state'] in ('starting', 'waiting'):
                return dict(self.progress)
            self.progress = {'state': 'starting', 'completed': 0, 'total': 0}
            result = dict(self.progress)
            threading.Thread(target=self.run, daemon=True, name='gemini-login-only').start()
            return result

    def run(self):
        from official_reauth import TTL
        try:
            pending = self.candidates()
            self.update(total=len(pending))
            for index, (key, entry, binary, request) in enumerate(pending):
                if not self.allowed(key, login_only=True):
                    raise ValueError('账号状态已变化，未继续授权。')
                self.update(state='waiting', email=entry['email'], completed=index)
                if request:
                    self.reauth.start(request, login_only=True)
                    for _ in range(TTL + 5):
                        with self.pool.connection() as db:
                            row = db.execute('SELECT state FROM official_reauth WHERE request_id=?', (request,)).fetchone()
                        if row and row[0] == 'completed':
                            break
                        if not row or row[0] != 'waiting':
                            raise ValueError('本次授权未完成，请重新检查。')
                        time.sleep(1)
                    else:
                        raise ValueError('授权等待超时，请重新检查。')
                else:
                    self.bootstrap(key, entry, binary)
                self.update(completed=index + 1)
            self.update(state='completed', email=None)
        except ValueError as exc:
            self.update(state='stopped', message=str(exc))
        except Exception:
            self.update(state='stopped', message='暂时无法完成授权，请确认 Ubuntu 桌面已登录后重试。')

    def bootstrap(self, key, entry, binary):
        from official_reauth import TOKEN, TTL, close_authorization_tab, private_bytes
        root = Path(entry['root'])
        origin = root / TOKEN
        if (not root.is_absolute() or root.is_symlink() or origin.exists() or origin.is_symlink()
                or not (root/'workspace').is_dir()):
            raise ValueError('账号授权位置已变化，未覆盖原有资料。')
        policy = json.loads(private_bytes(origin.parent/'settings.json', 65536))
        if not DENY <= set(policy.get('permissions', {}).get('deny', [])):
            raise ValueError('账号配置未通过检查，未打开授权。')
        stage = Path(tempfile.mkdtemp(prefix='workbench-bootstrap-', dir='/dev/shm'))
        try:
            (stage/TOKEN).parent.mkdir(parents=True, mode=0o700)
            (stage/'workspace').mkdir(mode=0o700)
            for name in ('settings.json', 'jetski_state.pbtxt', 'cache/onboarding.json'):
                src = origin.parent/name
                if src.is_file():
                    dst = (stage/TOKEN).parent/name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(private_bytes(src, 1024*1024))
                    dst.chmod(0o600)
            self.reauth.launch(stage, {'binary': binary, 'email': entry['email']},
                               browser_only=True, account_key=key)
            for _ in range(TTL + 5):
                token_path = stage/TOKEN
                if token_path.is_file():
                    try:
                        token = private_bytes(token_path, 100000)
                        identity = json.loads(token).get('id_token_email', '').lower()
                    except (OSError, ValueError, AttributeError):
                        identity = ''
                    if identity:
                        if identity != entry['email'].lower():
                            raise ValueError('登录邮箱与该账号不一致，未保存授权。')
                        config = self.runtime.settings()
                        if (not config or config['accounts'].get(key) != entry
                                or not self.allowed(key, login_only=True)):
                            raise ValueError('账号状态已变化，未保存授权。')
                        fd, temporary = tempfile.mkstemp(prefix='.bootstrap-', dir=origin.parent)
                        try:
                            with os.fdopen(fd, 'wb') as stream:
                                stream.write(token)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.link(temporary, origin)
                        finally:
                            Path(temporary).unlink(missing_ok=True)
                        if not self.runtime.account(key):
                            raise ValueError('新授权未通过本地核对。')
                        close_authorization_tab(stage, key)
                        from official_quota import reader_for
                        reader_for(self.runtime).refresh_after_authorization(key)
                        return
                if (stage/'closed').exists():
                    raise ValueError('登录窗口已关闭或未打开，未保存授权。')
                time.sleep(1)
            raise ValueError('授权等待超时，请重新检查。')
        finally:
            if stage.parent == Path('/dev/shm') and stage.name.startswith('workbench-bootstrap-'):
                shutil.rmtree(stage, ignore_errors=True)
