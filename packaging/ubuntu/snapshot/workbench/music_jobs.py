"""Durable music receipts; reconnects read state and never replay a submission."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


class MusicJobs:
    def __init__(self, state, generate, ingest, paused=lambda:False):
        self.path=Path(state)/'music-jobs.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.generate,self.ingest,self.paused=generate,ingest,paused
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,digest TEXT,prompt TEXT,model TEXT,status TEXT,result TEXT,error TEXT,created REAL,updated REAL)')
            db.execute("UPDATE jobs SET status='unknown',error='服务重启，原任务结果待核对；不会重新生成。',updated=? WHERE status IN ('generating','ingesting')",(time.time(),))
        self.path.chmod(0o600)

    def db(self):
        db=sqlite3.connect(self.path,timeout=15,isolation_level=None)
        db.row_factory=sqlite3.Row
        return closing(db)

    def get(self, ident):
        with self.db() as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        if not row:return None
        return {k:row[k] for k in ('id','status','error','created','updated')} | {'result':json.loads(row['result']) if row['result'] else None}

    def latest(self):
        with self.db() as db:row=db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 1').fetchone()
        return self.get(row['id']) if row else None

    def submit(self,ident,prompt,model):
        try:
            if str(uuid.UUID(ident))!=ident:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise ValueError('请求编号不正确。')
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt.strip())>2000:raise ValueError('请填写1～2000字的音乐描述。')
        if model not in ('lyria','lyria-fast','lyria-pro','lyria-pro-fast'):raise ValueError('音乐模型不正确。')
        prompt=prompt.strip();digest=hashlib.sha256(json.dumps([prompt,model]).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT digest FROM jobs WHERE id=?',(ident,)).fetchone()
            if row:
                if row['digest']!=digest:raise ValueError('同一请求编号不能用于另一条音乐。')
                db.execute('COMMIT');return self.get(ident)
            if self.paused():raise ValueError('工作台维护中，尚未提交。')
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('generating','ingesting')").fetchone():raise ValueError('已有音乐任务正在执行，请等待完成。')
            now=time.time()
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL,NULL,?,?)',(ident,digest,prompt,model,'generating',now,now))
            db.execute('COMMIT')
        threading.Thread(target=self.run,args=(ident,prompt,model),daemon=True,name='music-'+ident).start()
        return self.get(ident)

    def save(self,ident,status,result=None,error=None):
        with self.db() as db:db.execute('UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?',(status,json.dumps(result) if result is not None else None,error,time.time(),ident))

    def run(self,ident,prompt,model):
        result=None
        try:
            result=self.generate(prompt,model)
            self.save(ident,'ingesting',result)
            try:
                receipt=self.ingest(result['clips'])
                result['ingested']=sum(bool(i.get('ok')) for i in receipt.get('ingested',[]))
            except Exception:
                result['ingested']=0;result['ingest_error']='音乐已生成，素材登记未完成；请保留原结果。'
            self.save(ident,'completed',result)
        except Exception:
            self.save(ident,'unknown',result,'生成结果待核对；不会自动重复提交。')
