"""Use one already-open Flow profile as the source of an in-memory Gemini session."""
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

from filelock import FileLock, Timeout
from playwright.async_api import async_playwright


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


async def apply_renewed_cookie(context, source_cookies, result):
    """Apply only a verified Google renewal to the exact original browser cookie."""
    rotated = result.pop('_rotated_cookie', None)  # Never return credentials to callers.
    prior = [c for c in source_cookies if c.get('name') == '__Secure-1PSIDTS' and c.get('value')]
    if len(prior) == 1:
        expiry = prior[0].get('expires')
        result['keepalive_expiry_before'] = expiry if type(expiry) in (int,float) and math.isfinite(expiry) else None
        result['keepalive_expiry_after'] = result['keepalive_expiry_before']
        result['keepalive_value_changed'] = False
    if result.get('keepalive_status') != 'renewed':
        return result
    def unavailable():
        result.update(keepalive_status='unavailable', keepalive_phase='checking_cookie', keepalive_reason='cookie_unconfirmed')
        return result
    try:
        if (result.get('result') != 'checked' or result.get('identity', {}).get('identity_confirmed') is not True
                or result.get('account_available') is not True or not isinstance(rotated, dict)):
            return unavailable()
        names = ('__Secure-1PSID', '__Secure-1PSIDTS')
        original = {name: [c for c in source_cookies if c.get('name') == name and c.get('value')] for name in names}
        if any(len(original[name]) != 1 for name in names):
            return unavailable()
        old = original['__Secure-1PSIDTS'][0]
        value = rotated.get('value')
        if (not isinstance(value, str) or not 8 <= len(value) <= 16000 or any(c in value for c in '\r\n\x00')
                or rotated.get('domain') != old.get('domain')
                or rotated.get('path') != old.get('path')):
            return unavailable()
        expiry = rotated.get('expires')
        old_expiry = old.get('expires')
        if value == old['value'] and (type(expiry) not in (int,float) or not math.isfinite(expiry)
                or expiry <= time.time() or (type(old_expiry) in (int,float) and expiry <= old_expiry)):
            result['keepalive_status'] = 'accepted'
            return result
        current = await context.cookies(['https://gemini.google.com/'])
        present = {name: [c for c in current if c.get('name') == name and c.get('value')] for name in names}
        if any(len(present[name]) != 1 or any(present[name][0].get(field) != original[name][0].get(field)
               for field in ('value', 'domain', 'path')) for name in names):
            return unavailable()
        replacement = dict(old)
        replacement['value'] = value
        if expiry is not None:
            if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time():
                return unavailable()
            replacement['expires'] = expiry
        await context.add_cookies([replacement])
        after = await context.cookies(['https://gemini.google.com/'])
        new_cookies = [c for c in after if c.get('name') == '__Secure-1PSIDTS' and c.get('value')]
        if len(new_cookies) != 1 or any(new_cookies[0].get(field) != expected for field, expected in
                                        (('value', value), ('domain', old.get('domain')), ('path', old.get('path')))):
            return unavailable()
        after = new_cookies[0].get('expires')
        result['keepalive_expiry_after'] = after if type(after) in (int,float) and math.isfinite(after) else None
        result['keepalive_value_changed'] = value != old['value']
        return result
    except Exception:
        return unavailable()


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


