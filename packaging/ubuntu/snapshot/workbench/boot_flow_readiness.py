"""Recover registered Flow windows after the Ubuntu desktop starts."""
import asyncio
import json
import subprocess
import time
from urllib.request import ProxyHandler, Request, build_opener

from flow_browser_recovery import network_ready
from flow_browser_state import gui_env

BASE = 'http://127.0.0.1:18920'


def request(path, repair=False):
    headers = {'Origin': BASE, 'Content-Type': 'application/json', 'X-Workbench-Request': '1'}
    req = Request(BASE + path, data=b'{}' if repair else None, headers=headers)
    with build_opener(ProxyHandler({})).open(req, timeout=180 if repair else 10) as response:
        return json.load(response)


def prepare_keyboard(env):
    """Restore the shared portal keyboard helper after login; never block recovery."""
    try:
        from input_backend import open_keyboard
        keyboard = open_keyboard(None, gui_environment=env)
        if keyboard.ready():
            return True
        asyncio.run(keyboard.ensure_ready(timeout=45))
        return keyboard.ready()
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(f'桌面键盘暂未就绪（{type(error).__name__}）', flush=True)
        return False


def main():
    keyboard_ready = False
    keyboard_attempts = 0
    for attempt in range(6):
        try:
            env = gui_env()
            if ((env.get('WAYLAND_DISPLAY') or env.get('DISPLAY')) and not keyboard_ready
                    and keyboard_attempts < 3):
                keyboard_attempts += 1
                keyboard_ready = prepare_keyboard(env)
            if (env.get('WAYLAND_DISPLAY') or env.get('DISPLAY')) and network_ready():
                accounts = request('/api/flow-readiness')['accounts']
                if all(state in ('ok', 'busy') for state in accounts.values()):
                    return
                result = request('/api/flow-readiness/auto', repair=True)
                if all(state in ('ok', 'busy') for state in result['accounts'].values()):
                    return
        except (OSError, ValueError, KeyError) as error:
            print(f'Flow 开机检查暂缓（{type(error).__name__}）', flush=True)
        if attempt < 5:
            time.sleep(60)
    print('Flow 开机检查结束，后台将继续周期检查。', flush=True)


if __name__ == '__main__':
    main()
