"""Read the existing fixed task page when Playwright has no main frame metadata."""
import base64
import json


class FixedBalancePage:
    def __init__(self, page, session, url):
        self.page, self.session, self.url = page, session, url

    async def evaluate(self, function, arg=None):
        result = await self.session.send('Runtime.evaluate', {
            'expression': f"""(() => {{
                const url = location.href;
                const value = ({function})({json.dumps(arg)});
                return {{url, value}};
            }})()""",
            'returnByValue': True,
        })
        value = result.get('result', {}).get('value')
        if 'exceptionDetails' in result or not isinstance(value, dict):
            raise ValueError('Flow 任务页暂不可读取。')
        self.url = value['url']
        return value.get('value')

    async def current_url(self):
        await self.evaluate('() => null')
        return self.url

    def locator(self, selector):
        return BalanceElements(self, f'[...document.querySelectorAll({json.dumps(selector)})]')

    def get_by_role(self, role, *, name):
        assert role == 'button'
        pattern = json.dumps(name.pattern)
        return BalanceElements(self, f"""[...document.querySelectorAll('button,[role=button]')]
            .filter(e => new RegExp({pattern}, 'i').test((e.getAttribute('aria-label') || e.innerText || '').trim()))""")

    def get_by_text(self, pattern):
        pattern = json.dumps(pattern.pattern)
        return BalanceElements(self, f"""[...document.querySelectorAll('body *')].filter(e => {{
            const matches = node => new RegExp({pattern}, 'i').test((node.innerText || '').trim());
            return matches(e) && ![...e.children].some(matches);
        }})""")

    def is_closed(self):
        return self.page.is_closed()

    async def bring_to_front(self):
        await self.session.send('Page.bringToFront')

    async def wait_for_load_state(self, state, *, timeout):
        # The account-panel routine separately waits for identity and visible controls.
        return

    async def screenshot(self, *, timeout):
        result = await self.session.send('Page.captureScreenshot', {'format': 'png'})
        await self.current_url()
        return base64.b64decode(result['data'])


class BalanceElements:
    def __init__(self, page, selection, index=None):
        self.page, self.selection, self.index = page, selection, index

    def filter(self, *, visible):
        assert visible
        return BalanceElements(self.page, f"""({self.selection}).filter(e =>
            e.getClientRects().length && getComputedStyle(e).visibility === 'visible')""", self.index)

    def nth(self, index):
        return BalanceElements(self.page, self.selection, index)

    async def count(self):
        return await self.page.evaluate(f'() => ({self.selection}).length')

    async def read(self, expression):
        index = 0 if self.index is None else self.index
        return await self.page.evaluate(f'() => {{ const element = ({self.selection})[{index}]; return element ? ({expression})(element) : null; }}')

    async def is_visible(self):
        return await self.read("e => !!e.getClientRects().length && getComputedStyle(e).visibility === 'visible'")

    async def inner_text(self, *, timeout):
        return await self.read("e => e.innerText || ''") or ''

    async def click(self, *, timeout):
        from adapter import READ_STATE
        result = await self.page.evaluate(f"""([url, expected]) => {{
            if (location.href !== url) return false;
            const identity = ({READ_STATE})();
            if (!identity.signed_in || identity.emails.length !== 1 || identity.emails[0] !== expected) return false;
            const matches = {self.selection};
            if (matches.length !== 1 || matches[0].disabled || matches[0].getAttribute('aria-disabled') === 'true') return false;
            matches[0].click();
            return true;
        }}""", [self.page.expected_url, self.page.email])
        if result is not True:
            raise ValueError('Flow 账号详情入口已变化，保留上次余额。')

    async def hover(self, *, timeout):
        point = await self.read("""e => {
            const r = e.getBoundingClientRect();
            const x = r.x + r.width / 2, y = r.y + r.height / 2;
            return x >= 0 && y >= 0 && x < innerWidth && y < innerHeight ? {x, y} : null;
        }""")
        if point and self.page.url == self.page.expected_url:
            await self.page.session.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', **point})
