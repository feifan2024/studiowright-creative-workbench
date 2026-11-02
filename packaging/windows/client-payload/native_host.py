"""Minimal per-user native host; the connection token is protected by DPAPI."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import struct
import sys

if os.name == 'nt':
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]


def crypt(data, protect):
    source = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    incoming, outgoing = Blob(len(data), source), Blob()
    library = ctypes.windll.crypt32
    if protect:
        ok = library.CryptProtectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        ok = library.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    if not ok:
        raise OSError('Windows 用户加密不可用')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)


ROOT = Path(os.environ.get('LOCALAPPDATA', '')) / 'StudioWrightClient' / 'native'
CONFIG = ROOT / 'connection.json'


def perform(message):
    if not isinstance(message, dict):
        raise ValueError('消息格式不正确')
    op = message.get('action')
    if op == 'sw_get':
        if CONFIG.is_symlink():
            raise ValueError('连接配置不可用')
        if not CONFIG.exists():
            return {'ok': True, 'config': None}
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
        token = crypt(base64.b64decode(data['token']), False).decode('utf-8')
        return {'ok': True, 'config': {'apiUrl': data['apiUrl'], 'connectionToken': token}}
    if op != 'sw_set':
        raise ValueError('不支持的请求')
    config = message.get('config') or {}
    url, token = config.get('apiUrl'), config.get('connectionToken')
    if url != 'http://127.0.0.1:18888/api/plugin/session' or not isinstance(token, str) or not 20 <= len(token) <= 200:
        raise ValueError('连接配置不正确')
    data = {'apiUrl': url, 'token': base64.b64encode(crypt(token.encode(), True)).decode('ascii')}
    ROOT.mkdir(parents=True, exist_ok=True)
    temporary = ROOT / ('.connection-' + secrets.token_hex(16) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as out:
            json.dump(data, out)
        os.replace(temporary, CONFIG)
    finally:
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
    return {'ok': True}


def main():
    try:
        allowed = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))['allowed_origins']
        if len(sys.argv) < 2 or sys.argv[1] not in allowed:
            raise ValueError('插件身份不匹配')
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            raise ValueError('消息不完整')
        size = struct.unpack('<I', header)[0]
        if size > 65536:
            raise ValueError('消息过大')
        payload = sys.stdin.buffer.read(size)
        if len(payload) != size:
            raise ValueError('消息不完整')
        response = perform(json.loads(payload.decode('utf-8')))
    except Exception:
        response = {'ok': False, 'error': '本机连接组件无法处理请求，请重新连接。'}
    encoded = json.dumps(response, ensure_ascii=True).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('<I', len(encoded)) + encoded)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
