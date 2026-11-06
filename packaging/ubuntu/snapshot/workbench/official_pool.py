"""Durable FIFO jobs, round-robin account assignment, and pinned conversations."""
import hashlib,json,re,sqlite3,threading,time,uuid
from pathlib import Path
from official_runtime import (OfficialRuntime,MODEL,CHANNEL,TEMPORARY_LAUNCH_FAILURE,AUTH_UNSENT,
                              BAD_REPLY,INCOMPLETE_REPLY,generic_refusal)
from official_reauth import Reauthorization,SCHEMA as REAUTH_SCHEMA,private_bytes
import gemini_image_input as vision

# Ownership is explicit: queued official jobs or a saved native official conversation.
# Titles, model names and old monitor state never determine channel ownership.
OFFICIAL_REQUEST_SQL = """(EXISTS(SELECT 1 FROM official_jobs j WHERE j.request_id=r.request_id)
    OR EXISTS(SELECT 1 FROM conversation_turns t JOIN conversations c ON c.id=t.conversation_id
        WHERE t.request_id=r.request_id AND CASE WHEN json_valid(c.native_json)
        THEN (json_extract(c.native_json,'$.channel')='official_antigravity_cli'
              OR json_extract(c.native_json,'$.official_cli_trial')=1) ELSE 0 END))"""

# Quota lookup validation is advisory; only actual dialogue restrictions block dispatch.
QUOTA_CHECK_ONLY_BLOCKS=('activation_verification_required','official_authorization_verification_required')
# These blocks can be checked by a new short probe. Business messages remain
# blocked until that probe succeeds; unknown business results are checked separately.
READINESS_RECOVERABLE_BLOCKS=('provider_error','provider_connection_failed','provider_unavailable')
READINESS_REPLY='GEMINI_READY'
READINESS_PROMPT='这是创作工作台的云端连通性检查。请只回复 GEMINI_READY，不要添加其他文字。'

