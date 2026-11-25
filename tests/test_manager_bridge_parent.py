import ctypes
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PAYLOAD = Path(__file__).resolve().parents[1] / 'packaging/windows/client-payload'


def process_alive(pid):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


@unittest.skipUnless(sys.platform == 'win32', 'Windows parent-death test')
class ManagerBridgeParent(unittest.TestCase):
    def test_bridge_exits_after_its_client_process_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / 'child.py'
            child.write_text(textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(PAYLOAD)!r})
                from manager_bridge import exit_when_parent_exits
                exit_when_parent_exits()
                print('READY', flush=True)
                time.sleep(60)
            """), encoding='utf-8')
            parent = root / 'parent.py'
            parent.write_text(textwrap.dedent(f"""
                import subprocess, sys
                child = subprocess.Popen([sys.executable, {str(child)!r}],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                print(child.pid, flush=True)
                print(child.stdout.readline().strip(), flush=True)
            """), encoding='utf-8')
            process = subprocess.Popen([sys.executable, str(parent)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                pid_line = process.stdout.readline().strip()
                ready = process.stdout.readline().strip()
                self.assertEqual(ready, 'READY', process.stderr.read())
                pid = int(pid_line)
                self.assertTrue(process_alive(pid))
                self.assertEqual(process.wait(timeout=5), 0)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and process_alive(pid):
                    time.sleep(.1)
                self.assertFalse(process_alive(pid))
            finally:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
