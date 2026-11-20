"""Shared Flow admission and tracking locks, released automatically after a crash."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path
import time
import uuid

MAX_CONCURRENCY = 5
DEFAULT_CONCURRENCY = 2
_current = ContextVar('studiowright_dispatch_slot', default=None)


class DispatchBusy(ValueError):
    pass


def _open(path):
    if path.is_symlink():
        raise OSError('并发控制文件不可用。')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    if not os.fstat(fd).st_size:
        os.write(fd, b'0')
    return fd


def _lock(fd):
    try:
        if os.name == 'posix':
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except (BlockingIOError, PermissionError):
        return False
    except OSError as exc:
        if exc.errno in (11, 13, 35, 36):
            return False
        raise


class Slot:
    def __init__(self, fd, root):
        self.fd, self.root, self.pid = fd, root, os.getpid()

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


class DispatchLimit:
    def __init__(self, root):
        root = Path(root)
        if root.is_symlink():
            raise ValueError('并发控制目录不安全。')
        root.mkdir(parents=True, exist_ok=True, mode=0o700 if os.name == 'posix' else 0o777)
        self.root = root.resolve()
        self.config = self.root / 'limit.json'
        self.marker = self.root.parent / 'maintenance'

    def paused(self):
        # A private scoped stress batch suspends ordinary admissions only.
        stress = self.root / 'flow-stress-once.json'
        return (self.marker.exists() or self.marker.is_symlink() or stress.exists() or stress.is_symlink())

    @contextmanager
    def _guard(self, timeout=2):
        fd = _open(self.root / 'admission.lock')
        try:
            deadline = time.monotonic() + timeout
            while not _lock(fd):
                if time.monotonic() >= deadline:
                    raise DispatchBusy('并发设置正在更新，请稍后操作。')
                time.sleep(.01)
            yield
        finally:
            os.close(fd)

    def _limit(self):
        if self.config.is_symlink():
            raise ValueError('并发设置不可读取，任务已暂缓。')
        try:
            raw = self.config.read_bytes()
        except FileNotFoundError:
            return DEFAULT_CONCURRENCY
        if len(raw) > 1024:
            raise ValueError('并发设置损坏，请重新保存。')
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {'maximum'}:
            raise ValueError('并发设置损坏，请重新保存。')
        self.validate(data['maximum'])
        return data['maximum']

    @staticmethod
    def validate(value):
        if type(value) is not int or not 1 <= value <= MAX_CONCURRENCY:
            raise ValueError('最大并发只能选择 1～5。')

    def _slots(self):
        free, active = [], 0
        try:
            for index in range(MAX_CONCURRENCY):
                fd = _open(self.root / ('slot-' + str(index) + '.lock'))
                try:
                    locked = _lock(fd)
                except BaseException:
                    os.close(fd)
                    raise
                if locked:
                    free.append(fd)
                else:
                    active += 1
                    os.close(fd)
            return active, free
        except BaseException:
            for fd in free:
                os.close(fd)
            raise

    def _background_videos(self):
        count = 0
        for path in self.root.glob('video-background-*.lock'):
            fd = _open(path)
            try:
                count += not _lock(fd)
            finally:
                os.close(fd)
        return count

    def track_submitted_video(self, slot, profile):
        """Keep the accepted video visible to maintenance while freeing admission."""
        if slot.fd is None:
            return None
        key = sha256(profile.encode('utf-8')).hexdigest()
        with self._guard(timeout=.1):
            fd = _open(self.root / f'video-background-{key}.lock')
            try:
                if not _lock(fd):
                    return None
                slot.close()
                background = Slot(fd, self.root)
                fd = None
                return background
            finally:
                if fd is not None:
                    os.close(fd)

    def status(self):
        with self._guard():
            maximum = self._limit()
            occupied, free = self._slots()
            for fd in free:
                os.close(fd)
            background = self._background_videos()
            return {'maximum': maximum, 'active': occupied + background, 'occupied_slots': occupied,
                    'background_videos': background, 'minimum': 1,
                    'ceiling': MAX_CONCURRENCY, 'paused': self.paused(), 'scope': 'shared', 'available': True}

    def public_status(self):
        try:
            return self.status()
        except (OSError, ValueError):
            return {'available': False, 'maximum': None, 'active': None, 'minimum': 1,
                    'ceiling': MAX_CONCURRENCY, 'paused': True, 'scope': 'shared',
                    'error': '并发设置暂不可读取，任务已暂缓。'}

    def set_limit(self, value):
        self.validate(value)
        with self._guard():
            if self.paused():
                raise DispatchBusy('工作台正在更新，请稍后设置。')
            if self.config.is_symlink():
                raise OSError('并发设置文件不安全。')
            temp = self.root / ('.limit-' + uuid.uuid4().hex)
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(json.dumps({'maximum': value}).encode('utf-8'))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, self.config)
            finally:
                if temp.is_file() and not temp.is_symlink():
                    temp.unlink()
        return self.status()

    def try_acquire(self):
        if self.paused():
            return None
        free = []
        try:
            with self._guard(timeout=0):
                if self.paused():
                    return None
                maximum = self._limit()
                active, free = self._slots()
                if active >= maximum:
                    return None
                return Slot(free.pop(0), self.root)
        except (OSError, ValueError):
            # Invalid/unavailable configuration must not start provider work.
            return None
        finally:
            for fd in free:
                os.close(fd)

    @contextmanager
    def slot(self, timeout=60):
        current = _current.get()
        if (current and current.root == self.root and current.pid == os.getpid()
                and current.fd is not None):
            yield current
            return
        deadline = time.monotonic() + timeout
        slot = None
        while slot is None:
            if self.paused():
                raise DispatchBusy('工作台正在更新，本次没有发送。')
            slot = self.try_acquire()
            if slot is None:
                if time.monotonic() >= deadline:
                    raise DispatchBusy('并发等待超时，本次没有发送，请稍后操作。')
                time.sleep(.05)
        token = _current.set(slot)
        try:
            yield slot
        finally:
            _current.reset(token)
            slot.close()

    def wrap(self, function):
        @wraps(function)
        def run(*args, **kwargs):
            with self.slot():
                return function(*args, **kwargs)
        return run


def limited(function):
    @wraps(function)
    def run(self, *args, **kwargs):
        workbench = getattr(self, 'workbench', self)
        with workbench.dispatch_limit.slot():
            return function(self, *args, **kwargs)
    return run
