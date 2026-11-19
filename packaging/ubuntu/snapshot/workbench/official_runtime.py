"""Account-pinned official CLI transport; no browser, retry, fallback, or shell."""
import ast,errno,http.client,json,os,re,selectors,signal,subprocess,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from gemini_execution import call_budget
from gemini_effort import NEON,resolve
from gemini_completion import completed_after_capacity_error, RECOVERED

CHANNEL='official_antigravity_cli'
MODEL='gemini-3.8-flash-medium'
TEMPORARY_LAUNCH_FAILURE='temporary_cli_launch_failure'
AUTH_UNSENT='authorization_required_unsent'
BAD_REPLY='confirmed_bad_reply'
INCOMPLETE_REPLY='confirmed_incomplete_reply'
_REFUSALS=frozenset({
    '身为一个语言模型我没办法提供这方面的帮助',
    '我只是一个语言模型不具备这方面的信息或能力因此没法帮到你',
    '作为一个语言模型我无法提供这方面的帮助',
    "asalanguagemodelican'thelpwiththat",
})

def generic_refusal(response):
    # Match only whole known boilerplate replies. Do not classify ordinary answers,
    # policy refusals with a reason, or arbitrary factual claims by keywords.
    return isinstance(response,str) and re.sub(r"[\s，。！、,.!]+",'',response.casefold()) in _REFUSALS
AUTH_MESSAGES=frozenset({
    'Authentication required. Please visit the URL to log in:',
    'Error: authentication timed out.',
})

def auth_failure_proven(markers, answer, stdout_lines):
    return (AUTH_MESSAGES <= markers and stdout_lines == 1 and isinstance(answer,dict)
            and answer.get('status')=='ERROR' and answer.get('error')=='authentication failed or timed out'
            and not answer.get('response') and not answer.get('conversation_id')
            and type(answer.get('num_turns')) is int and answer['num_turns']==0
            and isinstance(answer.get('usage'),dict) and 'total_tokens' in answer['usage']
            and all(type(n) is int and n==0 for n in answer['usage'].values()))
DENY={'command(*)','read_file(*)','write_file(*)','read_url(*)','execute_url(*)','mcp(*)'}
KEY=re.compile(r'[0-9a-f]{64}\Z')

def desktop_authorization_proxy():
    """Read the enrolled Ubuntu desktop's local HTTPS proxy; never change it."""
    def setting(schema,key):
        return ast.literal_eval(subprocess.check_output(
            ['gsettings','get',schema,key],text=True,timeout=1,
            stderr=subprocess.DEVNULL).strip())
    try:
        if setting('org.gnome.system.proxy','mode')!='manual':return None
        schema='org.gnome.system.proxy.https'
        host=setting(schema,'host')
        port=setting(schema,'port')
    except (OSError,ValueError,SyntaxError,subprocess.SubprocessError):
        return None
    if host not in ('localhost','127.0.0.1','::1') or type(port) is not int or not 1<=port<=65535:
        return None
    return host,port


def official_network_env():
    proxy=desktop_authorization_proxy()
    if not proxy:return {}
    host,port=proxy
    url=f'http://{"[::1]" if host=="::1" else host}:{port}'
    return {'HTTPS_PROXY':url,'HTTP_PROXY':url,'NO_PROXY':'localhost,127.0.0.1,::1'}


