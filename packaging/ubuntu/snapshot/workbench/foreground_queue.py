"""Cross-process FIFO for the short foreground clipboard/input section.

No prompts, replies, account identifiers or clipboard content are stored here.
Responses can be awaited after the caller leaves the queue.
"""
import asyncio
from contextlib import asynccontextmanager, closing, contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid


class QueueTimeout(TimeoutError):
    pass


def process_start(pid):
    """Distinguish a live owner from a dead owner whose PID was reused."""
    try:
        raw = Path('/proc', str(pid), 'stat').read_text()
        fields = raw.rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except FileNotFoundError:
        return None
    except (OSError, IndexError):
        return '?'  # Cannot prove it is dead: retain its place.


class ForegroundQueue:
    def __init__(self, directory):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise ValueError('unsafe_queue_directory')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError('unsafe_queue_directory')
        if os.name != 'posix' or self.directory.stat().st_uid != os.getuid():
            raise ValueError('unsafe_queue_owner')
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'foreground-queue.sqlite3'
        if self.path.is_symlink():
            raise ValueError('unsafe_queue_file')
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('unsafe_queue_file')
        else:
            os.close(fd)
        with closing(self._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS tickets (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'token TEXT NOT NULL UNIQUE, pid INTEGER NOT NULL, start TEXT NOT NULL)')
        os.chmod(self.path, 0o600)

    def _connect(self, timeout=5):
        if self.path.is_symlink():
            raise ValueError('unsafe_queue_file')
        return sqlite3.connect(self.path, timeout=timeout, isolation_level=None)

    @staticmethod
    def _prune(db):
        for token, pid, start in db.execute('SELECT token,pid,start FROM tickets').fetchall():
            current = process_start(pid)
            if current is None or (current != '?' and current != start):
                db.execute('DELETE FROM tickets WHERE token=?', (token,))

    @contextmanager
    def acquire(self, timeout=60):
        token, pid = uuid.uuid4().hex, os.getpid()
        start = process_start(pid)
        if start in (None, '?'):
            raise ValueError('process_identity_unavailable')
        with closing(self._connect()) as db:
            db.execute('INSERT INTO tickets(token,pid,start) VALUES(?,?,?)', (token, pid, start))
        deadline = time.monotonic() + timeout
        try:
            while True:
                with closing(self._connect()) as db:
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        self._prune(db)
                        row = db.execute('SELECT token FROM tickets ORDER BY seq LIMIT 1').fetchone()
                        db.execute('COMMIT')
                    except BaseException:
                        db.execute('ROLLBACK')
                        raise
                if row and row[0] == token:
                    break
                if time.monotonic() >= deadline:
                    raise QueueTimeout('foreground_queue_timeout')
                time.sleep(.05)
            yield
        finally:
            with closing(self._connect()) as db:
                db.execute('DELETE FROM tickets WHERE token=?', (token,))

    @asynccontextmanager
    async def try_acquire_async(self):
        """Low priority maintenance never waits ahead of a task ticket."""
        token,pid=uuid.uuid4().hex,os.getpid();start=process_start(pid);acquired=False
        if start in (None,'?'):raise ValueError('process_identity_unavailable')
        with closing(self._connect(timeout=.1)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._prune(db)
                if db.execute('SELECT count(*) FROM tickets').fetchone()[0]==0:
                    db.execute('INSERT INTO tickets(token,pid,start) VALUES(?,?,?)',(token,pid,start));acquired=True
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK');raise
        try:yield acquired
        finally:
            if acquired:
                with closing(self._connect()) as db:db.execute('DELETE FROM tickets WHERE token=?',(token,))

    @asynccontextmanager
    async def acquire_async(self, timeout=60):
        """FIFO acquisition without blocking the service while another job waits."""
        token, pid = uuid.uuid4().hex, os.getpid()
        start = process_start(pid)
        if start in (None, '?'):
            raise ValueError('process_identity_unavailable')
        # Registration and deletion have short bounded SQLite waits and never
        # schedule background work which could outlive task cancellation.
        with closing(self._connect()) as db:
            db.execute('INSERT INTO tickets(token,pid,start) VALUES(?,?,?)', (token, pid, start))
        deadline = time.monotonic() + timeout
        try:
            while True:
                row = None
                try:
                    with closing(self._connect(timeout=.1)) as db:
                        db.execute('BEGIN IMMEDIATE')
                        try:
                            self._prune(db)
                            row = db.execute('SELECT token FROM tickets ORDER BY seq LIMIT 1').fetchone()
                            db.execute('COMMIT')
                        except BaseException:
                            db.execute('ROLLBACK')
                            raise
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower():
                        raise
                if row and row[0] == token:
                    break
                if time.monotonic() >= deadline:
                    raise QueueTimeout('foreground_queue_timeout')
                await asyncio.sleep(.05)
            yield
        finally:
            with closing(self._connect()) as db:
                db.execute('DELETE FROM tickets WHERE token=?', (token,))
