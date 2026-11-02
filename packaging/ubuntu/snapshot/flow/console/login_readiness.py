"""Resume a known Google web session; never enter credentials or solve challenges."""
import asyncio
import json
import re
from urllib.parse import urlsplit

ENTRY='https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fflow.google.com%2Fprojects'
GOOGLE_STATE=r"""() => ({
 password:!!document.querySelector('input[type="password"]'),
 email:!!document.querySelector('input[type="email"]'),
 rows:[...document.querySelectorAll('[data-email]')].map(n=>({
   email:(n.getAttribute('data-email')||'').trim().toLowerCase(),
   signed_out:/signed\s*out|已退出|已登出|已注销/i.test((n.closest('[data-identifier]')||n.closest('li')||n).innerText||'')
 }))
})"""

async def read_when_ready(page, script, *, attempts=5, pause=asyncio.sleep):
    """Retry only a read interrupted by browser navigation, never an action."""
    from playwright.async_api import Error
    for attempt in range(attempts):
        try:
            return await page.evaluate(script)
        except Error as error:
            if not any(text in str(error) for text in (
                'Execution context was destroyed', 'Cannot find context with specified id'
            )):
                raise
            if attempt == attempts-1:
                raise ValueError('页面持续跳转，登录检查暂未完成；未判定账号失效，未提交生成。') from error
            await pause(.5)

async def resume_google_session(context, expected, read_flow, *, attempts=16, pause=asyncio.sleep):
    expected=(expected or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}',expected):
        return {'status':'unknown','reason':'没有已登记的目标邮箱，未选择其他账号。'}
    page=await context.new_page()
    selected=False
    try:
        await page.goto(ENTRY,wait_until='domcontentloaded',timeout=30000)
        for _ in range(attempts):
            route=urlsplit(page.url)
            if route.hostname=='flow.google.com':
                state=await read_when_ready(page,read_flow,pause=pause)
                if state.get('signed_in') and state.get('emails')==[expected]:
                    return {'status':'resumed','reason':'Google 已有会话已恢复到 Flow。'}
                if state.get('emails') and state['emails']!=[expected]:
                    return {'status':'mismatch','reason':'Google 返回的邮箱与目标账号不一致，未继续操作。'}
            elif route.hostname=='accounts.google.com':
                state=await read_when_ready(page,GOOGLE_STATE,pause=pause)
                if state.get('password') or '/challenge/pwd' in route.path:
                    return {'status':'reauth_required','reason':'Google 要求重新输入此账号的密码。'}
                rows=[r for r in state.get('rows',[]) if r.get('email')==expected]
                if len(rows)==1:
                    if rows[0].get('signed_out'):
                        return {'status':'reauth_required','reason':'Google 明确显示此账号已退出登录。'}
                    if not selected:
                        # Only the known account row. No password, OTP, CAPTCHA,
                        # account creation, project creation or generation controls.
                        await page.locator('[data-email='+json.dumps(expected)+']').click(timeout=5000)
                        selected=True
                elif state.get('rows'):
                    return {'status':'mismatch','reason':'Google 的账号列表没有唯一匹配的目标邮箱，未选择其他账号。'}
                elif state.get('email') or '/challenge/' in route.path:
                    return {'status':'reauth_required','reason':'Google 要求本人重新完成登录验证。'}
            else:
                return {'status':'unknown','reason':'登录检查跳到了预期之外的站点，未继续操作。'}
            await pause(1)
        return {'status':'unknown','reason':'Google 登录检查尚未确认结果；没有自动输入任何凭证。'}
    except Exception:
        return {'status':'unknown','reason':'Google 登录检查未完成，可能是网络或页面加载问题；未判定账号失效。'}
    finally:
        await page.close()
