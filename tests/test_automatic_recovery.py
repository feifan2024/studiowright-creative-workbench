import asyncio
from io import BytesIO
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
import flow_readiness_recovery as recovery
from test_flow_regressions import fixture_directory


class AutomaticRecoverySafety(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, page='binding', busy=False, review=None, repairable=True):
        with fixture_directory() as directory:
            controller=types.ModuleType('shared_session_controller')
            controller.flow_runtime=lambda:(None,types.SimpleNamespace(locks_dir=lambda:directory))
            account=dict(profile='fixture',key='flow-account-'+'a'*64,email='fixture@example.invalid',
                         projects=[],review_task=review,repairable=repairable)
            action=AsyncMock(return_value=('ok',None))
            with patch.dict(sys.modules,{'shared_session_controller':controller}), \
                 patch('flow_window_policy.Lifecycle') as lifecycle, \
                 patch.object(recovery,'inspect_page',AsyncMock(return_value=page)), \
                 patch.object(recovery,'repair',action), \
                 patch.object(recovery,'review_hold',return_value=False), \
                 patch.object(recovery.subprocess,'check_output',return_value='active'):
                lifecycle.return_value.status.return_value=[]
                result=await recovery.recover(dict(accounts=[account],bindings=[],busy=['fixture'] if busy else [],safe_only=True))
            return result,action

    async def test_recoverable_page_uses_guarded_repair(self):
        result,action=await self.run_case()
        self.assertEqual(result['fixture']['page'],'ok')
        self.assertTrue(action.await_args.kwargs['safe_only'])

    async def test_running_tasks_and_unconfirmed_results_are_not_touched(self):
        for args in [dict(busy=True),dict(repairable=False)]:
            result,action=await self.run_case(**args)
            self.assertEqual(result,{})
            action.assert_not_awaited()

        result,action=await self.run_case(review='original-task')
        self.assertEqual(result['fixture']['page'],'unconfirmed')
        action.assert_not_awaited()

    async def test_task_drafts_are_repaired_but_login_challenges_are_not_bypassed(self):
        result,action=await self.run_case(page='draft')
        self.assertEqual(result['fixture']['page'],'ok')
        self.assertTrue(action.await_args.kwargs['safe_only'])
        result,action=await self.run_case(page='login')
        self.assertEqual(result['fixture']['page'],'login')
        action.assert_not_awaited()

    async def test_multiple_windows_reuse_exact_registered_task_target(self):
        account=dict(profile='fixture',key='flow-account-'+'a'*64)
        known=dict(browser_pid=123,port=9222,process_start='stamp',page_target='A',window_id=101,workspace=0)
        pages=[dict(id=x,webSocketDebuggerUrl='ws://127.0.0.1:9222/devtools/page/'+x) for x in ['A','B']]
        windows=[dict(id=x,workspace=0) for x in [101,102]]
        with patch.object(recovery,'registration',return_value=dict(pid=123,port=9222)), \
             patch.object(recovery,'process_identity',return_value=('fixture','stamp')), \
             patch.object(recovery,'_pages',return_value=pages), \
             patch.object(recovery,'focus_call',return_value=windows), \
             patch.object(recovery,'TaskWindowRegistry') as registry:
            registry.return_value.get.return_value=known
            current,page,_=await recovery.candidate(account)
        self.assertEqual(current['window_id'],101)
        self.assertEqual(page['id'],'A')

    async def test_bounded_no_output_evidence_is_distinct_from_a_generic_timeout(self):
        task=dict(status='failed',error='no image reply within 240 s, and no new image is in the project',
                  result={'source_jobs':[dict(status='failed',media_ids=[],files=[])]})
        self.assertTrue(recovery.failed_without_result(task))
        task['error']='connection timeout'
        self.assertFalse(recovery.failed_without_result(task))
        task['error']='no image reply within 240 s, and no new image is in the project'
        task['result']['source_jobs'][0]['media_ids']=['existing-media']
        self.assertFalse(recovery.failed_without_result(task))

    async def test_responsive_restart_requires_the_single_owned_task_page(self):
        binding=dict(profile='fixture',browser_pid=123,account_key='a'*64,port=9222,
                     process_start='stamp',page_target='A',window_id=101)
        for tabs,allowed in [([dict(type='page',id='A')],True),
                             ([dict(type='page',id='A'),dict(type='page',id='manual')],False)]:
            with patch.object(recovery.subprocess,'check_output',return_value='123'), \
                 patch.object(recovery.subprocess,'run') as restart, \
                 patch.object(recovery,'process_identity',return_value=('fixture','stamp')), \
                 patch.object(recovery,'focus_call',return_value=[dict(id=101)]), \
                 patch.object(recovery,'build_opener') as opener:
                opener.return_value.open.side_effect=[BytesIO(b'{}'),BytesIO(json.dumps(tabs).encode())]
                if allowed:
                    recovery.restart_managed(binding,allow_responsive=True)
                    restart.assert_called_once()
                else:
                    with self.assertRaisesRegex(ValueError,'restart_not_safe'):
                        recovery.restart_managed(binding,allow_responsive=True)
                    restart.assert_not_called()
