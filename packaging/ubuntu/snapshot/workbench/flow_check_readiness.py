"""Inspect fixed Flow pages without focusing, navigating, typing, or sending."""
import asyncio
import json
import re
import sys
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from flow_browser_state import _pages, focus_call
from task_window_registry import canonical_project_url, process_identity


def composer_expression(email):
    return r"""(() => {
      if (location.origin !== 'https://flow.google.com') return {origin:false};
      const w=window.WIZ_global_data||{};
      const valid=v=>typeof v==='string' && /^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$/i.test(v.trim());
      const emails=[...new Set((valid(w.oPEP7c)?[w.oPEP7c]:Object.values(w).filter(valid)).map(v=>v.trim().toLowerCase()))];
      const boxes=[...document.querySelectorAll("flow-prompt-box [contenteditable='true']")];
      const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility==='visible';
      const account=emails.length===1 && emails[0]===EXPECTED && !!(w.SNlM0e&&w.FdrFJe);
      return {origin:true,path:location.pathname,account,
        busy:[...document.querySelectorAll('[role=progressbar],[aria-busy=true]')].some(visible),
        overlay:[...document.querySelectorAll('[role=dialog],[aria-modal=true],flow-change-log-modal')].some(visible),
        draft:boxes.some(e=>(e.innerText||'').trim()) ||
          !!document.querySelector('flow-prompt-box button.chip-container img,flow-prompt-box .mention-chip') ||
          [...document.querySelectorAll('input[type=file]')].some(e=>e.files?.length),
        editor:boxes.filter(e=>visible(e) && e.getAttribute('aria-disabled')!=='true').length===1 &&
          [...document.querySelectorAll('flow-prompt-box button mat-icon')].some(i=>i.textContent.trim()==='arrow_forward'&&visible(i))};
    })()""".replace('EXPECTED', json.dumps(email.lower()))


async def inspect_page(account, binding):
    import websockets
    profile = account['profile']
    key = account['key'].removeprefix('flow-account-')
    if binding.get('binding_state') != 'verified' or binding.get('account_key') != key:
        return 'binding'
    pid, port = binding['browser_pid'], binding['port']
    if process_identity(pid, key, port) != (profile, binding['process_start']):
        return 'binding'
    pages = await asyncio.to_thread(_pages, key, pid, port)
    pages = [page for page in pages if page.get('id') == binding['page_target']]
    if len(pages) != 1:
        return 'binding'
    page = pages[0]
    if page['id'] != binding['page_target']:
        return 'binding'
    project = canonical_project_url(binding['project_url'])
    if canonical_project_url(page['url']) != project or project.rsplit('/', 1)[1] not in account['projects']:
        return 'project'
    windows = await asyncio.to_thread(focus_call, 'List', pid)
    if sum(w.get('id') == binding['window_id'] and w.get('workspace') == binding['workspace'] for w in windows) != 1:
        return 'binding'
    endpoint = urlsplit(page.get('webSocketDebuggerUrl', ''))
    if (endpoint.scheme, endpoint.netloc, endpoint.path, endpoint.query, endpoint.fragment) != (
            'ws', f'127.0.0.1:{port}', '/devtools/page/' + page['id'], '', ''):
        return 'binding'
    # Return flags only. Do not return prompt text or account credentials.
    expression = composer_expression(account['email'])
    async with websockets.connect(endpoint.geturl(), open_timeout=2, close_timeout=1, max_size=65536) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Browser.getWindowForTarget', 'params': {'targetId': page['id']}}))
        window = json.loads(await ws.recv()).get('result', {}).get('windowId')
        if window != binding['chrome_window']:
            return 'binding'
        await ws.send(json.dumps({'id': 2, 'method': 'Runtime.evaluate',
                                  'params': {'expression': expression, 'returnByValue': True}}))
        state = json.loads(await ws.recv()).get('result', {}).get('result', {}).get('value', {})
    if not state.get('origin') or 'https://flow.google.com' + state.get('path', '').rstrip('/') != project:
        return 'project'
    if not state.get('account'):
        return 'login'
    if state.get('busy'):
        return 'busy'
    if state.get('draft'):
        return 'draft'
    if state.get('overlay'):
        return 'overlay'
    return 'ok' if state.get('editor') else 'editor'


async def probe(data):
    bindings = {b['profile']: b for b in data['bindings']}
    busy = set(data['busy'])
    async def check(account):
        profile = account['profile']
        if profile in busy:
            return profile, 'busy'
        try:
            async with asyncio.timeout(6):
                return profile, await inspect_page(account, bindings.get(profile, {}))
        except Exception:
            return profile, 'unconfirmed'
    return dict(await asyncio.gather(*(check(a) for a in data['accounts'])))


if __name__ == '__main__':
    print(json.dumps(asyncio.run(probe(json.load(sys.stdin)))))
