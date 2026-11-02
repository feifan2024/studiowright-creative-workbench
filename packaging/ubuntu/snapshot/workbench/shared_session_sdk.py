"""Single-request Gemini adapter, run inside the existing Gemini runtime.

Credentials enter through stdin only and remain in memory. This file never writes
cookies or starts refresh tasks; the caller holds the Flow profile lock.
"""
import asyncio
import ast
from contextlib import asynccontextmanager
import importlib
import inspect
import hashlib
from importlib.metadata import version
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import time
import uuid
from urllib.parse import urljoin, urlsplit

from curl_cffi.requests import AsyncSession
from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus, BROWSER_TYPE, Endpoint, Headers
from gemini_webapi.utils import logger

logger.remove()


def workbench_proxy_override():
    """Read an optional private loopback-only route; invalid settings fail closed."""
    path = Path.home() / '.local/state/creative-unified-workbench/gemini-proxy-route.json'
    if path.is_symlink():
        raise ValueError('unsafe_workbench_proxy_route')
    if not path.exists():
        return None
    metadata = path.stat()
    if (not path.is_file() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
        raise ValueError('unsafe_workbench_proxy_route')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('invalid_workbench_proxy_route') from None
    if (not isinstance(data, dict) or set(data) != {'kind', 'port'}
            or data['kind'] not in ('reverse_ssh', 'isolated') or type(data['port']) is not int
            or not 1024 <= data['port'] <= 65535):
        raise ValueError('invalid_workbench_proxy_route')
    return 'http://127.0.0.1:' + str(data['port'])


def system_loopback_proxy():
    """Use the user's existing local desktop HTTPS proxy, if configured."""
    override = workbench_proxy_override()
    if override:
        return override
    def setting(schema, field):
        value = subprocess.check_output(['gsettings', 'get', schema, field], text=True,
                                        stderr=subprocess.DEVNULL, timeout=3).strip()
        return ast.literal_eval(value)
    try:
        if setting('org.gnome.system.proxy', 'mode') != 'manual':
            return None
        host = setting('org.gnome.system.proxy.https', 'host')
        port = setting('org.gnome.system.proxy.https', 'port')
        if not isinstance(host, str) or type(port) is not int or not 0 < port < 65536:
            return None
        if host.lower() == 'localhost':
            host = '127.0.0.1'
        if not ipaddress.ip_address(host).is_loopback:
            return None
        return 'http://' + ('[' + host + ']' if ':' in host else host) + ':' + str(port)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError):
        return None


def identity_status(page, expected):
    matches = re.findall(r'["\']oPEP7c["\']\s*:\s*["\']([^"\']+)["\']', page)
    identities = {value.strip().lower() for value in matches if '@' in value}
    return {'key_present': bool(matches), 'expected_seen': expected.lower() in page.lower(),
            'identity_confirmed': identities == {expected.lower()}}


def usage_summary(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for name in ('current_5h', 'weekly'):
        metric = value.get(name)
        if not isinstance(metric, dict):
            continue
        percent = metric.get('usage_percentage')
        remaining = metric.get('remaining_credits')
        if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
            percent = None
        if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0:
            remaining = None
        reset = metric.get('reset_at')
        if not isinstance(reset, str) or len(reset) > 64 or any(ord(c) < 32 for c in reset):
            reset = None
        if percent is not None or remaining is not None:
            result[name] = {'usage_percentage': percent, 'remaining_credits': remaining, 'reset_at': reset}
    credits = value.get('ai_credits_remaining')
    if type(credits) in (int, float) and math.isfinite(credits) and credits >= 0:
        result['ai_credits_remaining'] = credits
    return result or None


def model_choices(models):
    """Return only public model names and labels, never model headers or session data."""
    choices, seen = [], set()
    for model in models:
        ident = getattr(model, 'model_name', None)
        label = getattr(model, 'display_name', None) or ident
        if (not isinstance(ident, str) or not ident.strip() or len(ident) > 200
                or not isinstance(label, str) or not label.strip() or len(label) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in ident + label) or ident in seen):
            continue
        choices.append({'id': ident, 'label': label})
        seen.add(ident)
        if len(choices) >= 100:
            break
    return choices


