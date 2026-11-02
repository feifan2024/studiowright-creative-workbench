import asyncio
import contextlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from account_pacing import account_status, resume_after_review
from adapter import PreSubmissionTransient
from api_keys import APIKeys
from account_insights import Insights
from console_store import Store
from web_auth import WebAuth
from login_sessions import Vault, normalize_bundle
from material_library import Library
from material_tags import DIMENSIONS
from remote_login import RemoteLogins

HERE = Path(__file__).resolve().parent
PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
KINDS = {"sync", "login", "media", "doctor", "quote", "image", "video"}


def safe_error(exc):
    text = str(exc)[:1800]
    text = re.sub(r"https?://[^\s<>\"\)]+", "[服务地址已省略]", text)
    text = re.sub(
        r"(?i)(cookie|csrf|authorization|token|sapisid|sid)\s*[:=]\s*\S+", r"\1=[已隐藏]", text
    )
    return text


class Console:
    def __init__(self, root, adapter, test=False):
        self.root, self.adapter, self.test = Path(root), adapter, test
        self.store = Store(self.root / "console-data")
        self.keys = APIKeys(self.store)
        self.insights = Insights(self.store, self.root / "data/studiowright.db")
        self.balance_wake = asyncio.Event()
        self.balance_refreshing = False
        self.library = Library(self.root / "material-library")
        self.vault = Vault(self.root / "console-data")
        self.remote = RemoteLogins(self.root, fixture=test)
        self.remote_gate = asyncio.Lock()
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS bridge_devices(hash TEXT PRIMARY KEY,created REAL)"
        )
        self.busy = set()
        self.running = set()
        self.worker = None
        for profile in adapter.discover():
            if not self.store.account(profile):
                self.store.add_account(profile, profile)

    async def execute(self, task):
        # This second check is before the adapter is called, so a racing restriction
        # can safely leave the existing task in the queue without resubmission.
        try:
            database = self.root / 'data/studiowright.db'
            if task['kind'] in {'image', 'video'} and not (self.test and not database.exists()):
                status = account_status([{'profile': task['profile']}], database)[0]
                detail = self.store.retry_detail(task['id'])
                switched = detail and detail['reason'] == 'switch_account'
                mapped = {(p['profile'], p['project']) for p in self.store.retry_projects()}
                if (status['generation_state'] != 'not_cooling_down' or status['risk_paused'] is not False
                        or (switched and (task['profile'] not in self.switch_ready_profiles()
                                          or (task['profile'], task['project']) not in mapped))):
                    self.store.release_unsubmitted(task['id'])
                    self.busy.discard(task['profile'])
                    return
        except Exception:
            # Do not turn a failure to release the claim into a provider failure.
            self.busy.discard(task['profile'])
            raise
        before = []
        try:
            before = [dict(r)["id"] for r in self.adapter.source_jobs(task["profile"])]
            result = await self.adapter.run(task, self.store.account(task["profile"]))
            if task["kind"] in {"sync", "session_import"}:
                self.store.update_account(task["profile"], result["email"], "verified")
                for project in result["projects"]:
                    self.store.project(task["profile"], project["id"], project["title"])
            self.store.finish(task["id"], result)
        except asyncio.CancelledError:
            # Leave running state for recovery. Never imply generation was cancelled.
            raise
        except Exception as exc:
            provenance = []
            retry_evidence = None
            with contextlib.suppress(Exception):
                exact = getattr(self.adapter, 'source_jobs_for_task', None)
                linked = list(exact(task['profile'], task['id'])) if exact and self.store.retry_detail(task['id']) else []
                rows = linked if linked else self.adapter.source_jobs(task["profile"])
                for r in rows:
                    r = dict(r)
                    if linked or r["id"] not in before:
                        raw = json.loads(r["result"]) if r.get("result") else {}
                        provenance.append(
                            {
                                "job_id": r["id"],
                                "project_id": r["project_id"],
                                "status": r["status"],
                                "stage": raw.get("stage"),
                                "media_ids": raw.get("media_ids", []),
                                "files": raw.get("files", []),
                            }
                        )
                # A source record is admissible only when exactly correlated with
                # this console task and still proves no accepted/generated media.
                if len(linked) == 1:
                    source = dict(linked[0])
                    raw = json.loads(source.get('result') or '{}')
                    evidence, stage = raw.get('retry_evidence'), raw.get('stage')
                    if (source['status'] == 'failed' and source['project_id'] == task['project']
                            and source['kind'] == task['kind'] and not raw.get('media_ids') and not raw.get('files')
                            and ((evidence == 'transient_before_submit' and stage == 'preparing')
                                 or (evidence == 'provider_rejected_unusual' and stage == 'submitting'
                                     and task['params'].get('count') == 1))):
                        retry_evidence = evidence
                elif not linked and isinstance(exc, PreSubmissionTransient) and self.store.retry_detail(task['id']):
                    retry_evidence = 'transient_before_submit'
            if task["kind"] in {"sync", "session_import"}:
                account = self.store.account(task["profile"])
                self.store.update_account(
                    task["profile"], account["email"], "attention", safe_error(exc)
                )
            self.store.finish(task["id"], {"source_jobs": provenance}, safe_error(exc) or type(exc).__name__, retry_evidence)
        finally:
            if task["kind"] == "session_import":
                with contextlib.suppress(Exception):
                    self.vault.remove(task["params"]["bundle_id"])
            self.busy.discard(task["profile"])

    async def scheduler(self):
        next_switch_check = 0.0
        while True:
            for ident, s in list(self.remote.sessions.items()):
                if s["expires"] < time.time():
                    await asyncio.to_thread(self.remote.close, ident)
                    self.busy.discard(s["profile"])
            self.running = {t for t in self.running if not t.done()}
            if time.monotonic() >= next_switch_check:
                # Missing projects, stale balance, or uncertain account state all wait.
                with contextlib.suppress(Exception):
                    self.schedule_pending_switches()
                next_switch_check = time.monotonic() + 1.0
            while len(self.running) < 2:
                database = self.root / 'data/studiowright.db'
                accounts = self.store.accounts()
                states = account_status(accounts, database)
                allowed = {a['profile'] for a in states if a['generation_state'] == 'not_cooling_down'
                           and a['risk_paused'] is False}
                if self.test and not database.exists():
                    allowed = {a['profile'] for a in accounts}  # FakeAdapter fixture only.
                task = self.store.claim(self.busy, allowed, self.switch_ready_profiles())
                if not task:
                    break
                self.busy.add(task["profile"])
                self.running.add(asyncio.create_task(self.execute(task)))
            await asyncio.sleep(0.2)

    def schedule_pending_switches(self):
        roots = self.store.pending_retry_roots()
        if not roots:
            return
        now = time.time()
        states = account_status(self.store.accounts(), self.root / 'data/studiowright.db', now)
        allowed = {a['profile'] for a in states if a['state'] == 'verified'
                   and a['generation_state'] == 'not_cooling_down' and a['risk_paused'] is False
                   and a['profile'] not in self.busy}
        fresh = self.switch_ready_profiles(now)
        projects = sorted((p for p in self.store.retry_projects() if p['profile'] in allowed & fresh),
                          key=lambda p: (p['updated'], p['profile']))
        for root in roots:
            for p in projects:
                if self.store.enqueue_switch(root['root_id'], p['profile'], p['project']):
                    break

    def switch_ready_profiles(self, now=None):
        now = time.time() if now is None else now
        try:
            return {r['profile'] for r in self.store.rows("SELECT b.profile FROM account_balances b JOIN accounts a ON a.profile=b.profile WHERE a.state='verified' AND b.status='ready' AND b.balance>0 AND b.observed BETWEEN ? AND ?", (now - 900, now))}
        except sqlite3.Error:
            return set()  # no trusted balance: block switch, keep the ordinary queue running

    async def archive_worker(self):
        while True:
            # Restart recovery reads persisted task results, never repeats generation.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.library.reconcile, self.store, self.root / "output")
                await asyncio.to_thread(self.library.drain)
            await asyncio.sleep(5)

    async def index_worker(self):
        while True:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.library.drain_index)
            await asyncio.sleep(5)

    async def balance_worker(self):
        if self.test:
            return
        from balance_reader import read_balance
        from playwright.async_api import async_playwright
        while True:
            self.balance_wake.clear()
            self.balance_refreshing = True
            try:
                async with async_playwright() as pw:
                    for account in self.store.accounts():
                        profile = account["profile"]
                        if profile in self.busy or self.remote.for_profile(profile):
                            continue
                        try:
                            balance = await asyncio.wait_for(read_balance(account, pw), timeout=12)
                            self.insights.record(profile, balance)
                        except Exception as exc:
                            message = str(exc) if isinstance(exc, ValueError) else '余额读取暂未完成，保留上次记录。'
                            self.insights.record(profile, error=message)
            except Exception:
                pass
            finally:
                self.balance_refreshing = False
            try:
                await asyncio.wait_for(self.balance_wake.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

    def validate(self, body):
        profile, project, kind = body.get("profile"), body.get("project"), body.get("kind")
        if (
            not isinstance(profile, str)
            or not PROFILE.fullmatch(profile)
            or not self.store.account(profile)
        ):
            raise ValueError("请选择已登记账号。")
        if kind not in KINDS:
            raise ValueError("不支持的操作。")
        if kind not in {"sync", "login"}:
            try:
                if str(uuid.UUID(project)) != project:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("请选择完整、有效的已有项目 ID。") from None
            if not self.store.has_project(profile, project):
                raise ValueError("该项目未登记在此账号下，请先读取项目或登记已有项目。")
            account = self.store.account(profile)
            if account["state"] != "verified":
                raise ValueError("请先检查此账号的在线登录和项目，再操作项目。")
        else:
            project = None
        params = {}
        if kind in {"image", "video", "quote"}:
            p = body.get("params", {})
            if not isinstance(p, dict):
                raise ValueError("任务参数格式不正确。")
            prompt = p.get("prompt", "").strip() if isinstance(p.get("prompt", ""), str) else ""
            if not prompt or len(prompt) > 10000:
                raise ValueError("请填写提示词，最多 10000 个字。")
            if type(p.get("count", 1)) is not int or p.get("count", 1) not in (1, 2, 3, 4):
                raise ValueError("生成数量须为 1～4。")
            params = {
                "prompt": prompt,
                "model": p.get("model"),
                "aspect": p.get("aspect", "16:9"),
                "count": p.get("count", 1),
                "resolution": p.get("resolution", "720p"),
                "duration": p.get("duration", 8),
            }
            if params["aspect"] not in (
                ["16:9", "9:16"] if kind != "image" else ["16:9", "4:3", "1:1", "3:4", "9:16"]
            ):
                raise ValueError("当前生成类型不支持这个画面比例。")
            if (
                type(params["duration"]) is not int
                or params["duration"] not in (4, 6, 8, 10)
                or params["resolution"] not in ("360p", "720p")
            ):
                raise ValueError("视频时长或清晰度不正确。")
            self.adapter.validate("video" if kind == "quote" else kind, params)
            if kind in {"image", "video"} and body.get("confirm_generation") is not True:
                raise ValueError("提交前请确认账号、项目和可能消耗的积分。")
            if kind == "video":
                cap = p.get("max_credits")
                if type(cap) is not int or not 0 <= cap <= 10000:
                    raise ValueError("请填写本次允许的视频积分上限（0～10000）。")
                params["max_credits"] = cap
        key = body.get("request_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
            raise ValueError("缺少提交编号，请重新提交。")
        return profile, project, kind, params, key

    def public_task(self, row):
        row = dict(row)
        result = row.get("result") or {}
        files = []
        for path in result.get("files", []):
            try:
                resolved = Path(path).resolve()
                if resolved.is_relative_to((self.root / "output").resolve()) and resolved.is_file():
                    files.append(
                        {"name": resolved.name, "url": f"/api/tasks/{row['id']}/files/{len(files)}"}
                    )
            except (OSError, TypeError):
                pass
        row["downloads"] = files
        result = dict(result)
        result["files"] = [Path(f).name for f in result.get("files", [])]
        row["result"] = result if row.get("result") is not None else None
        row.pop("request_key", None)
        row['retry'] = self.store.retry_detail(row['id'])
        if row["kind"] == "session_import":
            row["params"] = {"expected_email": row["params"].get("expected_email")}
        return row


def create_app(root, adapter, test=False):
    c = Console(root, adapter, test)
    cookie_name = "sw_console_fixture" if test else "sw_console"
    web_auth = WebAuth(c.store)

    @asynccontextmanager
    async def lifespan(app):
        c.store.recover()
        for row in c.store.rows(
            "SELECT params FROM tasks WHERE kind='session_import' AND status NOT IN ('queued','running')"
        ):
            with contextlib.suppress(Exception):
                c.vault.remove(json.loads(row["params"])["bundle_id"])
        c.worker = asyncio.create_task(c.scheduler())
        archive_worker = asyncio.create_task(c.archive_worker())
        index_worker = asyncio.create_task(c.index_worker())
        balance_worker = asyncio.create_task(c.balance_worker())
        yield
        c.worker.cancel()
        archive_worker.cancel()
        index_worker.cancel()
        balance_worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await balance_worker
        with contextlib.suppress(asyncio.CancelledError):
            await index_worker
        with contextlib.suppress(asyncio.CancelledError):
            await archive_worker
        with contextlib.suppress(asyncio.CancelledError):
            await c.worker
        for task in c.running:
            task.cancel()
        await asyncio.gather(*c.running, return_exceptions=True)
        await c.remote.shutdown()

    async def index(request):
        return FileResponse(HERE / "static/index.html")

    def local_browser(request):
        return not test and request.client is not None and request.client.host in ("127.0.0.1", "::1")

    async def remote_page(request):
        if not local_browser(request) and not c.store.authenticated(request.cookies.get(cookie_name)):
            return JSONResponse({"error": "请先连接管理后台。"}, status_code=401)
        return FileResponse(HERE / "static/remote.html")

    async def health(request):
        return JSONResponse({"ok": True, "application": "studiowright-console", "test": test})

    async def payload(request):
        limit = 524288 if request.url.path.endswith("/session") else 32768
        if int(request.headers.get("content-length", "0")) > limit:
            raise ValueError("提交内容过大。")
        data = await request.body()
        if len(data) > limit:
            raise ValueError("提交内容过大。")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("提交内容格式不正确。")
        return value

    async def connect(request):
        try:
            data = await payload(request)
            ticket = data.get("ticket", "")
            token = (
                c.store.exchange(ticket) if isinstance(ticket, str) and len(ticket) < 200 else None
            )
            if not token:
                return JSONResponse(
                    {"error": "连接码无效或已过期，请重新打开本机后台入口。"}, status_code=401
                )
            response = JSONResponse({"ok": True})
            response.set_cookie(
                cookie_name, token, httponly=True, samesite="strict", max_age=7 * 86400
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ValueError, TypeError):
            return JSONResponse({"error": "连接请求不正确。"}, status_code=400)

    async def auth_status(request):
        return JSONResponse({'configured': web_auth.configured()}, headers={'Cache-Control': 'no-store'})

    async def password_login(request):
        try:
            body = await payload(request)
            remember = body.get('remember', False)
            token, age = await asyncio.to_thread(web_auth.login, body.get('username'), body.get('password'), remember)
            response = JSONResponse({'ok': True})
            response.set_cookie(cookie_name, token, httponly=True, samesite='strict',
                                secure=request.url.scheme == 'https', max_age=age if remember else None)
            return response
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=401)

    async def password_settings(request):
        try:
            body = await payload(request)
            await asyncio.to_thread(web_auth.set_credentials, body.get('username'), body.get('password'), body.get('current_password'))
            token, age = web_auth.new_session(True)
            response = JSONResponse({'ok': True, 'message': '后台账号密码已保存，其他浏览器的旧登录已退出。'})
            response.set_cookie(cookie_name, token, httponly=True, samesite='strict',
                                secure=request.url.scheme == 'https', max_age=age)
            return response
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)

    async def logout(request):
        token = request.cookies.get(cookie_name, "")
        c.store.execute("DELETE FROM sessions WHERE hash=?", (c.store.digest(token),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name)
        return response

    async def state(request):
        tasks = [c.public_task(t) for t in c.store.tasks()]
        now = time.time()
        accounts = account_status(c.store.accounts(), c.root / "data/studiowright.db", now)
        insights = await asyncio.to_thread(c.insights.report, accounts, now, c.balance_refreshing)
        return JSONResponse(
            {
                "test": test,
                "accounts": accounts,
                "account_insights": insights,
                "projects": c.store.projects(),
                "retry_projects": c.store.retry_projects(),
                "tasks": tasks,
                "models": adapter.models,
                "source_version": "0.2.12",
                "source_commit": "df9f5fa",
                "console_api_version": "2026-10-31.account-insights.1",
                "remote_login_ready": c.remote.supported(),
                "remote_logins": [c.remote.public(s) for s in c.remote.sessions.values()],
                "time": now,
            }
        )

    async def accounts(request):
        try:
            data = await payload(request)
            profile, label = data.get("profile", ""), data.get("label", "")
            if not isinstance(profile, str) or not PROFILE.fullmatch(profile):
                raise ValueError("账号名称使用 1～64 个英文字母、数字、下划线或短横线。")
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
                raise ValueError("请填写账号备注，最多 80 个字。")
            if len(c.store.accounts()) >= 50 and not c.store.account(profile):
                raise ValueError("当前最多登记 50 个账号。")
            c.store.add_account(profile, label.strip())
            return JSONResponse({"ok": True})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def resume_flow_risk(request):
        try:
            data = await payload(request)
            profile = data.get('profile')
            if set(data) != {'profile'} or not isinstance(profile, str) or not PROFILE.fullmatch(profile) or not c.store.account(profile):
                raise ValueError('请选择已登记账号。')
            resume_after_review(c.root / 'data/studiowright.db', profile)
            status = account_status([{'profile': profile}], c.root / 'data/studiowright.db')[0]
            return JSONResponse({'ok': True, 'cooling': status['generation_state'] == 'cooling_down',
                'message': 'Flow 已恢复；如仍在冷却，任务会继续等待冷却结束。'})
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)

    async def refresh_balances(request):
        if not c.test and not c.balance_refreshing:
            c.balance_wake.set()
        return JSONResponse({"queued": not c.test, "refreshing": c.balance_refreshing,
            "message": "正在读取现有账号窗口的积分，不打开新窗口、不生成。" if not c.test else "模拟环境不会读取真实账号。"})

    async def projects(request):
        try:
            data = await payload(request)
            profile, ident = data.get("profile"), data.get("id")
            if (
                not isinstance(profile, str)
                or not PROFILE.fullmatch(profile)
                or not c.store.account(profile)
            ):
                raise ValueError("请选择账号。")
            if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident:
                raise ValueError("请填写完整项目 ID。")
            title = data.get("title", "已登记项目")
            if not isinstance(title, str) or len(title) > 140:
                raise ValueError("项目备注最长 140 个字。")
            c.store.project(profile, ident, title)
            return JSONResponse({"ok": True})
        except (ValueError, TypeError, AttributeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def retry_project(request):
        try:
            data = await payload(request)
            if set(data) != {'profile', 'project'}:
                raise ValueError('重试项目设置格式不正确。')
            profile, project = data['profile'], data['project']
            if not isinstance(profile, str) or not PROFILE.fullmatch(profile) or not c.store.account(profile):
                raise ValueError('请选择已登记账号。')
            if project is not None and (not isinstance(project, str) or str(uuid.UUID(project)) != project):
                raise ValueError('请选择该账号已登记的项目。')
            c.store.set_retry_project(profile, project)
            return JSONResponse({'ok': True})
        except (ValueError, TypeError, AttributeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)

    async def submit(request):
        try:
            body = await payload(request)
            if request.state.api_key and body.get("kind") not in {"image", "video", "quote", "media", "doctor", "sync"}:
                return JSONResponse({"error": "API Key 不允许打开登录窗口或修改登录信息。"}, status_code=403)
            args = c.validate(body)
            automatic = body.get('auto_retry', False)
            if type(automatic) is not bool or (automatic and args[2] not in ('image', 'video')):
                raise ValueError('任务重试设置不正确。')
            row = c.store.create_task(*args, auto_retry=automatic)
            return JSONResponse(c.public_task(row), status_code=202)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def cancel(request):
        ident = request.path_params["ident"]
        row = c.store.task(ident)
        if not c.store.cancel(ident):
            return JSONResponse(
                {"error": "仅能取消排队任务；已开始任务不能直接中断或重试。"}, status_code=409
            )
        if row and row["kind"] == "session_import":
            with contextlib.suppress(Exception):
                c.vault.remove(row["params"]["bundle_id"])
        return JSONResponse({"ok": True})

    async def download(request):
        row = c.store.task(request.path_params["ident"])
        if row:
            paths = []
            for f in (row.get("result") or {}).get("files", []):
                p = Path(f).resolve()
                if p.is_relative_to((c.root / "output").resolve()) and p.is_file():
                    paths.append(p)
            index = request.path_params["index"]
            if index < len(paths):
                return FileResponse(paths[index], filename=paths[index].name)
        return JSONResponse({"error": "此任务没有可下载文件。"}, status_code=404)

    async def materials(request):
        try:
            p = request.query_params
            result = await asyncio.to_thread(
                c.library.search,
                p.get("q", ""),
                p.get("kind", ""),
                p.get("tag", ""),
                p.get("account", ""),
                int(p.get("page", "1")),
                dimensions={d: p[d] for d in DIMENSIONS if d in p},
                model=p.get("model", ""), project_id=p.get("project_id", ""),
                account_id=p.get("account_id", ""), created_from=p.get("created_from", ""),
                created_to=p.get("created_to", ""), index_status=p.get("index_status", ""),
                storage_status=p.get("storage_status", ""),
                limit=int(p.get("limit", "24")), offset=int(p["offset"]) if "offset" in p else None,
            )
            return JSONResponse(result)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def material_detail(request):
        item = await asyncio.to_thread(c.library.get, request.path_params["ident"])
        return JSONResponse(item or {"error": "素材不存在。"}, status_code=200 if item else 404)

    async def material_file(request):
        ident = request.path_params["ident"]
        item = c.library.get(ident)
        path = c.library.file(ident)
        if not item or not path:
            return JSONResponse({"error": "素材文件不存在。"}, status_code=404)
        return FileResponse(
            path,
            media_type=item["mime"],
            filename=path.name,
            content_disposition_type="attachment"
            if request.query_params.get("download") == "1"
            else "inline",
        )

    async def material_thumbnail(request):
        ident = request.path_params["ident"]
        item = c.library.get(ident)
        if not item or item["media_type"] != "image":
            return JSONResponse({"error": "没有图片预览。"}, status_code=404)
        try:
            path = await asyncio.to_thread(c.library.thumbnail, ident)
            return FileResponse(path, media_type="image/jpeg")
        except (ValueError, OSError):
            return JSONResponse({"error": "预览生成失败，原文件保留。"}, status_code=422)

    async def material_tags(request):
        try:
            body = await payload(request)
            ident = request.path_params["ident"]
            if "structured_tags" in body:
                from material_tags import clean_dimensions
                clean_dimensions(body["structured_tags"])
            # Validate both parts before either is saved.
            if "tags" in body and (not isinstance(body["tags"], list) or len(body["tags"]) > 30 or any(not isinstance(t, str) or not 0 < len(t.strip()) <= 80 for t in body["tags"])):
                raise ValueError("最多填写 30 个自定义标签，每个最多 80 字。")
            if "tags" in body:
                c.library.set_tags(ident, body["tags"])
            if "structured_tags" in body:
                c.library.set_dimensions(ident, body["structured_tags"])
            return JSONResponse({"ok": True, "item": c.library.get(ident)})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def material_facets(request):
        return JSONResponse(await asyncio.to_thread(c.library.facets))

    def legacy_item(item):
        if not item:
            return None
        origin = item["origins"][0] if item["origins"] else {}
        return {**item, **{k: origin.get(k, "") for k in ("account_id", "project_id", "model", "prompt", "created")},
                "custom_tags": item["tags"], "tags": item["structured_tags"],
                "file_url": item["url"], "mime_type": item["mime"], "storage_status": "ready",
                "created_at": origin.get("created", ""), "metadata": {"origins": item["origins"]}}

    async def legacy_materials(request):
        p = request.query_params
        try:
            result = await asyncio.to_thread(c.library.search, q=p.get("q", ""), kind=p.get("media_type", ""),
                dimensions={d: p[d] for d in DIMENSIONS if d in p}, model=p.get("model", ""),
                account_id=p.get("account_id", ""), project_id=p.get("project_id", ""),
                created_from=p.get("created_from", ""), created_to=p.get("created_to", ""),
                storage_status=p.get("storage_status", ""), index_status=p.get("index_status", ""),
                limit=int(p.get("limit", "30")), offset=int(p.get("offset", "0")))
            return JSONResponse({"items": [legacy_item(i) for i in result["items"]], "total": result["total"], "limit": result["limit"], "offset": result["offset"]})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def legacy_detail(request):
        item = legacy_item(c.library.get(request.path_params["ident"]))
        return JSONResponse(item or {"error": "素材不存在。"}, status_code=200 if item else 404)

    async def index_config(request):
        try:
            result = c.library.index_config() if request.method == "GET" else c.library.configure_index(await payload(request))
            return JSONResponse(result)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def index_retry(request):
        try:
            c.library.retry_index(request.path_params["ident"])
            return JSONResponse({"queued": True, "message": "标签已排队；只有启用标签分析才会调用模型，不会生成媒体。"})
        except ValueError as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def api_keys(request):
        if request.method == "GET":
            return JSONResponse({"items": c.keys.list()})
        try:
            body = await payload(request)
            return JSONResponse(c.keys.create(body.get("name"), body.get("scope", "read")), status_code=201)
        except ValueError as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def api_key_revoke(request):
        found = c.keys.revoke(request.path_params["ident"])
        return JSONResponse({"ok": bool(found)}, status_code=200 if found else 404)

    async def bridge_pair(request):
        token = secrets.token_urlsafe(40)
        c.store.execute(
            "INSERT INTO bridge_devices VALUES(?,?)", (c.store.digest(token), time.time())
        )
        return JSONResponse(
            {
                "apiUrl": "http://127.0.0.1:18888/api/plugin/session",
                "connectionToken": token,
                "extensionId": "kbnibljeinmjmmpholicjkaiehmilbbk",
            }
        )

    async def import_session(request):
        try:
            body = await payload(request)
            bundle = normalize_bundle(body)
            matches = [
                a
                for a in c.store.accounts()
                if a["email"] and a["email"].lower() == bundle["email"]
            ]
            profile = body.get("profile")
            if profile:
                account = c.store.account(profile)
                if not account or account["email"] and account["email"].lower() != bundle["email"]:
                    raise ValueError("登录包邮箱与所选账号不一致，未导入。")
            elif len(matches) == 1:
                profile = matches[0]["profile"]
            elif matches:
                raise ValueError("此邮箱对应多个配置，请在后台明确选择目标账号。")
            else:
                profile = "account-" + c.store.digest(bundle["email"])[:12]
                if c.store.account(profile):
                    raise ValueError("账号名称冲突，请先在后台登记目标账号。")
                c.store.add_account(profile, bundle["email"])
                c.store.update_account(profile, bundle["email"], "unknown")
            if c.remote.for_profile(profile):
                raise ValueError("此账号正在远程登录，请先完成远程登录再同步。")
            if c.store.rows(
                "SELECT id FROM tasks WHERE profile=? AND kind='session_import' AND status IN ('queued','running')",
                (profile,),
            ):
                raise ValueError("此账号已有登录同步正在处理，请等待结果。")
            ident = c.vault.put(bundle)
            try:
                task = c.store.create_task(
                    profile,
                    None,
                    "session_import",
                    {"bundle_id": ident, "expected_email": bundle["email"]},
                    secrets.token_hex(16),
                )
            except Exception:
                c.vault.remove(ident)
                raise
            return JSONResponse(
                {
                    "id": task["id"],
                    "profile": profile,
                    "email": bundle["email"],
                    "status": "queued",
                    "message": "登录信息已安全接收，等待在线邮箱核对；接收不代表登录成功。",
                },
                status_code=202,
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def import_status(request):
        row = c.store.task(request.path_params["ident"])
        if not row or row["kind"] != "session_import":
            return JSONResponse({"error": "登录同步记录不存在。"}, status_code=404)
        return JSONResponse(
            {
                "id": row["id"],
                "status": row["status"],
                "email": (row.get("result") or {}).get("email"),
                "error": row.get("error"),
                "message": (row.get("result") or {}).get("message"),
            }
        )

    async def remote_start(request):
        profile = request.path_params["profile"]
        async with c.remote_gate:
            try:
                if not c.store.account(profile):
                    raise ValueError("此账号尚未登记。")
                old = c.remote.for_profile(profile)
                if old:
                    return JSONResponse(c.remote.public(old))
                if profile in c.busy:
                    raise ValueError("此账号正在执行任务，请结束后再远程登录。")
                c.busy.add(profile)
                try:
                    result = await asyncio.to_thread(c.remote.start, profile)
                except Exception:
                    c.busy.discard(profile)
                    raise
                return JSONResponse(result)
            except Exception as exc:
                return JSONResponse({"error": safe_error(exc)}, status_code=409)

    async def remote_finish(request):
        ident = request.path_params["ident"]
        async with c.remote_gate:
            s = await asyncio.to_thread(c.remote.close, ident)
            if not s:
                return JSONResponse({"error": "远程登录窗口已关闭。"}, status_code=404)
            c.busy.discard(s["profile"])
            task = c.store.create_task(s["profile"], None, "sync", {}, secrets.token_hex(16))
            return JSONResponse(
                {"id": task["id"], "message": "远程窗口已保存并关闭，正在核对邮箱与登录状态。"}
            )

    async def remote_status(request):
        s = c.remote.sessions.get(request.path_params["ident"])
        if not s:
            return JSONResponse(
                {"error": "远程窗口已结束，请返回账号管理重新打开。"}, status_code=404
            )
        s["expires"] = time.time() + 1200
        return JSONResponse(c.remote.public(s))

    async def remote_ws(ws):
        host, origin = ws.headers.get("host"), ws.headers.get("origin")
        s = c.remote.sessions.get(ws.path_params["ident"])
        if (
            host
            not in {
                "127.0.0.1:18888",
                "localhost:18888",
                "127.0.0.1:18889",
                "localhost:18889",
                "testserver",
            }
            or origin != "http://" + host
            or not c.store.authenticated(ws.cookies.get(cookie_name))
            or not s
            or s["expires"] < time.time()
        ):
            await ws.close(code=4403)
            return
        if s["connected"]:
            await ws.close(code=4409)
            return
        s["connected"] = True
        writer = None
        await ws.accept()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", s["port"])

            async def incoming():
                while True:
                    data = await ws.receive_bytes()
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("帧过大")
                    writer.write(data)
                    await writer.drain()

            async def outgoing():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)

            async def alive():
                while True:
                    if (
                        not c.store.authenticated(ws.cookies.get(cookie_name))
                        or s["id"] not in c.remote.sessions
                        or s["expires"] < time.time()
                    ):
                        break
                    await asyncio.sleep(2)

            jobs = [asyncio.create_task(fn()) for fn in (incoming, outgoing, alive)]
            try:
                await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
        except Exception:
            pass
        finally:
            s["connected"] = False
            if writer:
                writer.close()
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                await ws.close()

    routes = [
        Route("/", index),
        Route("/remote", remote_page),
        Route("/health", health),
        Route("/api/auth/connect", connect, methods=["POST"]),
        Route("/api/auth/status", auth_status, methods=["GET"]),
        Route("/api/auth/login", password_login, methods=["POST"]),
        Route("/api/auth/settings", password_settings, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/accounts/credits/refresh", refresh_balances, methods=["POST"]),
        Route("/api/accounts/risk-resume", resume_flow_risk, methods=["POST"]),
        Route("/api/keys", api_keys, methods=["GET", "POST"]),
        Route("/api/keys/{ident}/revoke", api_key_revoke, methods=["POST"]),
        Route("/api/bridge/pair", bridge_pair, methods=["POST"]),
        Route("/api/plugin/session", import_session, methods=["POST"]),
        Route("/api/plugin/session/{ident}", import_status),
        Route("/api/accounts/session", import_session, methods=["POST"]),
        Route("/api/accounts/{profile}/remote", remote_start, methods=["POST"]),
        Route("/api/remote/{ident}", remote_status),
        Route("/api/remote/{ident}/finish", remote_finish, methods=["POST"]),
        WebSocketRoute("/api/remote/{ident}/ws", remote_ws),
        Route("/api/library", materials),
        Route("/api/library/facets", material_facets),
        Route("/api/library/config", index_config, methods=["GET", "POST"]),
        Route("/api/material-library/facets", material_facets),
        Route("/api/material-library/assets", legacy_materials),
        Route("/api/material-library/assets/{ident}/file", material_file),
        Route("/api/material-library/assets/{ident}", legacy_detail),
        Route("/api/library/{ident}/retry-index", index_retry, methods=["POST"]),
        Route("/api/library/{ident}/file", material_file),
        Route("/api/library/{ident}/thumbnail", material_thumbnail),
        Route("/api/library/{ident}/tags", material_tags, methods=["POST"]),
        Route("/api/library/{ident}", material_detail),
        Route("/api/accounts", accounts, methods=["POST"]),
        Route("/api/projects", projects, methods=["POST"]),
        Route("/api/retry-project", retry_project, methods=["POST"]),
        Route("/api/tasks", submit, methods=["POST"]),
        Route("/api/tasks/{ident}/cancel", cancel, methods=["POST"]),
        Route("/api/tasks/{ident}/files/{index:int}", download),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    vendor = c.root / "remote-tools/novnc"
    if vendor.is_dir():
        routes.append(Mount("/remote-vendor", StaticFiles(directory=vendor), name="remote-vendor"))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.console = c

    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in {
            "127.0.0.1:18888",
            "localhost:18888",
            "127.0.0.1:18889",
            "localhost:18889",
            "testserver",
        }:
            return JSONResponse({"error": "不允许的访问地址。"}, status_code=403)
        plugin_path = request.url.path == "/api/plugin/session" or request.url.path.startswith(
            "/api/plugin/session/"
        )
        bearer = request.headers.get("authorization", "")
        plugin_auth = (
            plugin_path
            and bearer.startswith("Bearer ")
            and bool(
                c.store.rows(
                    "SELECT hash FROM bridge_devices WHERE hash=?", (c.store.digest(bearer[7:]),)
                )
            )
        )
        api_key = None
        if bearer and not plugin_auth:
            api_key = c.keys.verify(bearer[7:]) if bearer.startswith("Bearer ") else None
            if not api_key:
                return JSONResponse({"error": "API Key 无效或已撤销。"}, status_code=401, headers={"Cache-Control": "no-store"})
            if not c.keys.permits(api_key, request.method, request.url.path):
                return JSONResponse({"error": "此 API Key 没有该操作权限，请使用后台登录管理。"}, status_code=403)
        request.state.api_key = api_key
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if (
                origin
                and origin != "http://" + host
                and not (
                    plugin_auth and origin == "chrome-extension://kbnibljeinmjmmpholicjkaiehmilbbk"
                )
            ):
                return JSONResponse({"error": "请从后台页面发起操作。"}, status_code=403)
            if request.headers.get("x-console-request") != "1" or not request.headers.get(
                "content-type", ""
            ).startswith("application/json"):
                return JSONResponse({"error": "请求格式不正确。"}, status_code=403)
        if (
            request.url.path.startswith("/api/")
            and request.url.path not in ("/api/auth/connect", "/api/auth/status", "/api/auth/login")
            and not plugin_auth
            and not api_key
            and not local_browser(request)
            and not c.store.authenticated(request.cookies.get(cookie_name))
        ):
            return JSONResponse(
                {"error": "后台连接已到期，请重新打开本机后台入口。"}, status_code=401
            )
        # The user explicitly opted out of the local browser login gate.
        # Retain an automatic cookie for remote-login WebSocket connections.
        auto_token = None
        if local_browser(request) and not bearer and not c.store.authenticated(request.cookies.get(cookie_name)):
            auto_token, _ = web_auth.new_session(False)
        response = await call_next(request)
        if auto_token:
            response.set_cookie(cookie_name, auto_token, httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' http://127.0.0.1:18890; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=guard)
    return app


def configured():
    root = Path(
        os.environ.get("SW_ROOT", str(Path.home() / ".local/share/studiowright-evaluation"))
    )
    if os.environ.get("SW_CONSOLE_TEST") == "1":
        if not root.name.startswith("console-fixture-"):
            raise RuntimeError("模拟执行器只能在独立 console-fixture-* 目录中运行。")
        from adapter import FakeAdapter

        return create_app(root, FakeAdapter(root), test=True)
    from adapter import RealAdapter

    return create_app(root, RealAdapter(root))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ticket":
        root = Path(
            os.environ.get("SW_ROOT", str(Path.home() / ".local/share/studiowright-evaluation"))
        )
        print(Store(root / "console-data").issue_ticket())
    else:
        import uvicorn

        uvicorn.run(
            configured(),
            host="127.0.0.1",
            port=18889 if os.environ.get("SW_CONSOLE_TEST") == "1" else 18888,
            access_log=False,
        )
