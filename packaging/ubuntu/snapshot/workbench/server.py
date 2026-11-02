"""Workbench using the existing Flow accounts and source-specific execution."""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
MAX_RESPONSE = 16 * 1024 * 1024
APPLY_LIBRARY_TAGS = r'''
import json,os,pathlib,re,sqlite3,subprocess,sys
payload=json.load(sys.stdin);ident=payload.get('asset_id');values=payload.get('tags')
if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{64}',ident) or not isinstance(values,dict): raise ValueError('invalid')
pid=subprocess.check_output(['systemctl','--user','show','studiowright-console.service','--property=MainPID','--value'],text=True,timeout=5).strip()
if not pid.isdigit() or pid=='0': raise ValueError('service_unavailable')
env=dict(p.split(b'=',1) for p in pathlib.Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in p)
home=pathlib.Path.home().resolve()
root=pathlib.Path(os.fsdecode(env.get(b'SW_ROOT',os.fsencode(home/'.local/share/studiowright-evaluation')))).resolve()
program=pathlib.Path(os.readlink('/proc/'+pid+'/cwd')).resolve();sys.path.insert(0,str(program))
from material_library import Library
from material_tags import DIMENSIONS,clean_dimensions
tags=clean_dimensions({k:v for k,v in values.items() if k in DIMENSIONS and v})
libraryroot=(root/'material-library').resolve();database=libraryroot/'library.db'
if database.is_symlink() or not database.is_file(): raise ValueError('library_unavailable')
library=Library.__new__(Library);library.root=libraryroot;library.db=database
with library.connect() as c:
 c.execute('BEGIN IMMEDIATE')
 if not c.execute('SELECT 1 FROM assets WHERE id=?',(ident,)).fetchone(): raise ValueError('asset_missing')
 existing={r[0] for r in c.execute('SELECT DISTINCT dimension FROM dimension_tags WHERE asset_id=?',(ident,))}
 manual={r[0] for r in c.execute('SELECT dimension FROM manual_dimensions WHERE asset_id=?',(ident,))}
 chosen={k:v for k,v in tags.items() if k not in existing and k not in manual}
 library.write_dimensions(c,ident,chosen,'visual_analysis')
 c.commit()
print(json.dumps({'added_dimensions':len(chosen)}))
'''
ALLOWED_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def clean_text(value, limit=300):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]


def perform_material_analysis(sources, real_pool, ident, request_id, account_data):
    """Run one already registered analysis; its request is never resent here."""
    try:
        with sources.library('GET', '/api/library/' + ident + '/file') as upstream:
            extensions = {'image/jpeg':'jpg', 'image/png':'png', 'image/webp':'webp', 'video/mp4':'mp4', 'video/webm':'webm'}
            ext = extensions.get(upstream.headers.get_content_type())
            length = upstream.headers.get('Content-Length', '')
            if upstream.status != 200 or not ext or not length.isdigit() or not 0 < int(length) <= 100 * 1024 * 1024:
                raise ValueError('素材格式或大小不支持分析。')
            attachment = real_pool.upload(request_id, 0, ext, upstream, int(length))
        prompt = ('请直接观察所附图片或视频，为素材填写以下标签。仅输出 JSON 对象，'
                  '键为 subject,scene,action,style,camera,color,keywords,shot_size,viewpoint,composition,movement，'
                  '每项都是简洁中文字符串数组；不确定或不适用时返回空数组，不要编造。'
                  '视频的运镜只描述实际观察到的运动。不要猜测时长或分辨率。')
        real_pool.generate('auto', prompt, request_id, account_data['items'], [attachment])
    except Exception:
        pass  # The durable request record decides whether anything was submitted.
    result = real_pool.finish_material_analysis(ident, request_id)
    if (result.get('analysis') or {}).get('status') == 'saving' and result.get('suggested'):
        try:
            sources.apply_gemini_tags(ident, result['suggested'])
            real_pool.material_analysis_note(request_id, 'Gemini 分析完成，原素材库空白标签已登记，已有标签保留。')
        except Exception:
            real_pool.material_analysis_note(request_id, 'Gemini 分析完成；原素材库标签保存状态未确认，请核对后再保存。', status='unknown')
        result = real_pool.material_extra(ident)
    return result


def discover_new_materials(sources, real_pool, since):
    """Scan recent library pages, retaining only items added after activation."""
    for page in range(1, 101):
        with sources.library('GET', '/api/library?page=' + str(page) + '&limit=100') as upstream:
            if upstream.status != 200:
                raise ValueError('素材库暂时不可读取。')
            data = upstream.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError('素材列表过大。')
        data = json.loads(data.decode('utf-8'))
        items, pages = data.get('items'), data.get('pages')
        if not isinstance(items, list) or type(pages) is not int or pages < 1:
            raise ValueError('素材列表不完整。')
        real_pool.discover_auto_materials(items)
        older = any(type(x.get('added')) in (int, float) and x['added'] < since for x in items if isinstance(x, dict))
        if not items or older or page >= pages:
            break


def run_auto_material_once(sources, real_pool):
    state = real_pool.auto_material_state()
    if not state['enabled'] or state['since'] is None:
        return
    discover_new_materials(sources, real_pool, state['since'])
    if state['paused_reason']:
        return
    accounts = sources.snapshot().get('real_accounts', {})
    if not accounts.get('available') or not isinstance(accounts.get('items'), list):
        return
    job = real_pool.claim_auto_material(accounts['items'])
    if not job:
        return
    try:
        perform_material_analysis(sources, real_pool, job['asset_id'], job['request_id'], accounts)
    finally:
        real_pool.finish_auto_material(job['request_id'])


def read_all_materials(sources):
    """Read a complete library snapshot; fail rather than queue a partial list."""
    result = []
    expected_total = expected_pages = None
    seen = set()
    for page in range(1, 101):
        with sources.library('GET', '/api/library?page=' + str(page) + '&limit=100') as upstream:
            if upstream.status != 200:
                raise ValueError('素材库暂时不可读取。')
            raw = upstream.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('素材列表过大。')
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict):
            raise ValueError('素材列表不完整。')
        items, pages, total = data.get('items'), data.get('pages'), data.get('total')
        if (not isinstance(items, list) or type(pages) is not int or not 1 <= pages <= 100 or page > pages
                or type(total) is not int or total < 0):
            raise ValueError('素材列表不完整。')
        if expected_total is None:
            expected_total, expected_pages = total, pages
        if total != expected_total or pages != expected_pages:
            raise ValueError('素材列表已变化，请刷新后重试。')
        if page < pages and not items:
            raise ValueError('素材列表不完整。')
        for item in items:
            ident = item.get('id') if isinstance(item, dict) else None
            if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{64}', ident) or ident in seen:
                raise ValueError('素材列表不完整或已变化。')
            seen.add(ident)
        result.extend(items)
        if page == pages:
            if len(result) != expected_total:
                raise ValueError('素材列表不完整或已变化。')
            return result
    raise ValueError('素材列表超过处理上限。')


def run_backfill_once(sources, real_pool, backfill):
    accounts = sources.snapshot().get('real_accounts', {})
    if not accounts.get('available') or not isinstance(accounts.get('items'), list):
        return
    job = backfill.claim(accounts['items'])
    if not job:
        return
    try:
        perform_material_analysis(sources, real_pool, job['asset_id'], job['request_id'], accounts)
    finally:
        backfill.finish(job['request_id'])


def identity_text(value):
    # Never trim or truncate source identities: that could merge distinct projects.
    if not isinstance(value, str) or not value or len(value) > 256 or re.search(r"[\x00-\x1f\x7f]", value):
        return ""
    return value


def numeric_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def project_key(profile, project):
    """Keep provider and complete source identity distinct, even with repeated names."""
    encoded = json.dumps(["flow", profile, project], ensure_ascii=False, separators=(",", ":"))
    return "flow-project-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def project_rows(state):
    rows = []
    for row in state.get("projects", []):
        if not isinstance(row, dict):
            continue
        profile = identity_text(row.get("profile"))
        ident = identity_text(row.get("id"))
        if not profile or not ident:
            continue
        rows.append({
            "key": project_key(profile, ident),
            "source": "Flow",
            "source_id": ident,
            "source_profile": profile,
            "title": clean_text(row.get("title"), 180) or "未命名项目",
            "checked": numeric_time(row.get("checked")),
        })
    return rows


