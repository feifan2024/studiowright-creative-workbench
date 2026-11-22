"""Selects the desktop pointer/keyboard backend for terminal input.

``hid`` drives kernel-level uinput devices, ``portal`` keeps the XDG
remote-desktop helper, and ``auto`` prefers HID and only falls back to the
portal when HID cannot be authorised. The policy lives in
``~/.config/creative-unified-workbench/input-backend.json``; a missing or
malformed file means ``auto`` with the conservative device defaults.
"""
from uinput_hid import (CONFIG_FILE, HidInputError, HidKeyboardClient,
                        HidPointerClient, HidUnavailable, hid_supported,
                        load_config, paired_devices)


class _FallbackInput:
    """Expose one interface while deciding HID-versus-portal at first use."""

    def __init__(self, primary, fallback, allow_fallback, label, primary_kind='hid'):
        self.primary = primary
        self.fallback = fallback
        self.allow_fallback = allow_fallback
        self.label = label
        self.primary_kind = primary_kind
        self.active = None

    @property
    def backend(self):
        if self.active is self.primary:
            return self.primary_kind
        if self.active is self.fallback and self.fallback is not None:
            return 'portal'
        return 'pending'

    def ready(self):
        if self.active is not None:
            return True
        try:
            if self.primary.ready():
                return True
        except Exception:
            pass
        if self.allow_fallback and self.fallback is not None:
            try:
                return bool(self.fallback.ready())
            except Exception:
                return False
        return False

    async def ensure_ready(self, timeout=30):
        if self.active is not None:
            if getattr(self.active, '_ready_pid', None) is None and hasattr(self.active, 'ensure_ready'):
                await self.active.ensure_ready(timeout=timeout)
            return
        try:
            await self.primary.ensure_ready(timeout=timeout)
            self.active = self.primary
            return
        except Exception:
            if not self.allow_fallback:
                raise
        await self.fallback.ensure_ready(timeout=min(timeout, 60))
        self.active = self.fallback

    async def move(self, dx, dy):
        await self._delegate('move', dx, dy)

    async def scroll(self, steps):
        await self._delegate('scroll', steps)

    async def click(self):
        await self._delegate('click')

    async def type(self, text, *, guard=None):
        if self.active is None:
            await self.ensure_ready()
        if guard is None:
            await self.active.type(text)
        elif self.backend == 'hid':
            await self.active.type(text, guard=guard)
        else:
            raise HidInputError('guarded_input_unavailable')

    async def paste(self):
        await self._delegate('paste')

    async def _delegate(self, action, *values):
        if self.active is None:
            await self.ensure_ready()
        await getattr(self.active, action)(*values)

    def describe(self):
        return {'label': self.label, 'backend': self.backend,
                'policy': 'auto' if self.allow_fallback else 'fixed'}


def _portal_pointer(target, gui_environment):
    from portal_pointer_client import PortalPointer
    return PortalPointer(target, gui_environment=gui_environment)


def _portal_keyboard(target, gui_environment):
    from portal_keyboard_client import PortalKeyboard
    return PortalKeyboard(target, gui_environment=gui_environment)


def _choose(policy, target, gui_environment, hid_factory, portal_factory, label):
    if policy == 'portal':
        return _FallbackInput(portal_factory(), None, False, label, primary_kind='portal')
    hid = hid_factory()
    if policy == 'hid':
        if not hid_supported():
            raise HidUnavailable('uinput_unavailable')
        return _FallbackInput(hid, None, False, label)
    portal = portal_factory()
    return _FallbackInput(hid, portal, True, label)


def open_pointer(target, gui_environment=None, config_path=None):
    config = load_config(config_path)
    return _choose(config['pointer'], target, gui_environment,
                   lambda: HidPointerClient(target, gui_environment=gui_environment,
                                            config_path=config_path),
                   lambda: _portal_pointer(target, gui_environment), 'pointer')


def open_keyboard(target, gui_environment=None, config_path=None):
    config = load_config(config_path)
    return _choose(config['keyboard'], target, gui_environment,
                   lambda: HidKeyboardClient(target, gui_environment=gui_environment,
                                             config_path=config_path),
                   lambda: _portal_keyboard(target, gui_environment), 'keyboard')


def backend_report(config_path=None):
    """Diagnostic snapshot for the workbench API and verification scripts."""
    config = load_config(config_path)
    devices = paired_devices()
    return {'version': 'input-backend-20261122', 'config_path': str(config_path or CONFIG_FILE),
            'pointer': config['pointer'], 'keyboard': config['keyboard'],
            'uinput_available': hid_supported(),
            'identity_source': config['identity_source'],
            'paired_devices': [{'event': device['event'], 'name': device['name'],
                                'kind': device['kind'],
                                'bustype': f'{device["bustype"]:04x}',
                                'vendor': f'{device["vendor"]:04x}',
                                'product': f'{device["product"]:04x}'}
                               for device in devices if device['bustype'] == 5],
            'pointer_device': config['pointer_device'],
            'keyboard_device': config['keyboard_device']}
