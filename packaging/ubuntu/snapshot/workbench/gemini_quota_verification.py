"""Open official quota verification in the enrolled account's desktop browser."""
import json
import re
import threading
import time
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def status(reader):
    with reader.lock:
        return dict(reader.verification)


def start(reader):
    try:
        accounts = (reader.runtime.settings() or {}).get('accounts', {})
    except (OSError, ValueError, TypeError):
        accounts = {}
    with reader.lock:
        if reader.verification['state'] in ('starting', 'waiting', 'rechecking'):
            return dict(reader.verification)
        now = time.time()
        plan = [(key, link) for key, (link, stamp) in reader.verification_links.items()
                if key in accounts and now-stamp <= 600 and reader.rows.get(key, {}).get('status') == 'verification_required']
        reader.verification = {'state': 'starting' if plan else 'completed',
                               'total':len(plan), 'completed':0, 'failed':0}
        if plan:
            threading.Thread(target=run, args=(reader,plan), daemon=True,
                             name='gemini-quota-verification').start()
        return dict(reader.verification)


def run(reader, plan):
    def update(**fields):
        with reader.lock:
            reader.verification.update(fields)
    complete = failed = 0
    for key, link in plan:
        try:
            account = reader.runtime.account(key)
            if not account:
                raise ValueError('authorization_unavailable')
            update(state='waiting',email=account['email'])
            browser, target = open_tab(key, link)
            deadline = time.monotonic()+300
            while time.monotonic() < deadline:
                tab = next((p for p in browser.pages() if p.get('id')==target),None)
                if tab is None or is_callback(tab.get('url','')):
                    break
                time.sleep(2)
            else:
                update(state='stopped',reason='verification_wait_timeout')
                return
            update(state='rechecking')
            result=reader.check(key)
            if result.get('status') == 'ok':
                complete += 1
                if tab and is_callback(tab.get('url','')):
                    browser.request('/json/close/'+target)
            else:
                failed += 1
            update(completed=complete,failed=failed)
        except Exception:
            update(state='stopped',reason='verification_page_unavailable')
            return
    update(state='completed',email=None)


def is_callback(value):
    u=urlsplit(value)
    return (u.scheme,u.netloc,u.path)==(
        'https','developers.google.com','/gemini-code-assist/auth/auth_success_gemini')


class Browser:
    def __init__(self, key, binding):
        self.key=key;self.binding=binding
        self.base='http://127.0.0.1:'+str(binding['port'])
        self.opener=build_opener(ProxyHandler({}))

    def request(self, path, method='GET'):
        from task_window_registry import process_identity
        b=self.binding
        if process_identity(b['browser_pid'],self.key,b['port']) != (b['profile'],b['process_start']):
            raise ValueError('browser_identity_changed')
        with self.opener.open(Request(self.base+path,method=method),timeout=4) as res:
            raw=res.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:
            raise ValueError('browser_inventory_too_large')
        return raw

    def pages(self):
        return [p for p in json.loads(self.request('/json/list')) if p.get('type')=='page']


def open_tab(key, link):
    from flow_browser_state import focus_call
    from task_window_registry import TaskWindowRegistry
    from official_quota import verification_entry
    if verification_entry(link) != link:
        raise ValueError('verification_url_untrusted')
    b=TaskWindowRegistry().get(key)
    if not b or b.get('binding_state')!='verified':
        raise ValueError('browser_binding_unverified')
    browser=Browser(key,b)
    pages=browser.pages()
    if not any(p.get('id')==b['page_target'] and p.get('url','').startswith('https://flow.google.com/') for p in pages):
        raise ValueError('browser_page_unverified')
    if not focus_call('Activate',b['browser_pid'],b['window_id']).get('ok'):
        raise ValueError('browser_window_unavailable')
    page=json.loads(browser.request('/json/new?'+quote(link,safe=''),'PUT'))
    target=page.get('id','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',target) or any(p.get('id')==target for p in pages):
        raise ValueError('verification_tab_unverified')
    return browser,target
