"""Install only the optional LAN locator, preserving the existing application.

It advertises the Ubuntu SSH endpoint; the workbench remains bound to localhost.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

UNIT = 'studiowright-discovery.service'
UNITS = ('studiowright-console.service', 'gemini-hanaoka-trial.service',
         'creative-unified-workbench.service', 'creative-unified-api.service')
PORT = 18925
MAGIC = b'STUDIOWRIGHT_DISCOVER_V1\n'


def command(*args, check=True):
    return subprocess.run([str(x) for x in args], text=True, capture_output=True,
                          timeout=20, check=check)


def status():
    result = {}
    for name in UNITS:
        if command('systemctl', '--user', 'is-active', name, check=False).stdout.strip() != 'active':
            raise RuntimeError('现有服务尚未就绪：' + name)
        pid = int(command('systemctl', '--user', 'show', name, '--property=MainPID', '--value').stdout.strip())
        if pid <= 0:
            raise RuntimeError('无法确认现有服务状态：' + name)
        result[name] = pid
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:18920/api/health', timeout=4) as response:
        info = json.load(response)
    if info.get('application') != 'unified-workbench' or info.get('deployment') != 'ubuntu':
        raise RuntimeError('未识别到 Ubuntu 工作台。')
    return result


def locally_responds():
    # systemd marks Type=simple active before Python has bound its socket.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.8)
            sock.sendto(MAGIC, ('127.0.0.1', PORT))
            try:
                data, peer = sock.recvfrom(1024)
            except socket.timeout:
                time.sleep(0.25)
                continue
        offer = json.loads(data)
        if peer[0] == '127.0.0.1' and offer.get('product') == 'StudioWright' and offer.get('workbench_port') == 18920:
            return True
    return False


def install(source: Path, ssh_port: int | None = None):
    if sys.platform != 'linux' or not source.is_file() or source.is_symlink():
        raise RuntimeError('局域网发现组件不完整。')
    if ssh_port is None:
        connection = os.environ.get('SSH_CONNECTION', '').split()
        ssh_port = int(connection[3]) if len(connection) == 4 else 22
    if not 1 <= ssh_port <= 65535:
        raise RuntimeError('SSH 端口不正确。')
    if command('systemctl', '--user', 'show-environment', check=False).returncode:
        raise RuntimeError('Ubuntu 用户服务不可用。')
    before = status()
    home = Path.home()
    target = home / '.local/share/studiowright-discovery'
    script = target / 'discovery.py'
    units = home / '.config/systemd/user'
    unit = units / UNIT
    unit_data = ('[Unit]\nDescription=StudioWright LAN discovery\nAfter=network.target\n\n'
                 '[Service]\nType=simple\nExecStart=/usr/bin/python3 -B %%h/.local/share/studiowright-discovery/discovery.py --ssh-port %d\n'
                 'Restart=on-failure\nRestartSec=5\nNoNewPrivileges=yes\nUMask=0077\n\n'
                 '[Install]\nWantedBy=default.target\n' % ssh_port)
    installed = target.exists() or target.is_symlink() or unit.exists() or unit.is_symlink()
    if installed:
        same = (target.is_dir() and not target.is_symlink() and script.is_file() and not script.is_symlink()
                and unit.is_file() and not unit.is_symlink()
                and hashlib.sha256(script.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
                and unit.read_text(encoding='utf-8') == unit_data)
        if same and command('systemctl', '--user', 'is-active', UNIT, check=False).stdout.strip() == 'active' and locally_responds():
            return {'discovery_ready': True, 'existing_unchanged': True, 'already_installed': True}
        raise RuntimeError('发现组件位置已有内容，未覆盖。')
    if command('systemctl', '--user', 'cat', UNIT, check=False).returncode == 0:
        raise RuntimeError('同名服务已存在，未覆盖。')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(('', PORT))
    target_created = unit_created = False
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
        target_created = True
        with script.open('xb') as out:
            out.write(source.read_bytes())
        units.mkdir(parents=True, exist_ok=True)
        with unit.open('x', encoding='utf-8') as out:
            out.write(unit_data)
        unit_created = True
        command('systemctl', '--user', 'daemon-reload')
        command('systemctl', '--user', 'enable', '--now', UNIT)
        if command('systemctl', '--user', 'is-active', UNIT).stdout.strip() != 'active':
            raise RuntimeError('局域网发现服务未能启动。')
        if not locally_responds():
            raise RuntimeError('局域网发现服务未能响应。')
        if status() != before:
            raise RuntimeError('现有服务状态发生变化，需要人工检查。')
        return {'discovery_ready': True, 'existing_unchanged': True, 'already_installed': False}
    except BaseException:
        if unit_created:
            command('systemctl', '--user', 'disable', '--now', UNIT, check=False)
            if unit.is_file() and not unit.is_symlink() and unit.read_text(encoding='utf-8') == unit_data:
                unit.unlink()
            command('systemctl', '--user', 'daemon-reload', check=False)
        if target_created and target.is_dir() and not target.is_symlink():
            if script.is_file() and not script.is_symlink() and script.read_bytes() == source.read_bytes():
                script.unlink()
            try:
                target.rmdir()
            except OSError:
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description='启用 StudioWright 局域网发现')
    parser.add_argument('--ssh-port', type=int)
    args = parser.parse_args()
    result = install(Path(__file__).with_name('discovery.py'), args.ssh_port)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('未能启用局域网发现：' + str(error), file=sys.stderr)
        raise SystemExit(1)