def open_flow_project_names(state, projects):
    """Overlay only current titles from the exact already-open account/project tab."""
    if state.get('test') is True or not isinstance(state.get('accounts'), list):
        return
    helper = ROOT / 'flow_open_project_names.py'
    payload = {'accounts': state['accounts'], 'projects': state['projects']}
    names = []
    try:
        if not helper.is_file() or helper.is_symlink():
            raise ValueError('name_reader_unavailable')
        pid = subprocess.check_output(['systemctl', '--user', 'show', 'studiowright-console.service',
                                       '--property=MainPID', '--value'], text=True, timeout=3).strip()
        if not pid.isdigit() or pid == '0':
            raise ValueError('flow_unavailable')
        python = Path(os.fsdecode(Path('/proc', pid, 'cmdline').read_bytes().split(b'\0', 1)[0]))
        if not python.is_absolute() or not python.is_file():
            raise ValueError('flow_python_unavailable')
        result = subprocess.run([str(python), '-B', str(helper)],
                                input=json.dumps(payload, ensure_ascii=True).encode(),
                                capture_output=True, timeout=15, check=True)
        names = json.loads(result.stdout)
        if not isinstance(names, list):
            names = []
    except (OSError, ValueError, subprocess.SubprocessError):
        names = []
    try:
        from project_name_cache import merge
        merge(projects, names)
    except (OSError, ValueError, ImportError, sqlite3.Error):
        pass


def model_rows(state):
    source = state.get('models', {})
    result = {'image': [], 'video': []}
    if not isinstance(source, dict):
        return result
    for kind in result:
        entries = source.get(kind, [])
        if not isinstance(entries, list):
            continue
        seen = set()
        for row in entries:
            if not isinstance(row, dict):
                continue
            ident = identity_text(row.get('id'))
            if not ident or ident in seen:
                continue
            seen.add(ident)
            result[kind].append({'id': ident, 'label': clean_text(row.get('label'), 120) or ident})
    return result


def task_rows(state, projects):
    titles = {row["key"]: row["title"] for row in projects}
    rows = []
    for row in state.get("tasks", []):
        if not isinstance(row, dict):
            continue
        ident = identity_text(row.get("id"))
        if not ident:
            continue
        profile = identity_text(row.get("profile"))
        project = identity_text(row.get("project"))
        key = project_key(profile, project) if profile and project else None
        downloads = []
        raw_downloads = row.get('downloads', [])
        if isinstance(raw_downloads, list):
            for item in raw_downloads:
                if not isinstance(item, dict):
                    continue
                path = item.get('url')
                if isinstance(path, str) and re.fullmatch(r'/api/tasks/' + re.escape(ident) + r'/files/[0-9]+', path):
                    downloads.append({'path': path, 'name': clean_text(item.get('name'), 180) or '下载文件'})
        note = ''
        result = row.get('result')
        source_jobs = result.get('source_jobs', []) if isinstance(result, dict) else []
        if (row.get('status') == 'failed' and isinstance(source_jobs, list)
                and any(isinstance(job, dict) and job.get('stage') == 'generated'
                        and isinstance(job.get('media_ids'), list) and job['media_ids']
                        and not job.get('files') for job in source_jobs)):
            note = '已返回素材编号，文件未下载；请到原后台核对。'
        recoverable = (row.get('status') == 'failed' and row.get('kind') == 'image'
                       and isinstance(source_jobs, list) and sum(1 for job in source_jobs if isinstance(job, dict)
                           and job.get('stage') == 'generated' and isinstance(job.get('media_ids'), list)
                           and len(job['media_ids']) == 1 and not job.get('files')) == 1
                       and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident)))
        raw_retry = row.get('retry')
        retry = None
        if isinstance(raw_retry, dict) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(raw_retry.get('root_id', ''))):
            ordinal = raw_retry.get('ordinal')
            if type(ordinal) is int and 0 <= ordinal <= 2 and raw_retry.get('reason') in ('original', 'same_account', 'switch_account'):
                retry = {'root_id': raw_retry['root_id'], 'ordinal': ordinal,
                         'reason': raw_retry['reason'], 'state': raw_retry.get('state')}
                if not note and retry['state'] == 'pending_switch':
                    note = '等待可用账号及已指定的备用项目。'
                elif not note and retry['state'] == 'succeeded' and row.get('status') == 'failed':
                    note = '关联的后续重试已成功。'
                elif not note and retry['state'] == 'active' and row.get('status') == 'failed':
                    note = '已安排后续重试，请查看关联任务。'
        rows.append({
            "key": "flow-task-" + hashlib.sha256(ident.encode("utf-8")).hexdigest(),
            "source": "Flow",
            "source_id": ident,
            "source_profile": profile,
            "project_key": key,
            "project_title": titles.get(key, "未登记项目" if key else "未关联项目"),
            "kind": clean_text(row.get("kind"), 80),
            "status": clean_text(row.get("status"), 80),
            "created": numeric_time(row.get("created")),
            "finished": numeric_time(row.get("finished")),
            "downloads": downloads,
            "note": note,
            "recoverable": recoverable,
            "retry": retry,
        })
    return rows


def account_rows(state):
    """Project existing Flow account status; never copy credentials or guess Gemini bindings."""
    accounts = state.get("accounts")
    if not isinstance(accounts, list) or state.get("test") is True:
        return None
    insights = state.get("account_insights")
    insight_rows = insights.get("accounts", []) if isinstance(insights, dict) else []
    if not isinstance(insight_rows, list):
        insight_rows = []
    by_profile = {}
    duplicates = set()
    for row in insight_rows:
        if not isinstance(row, dict):
            continue
        profile = identity_text(row.get("profile"))
        if not profile:
            continue
        if profile in by_profile:
            duplicates.add(profile)
        else:
            by_profile[profile] = row
    profiles = set()
    rows = []
    for row in accounts:
        if not isinstance(row, dict):
            continue
        profile = identity_text(row.get("profile"))
        if not profile or profile in profiles:
            continue
        profiles.add(profile)
        info = by_profile.get(profile, {}) if profile not in duplicates else {}
        email = identity_text(row.get("email"))
        if not re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", email):
            email = ""
        raw_health = info.get("health")
        health = raw_health if isinstance(raw_health, str) and raw_health in {"ready", "cooling", "attention", "empty", "paused"} else "unknown"
        balance = info.get("balance")
        if isinstance(balance, bool) or not isinstance(balance, (int, float)) or not math.isfinite(balance) or balance < 0:
            balance = None
        rows.append({"key": "flow-account-" + hashlib.sha256(profile.encode("utf-8")).hexdigest(),
                     "profile": profile, "label": clean_text(row.get("label"), 120), "email": email,
                     "flow_login": "verified" if row.get("state") == "verified" else "unknown",
                     "flow_health": health, "flow_balance": balance,
                     "flow_balance_fresh": info.get("balance_fresh") is True,
                     "flow_balance_observed": numeric_time(info.get("balance_observed")),
                     "flow_cooldown_seconds": max(0, numeric_time(row.get("cooldown_remaining")) or 0),
                     "flow_cooling": row.get('generation_state') == 'cooling_down',
                     "flow_pacing_known": row.get('generation_state') in ('cooling_down', 'not_cooling_down'),
                     "flow_risk_known": type(row.get('risk_paused')) is bool,
                     "flow_risk_paused": row.get('risk_paused') is True,
                     "flow_risk_consecutive": row.get('risk_consecutive') if type(row.get('risk_consecutive')) is int and row.get('risk_consecutive') >= 0 else None,
                     "gemini": "unlinked"})
    return rows


