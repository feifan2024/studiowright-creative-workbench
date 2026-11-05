"""Recover only enrolled Flow browsers; never arrange windows or restore Gemini."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from flow_browser_state import gui_env,mark,registry,flow_only_ready
from activation_browser import main

def network_ready():
    """Wait for a configured local proxy before attempting any account recovery."""
    try:
        enabled=subprocess.run(['systemctl','is-enabled','clash-verge-service.service'],capture_output=True,text=True,timeout=3)
        clash_enabled=enabled.returncode==0 and enabled.stdout.strip() in ('enabled','enabled-runtime')
        if clash_enabled:
            active=subprocess.run(['systemctl','is-active','clash-verge-service.service'],capture_output=True,text=True,timeout=3)
            if active.returncode!=0: return False
        env=gui_env()
        def setting(schema,name):
            p=subprocess.run(['gsettings','get',schema,name],env=env,capture_output=True,text=True,timeout=3)
            if p.returncode: raise ValueError('proxy_settings_unavailable')
            return p.stdout.strip().strip("'")
        if setting('org.gnome.system.proxy','mode')=='manual' or clash_enabled:
            host=setting('org.gnome.system.proxy.http','host')
            if host in ('127.0.0.1','localhost','::1'):
                port=int(setting('org.gnome.system.proxy.http','port'))
                with socket.create_connection((host,port),timeout=2): pass
        return True
    except Exception:
        return False

def recover_tick():
    if not network_ready():
        return  # Polling readiness never opens an account or submits a request.
    with registry() as db:
        rows=db.execute("SELECT account_key FROM layouts WHERE status='active'").fetchall()
    for (key,) in rows:
        try:
            if flow_only_ready(key): continue
        except Exception:
            continue
        with registry() as db:
            claimed=db.execute("UPDATE layouts SET status='recovering',changed=?,reason=NULL WHERE account_key=? AND status='active'",(time.time(),key)).rowcount
        if not claimed: continue
        try:
            result=main(key)
            if result.get('result')!='browser_ready': raise ValueError('flow_recovery_unconfirmed')
        except Exception as exc:
            mark(key,'blocked',str(exc)[:100])

def run():
    os.environ['STUDIOWRIGHT_DESKTOP_RECOVERY']='1'
    # An interrupted account operation is never replayed automatically.
    with registry() as db:
        db.execute("UPDATE layouts SET status='blocked',reason='recovery_interrupted',changed=? WHERE status='recovering'",(time.time(),))
    while True:
        try: recover_tick()
        except Exception: pass
        time.sleep(30)

if __name__=='__main__': run()
