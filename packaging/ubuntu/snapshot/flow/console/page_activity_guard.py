"""Capture a visible provider warning before persisting a dispatch hold."""
import asyncio
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

from console_store import Store


WARNING = re.compile(
    r"^(?:unusual activity(?: detected| from your (?:computer )?network)?|"
    r"(?:we(?:'ve| have)?|our systems have) detected unusual (?:activity|traffic)|"
    r"(?:(?:我们的系统|我们)(?:已)?)?检测到(?:了)?异常(?:活动|流量)|"
    r"(?:(?:我們的系統|我們)(?:已)?)?偵測到(?:了)?異常(?:活動|流量)|异常活动|異常活動)"
    r"(?:$|[\s.。!！,:：])", re.I)
WARNING_REGIONS = '[role="alert"], [role="alertdialog"], [role="dialog"], dialog[open], h1, h2, [role="heading"]'
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
MAX_SCREENSHOT = 16 * 1024 * 1024


class PageActivityWarning(ValueError):
    def __init__(self):
        super().__init__('Flow 页面显示异常活动提示，账号已停派，等待人工核查。')


async def stop_on_page_warning(page, root, profile, phase):
    original_url = page.url
    try:
        url = urlsplit(original_url)
        if (url.scheme != 'https' or url.hostname not in {'flow.google.com', 'accounts.google.com'}
                or url.username or url.password or url.port not in (None, 443)):
            return
    except ValueError:
        return
    regions = page.locator(WARNING_REGIONS)
    for index in range(await regions.count()):
        region = regions.nth(index)
        if not await region.is_visible():
            continue
        lines = (await region.inner_text(timeout=1000)).splitlines()
        if any(WARNING.match(line.strip()) for line in lines):
            break
    else:
        return

    screenshot = None
    def persist():
        Store(Path(root) / 'console-data').record_page_incident(profile, phase, screenshot)

    try:
        if page.url == original_url:
            image = await page.screenshot(timeout=3000)
            if page.url == original_url and image.startswith(PNG_MAGIC) and len(image) <= MAX_SCREENSHOT:
                screenshot = image
    except Exception:
        logging.getLogger(__name__).warning('Unable to capture Flow page warning for %s', profile)
    finally:
        # Capture completes before the hold is visible. A failed capture still stops dispatch.
        await asyncio.to_thread(persist)
    raise PageActivityWarning()
