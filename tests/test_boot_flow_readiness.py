import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
import boot_flow_readiness as boot


class BootKeyboard(unittest.TestCase):
    def run_coroutine(self, coroutine):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.close()

    def test_ready_helper_is_not_started_again(self):
        keyboard = MagicMock()
        keyboard.ready.return_value = True
        keyboard.ensure_ready = AsyncMock()
        with patch('input_backend.open_keyboard', return_value=keyboard) as open_keyboard:
            self.assertTrue(boot.prepare_keyboard({'DISPLAY': ':0'}))
        open_keyboard.assert_called_once_with(None, gui_environment={'DISPLAY': ':0'})
        keyboard.ensure_ready.assert_not_awaited()

    def test_stopped_helper_is_started_once(self):
        keyboard = MagicMock()
        keyboard.ready.side_effect = [False, True]
        keyboard.ensure_ready = AsyncMock()
        with patch('input_backend.open_keyboard', return_value=keyboard), \
                patch.object(boot.asyncio, 'run', side_effect=self.run_coroutine):
            self.assertTrue(boot.prepare_keyboard({'WAYLAND_DISPLAY': 'wayland-0'}))
        keyboard.ensure_ready.assert_awaited_once_with(timeout=45)

    def test_authorization_failure_does_not_stop_boot_recovery(self):
        keyboard = MagicMock()
        keyboard.ready.return_value = False
        keyboard.ensure_ready = AsyncMock(side_effect=RuntimeError('desktop_keyboard_authorization_timeout'))
        with patch('input_backend.open_keyboard', return_value=keyboard), \
                patch.object(boot.asyncio, 'run', side_effect=self.run_coroutine), \
                patch('builtins.print'):
            self.assertFalse(boot.prepare_keyboard({'DISPLAY': ':0'}))

    def test_boot_retries_keyboard_until_the_desktop_portal_is_ready(self):
        with patch.object(boot, 'gui_env', return_value={'DISPLAY': ':0'}), \
                patch.object(boot, 'network_ready', return_value=True), \
                patch.object(boot, 'prepare_keyboard', side_effect=[False, True]) as keyboard, \
                patch.object(boot, 'request', side_effect=[OSError('not ready'), {'accounts': {'a': 'ok'}}]), \
                patch.object(boot.time, 'sleep'):
            boot.main()
        self.assertEqual(keyboard.call_count, 2)


if __name__ == '__main__':
    unittest.main()
