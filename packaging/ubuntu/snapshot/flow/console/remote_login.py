"""Per-profile plain Chrome, isolated X display, authenticated VNC relay only."""

import asyncio
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

from filelock import FileLock, Timeout


class RemoteLogins:
    def __init__(self, root, fixture=False):
        self.root = Path(root)
        self.fixture = fixture
        self.sessions = {}
        self.tools = self.root / "remote-tools"

    def supported(self):
        return bool(shutil.which("Xvfb") and (self.tools / "usr/bin/x11vnc").exists())

    def for_profile(self, profile):
        return next((s for s in self.sessions.values() if s["profile"] == profile), None)

    def public(self, s):
        return {
            "id": s["id"],
            "profile": s["profile"],
            "url": "/remote?session=" + s["id"],
            "expires": s["expires"],
        }

    def start(self, profile):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", profile):
            raise ValueError("账号名称不正确。")
        old = self.for_profile(profile)
        if old:
            return self.public(old)
        if len(self.sessions) >= 2:
            raise ValueError("最多同时打开两个远程登录窗口，请先完成一个。")
        if not self.supported():
            raise ValueError("远程登录组件尚未就绪。")
        target = self.root / "data/profiles" / profile
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        locks = self.root / "data/locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(locks / (profile + ".lock")), thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise ValueError("此账号正在执行任务，请结束后再远程登录。") from None
        ident = secrets.token_hex(24)
        folder = self.root / "console-data/remote" / ident
        folder.mkdir(parents=True, mode=0o700)
        procs = []
        try:
            # Only stop the explicitly managed browser for this account.
            if not self.fixture:
                unit = "studiowright-browser-" + profile
                subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
            for proc in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    args = proc.read_bytes().split(b"\0")
                    if ("--user-data-dir=" + str(target)).encode() in args:
                        raise ValueError(
                            "此账号仍有其他浏览器窗口占用，已停止打开，避免损坏登录资料。"
                        )
                except OSError:
                    pass
            display = next(
                n
                for n in range(120, 220)
                if not Path(f"/tmp/.X11-unix/X{n}").exists()
                and not Path(f"/tmp/.X{n}-lock").exists()
            )
            auth = folder / "Xauthority"
            auth.touch(mode=0o600)
            subprocess.run(
                ["xauth", "-f", str(auth), "add", ":" + str(display), ".", secrets.token_hex(16)],
                check=True,
                capture_output=True,
            )
            env = dict(
                os.environ,
                DISPLAY=":" + str(display),
                XAUTHORITY=str(auth),
                LD_LIBRARY_PATH=str(self.tools / "usr/lib/x86_64-linux-gnu"),
            )
            env.pop("WAYLAND_DISPLAY", None)

            def launch(command, name):
                with (folder / (name + ".log")).open("ab") as log:
                    p = subprocess.Popen(
                        command, env=env, stdout=log, stderr=log, start_new_session=True
                    )
                procs.append(p)
                return p

            launch(
                [
                    "Xvfb",
                    ":" + str(display),
                    "-screen",
                    "0",
                    "1280x800x24",
                    "-nolisten",
                    "tcp",
                    "-auth",
                    str(auth),
                ],
                "display",
            )
            for _ in range(50):
                if Path(f"/tmp/.X11-unix/X{display}").exists():
                    break
                time.sleep(0.1)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            launch(
                [
                    str(self.tools / "usr/bin/x11vnc"),
                    "-display",
                    ":" + str(display),
                    "-auth",
                    str(auth),
                    "-localhost",
                    "-rfbport",
                    str(port),
                    "-nopw",
                    "-forever",
                    "-shared",
                    "-noxdamage",
                    "-quiet",
                ],
                "vnc",
            )
            url = "https://flow.google.com/"
            if self.fixture:
                fixture = folder / "fixture.html"
                fixture.write_text(
                    '<html lang="zh"><meta charset="utf-8"><title>远程登录模拟验收</title><body style="font:24px sans-serif;padding:60px;background:#eef4ef"><h1>远程登录模拟验收</h1><p>独立测试浏览器，不连接 Google。</p><label>测试输入 <input id="sample" autofocus placeholder="在这里输入测试文字"></label></body></html>'
                )
                url = fixture.as_uri()
            launch(
                [
                    shutil.which("google-chrome"),
                    "--user-data-dir=" + str(target),
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--ozone-platform=x11",
                    "--window-size=1280,800",
                    "--window-position=0,0",
                    *(
                        ["--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND"]
                        if self.fixture
                        else []
                    ),
                    url,
                ],
                "chrome",
            )
            for _ in range(60):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3) as connection:
                        if connection.recv(12).startswith(b"RFB"):
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise ValueError("远程画面启动失败。")
            session = {
                "id": ident,
                "profile": profile,
                "port": port,
                "procs": procs,
                "lock": lock,
                "expires": time.time() + 1200,
                "connected": False,
            }
            self.sessions[ident] = session
            return self.public(session)
        except Exception:
            self._stop(procs)
            lock.release()
            raise

    @staticmethod
    def _stop(procs):
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=3)

    def close(self, ident):
        session = self.sessions.pop(ident, None)
        if session:
            self._stop(session["procs"])
            session["lock"].release()
        return session

    async def shutdown(self):
        for ident in list(self.sessions):
            await asyncio.to_thread(self.close, ident)
