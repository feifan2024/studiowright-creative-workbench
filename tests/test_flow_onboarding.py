import ast
import asyncio
from contextlib import closing
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock,MagicMock,Mock,patch
root=Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/flow/console'
def method(file,name):
    tree=ast.parse((root/file).read_text(encoding='utf-8'))
    node=next(n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    ns={};exec(compile(ast.Module(body=[node],type_ignores=[]),str(file),'exec'),ns);return ns[name],ns
class Onboarding(unittest.IsolatedAsyncioTestCase):
    async def test_new_agent_draft_blocks_before_any_page_change(self):
        fn,_=method('browser_rendering.py','_safe_editor')
        box=S(count=AsyncMock(return_value=1),nth=lambda _:S(inner_text=AsyncMock(return_value='unsent draft')))
        page=S(locator=Mock(return_value=box))
        fake=S(selectors=S(COMPOSER='old',BOUND_CHIP='chip',MENTION_CHIP='mention'))
        class Blocked(Exception):pass
        with patch.dict(sys.modules,{'studiowright.flow':fake,'task_coordinator':S(CoordinationBlocked=Blocked)}):
            with self.assertRaises(Blocked):await fn(S(check_page_activity=AsyncMock()),page)
        self.assertIn('flow-base-prompt-box',page.locator.call_args.args[0])
    async def test_empty_agent_panel_prepared_only_after_safety_guards(self):
        fn,ns=method('flow_window_policy.py','safe_page');ns['SAFETY_JS']='flags'
        prepare=AsyncMock();page=S(url='https://flow.google.com/project/p',evaluate=AsyncMock(return_value={}))
        session=S(_safe_editor=AsyncMock())
        fake=S(FlowPage=Mock(return_value=S(_leave_agent_mode=prepare)))
        with patch.dict(sys.modules,{'studiowright.flow.page':fake}):
            await fn(session,page);prepare.assert_awaited_once()
            for flag in ['busy','uploads','dialog']:
                prepare.reset_mock();page.evaluate.return_value={flag:True}
                with self.assertRaises(ValueError):await fn(session,page)
                prepare.assert_not_awaited()

    async def test_import_reuses_flow_and_never_inspects_unrelated_uninitialized_tab(self):
        fn,_=method('browser_rendering.py','page_for')
        flow=S(url='https://flow.google.com/',is_closed=lambda:False)
        foreign=S(url='',is_closed=lambda:False)
        session=S(_onboarding=True,context=S(pages=[foreign,flow]),_safe_editor=AsyncMock(),
                  _new_window=AsyncMock(),check_page_activity=AsyncMock())
        self.assertIs(await fn(session,'https://flow.google.com/'),flow)
        session._new_window.assert_not_awaited()
        self.assertTrue(all(call.args[0] is flow for call in session._safe_editor.call_args_list))

    async def test_dispatch_prefers_bound_window_over_stale_coordinator_target(self):
        fn,ns=method('flow_window_policy.py','ensure_unique')
        project='https://flow.google.com/project/11111111-1111-4111-8111-111111111111'
        fixed=S(url=project,is_closed=lambda:False,close=AsyncMock(),evaluate=AsyncMock())
        duplicate=S(url=project,is_closed=lambda:False,close=AsyncMock(),evaluate=AsyncMock())
        keep=dict(page=fixed,role='other',target='fixed',window=1)
        copy=dict(page=duplicate,role='account-flow-task',target='old',window=2)
        coordinator=S(window=Mock(return_value={'state':'idle','target_id':'old'}),bind=Mock())
        registry=S(registration=Mock(return_value={'pid':7,'port':9222,'targets':{'fixed','old'}}),
                   verified_task_binding=Mock(return_value={'page_target':'fixed'}))
        modules={
            'task_coordinator':S(Coordinator=lambda:coordinator,CoordinationBlocked=ValueError,record=Mock()),
            'flow_task_window':registry,
            'task_window_registry':S(canonical_project_url=lambda url:url),
            'flow_browser_state':S(REGISTRY='fake')
        }
        ns.update(Lifecycle=MagicMock(),inventory=AsyncMock(side_effect=[[copy,keep],[keep]]),
                  safe_page=AsyncMock(),hashlib=hashlib,closing=closing,sqlite3=S(connect=MagicMock()))
        with patch.dict(sys.modules,modules):
            self.assertIs(await fn(S(port=9222),'fixture',task_id='new-task'),fixed)
        duplicate.close.assert_awaited_once()
        fixed.close.assert_not_awaited()
        coordinator.bind.assert_called_once_with('fixture','new-task','fixed')
