"""Create one project in an already-running, identity-checked Flow profile."""
import asyncio
import json
import re
import sys
import uuid

from filelock import FileLock, Timeout
from playwright.async_api import async_playwright
import shared_session_controller as controller

provider_call_started = False


async def main(profile):
    global provider_call_started
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
        raise ValueError('invalid profile')
    chrome, paths = controller.flow_runtime()
    from adapter import READ_STATE
    from browser_rendering import RenderingStudio as Studio
    state = controller.state()
    accounts = controller.eligible_accounts(state['accounts'])
    matched = [a for a in accounts if a.get('profile') == profile]
    running = controller.running_accounts(state['accounts'], chrome, paths)
    if len(matched) != 1 or profile not in running:
        raise ValueError('account not ready')
    email = matched[0]['email'].strip().lower()
    if email.count('@') != 1 or not all(email.split('@')):
        raise ValueError('account email unavailable')
    title = 'FLOW+' + email.split('@', 1)[0]
    guard = FileLock(str(paths.locks_dir() / (profile + '.lock')))
    try:
        guard.acquire(timeout=0)
    except Timeout:
        raise ValueError('account busy') from None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(
                'http://127.0.0.1:' + str(running[profile]), timeout=4000, no_defaults=True)
            contexts = [c for c in browser.contexts if any(p.url.startswith('https://flow.google.com/') for p in c.pages)]
            if len(contexts) != 1:
                raise ValueError('identity unavailable')
            states = [await p.evaluate(READ_STATE) for p in contexts[0].pages if p.url.startswith('https://flow.google.com/')]
            if not any(isinstance(s, dict) and s.get('signed_in') and s.get('emails') == [email] for s in states):
                raise ValueError('identity unavailable')
            # Existing Studio owns the actual provider operation and records its own project metadata.
            studio = Studio(profile)
            studio.configure_task('project-' + uuid.uuid4().hex)
            async with studio:
                provider_call_started = True
                project = await studio.create_project(title)
            return {'project': project}
    finally:
        guard.release()


if __name__ == '__main__':
    try:
        value = json.load(sys.stdin)
        print(json.dumps(asyncio.run(main(value.get('profile')))))
    except Exception:
        print(json.dumps({'result': 'unknown' if provider_call_started else 'not_started'}))
