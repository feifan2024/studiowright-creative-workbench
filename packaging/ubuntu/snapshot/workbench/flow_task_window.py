"""Bind current Flow pages to native windows using live account ownership."""
import asyncio
import re
import sqlite3
import secrets
from urllib.parse import urlparse


def registration(account_key):
    from flow_browser_state import REGISTRY, _live_browser, _pages
    if not isinstance(account_key, str) or not re.fullmatch(r'[0-9a-f]{64}', account_key):
        return None
    if REGISTRY.is_symlink():
        return None
    with sqlite3.connect(REGISTRY.resolve().as_uri()+'?mode=ro', uri=True) as db:
        rows = db.execute('SELECT pid,port FROM flow_windows WHERE account_key=?', (account_key,)).fetchall()
    if len(rows) != 1:
        return None
    previous_pid, port = rows[0]
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    pid = _live_browser(account_key, previous_pid, port)
    targets = {p['id'] for p in _pages(account_key, pid, port) if isinstance(p.get('id'), str)}
    return {'pid': pid, 'port': port, 'targets': targets}


def _trusted(url):
    try:
        u = urlparse(url)
        return (u.scheme == 'https' and u.hostname == 'flow.google.com' and
                not u.username and not u.password and u.port in (None, 443))
    except (TypeError, ValueError):
        return False


def verified_task_binding(account_key, registered):
    from task_window_registry import TaskWindowRegistry, process_identity

    binding = TaskWindowRegistry().get(account_key)
    if (not binding or binding['binding_state'] != 'verified'
            or binding['browser_pid'] != registered['pid'] or binding['port'] != registered['port']
            or binding['page_target'] not in registered['targets']):
        return None
    identity = process_identity(registered['pid'], account_key, registered['port'])
    return binding if identity == (binding['profile'], binding['process_start']) else None


async def find_flow_page(*, context, account_key, desired_url, layout_call, preferred_page=None,
                         before_activation=None):
    from foreground_input import Target
    from task_window_registry import TaskWindowRegistry, canonical_project_url, process_identity
    from task_coordinator import record
    registry = TaskWindowRegistry()
    desired_url = canonical_project_url(desired_url)
    row = await asyncio.to_thread(registration, account_key)
    if not row:
        raise ValueError('flow_window_registration_stale')
    if preferred_page is not None and preferred_page not in context.pages:
        raise ValueError('flow_page_account_mismatch')
    profile, process_start = process_identity(row['pid'], account_key, row['port'])
    pages = [preferred_page] if preferred_page is not None else [p for p in context.pages if _trusted(p.url) and p.url.split('?',1)[0].split('#',1)[0].rstrip('/') == desired_url]
    if len(pages) != 1:
        raise ValueError('flow_desktop_page_ambiguous')
    for page in pages:
        if not _trusted(page.url) or canonical_project_url(page.url) != desired_url:
            raise ValueError('task_window_project_unconfirmed')
        channel = await context.new_cdp_session(page)
        try:
            target = (await channel.send('Target.getTargetInfo'))['targetInfo']['targetId']
            if target not in row['targets']:
                raise ValueError('flow_page_account_mismatch')
            chrome_window = await channel.send('Browser.getWindowForTarget', {'targetId': target})
            if type(chrome_window.get('windowId')) is not int:
                raise ValueError('flow_window_registration_stale')
            if before_activation is not None:
                await before_activation(page)
                if not _trusted(page.url) or canonical_project_url(page.url) != desired_url:
                    raise ValueError('task_window_project_unconfirmed')
            # A task target can be in an inactive tab and several windows can share
            # its ordinary title. A temporary title ticket maps this exact owned
            # CDP target to exactly one native window. No prompt/draft is edited.
            await page.bring_to_front()
            ticket = 'SW-task-window-' + secrets.token_hex(16)
            previous = await page.evaluate('(ticket) => { const before=document.title; document.title=ticket; return before; }', ticket)
            matched = []
            try:
                for attempt in range(20):
                    windows = await asyncio.to_thread(layout_call, 'List', row['pid'])
                    matched = [w for w in windows if w.get('title') in (ticket, ticket+' - Google Chrome', ticket+' - Chromium') and
                               type(w.get('id')) is int and w['id'] > 0 and type(w.get('workspace')) is int and w['workspace'] >= 0]
                    if len(matched) == 1:
                        break
                    if len(matched) > 1:
                        raise ValueError('task_window_binding_conflict')
                    await asyncio.sleep(.05)
            finally:
                await page.evaluate('({ticket, previous}) => { if(document.title===ticket) document.title=previous; }', {'ticket': ticket, 'previous': previous})
            if len(matched) != 1:
                raise ValueError('flow_desktop_page_ambiguous')
            # Re-read ownership after the asynchronous probe, before storing it.
            again = await asyncio.to_thread(registration, account_key)
            if not again or again['pid'] != row['pid'] or again['port'] != row['port'] or target not in again['targets']:
                raise ValueError('task_window_process_unconfirmed')
            if process_identity(row['pid'], account_key, row['port']) != (profile, process_start):
                raise ValueError('task_window_process_unconfirmed')
            current_window = await channel.send('Browser.getWindowForTarget', {'targetId': target})
            if current_window.get('windowId') != chrome_window['windowId'] or canonical_project_url(page.url) != desired_url:
                raise ValueError('task_window_identity_changed')
            w = matched[0]
            mode = registry.bind(account_key=account_key, profile=profile, browser_pid=row['pid'],
                process_start=process_start, port=row['port'], page_target=target,
                chrome_window=chrome_window['windowId'], window_id=w['id'], workspace=w['workspace'], project_url=desired_url)
            record('task_window_binding_'+mode, window_id=w['id'], workspace=w['workspace'])
            return page, Target(account_key, row['pid'], w['id'], w['workspace'])
        except Exception as exc:
            registry.mark_unconfirmed(account_key, str(exc))
            raise
        finally:
            await channel.detach()
    raise ValueError('flow_desktop_page_ambiguous')


async def confirm_flow_identity(target, layout_call):
    try:
        from task_window_registry import TaskWindowRegistry, process_identity
        row = await asyncio.to_thread(registration, target.account_key)
        if not row or row['pid'] != target.process_id:
            return False
        binding = TaskWindowRegistry().get(target.account_key)
        if (not binding or binding['binding_state'] != 'verified' or binding['browser_pid'] != target.process_id
                or binding['window_id'] != target.window_id or binding['workspace'] != target.workspace
                or binding['page_target'] not in row['targets'] or binding['port'] != row['port']):
            return False
        if process_identity(row['pid'], target.account_key, row['port']) != (binding['profile'], binding['process_start']):
            return False
        windows = await asyncio.to_thread(layout_call, 'List', row['pid'])
        return sum(w.get('id') == target.window_id and w.get('workspace') == target.workspace
                   for w in windows) == 1
    except (OSError, ValueError, sqlite3.Error):
        return False
