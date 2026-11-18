import sys
import unittest
import threading
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from music_readiness import MusicReadiness, ready


def account(ident=1, active=True, valid=False):
    return {'id':ident, 'email':f'{ident}@example.test', 'is_active':active,
            'at_expires':'2099-01-01T00:00:00Z' if valid else '2000-01-01T00:00:00Z'}

class ReadinessTest(unittest.TestCase):
    def test_ready_and_disabled_are_never_touched(self):
        call=Mock(return_value=[account(valid=True),account(2,False)])
        service=MusicReadiness(call,lambda:'token',Mock(side_effect=AssertionError('must not check busy')))
        service.run()
        self.assertEqual(call.call_count,1)
        self.assertEqual(service.status()['state'],'completed')
        self.assertEqual([x['result'] for x in service.status()['items']],['已就绪，已跳过','已停用，保持原状'])

    def test_busy_tasks_skip_mutation(self):
        call=Mock(return_value=[account()]);service=MusicReadiness(call,lambda:'token',lambda:True)
        service.run();self.assertEqual(call.call_count,1)
        self.assertIn('有任务运行',service.status()['items'][0]['result'])

    def test_only_expired_active_target_is_refreshed_and_verified(self):
        rows=[account(valid=True),account(2),account(3,False)]
        call=Mock(side_effect=[rows,rows,{}, {},[account(2,valid=True)]])
        service=MusicReadiness(call,lambda:'token',lambda:False);service.run()
        self.assertEqual([c.args[:2] for c in call.call_args_list if c.args[0]=='POST'],
                         [('POST','/api/tokens/2/refresh-at'),('POST','/api/tokens/2/test')])
        self.assertEqual(service.status()['items'][1]['result'],'续期与认证成功')

    def test_fresh_recheck_skips_concurrently_recovered_account(self):
        call=Mock(side_effect=[[account()],[account(valid=True)]])
        service=MusicReadiness(call,lambda:'token',lambda:False);service.run()
        self.assertTrue(all(c.args[0]=='GET' for c in call.call_args_list))

    def test_busy_recheck_and_unknown_busy_state_fail_closed(self):
        for busy in [Mock(side_effect=[False,True]),Mock(side_effect=OSError('offline'))]:
            call=Mock(return_value=[account()]);service=MusicReadiness(call,lambda:'token',busy);service.run()
            self.assertTrue(all(c.args[0]=='GET' for c in call.call_args_list))

    def test_failure_never_enables_or_retries(self):
        call=Mock(side_effect=[[account()],[account()],ValueError('secret token')])
        service=MusicReadiness(call,lambda:'token',lambda:False);service.run()
        self.assertEqual(sum(c.args[0]=='POST' for c in call.call_args_list),1)
        self.assertNotIn('secret',str(service.status()))

    def test_duplicate_click_reuses_running_check(self):
        entered=threading.Event();release=threading.Event()
        def get_token():
            entered.set();release.wait(3);return 'token'
        call=Mock(return_value=[]);service=MusicReadiness(call,get_token,lambda:False)
        service.start();self.assertTrue(entered.wait(1))
        self.assertEqual(service.start()['state'],'running');release.set()

    def test_invalid_expiry_is_not_ready(self):
        for value in [None,'bad','2099-01-01T00:00:00']:
            self.assertFalse(ready({'is_active':True,'at_expires':value}))

if __name__=='__main__':unittest.main()
