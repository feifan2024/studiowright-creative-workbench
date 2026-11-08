"""Read the account panel in an existing managed Flow tab; never create a tab.

No login, navigation, project operations or generation. Uses the same per-account
file lock as upstream tasks; refuses a different online identity.
"""
import asyncio
import hashlib
import re

ACCOUNT_PANEL = re.compile(r'^(账号详情|帐号详情|帳戶詳細資料|Account details|アカウントの詳細)$', re.I)
CLOSE_PANEL = re.compile(r'^(关闭账号面板|Close account panel)$', re.I)
REVEAL_HEADER = """() => {
    const header = document.querySelectorAll('flow-tile-view-header.header-hidden');
    const views = document.querySelectorAll('.cdk-virtual-scrollable.page-container');
    if (header.length !== 1 || views.length !== 1 || views[0].scrollTop <= 0) return null;
    const top = views[0].scrollTop;
    views[0].scrollTo({top:0,behavior:'instant'});
    return top;
}"""
RESTORE_SCROLL = """top => {
    const views = document.querySelectorAll('.cdk-virtual-scrollable.page-container');
    if (views.length === 1 && views[0].scrollTop === 0) views[0].scrollTo({top,behavior:'instant'});
}"""

def parse_balance(text, branded_only=False):
    patterns=(r'^([\d,\u00a0 ]+)\s*个?\s*Google\s*Flow\s*(?:点数|积分|點數|credits?|クレジット|ポイント)$',
              r'^([\d,\u00a0 ]+)\s*(?:AI\s*)?(?:credits?|クレジット|ポイント)$',
              r'^(?:Google\s*Flow\s*)?(?:credits?|点数|积分)\s*[:：]\s*([\d,\u00a0 ]+)$')
    if branded_only:patterns=patterns[:1]
    for pattern in patterns:
        values={int(re.sub(r'[,\s\u00a0]','',m.group(1))) for line in text.splitlines() for m in re.finditer(pattern,line.strip(),re.I) if re.sub(r'[,\s\u00a0]','',m.group(1)).isdigit()}
        if len(values)==1:return values.pop()
    return None

async def fixed_flow_page(browser, profile):
    from task_window_registry import TaskWindowRegistry, canonical_project_url

    pages = [p for cx in browser.contexts for p in cx.pages if p.url.startswith('https://flow.google.com/')]
    if not pages:
        raise ValueError('现有浏览器没有 Flow 页面，保留上次余额。')
    binding = TaskWindowRegistry().get(hashlib.sha256(profile.encode('utf-8')).hexdigest())
    if not binding:
        if len(pages) == 1:
            return pages[0]
        raise ValueError('有多个 Flow 页面，固定任务页尚未登记，保留上次余额。')
    if binding['profile'] == profile and binding['binding_state'] == 'verified':
        for page in pages:
            try:
                if canonical_project_url(page.url) != binding['project_url']:
                    continue
            except ValueError:
                continue
            session = await page.context.new_cdp_session(page)
            try:
                info = (await session.send('Target.getTargetInfo'))['targetInfo']
            finally:
                await session.detach()
            if info['targetId'] == binding['page_target']:
                return page
    raise ValueError('未找到该账号登记的 Flow 固定任务页，保留上次余额。')


def verify_identity(state, email):
    emails = state.get('emails') or []
    if emails and emails != [email]:
        raise ValueError('Flow 页面邮箱与登记账号不符，未读取余额。')
    return bool(email and state.get('signed_in') and emails == [email])


async def read_balance(account, playwright):
    from filelock import FileLock,Timeout
    from playwright.async_api import TimeoutError as BrowserTimeout
    from studiowright import chrome,paths
    from adapter import READ_STATE
    lock=FileLock(str(paths.locks_dir()/f"{account['profile']}.lock"))
    try:lock.acquire(timeout=0)
    except Timeout:raise ValueError('账号正在执行任务，余额稍后更新。') from None
    opened=False;page=None;checked_url=None;scroll_top=None
    try:
        # Do not call upstream attach/ensure_running: that could open a new window.
        browser=await playwright.chromium.connect_over_cdp('http://127.0.0.1:'+str(chrome.debug_port(account['profile'])),timeout=4000,no_defaults=True)
        page=await fixed_flow_page(browser,account['profile'])
        checked_url=page.url
        email=(account.get('email') or '').lower()
        panel=page.get_by_role('button',name=ACCOUNT_PANEL).filter(visible=True)
        close=page.get_by_role('button',name=CLOSE_PANEL).filter(visible=True)
        body=page.locator('body')
        verified=False
        count=0
        for attempt in range(16):
            if page.url!=checked_url:
                raise ValueError('Flow 页面在读取时发生切换，保留上次余额。')
            verified=verify_identity(await page.evaluate(READ_STATE),email)
            if verified:
                if await close.count()==1:
                    break
                count=await panel.count()
                if count==0 and attempt>=3 and scroll_top is None:
                    scroll_top=await page.evaluate(REVEAL_HEADER)
                if count==1:
                    try:
                        await panel.click(timeout=900)
                    except BrowserTimeout:
                        raise ValueError('Flow 账号详情按钮暂不可点，可能有弹窗遮挡，保留上次余额。') from None
                    opened=True
                    break
            await asyncio.sleep(.2)
        else:
            if not verified:
                raise ValueError('Flow 登录信息尚未就绪，保留上次余额。')
            if count>1:
                raise ValueError('Flow 页面有多个账号详情入口，未读取余额。')
            raise ValueError('等待后仍未看到 Flow 账号详情按钮，保留上次余额。')
        for _ in range(12):
            if page.url!=checked_url:
                raise ValueError('Flow 页面在读取时发生切换，保留上次余额。')
            value=parse_balance(await body.inner_text(timeout=750),branded_only=True)
            if value is not None:
                if not verify_identity(await page.evaluate(READ_STATE),email) or page.url!=checked_url:
                    raise ValueError('Flow 页面或账号发生变化，未保存余额。')
                return value
            await asyncio.sleep(.25)
        raise ValueError('页面没有明确显示积分数，未估算余额。')
    finally:
        try:
            if opened and page and not page.is_closed() and page.url==checked_url:
                if await close.count()==1:await close.click(timeout=750)
        except Exception:pass
        finally:
            try:
                if scroll_top is not None and not page.is_closed() and page.url==checked_url:
                    await page.evaluate(RESTORE_SCROLL,scroll_top)
            except Exception:pass
            finally:
                lock.release()
