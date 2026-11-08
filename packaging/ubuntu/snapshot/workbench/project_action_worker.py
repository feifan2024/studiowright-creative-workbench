"""Per-request Flow project actions. Invoked only by an explicit workbench operation."""
import asyncio, hashlib, json, os, re, sqlite3, sys, tempfile, time
from pathlib import Path

root = Path.home() / '.local/share/studiowright-evaluation'
os.environ['STUDIOWRIGHT_HOME'] = str(root / 'data')
os.environ['STUDIOWRIGHT_OUTPUT'] = str(root / 'output')
import shared_session_controller as controller
chrome, paths = controller.flow_runtime()
from filelock import FileLock, Timeout
from adapter import READ_STATE
from browser_rendering import RenderingStudio
from material_library import Library
from studiowright.flow.page import FlowPage, ReplyWatch, LIST_RPC
from studiowright.flow import batchexecute as be, replies

class SafeError(ValueError):
    pass

def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)

def existing(library, project, media):
    with library.connect() as db:
        rows = db.execute('''SELECT a.sha256,a.relative_path FROM origins o JOIN assets a ON a.id=o.asset_id
                             WHERE o.project_id=? AND o.media_id=?''', (project,media)).fetchall()
    for row in rows:
        try:
            path = (library.root / row['relative_path']).resolve(strict=True)
            if not path.is_relative_to(library.root / 'objects') or not path.is_file(): continue
            with path.open('rb') as handle: digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            if digest == row['sha256']: return digest
        except (OSError, ValueError, RuntimeError): continue
    return None

async def run(data):
    profile, project, email, kind, op = (data[k] for k in ('profile','project','email','kind','id'))
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',profile)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',project)
            or kind not in ('transfer','delete') or not isinstance(email,str) or not email):
        raise SafeError('项目或账号信息不完整。')
    lock = FileLock(str(paths.locks_dir() / (profile + '.lock')), thread_local=False)
    try: lock.acquire(timeout=0)
    except Timeout: raise SafeError('该账号正在使用，请任务结束后再操作。') from None
    try:
        state = controller.state()
        running = controller.running_accounts(state['accounts'], chrome, paths)
        if profile not in running: raise SafeError('账号浏览器未连接，请先检查登录。')
        studio = RenderingStudio(profile); studio.configure_task(op)
        async with studio:
            context = studio.session.context
            source_pages = [p for p in context.pages if p.url.startswith('https://flow.google.com/')]
            identities = [await p.evaluate(READ_STATE) for p in source_pages]
            if not any(isinstance(x,dict) and x.get('signed_in') and x.get('emails') == [email.strip().lower()] for x in identities):
                raise SafeError('无法确认登录账号，未操作项目。')
            if any('/project/'+project in p.url for p in source_pages):
                raise SafeError('项目正在其他窗口使用，请关闭该项目窗口后再操作。')
            if kind == 'delete': return await delete_project(studio, data)
            return await transfer(studio, data)
    finally: lock.release()

async def transfer(studio, data):
    project,profile,email=data['project'],data['profile'],data['email']
    flow = await FlowPage.open(studio.session, project, role='project-transfer')
    async with ReplyWatch(flow.page,[LIST_RPC]) as watch:
        await flow.page.goto(flow.url, wait_until='domcontentloaded', timeout=30000)
        end=time.monotonic()+15
        while not watch.bodies and not watch.unreadable and time.monotonic()<end: await watch.wait(.4)
    parsed=[]
    for _,status,text in watch.bodies:
        if status != 200: raise SafeError('云端素材清单读取失败，未开始转移。')
        parsed.append(replies.project_media(be.parse_reply(text,LIST_RPC),project))
    if not parsed or watch.unreadable: raise SafeError('云端素材清单未能完整读取，请稍后核对。')
    items=max(parsed,key=len)
    if not items and await flow.page.locator('flow-image-tile,flow-video-tile').count():
        raise SafeError('素材清单与页面不一致，未开始转移。')
    library=Library(root/'material-library'); added=[]; skipped=0; failures=[]
    emit(phase='transfer',total=len(items),done=0,added=0,skipped=0,failed=0)
    with tempfile.TemporaryDirectory(prefix='project-transfer-') as temp:
        folder=Path(temp)
        for index,item in enumerate(items):
            media=item.media_id
            new_id=None
            try:
                if existing(library,project,media):
                    skipped+=1
                else:
                    urls=await flow.edit_view_urls(media)
                    url=urls.get('video') or urls.get('image')
                    from urllib.parse import urlsplit
                    parsed_url=urlsplit(url or '')
                    if parsed_url.scheme!='https' or parsed_url.hostname!='flow-content.google':
                        raise SafeError('素材下载地址未确认')
                    path=await flow.download(url,folder/(media+('.mp4' if urls.get('video') else '.jpg')))
                    with path.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
                    with library.connect() as db: already=bool(db.execute('SELECT 1 FROM assets WHERE sha256=?',(digest,)).fetchone())
                    ident=library.archive(path,{'source':'flow-project-transfer','source_id':project+':'+media,
                        'account_id':profile,'account_email':email,'project_id':project,'media_id':media,
                        'prompt':item.title or '', 'created':str(item.created or ''),'details':{'transfer':'project'}},expected=digest)
                    path.unlink()
                    if already: skipped+=1
                    else: added.append(ident);new_id=ident
            except Exception:
                failures.append(media)
            emit(phase='transfer',total=len(items),done=index+1,added=len(added),skipped=skipped,failed=len(failures),new_id=new_id)
    emit(phase='transfer_result',total=len(items),added_ids=added,skipped=skipped,failed=len(failures))

