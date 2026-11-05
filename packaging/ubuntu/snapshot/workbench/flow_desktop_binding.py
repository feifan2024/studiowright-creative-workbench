"""Read-only Flow binding after Gemini desktop residency is retired.

Chrome window IDs and GNOME window IDs are deliberately kept separate.
No browser creation, focus changes, account recovery or metadata writes.
"""
import asyncio
import hashlib
from pathlib import Path
import sqlite3
from urllib.parse import urlparse


def registration(account_key):
    from desktop_layout import REGISTRY, gemini_residency_disabled
    from shared_session_controller import process_args
    if not gemini_residency_disabled() or REGISTRY.is_symlink():return None
    with sqlite3.connect(REGISTRY.as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute('SELECT pid,window_id,port FROM flow_windows WHERE account_key=?',(account_key,)).fetchall()
    if len(rows)!=1:return None
    pid,cdp_window,port=rows[0]
    args=process_args(str(pid))
    profiles=[Path(a.split('=',1)[1]).name for a in args if a.startswith('--user-data-dir=')]
    ports=[a.split('=',1)[1] for a in args if a.startswith('--remote-debugging-port=')]
    if len(profiles)!=1 or hashlib.sha256(profiles[0].encode()).hexdigest()!=account_key or ports!=[str(port)]:return None
    return {'pid':pid,'cdp_window':cdp_window,'port':port}


async def find_flow_page(*,context,account_key,desired_url,layout_call):
    from foreground_input import Target
    row=await asyncio.to_thread(registration,account_key)
    if not row:raise ValueError('flow_window_registration_stale')
    windows=await asyncio.to_thread(layout_call,'List',row['pid'])
    choices=[]
    for page in context.pages:
        url=urlparse(page.url)
        if url.scheme!='https' or url.hostname!='flow.google.com' or url.username or url.password or url.port not in (None,443):continue
        session=await context.new_cdp_session(page)
        try:
            info=await session.send('Browser.getWindowForTarget')
            if info['windowId']!=row['cdp_window']:continue
            title=await page.title()
            if not title or not await page.evaluate('() => document.visibilityState === "visible"'):continue
            matched=[w for w in windows if w.get('title') in (title,title+' - Google Chrome',title+' - Chromium') and type(w.get('id')) is int and type(w.get('workspace')) is int]
            if len(matched)!=1:continue
            w=matched[0]
            choices.append((page,Target(account_key,row['pid'],w['id'],w['workspace'])))
        finally:await session.detach()
    exact=[c for c in choices if c[0].url.split('?',1)[0].split('#',1)[0].rstrip('/')==desired_url.rstrip('/')]
    selected=exact if exact else choices
    if len(selected)!=1:raise ValueError('flow_desktop_page_ambiguous')
    return selected[0]


async def confirm_flow_identity(target,layout_call):
    try:
        row=await asyncio.to_thread(registration,target.account_key)
        if not row or row['pid']!=target.process_id:return False
        windows=await asyncio.to_thread(layout_call,'List',row['pid'])
        return sum(w.get('id')==target.window_id and w.get('workspace')==target.workspace for w in windows)==1
    except (OSError,ValueError,sqlite3.Error):return False
