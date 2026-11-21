"""Sync Flow warning screenshots to the Windows desktop until they are reviewed."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time


MAX_PNG_BYTES = 16 * 1024 * 1024
PNG_START = b'\x89PNG\r\n\x1a\n'
PNG_END = b'\x00\x00\x00\x00IEND\xae\x42\x60\x82'
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))


def remote_reader(configuration):
    data = json.loads(configuration.read_text(encoding='utf-8-sig'))
    host = ipaddress.IPv4Address(data['host'])
    if not host.is_private or host.is_loopback or host.is_unspecified or host.is_multicast or host.is_link_local or host.is_reserved:
        raise ValueError('Ubuntu 内网地址无效。')
    ssh = Path(os.environ['SystemRoot']) / 'System32/OpenSSH/ssh.exe'
    config = Path.home() / '.ssh/flow2api_ubuntu.conf'
    args = [
        str(ssh), '-T', '-F', str(config), '-o', f'HostName={host}',
        '-o', 'HostKeyAlias=xiao-system-product-name.local', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=8', 'flow-ubuntu',
    ]

    def read(*parts):
        command = 'python3 -B ~/.local/share/creative-unified-workbench/flow_warning_archive.py ' + shlex.join(parts)
        process = subprocess.run(args + [command], stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            raise OSError('Ubuntu 警告截图暂不可读取')
        return process.stdout

    return read


def capture_name(row):
    ident, task, account = row['id'], row['task_id'], row['account']
    if (not re.fullmatch(r'[0-9a-f]{12}', ident) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task)
            or not re.fullmatch(r'[A-Za-z0-9_.+-]{1,48}', account)):
        raise ValueError('无效的警告截图记录。')
    stamp = datetime.fromtimestamp(row['finished'], CHINA_STANDARD_TIME).strftime('%Y-%m-%d_%H-%M-%S')
    warning = '异常活动' if row['reason'] == 'unusual' else '生成警告'
    return f'{stamp}_{account}_{warning}_{task}_{ident}.png'


def save_png(target, data):
    if not 0 < len(data) <= MAX_PNG_BYTES or not data.startswith(PNG_START) or not data.endswith(PNG_END):
        raise ValueError('截图文件不完整。')
    fd, tmp = tempfile.mkstemp(prefix='.flow-warning-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        try:
            os.link(tmp, target)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != data:
                raise
    finally:
        Path(tmp).unlink(missing_ok=True)


def sync_once(read, destination, database):
    transferred = 0
    removed = 0
    failures = 0
    index = json.loads(read('sync'))
    if not isinstance(index, dict) or any(not isinstance(index.get(key), list) for key in ('active', 'reviewed')):
        raise ValueError('警告截图状态不可用。')
    database.execute('CREATE TABLE IF NOT EXISTS delivered(job_id TEXT PRIMARY KEY)')
    for row in index['reviewed']:
        try:
            target = destination / capture_name(row)
            if (database.execute('SELECT 1 FROM delivered WHERE job_id=?', (row['id'],)).fetchone()
                    and not target.is_symlink() and target.is_file()):
                target.unlink(missing_ok=True)
                removed += 1
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            failures += 1
    for row in index['active']:
        try:
            name = capture_name(row)
            ident = row['id']
            if database.execute('SELECT 1 FROM delivered WHERE job_id=?', (ident,)).fetchone():
                continue
            save_png(destination / name, read('read', ident))
            with database:
                database.execute('INSERT OR IGNORE INTO delivered VALUES(?)', (ident,))
            transferred += 1
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
            failures += 1
    return transferred, removed, failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--desktop', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    options = parser.parse_args()
    destination = options.desktop / 'Flow警告截图'
    if (not options.desktop.is_dir() or destination.is_symlink() or options.state.is_symlink()
            or destination.resolve().parent != options.desktop.resolve()):
        raise SystemExit('桌面截图目录不可用。')
    destination.mkdir(exist_ok=True)
    options.state.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=options.state / 'sync.log', encoding='utf-8', level=logging.INFO,
                        format='%(asctime)s %(message)s')
    previous_failures = None
    with closing(sqlite3.connect(options.state / 'delivered.sqlite3')) as database:
        while True:
            try:
                transferred, removed, failures = sync_once(remote_reader(options.configuration), destination, database)
                if transferred or removed or failures != previous_failures:
                    logging.info('已连接 Ubuntu，保存 %d 张截图，清除 %d 张，%d 项待处理。', transferred, removed, failures)
                previous_failures = failures
                if options.once:
                    print(json.dumps({'transferred': transferred, 'removed': removed, 'pending': failures}))
                    return bool(failures)
            except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError):
                if previous_failures != 'offline':
                    logging.info('Ubuntu 暂不可连接，下轮继续读取。')
                previous_failures = 'offline'
                if options.once:
                    return 1
            time.sleep(45)


if __name__ == '__main__':
    raise SystemExit(main())
