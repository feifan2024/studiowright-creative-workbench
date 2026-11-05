"""Ubuntu Chrome rendering compatibility; upstream files and auth stay untouched."""
import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
import sys
from filelock import FileLock, Timeout

from studiowright.browser import ChromeSession
from studiowright.studio import Studio
from studiowright import paths
from studiowright.store import Store


_generation_leases = {}


@asynccontextmanager
async def generation_lease(profile):
    """One operation per account; other accounts continue concurrently."""
    gate = _generation_leases.setdefault(profile, asyncio.Lock())
    await gate.acquire()
    guard = None
    try:
        paths.locks_dir().mkdir(parents=True, exist_ok=True)
        guard = FileLock(str(paths.locks_dir() / (profile + '.lock')), thread_local=False)
        while True:
            try:
                guard.acquire(timeout=0)
                break
            except Timeout:
                await asyncio.sleep(.2)
        Store().mark_interrupted(profile)
        yield
    finally:
        if guard is not None and guard.is_locked:
            guard.release()
        gate.release()


class _ManagedLock:
    def release(self):
        pass


class RenderingSession(ChromeSession):
    def configure_task(self, task_id, profile, coordinator):
        self._task_role = 'account-flow-task'
        self._task_id, self._profile, self._coordinator = task_id, profile, coordinator
        self._task_pages = set()

    async def _safe_editor(self, page):
        from studiowright.flow import selectors as s
        from task_coordinator import CoordinationBlocked
        boxes = page.locator(s.COMPOSER)
        for index in range(await boxes.count()):
            if (await boxes.nth(index).inner_text()).strip():
                raise CoordinationBlocked('existing_flow_draft')
        for selector in (s.BOUND_CHIP, s.MENTION_CHIP):
            if await page.locator(selector).count():
                raise CoordinationBlocked('existing_flow_attachments')

    async def _target_id(self, page):
        channel = await self.context.new_cdp_session(page)
        try:
            return (await channel.send('Target.getTargetInfo'))['targetInfo']['targetId']
        finally:
            await channel.detach()

    async def _fixed_page(self, url_prefix):
        from urllib.parse import urlparse
        from task_coordinator import CoordinationBlocked, record
        from flow_window_policy import ensure_unique
        page = await ensure_unique(self,self._profile,task_id=self._task_id)
        row = self._coordinator.window(self._profile)
        target = row.get('target_id') if row else None
        pages=[page]
        for page in self.context.pages:
            if page.is_closed():
                continue
            if page not in pages and ((target and await self._target_id(page)==target) or await self.role_of(page)==self._task_role):
                pages.append(page)
        if len(pages)>1:
            raise CoordinationBlocked('multiple_account_task_windows')
        if pages:
            page=pages[0]
            origin=urlparse(page.url)
            if not (page.url=='about:blank' or (origin.scheme=='https' and origin.hostname=='flow.google.com')):
                raise CoordinationBlocked('task_window_requires_login')
            await self._safe_editor(page)
            record('fixed_window_reused')
        else:
            page=await self._new_window()
            self._coordinator.bind(self._profile,self._task_id,await self._target_id(page))
            record('fixed_window_created')
        self._task_pages.add(page)
        if page.url=='about:blank':
            # A failed navigation retains this same target; recovery never opens a second window.
            from task_coordinator import recover_preparation
            async def navigate():
                await self._safe_editor(page)
                try:
                    await page.goto(url_prefix,wait_until='domcontentloaded',timeout=30000)
                except Exception as exc:
                    if type(exc).__name__=='TimeoutError':
                        raise TimeoutError('task_page_not_ready') from exc
                    raise
            await recover_preparation(navigate,reason='initial_page_load')
        if urlparse(page.url).hostname!='flow.google.com':
            raise CoordinationBlocked('task_window_requires_login')
        await page.evaluate('(name) => { window.name = name; }','sw-'+self._task_role)
        self._coordinator.bind(self._profile,self._task_id,await self._target_id(page))
        # Recount after adopting/creating a blank target and finishing navigation.
        await ensure_unique(self,self._profile,task_id=self._task_id)
        return page

    def find_page(self, url_prefix):
        if hasattr(self, '_task_pages'):
            return next((p for p in self._task_pages if not p.is_closed() and p.url.startswith(url_prefix)), None)
        return super().find_page(url_prefix)

    async def _new_window(self):
        page = await super()._new_window()
        if hasattr(self, '_task_pages'):
            self._task_pages.add(page)
        return page

    async def page_for(self, url_prefix: str, role: str = ""):
        if hasattr(self, '_task_role'):
            # All operations of this task use one retained account-owned window.
            page = next((p for p in self._task_pages if not p.is_closed()), None)
            if page is None:
                page = await self._fixed_page(url_prefix)
        else:
            if url_prefix.startswith('https://flow.google.com/') and hasattr(self,'_profile'):
                from flow_window_policy import ensure_unique
                page=await ensure_unique(self,self._profile)
                if page.url=='about:blank':
                    await page.goto(url_prefix,wait_until='domcontentloaded',timeout=30000)
                    await page.evaluate('(name)=>{window.name=name}','sw-account-flow-task')
                    await ensure_unique(self,self._profile)
            else:
                page = await super().page_for(url_prefix, role)
        if sys.platform.startswith("linux"):
            # Ubuntu can stop rAF even with visible=true and a normal window. Playwright
            # then cannot perform its ordinary stable-element check. Keep this target
            # active through Chrome's scheduling override, not force-click or JS clicks.
            # Retain the CDP session: detaching it releases the override.
            if not hasattr(self, "_rendering_sessions"):
                self._rendering_sessions = {}
            if page not in self._rendering_sessions:
                channel = await self.context.new_cdp_session(page)
                try:
                    await channel.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                except BaseException:
                    await channel.detach()
                    raise
                self._rendering_sessions[page] = channel
        return page

    async def detach(self):
        try:
            for page, channel in getattr(self, "_rendering_sessions", {}).items():
                if not page.is_closed():
                    with suppress(Exception):
                        await channel.detach()
        finally:
            await super().detach()


