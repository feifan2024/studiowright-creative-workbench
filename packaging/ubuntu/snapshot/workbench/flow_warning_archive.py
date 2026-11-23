"""Serve Flow warning screenshots and remove them after an operator releases the task hold."""
import argparse
from contextlib import closing
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys


FLOW_ROOT = Path.home() / '.local/share/studiowright-evaluation'
REVIEW_DATABASE = Path.home() / '.local/state/creative-unified-workbench/task-coordination/state.sqlite3'
JOB = re.compile(r'[0-9a-f]{12}')
MAX_PNG_BYTES = 16 * 1024 * 1024


def database(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Flow data is not available.')
    return closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=4))


def _destroyed_ids(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='flow_destroyed_screenshots'").fetchone():
        return set()
    return {row[0] for row in db.execute('SELECT id FROM flow_destroyed_screenshots')}


def _reconcile(root, job_id=None, *, task_ids=None, review_database=None, profile_filter=None):
    if job_id is not None and not JOB.fullmatch(job_id):
        raise ValueError('Invalid Flow job ID.')
    incidents = root / 'data/incidents'
    if incidents.is_symlink():
        raise ValueError('Unexpected screenshots directory.')
    emails = {}
    with database(root / 'console-data/console.db') as db:
        destroyed = _destroyed_ids(db)
        if task_ids is None:
            emails = dict(db.execute('SELECT profile, email FROM accounts'))
    with database(root / 'data/studiowright.db') as db:
        rows = db.execute("""SELECT id, profile, finished, error,
                    CASE WHEN json_valid(params) THEN json_extract(params,'$.origin_console_task') END
                FROM jobs
                WHERE status='failed' AND kind IN ('image','video') AND finished IS NOT NULL
                    AND (CASE WHEN json_valid(result) THEN json_extract(result,'$.warning_screenshot') END) = id || '.png'
                    AND (? IS NULL OR id=?)
                ORDER BY finished, id""", (job_id, job_id)).fetchall()
    if not rows:
        return
    with database(review_database if review_database is not None else REVIEW_DATABASE) as db:
        approvals = {(profile, task): created for profile, task, created in db.execute("""
            SELECT profile, task_id, max(created) FROM events
            WHERE action='readiness_hold_reviewed_by_operator' GROUP BY profile, task_id
        """)}
    for ident, profile, finished, error, task_id in rows:
        if not JOB.fullmatch(ident) or not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id):
            continue
        if task_ids is not None and task_id not in task_ids:
            continue
        if profile_filter is not None and profile != profile_filter:
            continue
        path = incidents / (ident + '.png')
        approved_at = approvals.get((profile, task_id))
        reviewed = ident in destroyed or (approved_at is not None and finished <= approved_at)
        name = (emails.get(profile) or profile).split('@', 1)[0]
        name = re.sub(r'[^A-Za-z0-9_.+-]', '_', name)[:48] or 'account'
        reason = 'unusual' if 'PUBLIC_ERROR_UNUSUAL_ACTIVITY' in (error or '') else 'generation'
        record = {'id': ident, 'task_id': task_id, 'profile': profile, 'account': name,
                  'finished': finished, 'reason': reason}
        if reviewed:
            try:
                if not path.is_symlink() and path.is_file():
                    path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning('Could not remove reviewed Flow screenshot %s', ident)
            # Keep the approval discoverable even after the PNG is gone, for offline Windows clients.
            yield record, path, True
        elif not path.is_symlink():
            try:
                if path.is_file() and 0 < path.stat().st_size <= MAX_PNG_BYTES:
                    yield record, path, False
            except FileNotFoundError:
                continue


def records(root, job_id=None, *, task_ids=None, review_database=None):
    for record, path, reviewed in _reconcile(root, job_id, task_ids=task_ids, review_database=review_database):
        if not reviewed:
            yield record, path


def purge_reviewed_screenshots(profile, task_id):
    # Reconcile the persisted review immediately; subsequent reads retry any failed removal.
    for _ in _reconcile(FLOW_ROOT, task_ids={task_id}, profile_filter=profile):
        pass