def flow_insights_summary(state):
    """Keep the original console's coverage and never turn missing balances into zero."""
    source = state.get('account_insights') if isinstance(state, dict) else None
    if not isinstance(source, dict):
        return None
    def number(value):
        return value if type(value) in (int,float) and math.isfinite(value) and value >= 0 else None
    def usage(name):
        item=source.get(name)
        if not isinstance(item,dict):
            return None
        return {'recorded_usage':number(item.get('recorded_usage')),
                'completed':number(item.get('completed')),'unpriced':number(item.get('unpriced')),
                'unresolved':number(item.get('unresolved')),
                'coverage':item.get('coverage') if item.get('coverage') in ('not_recorded','partial','recorded') else 'unknown'}
    health=source.get('health_counts') if isinstance(source.get('health_counts'),dict) else {}
    return {'healthy_accounts':number(source.get('healthy_accounts')),
            'total_accounts':number(source.get('total_accounts')),
            'health_counts':{k:number(health.get(k)) for k in ('ready','cooling','attention','empty','paused')},
            'pool_balance':number(source.get('pool_balance')),'available_today':number(source.get('available_today')),
            'balance_known_accounts':number(source.get('balance_known_accounts')),
            'balance_fresh_accounts':number(source.get('balance_fresh_accounts')),
            'balance_oldest_observed':number(source.get('balance_oldest_observed')),
            'today':usage('today'),'yesterday':usage('yesterday'),
            'today_date':clean_text(source.get('today_date'),20),'updated':number(source.get('updated'))}


def counts_only(payload):
    counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
    media = counts.get("media_type", {}) if isinstance(counts, dict) else {}

    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return {
        "total": count(counts.get("total")),
        "images": count(media.get("image", 0)) if isinstance(media, dict) else None,
        "videos": count(media.get("video", 0)) if isinstance(media, dict) else None,
    }


def mp4_metadata(path):
    """Read MP4 headers only; never send media to an external service."""
    def boxes(stream, start, end):
        offset, count = start, 0
        while offset + 8 <= end and count < 10000:
            stream.seek(offset)
            header = stream.read(8)
            size, kind = struct.unpack('>I4s', header)
            header_size = 8
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8: break
                size, header_size = struct.unpack('>Q', extended)[0], 16
            elif size == 0:
                size = end - offset
            if size < header_size or offset + size > end:
                break
            yield kind, offset + header_size, offset + size
            offset += size
            count += 1

    result = {'duration': None, 'width': None, 'height': None}
    with Path(path).open('rb') as stream:
        end = stream.seek(0, 2)
        moov = next(((a,b) for k,a,b in boxes(stream, 0, end) if k == b'moov'), None)
        if not moov:
            return result
        for kind, start, stop in boxes(stream, *moov):
            if kind == b'mvhd':
                stream.seek(start)
                body = stream.read(min(40, stop-start))
                if len(body) >= 20:
                    if body[0] == 1 and len(body) >= 32:
                        scale, duration = struct.unpack('>IQ', body[20:32])
                    elif body[0] == 0:
                        scale, duration = struct.unpack('>II', body[12:20])
                    else:
                        continue
                    if scale and duration not in (0xffffffff, 0xffffffffffffffff):
                        result['duration'] = duration / scale
            elif kind == b'trak':
                track_header, video = None, False
                for subkind, substart, subend in boxes(stream, start, stop):
                    if subkind == b'tkhd' and subend - substart >= 8:
                        track_header = (subend - 8, subend)
                    elif subkind == b'mdia':
                        for nested, nstart, nend in boxes(stream, substart, subend):
                            if nested == b'hdlr' and nend - nstart >= 12:
                                stream.seek(nstart + 8)
                                video = stream.read(4) == b'vide'
                if video and track_header:
                    stream.seek(track_header[0])
                    width, height = struct.unpack('>II', stream.read(8))
                    width, height = width >> 16, height >> 16
                    if 0 < width <= 65535 and 0 < height <= 65535:
                        result['width'], result['height'] = width, height
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a compromised local service cannot redirect this adapter elsewhere.
        return None


