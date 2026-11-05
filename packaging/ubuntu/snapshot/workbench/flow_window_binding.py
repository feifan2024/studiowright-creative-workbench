"""Bind a Flow project tab to its existing account and OS window."""
import asyncio
import hashlib
import shlex
from pathlib import Path
from foreground_input import Target

async def find_flow_target(session, page, layout_call):
    layouts = await asyncio.to_thread(layout_call, 'Status')
    matches = []
    for row in layouts:
        pid, workspace, account_key = row.get('pid'), row.get('workspace'), row.get('key')
        if type(pid) is not int or pid <= 0 or type(workspace) is not int or workspace < 0 or row.get('error'):
            continue
        try:
            raw = Path('/proc', str(pid), 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            args = [x.decode() for x in raw]
            if len(args) == 1:
                args = shlex.split(args[0])
            ports = [x.split('=', 1)[1] for x in args if x.startswith('--remote-debugging-port=')]
            paths = [x.split('=', 1)[1] for x in args if x.startswith('--user-data-dir=')]
            if len(ports) != 1 or ports[0] != str(session.port) or len(paths) != 1:
                continue
            profile = Path(paths[0]).name
            if hashlib.sha256(profile.encode()).hexdigest() != account_key:
                continue
            matches.append(row)
        except (OSError, UnicodeError, ValueError):
            continue
    if len(matches) != 1:
        raise ValueError('desktop_binding_unconfirmed')
    row = matches[0]
    windows = await asyncio.to_thread(layout_call, 'List', row['pid'])
    wanted = await session.window_id(page)
    candidates = set()
    for other in session.context.pages:
        try:
            if await session.window_id(other) != wanted:
                continue
            title = await other.title()
            if not title:
                continue
            for win in windows:
                if win.get('workspace') == row['workspace'] and win.get('title') in (title, title+' - Google Chrome', title+' - Chromium'):
                    if type(win.get('id')) is int and win['id'] > 0:
                        candidates.add(win['id'])
        except Exception:
            continue
    if len(candidates) != 1:
        raise ValueError('desktop_page_ambiguous')
    return Target(row['key'], row['pid'], candidates.pop(), row['workspace'])
