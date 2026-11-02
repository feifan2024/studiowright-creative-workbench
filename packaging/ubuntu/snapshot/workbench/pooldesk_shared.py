"""PoolDesk request flow adapted to the workbench's one shared account pool.

Based on Gemini-PoolDesk pool.py, Pool.complete (AGPL-3.0); see the preserved
source and notices. Uses the existing account/session/request storage and
provider adapter. It intentionally removes all automatic retry and failover.
The legacy standalone PoolDesk server/database are not involved.
"""
import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pooldesk_original import Pool, Problem, UpstreamFailure
from live_pool import Conflict, NETWORK_ERRORS, UPLOAD_TYPES, clean_model_choices, clean_usage


class SingleAccountUpstream:
    """The provider adapter for exactly the identity already reserved above."""
    def __init__(self, workbench, account, files, native, is_conversation):
        self.workbench, self.account, self.files = workbench, account, files
        self.native, self.is_conversation, self.result = native, is_conversation, None

    async def check(self, pointer):
        # The workbench transaction validated this selected identity. The real
        # browser identity and login are checked in the provider before sending.
        if pointer.get('account_key') != self.account:
            raise UpstreamFailure('login_invalid', False)

    async def generate(self, pointer, messages, metadata):
        if pointer.get('account_key') != self.account:
            raise UpstreamFailure('upstream_error', False)
        prompt = messages[-1]['content']
        try:
            if self.is_conversation:
                self.result = self.workbench.execute('generate', self.account, prompt, self.files, self.native)
            else:
                self.result = (self.workbench.execute('generate', self.account, prompt, self.files) if self.files
                               else self.workbench.execute('generate', self.account, prompt))
        except Exception:
            self.result = {'result': 'outcome_unknown', 'submitted': 'unknown'}
        if not isinstance(self.result, dict):
            self.result = {'result': 'outcome_unknown', 'submitted': 'unknown'}
        identity = self.result.get('identity', {})
        text = self.result.get('text')
        if (self.result.get('result') == 'completed' and self.result.get('submitted') is True
                and isinstance(identity, dict) and identity.get('identity_confirmed') is True
                and isinstance(text, str) and text.strip() and len(text) <= 200000):
            return text, {}
        raise UpstreamFailure('outcome_unknown' if self.result.get('submitted') is not False else 'upstream_error',
                              self.result.get('submitted') is True)


