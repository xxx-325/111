import unittest
from simulator.openhands.evidence_links import quoted_observations


class EvidenceLinkTests(unittest.TestCase):
    def test_only_exact_observation_not_command(self):
        line='datetime.date(2014, 12, 25)'
        checks=[{'id':'e1','observation':{'command':'invented output is not evidence',
            'content':[{'type':'text','text':line}]}}]
        self.assertEqual(quoted_observations(line,checks)[0]['id'],'e1')
        self.assertEqual(quoted_observations('invented output is not evidence',checks),[])
        self.assertEqual(quoted_observations('我已经确认所有测试都通过了',checks),[])

    def test_host_reviewed_logic_summary_can_infer_current_evidence(self):
        summary='当前复现稳定返回 UnsupportedOperation fileno failure'
        checks=[{'id':'judge1','simulated_experience':{'observation':{
            'kind':'logic_error','summary':summary}}}]
        self.assertEqual(quoted_observations('仍有问题：'+summary,checks)[0]['id'],
                         'judge1')
