"""Durable FIFO jobs, round-robin account assignment, and pinned conversations."""
import hashlib,json,sqlite3,threading,time,uuid
from pathlib import Path
from official_runtime import OfficialRuntime,MODEL,CHANNEL

# Ownership is explicit: queued official jobs or a saved native official conversation.
# Titles, model names and old monitor state never determine channel ownership.
OFFICIAL_REQUEST_SQL = """(EXISTS(SELECT 1 FROM official_jobs j WHERE j.request_id=r.request_id)
    OR EXISTS(SELECT 1 FROM conversation_turns t JOIN conversations c ON c.id=t.conversation_id
        WHERE t.request_id=r.request_id AND CASE WHEN json_valid(c.native_json)
        THEN (json_extract(c.native_json,'$.channel')='official_antigravity_cli'
              OR json_extract(c.native_json,'$.official_cli_trial')=1) ELSE 0 END))"""

# Quota lookup validation is advisory; only actual dialogue restrictions block dispatch.
QUOTA_CHECK_ONLY_BLOCKS=('activation_verification_required','official_authorization_verification_required')

def unresolved_official(db,key):
    return db.execute("SELECT 1 FROM requests r WHERE r.account_key=? AND r.status='unknown' AND "+OFFICIAL_REQUEST_SQL+" AND NOT EXISTS(SELECT 1 FROM account_unknown_release x WHERE x.request_id=r.request_id)",(key,)).fetchone() is not None

def busy_official(db,key):
    return db.execute("SELECT 1 FROM requests r WHERE r.account_key=? AND r.status IN ('queued','dispatching') AND "+OFFICIAL_REQUEST_SQL,(key,)).fetchone() is not None