class SingleAccountPoolDesk(Pool):
    """Run original Pool.complete with one reserved identity and no disk vault.

    The transient database is execution scratch space, not an account source
    or request ledger. The workbench persists the authoritative state.
    """
    mode = 'shared-live'

    def __init__(self, workbench, account, files, native, is_conversation, selected_model):
        self.clock = time.time
        self.lock = asyncio.Lock()
        self.upstream = SingleAccountUpstream(workbench, account, files, native, is_conversation)
        self.selected_model = selected_model
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE accounts(id TEXT PRIMARY KEY,name TEXT NOT NULL,secret BLOB NOT NULL,fingerprint TEXT UNIQUE,
              enabled INTEGER DEFAULT 1,health TEXT DEFAULT 'unchecked',cooldown REAL DEFAULT 0,busy INTEGER DEFAULT 0,
              last_used REAL DEFAULT 0,local_calls INTEGER DEFAULT 0,last_error TEXT,checked REAL);
            CREATE TABLE sessions(id TEXT PRIMARY KEY,account_id TEXT,model TEXT,metadata BLOB,blocked INTEGER DEFAULT 0);
            CREATE TABLE requests(id TEXT PRIMARY KEY,idem TEXT UNIQUE,digest TEXT,session_id TEXT,account_id TEXT,
              status TEXT,stage TEXT,error TEXT,created REAL,finished REAL,response BLOB);
            CREATE TABLE attempts(id INTEGER PRIMARY KEY,request_id TEXT,account_id TEXT,stage TEXT,outcome TEXT);
        ''')
        self.db.execute('INSERT INTO accounts(id,name,secret,health) VALUES(?,?,?,?)',
                        (account, 'selected shared account', self.seal({'account_key': account}), 'valid'))

    def model_allowed(self, model):
        return model == self.selected_model

    def seal(self, value):
        # This only serializes an account pointer and the transient response;
        # no browser cookies or credentials enter this scratch database.
        return json.dumps(value, ensure_ascii=False).encode('utf-8')

    def unseal(self, value):
        return json.loads(value)

    def execute_once(self, prompt, request_id, attachments):
        body = {'model': self.selected_model, 'messages': [{'role': 'user', 'content': prompt}],
                'attachments': attachments}
        try:
            asyncio.run(super().complete(body, request_id))
        except Problem:
            if self.upstream.result is None:
                self.upstream.result = {'result': 'check_failed', 'submitted': False}
        finally:
            self.db.close()
        return self.upstream.result or {'result': 'outcome_unknown', 'submitted': 'unknown'}


class SharedPoolDesk(Pool):
    """PoolDesk request admission, binding, one attempt and finalization."""
    mode = 'shared-workbench'

    def __init__(self, workbench):
        # Do not initialize Pool's separate database or credential vault: the
        # existing workbench ledger and Flow-owned account identities are used.
        self.workbench = workbench

    def __getattr__(self, name):
        return getattr(self.workbench, name)

    def check(self, account, items, owner_request_id=None):
        # Pool's standalone check assumes a second encrypted credential store.
        # Use the shared account controller for the selected account instead.
        return self.workbench.check(account, items, owner_request_id=owner_request_id)

    @staticmethod
    def validate_messages(messages):
        # Ported from Pool.complete: this is the actual admission gate. The
        # shared adapter carries file descriptors separately from the messages.
        if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
            raise ValueError('消息格式不正确。')
        for m in messages:
            if (not isinstance(m, dict) or set(m) != {'role', 'content'}
                    or m['role'] not in ('system', 'user', 'assistant')
                    or not isinstance(m['content'], str) or not m['content'].strip()):
                raise ValueError('请输入文本消息。')
        if messages[-1]['role'] != 'user':
            raise ValueError('最后一条消息必须来自用户。')

    @staticmethod
    def reserve_account(db, account):
        # PoolDesk reserves the chosen identity before provider processing.
        # The shared requests table is the lock, so there is no second pool.
        busy = db.execute("SELECT 1 FROM requests WHERE account_key=? AND status='dispatching' LIMIT 1", (account,)).fetchone()
        if busy:
            raise Conflict('该账号正在处理另一条请求，请稍后操作。')
        blocked = db.execute("""SELECT 1 FROM requests r WHERE r.account_key=? AND r.status='unknown'
                                AND NOT EXISTS(SELECT 1 FROM account_unknown_release x WHERE x.request_id=r.request_id) LIMIT 1""", (account,)).fetchone()
        if blocked:
            raise Conflict('该账号有对话结果未确认，请先核对。')

    def complete(self, account, prompt, request_id, items, attachments=None, conversation_id=None, model=None):
        self.validate_messages([{'role': 'user', 'content': prompt}])
        auto = account == 'auto'
        requested_auto = auto
        if conversation_id is not None:
            self.conversation_id(conversation_id)
        if model is not None and (not conversation_id or not isinstance(model, str) or not model.strip()
                                  or len(model) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in model)):
            raise ValueError('所选模型不正确。')
        if not auto:
            self.allowed(account, items)
        try:
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('请求标识不正确。') from None
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
            raise ValueError('请输入不超过 20000 字的内容。')
        attachments = [] if attachments is None else attachments
        if (not isinstance(attachments, list) or len(attachments) > 4
                or any(not isinstance(item, dict) or set(item) != {'index', 'extension', 'sha256'}
                       or type(item['index']) is not int or not 0 <= item['index'] < 4
                       or not isinstance(item['extension'], str) or item['extension'] not in UPLOAD_TYPES or not isinstance(item['sha256'], str)
                       or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']) for item in attachments)
                or len({item['index'] for item in attachments}) != len(attachments)):
            raise ValueError('附件信息不正确。')
        attachments = sorted(attachments, key=lambda item: item['index'])
        digest_data = [prompt, attachments, model] if model else [prompt, attachments]
        digest = (hashlib.sha256(json.dumps(digest_data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                  if attachments or model else hashlib.sha256(prompt.encode('utf-8')).hexdigest())
        refresh_needed = False
        auto_policy = None
        browser_states = self._read_browser_inventory() if auto else None
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                turn = db.execute('SELECT conversation_id FROM conversation_turns WHERE request_id=?', (request_id,)).fetchone()
                if (turn['conversation_id'] if turn else None) != conversation_id:
                    db.execute('ROLLBACK')
                    raise Conflict('该请求编号已用于其他对话。')
                same_account = row['selection'] == 'auto' if requested_auto else row['selection'] == 'manual' and row['account_key'] == account
                if not same_account or row['prompt_digest'] != digest:
                    db.execute('ROLLBACK')
                    raise Conflict('该请求编号已用于其他内容。')
                db.execute('COMMIT')
                return self.get(request_id), False
            conversation = None
            if conversation_id:
                conversation = db.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
                if not conversation or conversation['blocked']:
                    db.execute('ROLLBACK')
                    raise Conflict('对话不存在或需要先核对上一条结果。')
                pending = db.execute("""SELECT r.status FROM conversation_turns t JOIN requests r ON r.request_id=t.request_id
                                        WHERE t.conversation_id=? AND r.status IN ('dispatching','unknown') LIMIT 1""", (conversation_id,)).fetchone()
                if pending:
                    db.execute('ROLLBACK')
                    raise Conflict('上一条消息仍在处理或结果未确认，请勿重复发送。')
                if conversation['native_json'] and model:
                    try:
                        existing = json.loads(conversation['native_json'])
                    except (TypeError, ValueError):
                        db.execute('ROLLBACK')
                        raise Conflict('对话上下文需要先核对。') from None
                    if not isinstance(existing, dict) or not isinstance(existing.get('model'), str):
                        db.execute('ROLLBACK')
                        raise Conflict('对话上下文需要先核对。')
                if conversation['account_key']:
                    if not auto and account != conversation['account_key']:
                        db.execute('ROLLBACK')
                        raise Conflict('该对话需要继续使用原账号。')
                    account = conversation['account_key']
                    self.allowed(account, items)
                    auto = False
            if auto:
                if self.browser_inventory is not None and browser_states is None:
                    db.execute('ROLLBACK')
                    raise Conflict('无法核对现有账号的浏览器状态，本次没有发送。')
                # Capture the policy at selection time; changing it affects new requests.
                auto_policy = self.dispatch_policy(db)
                try:
                    account = self.choose_account(db, items, time.time(), model=model, policy=auto_policy, browser_states=browser_states)
                except ValueError:
                    try:
                        # Only previously verified accounts may be refreshed automatically.
                        # Pick once; a failed check does not cause account switching.
                        account = self.choose_account(db, items, time.time(), stale=True, model=model, policy=auto_policy, browser_states=browser_states)
                        refresh_needed = True
                    except ValueError:
                        db.execute('ROLLBACK')
                        raise
            record = db.execute('''SELECT a.gemini_status, a.models_json, m.status AS monitor_status,
                                          COALESCE(p.gemini_enabled,1) AS gemini_enabled FROM account_status a
                                   LEFT JOIN account_monitor m ON m.account_key=a.account_key
                                   LEFT JOIN account_preferences p ON p.account_key=a.account_key WHERE a.account_key=?''', (account,)).fetchone()
            if record and not record['gemini_enabled']:
                db.execute('ROLLBACK')
                raise Conflict('该账号的 Gemini 使用已暂停，请先在账号页启用。')
            if not record or record['gemini_status'] != 'verified' or record['monitor_status'] in ('paused', 'network_wait'):
                db.execute('ROLLBACK')
                raise Conflict('请先核对该账号的 Gemini 登录。')
            if model and not (conversation and conversation['native_json'] and existing.get('model') == model):
                try:
                    choices = clean_model_choices(json.loads(record['models_json'])) if record['models_json'] else []
                except (TypeError, ValueError):
                    choices = []
                if not any(choice['id'] == model for choice in choices):
                    db.execute('ROLLBACK')
                    raise Conflict('该账号尚未确认支持所选模型，请先核对 Gemini 登录。')
            try:
                files = self.upload_files(request_id, attachments) if attachments else []
            except ValueError:
                db.execute('ROLLBACK')
                raise
            try:
                self.reserve_account(db, account)
            except Conflict:
                db.execute('ROLLBACK')
                raise
            db.execute("INSERT INTO requests(request_id,account_key,prompt_digest,status,created,selection) VALUES(?,?,?,'dispatching',?,?)",
                       (request_id, account, digest, time.time(), 'auto' if requested_auto else 'manual'))
            # PoolDesk's per-request attempt is in the same transaction as the
            # workbench's authoritative request. Historic requests are never backfilled.
            db.execute("INSERT INTO pooldesk_attempts(request_id,account_key,stage,outcome,engine_version) VALUES(?,?,'preflight',NULL,'core-v2')",
                       (request_id, account))
            native = None
            if conversation_id:
                native = json.loads(conversation['native_json']) if conversation['native_json'] else {}
                if model:
                    native['model'] = model
                db.execute('UPDATE conversations SET account_key=?,updated=? WHERE id=?', (account, time.time(), conversation_id))
                db.execute('INSERT INTO conversation_turns VALUES(?,?,?,?)',
                           (request_id, conversation_id, prompt, json.dumps([a['extension'] for a in attachments])))
                if model:
                    db.execute('INSERT INTO conversation_attempt_models(request_id,model) VALUES(?,?)', (request_id, model))
            db.execute('COMMIT')
        if refresh_needed:
            result = {}
            try:
                result = self.check(account, items, owner_request_id=request_id)
                with self.connection() as db:
                    refreshed = db.execute('''SELECT a.gemini_status,a.quota_json,a.quota_checked,a.models_json,
                                                     COALESCE(p.gemini_enabled,1) AS gemini_enabled,
                                                     m.status AS monitor_status,
                                                     EXISTS(SELECT 1 FROM requests r WHERE r.request_id=? AND r.account_key=a.account_key
                                                            AND r.status='dispatching' AND r.selection='auto') AS owned
                                              FROM account_status a LEFT JOIN account_preferences p ON p.account_key=a.account_key
                                              LEFT JOIN account_monitor m ON m.account_key=a.account_key WHERE a.account_key=?''',
                                           (request_id, account)).fetchone()
                usage = clean_usage(json.loads(refreshed['quota_json'])) if refreshed and refreshed['quota_json'] else None
                quota_time = refreshed['quota_checked'] if refreshed else None
                choices = clean_model_choices(json.loads(refreshed['models_json'])) if model and refreshed and refreshed['models_json'] else []
                safe = (result.get('verified') is True and refreshed['gemini_status'] == 'verified'
                        and refreshed['owned'] and refreshed['gemini_enabled'] and refreshed['monitor_status'] == 'ready'
                        and (not model or any(choice['id'] == model for choice in choices))
                        and self.usage_dispatch_state(usage, quota_time, time.time(), auto_policy) == 'eligible')
            except Exception:
                safe = False
            if not safe:
                note = result.get('message') if isinstance(result, dict) and result.get('reason') in ('tls_error', 'connection_error', 'account_busy') else '额度或登录未确认，未提交 Gemini 请求'
                with self.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE requests SET status='failed',finished=?,note=? WHERE request_id=? AND status='dispatching'",
                               (time.time(), note, request_id))
                    db.execute("UPDATE pooldesk_attempts SET stage='preflight',outcome='failed' WHERE request_id=?", (request_id,))
                    db.execute('COMMIT')
                self.clear_uploads(request_id, attachments)
                return self.get(request_id), True
        with self.connection() as db:
            db.execute("UPDATE pooldesk_attempts SET stage='processing' WHERE request_id=?", (request_id,))
        try:
            runtime = SingleAccountPoolDesk(self.workbench, account, files, native, bool(conversation_id), model or 'shared-default')
            result = runtime.execute_once(prompt, request_id, attachments)
            if not isinstance(result, dict):
                raise ValueError('invalid_result')
        except Exception:
            result = {'result': 'outcome_unknown', 'submitted': 'unknown'}
        finally:
            self.clear_uploads(request_id, attachments)
        identity = result.get('identity', {})
        response = result.get('text')
        completed = (result.get('result') == 'completed' and result.get('submitted') is True
                     and isinstance(identity, dict) and identity.get('identity_confirmed') is True
                     and isinstance(response, str) and bool(response.strip()) and len(response) <= 200000)
        if completed:
            status, note = 'completed', '已收到 Gemini 回复'
        elif result.get('submitted') is False:
            reason = result.get('result')
            notes = {
                'account_busy': '该账号正在被 Flow 使用，本次没有发送；请稍后操作。',
                'no_confirmed_running_account': '无法确认账号对应的浏览器，本次没有发送。',
                'flow_identity_unavailable': '无法确认 Flow 登录身份，本次没有发送。',
                'gemini_session_unavailable': '该账号的 Gemini 登录未确认，本次没有发送。',
                'runtime_unavailable': 'Gemini 接入暂不可用，本次没有发送。',
                'gemini_runtime_unavailable': 'Gemini 接入暂不可用，本次没有发送。',
                'unsupported_runtime': 'Gemini 接入版本不匹配，本次没有发送。',
                'invalid_input': '发送内容或附件格式不正确，本次没有发送。',
                'upload_failed': '附件上传或连接失败，本次没有发送 Gemini 问题。',
                'check_failed': '连接或登录核对失败，本次没有发送。',
                'checked': 'Gemini 登录、模型或账号状态未确认，本次没有发送。',
                'model_unavailable': '当前账号暂不支持所选模型，本次没有发送。',
            }
            note = notes.get(reason, '发送前的核对未完成，本次没有发送。')
            if reason == 'upload_failed':
                stage, error = result.get('failure_stage'), result.get('error_type')
                if error in NETWORK_ERRORS:
                    prefix = '附件传输' if stage == 'file_transfer' else '发送前连接'
                    note = prefix + ('安全连接失败' if error == 'SSLError' else '连接失败或超时') + '，本次没有发送 Gemini 问题。'
                elif error in ('ValidationError', 'TypeError', 'AttributeError', 'AssertionError'):
                    note = '附件接入发生内部错误，本次没有发送 Gemini 问题。'
            elif reason == 'check_failed':
                error = result.get('error_type')
                if error in NETWORK_ERRORS:
                    note = ('Gemini 安全连接失败' if error == 'SSLError' else 'Gemini 连接失败或超时') + '，账号状态未改变，本次没有发送。'
            status, response = 'failed', None
        else:
            status, note, response = 'unknown', '结果未确认，请先核对，不会自动重新发送', None
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE requests SET status=?,finished=?,response=?,note=? WHERE request_id=? AND status='dispatching'",
                       (status, time.time(), response, note, request_id))
            if not completed:
                safe = lambda value: value if isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', value) else None
                http = result.get('http_status')
                http = http if type(http) is int and 100 <= http <= 599 else None
                seen = result.get('first_text_seen')
                try:
                    db.execute('INSERT OR REPLACE INTO request_diagnostics VALUES(?,?,?,?,?,?)',
                               (request_id, safe(result.get('phase')), safe(result.get('failure_stage')),
                                safe(result.get('error_type')), http, int(seen) if type(seen) is bool else None))
                except sqlite3.Error:
                    pass
            if completed:
                row=db.execute('SELECT created,finished FROM requests WHERE request_id=?',(request_id,)).fetchone()
                first=result.get('_first_text_at')
                first_ms=((first-row['created'])*1000 if type(first) in (int,float) and
                          row['created'] <= first <= row['finished'] else None)
                total_ms=(row['finished']-row['created'])*1000 if row['finished']>=row['created'] else None
                try:
                    db.execute('INSERT OR REPLACE INTO request_metrics(request_id,first_text_ms,total_ms,token_count) VALUES(?,?,?,NULL)',
                               (request_id,first_ms,total_ms))
                except sqlite3.Error:
                    pass  # Optional statistics must never discard a completed reply.
                image_dir = self.state_dir/'gemini-generated-images'
                saved = 0
                expected = result.get('_images_expected')
                images = result.get('_images')
                if type(expected) is int and 0 <= expected <= 4 and isinstance(images, list) and len(images) <= expected:
                    for item in images:
                        if not isinstance(item,dict):
                            continue
                        ident,mime,size,digest = item.get('id'),item.get('mime'),item.get('size'),item.get('sha256')
                        if (not isinstance(ident,str) or not re.fullmatch('[0-9a-f]{32}',ident)
                                or mime not in ('image/jpeg','image/png','image/webp') or type(size) is not int
                                or not 0 < size <= 20*1024*1024 or not isinstance(digest,str)
                                or not re.fullmatch('[0-9a-f]{64}',digest)):
                            continue
                        path = image_dir/(ident+'.image')
                        try:
                            if image_dir.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                                continue
                            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                                continue
                            db.execute('INSERT INTO gemini_images VALUES(?,?,?,?,?,?)',
                                       (request_id,saved,ident,mime,size,digest))
                            saved += 1
                        except (OSError,sqlite3.Error):
                            continue
                    if expected:
                        try:
                            db.execute('INSERT OR REPLACE INTO gemini_image_status VALUES(?,?,?)',(request_id,expected,saved))
                        except sqlite3.Error:
                            pass
            if conversation_id:
                context = result.get('_conversation') if completed else None
                valid_context = (isinstance(context, dict) and set(context) == {'metadata', 'model'}
                                 and isinstance(context['metadata'], list) and bool(context['metadata'])
                                 and isinstance(context['model'], str) and 0 < len(context['model']) <= 200
                                 and len(json.dumps(context, ensure_ascii=False)) <= 16000)
                if completed and valid_context:
                    db.execute('UPDATE conversations SET native_json=?,updated=? WHERE id=?',
                               (json.dumps(context, ensure_ascii=False), time.time(), conversation_id))
                elif completed:
                    db.execute("UPDATE conversations SET blocked='无法确认对话上下文，请新建对话。',updated=? WHERE id=?", (time.time(), conversation_id))
                elif status == 'unknown':
                    db.execute("UPDATE conversations SET blocked='上一条消息结果未确认，请先核对。',updated=? WHERE id=?", (time.time(), conversation_id))
            if (not completed and result.get('result') == 'check_failed' and result.get('submitted') is False
                    and result.get('error_type') in NETWORK_ERRORS):
                db.execute("UPDATE account_monitor SET status='paused',next_due=NULL,last_note=? WHERE account_key=? AND status!='dispatching'",
                           (('安全连接失败，账号状态未改变' if result.get('error_type') == 'SSLError' else '连接失败，账号状态未改变') + '；请手动核对', account))
            if completed:
                usage = clean_usage(result.get('usage'))
                checked = time.time()
                db.execute("UPDATE account_status SET gemini_status='verified',checked=?,last_note=NULL,quota_json=?,quota_checked=? WHERE account_key=?",
                           (checked, json.dumps(usage) if usage else None, checked if usage else None, account))
                if isinstance(result.get('models'), list):
                    db.execute('UPDATE account_status SET models_json=? WHERE account_key=?',
                               (json.dumps(clean_model_choices(result['models']), ensure_ascii=False), account))
            stage = ('submitted' if result.get('submitted') is True else
                     'preflight' if result.get('submitted') is False else 'uncertain')
            db.execute('UPDATE pooldesk_attempts SET stage=?,outcome=? WHERE request_id=?',
                       (stage, status, request_id))
            db.execute('COMMIT')
        return self.get(request_id), True
