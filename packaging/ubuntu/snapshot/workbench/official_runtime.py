"""Account-pinned official CLI transport; no browser, retry, fallback, or shell."""
import json,os,re,selectors,signal,subprocess,time
from pathlib import Path

CHANNEL='official_antigravity_cli'
MODEL='gemini-3.8-flash-medium'
DENY={'command(*)','read_file(*)','write_file(*)','read_url(*)','execute_url(*)','mcp(*)'}
KEY=re.compile(r'[0-9a-f]{64}\Z')

class OfficialRuntime:
    def __init__(self,config):
        self.config=Path(config)

    def settings(self):
        if self.config.is_symlink() or not self.config.is_file():return None
        if self.config.stat().st_size>32768:raise ValueError('官方调用配置不可读取。')
        data=json.loads(self.config.read_text(encoding='utf-8'))
        if data.get('schema_version')!=1 or data.get('enabled') is not True:return None
        if data.get('model')!=MODEL or not isinstance(data.get('accounts'),dict):raise ValueError('官方调用配置不完整。')
        binary=Path(data.get('binary',''))
        if not binary.is_absolute() or not binary.is_file() or binary.is_symlink():raise ValueError('官方程序未就绪。')
        if len(data['accounts'])>50 or any(not KEY.fullmatch(k) for k in data['accounts']):raise ValueError('账号配置不正确。')
        return data

    def account(self,key):
        data=self.settings()
        if not data or key not in data['accounts']:return None
        item=data['accounts'][key];root=Path(item['root'])
        if not root.is_absolute() or root.is_symlink():return None
        home=root/'home';token=home/'.gemini/antigravity-cli/antigravity-oauth-token'
        settings=home/'.gemini/antigravity-cli/settings.json'
        if any(p.is_symlink() or not p.is_file() for p in [token,settings]):return None
        if token.stat().st_size>100000 or settings.stat().st_size>65536:return None
        identity=json.loads(token.read_text()).get('id_token_email','').lower()
        if not isinstance(item.get('email'),str) or identity!=item['email'].lower():return None
        policy=json.loads(settings.read_text())
        if not DENY<=set(policy.get('permissions',{}).get('deny',[])):return None
        if not (root/'workspace').is_dir():return None
        return {'root':root,'binary':Path(data['binary']),'email':identity}

    def inventory(self):
        try:data=self.settings()
        except (OSError,ValueError,TypeError):return {}
        if not data:return {}
        result={}
        for key in data['accounts']:
            try:result[key]=bool(self.account(key))
            except (OSError,ValueError,TypeError,KeyError):result[key]=False
        return result

    def run(self,key,prompt,native,audit):
        account=self.account(key)
        if not account:return {'submitted':False,'status':'failed','reason':'authorization_not_ready'}
        root=account['root'];home=root/'home'
        env={'HOME':str(home),'XDG_CONFIG_HOME':str(home/'.config'),'XDG_CACHE_HOME':str(home/'.cache'),
             'XDG_DATA_HOME':str(home/'.local/share'),'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TERM':'xterm-256color'}
        argv=[str(account['binary']),'--print',prompt,'--output-format','stream-json','--model',MODEL,
              '--disable-slash-commands','--print-timeout','150s']
        if native:argv+=['--conversation',native]
        audit=Path(audit);audit.mkdir(mode=0o700,parents=True,exist_ok=False)
        # A receipt precedes the one process launch; the caller never replays it.
        with (audit/'submission.json').open('x',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o600);json.dump({'account_key':key,'created':time.time(),'native':native,'model':MODEL},f);f.flush();os.fsync(f.fileno())
        started=time.monotonic();first=None;answer=None;error=None;rc=None;proc=None
        selector=selectors.DefaultSelector();buffers={'stdout':b'','stderr':b''};total=0
        def event_line(channel,line):
            nonlocal first,answer,error
            if channel=='stderr':
                if re.search(r'\bretrying\b|\bretry attempt\b|switching.*model|falling back',line,re.I):error='retry_or_fallback_stopped'
                return
            try:event=json.loads(line)
            except ValueError:return
            if event.get('event')=='step_update':
                step=event.get('step_update',{})
                if step.get('step_type')=='agent_response' and step.get('text_delta') and first is None:first=round((time.monotonic()-started)*1000)
                if step.get('step_type')=='tool':error='unexpected_tool_stopped'
            if event.get('event')=='result':
                if answer is not None:error='duplicate_result'
                answer=event.get('result')
        submitted=False
        try:
            with (audit/'events.jsonl').open('x',encoding='utf-8') as log:
                os.fchmod(log.fileno(),0o600)
                proc=subprocess.Popen(argv,env=env,cwd=root/'workspace',stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
                submitted=True
                for name,stream in [('stdout',proc.stdout),('stderr',proc.stderr)]:
                    os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ,name)
                while selector.get_map() and not error:
                    if time.monotonic()-started>180:error='timeout_unconfirmed';break
                    for key_event,_ in selector.select(.2):
                        raw=os.read(key_event.fileobj.fileno(),65536)
                        if not raw:selector.unregister(key_event.fileobj);continue
                        total+=len(raw)
                        if total>8*1024*1024:error='output_limit_unconfirmed';break
                        name=key_event.data;buffers[name]+=raw
                        while b'\n' in buffers[name]:
                            rawline,buffers[name]=buffers[name].split(b'\n',1);line=rawline.decode('utf-8',errors='replace')
                            log.write(json.dumps({'channel':name,'elapsed_ms':round((time.monotonic()-started)*1000),'line':line},ensure_ascii=False)+'\n');log.flush()
                            event_line(name,line)
                for name,raw in buffers.items():
                    if raw:event_line(name,raw.decode('utf-8',errors='replace'))
        except (OSError,ValueError,subprocess.SubprocessError):error='execution_interrupted'
        finally:
            selector.close()
            if proc:
                if error and proc.poll() is None:
                    try:os.killpg(proc.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                try:rc=proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:os.killpg(proc.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    rc=proc.wait(timeout=5)
                proc.stdout.close();proc.stderr.close()
        elapsed=round((time.monotonic()-started)*1000)
        success=bool(not error and rc==0 and isinstance(answer,dict) and answer.get('status')=='SUCCESS'
                     and isinstance(answer.get('response'),str) and answer['response'].strip()
                     and isinstance(answer.get('conversation_id'),str) and re.fullmatch(r'[0-9a-f-]{36}',answer['conversation_id'])
                     and (native is None or answer['conversation_id']==native))
        provider_error=str((answer or {}).get('error') or '') if isinstance(answer,dict) else ''
        reason=('verification_required' if re.search(r'verify your account|VALIDATION_REQUIRED',provider_error,re.I) else
                'quota_or_rate_limit' if re.search(r'quota|rate.limit|RESOURCE_EXHAUSTED|\b429\b',provider_error,re.I) else
                'provider_error' if answer and not success else error)
        result={'submitted':submitted,'status':'completed' if success else 'failed' if answer or not submitted else 'unknown',
                'reason':reason,'response':answer.get('response') if isinstance(answer,dict) else None,
                'native':answer.get('conversation_id') if success else native,'first_text_ms':first,'total_ms':elapsed,
                'token_count':(answer.get('usage') or {}).get('total_tokens') if isinstance(answer,dict) else None}
        with (audit/'result.json').open('x',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o600);json.dump(result,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        return result