def check_official_network():
    """Confirm HTTPS connectivity without credentials or a Gemini request."""
    proxy=desktop_authorization_proxy()
    def reachable(host):
        conn=http.client.HTTPSConnection(*(proxy or (host,443)),timeout=3)
        try:
            if proxy:conn.set_tunnel(host,443)
            conn.request('HEAD','/')
            return conn.getresponse().status not in (407,502,503,504)
        except (OSError,http.client.HTTPException):
            return False
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        return all(list(pool.map(reachable,('oauth2.googleapis.com','daily-cloudcode-pa.googleapis.com'))))


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

    def _token_backup(self,key):
        return self.config.parent/'official-token-backups'/f'{key}.token'

    def _restore_token(self,key,token,item):
        """Restore a missing token without overwriting a concurrent CLI refresh.

        A run terminated during the CLI's internal token rotation can leave the
        oauth token file deleted or half-written. The backup taken at the last
        validated read brings the account back without a manual re-login; a
        stale-but-restored token still refreshes on the CLI's next run."""
        backup=self._token_backup(key)
        if backup.is_symlink() or not backup.is_file():return False
        try:
            data=backup.read_bytes()
            payload=json.loads(data)
            identity=payload.get('id_token_email','') if isinstance(payload,dict) else ''
            identity=identity.lower() if isinstance(identity,str) else ''
            if not identity or not isinstance(item.get('email'),str) or identity!=item['email'].lower():
                return False
            temporary=token.with_name(f'.token-{time.time_ns()}.tmp')
            with temporary.open('xb') as stream:
                os.fchmod(stream.fileno(),0o600);stream.write(data)
            try:
                os.link(temporary,token)
            finally:
                temporary.unlink(missing_ok=True)
            print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} restored missing CLI token for {key[:8]} from backup', flush=True)
            return True
        except (OSError,ValueError):return False

    def _backup_token(self,key,token):
        """Keep a private copy of the current validated token for recovery."""
        try:
            backup=self._token_backup(key);data=token.read_bytes()
            payload=json.loads(data)
            item=(self.settings() or {}).get("accounts",{}).get(key,{})
            identity=payload.get("id_token_email") if isinstance(payload,dict) else None
            if not isinstance(identity,str) or not identity or identity.lower()!=str(item.get("email", "")).lower():return
            if backup.is_symlink() or (backup.is_file() and backup.read_bytes()==data):return
            backup.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            temporary=backup.with_name(f'.{time.time_ns()}.tmp')
            with temporary.open('xb') as stream:
                os.fchmod(stream.fileno(),0o600);stream.write(data)
            temporary.replace(backup)
        except (OSError,ValueError):pass

    def account(self,key):
        data=self.settings()
        if not data or key not in data['accounts']:return None
        item=data['accounts'][key];root=Path(item['root'])
        if not root.is_absolute() or root.is_symlink():return None
        home=root/'home';token=home/'.gemini/antigravity-cli/antigravity-oauth-token'
        settings=home/'.gemini/antigravity-cli/settings.json'
        if settings.is_symlink() or not settings.is_file() or settings.stat().st_size>65536:return None
        if token.is_symlink():return None
        if not token.is_file():
            if not self._restore_token(key,token,item):return None
        try:
            payload=json.loads(token.read_text())
            identity=payload.get('id_token_email','') if isinstance(payload,dict) else ''
            identity=identity.lower() if isinstance(identity,str) else ''
        except (OSError,ValueError):
            # An existing partial file may be undergoing a live CLI refresh.
            return None
        if not isinstance(item.get('email'),str) or identity!=item['email'].lower():return None
        if token.stat().st_size>100000:return None
        policy=json.loads(settings.read_text())
        if not DENY<=set(policy.get('permissions',{}).get('deny',[])):return None
        if not (root/'workspace').is_dir():return None
        self._backup_token(key,token)
        return {'root':root,'binary':Path(data['binary']),'email':identity,
                'neon_text_enabled':item.get('neon_text_enabled') is True}

    def neon_text_accounts(self):
        try:
            accounts=(self.settings() or {}).get('accounts',{})
            return {key for key,item in accounts.items() if item.get('neon_text_enabled') is True}
        except (OSError,ValueError,TypeError,AttributeError):
            return set()

    def inventory(self):
        try:data=self.settings()
        except (OSError,ValueError,TypeError):return {}
        if not data:return {}
        result={}
        for key in data['accounts']:
            try:result[key]=bool(self.account(key))
            except (OSError,ValueError,TypeError,KeyError):result[key]=False
        return result

    def run(self,key,prompt,native,audit,*,probe=False,model=None):
        selected=resolve(model)
        account=self.account(key)
        if not account:return {'submitted':False,'status':'failed','reason':'authorization_not_ready'}
        root=account['root'];home=root/'home'
        budget=call_budget(self, 'probe' if probe else 'text')
        env={'HOME':str(home),'XDG_CONFIG_HOME':str(home/'.config'),'XDG_CACHE_HOME':str(home/'.cache'),
             'XDG_DATA_HOME':str(home/'.local/share'),'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TERM':'xterm-256color'}
        env.update(official_network_env())
        if selected == NEON:
            if not account.get('neon_text_enabled'):
                return {'submitted':False,'status':'failed','reason':'model_account_unavailable'}
            # Match the SSH OAuth path used to enroll this headless CLI account.
            env['SSH_TTY']='workbench'
        argv=[str(account['binary']),'--print',prompt,'--output-format','stream-json','--model',selected,
              '--disable-slash-commands','--print-timeout','0']
        if native:argv+=['--conversation',native]
        audit=Path(audit);audit.mkdir(mode=0o700,parents=True,exist_ok=False)
        # A receipt precedes the one process launch; the caller never replays it.
        with (audit/'submission.json').open('x',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o600);json.dump({'account_key':key,'created':time.time(),'native':native,'model':selected,'total_seconds':budget.total_seconds,'idle_seconds':budget.idle_seconds},f);f.flush();os.fsync(f.fileno())
        started=time.monotonic();first=None;answer=None;error=None;rc=None;proc=None
        selector=selectors.DefaultSelector();buffers={'stdout':b'','stderr':b''};total=0
        auth_markers=set();stdout_lines=0;chunks=[];timeout_kind=None;steps=[]
        def event_line(channel,line):
            nonlocal first,answer,error,stdout_lines
            if channel=='stderr':
                if line.strip() in AUTH_MESSAGES:auth_markers.add(line.strip())
                if re.search(r'\bretrying\b|\bretry attempt\b|switching.*model|falling back',line,re.I):error='retry_or_fallback_stopped'
                return
            if line.strip():stdout_lines+=1
            try:event=json.loads(line)
            except ValueError:return
            if event.get('event')=='step_update':
                if answer is not None:error='event_after_terminal_result'
                step=event.get('step_update',{})
                steps.append(step)
                delta=step.get('text_delta')
                if isinstance(delta,str) and delta:
                    budget.progress()
                if step.get('step_type')=='agent_response' and isinstance(delta,str) and delta:
                    chunks.append(delta)
                    if first is None:first=round((time.monotonic()-started)*1000)
                if step.get('step_type')=='tool':error='unexpected_tool_stopped'
            if event.get('event')=='result':
                if answer is not None:error='duplicate_result'
                answer=event.get('result')
        submitted=False
        try:
            with (audit/'events.jsonl').open('x',encoding='utf-8') as log:
                os.fchmod(log.fileno(),0o600)
                try:
                    proc=subprocess.Popen(argv,env=env,cwd=root/'workspace',stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
                except OSError as exc:
                    # These errors at process creation mean the CLI did not run.
                    if exc.errno in (errno.EAGAIN,errno.ETXTBSY):error=TEMPORARY_LAUNCH_FAILURE
                    raise
                submitted=True
                for name,stream in [('stdout',proc.stdout),('stderr',proc.stderr)]:
                    os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ,name)
                while selector.get_map() and not error:
                    timeout_kind=budget.expired()
                    if timeout_kind:error='timeout_unconfirmed';break
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
        except (OSError,ValueError,subprocess.SubprocessError):error=error or 'execution_interrupted'
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
        recovered=bool(not error and rc in (0,1) and completed_after_capacity_error(answer,steps,native))
        terminal=bool(not error and (rc==0 or recovered) and isinstance(answer,dict) and (answer.get('status')=='SUCCESS' or recovered)
                     and isinstance(answer.get('conversation_id'),str) and re.fullmatch(r'[0-9a-f-]{36}',answer['conversation_id'])
                     and (native is None or answer['conversation_id']==native))
        response=answer.get('response') if isinstance(answer,dict) else None
        nonempty=isinstance(response,str) and bool(response.strip())
        finish_reason=answer.get('finish_reason') if terminal else None
        incomplete=terminal and (not nonempty or finish_reason in ('MAX_TOKENS','max_tokens','length'))
        bad=terminal and not incomplete and generic_refusal(response)
        success=terminal and not incomplete and not bad
        provider_error=str((answer or {}).get('error') or '') if isinstance(answer,dict) else ''
        print_timeout=(not terminal and isinstance(answer,dict) and
                       (answer.get('finished_reason')=='print_timeout'
                        or bool(re.match(r'^print timeout after\b',provider_error,re.I))))
        reason=('timeout_unconfirmed' if print_timeout else
                'verification_required' if re.search(r'verify your account|VALIDATION_REQUIRED',provider_error,re.I) else
                'quota_or_rate_limit' if re.search(r'quota|rate.limit|RESOURCE_EXHAUSTED|\b429\b',provider_error,re.I) else
                'provider_connection_failed' if re.search(r'ECONNREFUSED|ECONNRESET|ENOTFOUND|ETIMEDOUT|fetch failed|network error',provider_error,re.I) else
                'provider_unavailable' if re.search(r'\b50[0234]\b|SERVICE_UNAVAILABLE|upstream unavailable',provider_error,re.I) else
                'provider_error' if answer and not success else error)
        if submitted and not error and first is None and auth_failure_proven(auth_markers,answer,stdout_lines):
            submitted=False;reason=AUTH_UNSENT
        if success:reason=None
        if incomplete or bad:reason=INCOMPLETE_REPLY if incomplete else BAD_REPLY
        explicit_failure=not error and not print_timeout and isinstance(answer,dict) and answer.get('status')=='ERROR' and first is None and not nonempty
        result={'submitted':submitted,'status':'completed' if success else 'failed' if terminal or explicit_failure or not submitted else 'unknown',
                'reason':reason,'response':response if nonempty else ''.join(chunks) or None,
                'native':answer.get('conversation_id') if terminal else native,'first_text_ms':first,'total_ms':elapsed,
                'timeout_kind':timeout_kind,
                'token_count':(answer.get('usage') or {}).get('total_tokens') if isinstance(answer,dict) else None}
        result['capacity_retry_safe']=bool(explicit_failure and answer.get('finished_reason')=='error' and rc in (0,1) and not response and not chunks
            and reason=='provider_unavailable' and 'No capacity available for model ' in provider_error
            and 'UNAVAILABLE' in provider_error and re.search(r'\b503\b',provider_error))
        result['returncode']=rc
        if recovered and success:
            result.update(completion_source=RECOVERED,provider_warning='recovered_capacity_503')
        # Set only for a terminal CLI receipt, after that process has exited.
        if print_timeout and not error and submitted:
            result['cli_print_timeout']=True
        with (audit/'result.json').open('x',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o600);json.dump(result,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        return result