class RenderingStudio(Studio):
    def configure_task(self, task_id):
        self._task_id = task_id
        # The adapter holds the shared, cross-process lease for the full task.
        self._lock = _ManagedLock()

    async def _locked(self):
        if hasattr(self, '_task_id'):
            return
        await super()._locked()

    async def __aenter__(self):
        if hasattr(self, '_task_id'):
            from task_coordinator import Coordinator
            self._coordinator = Coordinator()
            self._coordinator.begin(self.profile,self._task_id)
        try:
            await super().__aenter__()
        except BaseException:
            if hasattr(self,'_coordinator'):
                self._coordinator.finish(self.profile,self._task_id,False)
            if self._playwright:
                await self._playwright.stop()
            raise
        session = self.session
        self._session = RenderingSession(session.browser, session.context, session.port)
        self._session._profile = self.profile
        if hasattr(self, '_task_id'):
            self._session.configure_task(self._task_id,self.profile,self._coordinator)
        return self

    def _progress(self, job_id, stage, media_ids):
        # Save the conservative boundary before any submit action can execute.
        if hasattr(self,'_coordinator'):
            self._coordinator.progress(self.profile,self._task_id,stage)
        return super()._progress(job_id,stage,media_ids)

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if hasattr(self,'_coordinator'):
                draft=False
                for page in getattr(self.session,'_task_pages',set()):
                    if not page.is_closed():
                        try:
                            await self.session._safe_editor(page)
                        except Exception:
                            draft=True
                self._coordinator.finish(self.profile,self._task_id,exc_type is None,draft,reason=str(exc) if exc else None)
        finally:
            from flow_window_policy import Lifecycle
            with suppress(Exception):Lifecycle().touch(self.profile)
            await super().__aexit__(exc_type,exc,tb)
