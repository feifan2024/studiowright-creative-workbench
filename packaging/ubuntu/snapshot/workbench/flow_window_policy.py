"""Enforce one Flow page/window per account without deleting provider data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

INTERVAL = 1800
ROOT = Path.home()/'.local/state/creative-unified-workbench/task-coordination'
SAFETY_JS = '''() => {
 const visible=e=>!!e.getClientRects().length;
 return {uploads:[...document.querySelectorAll('input[type=file]')].some(e=>e.files&&e.files.length),
 busy:[...document.querySelectorAll('[role=progressbar],[aria-busy=true]')].some(visible),
 dialog:[...document.querySelectorAll('[role=dialog],dialog[open]')].some(visible)};
}'''

class Lifecycle:
    def __init__(self, root=None):
        self.root=Path(root) if root else ROOT; self.path=self.root/'window-lifecycle.sqlite3'
        if self.root.is_symlink() or self.path.is_symlink():raise ValueError('window_lifecycle_unavailable')
    def connect(self):
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        db.executescript('''CREATE TABLE IF NOT EXISTS lifecycle(profile TEXT PRIMARY KEY,
          observed REAL,flow_windows INTEGER,flow_pages INTEGER,protected INTEGER,unique_ok INTEGER,
          reason TEXT,last_activity REAL,last_keepalive REAL,last_attempt REAL,next_due REAL,last_outcome TEXT);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,profile TEXT,action TEXT,detail TEXT,created REAL);''')
        self.path.chmod(0o600);return db
    def observe(self,profile,windows,pages,protected=0,reason=None):
        now=time.time()
        with closing(self.connect()) as db,db:
            db.execute('INSERT INTO lifecycle VALUES(?,?,?,?,?,?,?, ?,NULL,NULL,?,NULL) ON CONFLICT(profile) DO UPDATE SET observed=excluded.observed,flow_windows=excluded.flow_windows,flow_pages=excluded.flow_pages,protected=excluded.protected,unique_ok=excluded.unique_ok,reason=excluded.reason',
                       (profile,now,windows,pages,protected,int(windows==pages==1 and not protected and not reason),reason,now,now+INTERVAL))
    def event(self,profile,action,**detail):
        with closing(self.connect()) as db,db:db.execute('INSERT INTO events(profile,action,detail,created) VALUES(?,?,?,?)',(profile,action,json.dumps(detail,sort_keys=True),time.time()))
    def touch(self,profile):
        now=time.time()
        with closing(self.connect()) as db,db:db.execute('UPDATE lifecycle SET last_activity=?,next_due=? WHERE profile=?',(now,now+INTERVAL,profile))
    def attempt(self,profile,outcome,success=False):
        now=time.time()
        with closing(self.connect()) as db,db:
            db.execute('UPDATE lifecycle SET last_attempt=?,last_outcome=?,last_keepalive=CASE WHEN ? THEN ? ELSE last_keepalive END,next_due=? WHERE profile=?',(now,outcome,int(success),now,now+INTERVAL,profile))
            db.execute('INSERT INTO events(profile,action,detail,created) VALUES(?,?,?,?)',(profile,'keepalive_result',json.dumps({'outcome':outcome,'success':success}),now))
    def status(self):
        if not self.path.exists():return []
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.row_factory=sqlite3.Row;return [dict(r) for r in db.execute('SELECT * FROM lifecycle ORDER BY profile')]

def trusted_flow(url):
    try:
        u=urlparse(url);return u.scheme=='https' and u.hostname=='flow.google.com' and not u.username and not u.password and u.port in (None,443)
    except (TypeError,ValueError):return False

async def safe_page(session,page):
    await session._safe_editor(page)
    flags=await page.evaluate(SAFETY_JS)
    for key in ('uploads','busy','dialog'):
        if flags.get(key):raise ValueError('flow_window_protected_'+key)

async def inventory(session):
    rows=[]
    for page in session.context.pages:
        if page.is_closed() or not trusted_flow(page.url):continue
        rows.append({'page':page,'target':await session._target_id(page),'window':await session.window_id(page),'role':await session.role_of(page)})
    return rows

async def ensure_unique(session,profile,*,task_id=None):
    from task_coordinator import Coordinator,CoordinationBlocked,record
    from flow_task_window import registration, verified_task_binding
    from task_window_registry import canonical_project_url
    from flow_browser_state import REGISTRY
    lifecycle=Lifecycle();coordinator=Coordinator();old=coordinator.window(profile)
    rows=await inventory(session);count=len({r['window'] for r in rows})
    lifecycle.observe(profile,count,len(rows))
    if old and old['state'] in ('submitting','submitted','generated','result_unknown','needs_verification') and old['task_id']!=task_id:
        lifecycle.observe(profile,count,len(rows),len(rows),'previous_task_result_unconfirmed')
        raise CoordinationBlocked('previous_task_result_unconfirmed')
    key=hashlib.sha256(profile.encode()).hexdigest();registered=registration(key)
    if not registered or registered['port']!=session.port or any(r['target'] not in registered['targets'] for r in rows):
        raise CoordinationBlocked('flow_page_account_mismatch')
    # Check every candidate before closing even one. A protected page means the
    # uniqueness requirement is visibly unmet, never "fixed" by opening another.
    for r in rows:
        try:await safe_page(session,r['page'])
        except Exception:
            lifecycle.observe(profile,count,len(rows),1,'flow_window_protected')
            raise
    if not rows:
        pages=[p for p in session.context.pages if not p.is_closed()]
        if any(urlparse(p.url).hostname in ('accounts.google.com','myaccount.google.com') for p in pages):
            raise CoordinationBlocked('task_window_requires_login')
        blank=[p for p in pages if p.url=='about:blank']
        if len(blank)>1:raise CoordinationBlocked('multiple_account_task_windows')
        if blank:
            page=blank[0]
        else:
            page=await session._new_window()
        return page
    binding=verified_task_binding(key,registered)
    fixed=[r for r in rows if binding and r['target']==binding['page_target']]
    preferred=[r for r in rows if old and r['target']==old['target_id']]
    tagged=[r for r in rows if r['role']=='account-flow-task']
    if fixed:keep=fixed[0]
    elif preferred:keep=preferred[0]
    elif len(tagged)==1:keep=tagged[0]
    elif len(tagged)>1:raise CoordinationBlocked('multiple_account_task_windows')
    else:keep=rows[0]  # Existing verified empty page, not a new window.
    if len(rows)>1:
        try:
            project=canonical_project_url(keep['page'].url)
            if any(canonical_project_url(r['page'].url)!=project for r in rows):
                raise ValueError('different_flow_projects')
        except ValueError:
            lifecycle.observe(profile,count,len(rows),reason='flow_window_not_unique')
            raise CoordinationBlocked('flow_window_not_unique') from None
    for r in rows:
        if r is keep:continue
        await safe_page(session,keep['page'])
        await safe_page(session,r['page'])
        if keep['page'].is_closed():raise CoordinationBlocked('task_window_identity_changed')
        if canonical_project_url(keep['page'].url)!=project or canonical_project_url(r['page'].url)!=project:
            raise CoordinationBlocked('task_window_identity_changed')
        await r['page'].close()
        lifecycle.event(profile,'duplicate_empty_flow_page_closed',target=r['target'],chrome_window=r['window'])
        record('duplicate_empty_flow_page_closed')
    final=await inventory(session)
    if len(final)!=1 or final[0]['target']!=keep['target']:
        lifecycle.observe(profile,len({r['window'] for r in final}),len(final),reason='flow_window_not_unique')
        raise CoordinationBlocked('flow_window_not_unique')
    await keep['page'].evaluate('(name)=>{window.name=name}', 'sw-account-flow-task')
    # The old homepage registry must point at the retained Chrome window too.
    with closing(sqlite3.connect(REGISTRY,timeout=5)) as db,db:
        db.execute('UPDATE flow_windows SET window_id=? WHERE account_key=? AND pid=? AND port=?',(keep['window'],key,registered['pid'],registered['port']))
    if task_id:
        coordinator.bind(profile,task_id,keep['target'])
    else:
        with closing(coordinator.connect()) as db,db:
            db.execute('UPDATE windows SET target_id=?,updated=? WHERE profile=? AND state IN (\'idle\',\'blocked_draft\')',(keep['target'],time.time(),profile))
    lifecycle.observe(profile,1,1)
    return keep['page']
