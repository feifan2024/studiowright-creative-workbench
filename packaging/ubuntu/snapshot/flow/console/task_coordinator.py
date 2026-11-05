"""Persistent account-window ownership and bounded recovery; never submits work."""
import asyncio
from contextvars import ContextVar
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

AUTO_REGENERATE = False
AUTO_SWITCH_ACCOUNT = False
_current = ContextVar('flow_coordinator_task', default=None)


class CoordinationBlocked(ValueError):
    pass


class Coordinator:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path.home()/'.local/state/creative-unified-workbench/task-coordination'
        if self.root.is_symlink():
            raise CoordinationBlocked('coordination_storage_unavailable')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root/'state.sqlite3'
        if self.path.is_symlink():
            raise CoordinationBlocked('coordination_storage_unavailable')
        with closing(self.connect()) as db, db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS windows(
              profile TEXT PRIMARY KEY, target_id TEXT, task_id TEXT,
              stage TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, profile TEXT, task_id TEXT,
              action TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def window(self, profile):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM windows WHERE profile=?', (profile,)).fetchone()
            return dict(row) if row else None

    def begin(self, profile, task_id):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM windows WHERE profile=?', (profile,)).fetchone()
            if row and row['state']=='needs_verification':
                raise CoordinationBlocked('task_window_requires_login')
            if row and row['state'] in ('submitting','submitted','generated','result_unknown'):
                raise CoordinationBlocked('previous_task_result_unconfirmed')
            db.execute('INSERT INTO windows VALUES(?,?,?,\'preparing\',\'preparing\',?) '
                       'ON CONFLICT(profile) DO UPDATE SET task_id=excluded.task_id,stage=excluded.stage,state=excluded.state,updated=excluded.updated',
                       (profile, None, task_id, time.time()))
        _current.set((self, profile, task_id))
        self.event(profile, task_id, 'task_preparation_started')

    def event(self, profile, task_id, action, **detail):
        # Callers pass enumerated reasons/counts only: never prompts, URLs or credentials.
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO events(profile,task_id,action,detail,created) VALUES(?,?,?,?,?)',
                       (profile, task_id, action, json.dumps(detail, sort_keys=True), time.time()))

    def bind(self, profile, task_id, target_id):
        with closing(self.connect()) as db, db:
            changed = db.execute('UPDATE windows SET target_id=?,updated=? WHERE profile=? AND task_id=?',
                                 (target_id,time.time(),profile,task_id)).rowcount
            if changed != 1:
                raise CoordinationBlocked('task_window_owner_changed')

    def progress(self, profile, task_id, stage):
        allowed = ('preparing','submitting','submitted','generated')
        if stage not in allowed:
            raise CoordinationBlocked('invalid_task_stage')
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT stage FROM windows WHERE profile=? AND task_id=?', (profile,task_id)).fetchone()
            if not row:
                raise CoordinationBlocked('task_window_owner_changed')
            if row['stage'] in allowed and allowed.index(stage) < allowed.index(row['stage']):
                return
            db.execute('UPDATE windows SET stage=?,state=?,updated=? WHERE profile=? AND task_id=?',
                       (stage,stage,time.time(),profile,task_id))

    def finish(self, profile, task_id, success, draft=False, reason=None):
        with closing(self.connect()) as db, db:
            row=db.execute('SELECT stage FROM windows WHERE profile=? AND task_id=?',(profile,task_id)).fetchone()
            if not row:
                return
            state = ('blocked_draft' if draft else 'idle') if success or row['stage']=='preparing' else 'result_unknown'
            if reason=='task_window_requires_login' and state!='result_unknown':
                state='needs_verification'
            db.execute('UPDATE windows SET state=?,updated=? WHERE profile=? AND task_id=?',
                       (state,time.time(),profile,task_id))
        self.event(profile,task_id,'window_retained',state=state)

    def release_verified(self, profile):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE windows SET state='idle',updated=? WHERE profile=? AND state='needs_verification'",(time.time(),profile))

    def events(self, task_id):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT action,detail,created FROM events WHERE task_id=? ORDER BY id', (task_id,))]

    def status(self):
        with closing(self.connect()) as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM windows ORDER BY profile')]
        try:
            from task_window_registry import TaskWindowRegistry
            bindings = TaskWindowRegistry(self.root).status()
            binding_available = True
        except (OSError, ValueError, sqlite3.Error):
            bindings, binding_available = [], False
        try:
            from flow_window_policy import Lifecycle
            lifecycle=Lifecycle(self.root).status()
            lifecycle_available=True
        except (OSError,ValueError,sqlite3.Error):
            lifecycle=[];lifecycle_available=False
        return {'window_policy':'one_per_account','same_account':'serialized',
                'automatic_regeneration':False,'automatic_account_switch':False,'windows':rows,
                'window_bindings':bindings,'window_bindings_available':binding_available,
                'window_lifecycle':lifecycle,'window_lifecycle_available':lifecycle_available,'keepalive_interval_seconds':1800,
                'binding_policy':'verify_exact_target_and_native_window_before_input'}


def current():
    return _current.get()


def record(action, **detail):
    item=current()
    if item:
        item[0].event(item[1],item[2],action,**detail)


def preparation_safe():
    item=current()
    return not item or item[0].window(item[1])['stage']=='preparing'


async def recover_preparation(operation, *, reason, repair=None, attempts=2):
    """Only for reversible preparation, never wrap paste, submit or generation."""
    for attempt in range(attempts):
        if not preparation_safe():
            raise CoordinationBlocked('submission_boundary_already_recorded')
        try:
            result=await operation()
            if attempt:
                record('preparation_recovered',reason=reason,attempt=attempt+1)
            return result
        except (TimeoutError, ConnectionError) as exc:
            record('preparation_recovery',reason=reason,attempt=attempt+1,error=type(exc).__name__)
            if attempt+1==attempts:
                raise
            if repair:
                await repair()
            await asyncio.sleep(.2)


def account_lock_name(profile):
    return hashlib.sha256(profile.encode()).hexdigest()
