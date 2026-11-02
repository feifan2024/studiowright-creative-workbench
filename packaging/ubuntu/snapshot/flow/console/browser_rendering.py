"""Ubuntu Chrome rendering compatibility; upstream files and auth stay untouched."""
import sys

from studiowright.browser import ChromeSession
from studiowright.studio import Studio


class RenderingSession(ChromeSession):
    async def page_for(self, url_prefix: str, role: str = ""):
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
                    await channel.detach()
        finally:
            await super().detach()


class RenderingStudio(Studio):
    async def __aenter__(self):
        await super().__aenter__()
        session = self.session
        self._session = RenderingSession(session.browser, session.context, session.port)
        return self
