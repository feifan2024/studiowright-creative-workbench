"""Read titles of already-open Flow project tabs; never navigate or use credentials."""
import json
import re
import sqlite3
import sys
from collections import defaultdict
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import shared_session_controller as sessions


def main():
    data = json.load(sys.stdin)
    accounts = data.get('accounts', [])
    known = {(x.get('profile'), str(x.get('id', '')).lower())
             for x in data.get('projects', []) if isinstance(x, dict)}
    chrome, paths = sessions.flow_runtime()
    running = sessions.running_accounts(accounts, chrome, paths)
    opener = build_opener(ProxyHandler({}))
    observations = defaultdict(set)
    historical = {}
    # Only look up registered Flow project URLs, within each account's own history.
    # Other browsing history and credentials are not read.
    for profile in {p for p, _ in known if isinstance(p, str)}:
        path = paths.profile_dir(profile) / 'Default' / 'History'
        try:
            before = path.stat()
            db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=1)
            try:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    continue
                rows = db.execute("SELECT url,title,last_visit_time FROM urls WHERE url LIKE 'https://flow.google.com/project/%' AND title LIKE 'Google Flow - %'").fetchall()
            finally:
                db.close()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                continue
            for url_text, title, visited in rows:
                url = urlsplit(url_text)
                match = re.fullmatch(r'/project/([0-9a-fA-F-]{36})/?', url.path)
                if url.scheme != 'https' or url.netloc != 'flow.google.com' or not match:
                    continue
                key = (profile, match.group(1).lower())
                value = title[len('Google Flow - '):].strip()
                observed = visited / 1000000 - 11644473600
                if key in known and value and 0 < observed and len(value) <= 180 and not re.search(r'[\x00-\x1f\x7f]', value):
                    if key not in historical or observed > historical[key]['observed']:
                        historical[key] = {'profile': profile, 'id': key[1], 'title': value,
                                           'observed': observed, 'source': 'browser_history'}
        except (OSError, ValueError, TypeError, sqlite3.Error):
            continue
    for profile, port in running.items():
        try:
            with opener.open('http://127.0.0.1:' + str(port) + '/json/list', timeout=1) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                continue
            tabs = json.loads(raw)
            if not isinstance(tabs, list):
                continue
            for tab in tabs:
                if not isinstance(tab, dict) or tab.get('type') != 'page':
                    continue
                url = urlsplit(str(tab.get('url', '')))
                match = re.fullmatch(r'/project/([0-9a-fA-F-]{36})/?', url.path)
                title = tab.get('title')
                if (url.scheme != 'https' or url.netloc != 'flow.google.com' or not match
                        or not isinstance(title, str) or not title.startswith('Google Flow - ')):
                    continue
                ident = match.group(1).lower()
                value = title[len('Google Flow - '):].strip()
                if ((profile, ident) in known and value and len(value) <= 180
                        and not re.search(r'[\x00-\x1f\x7f]', value)):
                    observations[(profile, ident)].add(value)
        except (OSError, ValueError, TypeError):
            continue
    for (profile, ident), values in observations.items():
        if len(values) == 1:
            historical[(profile, ident)] = {'profile': profile, 'id': ident,
                                             'title': next(iter(values)), 'source': 'open_flow_tab'}
    print(json.dumps(list(historical.values()), ensure_ascii=True))


if __name__ == '__main__':
    main()
