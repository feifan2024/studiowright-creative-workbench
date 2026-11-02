"""Gemini-PoolDesk request core, adapted for shared-workbench execution.

Changes: accept attachment descriptors, raise the prompt envelope limit to
match the workbench, and disable all automatic preflight retries/failover.
See POOL_DESK_NOTICE.md and POOL_DESK_LICENSE for provenance and terms.
"""
import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from cryptography.fernet import Fernet


class Problem(Exception):
    def __init__(self, code, status=409, request_id=None, session_id=None):
        self.code, self.status = code, status
        self.request_id, self.session_id = request_id, session_id


class UpstreamFailure(Exception):
    def __init__(self, kind, submitted=False):
        self.kind, self.submitted = kind, submitted


class FakeUpstream:
    """Synthetic credentials only; intentionally has no HTTP client."""
    async def check(self, credential):
        await asyncio.sleep(0.01)
        scenario = credential['scenario']
        if scenario in ('invalid', 'network'):
            raise UpstreamFailure('login_invalid' if scenario == 'invalid' else 'network')

    async def generate(self, credential, messages, metadata):
        await asyncio.sleep(0.05)
        scenario = credential['scenario']
        if scenario in ('rate', 'timeout', 'crash'):
            raise UpstreamFailure({'rate': 'rate_limited', 'timeout': 'outcome_unknown', 'crash': 'upstream_error'}[scenario], True)
        return '【模拟回复，不代表 Google 生成】' + messages[-1]['content'], {'turn': metadata.get('turn', 0) + 1}


