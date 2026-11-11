import importlib.util
from pathlib import Path
import unittest
import uuid


class OmniFrameContract(unittest.TestCase):
    def test_both_services_accept_omni_12_start_and_optional_end_frame(self):
        root = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
        for index, path in enumerate([root/'workbench/flow_references.py', root/'flow/console/flow_references.py']):
            spec = importlib.util.spec_from_file_location('frame_contract_' + str(index), path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            start, end = str(uuid.uuid4()), str(uuid.uuid4())
            for model in ['omni-flash', 'omni-flash-2']:
                with self.subTest(service=index, model=model):
                    params = dict(model=model, mode='frames', start_reference_id=start)
                    self.assertEqual(module.task_reference_ids('video', params), [start])
                    params['end_reference_id'] = end
                    self.assertEqual(module.task_reference_ids('video', params), [start, end])
            with self.assertRaises(ValueError):
                module.task_reference_ids('video', dict(model='unsupported-model', mode='frames', start_reference_id=start))
            with self.assertRaises(ValueError):
                module.task_reference_ids('video', dict(model='omni-flash-2', mode='frames', end_reference_id=end))
