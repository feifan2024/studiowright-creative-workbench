"""Isolated trial adapter. Never initializes Google or generates on startup."""
import asyncio
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
os.environ['GEMINI_COOKIE_PATH'] = str(ROOT / 'cookie-cache')
app = FastAPI(title='Gemini Hanaoka 隔离试用', docs_url=None, redoc_url=None)


class AccountInput(BaseModel):
    cookie: str = Field(min_length=10, max_length=65536)
    exclusive: bool


class ChatInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str
    messages: list[dict] = Field(min_length=1, max_length=100)
    stream: bool = False


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out)


class Engine:
    def __init__(self, root=ROOT, factory=None):
        self.root = root
        self.factory = factory
        self.client = None
        self.lock = asyncio.Lock()
        self.status = '未配置账号'
        self.detail = '请使用独立试用账号，不要与旧服务同时使用同一账号。'
        self.last_generation = None

    async def close(self):
        old, self.client = self.client, None
        if old:
            await old.close()

    async def configure(self, value):
        if not value.exclusive:
            raise HTTPException(400, '请先确认这个账号不在旧服务中同时使用。')
        cookies = {}
        for part in value.cookie.split(';'):
            if '=' not in part:
                continue
            name, content = part.strip().split('=', 1)
            if name in cookies and cookies[name] != content:
                raise HTTPException(400, '同名 Cookie 存在不同值，请从单一账号重新导出。')
            cookies[name] = content
        if not cookies.get('__Secure-1PSID'):
            raise HTTPException(400, '缺少 __Secure-1PSID；需要 Cookie 字符串，不能粘贴 JSON。')
        async with self.lock:
            await self.close()
            private_write(self.root / 'account.json', cookies)
            self.status = '已保存，尚未检测'
            self.detail = '保存不代表登录有效。请点击检查登录；该步骤不生成内容。'

    async def initialize(self):
        async with self.lock:
            await self.close()
            path = self.root / 'account.json'
            if not path.exists():
                raise HTTPException(503, '尚未导入独立试用账号。')
            cookies = json.loads(path.read_text())
            if self.factory is None:
                from gemini_webapi import GeminiClient
                factory = GeminiClient
            else:
                factory = self.factory
            client = factory(cookies['__Secure-1PSID'], cookies.get('__Secure-1PSIDTS'), cookies=cookies)
            try:
                # Explicit check only, no background refresh competing with another service.
                await asyncio.wait_for(client.init(timeout=60, auto_refresh=False, auto_close=False), 75)
                client._check_account_status(raise_error=True)
                self.client = client
                self.status = '登录检查通过'
                self.detail = '只验证登录，不代表真实生成成功；没有后台自动保活。'
            except Exception as exc:
                await client.close()
                self.status = '登录检查失败'
                self.detail = '登录初始化失败（%s）。请检查 Cookie 和服务主机网络。' % type(exc).__name__
                raise HTTPException(502, self.detail)

    def models(self):
        if self.client is None:
            raise HTTPException(503, '请先检查账号登录，再读取模型列表。')
        return [m for m in self.client.list_models() or [] if m.is_available]

    async def generate(self, value):
        if value.stream:
            raise HTTPException(400, '试用版目前仅支持 stream=false；不伪装成流式响应。')
        lines = []
        for msg in value.messages:
            if set(msg) - {'role', 'content'} or msg.get('role') not in ('system', 'user', 'assistant') or not isinstance(msg.get('content'), str):
                raise HTTPException(400, '试用版仅支持纯文本 system/user/assistant 消息。附件与工具调用尚未适配。')
            lines.append('%s:\n%s' % (msg['role'], msg['content']))
        if value.messages[-1].get('role') != 'user':
            raise HTTPException(400, '最后一条消息必须为 user。')
        prompt = '\n\n'.join(lines)
        if len(prompt.encode()) > 524288:
            raise HTTPException(413, '试用版对话超过 512 KiB。')
        if self.lock.locked():
            raise HTTPException(429, '试用账号正在处理请求；本适配层不会排队或自动重发。')
        async with self.lock:
            selected = next((m for m in self.models() if value.model in (m.model_name, m.model_id)), None)
            if selected is None:
                raise HTTPException(400, '模型不在该账号返回的可用列表中。')
            begin = time.monotonic()
            try:
                result = await asyncio.wait_for(self.client.generate_content(prompt, model=selected), 600)
                text = result.text
                if not isinstance(text, str) or not text.strip():
                    raise ValueError('empty_text')
                self.last_generation = {'ok': True, 'seconds': round(time.monotonic()-begin, 2)}
                return {'id': 'chatcmpl-'+uuid.uuid4().hex, 'object': 'chat.completion', 'created': int(time.time()), 'model': selected.model_name,
                        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}
            except Exception as exc:
                timed_out = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or 'Timeout' in type(exc).__name__
                self.last_generation = {'ok': False, 'seconds': round(time.monotonic()-begin, 2), 'error_type': type(exc).__name__}
                if 'Auth' in type(exc).__name__:
                    self.status = '登录异常，需要重新检测'
                    await self.close()
                raise HTTPException(504 if timed_out else 502, '请求失败（%s），执行结果未确认。未由适配层重发，请勿盲目重复生成。' % type(exc).__name__)


engine = Engine()


def authenticate(header):
    expected = json.loads((ROOT / 'access.json').read_text())['api_key']
    supplied = header.removeprefix('Bearer ') if header else ''
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(401, '请填写试用服务 API 密钥。')


@app.get('/', response_class=HTMLResponse)
def home():
    return (ROOT / 'index.html').read_text(encoding='utf-8')


@app.get('/health')
def health():
    return {'status': 'ok', 'project': 'HanaokaYuzu/Gemini-API', 'version': '2.1.1', 'generation_on_startup': False}


@app.middleware('http')
async def protect(request: Request, call_next):
    if int(request.headers.get('content-length', '0')) > 4194304:
        from fastapi.responses import JSONResponse
        return JSONResponse({'error': {'message': '请求超过试用版 4 MiB 限制。'}}, status_code=413)
    if request.url.path not in ('/', '/health', '/openapi.json'):
        try:
            authenticate(request.headers.get('authorization'))
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse({'error': {'message': exc.detail}}, status_code=exc.status_code)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/admin/status')
def status():
    return {'status': engine.status, 'detail': engine.detail, 'last_generation': engine.last_generation, 'busy': engine.lock.locked()}


@app.post('/admin/account')
async def account(value: AccountInput):
    await engine.configure(value)
    return status()


@app.post('/admin/check')
async def check():
    await engine.initialize()
    return status()


@app.get('/v1/models')
def models():
    return {'object': 'list', 'data': [{'id': m.model_name, 'object': 'model', 'owned_by': 'gemini-web'} for m in engine.models()]}


@app.post('/v1/chat/completions')
async def chat(value: ChatInput):
    return await engine.generate(value)
