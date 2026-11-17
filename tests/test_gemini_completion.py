"""Original-receipt recovery with no provider requests or resubmission."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from gemini_completion import completed_after_capacity_error, saved_capacity_completion, RECOVERED
from official_pool import OfficialPool
from official_runtime import MODEL
import test_gemini_stability as stability

CID = '11111111-1111-4111-8111-111111111111'


def fixture():
    steps = [dict(conversation_id=CID, step_index=0, step_type='user_input', state='DONE'),
             dict(conversation_id=CID, step_index=1, step_type='error_message', state='DONE'),
             dict(conversation_id=CID, step_index=2, step_type='agent_response', state='ACTIVE', text_delta='OK'),
             dict(conversation_id=CID, step_index=2, step_type='agent_response', state='DONE', text_delta='\n')]
    answer = dict(conversation_id=CID, status='ERROR', finished_reason='error', response='OK\n',
                  error='API error (attempt 1): UNAVAILABLE (code 503): No capacity available for model test on the server')
    return answer, steps


class CompletionEvidence(unittest.TestCase):
    def test_requires_earlier_capacity_error_then_exact_done_reply(self):
        answer, steps = fixture()
        self.assertTrue(completed_after_capacity_error(answer, steps))
        self.assertTrue(completed_after_capacity_error(answer, steps, CID))

    def test_incomplete_inconsistent_and_later_errors_are_never_success(self):
        answer, steps = fixture()
        cases = []
        cases.append((answer, steps[:-1]))
        cases.append((answer, [steps[0]] + steps[2:]))
        cases.append((answer, steps + [dict(steps[1], step_index=3)]))
        cases.append((answer, steps + [dict(steps[-1], state='ACTIVE', text_delta='more')]))
        cases.append((dict(answer, response='OK\nmore'), steps))
        cases.append((dict(answer, finish_reason='MAX_TOKENS'), steps))
        cases.append((dict(answer, error='UNAVAILABLE (503): downstream connection lost'), steps))
        cases.append((dict(answer, error='RESOURCE_EXHAUSTED 429'), steps))
        cases.append((answer, steps[:2] + [dict(steps[2], conversation_id=str(uuid.uuid4()))] + steps[3:]))
        cases.append((answer, steps[:2] + [dict(steps[2], step_type='tool')] + steps[3:]))
        cases.append((answer, steps[:2] + [dict(steps[2], step_index=0)] + steps[3:]))
        for a, s in cases:
            with self.subTest(a=a, s=s):
                self.assertFalse(completed_after_capacity_error(a, s))
        self.assertFalse(completed_after_capacity_error(answer, steps, str(uuid.uuid4())))

    def test_cached_receipt_rejects_mismatch_timeout_and_post_result_events(self):
        answer, steps = fixture()
        result = dict(submitted=True, status='unknown', reason='provider_unavailable', response='OK\n', total_ms=20)
        lines = [dict(channel='stdout', elapsed_ms=i, line=json.dumps(dict(event='step_update', step_update=s))) for i, s in enumerate(steps)]
        lines.append(dict(channel='stdout', elapsed_ms=10, line=json.dumps(dict(event='result', result=answer))))
        def read(path, limit):
            return '\n'.join(json.dumps(line) for line in lines).encode()
        recovered = saved_capacity_completion(Path('/unused'), result, None, read)
        self.assertEqual(recovered['completion_source'], RECOVERED)
        for bad in (dict(result, response='different'), dict(result, timeout_kind='idle'),
                    dict(result, submitted=False), dict(result, returncode=-15), dict(result, returncode=2)):
            self.assertIs(saved_capacity_completion(Path('/unused'), bad, None, read), bad)
        lines.append(lines[0])
        self.assertIs(saved_capacity_completion(Path('/unused'), result, None, read), result)


@unittest.skipUnless(os.name == 'posix', 'Actual CLI pipe integration requires Linux')
class CompletionTransport(unittest.TestCase):
    @patch('official_runtime.official_network_env', return_value={})
    def test_real_parser_accepts_done_but_not_partial_or_last_error(self, _):
        answer, steps = fixture()
        cases = [(steps, answer, 'completed'), (steps[:-1], answer, 'unknown'),
                 (steps + [dict(steps[1], step_index=3)], answer, 'unknown')]
        for events, terminal, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as d:
                packets = [dict(event='step_update', step_update=s) for s in events]
                packets.append(dict(event='result', result=terminal))
                body = 'import json,sys\n'
                body += 'packets=json.loads(' + repr(json.dumps(packets)) + ')\n'
                body += 'for packet in packets: print(json.dumps(packet),flush=True)\n'
                body += 'sys.exit(1)\n'
                runtime, root = stability.LocalTransport().setup_runtime(d, body)
                result = runtime.run('a', 'fixture only', None, root/'audit')
                self.assertEqual(result['status'], expected)
                if expected == 'completed':
                    self.assertIsNone(result['reason'])
                    self.assertEqual(result['native'], CID)
                    self.assertEqual(result['provider_warning'], 'recovered_capacity_503')


class SavedRequestReconciliation(unittest.TestCase):
    def test_original_request_restored_once_without_launch_or_receipt_changes(self):
        answer, steps = fixture()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rid = str(uuid.uuid4())
            audit = root/'official-audit'/rid
            audit.mkdir(parents=True)
            (audit/'submission.json').write_text(json.dumps(dict(account_key='a', created=100, native=None, model=MODEL)))
            (audit/'result.json').write_text(json.dumps(dict(submitted=True, status='unknown', reason='provider_unavailable',
                response='OK\n', native=None, first_text_ms=2, total_ms=20, token_count=9)))
            packets = [dict(event='step_update', step_update=s) for s in steps]
            packets.append(dict(event='result', result=answer))
            (audit/'events.jsonl').write_text('\n'.join(json.dumps(dict(channel='stdout', elapsed_ms=i, line=json.dumps(e))) for i, e in enumerate(packets)))
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in audit.iterdir()}
            dbpath = root/'state.db'
            with sqlite3.connect(dbpath) as db:
                db.executescript('''
                    CREATE TABLE requests(request_id TEXT PRIMARY KEY,account_key TEXT,status TEXT,finished REAL,response TEXT,note TEXT,created REAL);
                    CREATE TABLE official_jobs(request_id TEXT,conversation_id TEXT,native_before TEXT,readiness_probe INTEGER,reason TEXT);
                    CREATE TABLE conversation_turns(request_id TEXT,conversation_id TEXT,attachments TEXT);
                    CREATE TABLE conversations(id TEXT PRIMARY KEY,native_json TEXT,updated REAL,blocked TEXT);
                    CREATE TABLE official_attempts(request_id TEXT,ordinal INTEGER,status TEXT,reason TEXT,submitted INTEGER,started REAL,finished REAL,total_ms REAL,response TEXT);
                    CREATE TABLE request_metrics(request_id TEXT PRIMARY KEY,first_text_ms REAL,total_ms REAL,token_count INTEGER);
                    CREATE TABLE official_account_blocks(account_key TEXT,created REAL);
                ''')
                db.execute("INSERT INTO requests VALUES(?,'a','unknown',101,NULL,'old',99)",(rid,))
                db.execute("INSERT INTO official_jobs VALUES(?,? ,NULL,0,'provider_unavailable')",(rid,rid))
                db.execute("INSERT INTO conversation_turns VALUES(?,?,'[]')",(rid,rid))
                db.execute("INSERT INTO conversations VALUES(?,NULL,101,'blocked')",(rid,))
                db.execute("INSERT INTO official_attempts VALUES(?,1,'unknown','provider_unavailable',1,100,101,20,NULL)",(rid,))
            def connect():
                db = sqlite3.connect(dbpath, isolation_level=None)
                db.row_factory = sqlite3.Row
                return closing(db)
            pool = OfficialPool.__new__(OfficialPool)
            pool.pool = SimpleNamespace(state_dir=root, maintenance=lambda: False, connection=connect)
            with patch('subprocess.Popen', side_effect=AssertionError('Must never send')):
                self.assertEqual(pool.reconcile_saved_replies({str(uuid.uuid4())}), 0)
                self.assertEqual(pool.reconcile_saved_replies({rid}), 1)
                self.assertEqual(pool.reconcile_saved_replies({rid}), 0)
            with sqlite3.connect(dbpath) as db:
                self.assertEqual(db.execute('SELECT status,response FROM requests').fetchone(), ('completed','OK\n'))
                self.assertEqual(db.execute('SELECT count(*) FROM official_attempts').fetchone()[0], 1)
                native, blocked = db.execute('SELECT native_json,blocked FROM conversations').fetchone()
                self.assertEqual(json.loads(native)['official_conversation_id'], CID)
                self.assertIsNone(blocked)
            self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in audit.iterdir()})
