"""Read the account panel in an existing managed Flow tab; never create a tab.

No login, navigation, project operations or generation. Uses the same per-account
file lock as upstream tasks; refuses a different online identity.
"""
import asyncio
import re

def parse_balance(text, branded_only=False):
    patterns=(r'^([\d,\u00a0 ]+)\s*个?\s*Google\s*Flow\s*(?:点数|积分|點數|credits?|クレジット|ポイント)$',
              r'^([\d,\u00a0 ]+)\s*(?:AI\s*)?(?:credits?|クレジット|ポイント)$',
              r'^(?:Google\s*Flow\s*)?(?:credits?|点数|积分)\s*[:：]\s*([\d,\u00a0 ]+)$')
    if branded_only:patterns=patterns[:1]
    for pattern in patterns:
        values={int(re.sub(r'[,\s\u00a0]','',m.group(1))) for line in text.splitlines() for m in re.finditer(pattern,line.strip(),re.I) if re.sub(r'[,\s\u00a0]','',m.group(1)).isdigit()}
        if len(values)==1:return values.pop()
    return None

async def read_balance(account, playwright):
    from filelock import FileLock,Timeout
    from studiowright import chrome,paths
    from adapter import READ_STATE
    lock=FileLock(str(paths.locks_dir()/f"{account['profile']}.lock"))
    try:lock.acquire(timeout=0)
    except Timeout:raise ValueError('账号正在执行任务，余额稍后更新。') from None
    opened=False;page=None
    try:
        # Do not call upstream attach/ensure_running: that could open a new window.
        browser=await playwright.chromium.connect_over_cdp('http://127.0.0.1:'+str(chrome.debug_port(account['profile'])),timeout=4000,no_defaults=True)
        pages=[p for cx in browser.contexts for p in cx.pages if p.url.startswith('https://flow.google.com/')]
        if not pages:raise ValueError('现有浏览器没有Flow页面，保留上次余额。')
        page=pages[0];state=await page.evaluate(READ_STATE)
        if not state.get('signed_in') or state.get('emails')!=[(account.get('email') or '').lower()]:
            raise ValueError('线上邮箱尚未匹配，未读取或修改账号登录。')
        text=await page.locator('body').inner_text(timeout=3000)
        value=parse_balance(text,branded_only=True)
        if value is not None:return value
        close=page.get_by_role('button',name=re.compile(r'^(关闭账号面板|Close account panel)$'))
        already_open=await close.count()>0 and await close.first.is_visible()
        if not already_open:
            panel=page.get_by_role('button',name=re.compile(r'^(账号详情|Account details|アカウントの詳細)$'))
            if await panel.count()!=1:raise ValueError('未找到唯一的账号详情入口。')
            await panel.click(timeout=4000,force=True);opened=True
        for _ in range(12):
            value=parse_balance(await page.locator('body').inner_text(timeout=3000),branded_only=True)
            if value is not None:
                state=await page.evaluate(READ_STATE)
                if state.get('emails')!=[(account.get('email') or '').lower()]:raise ValueError('账号发生变化，未保存余额。')
                return value
            await asyncio.sleep(.25)
        raise ValueError('页面没有明确显示积分数，未估算余额。')
    finally:
        if opened and page and not page.is_closed():
            try:
                close=page.get_by_role('button',name=re.compile(r'^(关闭账号面板|Close account panel)$'))
                if await close.count()==1:await close.click(timeout=3000,force=True)
            except Exception:pass
        lock.release()
