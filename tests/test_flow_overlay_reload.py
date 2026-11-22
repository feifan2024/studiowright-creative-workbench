import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
import flow_readiness_recovery as recovery


PROJECT = 'https://flow.google.com/project/11111111-1111-4111-8111-111111111111'
STATE = dict(origin=True, account=True, path=PROJECT.removeprefix('https://flow.google.com'), overlay=True)


class FlowOverlayReload(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_same_project_and_checks_new_document(self):
        loaded = dict(STATE, overlay=False, editor=True)
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        new = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'new'}}}
        actions = []
        with patch.object(recovery, 'guard_live'), patch.object(recovery, 'command', AsyncMock(side_effect=[old, {}, old, new])) as c, \
                patch.object(recovery, 'evaluate', AsyncMock(side_effect=[STATE, loaded])), patch.object(recovery.asyncio, 'sleep', AsyncMock()):
            self.assertEqual(await recovery.reload_overlay(None, {}, '', PROJECT, actions), loaded)
        self.assertEqual(actions, ['reload'])
        self.assertEqual([a.args[1] for a in c.await_args_list],
                         ['Page.getFrameTree', 'Page.reload', 'Page.getFrameTree', 'Page.getFrameTree'])
        self.assertEqual(c.await_args_list[1].args[2], {'loaderId': 'old'})

    async def test_busy_or_draft_pages_are_never_refreshed(self):
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        for flag in ('busy', 'draft'):
            state = dict(STATE, **{flag: True})
            actions = []
            with patch.object(recovery, 'guard_live'), patch.object(recovery, 'command', AsyncMock(return_value=old)) as c, \
                    patch.object(recovery, 'evaluate', AsyncMock(return_value=state)):
                self.assertEqual(await recovery.reload_overlay(None, {}, '', PROJECT, actions), state)
            c.assert_awaited_once()
            self.assertEqual(actions, [])

    async def test_blank_page_reloads_once_and_waits_for_the_original_editor(self):
        blank = dict(origin=True, account=False, loadFailed=True, path=STATE['path'])
        loading = dict(blank, loadFailed=False)
        ready = dict(origin=True, account=True, editor=True, path=STATE['path'])
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        new = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'new'}}}
        actions = []
        self.assertEqual(recovery.page_verdict(blank), 'load_failed')
        self.assertEqual(recovery.page_verdict(loading), 'unconfirmed')
        with patch.object(recovery, 'guard_live'), \
                patch.object(recovery, 'command', AsyncMock(side_effect=[old, {}, old, new, new])) as c, \
                patch.object(recovery, 'evaluate', AsyncMock(side_effect=[blank, loading, ready])), \
                patch.object(recovery.asyncio, 'sleep', AsyncMock()):
            self.assertEqual(await recovery.reload_failed_project(None, {}, '', PROJECT, actions), ready)
        self.assertEqual(actions, ['reload'])
        self.assertEqual([call.args[2] for call in c.await_args_list if call.args[1] == 'Page.reload'],
                         [{'loaderId': 'old'}])

    async def test_blank_page_recovery_leaves_sign_in_and_drafts_alone(self):
        frame = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        for flag, verdict in [('login', 'login'), ('busy', 'busy'), ('draft', 'draft')]:
            with self.subTest(flag=flag):
                state = dict(origin=True, account=False, loadFailed=True, path=STATE['path'], **{flag: True})
                actions = []
                self.assertEqual(recovery.page_verdict(state), verdict)
                with patch.object(recovery, 'guard_live'), \
                        patch.object(recovery, 'command', AsyncMock(return_value=frame)) as c, \
                        patch.object(recovery, 'evaluate', AsyncMock(return_value=state)):
                    self.assertEqual(await recovery.reload_failed_project(None, {}, '', PROJECT, actions), state)
                c.assert_awaited_once()
                self.assertEqual(actions, [])
