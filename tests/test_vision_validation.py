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