def sync_index(root=FLOW_ROOT, *, review_database=None):
    index = {'active': [], 'reviewed': []}
    for record, _, reviewed in _reconcile(root, review_database=review_database):
        index['reviewed' if reviewed else 'active'].append(record)
    return index


def current_window_screenshots(coordination, root=FLOW_ROOT, *, review_database=None):
    task_by_profile = {row['profile']: row['task_id'] for row in (coordination or {}).get('windows', [])
                       if row.get('profile') and row.get('task_id')}
    if not task_by_profile:
        return {}
    shots = {}
    for record, _, reviewed in _reconcile(root, task_ids=set(task_by_profile.values()), review_database=review_database):
        if task_by_profile.get(record['profile']) == record['task_id']:
            shots[record['profile']] = ({'task_id': record['task_id'], 'destroyed': True} if reviewed
                                        else {key: record[key] for key in ('id', 'task_id')})
    return shots


def read_png(root, job_id, *, review_database=None):
    match = next(records(root, job_id, review_database=review_database), None)
    if match is None:
        raise FileNotFoundError('Warning screenshot is not available.')
    with match[1].open('rb') as stream:
        data = stream.read(MAX_PNG_BYTES + 1)
    if len(data) > MAX_PNG_BYTES or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Warning screenshot is invalid.')
    return data


def read_page_png(root, ident):
    if not re.fullmatch(r'[0-9a-f]{32}', ident):
        raise ValueError('Invalid Flow page incident ID.')
    with database(root / 'console-data/console.db') as db:
        row = db.execute("""SELECT screenshot FROM flow_page_incidents
            WHERE id=? AND reviewed IS NULL AND length(screenshot) BETWEEN 1 AND ?""",
            (ident, MAX_PNG_BYTES)).fetchone()
    if row is None:
        raise FileNotFoundError('Warning screenshot is not available.')
    if not isinstance(row[0], bytes) or not row[0].startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Warning screenshot is invalid.')
    return row[0]


def destroy_screenshot(root, ident, *, review_database=None):
    """Remove one archived image while retaining its account or task record."""
    if not re.fullmatch(r'[0-9a-f]{12}|[0-9a-f]{32}', ident):
        raise ValueError('Invalid Flow screenshot ID.')
    path = None
    if len(ident) == 12:
        match = next(_reconcile(root, ident, review_database=review_database), None)
        if match is None:
            raise FileNotFoundError('Warning screenshot is not available.')
        _, path, _ = match
    database_path = root / 'console-data/console.db'
    if database_path.is_symlink() or not database_path.is_file():
        raise ValueError('Flow data is not available.')
    with closing(sqlite3.connect(database_path.as_uri() + '?mode=rw', uri=True, timeout=4)) as db, db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS flow_destroyed_screenshots(id TEXT PRIMARY KEY)')
        if path is None:
            changed = db.execute("""UPDATE flow_page_incidents SET screenshot=NULL
                WHERE id=? AND reviewed IS NULL AND screenshot IS NOT NULL""", (ident,)).rowcount
            if not changed and not db.execute('SELECT 1 FROM flow_destroyed_screenshots WHERE id=?', (ident,)).fetchone():
                raise FileNotFoundError('Warning screenshot is not available.')
        db.execute('INSERT OR IGNORE INTO flow_destroyed_screenshots VALUES(?)', (ident,))
    if path is not None:
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError('Unexpected screenshot path.')
        path.unlink(missing_ok=True)


def main(argv=None, root=FLOW_ROOT, *, review_database=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('list', 'read', 'sync'))
    parser.add_argument('job_id', nargs='?')
    options = parser.parse_args(argv)
    if options.action == 'list' and options.job_id is None:
        print(json.dumps([record for record, _ in records(root, review_database=review_database)]))
    elif options.action == 'sync' and options.job_id is None:
        print(json.dumps(sync_index(root, review_database=review_database)))
    elif options.action == 'read' and options.job_id:
        sys.stdout.buffer.write(read_png(root, options.job_id, review_database=review_database))
    else:
        parser.error('Only read takes a job ID.')


if __name__ == '__main__':
    main()
