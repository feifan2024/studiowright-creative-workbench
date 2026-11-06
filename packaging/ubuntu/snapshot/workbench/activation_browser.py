"""Open one already registered Flow profile, without navigation or generation."""
import asyncio
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


async def ensure_window(port):
    """Reuse this profile's window, or create one only when none exists."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(
            'http://127.0.0.1:' + str(port),
            timeout=30000 if os.environ.get('STUDIOWRIGHT_DESKTOP_RECOVERY') == '1' else 4000,
            no_defaults=True)
        session = await browser.new_browser_cdp_session()
        targets = (await session.send('Target.getTargets'))['targetInfos']
        page_targets = [target for target in targets if target.get('type') == 'page']
        windows = set()
        for target in page_targets:
            # If any page cannot be mapped, window state is uncertain: never create another.
            result = await session.send('Browser.getWindowForTarget', {'targetId': target['targetId']})
            windows.add(result['windowId'])
        if any(target.get('url', '').startswith('https://flow.google.com/') for target in page_targets):
            return 'multiple_existing' if len(windows) > 1 else 'reused'
        if any(not target.get('url', '').startswith('https://gemini.google.com/') for target in page_targets):
            raise ValueError('flow_window_unconfirmed')
        from studiowright.chrome import FLOW_URL
        created = await session.send('Target.createTarget', {'url': FLOW_URL, 'newWindow': True})
        await session.send('Browser.getWindowForTarget', {'targetId': created['targetId']})
        return 'opened'


def main(account_key, *, flow_only_start=False):
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
    if any(t.get('profile') == profile and t.get('status') in ('queued', 'running') for t in state.get('tasks', [])):
        raise ValueError('flow_busy')
    if any(s.get('profile') == profile for s in state.get('remote_logins', [])):
        raise ValueError('interactive_login')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        preference = db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?', (account_key,)).fetchone()
        if not flow_only_start and preference and not preference[0]:
            raise ValueError('gemini_paused')
        if db.execute("SELECT 1 FROM flow_requests WHERE profile=? AND status='dispatching'", (profile,)).fetchone():
            raise ValueError('flow_busy')
        if (db.execute("SELECT 1 FROM requests WHERE account_key=? AND status='dispatching'", (account_key,)).fetchone()
                or not flow_only_start and has_unresolved_request(db, account_key)):
            raise ValueError('gemini_pending')
        if db.execute("SELECT 1 FROM account_monitor WHERE account_key=? AND status='dispatching'", (account_key,)).fetchone():
            raise ValueError('gemini_busy')
    os.environ['STUDIOWRIGHT_HOME'] = str(root / 'data')
    os.environ['STUDIOWRIGHT_OUTPUT'] = str(root / 'output')
    sys.path.insert(0, str(root / 'console'))
    from adapter import RealAdapter
    from filelock import FileLock, Timeout
    from studiowright import chrome, paths
    from shared_session_controller import running_accounts, process_args
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
                or any(t.get('profile') == profile and t.get('status') in ('queued', 'running') for t in current.get('tasks', []))
                or any(s.get('profile') == profile for s in current.get('remote_logins', []))):
            raise ValueError('account_state_changed')
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            preference = db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?', (account_key,)).fetchone()
            if not flow_only_start and preference and not preference[0]:
                raise ValueError('gemini_paused')
            if db.execute("SELECT 1 FROM flow_requests WHERE profile=? AND status='dispatching'", (profile,)).fetchone():
                raise ValueError('flow_busy')
            if (db.execute("SELECT 1 FROM requests WHERE account_key=? AND status='dispatching'", (account_key,)).fetchone()
                or not flow_only_start and has_unresolved_request(db, account_key)):
                raise ValueError('gemini_pending')
            if db.execute("SELECT 1 FROM account_monitor WHERE account_key=? AND status='dispatching'", (account_key,)).fetchone():
                raise ValueError('gemini_busy')
        if flow_only_start:
            wanted = '--user-data-dir=' + str(paths.profile_dir(profile))
            for proc in Path('/proc').glob('[0-9]*/cmdline'):
                try:
                    if wanted in process_args(proc.parent.name):
                        raise ValueError('flow_browser_already_running')
                except (OSError, UnicodeError):
                    continue
        RealAdapter(root)._browser(profile, login=False)
        running = running_accounts(current.get('accounts', []), chrome, paths)
        if profile not in running:
            raise ValueError('browser_identity_unconfirmed')
        window = asyncio.run(ensure_window(running[profile]))
        matches = []
        port = running[profile]
        for path in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                args = process_args(path.parent.name)
                if ('--remote-debugging-port=' + str(port) in args
                        and '--user-data-dir=' + str(paths.profile_dir(profile)) in args
                        and not any(a.startswith('--type=') for a in args)):
                    matches.append(int(path.parent.name))
            except (OSError, UnicodeError, ValueError):
                continue
        if len(matches) != 1:
            raise ValueError('browser_identity_unconfirmed')
        from flow_browser_state import register_flow, mark
        try:
            layout = asyncio.run(register_flow(account_key, matches[0], port, account['email'].strip()))
        except Exception as exc:
            mark(account_key, 'blocked', str(exc)[:100])
            raise
        mark(account_key, 'active')
    finally:
        guard.release()
    return {'result': 'browser_ready', 'window': window, 'desktop': layout}


if __name__ == '__main__':
    try:
        print(json.dumps(main(sys.argv[1])))
    except Exception as exc:
        print(json.dumps({'result': 'not_ready', 'reason': str(exc)[:100]}))
        sys.exit(1)
