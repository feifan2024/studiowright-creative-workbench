"""Read the Flow console's warning screenshots through the existing SSH connection."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys


FLOW_ROOT = Path.home() / '.local/share/studiowright-evaluation'
JOB = re.compile(r'[0-9a-f]{12}')
MAX_PNG_BYTES = 16 * 1024 * 1024


def database(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Flow data is not available.')
    return closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=4))


def records(root, job_id=None, *, task_ids=None):
    if job_id is not None and not JOB.fullmatch(job_id):
        raise ValueError('Invalid Flow job ID.')
    incidents = root / 'data/incidents'
    if incidents.is_symlink():
        raise ValueError('Unexpected screenshots directory.')
    emails = {}
    if task_ids is None:
        with database(root / 'console-data/console.db') as db:
            emails = dict(db.execute('SELECT profile, email FROM accounts'))
    with database(root / 'data/studiowright.db') as db:
        rows = db.execute("""SELECT id, profile, finished, error,
                    CASE WHEN json_valid(params) THEN json_extract(params,'$.origin_console_task') END
                FROM jobs
                WHERE status='failed' AND kind IN ('image','video') AND finished IS NOT NULL
                    AND (CASE WHEN json_valid(result) THEN json_extract(result,'$.warning_screenshot') END) = id || '.png'
                    AND (? IS NULL OR id=?)
                ORDER BY finished, id""", (job_id, job_id)).fetchall()
    for ident, profile, finished, error, task_id in rows:
        if not JOB.fullmatch(ident) or not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id):
            continue
        if task_ids is not None and task_id not in task_ids:
            continue
        path = incidents / (ident + '.png')
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_PNG_BYTES:
            continue
        name = (emails.get(profile) or profile).split('@', 1)[0]
        name = re.sub(r'[^A-Za-z0-9_.+-]', '_', name)[:48] or 'account'
        reason = 'unusual' if 'PUBLIC_ERROR_UNUSUAL_ACTIVITY' in (error or '') else 'generation'
        yield {'id': ident, 'task_id': task_id, 'profile': profile, 'account': name,
               'finished': finished, 'reason': reason}, path


def current_window_screenshots(coordination, root=FLOW_ROOT):
    task_by_profile = {row['profile']: row['task_id'] for row in (coordination or {}).get('windows', [])
                       if row.get('profile') and row.get('task_id')}
    if not task_by_profile:
        return {}
    shots = {}
    for record, _ in records(root, task_ids=set(task_by_profile.values())):
        if task_by_profile.get(record['profile']) == record['task_id']:
            shots[record['profile']] = {key: record[key] for key in ('id', 'task_id')}
    return shots


def read_png(root, job_id):
    match = next(records(root, job_id), None)
    if match is None:
        raise FileNotFoundError('Warning screenshot is not available.')
    with match[1].open('rb') as stream:
        data = stream.read(MAX_PNG_BYTES + 1)
    if len(data) > MAX_PNG_BYTES or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Warning screenshot is invalid.')
    return data


def main(argv=None, root=FLOW_ROOT):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('list', 'read'))
    parser.add_argument('job_id', nargs='?')
    options = parser.parse_args(argv)
    if options.action == 'list' and options.job_id is None:
        print(json.dumps([record for record, _ in records(root)]))
    elif options.action == 'read' and options.job_id:
        sys.stdout.buffer.write(read_png(root, options.job_id))
    else:
        parser.error('Only read takes a job ID.')


if __name__ == '__main__':
    main()
