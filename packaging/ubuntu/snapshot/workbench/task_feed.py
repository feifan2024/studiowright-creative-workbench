"""Fan out changes to recorded task state, without touching the Flow queue."""
import ctypes
import json
import os
from pathlib import Path
import select
import struct
import sys
import threading
import time


class TaskFeed:
    def __init__(self, load, databases=()):
        self.load = load
        self.databases = tuple(map(Path, databases))
        self.changed = threading.Condition()
        self.subscribers = 0
        self.worker = None
        self.version = 0
        self.event = None

    def subscribe(self):
        with self.changed:
            if self.subscribers >= 8:
                return False
            self.subscribers += 1
            if self.worker is None:
                self.event = None
                self.worker = threading.Thread(target=self._run, name='task-feed', daemon=True)
                self.worker.start()
            return True

    def unsubscribe(self):
        with self.changed:
            self.subscribers -= 1

    def next_event(self, after, timeout=10):
        with self.changed:
            if self.changed.wait_for(lambda: self.event is not None and self.version != after, timeout):
                return self.version, self.event
            return after, b'event: heartbeat\ndata: {}\n\n'

    def _watch(self):
        if sys.platform != 'linux' or not self.databases:
            return None, {}
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            return None, {}
        names = {}
        for db in self.databases:
            # SQLite commits may write the database directly or append to its WAL.
            # Watch the parent so that WAL recreation and checkpoints are covered.
            wd = libc.inotify_add_watch(fd, os.fsencode(db.parent), 0x00000082)
            if wd >= 0:
                names.setdefault(wd, set()).update((os.fsencode(db.name), os.fsencode(db.name + '-wal')))
        if not names:
            os.close(fd)
            return None, {}
        return fd, names

    @staticmethod
    def _wait(fd, names):
        if fd is None:
            time.sleep(2.5)
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                return
            try:
                events = os.read(fd, 65536)
            except BlockingIOError:
                continue
            offset = 0
            while offset < len(events):
                wd, mask, _, length = struct.unpack_from('iIII', events, offset)
                offset += 16
                name = events[offset:offset + length].split(b'\x00', 1)[0]
                offset += length
                if wd == -1 or name in names.get(wd, ()) or mask & 0x4000:
                    # Coalesce SQLite writes that belong to the same operation.
                    time.sleep(0.15)
                    return

    def _run(self):
        fd, names = None, {}
        try:
            try:
                fd, names = self._watch()
            except (OSError, AttributeError):
                pass
            previous = None
            while True:
                with self.changed:
                    if not self.subscribers:
                        return
                try:
                    result, _ = self.load()
                    if result is None:
                        kind, payload = 'unavailable', '{}'
                    else:
                        kind = 'tasks'
                        payload = json.dumps({k: v for k, v in result.items() if k != 'checked_at'},
                                             ensure_ascii=False, separators=(',', ':'))
                except Exception:
                    kind, payload = 'unavailable', '{}'
                if (kind, payload) != previous:
                    previous = kind, payload
                    with self.changed:
                        self.version += 1
                        self.event = f'event: {kind}\ndata: {payload}\n\n'.encode()
                        self.changed.notify_all()
                try:
                    self._wait(fd, names)
                except OSError:
                    if fd is not None:
                        os.close(fd)
                    fd = None
        finally:
            if fd is not None:
                os.close(fd)
            with self.changed:
                self.worker = None
                if self.subscribers:
                    self.worker = threading.Thread(target=self._run, name='task-feed', daemon=True)
                    self.worker.start()
