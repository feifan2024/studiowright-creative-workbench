import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path


MAX_GENERATION_RETRIES = 3
SAFE_VIDEO_RETRY = frozenset(('transient_before_submit', 'provider_rejected_unusual'))
RETRY_KINDS = frozenset(('image', 'video'))


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "console.db"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts(profile TEXT PRIMARY KEY, label TEXT, email TEXT, checked REAL, state TEXT DEFAULT 'unknown', error TEXT);
            CREATE TABLE IF NOT EXISTS projects(profile TEXT, id TEXT, title TEXT, checked REAL, PRIMARY KEY(profile,id));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, profile TEXT, project TEXT, kind TEXT, params TEXT, status TEXT, result TEXT, error TEXT, created REAL, started REAL, finished REAL, request_key TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS flow_retry_roots(root_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS flow_retry_attempts(task_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, ordinal INTEGER NOT NULL, reason TEXT NOT NULL, UNIQUE(root_id, ordinal));
            CREATE TABLE IF NOT EXISTS flow_retry_projects(profile TEXT PRIMARY KEY, project TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets(hash TEXT PRIMARY KEY, expires REAL);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, expires REAL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def rows(self, sql, args=()):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def accounts(self):
        return self.rows("SELECT * FROM accounts ORDER BY profile")

    def account(self, profile):
        rows = self.rows("SELECT * FROM accounts WHERE profile=?", (profile,))
        return rows[0] if rows else None

    def add_account(self, profile, label):
        self.execute(
            "INSERT INTO accounts(profile,label) VALUES(?,?) ON CONFLICT(profile) DO UPDATE SET label=excluded.label",
            (profile, label),
        )

    def update_account(self, profile, email, state, error=None):
        self.execute(
            "UPDATE accounts SET email=?,state=?,error=?,checked=? WHERE profile=?",
            (email, state, error, time.time(), profile),
        )

    def project(self, profile, project, title="已登记项目"):
        self.execute(
            "INSERT INTO projects VALUES(?,?,?,?) ON CONFLICT(profile,id) DO UPDATE SET title=excluded.title,checked=excluded.checked",
            (profile, project, title, time.time()),
        )

    def has_project(self, profile, project):
        return bool(
            self.rows("SELECT id FROM projects WHERE profile=? AND id=?", (profile, project))
        )

    def projects(self, profile=None):
        return self.rows(
            "SELECT * FROM projects"
            + (" WHERE profile=?" if profile else "")
            + " ORDER BY checked DESC,id",
            (profile,) if profile else (),
        )

    @staticmethod
    def decode(row):
        for key in ("params", "result"):
            row[key] = json.loads(row[key]) if row[key] else None
        return row

    def task(self, ident):
        rows = self.rows("SELECT * FROM tasks WHERE id=?", (ident,))
        return self.decode(rows[0]) if rows else None

    def tasks(self):
        # The recent window is measured in logical tasks, not internal attempts.
        return [self.decode(row) for row in self.rows("""
            SELECT t.* FROM tasks t JOIN (
                SELECT COALESCE(a.root_id,v.id) AS id, MAX(v.created) AS latest
                FROM tasks v LEFT JOIN flow_retry_attempts a ON a.task_id=v.id
                GROUP BY COALESCE(a.root_id,v.id)
                ORDER BY latest DESC, id DESC LIMIT 150
            ) recent ON recent.id=t.id ORDER BY recent.latest DESC,t.id DESC
        """)]

    def create_task(self, profile, project, kind, params, key, auto_retry=False):
        if auto_retry and (kind not in RETRY_KINDS or params.get('count') != 1):
            raise ValueError('换号重试只支持每次生成一张图片或一段视频。')
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM tasks WHERE request_key=?", (key,)).fetchone()
            if previous:
                row = self.decode(dict(previous))
                if (row["profile"], row["project"], row["kind"], row["params"]) != (
                    profile,
                    project,
                    kind,
                    params,
                ):
                    raise ValueError("同一提交编号不能用于不同任务，请重新提交。")
                enrolled = db.execute("SELECT 1 FROM flow_retry_roots WHERE root_id=?", (row["id"],)).fetchone()
                if auto_retry is not None and bool(enrolled) != bool(auto_retry):
                    raise ValueError("同一提交编号不能更改重试设置。")
                return row
            if kind == "sync":
                pending = db.execute("SELECT * FROM tasks WHERE profile=? AND kind='sync' AND status IN ('queued','running') ORDER BY created DESC LIMIT 1", (profile,)).fetchone()
                if pending:
                    return self.decode(dict(pending))
            ident = secrets.token_hex(8)
            db.execute(
                "INSERT INTO tasks(id,profile,project,kind,params,status,created,request_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    profile,
                    project,
                    kind,
                    json.dumps(params, ensure_ascii=False),
                    "queued",
                    time.time(),
                    key,
                ),
            )
            if auto_retry is True or (auto_retry is None and kind in RETRY_KINDS and params.get('count') == 1):
                db.execute("INSERT INTO flow_retry_roots VALUES(?,'active',?)", (ident, time.time()))
                db.execute("INSERT INTO flow_retry_attempts VALUES(?,?,0,'original')", (ident, ident))
        return self.task(ident)

    def recover(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute("UPDATE flow_retry_roots SET state='interrupted',updated=? WHERE root_id IN (SELECT a.root_id FROM flow_retry_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.status='running')", (now,))
            db.execute("UPDATE tasks SET status='interrupted',finished=?,error='后台曾中断；任务可能已提交，不会自动重试。请先核对原任务和项目素材。' WHERE status='running'", (now,))

    def claim(self, busy, generation_allowed=None, retry_switch_allowed=None, generation_shareable=None,
              active_generation_projects=None, request_keys=None):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT t.*,a.reason AS retry_reason FROM tasks t LEFT JOIN flow_retry_attempts a ON a.task_id=t.id WHERE t.status='queued' ORDER BY t.created"
            ).fetchall()
            row = next((r for r in rows if (request_keys is None or r['request_key'] in request_keys) and r["profile"] not in busy and
                        (r['kind'] not in ('image', 'video') or
                         (r['profile'], r['project']) not in (active_generation_projects or set())) and
                        (generation_allowed is None or r["kind"] not in ('image', 'video') or
                         r["profile"] in generation_allowed) and
                        (retry_switch_allowed is None or r['retry_reason'] != 'switch_account' or
                         r['profile'] in retry_switch_allowed)), None)
            if not row:
                return None
            db.execute(
                "UPDATE tasks SET status='running',started=? WHERE id=?", (time.time(), row["id"])
            )
        return self.task(row["id"])

    def release_unsubmitted(self, ident):
        """Return a newly claimed task before calling an adapter or provider."""
        self.execute("UPDATE tasks SET status='queued',started=NULL WHERE id=? AND status='running'", (ident,))

    def finish(self, ident, result=None, error=None, retry_evidence=None):
        """Record the outcome and next safe attempt together, at most once."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
            attempt = db.execute("SELECT * FROM flow_retry_attempts WHERE task_id=?", (ident,)).fetchone()
            if not row:
                return
            # Legacy callers can finish their own tasks as before. Retry attempts
            # may transition only once from running, including across a restart.
            if attempt and row["status"] != "running":
                return
            now = time.time()
            db.execute("UPDATE tasks SET status=?,result=?,error=?,finished=? WHERE id=?", (
                "failed" if error else "succeeded",
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error, now, ident,
            ))
            if not attempt:
                return
            root, ordinal = attempt["root_id"], attempt["ordinal"]
            if not error:
                state = 'succeeded' if result and result.get('files') else 'needs_review'
            elif retry_evidence != 'transient_before_submit' and not (
                    row['kind'] == 'video' and retry_evidence == 'provider_rejected_unusual'):
                state = 'needs_review'
            elif ordinal < MAX_GENERATION_RETRIES:
                state = 'pending_switch'
            else:
                state = 'exhausted'
            db.execute("UPDATE flow_retry_roots SET state=?,updated=? WHERE root_id=?", (state, now, root))

    def enroll_failed_video(self, ident, evidence):
        """Internal maintenance entry, after correlating proof of no output."""
        if evidence not in SAFE_VIDEO_RETRY:
            return False
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=? AND kind='video' AND status='failed' AND finished IS NOT NULL", (ident,)).fetchone()
            if (not row or json.loads(row['params']).get('count') != 1 or
                db.execute("SELECT 1 FROM flow_retry_attempts WHERE task_id=?", (ident,)).fetchone()):
                return False
            db.execute("INSERT INTO flow_retry_roots VALUES(?,'pending_switch',?)", (ident, time.time()))
            db.execute("INSERT INTO flow_retry_attempts VALUES(?,?,0,'original')", (ident, ident))
            return True

    def stop_retry_wait(self, root, state):
        if state not in ('references_unavailable', 'no_account'):
            raise ValueError('Unsupported retry stop state')
        return self.execute("UPDATE flow_retry_roots SET state=?,updated=? WHERE root_id=? AND state='pending_switch'",
                            (state,time.time(),root))

    def retry_detail(self, ident):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN')
            row = db.execute("SELECT a.root_id,a.ordinal,a.reason,r.state FROM flow_retry_attempts a JOIN flow_retry_roots r ON r.root_id=a.root_id WHERE a.task_id=?", (ident,)).fetchone()
            if not row:
                return None
            info = dict(row)
            info['attempts'] = [dict(r) for r in db.execute("""
                SELECT a.ordinal,a.reason,t.id,t.profile,t.project,t.status,t.started,t.finished,t.error
                FROM flow_retry_attempts a JOIN tasks t ON t.id=a.task_id
                WHERE a.root_id=? ORDER BY a.ordinal
            """, (info['root_id'],))]
            return info

    def pending_retry_roots(self):
        return self.rows("SELECT root_id,updated FROM flow_retry_roots WHERE state='pending_switch' ORDER BY updated")

    def expire_unstarted_switches(self, now):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT t.id,a.root_id,a.ordinal FROM tasks t
                JOIN flow_retry_attempts a ON a.task_id=t.id JOIN flow_retry_roots r ON r.root_id=a.root_id
                WHERE t.status='queued' AND t.started IS NULL AND a.reason='switch_account'
                AND r.state='active' AND t.created<?""", (now-900,)).fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET status='failed',finished=?,error=? WHERE id=?",
                           (now,'分配后账号一直不可用，本次未发送。',row['id']))
                db.execute("UPDATE flow_retry_roots SET state=?,updated=? WHERE root_id=?",
                           ('exhausted' if row['ordinal'] >= MAX_GENERATION_RETRIES else 'pending_switch', now, row['root_id']))

    def set_retry_project(self, profile, project):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if project is None:
                db.execute('DELETE FROM flow_retry_projects WHERE profile=?', (profile,))
                return
            if not db.execute('SELECT 1 FROM projects WHERE profile=? AND id=?', (profile, project)).fetchone():
                raise ValueError('该项目未登记在所选账号下。')
            db.execute('INSERT INTO flow_retry_projects VALUES(?,?,?) ON CONFLICT(profile) DO UPDATE SET project=excluded.project,updated=excluded.updated', (profile, project, time.time()))

    def retry_projects(self):
        return self.rows('SELECT profile,project,updated FROM flow_retry_projects')

    def enqueue_switch(self, root, profile, project, params, expected_ordinal):
        """Atomically create one attempt. A lost response or restart cannot add duplicates."""
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT state FROM flow_retry_roots WHERE root_id=?", (root,)).fetchone()
            if not current or current['state'] != 'pending_switch':
                return False
            candidates = db.execute("SELECT p.id FROM accounts a JOIN projects p ON p.profile=a.profile WHERE a.profile=? AND a.state='verified' AND p.title='FLOW+' || substr(a.email,1,instr(a.email,'@')-1)", (profile,)).fetchall()
            used = db.execute('SELECT 1 FROM flow_retry_attempts a JOIN tasks t ON t.id=a.task_id WHERE a.root_id=? AND t.profile=?', (root, profile)).fetchone()
            previous = db.execute('SELECT t.*,a.ordinal FROM flow_retry_attempts a JOIN tasks t ON t.id=a.task_id WHERE a.root_id=? ORDER BY a.ordinal DESC LIMIT 1', (root,)).fetchone()
            if ([r['id'] for r in candidates] != [project] or used or not previous or previous['kind'] not in RETRY_KINDS
                or previous['status'] != 'failed' or previous['ordinal']+1 != expected_ordinal
                or json.loads(previous['params']).get('count') != 1 or params.get('count') != 1
                or expected_ordinal > MAX_GENERATION_RETRIES):
                return False
            now = time.time()
            ident = secrets.token_hex(8)
            db.execute("INSERT INTO tasks(id,profile,project,kind,params,status,created,request_key) VALUES(?,?,?,?,?,'queued',?,?)",
                (ident,profile,project,previous['kind'],json.dumps(params,ensure_ascii=False),now,f"retry-{ident}"))
            db.execute("INSERT INTO flow_retry_attempts VALUES(?,?,?,'switch_account')", (ident,root,expected_ordinal))
            db.execute("UPDATE flow_retry_roots SET state='active',updated=? WHERE root_id=?", (now,root))
            return True

    def cancel(self, ident):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            changed = db.execute("UPDATE tasks SET status='cancelled',finished=? WHERE id=? AND status='queued'", (now, ident)).rowcount
            if changed:
                db.execute("UPDATE flow_retry_roots SET state='cancelled',updated=? WHERE root_id IN (SELECT root_id FROM flow_retry_attempts WHERE task_id=?)", (now, ident))
            return bool(changed)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def issue_ticket(self):
        token = secrets.token_urlsafe(32)
        self.execute("DELETE FROM tickets WHERE expires<?", (time.time(),))
        self.execute("INSERT INTO tickets VALUES(?,?)", (self.digest(token), time.time() + 300))
        return token

    def exchange(self, ticket):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "DELETE FROM tickets WHERE hash=? AND expires>? RETURNING hash",
                (self.digest(ticket), time.time()),
            ).fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?)", (self.digest(token), time.time() + 7 * 86400)
            )
            return token

    def authenticated(self, token):
        return bool(
            token
            and self.rows(
                "SELECT hash FROM sessions WHERE hash=? AND expires>?",
                (self.digest(token), time.time()),
            )
        )