class OfficialPool:
    def __init__(self,pool,runtime=None):
        self.pool=pool;self.runtime=runtime or OfficialRuntime(pool.state_dir/'official-runtime.json')
        self.stop=threading.Event();self.wake=threading.Event();self.threads=[]
        with pool.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS official_jobs(
                request_id TEXT PRIMARY KEY REFERENCES requests(request_id),conversation_id TEXT NOT NULL,
                model TEXT NOT NULL,native_before TEXT,started REAL,queue_wait_ms REAL,reason TEXT);
              CREATE TABLE IF NOT EXISTS official_pool_cursor(id INTEGER PRIMARY KEY CHECK(id=1),last_account TEXT);
              CREATE TABLE IF NOT EXISTS official_account_blocks(account_key TEXT PRIMARY KEY,reason TEXT NOT NULL,created REAL NOT NULL);
            ''')
            # Interrupted submitted jobs stay unknown; queued jobs were never sent and are not replayed after restart.
            db.execute("UPDATE requests SET status='failed',finished=?,note='工作台重启前尚未发送；不会自动恢复提交。' WHERE status='queued' AND request_id IN (SELECT request_id FROM official_jobs)",(time.time(),))
        self.ready=any(self.runtime.inventory().values())

    def start(self):
        if not self.ready or self.threads:return
        for i in range(5):
            t=threading.Thread(target=self.worker,name='official-worker-'+str(i),daemon=True);self.threads.append(t);t.start()

    def close(self):
        self.stop.set();self.wake.set()
        # Never close the pool DB lock while a provider call can still finish writing.
        for t in self.threads:t.join(timeout=190)

    def reason(self,db,key,available):
        if not available.get(key):return 'authorization_not_ready'
        pref=db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?',(key,)).fetchone()
        if pref and pref[0]==0:return 'dispatch_paused'
        block=db.execute('SELECT reason FROM official_account_blocks WHERE account_key=?',(key,)).fetchone()
        if block and block[0] not in QUOTA_CHECK_ONLY_BLOCKS:return 'official_call_blocked'
        if unresolved_official(db,key):return 'unresolved_request'
        return None

    def public_status(self):
        available=self.runtime.inventory()
        with self.pool.connection() as db:
            result={key:{'authorization':'saved_verified_identity' if ready else 'not_ready',
                         'dispatch_eligible':self.reason(db,key,available) is None,
                         'restriction':self.reason(db,key,available)} for key,ready in available.items()}
            queued=db.execute("SELECT count(*) FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status='queued'").fetchone()[0]
            active=db.execute("SELECT count(*) FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status='dispatching'").fetchone()[0]
        return {'available':any(available.values()),'channel':CHANNEL,'model':MODEL,'accounts':result,'queued':queued,'active':active,
                'selection':'round_robin','max_concurrency':5,'retry':False,'failover':False}

    def generate(self,account,prompt,request_id,items,attachments=None,conversation_id=None,model=None):
        from live_pool import Conflict
        if not self.ready:raise Conflict('官方调用尚未配置完成，未发送。')
        self.pool.conversation_id(request_id)
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>20000:raise ValueError('请输入1到20000字的消息。')
        if attachments:raise ValueError('当前官方通道仅接入文字对话；附件未发送，原文件保留。')
        if model not in (None,'',MODEL):raise Conflict('当前官方通道使用固定模型，不能自动切换模型。')
        if not isinstance(account,str):raise ValueError('请选择账号或自动轮询。')
        if account!='auto':self.pool.allowed(account,items)
        if conversation_id:self.pool.conversation_id(conversation_id)
        digest=hashlib.sha256(json.dumps({'account':account,'prompt':prompt,'conversation_id':conversation_id,'model':model or MODEL},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        now=time.time();available=self.runtime.inventory()
        selection='auto' if account=='auto' else 'manual'
        permitted={i.get('key','').removeprefix('flow-account-') for i in items if isinstance(i,dict)}
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                prior=db.execute('SELECT prompt_digest FROM requests WHERE request_id=?',(request_id,)).fetchone()
                if prior:
                    if prior[0]!=digest or not db.execute('SELECT 1 FROM official_jobs WHERE request_id=?',(request_id,)).fetchone():raise Conflict('该请求编号已经使用；不会重新提交。')
                    db.execute('COMMIT');return self.pool.get(request_id),False
                if self.pool.maintenance():raise Conflict('工作台正在更新，消息没有发送。')
                if db.execute("SELECT count(*) FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status IN ('queued','dispatching')").fetchone()[0]>=100:raise Conflict('等待队列已满，消息未发送。')
                conv=db.execute('SELECT * FROM conversations WHERE id=?',(conversation_id,)).fetchone() if conversation_id else None
                if conversation_id and (not conv or conv['hidden']):raise Conflict('请选择一个未隐藏的对话。')
                native=None
                if conv:
                    if conv['blocked']:raise Conflict('该对话已归档或有待核对的结果；请保留记录并新建对话。')
                    data=json.loads(conv['native_json'] or '{}')
                    if conv['account_key']:
                        if data.get('channel')!=CHANNEL:raise Conflict('旧通道对话保留为历史记录，请新建官方对话。')
                        if account not in ('auto',conv['account_key']):raise Conflict('多轮对话必须使用最初的账号，不能换号。')
                        account=conv['account_key'];native=data.get('official_conversation_id')
                        selection='manual'
                        if not native:raise Conflict('原生上下文尚未确认，不能重新提交。')
                    if db.execute("SELECT 1 FROM conversation_turns t JOIN requests r USING(request_id) WHERE t.conversation_id=? AND r.status IN ('queued','dispatching')",(conversation_id,)).fetchone():raise Conflict('该对话还有一轮正在等待或执行，请等待完成。')
                eligible=sorted(k for k in available if k in permitted and self.reason(db,k,available) is None)
                if account=='auto':
                    if not eligible:raise Conflict('没有允许派发且授权就绪的账号；暂停和未确认限制均保留。')
                    row=db.execute('SELECT last_account FROM official_pool_cursor WHERE id=1').fetchone();last=row[0] if row else ''
                    account=next((k for k in eligible if k>last),eligible[0])
                    db.execute('INSERT INTO official_pool_cursor(id,last_account) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET last_account=excluded.last_account',(account,))
                elif account not in eligible:raise Conflict('该账号授权未就绪、已暂停或存在未确认结果，消息未发送。')
                if not conv:
                    conversation_id=str(uuid.uuid4())
                    db.execute('INSERT INTO conversations(id,title,created,updated,hidden) VALUES(?,?,?,?,0)',(conversation_id,prompt.strip()[:36] or '新对话',now,now))
                data={'channel':CHANNEL,'model':MODEL,'official_conversation_id':native}
                db.execute('UPDATE conversations SET account_key=?,native_json=?,updated=? WHERE id=?',(account,json.dumps(data),now,conversation_id))
                db.execute("INSERT INTO requests(request_id,account_key,prompt_digest,status,created,note,selection) VALUES(?,?,?,'queued',?,?,?)",(request_id,account,digest,now,'已进入官方调用队列；固定账号，不自动重试或换号。',selection))
                db.execute('INSERT INTO conversation_turns VALUES(?,?,?,?)',(request_id,conversation_id,prompt,'[]'))
                db.execute('INSERT INTO conversation_attempt_models(request_id,model) VALUES(?,?)',(request_id,MODEL))
                db.execute('INSERT INTO official_jobs(request_id,conversation_id,model,native_before) VALUES(?,?,?,?)',(request_id,conversation_id,MODEL,native))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
        self.wake.set()
        return self.pool.get(request_id),True

    def claim(self):
        available=self.runtime.inventory()
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if self.pool.maintenance():db.execute('COMMIT');return None
                rows=db.execute("SELECT r.request_id,r.account_key,r.created,j.conversation_id,j.native_before,t.prompt FROM requests r JOIN official_jobs j USING(request_id) JOIN conversation_turns t USING(request_id) WHERE r.status='queued' ORDER BY r.created,r.rowid").fetchall()
                for row in rows:
                    if db.execute("SELECT 1 FROM requests r WHERE r.account_key=? AND r.status='dispatching' AND "+OFFICIAL_REQUEST_SQL,(row['account_key'],)).fetchone():continue
                    reason=self.reason(db,row['account_key'],available)
                    if reason:
                        db.execute("UPDATE requests SET status='failed',finished=?,note='排队期间账号状态变化；本轮未发送，不重试或换号。' WHERE request_id=?",(time.time(),row['request_id']))
                        db.execute('UPDATE official_jobs SET reason=? WHERE request_id=?',(reason,row['request_id']));continue
                    now=time.time()
                    db.execute("UPDATE requests SET status='dispatching',note='官方调用处理中；只提交一次，不自动换号。' WHERE request_id=?",(row['request_id'],))
                    db.execute('UPDATE official_jobs SET started=?,queue_wait_ms=? WHERE request_id=?',(now,round((now-row['created'])*1000),row['request_id']))
                    db.execute('COMMIT');return dict(row)
                db.execute('COMMIT');return None
            except BaseException:db.execute('ROLLBACK');raise

    def finish(self,job,result):
        status=result.get('status','unknown');reason=result.get('reason');now=time.time()
        note=('官方调用完成。' if status=='completed' else 'Google要求额外核验，已停止。' if reason=='verification_required' else '官方额度或频率受限，已停止。' if reason=='quota_or_rate_limit' else '调用结果需要核对，已停止。' if status=='unknown' else '本轮未成功，已停止。')+' 固定账号；无自动重试或换号。'
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('UPDATE requests SET status=?,finished=?,response=?,note=? WHERE request_id=?',(status,now,result.get('response'),note,job['request_id']))
                db.execute('UPDATE official_jobs SET reason=? WHERE request_id=?',(reason,job['request_id']))
                db.execute('INSERT OR REPLACE INTO request_metrics VALUES(?,?,?,?)',(job['request_id'],result.get('first_text_ms'),result.get('total_ms'),result.get('token_count')))
                if status=='completed':
                    native={'channel':CHANNEL,'model':MODEL,'official_conversation_id':result['native']}
                    db.execute('UPDATE conversations SET native_json=?,updated=? WHERE id=?',(json.dumps(native),now,job['conversation_id']))
                else:
                    db.execute('UPDATE conversations SET blocked=?,updated=? WHERE id=?',(note,now,job['conversation_id']))
                    if result.get('submitted'):
                        db.execute('INSERT INTO official_account_blocks VALUES(?,?,?) ON CONFLICT(account_key) DO UPDATE SET reason=excluded.reason,created=excluded.created',(job['account_key'],reason or status,now))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise

    def worker(self):
        while not self.stop.is_set():
            # Idle queue inspection must not occupy a shared Flow/Gemini slot.
            with self.pool.connection() as db:
                pending=db.execute("SELECT 1 FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status='queued' LIMIT 1").fetchone()
            if not pending:
                self.wake.wait(.5);self.wake.clear();continue
            slot=self.pool.dispatch_limit.try_acquire()
            job=None
            if slot:
                try:
                    job=self.claim()
                    if job:
                        try:result=self.runtime.run(job['account_key'],job['prompt'],job['native_before'],self.pool.state_dir/'official-audit'/job['request_id'])
                        except Exception:result={'submitted':True,'status':'unknown','reason':'worker_interrupted'}
                        self.finish(job,result)
                except Exception:
                    # A claimed job is never requeued or sent through another account.
                    if job:
                        try:self.finish(job,{'submitted':True,'status':'unknown','reason':'persistence_or_worker_failure'})
                        except Exception:pass
                finally:slot.close()
            if not job:self.wake.wait(.2);self.wake.clear()
