"""Install or safely reuse StudioWright on Ubuntu.

The installer never imports existing account data into its payload. Reuse is
read-only. A fresh install refuses every pre-existing application/state path,
unit and listening port before writing anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

PACKAGE = Path(__file__).resolve().parent
UNITS = ('studiowright-console.service', 'gemini-hanaoka-trial.service',
         'creative-unified-workbench.service', 'creative-unified-api.service')
PORTS = (18888, 8084, 18920, 18922)


def command(*args, check=True, timeout=30, env=None):
    return subprocess.run([str(x) for x in args], check=check, timeout=timeout,
                          capture_output=True, text=True, env=env)


def unit_state(unit):
    try:
        p = command('systemctl', '--user', 'is-active', unit, check=False, timeout=5)
        return p.stdout.strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def health(port, expected):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open('http://127.0.0.1:%d/health' % port, timeout=3) as response:
            value = json.load(response)
        if expected is None:
            return isinstance(value, dict) and value.get('status') == 'ok' and value.get('project') == 'HanaokaYuzu/Gemini-API'
        return isinstance(value, dict) and value.get('application') == expected
    except (OSError, ValueError, URLError):
        return False


def workbench_health():
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open('http://127.0.0.1:18920/api/health', timeout=3) as response:
            value = json.load(response)
        return value.get('application') == 'unified-workbench' and value.get('deployment') == 'ubuntu'
    except (OSError, ValueError, URLError):
        return False


def status():
    return {
        'services': {u: unit_state(u) for u in UNITS},
        # The legacy Flow health endpoint creates a web session when accessed
        # locally. Read the service state and listening port instead.
        'flow': unit_state('studiowright-console.service') == 'active' and occupied(18888),
        'gemini': health(8084, None),
        'workbench': workbench_health(),
        'api': health(18922, 'studiowright-unified-api'),
    }


def verify_payload():
    manifest_path = PACKAGE / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not manifest:
        raise RuntimeError('安装包清单不正确。')
    for name, expected in manifest.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not isinstance(expected, str):
            raise RuntimeError('安装包清单含不安全路径。')
        path = PACKAGE / rel
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('安装包文件缺失：' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('安装包文件校验失败：' + name)


def check_reuse():
    info = status()
    if all(value == 'active' for value in info['services'].values()) and all(
            info[name] for name in ('flow', 'gemini', 'workbench', 'api')):
        print('已识别现有服务，直接复用；未更改程序、账号、素材或记录。')
        return True
    print('现有服务尚未全部就绪，未进行安装或修改。')
    print(json.dumps(info, ensure_ascii=False))
    return False


def occupied(port):
    with socket.socket() as sock:
        sock.settimeout(.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def paths(home):
    share = home / '.local/share'
    units = home / '.config/systemd/user'
    return {'flow': share / 'studiowright-evaluation',
            'gemini': share / 'gemini-hanaoka-trial',
            'workbench': share / 'creative-unified-workbench',
            'state': home / '.local/state/creative-unified-workbench',
            'units': units}


def preflight_new(home):
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise RuntimeError('此安装包目前支持 Ubuntu x86-64。')
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('当前安装包需要 Python 3.12 和 python3.12-venv。')
    needed = ('systemctl', 'dpkg-deb', 'Xvfb', 'xauth')
    missing = [name for name in needed if not shutil.which(name)]
    if missing:
        raise RuntimeError('缺少系统组件：' + '、'.join(missing))
    if not (shutil.which('google-chrome') or shutil.which('google-chrome-stable') or shutil.which('chromium')):
        raise RuntimeError('请先安装 Google Chrome 或 Chromium，再运行安装。')
    try:
        import ensurepip  # noqa: F401
        if command('systemctl', '--user', 'show-environment', check=False).returncode:
            raise RuntimeError('用户服务管理器不可用，请以普通桌面用户登录 Ubuntu。')
    except ImportError:
        raise RuntimeError('缺少 Python 虚拟环境组件，请安装 python3.12-venv。') from None
    roots = paths(home)
    conflict = [str(roots[k]) for k in ('flow', 'gemini', 'workbench', 'state')
                if roots[k].exists() or roots[k].is_symlink()]
    conflict += [str(roots['units'] / u) for u in UNITS
                 if (roots['units'] / u).exists() or (roots['units'] / u).is_symlink()]
    conflict += ['端口 ' + str(p) for p in PORTS if occupied(p)]
    for u in UNITS:
        if command('systemctl', '--user', 'cat', u, check=False).returncode == 0:
            conflict.append('服务 ' + u)
    if conflict:
        raise RuntimeError('检测到现有程序、数据或端口，已停止以保护现有内容：' + '、'.join(conflict))
    for name in ('flow', 'gemini'):
        wheel_dir = PACKAGE / 'wheels' / name
        if not wheel_dir.is_dir() or not any(wheel_dir.glob('*.whl')):
            raise RuntimeError('安装包缺少离线依赖：' + name)
    return roots


def copy_new(source, target):
    if target.exists() or target.is_symlink():
        raise RuntimeError('目标已存在，未覆盖：' + str(target))
    shutil.copytree(source, target, symlinks=False)


def unit_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_unit(unit_dir, name, program, workdir, environment=(), after='network.target'):
    parts = ['[Unit]', 'Description=StudioWright ' + name, 'After=' + after,
             '', '[Service]', 'Type=simple', 'WorkingDirectory=' + unit_quote(workdir)]
    parts.extend('Environment=' + unit_quote(k + '=' + str(v)) for k, v in environment)
    parts += ['ExecStart=' + ' '.join(unit_quote(v) for v in program),
              'Restart=on-failure', 'RestartSec=3', 'NoNewPrivileges=yes',
              'UMask=0077', '', '[Install]', 'WantedBy=default.target', '']
    with (unit_dir / name).open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(parts))


def make_venv(python, target, requirements, wheels):
    command(python, '-m', 'venv', target, timeout=90)
    interp = target / 'bin/python'
    command(interp, '-m', 'pip', 'install', '--no-input', '--no-index',
            '--find-links', wheels, '-r', requirements, timeout=300)
    return interp


def new_install(home):
    roots = preflight_new(home)
    verify_payload()
    flow, gemini, work, units = (roots[k] for k in ('flow', 'gemini', 'workbench', 'units'))
    # Every protected path and port has been checked before the first write.
    flow.mkdir(mode=0o700, parents=True, exist_ok=False)
    print('正在安装 Flow 程序和运行环境……', flush=True)
    copy_new(PACKAGE / 'snapshot/flow/source', flow / 'source')
    copy_new(PACKAGE / 'snapshot/flow/console', flow / 'console')
    copy_new(PACKAGE / 'snapshot/flow/remote-tools', flow / 'remote-tools')
    for item in (flow / 'remote-tools/packages').glob('*.deb'):
        command('dpkg-deb', '-x', item, flow / 'remote-tools', timeout=30)
    for name in ('data', 'output', 'material-library', 'bin', 'console-data'):
        (flow / name).mkdir(mode=0o700)
    browser = shutil.which('google-chrome') or shutil.which('google-chrome-stable') or shutil.which('chromium')
    (flow / 'bin/google-chrome').symlink_to(browser)
    flow_python = make_venv(sys.executable, flow / 'venv',
                            PACKAGE / 'snapshot/flow/requirements-frozen.txt',
                            PACKAGE / 'wheels/flow')
    gemini.mkdir(mode=0o700, parents=True, exist_ok=False)
    print('正在安装 Gemini 运行环境……', flush=True)
    for name in ('server.py', 'index.html', 'requirements.lock.txt'):
        shutil.copyfile(PACKAGE / 'snapshot/gemini' / name, gemini / name)
    gemini_python = make_venv(sys.executable, gemini / 'venv',
                              PACKAGE / 'snapshot/gemini/requirements-frozen.txt',
                              PACKAGE / 'wheels/gemini')
    access = gemini / 'access.json'
    fd = os.open(access, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump({'api_key': secrets.token_urlsafe(48)}, stream)
    copy_new(PACKAGE / 'snapshot/workbench', work)
    units.mkdir(parents=True, exist_ok=True)
    pythonpath = flow / 'source/src'
    common = [('PYTHONPATH', pythonpath), ('SW_ROOT', flow),
              ('PATH', str(flow / 'bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin'))]
    write_unit(units, UNITS[0], [flow_python, flow / 'console/server.py'], flow / 'console', common)
    write_unit(units, UNITS[1], [gemini_python, '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1',
                                '--port', '8084', '--no-access-log'], gemini)
    write_unit(units, UNITS[2], [flow_python, '-B', work / 'server.py', '--port', '18920',
                                '--flow-port', '18888', '--gemini-port', '8084', '--deployment', 'ubuntu'],
               work, common, 'network.target studiowright-console.service gemini-hanaoka-trial.service')
    write_unit(units, UNITS[3], [flow_python, '-B', work / 'unified_api.py'], work, common,
               'network.target creative-unified-workbench.service')
    command('systemctl', '--user', 'daemon-reload')
    for unit in UNITS:
        command('systemctl', '--user', 'enable', '--now', unit, timeout=45)
    for _ in range(30):
        info = status()
        if all(info[n] for n in ('flow', 'gemini', 'workbench', 'api')):
            print('安装完成，服务已启动。账号、素材和历史记录均为空，需自行接入或通过单独的迁移流程导入。')
            return
        time.sleep(1)
    raise RuntimeError('程序已安装，但服务尚未全部就绪，请检查服务状态；安装数据已保留。')


def main():
    parser = argparse.ArgumentParser(description='创作工作台 Ubuntu 服务端安装')
    parser.add_argument('--mode', choices=('reuse', 'new', 'inspect'))
    parser.add_argument('--enable-discovery', action='store_true', help='为现有服务启用局域网发现')
    parser.add_argument('--discovery-ssh-port', type=int, help='局域网发现使用的 SSH 端口')
    args = parser.parse_args()
    if not args.mode:
        print('1. 识别并复用已有服务（不会修改现有内容）')
        print('2. 在全新环境安装')
        print('3. 复用已有服务并启用局域网发现')
        choice = input('请选择 1、2 或 3：').strip()
        args.mode = {'1': 'reuse', '2': 'new', '3': 'reuse'}.get(choice)
        if choice == '3':
            args.enable_discovery = True
        if not args.mode:
            raise SystemExit('未执行安装。')
    verify_payload()
    if args.mode == 'inspect':
        print(json.dumps(status(), ensure_ascii=False))
    elif args.mode == 'reuse':
        if not check_reuse():
            raise SystemExit(2)
        if args.enable_discovery:
            enable_discovery(args.discovery_ssh_port)
    else:
        new_install(Path.home())
        enable_discovery(args.discovery_ssh_port)


def enable_discovery(ssh_port=None):
    module = runpy.run_path(str(PACKAGE / 'discovery_install.py'))
    result = module['install'](PACKAGE / 'discovery.py', ssh_port)
    print('局域网发现已开启；Windows 桌面版可自动查找。')
    return result


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('安装未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
