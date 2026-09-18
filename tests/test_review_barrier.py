import json
import tempfile
import time
import unittest
from pathlib import Path
from simulator.openhands.review_barrier import await_review, fingerprint


class ReviewBarrierTests(unittest.TestCase):
    def test_exact_material_and_repeated_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            material={'payload':{'text':'original draft'}}
            decision=dict(sha256=fingerprint(material),approved=True,reviewer='assistant',reason='checked')
            (root/'s.decision.json').write_text(json.dumps(decision))
            for _ in range(2):
                self.assertEqual(await_review(root,'s',material,time.monotonic()+1),decision)
            with self.assertRaises(RuntimeError):
                await_review(root,'s',{'payload':{'text':'changed'}},time.monotonic()+1)

    def test_no_approval_never_defaults_to_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TimeoutError):
                await_review(tmp,'s',{'text':'draft'},time.monotonic()-1)
            self.assertTrue((Path(tmp)/'s.request.json').exists())

    def test_rejection_and_stale_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            material={'text':'draft'}
            decision=dict(sha256=fingerprint(material),approved=False,reviewer='assistant',reason='wrong role')
            (root/'s.decision.json').write_text(json.dumps(decision))
            self.assertFalse(await_review(root,'s',material,time.monotonic()+1)['approved'])
            decision['sha256']='stale'
            (root/'s.decision.json').write_text(json.dumps(decision))
            with self.assertRaises(ValueError):
                await_review(root,'s',material,time.monotonic()+1)