async def delete_project(studio, data):
    profile,project=data['profile'],data['project']
    page=await studio.session.page_for('https://flow.google.com/',role='project-delete')
    await page.goto('https://flow.google.com/',wait_until='domcontentloaded',timeout=30000)
    card=page.locator('flow-project-card').filter(has=page.locator('a[href*="/project/'+project+'"]'))
    await card.wait_for(state='visible',timeout=15000)
    if await card.count()!=1:raise SafeError('无法唯一确认云端项目。')
    button=card.get_by_role('button',name='删除项目',exact=True)
    if await button.count()!=1:raise SafeError('云端删除操作暂不可用。')
    await button.click(timeout=5000)
    dialog=page.get_by_role('dialog');await dialog.wait_for(timeout=5000)
    confirm=dialog.get_by_role('button',name=re.compile(r'^(删除|确认删除|删除项目)$'))
    if await dialog.count()!=1 or await confirm.count()!=1:raise SafeError('无法确认云端删除提示。')
    q=sqlite3.connect(root/'console-data/console.db',timeout=5,isolation_level=None)
    s=sqlite3.connect(root/'data/studiowright.db',timeout=5,isolation_level=None)
    try:
        q.execute('BEGIN IMMEDIATE');s.execute('BEGIN IMMEDIATE')
        if not q.execute('select 1 from projects where profile=? and id=?',(profile,project)).fetchone():raise SafeError('项目登记已变化。')
        queue = 'flow_task_execution_rows' if q.execute("SELECT 1 FROM sqlite_master WHERE name='flow_task_execution_rows' AND type='view'").fetchone() else 'tasks'
        if q.execute(f"select 1 from {queue} where profile=? and project=? and status in ('queued','running')",(profile,project)).fetchone():raise SafeError('项目有进行中的任务。')
        if s.execute("select 1 from jobs where profile=? and project_id=? and status in ('queued','running')",(profile,project)).fetchone():raise SafeError('项目有进行中的任务。')
        if any('/project/'+project in p.url for p in studio.session.context.pages if p!=page):raise SafeError('项目正在其他窗口使用。')
        emit(phase='delete_submitted')
        await confirm.click(timeout=5000)
        await card.wait_for(state='detached',timeout=15000)
        await page.goto('https://flow.google.com/project/'+project,wait_until='domcontentloaded',timeout=15000)
        for _ in range(40):
            if page.url.split('?')[0].rstrip('/').endswith('/404'):break
            await asyncio.sleep(.25)
        else:raise SafeError('云端删除结果未确认，请核对，勿重复删除。')
        q.execute('delete from projects where profile=? and id=?',(profile,project))
        if q.execute('select changes()').fetchone()[0]!=1:raise SafeError('本地项目登记状态未确认。')
        q.execute('delete from flow_retry_projects where profile=? and project=?',(profile,project))
        s.execute('delete from settings where key=? and value=?',('project.default.'+profile,project))
        s.execute('COMMIT');q.execute('COMMIT');emit(phase='deleted')
    finally:
        for db in (q,s):
            if db.in_transaction:db.execute('ROLLBACK')
            db.close()

if __name__=='__main__':
    try:asyncio.run(run(json.load(sys.stdin)))
    except Exception as exc:
        emit(phase='error', message=str(exc) if isinstance(exc,SafeError) else '操作未完成，请检查账号连接与项目状态。')
        sys.exit(1)
