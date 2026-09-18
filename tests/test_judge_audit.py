import unittest
from unittest.mock import patch
from simulator.openhands.judge import review_verdict


class JudgeAuditTests(unittest.TestCase):
    def check(self, outcome, result, feedback=None):
        result = dict(result)
        result.setdefault('required_failure_observed', outcome == 'unsolved')
        with patch('simulator.openhands.judge.call_json', return_value=result) as call:
            output = review_verdict(None, {'title': 'Problem', 'body': 'Text', 'identifier': 'hidden-task'},
                {'candidate_version': 'private-version', 'code_reply': {}},
                {'outcome': outcome, 'public_feedback': feedback or {}}, [], [],
                {'title': 'Problem', 'body': 'Text'}, {'items': []})
            self.assertIn('违反完整 issue 必需行为', call.call_args.args[1])
            self.assertIn('uncertain 仅在', call.call_args.args[1])
            self.assertIn('当前候选的观察', call.call_args.args[1])
            self.assertIn('真实可见结果', call.call_args.args[1])
            audit_input = call.call_args.args[2]
            self.assertNotIn('reference_diff', audit_input)
            self.assertNotIn('tool_events', audit_input)
            return output

    def test_all_supported_outcomes_can_be_valid(self):
        for outcome in ('solved', 'unsolved', 'uncertain'):
            self.assertTrue(self.check(outcome, dict(verdict_valid=True, grounded=True,
                feedback_safe=True, reasons=[]))['allowed'])

    def test_observed_fd_mismatch_rejects_uncertain_but_accepts_unsolved(self):
        rejected = self.check('uncertain', dict(
            verdict_valid=False, grounded=True, feedback_safe=True,
            required_failure_observed=True,
            reasons=['observed file descriptor behavior violates the issue'],
        ))
        self.assertFalse(rejected['allowed'])
        self.assertTrue(rejected['grounded'])
        self.assertFalse(rejected['verdict_valid'])
        accepted = self.check('unsolved', dict(
            verdict_valid=True, grounded=True, feedback_safe=True, reasons=[],
        ))
        self.assertTrue(accepted['allowed'])

    def test_real_uncertainty_remains_valid(self):
        result = self.check('uncertain', dict(
            verdict_valid=True, grounded=True, feedback_safe=True,
            reasons=['required runtime is unavailable'],
        ))
        self.assertTrue(result['allowed'])
        self.assertTrue(result['verdict_valid'])

    def test_host_rejects_self_contradictory_uncertain_audit(self):
        result = self.check('uncertain', dict(
            verdict_valid=True, grounded=True, feedback_safe=True,
            required_failure_observed=True,
            reasons=['required behavior is observably violated'],
        ))
        self.assertFalse(result['allowed'])
        self.assertFalse(result['verdict_valid'])

    def test_unsupported_and_legacy_results_fail_closed(self):
        for outcome in ('solved', 'unsolved'):
            for result in (dict(verdict_valid=False, grounded=False, feedback_safe=True),
                           dict(allowed=True, grounded=True, feedback_safe=True)):
                self.assertFalse(self.check(outcome, result)['allowed'])

    def test_private_feedback_still_rejected(self):
        self.assertFalse(self.check('unsolved', dict(verdict_valid=True, grounded=True,
            feedback_safe=True), {'actual': '/workspace/checks/private.py'})['allowed'])

    def test_unobserved_or_unsafe_feedback_still_rejected(self):
        result=self.check('unsolved', dict(verdict_valid=True, grounded=False,
            feedback_safe=False), {'actual': 'unsupported observation'})
        self.assertFalse(result['allowed'])
        self.assertFalse(result['conclusion_valid'])

    def test_valid_conclusion_is_separate_from_unsafe_feedback(self):
        result=self.check('unsolved',dict(verdict_valid=True,grounded=True,feedback_safe=False),
                          {'actual':'includes implementation analysis'})
        self.assertFalse(result['allowed'])
        self.assertTrue(result['conclusion_valid'])
        self.assertFalse(result['feedback_safe'])

    def test_current_judge_test_source_overlap_is_audited_not_hard_rejected(self):
        summary='调用 faulthandler.enable() 后实际返回 UnsupportedOperation fileno'
        event={'id':'e1','tool_name':'file_editor','action':{
            'command':'create','path':'/workspace/checks/repro.py',
            'file_text':'faulthandler.enable()'}}
        task={'title':'Problem','body':'Text','identifier':'hidden-task','patch':''}
        payload={'outcome':'unsolved','public_feedback':{
            'kind':'logic_error','evidence_id':'obs1','summary':summary}}
        with patch('simulator.openhands.judge.call_json',return_value={
                'verdict_valid':True,'grounded':True,'feedback_safe':True,
                'required_failure_observed':True,'reasons':[]}):
            result=review_verdict(None,task,
                {'candidate_version':'private-version','code_reply':{}},payload,
                [event],[],{'title':'Problem','body':'Text'},{'items':[]})
        self.assertTrue(result['allowed'])
        self.assertEqual([m['text'] for m in result['source_check']['matches']],
                         ['faulthandler.enable()'])

    def test_reference_implementation_remains_hard_rejected(self):
        implementation='return wrap_stream(stream, errors="replace", preserve_fileno=True)'
        task={'title':'Problem','body':'Text','identifier':'hidden-task','patch':
              'diff --git a/src/cli.py b/src/cli.py\n+++ b/src/cli.py\n+'+implementation}
        payload={'outcome':'unsolved','public_feedback':{
            'kind':'logic_error','evidence_id':'obs1','summary':implementation}}
        with patch('simulator.openhands.judge.call_json',return_value={
                'verdict_valid':True,'grounded':True,'feedback_safe':True,
                'required_failure_observed':True,'reasons':[]}):
            result=review_verdict(None,task,
                {'candidate_version':'private-version','code_reply':{}},payload,
                [],[],{'title':'Problem','body':'Text'},{'items':[]})
        self.assertFalse(result['allowed'])
        self.assertIn('reference implementation copied',result['reasons'])

    def test_wrong_output_summary_cannot_copy_reference_test_source(self):
        expected='assert result.output == "Arg1 [(custom)]: value", exact=True'
        task={'title':'Problem','body':'Text','identifier':'hidden-task','patch':
              'diff --git a/tests/test_prompt.py b/tests/test_prompt.py\n'
              '+++ b/tests/test_prompt.py\n+'+expected}
        payload={'outcome':'unsolved','public_feedback':{
            'kind':'wrong_output','evidence_id':'obs1','input':'custom',
            'output':'Arg1 [custom]: value','summary':expected}}
        with patch('simulator.openhands.judge.call_json',return_value={
                'verdict_valid':True,'grounded':True,'feedback_safe':True,
                'required_failure_observed':True,'reasons':[]}):
            result=review_verdict(None,task,
                {'candidate_version':'private-version','code_reply':{}},payload,
                [],[],{'title':'Problem','body':'Text'},{'items':[]})
        self.assertFalse(result['allowed'])
        self.assertIn('reference implementation copied',result['reasons'])
