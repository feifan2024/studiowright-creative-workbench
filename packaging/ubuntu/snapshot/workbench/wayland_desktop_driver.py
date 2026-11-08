"""Staged GNOME/Wayland desktop driver for the shared foreground queue.

Requires the separately installed, verified focus component and the Flow-only
window inventory. Does not start a browser, restart a service or
enable focus emulation. Never grants clipboard permissions programmatically.
"""
import ast
import asyncio
import json

FOCUS_BUS='local.studiowright.Foreground'
FOCUS_PATH='/local/studiowright/Foreground'


class WaylandDesktopDriver:
    def __init__(self, gui_environment):
        self.env = dict(gui_environment)

    async def _desktop_call(self, destination, path, method, *args):
        """Read native shell state; only the overview may be dismissed."""
        proc=await asyncio.create_subprocess_exec('gdbus','call','--session',
            '--dest',destination,'--object-path',path,'--method',method,*args,
            env=self.env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            raw,_=await asyncio.wait_for(proc.communicate(),3)
        except asyncio.TimeoutError:
            proc.kill();await proc.wait()
            raise ValueError('desktop_component_unavailable')
        if proc.returncode:raise ValueError('desktop_component_unavailable')
        return raw.decode().strip()

    async def _desktop_visible(self, restore=False):
        # A focused Chrome window may still sit behind GNOME's Activities view.
        # Do not dismiss authentication or security dialogs, or unlock the desktop.
        locked=await self._desktop_call('org.gnome.ScreenSaver','/org/gnome/ScreenSaver',
            'org.gnome.ScreenSaver.GetActive')
        if locked!='(false,)':return False
        async def overview():
            return await self._desktop_call('org.gnome.Shell','/org/gnome/Shell',
                'org.freedesktop.DBus.Properties.Get','org.gnome.Shell','OverviewActive')
        state=await overview()
        if state=='(<false>,)':return True
        if state!='(<true>,)' or not restore:return False
        await self._desktop_call('org.gnome.Shell','/org/gnome/Shell',
            'org.freedesktop.DBus.Properties.Set','org.gnome.Shell','OverviewActive','<false>')
        for _ in range(10):
            if await overview()=='(<false>,)':
                from task_coordinator import record
                record('desktop_overview_dismissed')
                return True
            await asyncio.sleep(.05)
        return False

    async def _call(self, bus, path, method, *args):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec('gdbus','call','--session',
                '--dest',bus,'--object-path',path,'--method',bus+'.'+method,
                *[str(x) for x in args],env=self.env,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            raw,_ = await asyncio.wait_for(proc.communicate(),5)
            if proc.returncode: raise ValueError('desktop_component_unavailable')
            result=ast.literal_eval(raw.decode().strip())[0]
            return json.loads(result) if isinstance(result,str) else result
        except asyncio.TimeoutError as exc:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise ValueError('desktop_component_unavailable') from exc
        except (OSError, ValueError, SyntaxError) as exc:
            raise ValueError('desktop_component_unavailable') from exc

    async def _identity_confirmed(self, target):
        from flow_task_window import confirm_flow_identity
        def list_windows(method, *args):
            from flow_browser_state import focus_call
            return focus_call(method, *args)
        return await confirm_flow_identity(target, list_windows)

    async def _os_focus_confirmed(self, target):
        if not await self._desktop_visible():return False
        state=await self._call(FOCUS_BUS,FOCUS_PATH,'Status')
        return (state.get('pid')==target.process_id and state.get('id')==target.window_id and
                state.get('workspace')==target.workspace and state.get('window_workspace')==target.workspace)

    async def window_geometry(self, target):
        windows=await self._call(FOCUS_BUS,FOCUS_PATH,'List',target.process_id)
        matches=[w for w in windows if w.get('id')==target.window_id and w.get('workspace')==target.workspace]
        if len(matches)!=1: raise ValueError('window_geometry_unavailable')
        return matches[0]

    async def activate_and_confirm(self, target):
        if not await self._identity_confirmed(target): return False
        if not await self._desktop_visible(restore=True):return False
        response=await self._call(FOCUS_BUS,FOCUS_PATH,'Activate',target.process_id,target.window_id)
        if not response.get('ok'): return False
        stable = 0
        for _ in range(30):
            # A workspace can briefly report focus before another desktop or
            # user action takes it away. Require a stable focused window.
            if await self._os_focus_confirmed(target):
                stable += 1
                if stable >= 3: return True
            else:
                stable = 0
            await asyncio.sleep(.1)
        return False

    async def composer_ready(self, page, composer, target):
        # Require a unique title match in this account's browser before doing
        # anything which might change the active tab or window.
        if not await self._identity_confirmed(target):return False
        windows=await self._call(FOCUS_BUS,FOCUS_PATH,'List',target.process_id)
        title=await page.title()
        if not title:return False
        matched=[w for w in windows if w.get('id')==target.window_id and w.get('workspace')==target.workspace
                 and w.get('title') in (title,title+' - Google Chrome',title+' - Chromium')]
        if len(matched)!=1 or matched[0].get('id')!=target.window_id:return False
        await page.bring_to_front()
        if not await self._os_focus_confirmed(target): return False
        from portal_pointer_client import PortalPointer
        from pointer_sequence import prepare_pointer
        pointer=PortalPointer(target,gui_environment=self.env)
        await pointer.ensure_ready()
        # Portal permission prompts can take focus while they are shown.
        if not await self.activate_and_confirm(target): return False
        await page.bring_to_front()
        if not await self._os_focus_confirmed(target): return False
        await prepare_pointer(page=page, composer=composer, target=target,
                              driver=self, pointer=pointer)
        return await self.confirm_focus(target,page,composer)

    async def composer_text(self, page, composer):
        return await composer.inner_text()

    async def confirm_focus(self, target, page, composer):
        if not await self._identity_confirmed(target) or not await self._os_focus_confirmed(target):
            return False
        if not await page.evaluate('() => document.visibilityState === "visible" && document.hasFocus()'):
            return False
        return await composer.evaluate('(el) => el === document.activeElement || el.contains(document.activeElement)')

    async def copy_prompt_and_paste(self, page, composer, target, prompt):
        if not await self.confirm_focus(target,page,composer): raise ValueError('focus_lost')
        from portal_keyboard_client import PortalKeyboard
        keyboard = PortalKeyboard(target, gui_environment=self.env)
        if not keyboard.ready():
            from foreground_input import InputNotSubmitted
            raise InputNotSubmitted('desktop_keyboard_not_ready')
        await page.evaluate('(text) => navigator.clipboard.writeText(text)',prompt)
        if not await self.confirm_focus(target, page, composer): raise ValueError('focus_lost')
        await keyboard.paste()

    async def copy_prompt_and_paste_chunks(self, page, composer, target, prompt, size=48):
        """Real Ctrl+V per chunk; text arrives in several events, never one blob."""
        import random
        if not await self.confirm_focus(target, page, composer): raise ValueError('focus_lost')
        from portal_keyboard_client import PortalKeyboard
        keyboard = PortalKeyboard(target, gui_environment=self.env)
        if not keyboard.ready():
            from foreground_input import InputNotSubmitted
            raise InputNotSubmitted('desktop_keyboard_not_ready')
        chunks = [prompt[i:i + size] for i in range(0, len(prompt), size)]
        for index, chunk in enumerate(chunks):
            if not await self.confirm_focus(target, page, composer): raise ValueError('focus_lost')
            await page.evaluate('(text) => navigator.clipboard.writeText(text)', chunk)
            if not await self.confirm_focus(target, page, composer): raise ValueError('focus_lost')
            await asyncio.sleep(random.uniform(.05, .18))
            await keyboard.paste()
            if index < len(chunks) - 1:
                await asyncio.sleep(random.uniform(.12, .4))
