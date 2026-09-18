import unittest
from simulator.openhands.simulated_experience import build_experience, visible_experience
from simulator.openhands.user_projection import control_result


class ExperienceTests(unittest.TestCase):
    def record(self):
        return dict(accepted=True, payload=dict(outcome='unsolved', public_feedback={
                        'kind':'wrong_output','evidence_id':'e1','input':'INPUT: x','output':'OUTPUT: markers missing'}, evidence_ids=['e1']),
                    job=dict(id='j1',revision=1,candidate_version='v1'),
                    review={'feedback_safe':True})

    def test_verified_result_not_operation_history(self):
        e=build_experience(self.record())
        self.assertEqual(e['executor'],'Judge')
        self.assertFalse(e['operation_details_allowed'])
        current=dict(simulated_experience=e,verdict={'revision':1,'candidate_version':'v1'},active_revision=1)
        visible=visible_experience(current)
        self.assertIsNotNone(visible)
        self.assertNotIn('evidence_id',visible['observation'])
        current['active_revision']=2
        self.assertIsNone(visible_experience(current))

    def test_unverified_or_empty_inspection_not_experience(self):
        for key in ('evidence','feedback'):
            r=self.record()
            if key=='evidence':r['payload']['evidence_ids']=[]
            if key=='feedback':r['payload']['public_feedback']={}
            self.assertIsNone(build_experience(r))

    def test_internal_denial_does_not_disclose_policy(self):
        raw={'accepted':False,'reasons':['secret whitelist path']}
        result=control_result('authorize_tools',raw,None,{})
        self.assertNotIn('secret',str(result))
        self.assertFalse(result['accepted'])
        self.assertEqual(raw['reasons'],['secret whitelist path'])
