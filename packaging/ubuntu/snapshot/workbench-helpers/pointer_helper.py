"""Session-local pointer helper for GNOME's authorised remote desktop portal.

Run with system Python (PyGObject). No keyboard, screen capture, or clipboard
access is requested. The socket is accessible only to the current Unix user.
"""
import json
import os
import pathlib
import signal
import socket
import sys
import time
import uuid
from gi.repository import Gio, GLib

DEST = 'org.freedesktop.portal.Desktop'
PATH = '/org/freedesktop/portal/desktop'
REMOTE = 'org.freedesktop.portal.RemoteDesktop'
FOCUS = 'local.studiowright.Foreground'
FOCUS_PATH = '/local/studiowright/Foreground'
ROOT = pathlib.Path(sys.argv[1])
ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
if ROOT.is_symlink() or ROOT.stat().st_uid != os.getuid() or ROOT.stat().st_mode & 0o077:
    raise RuntimeError('unsafe_runtime_directory')
socket_path = ROOT / 'pointer.sock'
status_path = ROOT / 'status.json'

def status(value):
    temp = ROOT / ('.status-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump(value, f)
    os.replace(temp, status_path)

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
responses = {}
def reply(connection, sender, object_path, interface, signal, params, data):
    responses[object_path] = params.unpack()
sub = bus.signal_subscribe(DEST, 'org.freedesktop.portal.Request', 'Response', None, None,
                           Gio.DBusSignalFlags.NONE, reply, None)

def call(destination, path, interface, method, sig=None, args=None):
    params = GLib.Variant(sig, args) if sig else None
    return bus.call_sync(destination, path, interface, method, params, None,
                         Gio.DBusCallFlags.NONE, 10000, None).unpack()

def request(method, sig, args, timeout=240):
    request_path = call(DEST, PATH, REMOTE, method, sig, args)[0]
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while request_path not in responses:
        while context.pending(): context.iteration(False)
        if time.monotonic() > end: raise TimeoutError(method)
        time.sleep(.02)
    code, data = responses.pop(request_path)
    if code: raise RuntimeError(method + '_not_granted')
    return data

def confirm(target):
    raw = call(FOCUS, FOCUS_PATH, FOCUS, 'Status')[0]
    state = json.loads(raw)
    return (state.get('pid') == target['pid'] and state.get('id') == target['window'] and
            state.get('workspace') == target['workspace'] and
            state.get('window_workspace') == target['workspace'])

listener = None
session = None
failed = False
session_closed = False
session_subscription = None
def terminate(signum, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)

def on_session_closed(connection, sender, object_path, interface, signal_name, params, data):
    global session_closed
    session_closed = True

try:
    if socket_path.exists() or socket_path.is_symlink(): raise RuntimeError('socket_already_exists')
    token = uuid.uuid4().hex
    status({'state': 'starting', 'pid': os.getpid()})
    session = request('CreateSession','(a{sv})',({'handle_token':GLib.Variant('s','swcreate'+token),
        'session_handle_token':GLib.Variant('s','swsession'+token)},))['session_handle']
    token_path = ROOT / 'restore-token'
    opts = {'handle_token':GLib.Variant('s','swdevices'+token), 'types':GLib.Variant('u',2),
            'persist_mode':GLib.Variant('u',2)}
    if token_path.exists():
        info = token_path.lstat()
        if not token_path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o077 and 0 < info.st_size <= 1024:
            opts['restore_token'] = GLib.Variant('s',token_path.read_text())
    request('SelectDevices','(oa{sv})',(session,opts))
    status({'state': 'awaiting_consent', 'pid': os.getpid()})
    result = request('Start','(osa{sv})',(session,'',{'handle_token':GLib.Variant('s','swstart'+token)}))
    if not (result.get('devices',0) & 2): raise RuntimeError('pointer_permission_not_granted')
    refreshed = result.get('restore_token')
    if isinstance(refreshed,str) and 0 < len(refreshed) <= 1024:
        temporary=ROOT/('.restore-'+uuid.uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:f.write(refreshed)
        os.replace(temporary,token_path)
    session_subscription=bus.signal_subscribe(DEST,'org.freedesktop.portal.Session','Closed',session,None,
                                               Gio.DBusSignalFlags.NONE,on_session_closed,None)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path)); os.chmod(socket_path, 0o600); listener.listen(4); listener.settimeout(.1)
    status({'state':'ready', 'pid':os.getpid()})
    while True:
        while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
        if session_closed: break
        try: client,_ = listener.accept()
        except socket.timeout: continue
        with client:
            client.settimeout(2)
            try:
                raw = b''
                while not raw.endswith(b'\n') and len(raw) <= 4096:
                    part=client.recv(4097-len(raw))
                    if not part: break
                    raw += part
                if len(raw) > 4096: raise ValueError('request_too_large')
                req=json.loads(raw); target=req['target']; action=req['action']
                if not confirm(target): raise ValueError('window_focus_unconfirmed')
                if action == 'move':
                    dx,dy=float(req['dx']),float(req['dy'])
                    if not (-100000 <= dx <= 100000 and -100000 <= dy <= 100000): raise ValueError('invalid_motion')
                    call(DEST,PATH,REMOTE,'NotifyPointerMotion','(oa{sv}dd)',(session,{},dx,dy))
                elif action == 'scroll':
                    steps=int(req['steps'])
                    if steps not in (-1,1): raise ValueError('invalid_scroll')
                    call(DEST,PATH,REMOTE,'NotifyPointerAxisDiscrete','(oa{sv}ui)',(session,{},0,steps))
                elif action == 'click':
                    call(DEST,PATH,REMOTE,'NotifyPointerButton','(oa{sv}iu)',(session,{},272,1))
                    try: pass
                    finally: call(DEST,PATH,REMOTE,'NotifyPointerButton','(oa{sv}iu)',(session,{},272,0))
                else: raise ValueError('invalid_action')
                client.sendall(b'{"ok":true}\n')
            except Exception as exc:
                client.sendall((json.dumps({'ok':False,'error':type(exc).__name__})+'\n').encode())
except Exception as exc:
    failed = True
    status({'state':'failed','error':type(exc).__name__+':'+str(exc)[:120], 'pid':os.getpid()})
    raise
finally:
    if listener:
        listener.close()
        if socket_path.exists() and not socket_path.is_symlink(): socket_path.unlink()
    if session:
        try: call(DEST,session,'org.freedesktop.portal.Session','Close')
        except Exception: pass
    if session_subscription: bus.signal_unsubscribe(session_subscription)
    bus.signal_unsubscribe(sub)
    if not failed: status({'state':'stopped','pid':os.getpid()})
