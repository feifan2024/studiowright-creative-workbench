"""A persistent, explicitly started queue for historical material analysis."""
import json
import math
import re
import threading
import time
import uuid

from live_pool import Conflict, UPLOAD_LIMIT


SUPPORTED = {'image/jpeg', 'image/png', 'image/webp', 'video/mp4', 'video/webm'}
IMPORTED_FORMATS = SUPPORTED | {'video/quicktime'}
IMPORTED_MAX_BYTES = 2 * 1024**3


def has_labels(value):
    if isinstance(value, dict):
        return any(has_labels(v) for v in value.values())
    if isinstance(value, list):
        return any(has_labels(v) for v in value)
    return isinstance(value, str) and bool(value.strip())


class MaterialBackfill:
    def __init__(self, pool):
        self.pool = pool
        self.wake = threading.Event()
        with pool.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS material_backfill_runs(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, created REAL NOT NULL, paused_reason TEXT);
                CREATE TABLE IF NOT EXISTS material_backfill_jobs(
                    asset_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, request_id TEXT UNIQUE,
                    status TEXT NOT NULL, added REAL NOT NULL, updated REAL NOT NULL, note TEXT);
                CREATE INDEX IF NOT EXISTS material_backfill_run ON material_backfill_jobs(run_id,status);
            ''')
            db.execute('BEGIN IMMEDIATE')
            interrupted = db.execute("SELECT DISTINCT run_id FROM material_backfill_jobs WHERE status='running'").fetchall()
            if interrupted:
                db.execute("UPDATE material_backfill_jobs SET status='unknown',updated=?,note='分析中断，结果需核对，不会重复提交' WHERE status='running'", (time.time(),))
                db.executemany("UPDATE material_backfill_runs SET paused_reason='有素材分析结果未确认，请核对后再继续' WHERE id=?", [(r['run_id'],) for r in interrupted])
            db.execute('COMMIT')

    @staticmethod
    def candidates(db, items):
        result = {'untagged': [], 'unanalyzed': [], 'windows_import': []}
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            ident = item.get('id')
            if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{64}', ident) or ident in seen:
                continue
            seen.add(ident)
            mime, kind, size = item.get('mime'), item.get('media_type'), item.get('size')
            imported = any(isinstance(origin, dict) and origin.get('source') == 'windows_local'
                           for origin in item.get('origins') or ())
            supported = mime in SUPPORTED and type(size) is int and 0 < size <= UPLOAD_LIMIT
            imported_supported = imported and (supported or kind == 'video' and mime in IMPORTED_FORMATS
                                               and type(size) is int and 0 < size <= IMPORTED_MAX_BYTES)
            if (not (supported or imported_supported)
                    or (kind, mime.split('/')[0]) not in (('image', 'image'), ('video', 'video'))):
                continue
            existing_job = db.execute("""SELECT j.status,r.paused_reason FROM material_backfill_jobs j
                JOIN material_backfill_runs r ON r.id=j.run_id WHERE j.asset_id=?""", (ident,)).fetchone()
            if (db.execute('SELECT 1 FROM material_analyses WHERE asset_id=? LIMIT 1', (ident,)).fetchone()
                    or db.execute("SELECT 1 FROM auto_material_jobs WHERE asset_id=? AND status IN ('waiting','running','unknown')", (ident,)).fetchone()):
                continue
            extra = db.execute('SELECT manual_json,suggested_json FROM material_extra WHERE asset_id=?', (ident,)).fetchone()
            try:
                manual = json.loads(extra['manual_json']) if extra else {}
                suggested = json.loads(extra['suggested_json']) if extra else {}
                if not isinstance(manual, dict) or not isinstance(suggested, dict) or has_labels(suggested):
                    continue
            except (ValueError, TypeError):
                continue
            tags, structured = item.get('tags'), item.get('structured_tags')
            if not isinstance(tags, list) or not isinstance(structured, dict):
                continue
            added = item.get('added')
            if type(added) not in (int, float) or not math.isfinite(added):
                added = 0
            row = (ident, float(added))
            can_move = existing_job and existing_job['status'] == 'waiting' and existing_job['paused_reason']
            if imported_supported and (not existing_job or can_move) and not has_labels(item.get('observed_tags')):
                result['windows_import'].append(row)
            if supported and not existing_job:
                result['unanalyzed'].append(row)
                if not has_labels(tags) and not has_labels(structured) and not has_labels(manual):
                    result['untagged'].append(row)
        return result

    def preview(self, items):
        with self.pool.connection() as db:
            db.execute('BEGIN')
            candidates = self.candidates(db, items)
            db.execute('COMMIT')
        return {key: len(value) for key, value in candidates.items()}

    def state(self):
        with self.pool.connection() as db:
            run = db.execute('SELECT id,scope,created,paused_reason FROM material_backfill_runs ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
            if not run:
                return {'run': None, 'counts': {}, 'issues': []}
            counts = {r['status']: r['n'] for r in db.execute('SELECT status,count(*) AS n FROM material_backfill_jobs WHERE run_id=? GROUP BY status', (run['id'],))}
            issues = [dict(r) for r in db.execute("SELECT asset_id,status,note FROM material_backfill_jobs WHERE run_id=? AND status IN ('failed','unknown') ORDER BY updated LIMIT 100", (run['id'],))]
            return {'run': dict(run), 'counts': counts, 'issues': issues}

    def has_pending(self):
        with self.pool.connection() as db:
            return bool(db.execute("""SELECT 1 FROM material_backfill_jobs j
                JOIN material_backfill_runs r ON r.id=j.run_id
                WHERE j.status='waiting' AND r.paused_reason IS NULL LIMIT 1""").fetchone())

    def start(self, scope, request_id, items):
        self.pool.conversation_id(request_id)
        if scope not in ('untagged', 'unanalyzed', 'windows_import'):
            raise ValueError('补打范围不正确。')
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT scope FROM material_backfill_runs WHERE id=?', (request_id,)).fetchone()
            if previous:
                if previous['scope'] != scope:
                    raise Conflict('补打请求与已有记录不一致。')
                db.execute('COMMIT')
                return self.state()
            if self.pool.maintenance() or db.execute("""SELECT 1 FROM material_backfill_jobs j
                JOIN material_backfill_runs r ON r.id=j.run_id WHERE j.status='running'
                OR (j.status='waiting' AND (?!='windows_import' OR r.paused_reason IS NULL)) LIMIT 1""", (scope,)).fetchone():
                raise Conflict('已有补打任务尚未完成，请先查看进度。')
            chosen = self.candidates(db, items)[scope]
            now = time.time()
            db.execute('INSERT INTO material_backfill_runs(id,scope,created) VALUES(?,?,?)', (request_id, scope, now))
            db.executemany("""INSERT INTO material_backfill_jobs(asset_id,run_id,status,added,updated,note)
                           VALUES(?,?,'waiting',?,?,?) ON CONFLICT(asset_id) DO UPDATE
                           SET run_id=excluded.run_id,updated=excluded.updated,note=excluded.note
                           WHERE material_backfill_jobs.status='waiting'""",
                           [(ident, request_id, added, now, '等待补打') for ident, added in chosen])
            db.execute('COMMIT')
        self.wake.set()
        return self.state()

    def enqueue_project_assets(self, run_id, items):
        """Queue only newly archived, untagged assets, without touching existing labels."""
        self.pool.conversation_id(run_id)
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.pool.maintenance():
                raise Conflict('工作台维护中，素材已保存，标签尚未排队。')
            if db.execute('SELECT 1 FROM material_backfill_runs WHERE id=?', (run_id,)).fetchone():
                db.execute('COMMIT')
                return 0
            chosen = self.candidates(db, items)['untagged']
            now = time.time()
            db.execute('INSERT INTO material_backfill_runs(id,scope,created) VALUES(?,?,?)', (run_id, 'untagged', now))
            db.executemany("INSERT INTO material_backfill_jobs(asset_id,run_id,status,added,updated,note) VALUES(?,?,'waiting',?,?,?)",
                           [(ident, run_id, added, now, '等待自动打标签') for ident, added in chosen])
            db.execute('COMMIT')
        if chosen:
            self.wake.set()
        return len(chosen)

    def resume(self):
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT id FROM material_backfill_runs ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
            if self.pool.maintenance() or not run:
                raise Conflict('当前没有可继续的补打任务。')
            db.execute('UPDATE material_backfill_runs SET paused_reason=NULL WHERE id=?', (run['id'],))
            db.execute('COMMIT')
        self.wake.set()
        return self.state()

    def claim(self, accounts):
        browser_states = self.pool._read_browser_inventory() if self.pool.browser_inventory is not None else None
        if self.pool.browser_inventory is not None and browser_states is None:
            return None
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT j.asset_id,j.run_id FROM material_backfill_jobs j
                JOIN material_backfill_runs r ON r.id=j.run_id
                WHERE j.status='waiting' AND r.paused_reason IS NULL ORDER BY j.added,j.asset_id LIMIT 1""").fetchone()
            if (self.pool.maintenance() or not row
                    or db.execute("SELECT 1 FROM material_analyses WHERE status IN ('preparing','saving') LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM material_backfill_jobs WHERE status='running' LIMIT 1").fetchone()):
                db.execute('COMMIT')
                return None
            try:
                try:
                    self.pool.choose_account(db, accounts, time.time(), browser_states=browser_states)
                except Conflict:
                    self.pool.choose_account(db, accounts, time.time(), stale=True, browser_states=browser_states)
            except Conflict:
                db.execute('COMMIT')
                return None
            ident = row['asset_id']
            extra = db.execute('SELECT suggested_json FROM material_extra WHERE asset_id=?', (ident,)).fetchone()
            try:
                suggested = json.loads(extra['suggested_json']) if extra else {}
                occupied = not isinstance(suggested, dict) or has_labels(suggested)
            except (ValueError, TypeError):
                occupied = True
            if (occupied or db.execute('SELECT 1 FROM material_analyses WHERE asset_id=? LIMIT 1', (ident,)).fetchone()
                    or db.execute("SELECT 1 FROM auto_material_jobs WHERE asset_id=? AND status IN ('waiting','running','unknown')", (ident,)).fetchone()):
                db.execute("UPDATE material_backfill_jobs SET status='skipped',updated=?,note='已有分析或标签建议，未重复提交' WHERE asset_id=?", (time.time(), ident))
                db.execute('COMMIT')
                return None
            request_id = str(uuid.uuid4())
            db.execute("INSERT INTO material_analyses(request_id,asset_id,status,created) VALUES(?,?,'preparing',?)", (request_id, ident, time.time()))
            db.execute("UPDATE material_backfill_jobs SET status='running',request_id=?,updated=?,note='正在补打' WHERE asset_id=?", (request_id, time.time(), ident))
            db.execute('COMMIT')
            return {'asset_id': ident, 'request_id': request_id}

    def finish(self, request_id):
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,note FROM material_analyses WHERE request_id=?', (request_id,)).fetchone()
            job = db.execute("SELECT run_id FROM material_backfill_jobs WHERE request_id=? AND status='running'", (request_id,)).fetchone()
            status = 'completed' if row and row['status'] == 'completed' else ('unknown' if not row or row['status'] in ('unknown','preparing','saving') else 'failed')
            note = row['note'] if row and row['note'] else '分析结果需核对'
            db.execute("UPDATE material_backfill_jobs SET status=?,updated=?,note=? WHERE request_id=? AND status='running'", (status, time.time(), note, request_id))
            if job and status != 'completed':
                db.execute("UPDATE material_backfill_runs SET paused_reason='有素材分析未完成，请核对后再继续；不会自动重试' WHERE id=?", (job['run_id'],))
            db.execute('COMMIT')
        return status
