"""User-started project transfers and deletions with persisted, idempotent state."""
import json, os, re, subprocess, threading, time
from pathlib import Path

class ProjectActions:
    def __init__(self, pool, sources, backfill, root, project_rows, read_materials):
        self.pool,self.sources,self.backfill,self.root=pool,sources,backfill,Path(root)
        self.project_rows,self.read_materials=project_rows,read_materials
        with pool.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS project_actions(
                id TEXT PRIMARY KEY, project_key TEXT NOT NULL, profile TEXT NOT NULL, project TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                detail TEXT NOT NULL)''')
            db.execute("UPDATE project_actions SET status='unknown',updated=?,detail=? WHERE status='running'",
                       (time.time(),json.dumps({'message':'操作中断，结果需核对；不会自动重试。'},ensure_ascii=False)))

    def status(self):
        with self.pool.connection() as db:
            rows=db.execute('SELECT id,project_key,kind,status,created,updated,detail FROM project_actions ORDER BY created DESC LIMIT 200').fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            try:item['detail']=json.loads(item['detail'])
            except (ValueError,TypeError):item['detail']={'message':'状态待核对'}
            result.append(item)
        return {'items':result}

    def start(self,request_id,key,kind):
        self.pool.conversation_id(request_id)
        if not isinstance(key,str) or not re.fullmatch(r'flow-project-[0-9a-f]{64}',key) or kind not in ('transfer','delete'):
            raise ValueError('项目或操作格式不正确。')
        state,error=self.sources.read('flow','/api/state')
        if not isinstance(state,dict) or state.get('test') is not False:raise ValueError('Flow 后台暂不可用。')
        projects=[p for p in self.project_rows(state) if p['key']==key]
        if len(projects)!=1:raise ValueError('项目登记已变化，请刷新。')
        project=projects[0]
        accounts=[a for a in state.get('accounts',[]) if isinstance(a,dict) and a.get('profile')==project['source_profile']]
        if len(accounts)!=1 or not isinstance(accounts[0].get('email'),str) or not accounts[0]['email']:
            raise ValueError('账号信息不完整，请先核对账号。')
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT project_key,kind FROM project_actions WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['project_key']!=key or old['kind']!=kind:raise ValueError('请求编号已用于其他操作。')
                db.execute('COMMIT');return {'id':request_id}
            if self.pool.maintenance() or db.execute("SELECT 1 FROM project_actions WHERE project_key=? AND status='running'",(key,)).fetchone():
                raise ValueError('该项目已有操作正在进行。')
            if kind=='delete' and db.execute("SELECT 1 FROM project_actions WHERE project_key=? AND kind='delete' AND status='unknown'",(key,)).fetchone():
                raise ValueError('上次删除结果待核对，不能重复删除。')
            now=time.time()
            db.execute('INSERT INTO project_actions VALUES(?,?,?,?,?,?,?,?,?)',(request_id,key,project['source_profile'],project['source_id'],kind,'running',now,now,json.dumps({'message':'正在准备'},ensure_ascii=False)))
            db.execute('COMMIT')
        payload={'id':request_id,'profile':project['source_profile'],'project':project['source_id'],'email':accounts[0]['email'],'kind':kind}
        threading.Thread(target=self._run,args=(payload,),name='project-action',daemon=True).start()
        return {'id':request_id}

    def _write(self,ident,status,detail):
        with self.pool.connection() as db:
            db.execute('UPDATE project_actions SET status=?,updated=?,detail=? WHERE id=?',
                       (status,time.time(),json.dumps(detail,ensure_ascii=False),ident))

    def _run(self,payload):
        ident=payload['id'];submitted=False;deleted=False;transfer_finished=False;added=set();detail={'message':'操作准备中'};error=None
        try:
            pid=subprocess.check_output(['systemctl','--user','show','studiowright-console.service','--property=MainPID','--value'],text=True,timeout=5).strip()
            if not pid.isdigit() or pid=='0':raise ValueError('Flow 服务暂不可用。')
            python=os.fsdecode(Path('/proc',pid,'cmdline').read_bytes().split(b'\0')[0])
            if not Path(python).is_absolute() or not Path(python).is_file():raise ValueError('Flow 运行环境不可用。')
            proc=subprocess.Popen([python,'-B',str(self.root/'project_action_worker.py')],cwd=self.root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf8')
            proc.stdin.write(json.dumps(payload,ensure_ascii=True));proc.stdin.close()
            for line in proc.stdout:
                try:event=json.loads(line)
                except (ValueError,TypeError):continue
                phase=event.get('phase')
                if phase=='delete_submitted':
                    submitted=True;detail={'message':'正在删除云端项目，结果确认前请勿重复操作。'}
                elif phase=='deleted':deleted=True;detail={'message':'云端项目已删除，素材库文件和历史记录已保留。'}
                elif phase=='error':error=str(event.get('message','操作未完成。'))[:180]
                elif phase=='transfer':
                    new_id=event.get('new_id')
                    if isinstance(new_id,str) and re.fullmatch(r'[0-9a-f]{64}',new_id):added.add(new_id)
                    detail={k:event.get(k,0) for k in ('total','done','added','skipped','failed')}
                    detail['message']='正在转移已识别素材。'
                elif phase=='transfer_result':
                    transfer_finished=True
                    added.update(x for x in event.get('added_ids',[]) if isinstance(x,str) and re.fullmatch(r'[0-9a-f]{64}',x))
                    detail={k:event.get(k,0) for k in ('total','skipped','failed')};detail['added']=len(added)
                    detail['message']='部分已识别素材转移失败，请核对。' if detail['failed'] else '已识别素材转移完成。'
                self._write(ident,'running',detail)
            code=proc.wait()
            if payload['kind']=='transfer' and added:
                try:
                    subset=[x for x in self.read_materials(self.sources) if x.get('id') in added]
                    if len(subset)!=len(added):raise ValueError('素材状态未确认')
                    detail['tag_queued']=self.backfill.enqueue_project_assets(ident,subset)
                    if detail['tag_queued'] < len(added):
                        detail['tag_message']='部分素材已有分析记录或格式、大小不支持自动打标签，请在素材库核对。'
                except Exception:
                    detail['tag_queued']=0;detail['tag_message']='素材已入库，自动打标签未排队，请在素材库补打。'
            if payload['kind']=='delete':
                status='completed' if deleted else ('unknown' if submitted else 'failed')
            else:status='completed' if code==0 and transfer_finished and not detail.get('failed') else 'failed'
            if error and not deleted:detail['message']=error
            self._write(ident,status,detail)
        except Exception:
            self._write(ident,'unknown' if submitted else 'failed',{'message':'操作状态未确认，请核对项目和素材记录。'})