def check_library_storage_target(current, requested, required_bytes):
    """Validate a proposed destination without creating or changing anything there."""
    if (not isinstance(requested, str) or not requested or len(requested) > 1024
            or re.search(r'[\x00-\x1f\x7f]', requested)):
        raise ValueError('请输入有效的 Ubuntu 绝对路径。')
    candidate = Path(requested)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('请输入不包含 .. 的 Ubuntu 绝对路径。')
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError('请选择已存在的普通目录。')
        target = candidate.resolve(strict=True)
        original = Path(current).resolve(strict=True)
        if target == original:
            raise ValueError('这就是当前保存位置，不需要切换。')
        if target in original.parents or original in target.parents:
            raise ValueError('新目录不能是当前素材库或其上级、下级目录。')
        if not os.access(target, os.W_OK | os.X_OK):
            raise ValueError('工作台没有权限写入这个目录。')
        with os.scandir(target) as entries:
            if next(entries, None) is not None:
                raise ValueError('请选择空目录，以免与已有文件混在一起。')
        available = shutil.disk_usage(target).free
    except (OSError, RuntimeError):
        raise ValueError('无法读取该目录，请检查路径和磁盘连接。') from None
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError('暂时无法确认现有素材大小。')
    # This is a preliminary check, not permission to migrate; recheck before any switch.
    minimum = required_bytes + max(512 * 1024**2, required_bytes // 10)
    if available < minimum:
        raise ValueError('新位置可用空间不足，暂不能作为计划位置。')
    return {'target': str(target), 'available_bytes': available,
            'minimum_bytes': minimum, 'active': False}


class Sources:
    """Only fixed localhost services and a small GET allowlist are reachable."""

    PATHS = {"flow": {"/health", "/api/state", "/api/library/facets"}, "gemini": {"/health"}}

    def __init__(self, flow_port=18888, gemini_port=8095, timeout=4, gemini_source="pooldesk"):
        self.ports = {"flow": int(flow_port), "gemini": int(gemini_port)}
        if any(not 1 <= port <= 65535 for port in self.ports.values()):
            raise ValueError("端口范围无效")
        if gemini_source not in {"pooldesk", "ubuntu-trial"}:
            raise ValueError("Gemini 服务类型无效")
        self.gemini_source = gemini_source
        self.timeout = timeout
        # Reuse the original console's local session in memory only.
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def read(self, service, path):
        if service not in self.PATHS or path not in self.PATHS[service]:
            raise ValueError("路径不在只读清单内")
        url = f"http://127.0.0.1:{self.ports[service]}{path}"
        try:
            request = Request(url, method="GET", headers={"Accept": "application/json"})
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                return None, "原后台返回的数据过大，暂时无法读取。"
            value = json.loads(body.decode("utf-8"))
            if not isinstance(value, dict):
                return None, "原后台返回的数据格式不正确。"
            return value, None
        except HTTPError as exc:
            if exc.code in (401, 403):
                return None, "原后台需要登录或授权，请从原入口处理。"
            return None, "原后台暂时无法读取，请稍后重试。"
        except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
            return None, "连接暂不可用，请检查对应后台。"

    def library(self, method, target, data=None, range_header=None):
        """Forward only documented library operations to the existing loopback service."""
        parts = urlsplit(target)
        path = parts.path
        item_path = re.fullmatch(r'/api/library/[0-9a-f]{64}(?:/(?:file|thumbnail|tags))?', path)
        allowed_query = {'q', 'kind', 'tag', 'account', 'page', 'limit', 'offset', 'subject', 'scene',
                         'action', 'style', 'camera', 'color', 'keywords', 'model', 'project_id', 'account_id',
                         'created_from', 'created_to', 'index_status', 'storage_status', 'download'}
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=30)
        if (parts.scheme or parts.netloc or parts.fragment or len(parts.query) > 3000
                or any(k not in allowed_query or len(v) > 500 for k, v in query)
                or len({k for k, _ in query}) != len(query)
                or not (path in ('/api/library', '/api/library/facets') or item_path)
                or method not in ('GET', 'POST') or (method == 'POST' and not path.endswith('/tags'))
                or (method == 'GET' and path.endswith('/tags'))
                or (range_header and not re.fullmatch(r'bytes=\d*-\d*', range_header))):
            raise ValueError('素材请求格式不正确。')
        if method == 'POST' and (query or not isinstance(data, dict) or set(data) - {'tags', 'structured_tags'}):
            raise ValueError('标签内容不正确。')
        suffix = '?' + urlencode(query) if query else ''
        headers = {'Accept': 'application/json'}
        encoded = None
        if method == 'POST':
            headers.update({'Content-Type': 'application/json', 'Origin': f"http://127.0.0.1:{self.ports['flow']}", 'X-Console-Request': '1'})
            encoded = json.dumps(data, ensure_ascii=False).encode('utf-8')
        if range_header:
            headers['Range'] = range_header
        request = Request(f"http://127.0.0.1:{self.ports['flow']}{path}{suffix}", data=encoded, headers=headers, method=method)
        try:
            return self.opener.open(request, timeout=max(self.timeout, 15))
        except HTTPError as exc:
            return exc

    def flow_risk_resume(self, profile):
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
            raise ValueError('账号标识不正确。')
        url = f"http://127.0.0.1:{self.ports['flow']}/api/accounts/risk-resume"
        body = json.dumps({'profile': profile}).encode('utf-8')
        request = Request(url, data=body, method='POST', headers={'Content-Type':'application/json',
                          'Origin': f"http://127.0.0.1:{self.ports['flow']}", 'X-Console-Request':'1'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(8193))
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('Flow 恢复状态未确认，请刷新后核对。')
            return result
        except HTTPError as exc:
            if exc.code == 400:
                raise ValueError('该账号当前没有需要人工恢复的暂停。') from None
            raise ValueError('Flow 恢复暂不可用，请稍后核对。') from None

    def refresh_flow_balances_once(self):
        """Ask the existing Flow service once to read its existing browser panels."""
        port = self.ports['flow']
        request = Request(f'http://127.0.0.1:{port}/api/accounts/credits/refresh', data=b'{}', method='POST',
                          headers={'Content-Type':'application/json', 'X-Console-Request':'1',
                                   'Origin':f'http://127.0.0.1:{port}'})
        with self.opener.open(request, timeout=self.timeout) as response:
            result = json.loads(response.read(8193))
        return isinstance(result, dict) and (result.get('queued') is True or result.get('refreshing') is True)

    def flow_retry_project(self, profile, project):
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
            raise ValueError('账号标识不正确。')
        if project is not None and (not isinstance(project, str) or not re.fullmatch(r'[0-9a-f-]{36}', project)):
            raise ValueError('项目标识不正确。')
        request = Request(f"http://127.0.0.1:{self.ports['flow']}/api/retry-project",
                          data=json.dumps({'profile': profile, 'project': project}).encode(), method='POST',
                          headers={'Content-Type':'application/json', 'X-Console-Request':'1',
                                   'Origin':f"http://127.0.0.1:{self.ports['flow']}"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(8193))
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('备用项目保存状态未确认，请刷新核对。')
            return result
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise ValueError('备用项目未确认保存，请刷新后核对。') from None

    def library_storage(self):
        """Read the current original library location from the running Flow process."""
        pid = subprocess.check_output(
            ['systemctl', '--user', 'show', 'studiowright-console.service', '--property=MainPID', '--value'],
            text=True, timeout=5).strip()
        if not pid.isdigit() or pid == '0':
            raise ValueError('Flow 素材库当前不可读取。')
        environment = dict(p.split(b'=', 1) for p in Path('/proc', pid, 'environ').read_bytes().split(b'\0') if b'=' in p)
        root = Path(os.fsdecode(environment.get(b'SW_ROOT', os.fsencode(Path.home()/'.local/share/studiowright-evaluation'))))
        library = (root/'material-library').resolve(strict=True)
        database = library/'library.db'
        if database.is_symlink() or not database.is_file():
            raise ValueError('Flow 素材库当前不可读取。')
        usage = shutil.disk_usage(library)
        with self.library('GET', '/api/library?limit=1') as response:
            if response.status != 200:
                raise ValueError('Flow 素材库当前不可读取。')
            result = json.load(response)
        asset_bytes = result.get('stats', {}).get('bytes')
        if type(asset_bytes) is not int or asset_bytes < 0:
            raise ValueError('暂时无法确认现有素材大小。')
        return {'current': str(library), 'available_bytes': usage.free,
                'total_bytes': usage.total, 'asset_bytes': asset_bytes}

    def library_technical(self, ident):
        if not re.fullmatch(r'[0-9a-f]{64}', ident):
            raise ValueError('素材编号不正确。')
        with self.library('GET', '/api/library/' + ident) as response:
            if response.status != 200:
                raise ValueError('素材不存在。')
            item = json.load(response)
        result = {'width': item.get('width'), 'height': item.get('height'), 'duration': item.get('duration')}
        if item.get('media_type') == 'video' and item.get('mime') == 'video/mp4' and (not result['width'] or not result['height'] or result['duration'] is None):
            library = Path(self.library_storage()['current']).resolve()
            objects = (library / 'objects').resolve()
            path = (objects / ident[:2] / (ident + '.mp4')).resolve()
            if path.is_relative_to(objects) and path.is_file():
                try:
                    result.update({k:v for k,v in mp4_metadata(path).items() if v is not None})
                except (OSError, ValueError, struct.error):
                    pass
        return result

    def apply_gemini_tags(self, ident, tags):
        if not re.fullmatch(r'[0-9a-f]{64}', ident) or not isinstance(tags, dict):
            raise ValueError('标签内容不正确。')
        python = Path.home() / '.local/share/studiowright-evaluation/venv/bin/python'
        if not python.is_file():
            raise ValueError('原素材库环境暂不可用。')
        payload = json.dumps({'asset_id': ident, 'tags': tags}, ensure_ascii=False).encode('utf-8')
        if len(payload) > 100000:
            raise ValueError('标签内容过长。')
        process = subprocess.run([str(python), '-B', '-c', APPLY_LIBRARY_TAGS], input=payload,
                                 capture_output=True, timeout=25)
        if process.returncode or len(process.stdout) > 1000:
            raise ValueError('原素材库标签保存状态未确认。')
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or type(result.get('added_dimensions')) is not int:
            raise ValueError('原素材库标签保存状态未确认。')
        return result['added_dimensions']

    def status(self):
        flow, flow_error = self.read("flow", "/health")
        gemini, gemini_error = self.read("gemini", "/health")
        gemini_name = "Gemini（Ubuntu 试验）" if self.gemini_source == "ubuntu-trial" else "Gemini PoolDesk（本机）"
        gemini_description = "Ubuntu 试验服务可响应；本机 PoolDesk 是独立后台" if self.gemini_source == "ubuntu-trial" else "本机 PoolDesk 后台可响应"
        return {
            "checked_at": time.time(),
            "flow": {"connected": bool(flow and flow.get("ok") is True), "message": flow_error},
            "gemini": {"connected": bool(gemini and gemini.get("status") == "ok"), "message": gemini_error,
                       "name": gemini_name, "description": gemini_description},
        }

    def snapshot(self):
        state, state_error = self.read("flow", "/api/state")
        facets, facets_error = self.read("flow", "/api/library/facets")
        if state is not None and (not isinstance(state.get("projects"), list) or not isinstance(state.get("tasks"), list)):
            state, state_error = None, "原后台项目或任务数据格式不正确。"
        projects = project_rows(state) if state is not None else []
        if state is not None:
            open_flow_project_names(state, projects)
        tasks = task_rows(state, projects) if state is not None else []
        accounts = account_rows(state) if state is not None else None
        valid_projects = {(p['source_profile'], p['source_id']): p['key'] for p in projects}
        retry_projects = {}
        for item in state.get('retry_projects', []) if isinstance(state, dict) else []:
            if isinstance(item, dict) and (item.get('profile'), item.get('project')) in valid_projects:
                retry_projects[item['profile']] = valid_projects[(item['profile'], item['project'])]
        by_profile = {a['profile']: a for a in accounts or []}
        for task in tasks:
            account = by_profile.get(task['source_profile'])
            if task['status'] == 'queued' and task['kind'] in ('image', 'video') and account:
                if account['flow_risk_paused']:
                    task['note'] = '等待人工核查并恢复该账号的 Flow。'
                elif account['flow_cooling']:
                    task['note'] = '账号冷却中，任务等待派发。'
        return {
            "checked_at": time.time(),
            "flow_state": {"available": state is not None, "message": state_error,
                           "simulated": bool(state and state.get("test") is True),
                           "skipped_projects": len(state["projects"]) - len(projects) if state else 0,
                           "skipped_tasks": len(state["tasks"]) - len(tasks) if state else 0},
            "library": {"available": facets is not None, "message": facets_error,
                        "counts": counts_only(facets) if facets is not None else None},
            "projects": projects,
            "retry_projects": retry_projects,
            "tasks": tasks,
            "flow_insights": flow_insights_summary(state) if state is not None else None,
            "flow_models": model_rows(state) if state is not None else {'image': [], 'video': []},
            "real_accounts": {"available": accounts is not None, "items": accounts or [],
                              "message": state_error if state is None else
                              ("当前 Flow 数据无法作为真实账号使用。" if accounts is None else None)},
        }


class Coordinator:
    """Reach only the workbench's own coordination component on loopback."""

    def __init__(self, port=18921, timeout=4):
        self.port = int(port)
        if not 1 <= self.port <= 65535:
            raise ValueError("调度组件端口无效")
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, data=None, key=None):
        if (method, path) not in {("GET", "/api/health"), ("GET", "/api/workflows"), ("POST", "/api/workflows"),
                                  ("GET", "/api/demo-accounts"), ("POST", "/api/demo-accounts/link"),
                                  ("POST", "/api/demo-accounts/unlink"), ("POST", "/api/demo-accounts/enabled"),
                                  ("POST", "/api/demo-accounts/account-enabled"), ("POST", "/api/demo-accounts/login"),
                                  ("POST", "/api/demo-accounts/keepalive"), ("POST", "/api/demo-accounts/quota"),
                                  ("POST", "/api/demo-accounts/quota-scope"), ("POST", "/api/demo-accounts/browser-sharing"), ("POST", "/api/demo-accounts/reconcile")}:
            raise ValueError("不支持的操作")
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Accept": "application/json"}
        encoded = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}",
                            "X-Coordinator-Request": "1"})
            if key is not None:
                headers["Idempotency-Key"] = key
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            with self.opener.open(Request(url, data=encoded, headers=headers, method=method), timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                status = response.status
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(4097)
            if status not in {400, 403, 404, 409, 500, 503}:
                return 502, {"error": "工作台任务组件返回异常。"}
        except (URLError, TimeoutError, OSError):
            return 503, {"error": "工作台任务组件暂不可用。"}
        try:
            if len(raw) > MAX_RESPONSE:
                raise ValueError()
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
            return status, result
        except (ValueError, UnicodeError):
            return 502, {"error": "工作台任务组件返回的数据不正确。"}


def make_handler(sources, deployment="local", coordinator=None, real_pool=None, activation=None, api_keys=None, backfill=None):
    coordinator = coordinator or Coordinator()
    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalWorkbench/1"

        def log_message(self, _format, *_args):
            # Never log source records, URLs, request bodies, or credentials.
            pass

        def respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def valid(self, mutation=False, content_type="application/json"):
            host = self.headers.get("Host", "").lower()
            valid_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            if host not in valid_hosts or (origin and origin != "http://" + host):
                return False
            if mutation and (origin != "http://" + host or self.headers.get("X-Workbench-Request") != "1" or
                             self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != content_type):
                return False
            return True

        def json_response(self, status, result):
            body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return self.respond(status, body, "application/json; charset=utf-8")

        def do_GET(self):
            if not self.valid():
                return self.respond(403, b"Forbidden", "text/plain; charset=utf-8")
            if api_keys and self.path == '/api/access-keys':
                try:
                    return self.json_response(200, {'items': api_keys.list()})
                except Exception:
                    return self.json_response(503, {'error': '密钥管理暂不可用。'})
            if activation and self.path == '/api/account-activation/schedule':
                return self.json_response(200, activation.schedule_status())
            if activation and self.path == '/api/account-activation/poll':
                return self.json_response(200, activation.poll_status())
            if activation and self.path == '/api/account-activation':
                return self.json_response(200, activation.status())
            if self.path in ALLOWED_STATIC:
                name, content_type = ALLOWED_STATIC[self.path]
                return self.respond(200, (ROOT / name).read_bytes(), content_type)
            if self.path == '/api/storage/library':
                try:
                    result = sources.library_storage()
                    result['plan'] = real_pool.library_storage_plan() if real_pool else None
                    return self.json_response(200, result)
                except (ValueError, OSError, subprocess.SubprocessError):
                    return self.json_response(503, {'error': '素材库存放位置暂时不可读取。'})
            if real_pool and self.path == '/api/auto-material-analysis':
                return self.json_response(200, real_pool.auto_material_state())
            if backfill and self.path == '/api/material-backfill':
                return self.json_response(200, backfill.state())
            if backfill and self.path == '/api/material-backfill/preview':
                try:
                    return self.json_response(200, backfill.preview(read_all_materials(sources)))
                except (ValueError, OSError, UnicodeError, URLError):
                    return self.json_response(503, {'error': '素材库暂时不可读取，请稍后再试。'})
            if urlsplit(self.path).path.startswith('/api/library'):
                try:
                    with sources.library('GET', self.path, range_header=self.headers.get('Range')) as upstream:
                        status = upstream.status
                        kind = upstream.headers.get_content_type()
                        length = upstream.headers.get('Content-Length', '')
                        is_media = bool(re.fullmatch(r'/api/library/[0-9a-f]{64}/(?:file|thumbnail)', urlsplit(self.path).path))
                        if is_media and status in (200, 206) and (kind.startswith('image/') or kind.startswith('video/')) and length.isdigit() and int(length) <= 1024**3:
                            self.send_response(status)
                            self.send_header('Content-Type', kind)
                            self.send_header('Content-Length', length)
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('X-Content-Type-Options', 'nosniff')
                            self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
                            for name in ('Content-Range', 'Accept-Ranges', 'Content-Disposition'):
                                value = upstream.headers.get(name)
                                if value and '\r' not in value and '\n' not in value:
                                    self.send_header(name, value)
                            self.end_headers()
                            remaining = int(length)
                            while remaining:
                                chunk = upstream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                            return
                        body = upstream.read(MAX_RESPONSE + 1)
                        if len(body) > MAX_RESPONSE:
                            return self.json_response(502, {'error': '素材库返回的数据过大。'})
                        return self.respond(status, body, 'application/json; charset=utf-8')
                except (ValueError, OSError, URLError):
                    return self.json_response(503, {'error': '素材库暂时不可读取。'})
            if real_pool and self.path == '/api/conversations':
                return self.json_response(200, real_pool.conversations())
            if real_pool and self.path == '/api/conversations/hidden':
                return self.json_response(200, real_pool.conversations(hidden=True))
            if real_pool and self.path == '/api/simulated-conversations':
                try:
                    from simulated_conversations import listing
                    return self.json_response(200, listing(real_pool.state_dir))
                except (ValueError, OSError, UnicodeError, ImportError):
                    return self.json_response(503, {'error': '模拟记录暂不可读取。'})
            if real_pool and re.fullmatch(r'/api/simulated-conversations/[0-9a-f-]{36}', self.path):
                try:
                    from simulated_conversations import detail
                    item = detail(real_pool.state_dir, self.path.rsplit('/', 1)[-1])
                    return self.json_response(200 if item else 404, item or {'error': '没有找到模拟记录。'})
                except (ValueError, OSError, UnicodeError, ImportError):
                    return self.json_response(503, {'error': '模拟记录暂不可读取。'})
            if real_pool and re.fullmatch(r'/api/gemini-images/[0-9a-f-]{36}/[0-3]', self.path):
                parts = self.path.split('/')
                item = real_pool.gemini_image(parts[-2],int(parts[-1]))
                return self.respond(200,item[0],item[1]) if item else self.json_response(404,{'error':'图片文件不可读取。'})
            if real_pool and urlsplit(self.path).path == '/api/material-extra-search':
                try:
                    query = parse_qsl(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=2)
                    if len({k for k, _ in query}) != len(query) or any(k not in ('q','page') for k, _ in query):
                        raise ValueError()
                    fields = dict(query)
                    return self.json_response(200, real_pool.search_material_extra(fields.get('q',''), int(fields.get('page','1'))))
                except ValueError:
                    return self.json_response(400, {'error': '搜索条件不正确。'})
            if real_pool and re.fullmatch(r'/api/conversations/[0-9a-f-]{36}', self.path):
                try:
                    item = real_pool.conversation(self.path.rsplit('/', 1)[-1])
                    return self.json_response(200 if item else 404, item or {'error': '对话不存在。'})
                except ValueError:
                    return self.json_response(400, {'error': '对话编号不正确。'})
            if real_pool and re.fullmatch(r'/api/material-extras/[0-9a-f]{64}', self.path):
                ident = self.path.rsplit('/', 1)[-1]
                try:
                    result = real_pool.material_extra(ident)
                    result['technical'] = sources.library_technical(ident)
                    return self.json_response(200, result)
                except (ValueError, OSError, URLError):
                    return self.json_response(503, {'error': '素材信息暂不可读取。'})
            if self.path == "/api/health":
                result = {"application": "unified-workbench", "version": 1, "deployment": deployment}
            elif self.path == "/api/status":
                result = sources.status()
            elif self.path == "/api/snapshot":
                result = sources.snapshot()
                if real_pool:
                    real_pool.annotate(result["real_accounts"])
                    result['gemini_stats'] = real_pool.dialogue_statistics()
                    result['keepalive_monitor'] = dict(real_pool.monitor_heartbeat)
                    if "tasks" in result:
                        real_pool.annotate_tasks(result["tasks"])
            elif self.path in {"/api/scheduler/health", "/api/scheduler/workflows", "/api/scheduler/demo-accounts"}:
                status, result = coordinator.request("GET", self.path.replace("/api/scheduler/", "/api/", 1))
                return self.json_response(status, result)
            elif real_pool and self.path == '/api/real-pool/activity':
                result = real_pool.request_history()
            elif real_pool and re.fullmatch(r"/api/real-pool/requests/[0-9a-f-]{36}", self.path):
                result = real_pool.get(self.path.rsplit('/', 1)[-1])
                if result is None:
                    return self.json_response(404, {"error": "没有找到该请求。"})
            elif real_pool and re.fullmatch(r"/api/real-pool/flow-requests/[0-9a-f-]{36}", self.path):
                result = real_pool.get_flow(self.path.rsplit('/', 1)[-1])
                if result is None:
                    return self.json_response(404, {"error": "没有找到该请求。"})
            elif real_pool and re.fullmatch(r"/api/real-pool/recoveries/[A-Za-z0-9_-]{1,128}/file", self.path):
                recovered = real_pool.recovered_file(self.path.split('/')[-2])
                if not recovered:
                    return self.json_response(404, {"error": "没有找到找回的图片。"})
                return self.respond(200, recovered[0], recovered[1])
            else:
                return self.respond(404, b"Not found", "text/plain; charset=utf-8")
            return self.json_response(200, result)

        def do_POST(self):
            if api_keys and (self.path == '/api/access-keys' or re.fullmatch(r'/api/access-keys/[0-9a-f]{32}/(?:revoke|copy)', self.path)):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                if real_pool and real_pool.maintenance():
                    return self.json_response(503, {'error': '工作台正在更新，请稍后操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1 or not length.isdigit() or not 0 < int(length) <= 4096:
                    return self.json_response(400, {'error': '密钥请求格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict):
                        raise ValueError('密钥请求格式不正确。')
                    if self.path == '/api/access-keys':
                        if set(data) != {'name'}:
                            raise ValueError('密钥请求格式不正确。')
                        return self.json_response(201, api_keys.create(data['name']))
                    if data:
                        raise ValueError('密钥请求格式不正确。')
                    if self.path.endswith('/copy'):
                        return self.json_response(200, {'key': api_keys.reveal(self.path.split('/')[-2])})
                    return self.json_response(200, {'revoked': api_keys.revoke(self.path.split('/')[-2])})
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '密钥请求格式不正确。'})
                except Exception:
                    return self.json_response(503, {'error': '密钥管理暂不可用，请先刷新密钥列表。'})
            if activation and self.path == '/api/account-activation/poll':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1 or not length.isdigit() or not 0 < int(length) <= 128:
                    return self.json_response(400, {'error': '轮询设置格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict) or set(data) != {'enabled', 'interval_minutes'}:
                        raise ValueError('轮询设置格式不正确。')
                    return self.json_response(200, activation.set_poll(data['enabled'], data['interval_minutes']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '轮询设置格式不正确。'})
                except Exception:
                    return self.json_response(503, {'error': '轮询设置暂不可用，请刷新核对。'})
            if activation and self.path == '/api/account-activation/schedule':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1 or not length.isdigit() or not 0 < int(length) <= 128:
                    return self.json_response(400, {'error': '定时设置格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict) or set(data) != {'enabled', 'time'}:
                        raise ValueError('定时设置格式不正确。')
                    return self.json_response(200, activation.set_schedule(data['enabled'], data['time']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '定时设置格式不正确。'})
                except Exception:
                    return self.json_response(503, {'error': '定时设置暂不可用，请刷新核对。'})
            if activation and self.path == '/api/account-activation':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length') != '2' or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {'error': '请求内容格式不正确。'})
                try:
                    return self.json_response(202, activation.start())
                except ValueError as exc:
                    return self.json_response(409, {'error': str(exc)})
                except Exception:
                    return self.json_response(503, {'error': '账号状态暂不可读取，未开始激活。'})
            if real_pool and self.path == '/api/auto-material-analysis':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100:
                    return self.json_response(400, {'error': '自动分析设置不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(body, dict) or set(body) != {'enabled'}:
                        raise ValueError('自动分析设置不正确。')
                    return self.json_response(200, real_pool.set_auto_material_enabled(body['enabled']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '自动分析设置不正确。'})
            if backfill and self.path in ('/api/material-backfill', '/api/material-backfill/resume'):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100:
                    return self.json_response(400, {'error': '补打请求格式不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if self.path.endswith('/resume'):
                        if body != {}:
                            raise ValueError('补打请求格式不正确。')
                        result = backfill.resume()
                    else:
                        if not isinstance(body, dict) or set(body) != {'scope'}:
                            raise ValueError('补打请求格式不正确。')
                        result = backfill.start(body['scope'], self.headers.get('Idempotency-Key', ''), read_all_materials(sources))
                    return self.json_response(200, result)
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(409, {'error': str(exc) or '补打请求格式不正确。'})
                except (OSError, URLError):
                    return self.json_response(503, {'error': '素材库暂不可读取，未开始补打。'})
            if self.path == '/api/storage/library/plan':
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 5000:
                    return self.json_response(400, {'error': '路径格式不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(body, dict) or set(body) != {'target'}:
                        raise ValueError('路径格式不正确。')
                    info = sources.library_storage()
                    checked = check_library_storage_target(info['current'], body['target'], info['asset_bytes'])
                    checked['plan'] = real_pool.save_library_storage_plan(info['current'], checked['target'])
                    return self.json_response(200, checked)
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '路径格式不正确。'})
                except (OSError, subprocess.SubprocessError, URLError):
                    return self.json_response(503, {'error': '暂时无法检查该位置。'})
            if real_pool and re.fullmatch(r'/api/material-analysis/[0-9a-f]{64}', self.path):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length') != '2' or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {'error': '请求内容格式不正确。'})
                ident, request_id = self.path.rsplit('/', 1)[-1], self.headers.get('Idempotency-Key', '')
                try:
                    account_data = sources.snapshot().get('real_accounts', {})
                    if not account_data.get('available'):
                        return self.json_response(503, {'error': '现有账号池暂不可读取。'})
                    real_pool.begin_material_analysis(ident, request_id)
                except ValueError as exc:
                    return self.json_response(400, {'error': str(exc)})
                result = perform_material_analysis(sources, real_pool, ident, request_id, account_data)
                return self.json_response(200, result)
            if real_pool and re.fullmatch(r'/api/material-extras/[0-9a-f]{64}', self.path):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 20000:
                    return self.json_response(400, {'error': '请求内容格式不正确。'})
                try:
                    ident = self.path.rsplit('/', 1)[-1]
                    sources.library_technical(ident)  # Refuse records for nonexistent assets.
                    values = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    return self.json_response(200, real_pool.save_material_extra(ident, values))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '标签格式不正确。'})
                except (OSError, URLError):
                    return self.json_response(503, {'error': '素材库暂时不可读取。'})
            if (self.path == '/api/conversations' or re.fullmatch(r'/api/conversations/[0-9a-f-]{36}/(?:rename|visibility)', self.path)
                    or re.fullmatch(r'/api/library/[0-9a-f]{64}/tags', self.path)):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100000:
                    return self.json_response(400, {'error': '请求格式不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(body, dict):
                        raise ValueError('请求内容格式不正确。')
                    if self.path.startswith('/api/library/'):
                        with sources.library('POST', self.path, body) as upstream:
                            raw = upstream.read(MAX_RESPONSE + 1)
                            if len(raw) > MAX_RESPONSE:
                                raise ValueError('素材库返回数据过大。')
                            return self.respond(upstream.status, raw, 'application/json; charset=utf-8')
                    if not real_pool or real_pool.maintenance():
                        return self.json_response(503, {'error': '工作台正在更新。'})
                    if self.path.endswith('/visibility'):
                        if set(body) != {'hidden'}:
                            raise ValueError('对话状态不正确。')
                        return self.json_response(200, real_pool.hide_conversation(self.path.split('/')[-2], body['hidden']))
                    if set(body) != {'title'}:
                        raise ValueError('对话名称格式不正确。')
                    if self.path == '/api/conversations':
                        return self.json_response(201, real_pool.create_conversation(body['title']))
                    return self.json_response(200, real_pool.rename_conversation(self.path.split('/')[-2], body['title']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '请求内容格式不正确。'})
                except (OSError, URLError):
                    return self.json_response(503, {'error': '暂时无法处理，请稍后再试。'})
            upload = re.fullmatch(r"/api/real-pool/uploads/([0-9a-f-]{36})/([0-3])/(jpg|jpeg|png|webp|mp4|mov|webm|pdf)", self.path)
            if upload:
                if not real_pool or not self.valid(mutation=True, content_type="application/octet-stream"):
                    return self.json_response(403, {"error": "请从工作台上传附件。"})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100 * 1024 * 1024:
                    return self.json_response(400, {"error": "附件大小不符合要求。"})
                try:
                    return self.json_response(201, real_pool.upload(upload.group(1), int(upload.group(2)), upload.group(3), self.rfile, int(length)))
                except ValueError as exc:
                    return self.json_response(400, {"error": str(exc)})
                except Exception:
                    return self.json_response(500, {"error": "附件未上传完成，本次没有发送给 Gemini。"})
            if re.fullmatch(r"/api/real-pool/recoveries/[A-Za-z0-9_-]{1,128}", self.path):
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {"error": "请从工作台发起操作。"})
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length') != '2' or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {"error": "请求内容格式不正确。"})
                try:
                    return self.json_response(200, real_pool.recover_image(self.path.rsplit('/', 1)[-1]))
                except ValueError as exc:
                    return self.json_response(400, {"error": str(exc)})
                except Exception:
                    return self.json_response(500, {"error": "暂时无法找回，请核对原任务。"})
            if self.path in {"/api/real-pool/check", "/api/real-pool/recover-account", "/api/real-pool/chat", "/api/real-pool/flow", "/api/real-pool/enabled", "/api/real-pool/keepalive-enabled", "/api/real-pool/dispatch-policy", "/api/real-pool/flow-risk-resume", "/api/real-pool/flow-retry-project"}:
                if not real_pool:
                    return self.json_response(503, {"error": "真实账号接入暂不可用。"})
                if getattr(real_pool, 'maintenance', lambda: False)():
                    return self.json_response(503, {"error": "工作台正在更新，请稍后操作。"})
                if not self.valid(mutation=True):
                    return self.json_response(403, {"error": "请从工作台发起操作。"})
                length = self.headers.get("Content-Length", "")
                if self.headers.get("Transfer-Encoding") or not length.isdigit() or int(length) > 100000:
                    return self.json_response(400, {"error": "请求格式不正确。"})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("请求内容格式不正确。")
                    snapshot = sources.snapshot()
                    account_data = snapshot.get("real_accounts", {})
                    if not account_data.get("available"):
                        return self.json_response(503, {"error": "现有账号池暂不可读取。"})
                    if self.path.endswith('/flow-retry-project'):
                        if set(data) != {'account', 'project'} or not isinstance(data['account'], str) or not re.fullmatch(r'[0-9a-f]{64}', data['account']):
                            raise ValueError('请选择已登记账号。')
                        matches = [a for a in account_data['items'] if a.get('key') == 'flow-account-' + data['account']]
                        if len(matches) != 1:
                            raise ValueError('账号无法唯一匹配，请刷新。')
                        profile, selected = matches[0]['profile'], data['project']
                        if selected is None:
                            return self.json_response(200, sources.flow_retry_project(profile, None))
                        projects = [p for p in snapshot.get('projects', []) if p['key'] == selected and p['source_profile'] == profile]
                        if len(projects) != 1:
                            raise ValueError('请选择此账号已登记的项目。')
                        return self.json_response(200, sources.flow_retry_project(profile, projects[0]['source_id']))
                    if self.path.endswith('/flow-risk-resume'):
                        if set(data) != {'account'} or not isinstance(data['account'], str) or not re.fullmatch(r'[0-9a-f]{64}', data['account']):
                            raise ValueError('请选择已登记账号。')
                        matches = [a for a in account_data['items'] if a.get('key') == 'flow-account-' + data['account']]
                        if len(matches) != 1 or matches[0].get('flow_risk_paused') is not True:
                            raise ValueError('该账号当前没有需要人工恢复的暂停。')
                        return self.json_response(200, sources.flow_risk_resume(matches[0]['profile']))
                    if self.path.endswith('/dispatch-policy'):
                        if set(data) != {'five_hour_limit', 'weekly_limit'}:
                            raise ValueError('请求内容格式不正确。')
                        return self.json_response(200, real_pool.set_dispatch_policy(data['five_hour_limit'], data['weekly_limit']))
                    if self.path.endswith('/flow'):
                        record, created = real_pool.submit_flow(data, self.headers.get('Idempotency-Key', ''), snapshot)
                        return self.json_response(201 if created else 200, record)
                    if self.path.endswith('/check'):
                        if set(data) != {"account"}:
                            raise ValueError("请求内容格式不正确。")
                        return self.json_response(200, real_pool.check(data['account'], account_data['items']))
                    if self.path.endswith('/recover-account'):
                        if set(data) != {'account'}:
                            raise ValueError('请求内容格式不正确。')
                        return self.json_response(200, real_pool.recover_account(data['account'], account_data['items']))
                    if self.path.endswith('/keepalive-enabled'):
                        if set(data) != {'account', 'enabled'}:
                            raise ValueError('请求内容格式不正确。')
                        return self.json_response(200, real_pool.set_keepalive_enabled(data['account'], data['enabled'], account_data['items']))
                    if self.path.endswith('/enabled'):
                        if set(data) != {'account', 'enabled'}:
                            raise ValueError('请求内容格式不正确。')
                        return self.json_response(200, real_pool.set_gemini_enabled(data['account'], data['enabled'], account_data['items']))
                    if set(data) - {"account", "prompt", "attachments", "conversation_id", "model"} or not {'account', 'prompt'} <= set(data):
                        raise ValueError("请求内容格式不正确。")
                    if 'attachments' in data and not isinstance(data['attachments'], list):
                        raise ValueError("附件信息不正确。")
                    if data.get('conversation_id'):
                        record, created = real_pool.generate(data['account'], data['prompt'], self.headers.get('Idempotency-Key', ''), account_data['items'], data.get('attachments', []), data['conversation_id'], data.get('model'))
                    elif data.get('model') is not None:
                        raise ValueError('模型选择需要在对话中使用。')
                    elif data.get('attachments'):
                        record, created = real_pool.generate(data['account'], data['prompt'], self.headers.get('Idempotency-Key', ''), account_data['items'], data['attachments'])
                    else:
                        record, created = real_pool.generate(data['account'], data['prompt'], self.headers.get('Idempotency-Key', ''), account_data['items'])
                    return self.json_response(201 if created else 200, record)
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {"error": str(exc) or "请求内容格式不正确。"})
                except Exception:
                    return self.json_response(500, {"error": "工作台暂不能处理，请查看本次请求记录。"})
            paths = {"/api/scheduler/workflows", "/api/scheduler/demo-accounts/link",
                     "/api/scheduler/demo-accounts/unlink", "/api/scheduler/demo-accounts/enabled",
                     "/api/scheduler/demo-accounts/account-enabled", "/api/scheduler/demo-accounts/login",
                     "/api/scheduler/demo-accounts/keepalive", "/api/scheduler/demo-accounts/quota",
                     "/api/scheduler/demo-accounts/quota-scope", "/api/scheduler/demo-accounts/browser-sharing", "/api/scheduler/demo-accounts/reconcile"}
            if self.path not in paths:
                return self.send_error(501, "Unsupported method")
            if not self.valid(mutation=True):
                return self.json_response(403, {"error": "请从工作台发起操作。"})
            length = self.headers.get("Content-Length", "")
            key = self.headers.get("Idempotency-Key", "")
            if (self.headers.get("Transfer-Encoding") or not length.isdigit() or int(length) > 4096 or
                    (self.path == "/api/scheduler/workflows" and not re.fullmatch(r"demo-[A-Za-z0-9_-]{8,95}", key))):
                return self.json_response(400, {"error": "模拟请求格式不正确。"})
            try:
                data = json.loads(self.rfile.read(int(length)).decode("utf-8"))
                flow_ids = {"demo-flow-shared", "demo-flow-separate"}
                gemini_ids = {"demo-gemini-shared", "demo-gemini-separate"}
                if not isinstance(data, dict):
                    raise ValueError()
                if self.path == "/api/scheduler/workflows":
                    if (set(data) - {"scenario", "mock_flow_source", "mock_gemini_source", "mock_pool_auto"} or data.get("scenario") not in {
                            "success", "flow_only", "gemini_only", "flow_failed", "flow_unknown", "save_failed", "analysis_failed", "analysis_unknown", "gemini_unknown"
                        } or ("mock_flow_source" in data and data["mock_flow_source"] not in flow_ids) or
                            ("mock_gemini_source" in data and data["mock_gemini_source"] not in gemini_ids) or
                            ("mock_pool_auto" in data and type(data["mock_pool_auto"]) is not bool) or
                            (data.get("mock_pool_auto") is True and ("mock_flow_source" in data or "mock_gemini_source" in data))):
                        raise ValueError()
                elif self.path.endswith("/link"):
                    if (set(data) != {"flow_source", "gemini_source", "confirm"} or data["flow_source"] not in flow_ids or
                            data["gemini_source"] not in gemini_ids or data["confirm"] is not True): raise ValueError()
                elif self.path.endswith("/unlink"):
                    if (set(data) != {"link_id", "confirm"} or not isinstance(data["link_id"], str) or
                            not re.fullmatch(r"demo-link-(shared|[0-9a-f]{32})", data["link_id"]) or data["confirm"] is not True): raise ValueError()
                elif self.path.endswith('/account-enabled') or self.path.endswith('/browser-sharing'):
                    if (set(data) != {'account_id', 'enabled'} or not isinstance(data['account_id'], str) or
                            not re.fullmatch(r'demo-account-demo-(flow|gemini)-(shared|separate)', data['account_id']) or
                            type(data['enabled']) is not bool): raise ValueError()
                elif self.path.endswith('/login'):
                    if (set(data) != {'source_id', 'state'} or data['source_id'] not in flow_ids | gemini_ids or
                            data['state'] not in ('ready', 'expired', 'unknown')): raise ValueError()
                elif self.path.endswith('/quota'):
                    if (set(data) != {'source_id', 'remaining'} or data['source_id'] not in flow_ids | gemini_ids or
                            (data['remaining'] is not None and (type(data['remaining']) is not int or not 0 <= data['remaining'] <= 10000))): raise ValueError()
                elif self.path.endswith('/quota-scope'):
                    if (set(data) != {'account_id', 'scope', 'remaining'} or not isinstance(data['account_id'], str) or
                            not re.fullmatch(r'demo-account-demo-(flow|gemini)-(shared|separate)', data['account_id']) or
                            data['scope'] not in ('per_service', 'shared') or
                            (data['remaining'] is not None and (type(data['remaining']) is not int or not 0 <= data['remaining'] <= 10000))): raise ValueError()
                elif self.path.endswith('/reconcile'):
                    if (set(data) != {'step_id', 'outcome'} or not isinstance(data['step_id'], str) or
                            not re.fullmatch(r'[0-9a-f-]{36}', data['step_id']) or data['outcome'] not in ('spent', 'not_spent')): raise ValueError()
                elif (set(data) != {"source_id", "enabled"} or data["source_id"] not in flow_ids | gemini_ids or
                      type(data["enabled"]) is not bool): raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                return self.json_response(400, {"error": "只允许操作模拟数据。"})
            if self.path == "/api/scheduler/workflows":
                spec = {"mode": "simulation", "title": "模拟流程", **data}
                status, result = coordinator.request("POST", "/api/workflows", spec, key)
            else:
                status, result = coordinator.request("POST", self.path.replace("/api/scheduler/", "/api/", 1), data)
            return self.json_response(status, result)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="创作工作台")
    parser.add_argument("--port", type=int, default=18910)
    parser.add_argument("--flow-port", type=int, default=18888)
    parser.add_argument("--gemini-port", type=int, default=8095)
    parser.add_argument("--coordinator-port", type=int, default=18921)
    parser.add_argument("--deployment", choices=("local", "ubuntu"), default="local")
    args = parser.parse_args()
    real_pool = None
    api_keys = None
    backfill = None
    if args.deployment == "ubuntu":
        os.umask(0o077)
        from live_pool import RealPool
        from unified_keys import UnifiedKeys
        real_pool = RealPool(Path.home() / '.local/state/creative-unified-workbench', ROOT)
        from material_backfill import MaterialBackfill
        backfill = MaterialBackfill(real_pool)
        api_keys = UnifiedKeys(Path.home() / '.local/state/creative-unified-workbench/api-keys.sqlite3')
    sources = Sources(args.flow_port, args.gemini_port, gemini_source="ubuntu-trial" if args.deployment == "ubuntu" else "pooldesk")
    activation = None
    coordinator = Coordinator(args.coordinator_port)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(sources, args.deployment, coordinator, real_pool, api_keys=api_keys))
    if real_pool:
        from activation import Activation
        activation = Activation(Path.home() / '.local/state/creative-unified-workbench', ROOT, sources, real_pool)
        server.RequestHandlerClass = make_handler(sources, args.deployment, coordinator, real_pool, activation, api_keys, backfill)
    stop_monitor = threading.Event()
    if real_pool:
        def monitor():
            # Let startup and any deployment finish before touching an account.
            if stop_monitor.wait(120):
                return
            while not stop_monitor.is_set():
                try:
                    real_pool.purge_stale_uploads()
                    account_data = sources.snapshot().get('real_accounts', {})
                    if account_data.get('available'):
                        real_pool.monitor_once(account_data.get('items', []))
                        real_pool.monitor_heartbeat.update(last_completed=time.time(), last_error=None)
                    else:
                        real_pool.monitor_heartbeat['last_error'] = 'accounts_unavailable'
                except Exception as error:
                    real_pool.monitor_heartbeat['last_error'] = type(error).__name__
                stop_monitor.wait(60)
        threading.Thread(target=monitor, name='account-monitor', daemon=True).start()
        def recover_new_images():
            if stop_monitor.wait(120):
                return
            while not stop_monitor.is_set():
                try:
                    if not real_pool.maintenance():
                        real_pool.auto_recover_tasks(sources.snapshot().get('tasks', []))
                except Exception:
                    pass
                stop_monitor.wait(60)
        threading.Thread(target=recover_new_images, name='flow-recovery', daemon=True).start()
        def analyze_new_materials():
            if stop_monitor.wait(120):
                return
            while not stop_monitor.is_set():
                try:
                    run_auto_material_once(sources, real_pool)
                except Exception:
                    pass  # Keep source material and the persisted job state intact.
                stop_monitor.wait(60)
        threading.Thread(target=analyze_new_materials, name='material-auto-analysis', daemon=True).start()
        def analyze_backfill():
            # Only a persisted task explicitly started by the user can be claimed.
            backfill.wake.wait(120)
            while not stop_monitor.is_set():
                backfill.wake.clear()
                try:
                    if not real_pool.maintenance() and backfill.has_pending():
                        run_backfill_once(sources, real_pool, backfill)
                except Exception:
                    pass
                backfill.wake.wait(10)
        threading.Thread(target=analyze_backfill, name='material-backfill', daemon=True).start()
    print(f"创作工作台：http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor.set()
        if backfill:
            backfill.wake.set()
        if activation:
            activation.close()
        server.server_close()
        if real_pool:
            real_pool.close()


if __name__ == "__main__":
    main()
