"""Read-only, separately stored synthetic conversation records."""
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

MAX_BYTES = 256 * 1024

def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('模拟记录编号不正确。') from None
    return value

def _folder(state_dir):
    state = Path(state_dir)
    folder = state / 'simulated-conversations'
    if state.is_symlink() or folder.is_symlink() or not folder.resolve().is_relative_to(state.resolve()):
        raise ValueError('模拟记录目录不安全。')
    return folder

def detail(state_dir, identifier):
    identifier = _id(identifier)
    folder = _folder(state_dir)
    path = folder / (identifier + '.json')
    if not folder.exists() or not path.exists():
        return None
    if path.is_symlink():
        raise ValueError('模拟记录文件不安全。')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('模拟记录文件不正确。')
        record = json.loads(stream.read(MAX_BYTES + 1).decode('utf-8'))
    if (not isinstance(record, dict) or record.get('id') != identifier or record.get('simulated') is not True
            or record.get('source') != 'isolated_synthetic_provider'
            or not isinstance(record.get('title'), str) or not 1 <= len(record['title']) <= 100
            or not all(type(record.get(k)) in (int, float) and math.isfinite(record[k]) for k in ('created', 'updated'))
            or not isinstance(record.get('turns'), list) or not 1 <= len(record['turns']) <= 100):
        raise ValueError('模拟记录内容不正确。')
    turns = []
    first_account = None
    for index, turn in enumerate(record['turns'], 1):
        if (not isinstance(turn, dict) or turn.get('ordinal') != index
                or not isinstance(turn.get('prompt'), str) or not 1 <= len(turn['prompt']) <= 20000
                or not isinstance(turn.get('response'), str) or len(turn['response']) > 200000
                or turn.get('status') not in ('completed', 'failed', 'unknown')
                or type(turn.get('attempts')) is not int or not 0 <= turn['attempts'] <= 100
                or type(turn.get('elapsed_ms')) is not int or not 0 <= turn['elapsed_ms'] <= 3600000):
            raise ValueError('模拟对话内容不正确。')
        account = turn.get('account_label')
        if account is not None and (not isinstance(account, str) or not re.fullmatch(r'模拟账号 [1-9][0-9]{0,2}', account)):
            raise ValueError('模拟账号标识不正确。')
        if first_account is None and account:
            first_account = account
        turns.append({'ordinal': index, 'prompt': turn['prompt'], 'response': turn['response'],
                      'status': turn['status'], 'account_label': account,
                      'matches_first': account == first_account if account and first_account else None,
                      'attempts': turn['attempts'], 'elapsed_ms': turn['elapsed_ms']})
    return {'id': identifier, 'title': record['title'], 'created': record['created'], 'updated': record['updated'],
            'simulated': True, 'turns': turns}

def listing(state_dir):
    folder = _folder(state_dir)
    if not folder.exists():
        return {'items': []}
    if not folder.is_dir():
        raise ValueError('模拟记录目录不可读取。')
    paths = list(folder.glob('*.json'))
    if len(paths) > 500:
        raise ValueError('模拟记录数量超出上限。')
    items = []
    for path in paths:
        record = detail(state_dir, path.stem)
        if record:
            items.append({key: record[key] for key in ('id', 'title', 'created', 'updated', 'simulated')})
    items.sort(key=lambda item: item['updated'], reverse=True)
    return {'items': items}
