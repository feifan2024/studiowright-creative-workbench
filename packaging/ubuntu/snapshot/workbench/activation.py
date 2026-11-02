"""One explicit activation batch, with durable, one-shot cooldown recovery."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path


class Activation:
    def __init__(self, state_dir, program_dir, sources, pool, opener=None, start_worker=True):
        self.path = Path(state_dir) / 'activation.sqlite3'
        self.program_dir = Path(program_dir)
        self.sources, self.pool = sources, pool
        self.opener = opener or self.open_browser
        self.guard = threading.RLock()
        self.next_watch = 0.0
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS items(batch_id TEXT NOT NULL, account_key TEXT NOT NULL,
                status TEXT NOT NULL, due REAL, message TEXT NOT NULL, PRIMARY KEY(batch_id,account_key));''')
            db.executescript('''CREATE TABLE IF NOT EXISTS activation_schedule(
              id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
              daily_time TEXT NOT NULL DEFAULT '08:00', next_due REAL, last_due REAL,
              last_outcome TEXT, last_note TEXT, flow_hold INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS activation_schedule_holds(
              account_key TEXT PRIMARY KEY, reason TEXT NOT NULL, created REAL NOT NULL);''')
            db.execute('INSERT OR IGNORE INTO activation_schedule(id) VALUES(1)')
            db.executescript('''CREATE TABLE IF NOT EXISTS activation_poll(
              id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
              interval_minutes INTEGER NOT NULL DEFAULT 30, next_due REAL, last_due REAL,
              last_account TEXT, last_outcome TEXT, last_note TEXT);
              INSERT OR IGNORE INTO activation_poll(id) VALUES(1);''')
            if 'origin' not in {r[1] for r in db.execute('PRAGMA table_info(batches)')}:
                db.execute("ALTER TABLE batches ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
            if 'flow_refresh' not in {r[1] for r in db.execute('PRAGMA table_info(batches)')}:
                db.execute("ALTER TABLE batches ADD COLUMN flow_refresh TEXT NOT NULL DEFAULT 'pending'")
            if 'gemini_checked' not in {r[1] for r in db.execute('PRAGMA table_info(items)')}:
                db.execute('ALTER TABLE items ADD COLUMN gemini_checked INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE items SET gemini_checked=1 WHERE status='done'")
            # Preserve the safety gate across restarts for operations already started.
            db.execute("""INSERT OR IGNORE INTO activation_schedule_holds(account_key,reason,created)
                          SELECT i.account_key,'定时检查中断，需手动核对',? FROM items i
                          JOIN batches b ON b.id=i.batch_id WHERE b.origin IN ('scheduled','poll') AND i.status='processing'""", (time.time(),))
            if db.execute("SELECT 1 FROM batches WHERE flow_refresh='issuing' LIMIT 1").fetchone():
                db.execute('UPDATE activation_schedule SET flow_hold=1 WHERE id=1')
            db.execute("UPDATE activation_schedule SET enabled=0,next_due=NULL,last_outcome='interrupted',last_note='启动时工作台中断，定时激活已暂停，请核查' WHERE last_outcome='starting'")
            db.execute("UPDATE activation_poll SET enabled=0,next_due=NULL,last_outcome='interrupted',last_note='启动时工作台中断，轮询已暂停，请核查' WHERE last_outcome='starting'")
            # An interrupted operation is never submitted again after restart.
            db.execute("UPDATE items SET status='interrupted', message='上次操作中断，结果待核对；没有自动重试' WHERE status IN ('queued','processing')")
            db.execute("UPDATE batches SET flow_refresh='unknown' WHERE flow_refresh='issuing'")
        if start_worker:
            self.thread = threading.Thread(target=self.run, name='account-activation', daemon=True)
            self.thread.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self):
        snapshot = self.sources.snapshot()
        accounts = snapshot.get('real_accounts', {})
        if not accounts.get('available') or not isinstance(accounts.get('items'), list):
            raise ValueError('账号状态暂不可读取')
        self.pool.annotate(accounts)
        return snapshot

    def status(self):
        with self.db() as db:
            batch = db.execute('SELECT id,created,flow_refresh FROM batches ORDER BY created DESC LIMIT 1').fetchone()
            if not batch:
                return {'status': 'idle', 'items': []}
            items = [dict(r) for r in db.execute('SELECT account_key,status,due,message FROM items WHERE batch_id=? ORDER BY rowid', (batch['id'],))]
        return {'status': 'running' if any(i['status'] in ('queued','processing','cooling') for i in items) else 'finished',
                'created': batch['created'], 'flow_refresh': batch['flow_refresh'], 'items': items}

    @staticmethod
    def next_daily(daily_time, now):
        hour, minute = map(int, daily_time.split(':'))
        tz = timezone(timedelta(hours=8))
        current = datetime.fromtimestamp(now, tz)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()

    def schedule_status(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM activation_schedule WHERE id=1').fetchone()
            held = db.execute('SELECT count(*) FROM activation_schedule_holds').fetchone()[0]
        return {'enabled':bool(row['enabled']), 'time':row['daily_time'], 'timezone':'Asia/Shanghai',
                'next_due':row['next_due'], 'last_due':row['last_due'], 'last_outcome':row['last_outcome'],
                'last_note':row['last_note'], 'held_accounts':held, 'flow_hold':bool(row['flow_hold'])}

    def set_schedule(self, enabled, daily_time):
        if type(enabled) is not bool or not isinstance(daily_time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', daily_time):
            raise ValueError('请选择正确的定时时间')
        with self.guard:
            if self.pool.maintenance():
                raise ValueError('工作台正在更新，请稍后操作')
            with self.db() as db:
                db.execute('UPDATE activation_schedule SET enabled=?,daily_time=?,next_due=? WHERE id=1',
                           (int(enabled), daily_time, self.next_daily(daily_time,time.time()) if enabled else None))
        self.wake_event.set()
        return self.schedule_status()

    def schedule_tick(self, now=None):
        now = time.time() if now is None else now
        with self.guard:
            if self.pool.maintenance():
                return
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM activation_schedule WHERE id=1').fetchone()
                if not row['enabled'] or row['next_due'] is None or row['next_due'] > now:
                    return
                due = row['next_due']
                next_due = self.next_daily(row['daily_time'], now)
                # Claim the date before taking action; never replay an interrupted or missed run.
                outcome = 'missed' if now - due > 120 else 'starting'
                note = '错过预定时间，本次跳过，不补跑' if outcome == 'missed' else '正在准备定时激活'
                db.execute('UPDATE activation_schedule SET next_due=?,last_due=?,last_outcome=?,last_note=? WHERE id=1',
                           (next_due,due,outcome,note))
            if outcome == 'missed':
                return
            if self.status()['status'] == 'running':
                with self.db() as db:
                    db.execute("UPDATE activation_schedule SET last_outcome='skipped',last_note='已有激活正在运行，本次跳过' WHERE id=1")
                return
            try:
                self.start(origin='scheduled')
                with self.db() as db:
                    db.execute("UPDATE activation_schedule SET last_outcome='started',last_note='定时激活已启动，查看本轮账号结果' WHERE id=1")
            except Exception:
                with self.db() as db:
                    db.execute("UPDATE activation_schedule SET enabled=0,next_due=NULL,last_outcome='failed',last_note='定时激活未能开始，已暂停且不会自动重试' WHERE id=1")

    @staticmethod
    def poll_delay(minutes):
        # Independent integer jitter in [85%, 115%], chosen and persisted per slot.
        base = minutes * 60
        spread = base * 15 // 100
        return base - spread + secrets.randbelow(2 * spread + 1)

    def poll_status(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM activation_poll WHERE id=1').fetchone()
            held = db.execute('SELECT count(*) FROM activation_schedule_holds').fetchone()[0]
        return {'enabled':bool(row['enabled']), 'interval_minutes':row['interval_minutes'], 'jitter_percent':15,
                'next_due':row['next_due'], 'last_due':row['last_due'], 'last_account':row['last_account'],
                'last_outcome':row['last_outcome'], 'last_note':row['last_note'], 'held_accounts':held}

    def set_poll(self, enabled, interval_minutes):
        if type(enabled) is not bool or type(interval_minutes) is not int or not 1 <= interval_minutes <= 1440:
            raise ValueError('请输入 1 至 1440 分钟的轮询间隔')
        with self.guard:
            if self.pool.maintenance():
                raise ValueError('工作台正在更新，请稍后操作')
            with self.db() as db:
                previous = db.execute('SELECT enabled,interval_minutes,next_due FROM activation_poll WHERE id=1').fetchone()
                due = (previous['next_due'] if enabled and previous['enabled'] and previous['interval_minutes'] == interval_minutes
                       else time.time()+self.poll_delay(interval_minutes) if enabled else None)
                db.execute('UPDATE activation_poll SET enabled=?,interval_minutes=?,next_due=? WHERE id=1',
                           (int(enabled),interval_minutes,due))
        self.wake_event.set()
        return self.poll_status()

    def poll_tick(self, now=None):
        now = time.time() if now is None else now
        with self.guard:
            if self.pool.maintenance():
                return
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM activation_poll WHERE id=1').fetchone()
                if not row['enabled'] or row['next_due'] is None or row['next_due'] > now:
                    return
                missed = now - row['next_due'] > 120
                db.execute('UPDATE activation_poll SET next_due=?,last_due=?,last_outcome=?,last_note=? WHERE id=1',
                           (now+self.poll_delay(row['interval_minutes']),row['next_due'],
                            'missed' if missed else 'starting', '错过轮询时间，本次跳过，不补跑' if missed else '正在准备轮询'))
            if missed:
                return
            if self.status()['status'] == 'running':
                with self.db() as db:
                    db.execute("UPDATE activation_poll SET last_outcome='skipped',last_note='已有激活正在运行，本次跳过' WHERE id=1")
                return
            try:
                self.start(origin='poll')
                with self.db() as db:
                    db.execute("UPDATE activation_poll SET last_outcome='started',last_note='已安排一个账号，查看本轮账号结果' WHERE id=1")
            except Exception:
                with self.db() as db:
                    db.execute("UPDATE activation_poll SET enabled=0,next_due=NULL,last_outcome='failed',last_note='轮询未能开始，已暂停且不会自动重试' WHERE id=1")

    def start(self, origin='manual'):
        if origin not in ('manual','scheduled','poll'):
            raise ValueError('激活方式不正确')
        with self.guard:
            if self.pool.maintenance():
                raise ValueError('工作台正在更新，请稍后操作')
            if self.status()['status'] == 'running':
                raise ValueError('已有一轮激活正在进行或等待冷却')
            snapshot = self.snapshot()
            items = snapshot['real_accounts']['items']
            keys = [a.get('key', '').removeprefix('flow-account-') for a in items]
            if not keys or len(keys) > 50 or len(set(keys)) != len(keys) or any(not re.fullmatch(r'[0-9a-f]{64}', k) for k in keys):
                raise ValueError('现有账号列表不能唯一确认')
            batch = str(uuid.uuid4())
            flow_refresh = 'pending'
            if origin == 'poll':
                keys.sort()
                with self.db() as db:
                    previous = db.execute('SELECT last_account FROM activation_poll WHERE id=1').fetchone()[0]
                keys = [next((key for key in keys if previous is None or key > previous), keys[0])]
                # The Flow service already refreshes balances; never trigger a pool-wide refresh per account.
                flow_refresh = 'skipped_poll'
            if origin == 'scheduled':
                if any(t.get('status') in ('queued','running') for t in snapshot.get('tasks', [])):
                    flow_refresh = 'skipped_busy'
                elif all(a.get('flow_balance_fresh') is True for a in items):
                    flow_refresh = 'skipped_fresh'
                elif any(a.get('flow_login') != 'verified' or not a.get('flow_pacing_known') or
                         not a.get('flow_risk_known') or a.get('flow_risk_paused') or a.get('flow_cooling') or
                         a.get('gemini_unresolved') or a.get('gemini_busy') for a in items):
                    flow_refresh = 'skipped_safety'
            with self.db() as db:
                db.execute('INSERT INTO batches(id,created,origin,flow_refresh) VALUES (?,?,?,?)', (batch,time.time(),origin,flow_refresh))
                db.executemany('INSERT INTO items(batch_id,account_key,status,due,message) VALUES (?,?,?,NULL,?)', [(batch,k,'queued','等待逐个核对') for k in keys])
                if origin == 'poll':
                    db.execute('UPDATE activation_poll SET last_account=? WHERE id=1', (keys[0],))
            self.wake_event.set()
            return self.status()

    def update(self, batch, key, status, message, due=None, checked=None):
        with self.db() as db:
            db.execute('UPDATE items SET status=?,message=?,due=?,gemini_checked=COALESCE(?,gemini_checked) WHERE batch_id=? AND account_key=?',
                       (status,message,due,checked,batch,key))
            row = db.execute('SELECT origin FROM batches WHERE id=?', (batch,)).fetchone()
            if row and row['origin'] in ('scheduled','poll') and status in ('failed','interrupted'):
                db.execute('INSERT OR REPLACE INTO activation_schedule_holds(account_key,reason,created) VALUES(?,?,?)',
                           (key, '上次定时检查失败或中断，需手动核对', time.time()))
            elif row and row['origin'] == 'manual' and status == 'done':
                db.execute('DELETE FROM activation_schedule_holds WHERE account_key=?', (key,))
            if row and row['origin'] == 'poll' and status in ('done','blocked','failed','interrupted'):
                db.execute('UPDATE activation_poll SET last_outcome=?,last_note=? WHERE id=1', (status,message))

    def open_browser(self, account):
        flow_python = Path.home() / '.local/share/studiowright-evaluation/venv/bin/python'
        helper = self.program_dir / 'activation_browser.py'
        if not flow_python.is_file() or not helper.is_file() or helper.is_symlink():
            return False
        try:
            result = subprocess.run([str(flow_python), '-B', str(helper), account], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
            data = json.loads(result.stdout)
            return result.returncode == 0 and data.get('result') == 'browser_ready'
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def process_one(self, batch, key, snapshot):
        account_data = snapshot['real_accounts']
        items = account_data['items']
        matches = [a for a in items if a.get('key') == 'flow-account-' + key]
        if len(matches) != 1:
            return self.update(batch,key,'blocked','账号归属无法确认，未操作')
        a = matches[0]
        with self.db() as db:
            batch_row = db.execute('SELECT origin FROM batches WHERE id=?', (batch,)).fetchone()
            scheduled = bool(batch_row and batch_row['origin'] in ('scheduled','poll'))
            polling = bool(batch_row and batch_row['origin'] == 'poll')
            held = bool(db.execute('SELECT 1 FROM activation_schedule_holds WHERE account_key=?', (key,)).fetchone())
        if scheduled and held:
            return self.update(batch,key,'blocked','上次定时检查未完成，需先手动核对，不会自动重试')
        if scheduled and (a.get('keepalive_enabled') is False or a.get('keepalive_state') in ('paused','network_wait')):
            return self.update(batch,key,'blocked','Gemini 保活已暂停，需先手动核对')
        if a.get('gemini_enabled') is False:
            return self.update(batch,key,'blocked','Gemini 已手动暂停，未自动启用')
        if a.get('gemini_unresolved'):
            return self.update(batch,key,'blocked','有结果未确认的消息，未操作')
        if a.get('gemini_busy') or any(t.get('source_profile') == a.get('profile') and t.get('status') in ('queued','running') for t in snapshot.get('tasks', [])):
            return self.update(batch,key,'blocked','账号正在处理任务，未操作')
        if a.get('flow_login') != 'verified' or not a.get('flow_pacing_known') or not a.get('flow_risk_known'):
            return self.update(batch,key,'blocked','Flow 登录或状态无法确认，未操作')
        if a.get('flow_risk_paused'):
            return self.update(batch,key,'blocked','Flow 已风控暂停，请先人工核查')
        if a.get('flow_cooling'):
            if polling:
                return self.update(batch,key,'blocked','Flow 冷却中，本次跳过，到期后再检查')
            seconds = a.get('flow_cooldown_seconds')
            if type(seconds) not in (int,float) or seconds <= 0:
                return self.update(batch,key,'blocked','冷却结束时间不明，未自动恢复')
            return self.update(batch,key,'cooling','Flow 冷却中，到期后核对状态并恢复',time.time()+seconds)
        browsers = self.pool._read_browser_inventory()
        if browsers is None or key not in browsers:
            return self.update(batch,key,'blocked','账号浏览器状态不明，未打开')
        if browsers[key] is False:
            if not self.opener(key):
                return self.update(batch,key,'failed','浏览器未能打开，已停止，不会自动重试')
        with self.db() as db:
            prior = db.execute('SELECT gemini_checked FROM items WHERE batch_id=? AND account_key=?', (batch,key)).fetchone()
        if prior and prior['gemini_checked']:
            return self.update(batch,key,'done','Flow 冷却已结束，账号浏览器就绪；保留上次 Gemini 核对结果')
        snapshot = self.snapshot()
        items = snapshot['real_accounts']['items']
        current = [x for x in items if x.get('key') == 'flow-account-' + key]
        if (len(current) != 1 or current[0].get('gemini_unresolved') or current[0].get('gemini_busy')
                or current[0].get('gemini_enabled') is False
                or any(t.get('source_profile') == a.get('profile') and t.get('status') in ('queued','running') for t in snapshot.get('tasks', []))):
            return self.update(batch,key,'blocked','浏览器已就绪；账号有任务或状态已变化，Gemini 检查留待处理')
        fresh_at = current[0].get('gemini_usage_checked')
        if (scheduled and current[0].get('gemini') == 'verified' and current[0].get('keepalive_state') == 'ready'
                and type(fresh_at) in (int,float) and 0 <= time.time()-fresh_at <= 900):
            return self.update(batch,key,'done','Flow 状态已读取；Gemini 近期已核对，本次不重复检查',checked=1)
        result = self.pool.check(key, items)
        if result.get('verified') and result.get('quota_available'):
            return self.update(batch,key,'done','Flow 状态已读取；Gemini 登录和额度已核对',checked=1)
        message = result.get('message') or 'Gemini 检查未完成，不会自动重试'
        self.update(batch,key,'failed',str(message)[:200])

    def watch_cooldowns(self):
        """Track new cooldown episodes without repeating Gemini checks."""
        with self.db() as db:
            batch = db.execute('SELECT id,origin FROM batches ORDER BY created DESC LIMIT 1').fetchone()
            if not batch or batch['origin'] == 'poll':
                return
            keys = {r[0] for r in db.execute("SELECT account_key FROM items WHERE batch_id=? AND status='done'", (batch['id'],))}
        if not keys:
            return
        snapshot = self.snapshot()
        for account in snapshot['real_accounts']['items']:
            key = account.get('key','').removeprefix('flow-account-')
            seconds = account.get('flow_cooldown_seconds')
            if (key in keys and account.get('flow_cooling') is True and account.get('flow_pacing_known') is True
                    and account.get('flow_risk_known') is True and account.get('flow_risk_paused') is False
                    and type(seconds) in (int,float) and seconds > 0):
                self.update(batch['id'],key,'cooling','Flow 冷却中，到期后核对状态并恢复',time.time()+seconds)

    def tick(self):
        if self.pool.maintenance():
            return
        self.schedule_tick()
        self.poll_tick()
        with self.guard:
            with self.db() as db:
                batch = db.execute("SELECT id,origin FROM batches WHERE flow_refresh='pending' AND EXISTS (SELECT 1 FROM items WHERE items.batch_id=batches.id AND status IN ('queued','processing','cooling')) ORDER BY created LIMIT 1").fetchone()
                if batch and batch['origin'] == 'scheduled' and db.execute('SELECT flow_hold FROM activation_schedule WHERE id=1').fetchone()[0]:
                    db.execute("UPDATE batches SET flow_refresh='held' WHERE id=?", (batch['id'],))
                    batch = None
                if batch:
                    db.execute("UPDATE batches SET flow_refresh='issuing' WHERE id=?", (batch['id'],))
            if batch:
                try:
                    acknowledged = self.sources.refresh_flow_balances_once()
                except Exception:
                    acknowledged = False
                with self.db() as db:
                    db.execute('UPDATE batches SET flow_refresh=? WHERE id=?', ('accepted' if acknowledged else 'unknown',batch['id']))
                    if not acknowledged:
                        db.execute('UPDATE activation_schedule SET flow_hold=1 WHERE id=1')
                    elif batch['origin'] == 'manual' and acknowledged:
                        db.execute('UPDATE activation_schedule SET flow_hold=0 WHERE id=1')
            with self.db() as db:
                row = db.execute("SELECT batch_id,account_key,status,due FROM items WHERE status='queued' OR (status='cooling' AND due<=?) ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END,rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                if time.monotonic() >= self.next_watch:
                    self.next_watch = time.monotonic() + 30
                    self.watch_cooldowns()
                return
            batch,key = row['batch_id'],row['account_key']
            # Reserve before any action. A restart leaves this interrupted, never retried.
            self.update(batch,key,'processing','正在核对账号状态')
        try:
            self.process_one(batch,key,self.snapshot())
        except Exception:
            self.update(batch,key,'failed','状态检查未完成，已停止，不会自动重试')

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self.wake_event.wait(5)
            self.wake_event.clear()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