async def execute(operation, sdk_path, requested, prompt=None, files=None, conversation=None):
    chrome, paths = flow_runtime()
    from adapter import READ_STATE
    accounts = state()['accounts']
    candidates = eligible_accounts(accounts, requested)
    running = running_accounts(accounts, chrome, paths)
    selected = next(((account, running[account['profile']]) for account in candidates
                     if account['profile'] in running), None)
    if not selected:
        return {'result': 'no_confirmed_running_account', 'submitted': False}
    account, port = selected
    profile, email = account['profile'], account['email'].strip().lower()
    lock = FileLock(str(paths.locks_dir() / (profile + '.lock')))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return {'result': 'account_busy', 'submitted': False, 'account_key': key(profile)}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp('http://127.0.0.1:' + str(port), timeout=4000, no_defaults=True)
            contexts = [cx for cx in browser.contexts if any(p.url.startswith('https://flow.google.com/') for p in cx.pages)]
            if len(contexts) != 1:
                return {'result': 'flow_identity_unavailable', 'submitted': False, 'account_key': key(profile)}
            context = contexts[0]
            pages = [page for page in context.pages if page.url.startswith('https://flow.google.com/')]
            states = [await page.evaluate(READ_STATE) for page in pages]
            if not any(isinstance(s, dict) and s.get('signed_in') and s.get('emails') == [email] for s in states):
                return {'result': 'flow_identity_unavailable', 'submitted': False, 'account_key': key(profile)}
            cookies = await context.cookies(['https://gemini.google.com/'])
            selected_cookies = {}
            for name in ('__Secure-1PSID', '__Secure-1PSIDTS'):
                values = [c.get('value') for c in cookies if c.get('name') == name and c.get('value')]
                if len(values) != 1:
                    return {'result': 'gemini_session_unavailable', 'submitted': False, 'account_key': key(profile)}
                selected_cookies[name] = values[0]
            gemini_pid = service_pid('gemini-hanaoka-trial.service')
            args = process_args(gemini_pid)
            if not args or not Path(args[0]).is_file() or not sdk_path.is_file() or sdk_path.is_symlink():
                return {'result': 'gemini_runtime_unavailable', 'submitted': False, 'account_key': key(profile)}
            proc = await asyncio.create_subprocess_exec(args[0], '-B', str(sdk_path), stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            sdk_operation = 'renew' if operation == 'keepalive' else operation
            payload = json.dumps({'operation': sdk_operation, 'expected': email, 'cookies': selected_cookies, 'prompt': prompt, 'files': files or [], 'conversation': conversation}).encode()
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(payload), 330 if operation == 'generate' else 220)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {'result': 'outcome_unknown', 'submitted': 'unknown', 'account_key': key(profile)}
            if proc.returncode or len(raw) > 1024 * 1024:
                return {'result': 'outcome_unknown', 'submitted': 'unknown', 'account_key': key(profile)}
            try:
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get('result') not in ('checked', 'completed', 'check_failed',
                                                                                 'outcome_unknown', 'unsupported_runtime', 'invalid_input', 'internal_error', 'upload_failed', 'model_unavailable'):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return {'result': 'outcome_unknown', 'submitted': 'unknown', 'account_key': key(profile)}
            if operation == 'keepalive':
                result = await apply_renewed_cookie(context, cookies, result)
            else:
                result.pop('_rotated_cookie', None)
            result['account_key'] = key(profile)
            return result
    finally:
        lock.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operation', choices=['check', 'keepalive', 'generate', 'inventory'], required=True)
    parser.add_argument('--sdk-path', type=Path, required=True)
    parser.add_argument('--account')
    args = parser.parse_args()
    if args.operation == 'generate' and (not args.account or not re.fullmatch('[0-9a-f]{64}', args.account)):
        raise SystemExit('Generation requires the previously checked account.')
    try:
        supplied = json.load(sys.stdin) if args.operation == 'generate' else {}
        prompt = supplied.get('prompt') if isinstance(supplied, dict) else None
        files = supplied.get('files', []) if isinstance(supplied, dict) else []
        conversation = supplied.get('conversation') if isinstance(supplied, dict) else None
        if conversation is not None and (not isinstance(conversation, dict) or len(json.dumps(conversation)) > 16000):
            raise ValueError('invalid_conversation')
        if args.operation == 'generate' and (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000):
            raise ValueError('invalid_prompt')
        root = Path.home() / '.local/state/creative-unified-workbench/uploads'
        if not isinstance(files, list) or len(files) > 4 or root.is_symlink():
            raise ValueError('invalid_files')
        for name in files:
            if not isinstance(name, str):
                raise ValueError('invalid_files')
            path = Path(name)
            if (not path.is_absolute() or path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                    or path.parent.parent != root or not re.fullmatch(r'[0-9a-f-]{36}', path.parent.name)
                    or not re.fullmatch(r'[0-3]\.(jpg|jpeg|png|webp|mp4|mov|webm|pdf)', path.name)):
                raise ValueError('invalid_files')
        result = (browser_inventory() if args.operation == 'inventory' else
                  asyncio.run(execute(args.operation, args.sdk_path, args.account, prompt, files, conversation)))
    except Exception as exc:
        result = {'result': 'inspection_unavailable', 'error_type': type(exc).__name__,
                  'submitted': 'unknown' if args.operation == 'generate' else False}
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
