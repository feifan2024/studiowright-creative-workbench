"""Check the HTTP validation route with the current media-reader return type."""
import hashlib
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
import gemini_image_input as vision
from server import make_handler
from test_flow_regressions import fixture_directory


class VisionValidationHTTP(unittest.TestCase):
    def test_text_dispatch_reads_its_own_pool_during_flow_unavailability(self):
        with fixture_directory() as directory:
            pool, sources = Mock(), Mock()
            pool.path = directory / 'unused.sqlite3'
            pool.maintenance.return_value = False
            pool.generate.return_value = ({'status': 'queued'}, True)
            sources.snapshot.return_value = {'real_accounts': {'available': False}}
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(sources, real_pool=pool))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            receipt = '00000000-0000-4000-8000-000000000000'
            try:
                request = Request(base + '/api/real-pool/chat',
                    data=json.dumps({'account':'auto', 'prompt':'fixture'}).encode(),
                    headers={'Origin':base, 'X-Workbench-Request':'1',
                             'Content-Type':'application/json', 'Idempotency-Key':receipt})
                with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                    self.assertEqual(response.status, 201)
                pool.generate.assert_called_once_with('auto', 'fixture', receipt, None)
                sources.snapshot.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_automatic_recovery_route_cannot_request_manual_hold_release(self):
        with fixture_directory() as directory:
            pool, sources = Mock(), Mock()
            pool.path = directory / 'unused.sqlite3'
            pool.maintenance.return_value = False
            sources.flow_readiness.return_value = {'accounts':{},'repairs':{}}
            server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(sources,deployment='ubuntu',real_pool=pool))
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            try:
                request = Request(base+'/api/flow-readiness/auto',data=b'{}',
                    headers={'Origin':base,'X-Workbench-Request':'1','Content-Type':'application/json'})
                with build_opener(ProxyHandler({})).open(request,timeout=3) as response:
                    self.assertEqual(response.status,200)
                sources.flow_readiness.assert_called_once_with(repair=True,manual_review=None,automatic=True)
                pool.official.generate.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_tuple_media_receipt_validates_without_model_submission(self):
        from PIL import Image
        with fixture_directory() as directory:
            image = directory / '0.png'
            Image.new('RGB', (16, 16), 'blue').save(image)
            attachments = [dict(index=0, extension='png', sha256=hashlib.sha256(image.read_bytes()).hexdigest())]
            pool = Mock()
            pool.path = directory / 'unused.sqlite3'
            pool.maintenance.return_value = False
            pool.conversation_id.return_value = '00000000-0000-4000-8000-000000000000'
            sources = Mock()
            sources.snapshot.return_value = {'real_accounts': {'available': True, 'items': []}}
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(sources, real_pool=pool))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            try:
                body = json.dumps(dict(account='auto', prompt='Describe only this fixture.', attachments=attachments)).encode()
                request = Request(base + '/api/real-pool/image-analysis-validate', data=body,
                    headers={'Origin': base, 'X-Workbench-Request': '1', 'Content-Type': 'application/json',
                             'Idempotency-Key': pool.conversation_id.return_value})
                with patch.object(vision, 'files_for', return_value=(attachments, [image])):
                    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                        result = json.load(response)
                        self.assertEqual(response.status, 200)
                self.assertEqual(result, dict(valid=True, submitted=False, image_count=1))
                pool.official.generate.assert_not_called()
                pool.generate.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


    def test_image_submission_passes_optional_retry_policy_and_rejects_invalid(self):
        from urllib.error import HTTPError
        with fixture_directory() as directory:
            pool, sources = Mock(), Mock()
            pool.path = directory / 'unused.sqlite3'
            pool.maintenance.return_value = False
            pool.conversation_id.return_value = '00000000-0000-4000-8000-000000000000'
            sources.snapshot.return_value = {'real_accounts': {'available': True, 'items': []}}
            pool.official.generate.return_value = ({'request_id': pool.conversation_id.return_value, 'status':'queued'}, True)
            server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(sources,real_pool=pool))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            attachments = [dict(index=0, extension='png', sha256='0'*64)]
            try:
                for policy in ('omitted','never','invalid'):
                    pool.official.generate.reset_mock()
                    payload = dict(account='auto',prompt='Fixture only',attachments=attachments)
                    if policy != 'omitted': payload['retry_policy'] = policy
                    request = Request(base+'/api/real-pool/image-analysis',data=json.dumps(payload).encode(),
                        headers={'Origin':base,'X-Workbench-Request':'1','Content-Type':'application/json','Idempotency-Key':pool.conversation_id.return_value})
                    if policy == 'invalid':
                        with self.assertRaises(HTTPError) as caught:
                            build_opener(ProxyHandler({})).open(request,timeout=3)
                        self.assertEqual(caught.exception.code,400)
                        caught.exception.close()
                        pool.official.generate.assert_not_called()
                    else:
                        with build_opener(ProxyHandler({})).open(request,timeout=3) as response:
                            self.assertEqual(response.status,201)
                        kwargs = dict(image_analysis=True)
                        if policy=='never': kwargs['retry_policy']='never'
                        pool.official.generate.assert_called_once_with(
                            'auto','Fixture only',pool.conversation_id.return_value,[],attachments,**kwargs)
            finally:
                server.shutdown(); server.server_close(); thread.join()
