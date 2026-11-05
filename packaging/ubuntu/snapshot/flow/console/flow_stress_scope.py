"""Private, expiring, exact-request admission for one nine-account image or video batch.

Ordinary dispatch remains 1..5. No account selection, retry or provider calls here.
An invalid/pending manifest pauses admission rather than falling back to normal.
"""
import json
import os
import re
import stat
import time
from pathlib import Path
from dispatch_limit import _open, _lock, Slot

NAME='flow-stress-once.json'

def read_scope(root):
    path=Path(root)/NAME
    if not path.exists() and not path.is_symlink():return None
    blocked={'ready':False,'requests':{}}
    try:
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
            return blocked
        if info.st_size>24576:return blocked
        data=json.loads(path.read_text())
        requests=data['requests']; now=time.time()
        if (data.get('schema')!=1 or data.get('maximum')!=9 or data.get('phase')!='ready'
                or not now-1800 <= data['created'] <= now+5
                or not now < data['expires'] <= data['created']+1800
                or not isinstance(requests,dict) or len(requests)!=9):return blocked
        if len({r['profile'] for r in requests.values()})!=9:return blocked
        for key,item in requests.items():
            if not re.fullmatch(r'[0-9a-f-]{36}',key):return blocked
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',item['profile']):return blocked
            if not re.fullmatch(r'[0-9a-f-]{36}',item['project']):return blocked
            if not re.fullmatch(r'[0-9a-f]{64}',item['params_digest']):return blocked
        return {**data,'ready':True}
    except (OSError,ValueError,KeyError,TypeError):return blocked

def acquire_scope_slot(limit,scope):
    if not scope.get('ready') or limit.marker.exists() or limit.marker.is_symlink():return None
    held=[]
    try:
        with limit._guard(timeout=0):
            current=read_scope(limit.root)
            if not current or not current.get('ready') or current['batch_id']!=scope['batch_id']:return None
            # Do not overlap a previously admitted ordinary operation.
            active,ordinary=limit._slots()
            for fd in ordinary:os.close(fd)
            if active:return None
            for index in range(9):
                fd=_open(limit.root/('stress-slot-'+str(index)+'.lock'))
                if _lock(fd):
                    held.append(fd)
                    return Slot(held.pop(),limit.root)
                os.close(fd)
    except (OSError,ValueError):return None
    finally:
        for fd in held:os.close(fd)
    return None

def validate_task(task,scope,store):
    import hashlib
    item=scope['requests'].get(task['request_key'])
    return bool(item and task['kind'] in ('image','video') and task['kind']==scope.get('kind','image') and task['profile']==item['profile']
        and task['project']==item['project'] and task['params'].get('count')==1
        and hashlib.sha256(json.dumps(task['params'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()==item['params_digest']
        and store.retry_detail(task['id']) is None)
