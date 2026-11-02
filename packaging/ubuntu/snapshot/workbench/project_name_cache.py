"""Workbench-only names with source and observation time; Flow data is untouched."""
import sqlite3
import time
from pathlib import Path


def merge(projects, observations, state_dir=None):
    state = Path(state_dir) if state_dir else Path.home() / '.local/state/creative-unified-workbench'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = state / 'project-names.sqlite3'
    if database.is_symlink():
        raise ValueError('unsafe_project_name_database')
    known = {(p['source_profile'], p['source_id'].lower()): p for p in projects}
    with sqlite3.connect(str(database), timeout=5) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS names (
          profile TEXT NOT NULL, project TEXT NOT NULL, title TEXT NOT NULL,
          observed REAL NOT NULL, source TEXT NOT NULL, PRIMARY KEY(profile,project))''')
        now = time.time()
        for item in observations:
            if not isinstance(item, dict):
                continue
            profile, ident, title = item.get('profile'), item.get('id'), item.get('title')
            source = item.get('source', 'open_flow_tab')
            observed = item.get('observed', now)
            if ((profile, ident) not in known or not isinstance(title, str) or not title.strip()
                    or len(title) > 180 or any(ord(c) < 32 or ord(c) == 127 for c in title)
                    or source not in ('open_flow_tab', 'browser_history')
                    or not isinstance(observed, (int, float)) or not 0 < observed <= now + 60):
                continue
            db.execute('''INSERT INTO names(profile,project,title,observed,source) VALUES(?,?,?,?,?)
                       ON CONFLICT(profile,project) DO UPDATE SET title=excluded.title,
                       observed=excluded.observed,source=excluded.source
                       WHERE excluded.observed > names.observed''',
                       (profile, ident, title.strip(), observed, source))
        saved = db.execute('SELECT profile,project,title,observed,source FROM names').fetchall()
    for profile, ident, title, observed, source in saved:
        row = known.get((profile, ident))
        if row and row.get('title') in ('', '未命名项目'):
            row.update(title=title, title_source='saved_flow_name', title_observed=observed,
                       title_origin=source)
