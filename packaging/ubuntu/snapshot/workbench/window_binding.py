"""Find a unique existing browser page in an account's registered desktop."""
import asyncio
from urllib.parse import urlparse
from foreground_input import Target


def _trusted_url(url, hostname):
    try:
        parsed=urlparse(url)
        return (parsed.scheme=='https' and parsed.hostname==hostname and
                not parsed.username and not parsed.password and parsed.port in (None,443))
    except (ValueError,TypeError):
        return False


async def find_page_and_target(*, context, account_key, hostname, desired_url, layout_call, preferred_page=None):
    """Return only a page whose OS window maps uniquely to this account."""
    if hostname == 'flow.google.com':
        from flow_task_window import find_flow_page
        return await find_flow_page(context=context, account_key=account_key,
            desired_url=desired_url, layout_call=layout_call, preferred_page=preferred_page)
    layouts=await asyncio.to_thread(layout_call,'Status')
    found=[p for p in layouts if p.get('key')==account_key and not p.get('error')]
    if len(found)!=1:raise ValueError('desktop_binding_unconfirmed')
    binding=found[0];pid=binding.get('pid');workspace=binding.get('workspace')
    if type(pid) is not int or pid<=0 or type(workspace) is not int or workspace<0:
        raise ValueError('desktop_binding_unconfirmed')
    windows=await asyncio.to_thread(layout_call,'List',pid)
    candidates=[]
    for page in context.pages:
        if not _trusted_url(page.url,hostname):continue
        try:
            title=await page.title()
            visible=await page.evaluate('() => document.visibilityState === "visible"')
        except Exception:
            continue
        if not visible:continue
        if not title:continue
        matches=[w for w in windows if w.get('workspace')==workspace and w.get('id')==binding.get('window_id') and
            w.get('title') in (title,title+' - Google Chrome',title+' - Chromium')]
        if len(matches)==1 and type(matches[0].get('id')) is int:
            candidates.append((page,matches[0]['id']))
    exact=[x for x in candidates if x[0].url.split('?',1)[0].split('#',1)[0]==desired_url]
    choices=exact if exact else candidates
    if len(choices)!=1:raise ValueError('desktop_page_ambiguous')
    page,window_id=choices[0]
    return page,Target(account_key,pid,window_id,workspace)