def renewed_cookie_from_session(before, after):
    """Return a single rotated session cookie only when this operation changed it."""
    if len(before) != 1 or len(after) != 1:
        return None
    old = before[0]
    new = after[0]
    if (old.get('name') != '__Secure-1PSIDTS' or new.get('name') != old.get('name')
            or not old.get('value') or not new.get('value')
            or new.get('domain') != old.get('domain') or new.get('path') != old.get('path')):
        return None
    old_expiry, new_expiry = old.get('expires'), new.get('expires')
    expiry_increased = (type(new_expiry) in (int, float) and math.isfinite(new_expiry) and new_expiry > 0
                        and (type(old_expiry) not in (int, float) or not math.isfinite(old_expiry) or new_expiry > old_expiry))
    if new.get('value') == old.get('value') and not expiry_increased:
        return None
    return new


def session_cookie_snapshot(jar):
    # This metadata and cookie value stay in the private adapter process/pipe only.
    return [{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'expires':c.expires}
            for c in jar if c.name == '__Secure-1PSIDTS' and c.value]


class DuplicateSubmission(Exception):
    pass


class GuardedSession(AsyncSession):
    """An SDK-compatible session that also prevents repeat submissions."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = 0
        self.generation_status = None

    def enter(self, url):
        if 'StreamGenerate' in str(url):
            if self.sent:
                raise DuplicateSubmission()
            self.sent += 1

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.enter(url)
        kwargs['allow_redirects'] = False
        if 'StreamGenerate' in str(url):
            # Stream stalls may last longer than short login/metadata requests.
            kwargs['timeout'] = 120
        async with super().stream(method, url, **kwargs) as response:
            if 'StreamGenerate' in str(url):
                self.generation_status = response.status_code
            yield response

    async def post(self, url, **kwargs):
        self.enter(url)
        kwargs['allow_redirects'] = False
        return await super().post(url, **kwargs)


class ExplicitClient(GeminiClient):
    def __init__(self, cookies, expected):
        super().__init__(cookies['__Secure-1PSID'], cookies['__Secure-1PSIDTS'])
        self.expected_identity = expected.lower()
        self.identity = {}
        self.guard = None
        self.phase = 'initializing'
        self.first_text_at = None

    async def init(self, **_kwargs):
        async with self._lock:
            if self._running:
                return
            auth = importlib.import_module('gemini_webapi.utils.get_access_token')
            proxy = system_loopback_proxy()
            options = {'impersonate': BROWSER_TYPE, 'timeout': 45, 'retry': 0}
            if proxy:
                options['proxies'] = {'https': proxy}
            self.guard = GuardedSession(**options)
            self.client = self.guard
            try:
                self.phase = 'opening_google'
                response = await auth._send_request(self.client, self._cookies)
                payload = auth._extract_payload(response)
                if not payload or not payload[0]:
                    raise ValueError('authentication_unavailable')
                page = response.text
                # Only WIZ's account identity is authoritative for this session.
                self.identity = identity_status(page, self.expected_identity)
                self.access_token, self.build_label, self.session_id, language, self.push_id = payload
                self.language = language or 'en'
                self._cookie_source = 'Explicit Account'
                self.timeout, self.watchdog_timeout = 120, 120
                self.auto_refresh = self.auto_close = False
                self._running = True
                self._model_registry.clear()
                self.phase = 'loading_account'
                await self._fetch_user_status()
            except BaseException:
                await self.close()
                raise

    async def close(self, delay=0):
        self._running = False
        if self.client:
            await self.client.close()
            self.client = None

    async def _batch_execute(self, *args, **kwargs):
        kwargs['current_retry'] = 0
        return await super()._batch_execute(*args, **kwargs)

    async def _fetch_usage_info(self, *args, **kwargs):
        # The SDK refreshes usage after a complete response. A failure there
        # must not discard an answer that its stream has already completed.
        caller = inspect.currentframe().f_back
        after_generation = caller is not None and caller.f_code is inspect.unwrap(GeminiClient._generate).__code__
        if after_generation:
            return None
        return await super()._fetch_usage_info(*args, **kwargs)

    async def _generate(self, *args, **kwargs):
        kwargs['current_retry'] = 0
        async for item in super()._generate(*args, **kwargs):
            if self.first_text_at is None:
                try:
                    if item.text_delta:
                        self.first_text_at = time.time()
                except Exception:
                    pass
            yield item


def allowed_generated_image_url(url):
    try:
        u = urlsplit(url)
        return (isinstance(url, str) and len(url) <= 8192 and u.scheme == 'https' and not u.username and not u.password
                and u.port in (None, 443) and bool(u.hostname)
                and (u.hostname in ('googleusercontent.com', 'lh3.google.com')
                     or u.hostname.endswith('.googleusercontent.com') or u.hostname.endswith('.ggpht.com')))
    except (TypeError, ValueError):
        return False


async def capture_generated_images(output, client):
    """Store only real generated image bytes, never a model-written placeholder."""
    try:
        images = output.candidates[output.chosen].generated_images or []
    except Exception:
        return [], 0
    expected = min(len(images), 4)
    captured = []
    if not expected:
        return captured, expected
    directory = Path.home()/'.local/state/creative-unified-workbench/gemini-generated-images'
    if directory.is_symlink() or directory.parent.is_symlink():
        return [], expected
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return [], expected
    for item in images[:expected]:
        try:
            url = item.url
            data = bytearray()
            visited = set()
            for _ in range(4):  # At most three redirects, never a generation retry.
                if not allowed_generated_image_url(url) or url in visited:
                    raise ValueError('invalid_image_redirect')
                visited.add(url)
                next_url = None
                async with client.guard.stream('GET', url, headers=Headers.REFERER.value, timeout=20, allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location', '')
                        next_url = urljoin(url, location) if location else ''
                        if not next_url or not allowed_generated_image_url(next_url):
                            raise ValueError('invalid_image_redirect')
                    elif response.status_code != 200:
                        raise ValueError('image_download_failed')
                    else:
                        async for chunk in response.aiter_content():
                            data.extend(chunk)
                            if len(data) > 20*1024*1024:
                                raise ValueError('image_too_large')
                if next_url is None:
                    break
                url = next_url
            else:
                raise ValueError('too_many_image_redirects')
            mime = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                    'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                    'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None)
            if not mime:
                continue
            ident = uuid.uuid4().hex
            target = directory/(ident+'.image')
            with os.fdopen(os.open(target, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600),'wb') as f:
                f.write(data)
            captured.append({'id':ident,'mime':mime,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        except Exception:
            continue
    return captured, expected


async def main(payload):
    if version('gemini-webapi') != '2.1.1':
        return {'result': 'unsupported_runtime', 'submitted': False}
    operation = payload.get('operation')
    cookies = payload.get('cookies')
    expected = payload.get('expected')
    prompt = payload.get('prompt')
    files = payload.get('files', [])
    conversation = payload.get('conversation')
    if (operation not in ('check', 'renew', 'generate') or not isinstance(cookies, dict)
            or set(cookies) != {'__Secure-1PSID', '__Secure-1PSIDTS'}
            or not all(isinstance(v, str) and 8 <= len(v) <= 16000 and not any(c in v for c in '\r\n\x00') for v in cookies.values())
            or not isinstance(expected, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+', expected)
            or (operation == 'generate' and (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000))
            or not isinstance(files, list) or len(files) > 4):
        return {'result': 'invalid_input', 'submitted': False}
    if conversation is not None and (not isinstance(conversation, dict)
            or set(conversation) not in (set(), {'model'}, {'metadata', 'model'})
            or len(json.dumps(conversation, ensure_ascii=False)) > 16000
            or (conversation and (not isinstance(conversation['model'], str) or not conversation['model']
                or len(conversation['model']) > 200 or any(ord(c) < 32 for c in conversation['model'])))
            or ('metadata' in conversation and (not isinstance(conversation['metadata'], list) or not conversation['metadata']))):
        return {'result': 'invalid_input', 'submitted': False}
    root = Path.home() / '.local/state/creative-unified-workbench/uploads'
    for name in files:
        if not isinstance(name, str):
            return {'result': 'invalid_input', 'submitted': False}
        path = Path(name)
        if (not path.is_absolute() or root.is_symlink() or path.parent.is_symlink() or path.is_symlink() or not path.is_file()
                or path.parent.parent != root or not re.fullmatch(r'[0-9a-f-]{36}', path.parent.name)
                or not re.fullmatch(r'[0-3]\.(jpg|jpeg|png|webp|mp4|mov|webm|pdf)', path.name)):
            return {'result': 'invalid_input', 'submitted': False}
    client = ExplicitClient(cookies, expected)
    sending = False
    try:
        await asyncio.wait_for(client.init(), 60)
        models = [m for m in client.list_models() or [] if m.is_available]
        result = {'result': 'checked', 'submitted': False, 'identity': client.identity,
                  'account_available': client.account_status == AccountStatus.AVAILABLE,
                  'available_models': len(models), 'models': model_choices(models), 'usage': None,
                  'quota_read': 'not_attempted'}
        if operation in ('check', 'renew') and result['identity']['identity_confirmed'] and result['account_available']:
            try:
                client.phase = 'loading_usage'
                await asyncio.wait_for(client._fetch_usage_info(), 20)
                result['usage'] = usage_summary(client.usage_info)
                result['quota_read'] = 'available' if result['usage'] else 'not_returned'
            except Exception as exc:
                result.update(quota_read='failed', quota_error=type(exc).__name__)
        if operation == 'renew' and result['identity']['identity_confirmed'] and result['account_available'] and models:
            client.phase = 'renewing'
            before_rotation = session_cookie_snapshot(client.guard.cookies.jar)
            try:
                response = await asyncio.wait_for(client.guard.post(
                    url=Endpoint.ROTATE_COOKIES, headers=Headers.ROTATE_COOKIES.value,
                    data='[000,"-0000000000000000000"]'), 20)
                if response.status_code in (401, 403, 429):
                    result.update(keepalive_status='denied', keepalive_phase='renewing', keepalive_reason='rejected', error_type='KeepaliveHttp' + str(response.status_code))
                elif response.status_code != 200:
                    result.update(keepalive_status='unavailable', keepalive_phase='renewing', keepalive_reason='unexpected_response')
                else:
                    after_rotation = session_cookie_snapshot(client.guard.cookies.jar)
                    response_cookies = session_cookie_snapshot(response.cookies.jar)
                    renewed = renewed_cookie_from_session(before_rotation, after_rotation)
                    if len(before_rotation) != 1 or len(after_rotation) != 1 or len(response_cookies) > 1:
                        result.update(keepalive_status='unavailable', keepalive_phase='checking_cookie', keepalive_reason='cookie_unconfirmed')
                    elif renewed is None:
                        result.update(keepalive_status='accepted', keepalive_phase='renewing', keepalive_cookie_returned=False)
                    else:
                        # The session jar can be updated even when response.cookies is empty.
                        # Send only over the private pipe; the controller rechecks the source browser.
                        result.update(keepalive_status='renewed', keepalive_phase='checking_cookie',
                                      keepalive_cookie_returned=True,
                                      _rotated_cookie={k:renewed[k] for k in ('value','domain','path','expires')})
            except Exception as exc:
                reason = ('tls_error' if type(exc).__name__ == 'SSLError' else
                          'timeout' if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or type(exc).__name__ in ('Timeout', 'ConnectTimeout', 'ReadTimeout') else 'connection_error')
                result.update(keepalive_status='unavailable', keepalive_phase='renewing', keepalive_reason=reason)
            return result
        if operation in ('check', 'renew') or not result['identity']['identity_confirmed'] or not result['account_available'] or not models:
            return result
        flash = next((m for m in models if 'flash' in (str(m.model_name or '') + ' ' + str(m.display_name or '')).lower()), None)
        if conversation and 'model' in conversation:
            flash = next((m for m in models if m.model_name == conversation['model']), None)
        if conversation is not None and not flash:
            return {'result': 'model_unavailable', 'submitted': False}
        sending = True
        client.phase = 'uploading_or_generating' if files else 'generating'
        chat = None
        if conversation is not None:
            chat = client.start_chat(model=flash, **({'metadata': conversation['metadata']} if conversation and 'metadata' in conversation else {}))
            output = await asyncio.wait_for(chat.send_message(prompt, files=files or None, temporary=False), 180)
        else:
            kwargs = {'model': flash, 'temporary': True}
            if files:
                kwargs['files'] = files
            output = await asyncio.wait_for(client.generate_content(prompt, **kwargs), 180)
        if client.guard.sent != 1 or not isinstance(output.text, str) or len(output.text) > 200000:
            return {'result': 'outcome_unknown', 'submitted': bool(client.guard.sent)}
        images, images_expected = await capture_generated_images(output, client)
        text = output.text if output.text.strip() else ('已收到 Gemini 图片结果。' if images_expected else '')
        if not text:
            return {'result': 'outcome_unknown', 'submitted': True}
        usage = None
        try:
            await asyncio.wait_for(client._fetch_usage_info(), 10)
            usage = usage_summary(client.usage_info)
        except Exception:
            pass
        result = {'result': 'completed', 'submitted': True, 'text': text, 'identity': client.identity,
                  'usage': usage, 'models': model_choices(models), '_first_text_at': client.first_text_at,
                  '_images': images, '_images_expected': images_expected}
        if chat is not None:
            metadata = chat.metadata
            if isinstance(metadata, list) and metadata and len(json.dumps(metadata, ensure_ascii=False)) <= 15000:
                result['_conversation'] = {'metadata': metadata, 'model': str(flash.model_name)}
        return result
    except Exception as exc:
        # Keep only known function names, never exception text, request URLs or files.
        frames = {frame.name for frame in traceback.extract_tb(exc.__traceback__)}
        stage = ('file_transfer' if 'upload_file' in frames else
                 'session_sync' if '_sync_activity' in frames else
                 'generation_setup' if sending else 'account_check')
        return {'result': 'outcome_unknown' if client.guard and client.guard.sent else ('upload_failed' if files and sending else 'check_failed'),
                'submitted': bool(client.guard and client.guard.sent), 'error_type': type(exc).__name__, 'phase': client.phase,
                'http_status': client.guard.generation_status if client.guard else None,
                'first_text_seen': client.first_text_at is not None,
                'error_code': str(exc) if type(exc) is ValueError and str(exc) in ('authentication_unavailable','unsafe_workbench_proxy_route','invalid_workbench_proxy_route') else None,
                'failure_stage': stage}
    finally:
        await client.close()


if __name__ == '__main__':
    supplied = {}
    try:
        supplied = json.load(sys.stdin)
        print(json.dumps(asyncio.run(main(supplied)), ensure_ascii=False), flush=True)
    except Exception:
        print(json.dumps({'result': 'internal_error', 'submitted': 'unknown' if supplied.get('operation') == 'generate' else False}), flush=True)
