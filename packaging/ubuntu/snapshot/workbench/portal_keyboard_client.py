"""Single-operation client for the local authorised keyboard helper."""
import asyncio
import json
import os
import socket
import stat
import subprocess
import time
from pathlib import Path


class PortalKeyboard:
    def __init__(self, target, runtime=None, gui_environment=None):
        self.root = Path(runtime or Path.home()/'.local/state/creative-unified-workbench/desktop-keyboard')
        self.path = self.root/'keyboard.sock'
        self.env = dict(gui_environment or os.environ)
        self.target = None if target is None else {'pid':target.process_id,'window':target.window_id,'workspace':target.workspace}

    def _helper_running(self, pid):
        if type(pid) is not int or pid <= 1: return False
        try:
            cmd = Path('/proc')/str(pid)/'cmdline'
            return str(self.root/'helper.py').encode() in cmd.read_bytes().split(b'\0')
        except OSError:
            return False

    def ready(self):
        """Read the existing desktop session without launching it or injecting keys."""
        try:
            root = self.root.lstat()
            status = self.root / 'status.json'
            meta, endpoint = status.lstat(), self.path.lstat()
            if (not stat.S_ISDIR(root.st_mode) or root.st_mode & 0o077
                    or not stat.S_ISREG(meta.st_mode) or not stat.S_ISSOCK(endpoint.st_mode)
                    or any(item.st_uid != os.getuid() for item in (root, meta, endpoint))):
                return False
            current = json.loads(status.read_text(encoding='utf-8'))
            return isinstance(current, dict) and current.get('state') == 'ready' and self._helper_running(current.get('pid'))
        except (OSError, ValueError):
            return False

    async def ensure_ready(self, timeout=250):
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise RuntimeError('desktop_keyboard_runtime_unavailable')
        helper=self.root/'helper.py'; status=self.root/'status.json'
        if helper.is_symlink() or not helper.is_file(): raise RuntimeError('desktop_keyboard_helper_unavailable')
        def read_status():
            if status.is_symlink(): raise RuntimeError('desktop_keyboard_status_unavailable')
            try: return json.loads(status.read_text(encoding='utf-8'))
            except (OSError, ValueError): return {}
        current=read_status(); pid=current.get('pid')
        if not self._helper_running(pid):
            if self.path.is_symlink(): raise RuntimeError('desktop_keyboard_socket_unavailable')
            if self.path.exists():
                info=self.path.stat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise RuntimeError('desktop_keyboard_socket_unavailable')
                self.path.unlink()
            proc=subprocess.Popen(['/usr/bin/python3','-B',str(helper),str(self.root)],env=self.env,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            pid=proc.pid
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            current=read_status()
            if current.get('pid')==pid and current.get('state')=='ready':
                if self.path.is_symlink() or not self.path.exists() or not stat.S_ISSOCK(self.path.stat().st_mode):
                    raise RuntimeError('desktop_keyboard_socket_unavailable')
                return
            if current.get('pid')==pid and current.get('state')=='failed':
                raise RuntimeError('desktop_keyboard_authorization_failed')
            if not self._helper_running(pid): raise RuntimeError('desktop_keyboard_helper_stopped')
            await asyncio.sleep(.2)
        raise RuntimeError('desktop_keyboard_authorization_timeout')

    async def _send(self, action, **values):
        if self.target is None:
            raise RuntimeError('desktop_keyboard_target_required')
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.path)), 3)
        try:
            data = {'action':action, 'target':self.target, **values}
            writer.write((json.dumps(data, allow_nan=False)+'\n').encode())
            await asyncio.wait_for(writer.drain(), 3)
            response = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if response.get('ok') is not True: raise RuntimeError('desktop_keyboard_unavailable')
        finally:
            writer.close()
            await writer.wait_closed()

    async def paste(self): await self._send('paste')
