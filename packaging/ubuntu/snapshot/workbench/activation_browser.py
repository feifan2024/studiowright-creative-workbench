"""Open one already registered Flow profile, without navigation or generation."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.request import ProxyHandler, build_opener


def has_unresolved_request(db, account_key):
    return bool(db.execute("""SELECT 1 FROM requests r WHERE r.account_key=? AND
        (r.status='dispatching' OR (r.status='unknown' AND NOT EXISTS
        (SELECT 1 FROM account_unknown_release x WHERE x.request_id=r.request_id))) LIMIT 1""", (account_key,)).fetchone())


def main(account_key):
    if not re.fullmatch(r'[0-9a-f]{64}', account_key):
        raise ValueError('invalid_account')
    home = Path.home()
    root = home / '.local/share/studiowright-evaluation'
    database = home / '.local/state/creative-unified-workbench/real-pool.sqlite3'
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:18888/api/state', timeout=8) as response:
        state = json.load(response)
    accounts = [a for a in state.get('accounts', []) if isinstance(a, dict)
                and isinstance(a.get('profile'), str)
                and hashlib.sha256(a['profile'].encode()).hexdigest() == account_key]
    if len(accounts) != 1:
        raise ValueError('account_not_unique')
    account = accounts[0]
    profile = account['profile']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
        raise ValueError('invalid_profile')
    if (account.get('state') != 'verified' or account.get('risk_paused') is not False
            or account.get('generation_state') != 'not_cooling_down'):
        raise ValueError('flow_not_ready')
    if any(t.get('profile') == profile and t.get('status') == 'running' for t in state.get('tasks', [])):
        raise ValueError('flow_busy')
    if any(s.get('profile') == profile for s in state.get('remote_logins', [])):
        raise ValueError('interactive_login')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        if has_unresolved_request(db, account_key):
            raise ValueError('gemini_pending')
        if db.execute("SELECT 1 FROM account_monitor WHERE account_key=? AND status='dispatching'", (account_key,)).fetchone():
            raise ValueError('gemini_busy')
    os.environ['STUDIOWRIGHT_HOME'] = str(root / 'data')
    os.environ['STUDIOWRIGHT_OUTPUT'] = str(root / 'output')
    sys.path.insert(0, str(root / 'console'))
    from adapter import RealAdapter
    from filelock import FileLock, Timeout
    from studiowright import paths
    guard = FileLock(str(paths.locks_dir() / (profile + '.lock')))
    try:
        guard.acquire(timeout=0)
    except Timeout:
        raise ValueError('account_busy') from None
    try:
        # Re-read under the same profile lock used by the existing Flow runtime.
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:18888/api/state', timeout=8) as response:
            current = json.load(response)
        rows = [a for a in current.get('accounts', []) if a.get('profile') == profile]
        if (len(rows) != 1 or rows[0].get('state') != 'verified'
                or rows[0].get('risk_paused') is not False
                or rows[0].get('generation_state') != 'not_cooling_down'
                or any(t.get('profile') == profile and t.get('status') == 'running' for t in current.get('tasks', []))
                or any(s.get('profile') == profile for s in current.get('remote_logins', []))):
            raise ValueError('account_state_changed')
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            if has_unresolved_request(db, account_key):
                raise ValueError('gemini_pending')
            if db.execute("SELECT 1 FROM account_monitor WHERE account_key=? AND status='dispatching'", (account_key,)).fetchone():
                raise ValueError('gemini_busy')
        RealAdapter(root)._browser(profile, login=False)
    finally:
        guard.release()
    return {'result': 'browser_ready'}


if __name__ == '__main__':
    try:
        print(json.dumps(main(sys.argv[1])))
    except Exception as exc:
        print(json.dumps({'result': 'not_ready', 'reason': str(exc)[:100]}))
        sys.exit(1)
