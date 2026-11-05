"""Flow account/browser inventory utilities. Legacy Gemini transports removed."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener



def service_pid(unit):
    value = subprocess.check_output(['systemctl', '--user', 'show', unit, '--property=MainPID', '--value'], text=True).strip()
    if not value.isdigit() or value == '0':
        raise ValueError('service_unavailable')
    return value


def process_args(pid):
    args = [os.fsdecode(v) for v in Path('/proc', pid, 'cmdline').read_bytes().split(b'\0') if v]
    return shlex.split(args[0]) if len(args) == 1 else args


def state():
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:18888/api/state', timeout=5) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('state_too_large')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('test') is True or not isinstance(data.get('accounts'), list):
        raise ValueError('account_state_unavailable')
    return data


def key(profile):
    return hashlib.sha256(profile.encode()).hexdigest()


def eligible_accounts(accounts, requested=None):
    # Gemini's eligibility is not inferred from Flow credits or Flow task health.
    result = []
    for account in accounts:
        if (not isinstance(account, dict) or not isinstance(account.get('profile'), str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', account['profile'])
                or account.get('state') != 'verified' or not isinstance(account.get('email'), str) or not account['email']):
            continue
        if sum(1 for row in accounts if isinstance(row, dict) and row.get('profile') == account['profile']) != 1:
            continue
        if requested and key(account['profile']) != requested:
            continue
        result.append(account)
    return result




def flow_runtime():
    console_pid = service_pid('studiowright-console.service')
    console_root = Path(os.readlink('/proc/' + console_pid + '/cwd')).resolve()
    environment = dict(part.split(b'=', 1) for part in Path('/proc', console_pid, 'environ').read_bytes().split(b'\0') if b'=' in part)
    root = Path(os.fsdecode(environment.get(b'SW_ROOT', os.fsencode(Path.home() / '.local/share/studiowright-evaluation')))).resolve()
    os.environ['STUDIOWRIGHT_HOME'] = str(root / 'data')
    os.environ['STUDIOWRIGHT_OUTPUT'] = str(root / 'output')
    sys.path.insert(0, str(console_root))
    from studiowright import chrome, paths
    return chrome, paths


def running_accounts(accounts, chrome, paths):
    """Read process flags only; do not open a browser or inspect login cookies."""
    processes = []
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = process_args(path.parent.name)
            if args and not any(a.startswith('--type=') for a in args):
                processes.append(args)
        except (OSError, UnicodeError, ValueError):
            continue
    running = {}
    for account in eligible_accounts(accounts):
        profile = account['profile']
        port = chrome.debug_port(profile)
        if sum(1 for a in accounts if isinstance(a, dict) and isinstance(a.get('profile'), str)
               and chrome.debug_port(a['profile']) == port) != 1:
            continue
        port_flag = '--remote-debugging-port=' + str(port)
        target_flag = '--user-data-dir=' + str(paths.profile_dir(profile))
        matches = [args for args in processes if port_flag in args]
        if len(matches) == 1 and target_flag in matches[0]:
            running[profile] = port
    return running


def browser_inventory():
    chrome, paths = flow_runtime()
    accounts = state()['accounts']
    running = running_accounts(accounts, chrome, paths)
    return {'result': 'browser_inventory',
            'accounts': {key(a['profile']): a['profile'] in running for a in eligible_accounts(accounts)}}






def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--operation',choices=['inventory'],required=True)
    parser.parse_args()
    print(json.dumps(browser_inventory(),ensure_ascii=False))

if __name__=='__main__':
    main()
