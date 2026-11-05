"""Recover explicitly crashed idle home pages once; never replay work requests."""
import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from filelock import FileLock, Timeout
import websockets

sys.path.insert(0, str(Path.home()/'.local/share/creative-unified-workbench'))
from shared_session_controller import flow_runtime, state, running_accounts
from flow_browser_state import registry

STATE = Path.home()/'.local/state/creative-unified-workbench/crashed-pages'
SAFE_URLS = {'https://flow.google.com/'}

def receipt(account_key, target_id):
    if STATE.is_symlink():
        raise ValueError('unsafe_state_directory')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    ident = hashlib.sha256((account_key+'\0'+target_id).encode()).hexdigest()
    path = STATE/(ident+'.json')
    try:
        fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(fd,'w') as f:
        json.dump({'account':account_key[:8], 'started':time.time(), 'status':'attempted_once'},f)
        f.flush(); os.fsync(f.fileno())
    return path

async def inspect_account(account_key, profile, port, paths):
    guard=FileLock(str(paths.locks_dir()/(profile+'.lock')))
    try:
        guard.acquire(timeout=0)
    except Timeout:
        return
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version',timeout=3) as response:
            endpoint=json.load(response)['webSocketDebuggerUrl']
        if not endpoint.startswith('ws://127.0.0.1:'):
            return
        async with websockets.connect(endpoint,open_timeout=3,max_size=1024*1024) as ws:
            counter=0
            async def command(method,params=None,session=None,timeout=3):
                nonlocal counter
                counter+=1; ident=counter
                data={'id':ident,'method':method,'params':params or {}}
                if session: data['sessionId']=session
                await ws.send(json.dumps(data))
                events=[]
                try:
                    async with asyncio.timeout(timeout):
                        while True:
                            message=json.loads(await ws.recv())
                            if message.get('id')==ident: return message,events
                            events.append(message)
                except TimeoutError:
                    return {'timeout':True},events
            answer,_=await command('Target.getTargets')
            for target in answer.get('result',{}).get('targetInfos',[]):
                if target.get('type')!='page' or target.get('url') not in SAFE_URLS:
                    continue
                target_id=target['targetId']
                attached,_=await command('Target.attachToTarget',{'targetId':target_id,'flatten':True})
                session=attached.get('result',{}).get('sessionId')
                if not session: continue
                try:
                    _,events=await command('Inspector.enable',session=session)
                    _,more=await command('Runtime.evaluate',{'expression':'1+1','returnByValue':True},session=session,timeout=3)
                    events.extend(more)
                    crashed=any(e.get('method')=='Inspector.targetCrashed' and e.get('sessionId')==session for e in events)
                    if crashed and receipt(account_key,target_id):
                        result,_=await command('Page.reload',session=session,timeout=6)
                        print(json.dumps({'account':account_key[:8], 'event':'crashed_page_reload',
                                          'accepted':'result' in result}),flush=True)
                finally:
                    try: await command('Target.detachFromTarget',{'sessionId':session})
                    except Exception: pass
    finally:
        guard.release()

async def tick():
    chrome,paths=flow_runtime()
    accounts=state()['accounts']
    running=running_accounts(accounts,chrome,paths)
    with registry() as db:
        enrolled={r[0] for r in db.execute("SELECT account_key FROM layouts WHERE status='active'")}
    for account in accounts:
        profile=account.get('profile','')
        account_key=hashlib.sha256(profile.encode()).hexdigest()
        if account_key not in enrolled or profile not in running: continue
        try: await inspect_account(account_key,profile,running[profile],paths)
        except Exception as exc:
            print(json.dumps({'account':account_key[:8], 'event':'inspection_failed',
                              'reason':type(exc).__name__}),flush=True)

if __name__=='__main__':
    asyncio.run(tick())