def unresolved_official(db,key):
    return db.execute("SELECT 1 FROM requests r WHERE r.account_key=? AND r.status='unknown' AND "+OFFICIAL_REQUEST_SQL+" AND NOT EXISTS(SELECT 1 FROM official_jobs p WHERE p.request_id=r.request_id AND p.readiness_probe=1) AND NOT EXISTS(SELECT 1 FROM account_unknown_release x WHERE x.request_id=r.request_id)",(key,)).fetchone() is not None

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
              CREATE TABLE IF NOT EXISTS official_attempts(
                request_id TEXT NOT NULL REFERENCES requests(request_id),
                ordinal INTEGER NOT NULL CHECK(ordinal IN (1,2)), status TEXT NOT NULL,
                reason TEXT, submitted INTEGER, started REAL NOT NULL, finished REAL, total_ms REAL,
                PRIMARY KEY(request_id,ordinal));
            ''')
            if 'response' not in {row['name'] for row in db.execute('PRAGMA table_info(official_attempts)')}:
                db.execute('ALTER TABLE official_attempts ADD COLUMN response TEXT')
            if 'readiness_probe' not in {row['name'] for row in db.execute('PRAGMA table_info(official_jobs)')}:
                db.execute('ALTER TABLE official_jobs ADD COLUMN readiness_probe INTEGER NOT NULL DEFAULT 0')
            db.executescript(REAUTH_SCHEMA)
            # Interrupted submitted jobs stay unknown; queued jobs were never sent and are not replayed after restart.
            db.execute("UPDATE requests SET status='failed',finished=?,note='工作台重启前尚未发送；不会自动恢复提交。' WHERE status='queued' AND request_id IN (SELECT request_id FROM official_jobs)",(time.time(),))
            db.execute("UPDATE official_attempts SET status='unknown',reason='workbench_restarted',finished=? WHERE status='running'",(time.time(),))
            db.execute("UPDATE official_reauth SET state='stopped' WHERE state IN ('queued','resending')")
        self.reauth=Reauthorization(self)
        self.ready=any(self.runtime.inventory().values())

    def start(self):
        if self.threads:return
        monitor=threading.Thread(target=self.monitor_reauth,name='official-reauth',daemon=True)
        self.threads.append(monitor);monitor.start()
        replies=threading.Thread(target=self.monitor_saved_replies,name='official-saved-replies',daemon=True)
        self.threads.append(replies);replies.start()
        if not self.ready:return
        for i in range(5):
            t=threading.Thread(target=self.worker,name='official-worker-'+str(i),daemon=True);self.threads.append(t);t.start()

    def close(self):
        self.stop.set();self.wake.set()
        # Never close the pool DB lock while a provider call can still finish writing.
        for t in self.threads:t.join(timeout=190)

    def monitor_reauth(self):
        while not self.stop.wait(1):
            try:self.reauth.tick()
            except (sqlite3.Error,OSError,ValueError):pass

    def monitor_saved_replies(self):
        # Conversation recovery runs separately from the account readiness button.
        while not self.stop.wait(60):
            try:self.reconcile_saved_replies()
            except (sqlite3.Error,OSError,ValueError):pass

    def reconcile_saved_replies(self):
        """Restore only proven replies from original CLI receipts. Never call a model."""
        from contextlib import suppress
        restored=0
        if self.pool.maintenance():return restored
        with self.pool.connection() as db:
            rows=db.execute("""SELECT r.request_id,r.account_key,r.finished,j.conversation_id,j.native_before,j.readiness_probe,
                       a.ordinal,a.started
                FROM requests r JOIN official_jobs j USING(request_id)
                JOIN conversation_turns t USING(request_id)
                JOIN official_attempts a USING(request_id)
                WHERE r.status='unknown' AND t.attachments='[]'
                AND a.ordinal=(SELECT max(ordinal) FROM official_attempts WHERE request_id=r.request_id)
                ORDER BY r.created DESC LIMIT 100""").fetchall()
        for row in rows:
            with suppress(OSError,ValueError,TypeError,AttributeError,KeyError):
                rid=row['request_id']
                if str(uuid.UUID(rid))!=rid:continue
                audit=self.pool.state_dir/'official-audit'/rid
                if audit.is_symlink():continue
                if row['ordinal']==2:audit=audit/'attempt-2'
                if audit.is_symlink():continue
                proof=json.loads(private_bytes(audit/'submission.json',4096))
                result=json.loads(private_bytes(audit/'result.json',8*1024*1024))
                response=result.get('response');native=result.get('native')
                if (proof.get('account_key')!=row['account_key'] or proof.get('model')!=MODEL
                    or type(proof.get('created')) not in (int,float) or not row['started']<=proof['created']<=time.time()
                    or result.get('submitted') is not True or result.get('status')!='completed' or result.get('reason')
                    or not isinstance(response,str) or not response.strip() or generic_refusal(response)
                    or (row['readiness_probe'] and response.strip()!=READINESS_REPLY)
                    or not isinstance(native,str) or not re.fullmatch(r'[0-9a-f-]{36}',native)
                    or proof.get('native') not in (None,native)
                    or (row['native_before'] is not None and row['native_before']!=native)):
                    continue
                with self.pool.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if self.pool.maintenance():
                        db.execute('ROLLBACK');break
                    now=time.time()
                    updated=db.execute("""UPDATE requests SET status='completed',response=?,finished=?,note=?
                        WHERE request_id=? AND status='unknown' AND finished IS ?""",
                        (response,now,'已从原调用记录找回完整回复；没有重新发送。',rid,row['finished']))
                    if not updated.rowcount:
                        db.execute('ROLLBACK');continue
                    db.execute("UPDATE official_attempts SET status='completed',reason=NULL,submitted=1,finished=?,total_ms=?,response=? WHERE request_id=? AND ordinal=?",
                               (now,result.get('total_ms'),response,rid,row['ordinal']))
                    db.execute('UPDATE official_jobs SET reason=NULL WHERE request_id=?',(rid,))
                    db.execute('INSERT OR REPLACE INTO request_metrics VALUES(?,?,?,?)',
                               (rid,result.get('first_text_ms'),result.get('total_ms'),result.get('token_count')))
                    db.execute("""UPDATE conversations SET native_json=?,updated=?,blocked=NULL WHERE id=?
                        AND NOT EXISTS(SELECT 1 FROM conversation_turns t JOIN requests r USING(request_id)
                                       WHERE t.conversation_id=? AND r.rowid>(SELECT rowid FROM requests WHERE request_id=?))""",
                        (json.dumps({'channel':CHANNEL,'model':MODEL,'official_conversation_id':native}),now,
                         row['conversation_id'],row['conversation_id'],rid))
                    db.execute('DELETE FROM official_account_blocks WHERE account_key=? AND created=?',
                               (row['account_key'],row['finished']))
                    db.execute('COMMIT')
                    restored+=1
        return restored

    def reason(self,db,key,available,allowed_block=None):
        if not available.get(key):return 'authorization_not_ready'
        pref=db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?',(key,)).fetchone()
        if pref and pref[0]==0:return 'dispatch_paused'
        if db.execute("""SELECT 1 FROM official_reauth a JOIN official_jobs j USING(request_id)
                        WHERE a.account_key=? AND a.state='waiting' AND j.readiness_probe=1""",(key,)).fetchone():
            return 'authorization_in_progress'
        if unresolved_official(db,key):return 'unresolved_request'
        block=db.execute('SELECT reason,created FROM official_account_blocks WHERE account_key=?',(key,)).fetchone()
        if block and tuple(block)!=allowed_block and block[0] not in QUOTA_CHECK_ONLY_BLOCKS:return 'official_call_blocked'
        return None

    @staticmethod
    def readiness_block(db,key):
        row=db.execute('SELECT reason,created FROM official_account_blocks WHERE account_key=?',(key,)).fetchone()
        return tuple(row) if row and row[0] in READINESS_RECOVERABLE_BLOCKS else None

    def public_status(self):
        available=self.runtime.inventory()
        with self.pool.connection() as db:
            blocks=dict(db.execute('SELECT account_key,reason FROM official_account_blocks'))
            result={key:{'authorization':'saved_verified_identity' if ready else 'not_ready',
                         'dispatch_eligible':self.reason(db,key,available) is None,
                         'restriction':self.reason(db,key,available),'block_reason':blocks.get(key),
                         'busy':busy_official(db,key)} for key,ready in available.items()}
            queued=db.execute("SELECT count(*) FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status='queued'").fetchone()[0]
            active=db.execute("SELECT count(*) FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status='dispatching'").fetchone()[0]
        return {'available':any(available.values()),'channel':CHANNEL,'model':MODEL,'accounts':result,'queued':queued,'active':active,
                'selection':'round_robin','max_concurrency':5,'retry':True,'failover':False,
                'shared_dispatch':self.pool.dispatch_limit.public_status()}

    @staticmethod
    def recent_readiness_probe(db,key):
        return db.execute("""SELECT r.request_id FROM requests r JOIN official_jobs j USING(request_id)
            WHERE r.account_key=? AND j.readiness_probe=1
            AND (r.status IN ('queued','dispatching') OR r.created>=?)
            ORDER BY r.created DESC LIMIT 1""",(key,time.time()-30)).fetchone()

    def queue_readiness_probes(self,items,quotas,*,blocked_only=False):
        """One real check per eligible account, with double-click protection."""
        from live_pool import Conflict
        if not self.ready or self.pool.maintenance():return []
        with self.pool.connection() as db:
            inventory=self.runtime.inventory()
            candidates=[]
            for key in inventory:
                block=self.readiness_block(db,key)
                if (not blocked_only or block) and self.reason(db,key,inventory,block) is None:
                    candidates.append(key)
        queued=[]
        now=time.time()
        for key in candidates:
            row=quotas.get(key,{})
            checked=row.get('checked_at')
            if type(checked) in (int,float) and 0<=now-checked<=600:
                if row.get('status') in ('authorization_required','verification_required'):
                    continue
                if row.get('status')=='ok' and any(row.get(w,{}).get('remaining_percent')==0 for w in ('five_hour','weekly')):
                    continue
            rid=str(uuid.uuid4())
            try:
                record,new=self.generate(key,READINESS_PROMPT,rid,items,readiness_probe=True)
            except (Conflict,ValueError):
                continue
            queued.append({'request_id':record['request_id'],'account_key':key,'new':new})
        return queued

    def generate(self,account,prompt,request_id,items,attachments=None,conversation_id=None,model=None,*,image_analysis=False,readiness_probe=False):
        from live_pool import Conflict
        if not self.ready:raise Conflict('官方调用尚未配置完成，未发送。')
        self.pool.conversation_id(request_id)
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>20000:raise ValueError('请输入1到20000字的消息。')
        if image_analysis:
            if conversation_id:raise ValueError('识图请求是独立对话。')
            attachments=vision.normalized_attachments(attachments)
        elif attachments:raise ValueError('普通对话只收文字；请使用 Gemini 识图接口传图。')
        if model not in (None,'',MODEL):raise Conflict('当前官方通道使用固定模型，不能自动切换模型。')
        if not isinstance(account,str):raise ValueError('请选择账号或自动轮询。')
        if account!='auto':self.pool.allowed(account,items)
        if conversation_id:self.pool.conversation_id(conversation_id)
        payload={'account':account,'prompt':prompt,'conversation_id':conversation_id,'model':model or MODEL}
        if image_analysis:payload.update(channel=vision.CHANNEL,attachments=attachments)
        digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        now=time.time();available=self.runtime.inventory()
        selection='auto' if account=='auto' else 'manual'
        permitted={i.get('key','').removeprefix('flow-account-') for i in items if isinstance(i,dict)}
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('SELECT 1 FROM flow_requests WHERE request_id=?',(request_id,)).fetchone():
                    raise Conflict('该请求编号属于 Flow，不能在 Gemini 提交或重试。')
                prior=db.execute('SELECT prompt_digest FROM requests WHERE request_id=?',(request_id,)).fetchone()
                if prior:
                    if prior[0]!=digest or not db.execute('SELECT 1 FROM official_jobs WHERE request_id=?',(request_id,)).fetchone():raise Conflict('该请求编号已经使用；不会重新提交。')
                    db.execute('COMMIT');return self.pool.get(request_id),False
                if self.pool.maintenance():raise Conflict('工作台正在更新，消息没有发送。')
                if readiness_probe:
                    recent=self.recent_readiness_probe(db,account)
                    if recent:
                        db.execute('COMMIT');return self.pool.get(recent[0]),False
                if image_analysis:vision.files_for(self.pool,request_id,attachments)
                if db.execute("SELECT count(*) FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status IN ('queued','dispatching')").fetchone()[0]>=100:raise Conflict('等待队列已满，消息未发送。')
                conv=db.execute('SELECT * FROM conversations WHERE id=?',(conversation_id,)).fetchone() if conversation_id else None
                if conversation_id and (not conv or conv['hidden']):raise Conflict('请选择一个未隐藏的对话。')
                native=None
                if conv:
                    if conv['blocked']:raise Conflict('该对话已归档或有待核对的结果；请保留记录并新建对话。')
                    data=json.loads(conv['native_json'] or '{}')
                    if conv['account_key']:
                        if data.get('channel')==vision.CHANNEL:raise Conflict('识图记录不能续接到文字对话，请新建对话。')
                        if data.get('channel')!=CHANNEL:raise Conflict('旧通道对话保留为历史记录，请新建官方对话。')
                        if account not in ('auto',conv['account_key']):raise Conflict('多轮对话必须使用最初的账号，不能换号。')
                        account=conv['account_key'];native=data.get('official_conversation_id')
                        selection='manual'
                        if not native:raise Conflict('原生上下文尚未确认，不能重新提交。')
                    if db.execute("SELECT 1 FROM conversation_turns t JOIN requests r USING(request_id) WHERE t.conversation_id=? AND r.status IN ('queued','dispatching')",(conversation_id,)).fetchone():raise Conflict('该对话还有一轮正在等待或执行，请等待完成。')
                eligible=sorted(k for k in available if k in permitted and self.reason(
                    db,k,available,self.readiness_block(db,k) if readiness_probe and k==account else None) is None)
                if account=='auto':
                    if not eligible:raise Conflict('没有允许派发且授权就绪的账号；暂停和未确认限制均保留。')
                    row=db.execute('SELECT last_account FROM official_pool_cursor WHERE id=1').fetchone();last=row[0] if row else ''
                    account=next((k for k in eligible if k>last),eligible[0])
                    db.execute('INSERT INTO official_pool_cursor(id,last_account) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET last_account=excluded.last_account',(account,))
                elif account not in eligible:raise Conflict('该账号授权未就绪、已暂停或存在未确认结果，消息未发送。')
                if not conv:
                    conversation_id=str(uuid.uuid4())
                    title='Gemini 云端验活' if readiness_probe else ('识图 · ' if image_analysis else '')+prompt.strip()[:36]
                    db.execute('INSERT INTO conversations(id,title,created,updated,hidden) VALUES(?,?,?,?,0)',(conversation_id,title,now,now))
                data={'channel':vision.CHANNEL if image_analysis else CHANNEL,'model':MODEL,'official_conversation_id':native}
                db.execute('UPDATE conversations SET account_key=?,native_json=?,updated=? WHERE id=?',(account,json.dumps(data),now,conversation_id))
                db.execute("INSERT INTO requests(request_id,account_key,prompt_digest,status,created,note,selection) VALUES(?,?,?,'queued',?,?,?)",(request_id,account,digest,now,'已进入官方调用队列；固定账号。',selection))
                db.execute('INSERT INTO conversation_turns VALUES(?,?,?,?)',(request_id,conversation_id,prompt,json.dumps(attachments if image_analysis else [])))
                db.execute('INSERT INTO conversation_attempt_models(request_id,model) VALUES(?,?)',(request_id,MODEL))
                db.execute('INSERT INTO official_jobs(request_id,conversation_id,model,native_before,readiness_probe) VALUES(?,?,?,?,?)',
                           (request_id,conversation_id,MODEL,native,int(readiness_probe)))
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
                rows=db.execute("""SELECT r.request_id,r.account_key,r.created,j.conversation_id,j.native_before,j.readiness_probe,t.prompt,t.attachments,
                                  a.state AS reauth_state,a.block_reason,a.block_created,a.account_key AS reauth_account,c.hidden
                                  FROM requests r JOIN official_jobs j USING(request_id)
                                  JOIN conversation_turns t USING(request_id) JOIN conversations c ON c.id=j.conversation_id
                                  LEFT JOIN official_reauth a USING(request_id) WHERE r.status='queued' ORDER BY r.created,r.rowid""").fetchall()
                for row in rows:
                    if db.execute("SELECT 1 FROM requests r WHERE r.account_key=? AND r.status='dispatching' AND "+OFFICIAL_REQUEST_SQL,(row['account_key'],)).fetchone():continue
                    resumed=row['reauth_state']=='queued'
                    recovery=self.readiness_block(db,row['account_key']) if row['readiness_probe'] else None
                    reason=self.reason(db,row['account_key'],available,
                                       (row['block_reason'],row['block_created']) if resumed else recovery)
                    if resumed:
                        block=db.execute('SELECT reason,created FROM official_account_blocks WHERE account_key=?',(row['account_key'],)).fetchone()
                        attempts=self.reauth.attempts(db,row['request_id'])
                        if (not block or tuple(block)!=(row['block_reason'],row['block_created'])
                                or row['account_key']!=row['reauth_account'] or row['attachments']!='[]' or row['hidden']
                                or len(attempts)!=1 or attempts[0]['reason']!=AUTH_UNSENT or attempts[0]['submitted']!=0):
                            reason='reauth_state_changed'
                    if reason:
                        db.execute("UPDATE requests SET status='failed',finished=?,note='排队期间账号状态变化；本轮未发送，不重试或换号。' WHERE request_id=?",(time.time(),row['request_id']))
                        if resumed:db.execute("UPDATE official_reauth SET state='stopped' WHERE request_id=?",(row['request_id'],))
                        db.execute('UPDATE official_jobs SET reason=? WHERE request_id=?',(reason,row['request_id']));continue
                    now=time.time()
                    db.execute("UPDATE requests SET status='dispatching',note='官方调用处理中；固定账号。' WHERE request_id=?",(row['request_id'],))
                    db.execute('UPDATE official_jobs SET started=?,queue_wait_ms=? WHERE request_id=?',(now,round((now-row['created'])*1000),row['request_id']))
                    db.execute("INSERT INTO official_attempts(request_id,ordinal,status,started) VALUES(?,?,'running',?)",(row['request_id'],2 if resumed else 1,now))
                    if resumed:db.execute("UPDATE official_reauth SET state='resending' WHERE request_id=?",(row['request_id'],))
                    db.execute('COMMIT');return {**dict(row),'readiness_recovery':recovery}
                db.execute('COMMIT');return None
            except BaseException:db.execute('ROLLBACK');raise

    @staticmethod
    def finish_attempt(db,request_id,result,now):
        submitted=result.get('submitted')
        db.execute("""UPDATE official_attempts SET status=?,reason=?,submitted=?,finished=?,total_ms=?,response=?
                      WHERE request_id=? AND status='running'""",
                   (result.get('status','unknown'),result.get('reason'),
                    int(submitted) if type(submitted) is bool else None,now,result.get('total_ms'),result.get('response'),request_id))

    def reserve_retry(self,job,result,auth_recovered=False):
        reason=result.get('reason')
        unsent=(result.get('submitted') is False and result.get('response') is None
                and result.get('first_text_ms') is None
                and (reason==TEMPORARY_LAUNCH_FAILURE or (reason==AUTH_UNSENT and auth_recovered)))
        finished_bad=(result.get('submitted') is True and reason in (BAD_REPLY,INCOMPLETE_REPLY))
        if result.get('status')!='failed' or not (unsent or finished_bad):
            return False
        available=self.runtime.inventory()
        request_id=job['request_id']
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            first=db.execute("""SELECT 1 FROM requests r JOIN official_attempts a USING(request_id)
                                WHERE r.request_id=? AND r.account_key=? AND r.status='dispatching'
                                AND a.ordinal=1 AND a.status='running'""",(request_id,job['account_key'])).fetchone()
            if (not first or self.stop.is_set() or self.pool.maintenance()
                    or self.reason(db,job['account_key'],available)):
                db.execute('ROLLBACK')
                return False
            now=time.time()
            self.finish_attempt(db,request_id,result,now)
            db.execute("INSERT INTO official_attempts(request_id,ordinal,status,started) VALUES(?,2,'running',?)",(request_id,now))
            note=('首次回复已结束但未能正常回答' if finished_bad else
                  '首次未发送，已有授权已恢复' if auth_recovered else '首次未发送，启动遇到临时故障')
            db.execute("UPDATE requests SET note=? WHERE request_id=?",(note+'，正在用原账号重试一次。',request_id))
            db.execute('COMMIT')
        return True

    def run_text(self,job):
        audit=self.pool.state_dir/'official-audit'/job['request_id']
        args=(job['account_key'],job['prompt'],job['native_before'])
        if job.get('reauth_state')=='queued':
            return self.runtime.run(*args,audit/'attempt-2')
        result=self.runtime.run(*args,audit)
        if job.get('readiness_probe'):
            if result.get('reason')==AUTH_UNSENT:
                return {**result,'reason':'readiness_authorization_required'}
            if result.get('status')=='completed' and str(result.get('response','')).strip()!=READINESS_REPLY:
                return {**result,'status':'failed','reason':'readiness_unexpected_reply'}
            return result
        auth_recovered=False
        if (result.get('reason')==AUTH_UNSENT and result.get('submitted') is False
                and not self.stop.is_set() and not self.pool.maintenance()):
            from official_quota import recover_saved_authorization
            auth_recovered=recover_saved_authorization(self.runtime,job['account_key'])
        if self.reserve_retry(job,result,auth_recovered):
            retry_args=args
            if result.get('reason') in (BAD_REPLY,INCOMPLETE_REPLY):
                retry_args=(args[0], '请重新完整回答以下原问题；如确实无法回答，请说明具体原因。\n\n'+args[1], result['native'])
            result=self.runtime.run(*retry_args,audit/'attempt-2')
            if result.get('reason')==AUTH_UNSENT and auth_recovered:
                result={**result,'reason':'authorization_still_required'}
        return result

    def finish(self,job,result):
        status=result.get('status','unknown');reason=result.get('reason');now=time.time()
        note=('官方调用完成。' if status=='completed' else '原账号需要重新授权，消息尚未发送。' if reason in (AUTH_UNSENT,'authorization_still_required','readiness_authorization_required') else '图片文件无法读取或校验失败，未发送。' if reason=='invalid_image_input' else 'Google要求额外核验，已停止。' if reason=='verification_required' else '官方额度或频率受限，已停止。' if reason=='quota_or_rate_limit' else '调用结果需要核对，已停止。' if status=='unknown' else '回复已结束，但未能正常回答。' if reason in (BAD_REPLY,INCOMPLETE_REPLY,'readiness_unexpected_reply') else '本轮未成功，已停止。')+' 全程使用原账号。'
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                updated=db.execute("UPDATE requests SET status=?,finished=?,response=?,note=? WHERE request_id=? AND status='dispatching'",(status,now,result.get('response'),note,job['request_id']))
                if not updated.rowcount:
                    db.execute('ROLLBACK');return
                self.finish_attempt(db,job['request_id'],result,now)
                recovered=job.get('readiness_recovery')
                if (job.get('readiness_probe') and recovered and status=='completed'
                        and (result.get('response') or '').strip()==READINESS_REPLY):
                    db.execute("DELETE FROM official_account_blocks WHERE account_key=? AND reason=? AND created=?",
                               (job['account_key'],*recovered))
                if job.get('reauth_state')=='queued':
                    db.execute("UPDATE official_reauth SET state=? WHERE request_id=? AND state='resending'",
                               ('completed' if status=='completed' else 'stopped',job['request_id']))
                    if status=='completed':
                        db.execute("DELETE FROM official_account_blocks WHERE account_key=? AND reason=? AND created=?",
                                   (job['account_key'],job['block_reason'],job['block_created']))
                db.execute('UPDATE official_jobs SET reason=? WHERE request_id=?',(reason,job['request_id']))
                db.execute('INSERT OR REPLACE INTO request_metrics VALUES(?,?,?,?)',(job['request_id'],result.get('first_text_ms'),result.get('total_ms'),result.get('token_count')))
                if status=='completed' and job.get('attachments','[]')!='[]':
                    db.execute('UPDATE conversations SET updated=? WHERE id=?',(now,job['conversation_id']))
                elif status=='completed' or reason in (BAD_REPLY,INCOMPLETE_REPLY) and result.get('native'):
                    native={'channel':CHANNEL,'model':MODEL,'official_conversation_id':result['native']}
                    db.execute('UPDATE conversations SET native_json=?,updated=?,blocked=NULL WHERE id=?',(json.dumps(native),now,job['conversation_id']))
                else:
                    db.execute('UPDATE conversations SET blocked=?,updated=? WHERE id=?',(note,now,job['conversation_id']))
                    if not job.get('readiness_probe') and (status=='unknown' or reason in (AUTH_UNSENT,'authorization_still_required','verification_required','quota_or_rate_limit')):
                        db.execute('INSERT INTO official_account_blocks VALUES(?,?,?) ON CONFLICT(account_key) DO UPDATE SET reason=excluded.reason,created=excluded.created',(job['account_key'],reason or status,now))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise

    def run_image(self,job):
        from material_vision import analyze
        try:
            attachments=vision.normalized_attachments(json.loads(job['attachments']))
            parts=vision.prompt_parts(job['prompt'],attachments,vision.files_for(self.pool,job['request_id'],attachments))
        except (OSError, ValueError):
            return {'submitted':False,'status':'failed','reason':'invalid_image_input'}
        receipt=self.pool.state_dir/'official-image-audit'/(job['request_id']+'.json')
        result=analyze(self.runtime,job['account_key'],parts,receipt,keep_partial=True)
        if result.get('status')=='completed' and generic_refusal(result.get('response')):
            result={**result,'status':'failed','reason':BAD_REPLY}
        if self.reserve_retry(job,result):
            result=analyze(self.runtime,job['account_key'],parts,receipt.with_suffix('.attempt-2.json'),keep_partial=True)
            if result.get('status')=='completed' and generic_refusal(result.get('response')):
                result={**result,'status':'failed','reason':BAD_REPLY}
        return result

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
                        try:
                            if job['attachments']!='[]':result=self.run_image(job)
                            else:result=self.run_text(job)
                        except Exception:result={'submitted':True,'status':'unknown','reason':'worker_interrupted'}
                        self.finish(job,result)
                except Exception:
                    # A claimed job is never requeued or sent through another account.
                    if job:
                        try:self.finish(job,{'submitted':True,'status':'unknown','reason':'persistence_or_worker_failure'})
                        except Exception:pass
                finally:slot.close()
            if not job:self.wake.wait(.2);self.wake.clear()
