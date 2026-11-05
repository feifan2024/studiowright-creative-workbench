"""Account-owned task page/native-window bindings; never generates or closes tabs."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse


def process_identity(pid, account_key, port):
    """Reject PID reuse and a debugger port belonging to another browser profile."""
    from shared_session_controller import process_args
    if type(pid) is not int or pid <= 0 or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('task_window_process_unconfirmed')
    args = process_args(str(pid))
    profiles = [x.split('=', 1)[1] for x in args if x.startswith('--user-data-dir=')]
    ports = [x.split('=', 1)[1] for x in args if x.startswith('--remote-debugging-port=')]
    if len(profiles) != 1 or ports != [str(port)] or any(x.startswith('--type=') for x in args):
        raise ValueError('task_window_process_unconfirmed')
    profile = Path(profiles[0]).name
    if hashlib.sha256(profile.encode()).hexdigest() != account_key:
        raise ValueError('task_window_account_mismatch')
    try:
        stamp = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        raise ValueError('task_window_process_unconfirmed') from None
    if not stamp.isdigit():
        raise ValueError('task_window_process_unconfirmed')
    return profile, stamp


def canonical_project_url(url):
    try:
        u = urlparse(url)
        if (u.scheme != 'https' or u.hostname != 'flow.google.com' or u.username or u.password
                or u.port not in (None, 443) or not re.fullmatch(r'/project/[0-9a-fA-F-]{36}/?', u.path)):
            raise ValueError('task_window_project_unconfirmed')
    except (TypeError, ValueError):
        raise ValueError('task_window_project_unconfirmed') from None
    return 'https://flow.google.com' + u.path.rstrip('/')


class TaskWindowRegistry:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path.home()/'.local/state/creative-unified-workbench/task-coordination'
        self.path = self.root/'window-bindings.sqlite3'
        if self.root.is_symlink() or self.path.is_symlink():
            raise ValueError('task_window_registry_unavailable')

    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.executescript('''
        CREATE TABLE IF NOT EXISTS bindings(
          account_key TEXT PRIMARY KEY, profile TEXT UNIQUE NOT NULL,
          browser_pid INTEGER NOT NULL, process_start TEXT NOT NULL, port INTEGER NOT NULL,
          page_target TEXT NOT NULL, chrome_window INTEGER NOT NULL, window_id INTEGER NOT NULL,
          workspace INTEGER NOT NULL, project_url TEXT NOT NULL, verified_at REAL NOT NULL,
          binding_state TEXT NOT NULL, reason TEXT,
          UNIQUE(browser_pid,process_start,page_target),
          UNIQUE(browser_pid,process_start,chrome_window),
          UNIQUE(browser_pid,process_start,window_id));
        CREATE TABLE IF NOT EXISTS binding_events(
          id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, action TEXT NOT NULL,
          detail TEXT NOT NULL, created REAL NOT NULL);
        ''')
        os.chmod(self.path, 0o600)
        return db

    def bind(self, *, account_key, profile, browser_pid, process_start, port, page_target,
             chrome_window, window_id, workspace, project_url):
        if not re.fullmatch(r'[a-f0-9]{64}', account_key or '') or hashlib.sha256(profile.encode()).hexdigest() != account_key:
            raise ValueError('task_window_account_mismatch')
        if any(type(x) is not int or x <= 0 for x in (browser_pid, port, chrome_window, window_id)) or type(workspace) is not int or workspace < 0:
            raise ValueError('task_window_identity_unconfirmed')
        if not isinstance(page_target, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', page_target):
            raise ValueError('task_window_identity_unconfirmed')
        if not isinstance(process_start, str) or not process_start.isdigit():
            raise ValueError('task_window_identity_unconfirmed')
        project_url = canonical_project_url(project_url)
        fields = dict(account_key=account_key, profile=profile, browser_pid=browser_pid,
                      process_start=process_start, port=port, page_target=page_target,
                      chrome_window=chrome_window, window_id=window_id, workspace=workspace,
                      project_url=project_url)
        try:
            with closing(self.connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT * FROM bindings WHERE account_key=?', (account_key,)).fetchone()
                identity = ('browser_pid', 'process_start', 'page_target', 'chrome_window', 'window_id')
                mode = 'created' if old is None else 'reused' if all(old[k] == fields[k] for k in identity) else 'repaired'
                values = list(fields.values()) + [time.time(), 'verified', None]
                db.execute('INSERT INTO bindings VALUES('+','.join('?' for _ in values)+') ON CONFLICT(account_key) DO UPDATE SET '+
                           ','.join(k+'=excluded.'+k for k in fields if k != 'account_key')+
                           ',verified_at=excluded.verified_at,binding_state=excluded.binding_state,reason=NULL', values)
                db.execute('INSERT INTO binding_events(account_key,action,detail,created) VALUES(?,?,?,?)',
                           (account_key, mode, json.dumps({'window_id': window_id, 'page_target': page_target,
                            'previous_window_id': old['window_id'] if old else None}), time.time()))
                return mode
        except sqlite3.IntegrityError:
            raise ValueError('task_window_binding_conflict') from None

    def mark_unconfirmed(self, account_key, reason):
        if not self.path.exists():
            return
        if not re.fullmatch(r'[a-z_]{3,80}', reason):
            reason = 'task_window_identity_unconfirmed'
        with closing(self.connect()) as db, db:
            db.execute("UPDATE bindings SET binding_state='unconfirmed',reason=? WHERE account_key=?", (reason, account_key))

    def get(self, account_key):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM bindings WHERE account_key=?', (account_key,)).fetchone()
            return dict(row) if row else None

    def status(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM bindings ORDER BY profile')]
        for row in rows:
            try:
                stamp = (Path('/proc')/str(row['browser_pid'])/'stat').read_text().rsplit(')',1)[1].split()[19]
                alive = stamp == row['process_start']
            except (OSError, IndexError):
                alive = False
            if not alive:
                row.update(binding_state='unconfirmed', reason='task_window_process_changed')
        return rows