class Pool:
    mode = 'simulation'

    def model_allowed(self, model):
        return model == 'simulated-gemini'
    def __init__(self, directory, upstream=None, clock=time.time):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.owner = sqlite3.connect(directory / 'owner.sqlite', timeout=0)
        try:
            self.owner.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError:
            self.owner.close()
            raise RuntimeError('该数据库已被另一实例使用；请勿重复启动。') from None
        key = directory / 'vault.key'
        if not key.exists():
            with key.open('xb') as f:
                f.write(Fernet.generate_key())
        self.vault = Fernet(key.read_bytes())
        self.db = sqlite3.connect(directory / 'pool.sqlite', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,name TEXT NOT NULL,secret BLOB NOT NULL,fingerprint TEXT UNIQUE,
          enabled INTEGER DEFAULT 1,health TEXT DEFAULT 'unchecked',cooldown REAL DEFAULT 0,busy INTEGER DEFAULT 0,
          last_used REAL DEFAULT 0,local_calls INTEGER DEFAULT 0,last_error TEXT,checked REAL);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,account_id TEXT,model TEXT,metadata BLOB,blocked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,idem TEXT UNIQUE,digest TEXT,session_id TEXT,account_id TEXT,
          status TEXT,stage TEXT,error TEXT,created REAL,finished REAL,response BLOB);
        CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,request_id TEXT,account_id TEXT,stage TEXT,outcome TEXT);
        ''')
        # A process interruption cannot prove whether an in-flight generation completed.
        self.db.execute("UPDATE sessions SET blocked=1 WHERE id IN (SELECT session_id FROM requests WHERE status='running')")
        self.db.execute("UPDATE requests SET status='outcome_unknown',error='process_interrupted',finished=? WHERE status='running'", (clock(),))
        self.db.execute('UPDATE accounts SET busy=0')
        self.lock, self.upstream, self.clock = asyncio.Lock(), upstream or FakeUpstream(), clock

    def close(self):
        self.db.close()
        self.owner.close()

    def seal(self, value):
        return self.vault.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def unseal(self, value):
        return json.loads(self.vault.decrypt(value)) if value else {}

    def accounts(self):
        return [dict(r) | {'official_quota': None, 'cooldown_seconds': max(0, round(r['cooldown'] - self.clock()))}
                for r in self.db.execute('SELECT id,name,enabled,health,cooldown,busy,local_calls,last_error,checked FROM accounts ORDER BY rowid')]

    def import_accounts(self, entries):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise Problem('import_requires_1_to_100_accounts', 422)
        prepared, seen = [], set()
        for e in entries:
            if not isinstance(e, dict) or set(e) - {'name', 'credential', 'scenario'}:
                raise Problem('synthetic_fields_only', 422)
            name, credential, scenario = e.get('name'), e.get('credential'), e.get('scenario', 'ok')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                raise Problem('invalid_name', 422)
            if not isinstance(credential, str) or not credential.startswith('fake-') or not 8 <= len(credential) <= 200:
                raise Problem('synthetic_credentials_must_start_fake', 422)
            if scenario not in {'ok', 'invalid', 'network', 'rate', 'timeout', 'crash'}:
                raise Problem('invalid_scenario', 422)
            fp = hashlib.sha256(credential.encode()).hexdigest()
            if fp in seen or self.db.execute('SELECT 1 FROM accounts WHERE fingerprint=?', (fp,)).fetchone():
                raise Problem('duplicate_credential', 409)
            seen.add(fp)
            prepared.append((str(uuid.uuid4()), name.strip(), self.seal({'credential': credential, 'scenario': scenario}), fp))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.executemany('INSERT INTO accounts(id,name,secret,fingerprint) VALUES(?,?,?,?)', prepared)
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return {'imported': len(prepared)}

    def account(self, aid):
        r = self.db.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
        if not r:
            raise Problem('account_not_found', 404)
        return r

    async def toggle(self, aid, enabled):
        async with self.lock:
            self.account(aid)
            if type(enabled) is not bool:
                raise Problem('enabled_must_be_boolean', 422)
            self.db.execute('UPDATE accounts SET enabled=? WHERE id=?', (enabled, aid))

    def record_health(self, aid, kind):
        health = 'invalid' if kind == 'login_invalid' else 'valid' if kind is None else 'network_error' if kind == 'network' else None
        delay = 60 if kind == 'rate_limited' else 15 if kind in ('network', 'outcome_unknown', 'upstream_error') else 0
        self.db.execute('UPDATE accounts SET health=COALESCE(?,health),last_error=?,checked=?,cooldown=MAX(cooldown,?) WHERE id=?',
                        (health, kind, self.clock(), self.clock() + delay, aid))

    async def check(self, aid):
        async with self.lock:
            r = self.account(aid)
            if r['busy']:
                raise Problem('account_busy')
            self.db.execute('UPDATE accounts SET busy=1 WHERE id=?', (aid,))
        try:
            await self.upstream.check(self.unseal(r['secret']))
            self.record_health(aid, None)
        except UpstreamFailure as e:
            self.record_health(aid, e.kind)
        finally:
            self.db.execute('UPDATE accounts SET busy=0 WHERE id=?', (aid,))
        return next(x for x in self.accounts() if x['id'] == aid)

    def logs(self):
        rows = [dict(r) for r in self.db.execute('SELECT id,session_id,account_id,status,stage,error,created,finished FROM requests ORDER BY created DESC LIMIT 100')]
        for r in rows:
            r['attempts'] = [dict(a) for a in self.db.execute('SELECT account_id,stage,outcome FROM attempts WHERE request_id=? ORDER BY id', (r['id'],))]
        return rows

    async def complete(self, body, idem):
        if not isinstance(body, dict) or set(body) - {'model', 'messages', 'session_id', 'stream', 'attachments'}:
            raise Problem('unsupported_fields', 422)
        if body.get('stream', False) is not False:
            raise Problem('stream_not_supported', 422)
        if not self.model_allowed(body.get('model')):
            raise Problem('model_not_available_in_simulation' if self.mode == 'simulation' else 'check_login_and_select_model', 422)
        messages = body.get('messages')
        if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
            raise Problem('invalid_messages', 422)
        for m in messages:
            if not isinstance(m, dict) or set(m) != {'role', 'content'} or m['role'] not in ('system', 'user', 'assistant') or not isinstance(m['content'], str) or not m['content'].strip():
                raise Problem('text_messages_required', 422)
        if messages[-1]['role'] != 'user' or len(json.dumps(body)) > 260000:
            raise Problem('invalid_or_oversized_prompt', 422)
        sid = body.get('session_id')
        if sid is not None and (not isinstance(sid, str) or not 1 <= len(sid) <= 100):
            raise Problem('invalid_session_id', 422)
        if not isinstance(idem, str) or not 8 <= len(idem) <= 128:
            raise Problem('idempotency_key_required_8_to_128_chars', 422)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        async with self.lock:
            old = self.db.execute('SELECT * FROM requests WHERE idem=?', (idem,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise Problem('idempotency_key_conflict')
                if old['status'] == 'succeeded':
                    return self.unseal(old['response'])
                raise Problem('request_' + old['status'], request_id=old['id'], session_id=old['session_id'])
            if sid:
                session = self.db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
                if not session:
                    raise Problem('session_not_found', 404)
                if session['blocked']:
                    raise Problem('session_outcome_unknown')
                if session['model'] != body['model']:
                    raise Problem('session_model_mismatch')
            else:
                sid, session = str(uuid.uuid4()), None
            rid = str(uuid.uuid4())
            self.db.execute('INSERT INTO requests(id,idem,digest,session_id,status,stage,created) VALUES(?,?,?,?,?,?,?)',
                            (rid, idem, digest, sid, 'running', 'preflight', self.clock()))
        excluded = set()
        try:
            while True:
                async with self.lock:
                    if session:
                        r = self.account(session['account_id'])
                        self.db.execute('UPDATE requests SET account_id=? WHERE id=?', (r['id'], rid))
                        if not r['enabled'] or r['health'] == 'invalid' or r['busy'] or r['cooldown'] > self.clock():
                            raise Problem('pinned_account_unavailable', 503)
                    else:
                        candidates = self.db.execute("SELECT * FROM accounts WHERE enabled=1 AND health!='invalid' AND busy=0 AND cooldown<=? ORDER BY last_used,rowid", (self.clock(),)).fetchall()
                        r = next((a for a in candidates if a['id'] not in excluded), None)
                        if r is None:
                            raise Problem('no_available_accounts', 503)
                    aid = r['id']
                    self.db.execute('UPDATE accounts SET busy=1,last_used=? WHERE id=?', (self.clock(), aid))
                    self.db.execute('UPDATE requests SET account_id=? WHERE id=?', (aid, rid))
                secret = self.unseal(r['secret'])
                secret['_model'] = body['model']
                try:
                    await self.upstream.check(secret)
                    self.record_health(aid, None)
                except UpstreamFailure as e:
                    self.record_health(aid, e.kind)
                    self.db.execute('INSERT INTO attempts(request_id,account_id,stage,outcome) VALUES(?,?,?,?)', (rid, aid, 'preflight', e.kind))
                    self.db.execute('UPDATE accounts SET busy=0 WHERE id=?', (aid,))
                    excluded.add(aid)
                    # Shared-workbench policy: never retry or change account.
                    raise Problem(e.kind, 503)
                break
            async with self.lock:
                if not self.account(aid)['enabled']:
                    raise Problem('account_disabled_before_submission', 503)
                if not session:
                    self.db.execute('INSERT INTO sessions(id,account_id,model,metadata) VALUES(?,?,?,?)', (sid, aid, body['model'], self.seal({})))
                self.db.execute("UPDATE requests SET stage='submitted' WHERE id=?", (rid,))
                self.db.execute('UPDATE accounts SET local_calls=local_calls+1 WHERE id=?', (aid,))
            try:
                text, metadata = await self.upstream.generate(secret, messages, self.unseal(session['metadata']) if session else {})
            except UpstreamFailure as e:
                self.record_health(aid, e.kind)
                self.db.execute('INSERT INTO attempts(request_id,account_id,stage,outcome) VALUES(?,?,?,?)', (rid, aid, 'submitted', e.kind))
                # No retry after entering generation; all ambiguous errors block this session.
                if e.kind != 'rate_limited':
                    self.db.execute('UPDATE sessions SET blocked=1 WHERE id=?', (sid,))
                raise Problem(e.kind, 429 if e.kind == 'rate_limited' else 502)
            result = {'id': rid, 'object': 'chat.completion', 'model': body['model'], 'session_id': sid,
                      'simulation': self.mode == 'simulation', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('UPDATE sessions SET metadata=? WHERE id=?', (self.seal(metadata), sid))
                self.db.execute("UPDATE requests SET status='succeeded',finished=?,response=? WHERE id=?", (self.clock(), self.seal(result), rid))
                self.db.execute('INSERT INTO attempts(request_id,account_id,stage,outcome) VALUES(?,?,?,?)', (rid, aid, 'submitted', 'succeeded'))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            return result
        except BaseException as e:
            row = self.db.execute('SELECT stage FROM requests WHERE id=?', (rid,)).fetchone()
            kind = e.code if isinstance(e, Problem) else 'process_interrupted'
            unknown = row['stage'] == 'submitted' and kind != 'rate_limited'
            self.db.execute('UPDATE requests SET status=?,error=?,finished=? WHERE id=?', ('outcome_unknown' if unknown else 'failed', kind, self.clock(), rid))
            if unknown:
                self.db.execute('UPDATE sessions SET blocked=1 WHERE id=?', (sid,))
            if isinstance(e, Problem):
                e.request_id, e.session_id = rid, sid
            raise
        finally:
            if 'aid' in locals():
                self.db.execute('UPDATE accounts SET busy=0 WHERE id=?', (aid,))
