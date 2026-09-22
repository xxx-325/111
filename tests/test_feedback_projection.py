import unittest

from simulator.openhands.feedback_projection import (
    PublicFeedbackError,
    authorized_feedback_values,
    feedback_failure_key,
    feedback_units,
    feedback_projection_mapping,
    has_public_feedback_source,
    has_projectable_execution_blocks,
    labeled_blocks,
    project_latest_feedback,
    project_public_feedback,
)


class FeedbackProjectionTests(unittest.TestCase):
    def observation(self, text):
        return [{'id': 'obs1', 'observation': {
            'content': [{'type': 'text', 'text': text}],
            'command': 'cd /workspace/candidate && python check.py',
            'exit_code': 0,
        }, 'tool': 'terminal'}]

    def test_labeled_blocks_preserve_multiline_content(self):
        text = ('prefix\n---INPUT---\na = 1\n b = 2\n---END INPUT---\n'
                '---ERROR---\nTraceback\nValueError: bad\n---END ERROR---\n')
        self.assertEqual(labeled_blocks(text), {
            'INPUT': 'a = 1\n b = 2',
            'ERROR': 'Traceback\nValueError: bad',
        })

    def test_runtime_error_uses_host_observation_not_model_copy(self):
        proposal = {
            'evidence_id': 'obs1',
        }
        result = project_public_feedback(
            proposal,
            self.observation('=== INPUT ===\ncall()\n=== END INPUT ===\n'
                             '=== ERROR ===\nTraceback\nBoom\n=== END ERROR ==='),
            ['obs1'],
        )
        self.assertEqual(result, {
            'kind': 'runtime_error',
            'evidence_id': 'obs1',
            'input': 'call()',
            'error': 'Traceback\nBoom',
        })

    def test_wrong_output_keeps_reviewable_observable_difference(self):
        observations = self.observation(
            '---INPUT---\nshow_default="custom"\n---END INPUT---\n'
            "---RESULT---\nprompt: 'Arg1 [custom]: '\n---END RESULT---")
        summary = "prompt 实际为 'Arg1 [custom]: '，预期为 'Arg1 [(custom)]: '，缺少圆括号。"
        result = project_latest_feedback(observations, summary)
        self.assertEqual(result, {
            'kind': 'wrong_output',
            'evidence_id': 'obs1',
            'input': 'show_default="custom"',
            'output': "prompt: 'Arg1 [custom]: '",
            'summary': summary,
        })
        self.assertEqual(authorized_feedback_values(result), [
            'show_default="custom"',
            "prompt: 'Arg1 [custom]: '",
            summary,
        ])

    def test_logic_error_keeps_only_grounded_summary(self):
        result = project_public_feedback(
            {'evidence_id': 'obs1', 'summary': '跨月结果还是不对'},
            self.observation('observed behavior'),
            ['obs1'],
        )
        self.assertEqual(result['summary'], '跨月结果还是不对')
        self.assertEqual(authorized_feedback_values(result), ['跨月结果还是不对'])

    def test_missing_labels_and_private_test_path_fail_closed(self):
        proposal = {'evidence_id': 'obs1'}
        with self.assertRaises(PublicFeedbackError):
            project_public_feedback(proposal, self.observation('actual only'), ['obs1'])
        with self.assertRaises(PublicFeedbackError):
            project_public_feedback(
                proposal,
                self.observation('---INPUT---\n/workspace/checks/test.py\n---END INPUT---\n'
                                 '---RESULT---\nFalse\n---END RESULT---'),
                ['obs1'],
            )

    def test_empty_error_is_unavailable_but_empty_result_is_valid(self):
        empty_error = self.observation(
            '---INPUT---\nx\n---END INPUT---\n'
            '---ERROR---\n---END ERROR---')
        with self.assertRaisesRegex(PublicFeedbackError, 'observable error'):
            project_latest_feedback(empty_error)
        self.assertFalse(has_projectable_execution_blocks(empty_error))

        empty_result = self.observation(
            '---INPUT---\nx\n---END INPUT---\n'
            '---RESULT---\n---END RESULT---')
        self.assertEqual(project_latest_feedback(empty_result), {
            'kind':'wrong_output','evidence_id':'obs1','input':'x','output':''})
        self.assertTrue(has_projectable_execution_blocks(empty_result))

    def test_host_selects_latest_labeled_observation(self):
        observations = self.observation('ordinary output') + [{
            'id': 'obs2', 'observation': {'content': [{
                'type': 'text', 'text': '---INPUT---\nx\n---END INPUT---\n'
                                      '---RESULT---\ny\n---END RESULT---'}],
                'command':'cd /workspace/candidate && python check.py','exit_code':0},
            'tool':'terminal'}]
        self.assertEqual(project_latest_feedback(observations), {
            'kind': 'wrong_output', 'evidence_id': 'obs2', 'input': 'x', 'output': 'y'})
        self.assertEqual(project_latest_feedback(self.observation('ordinary output'), 'still wrong'), {
            'kind': 'logic_error', 'evidence_id': 'obs1', 'summary': 'still wrong'})

    def test_complete_run_survives_later_ordinary_terminal_output(self):
        complete = self.observation(
            '---INPUT---\nvalue=custom\n---END INPUT---\n'
            '---RESULT---\nactual output\n---END RESULT---'
        )[0]
        ordinary = self.observation('git diff --stat\nworker finished')[0]
        ordinary['id'] = 'obs2'
        result = project_latest_feedback([complete, ordinary], '仍然不对')
        self.assertEqual(result['evidence_id'], 'obs1')
        self.assertEqual(result['input'], 'value=custom')
        self.assertEqual(result['output'], 'actual output')
        self.assertEqual(result['summary'], '仍然不对')

    def test_closed_execution_block_availability_is_content_free(self):
        self.assertFalse(has_projectable_execution_blocks(
            self.observation('ordinary output')))
        self.assertFalse(has_projectable_execution_blocks(
            self.observation('---INPUT---\nx\n---END INPUT---\n---RESULT---\ny')))
        self.assertTrue(has_projectable_execution_blocks(
            self.observation('---INPUT---\nx\n---END INPUT---\n'
                             '---RESULT---\ny\n---END RESULT---')))

    def test_reference_and_timed_out_outputs_are_not_public_feedback(self):
        candidate = self.observation('---INPUT---\nx\n---END INPUT---\n'
                                     '---RESULT---\nEQUAL: False\n---END RESULT---')[0]
        reference = {
            'id':'reference', 'tool':'terminal', 'observation':{
                'content':[{'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\nEQUAL: True\n---END RESULT---'}],
                'command':'cd /reference/fixed && python check.py','exit_code':0}}
        timed_out = {
            'id':'timeout', 'tool':'terminal', 'observation':{
                'content':[{'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\ncommand source\n---END RESULT---'}],
                'command':'cd /workspace/candidate && python check.py','exit_code':-1,'timeout':True}}
        self.assertEqual(project_latest_feedback([candidate, reference, timed_out])['output'], 'EQUAL: False')

    def test_public_feedback_source_excludes_reference_only_observations(self):
        reference = self.observation('comparison output')[0]
        reference['observation']['command'] = 'cd /reference/fixed && check'
        self.assertFalse(has_public_feedback_source([reference]))
        self.assertTrue(has_public_feedback_source(self.observation('candidate output')))

    def test_unclosed_and_ambiguous_blocks_fail_closed(self):
        proposal = {'evidence_id': 'obs1'}
        for text in (
            '---INPUT---\nx\n---RESULT---\ny\n---END RESULT---',
            '---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END ERROR---',
        ):
            with self.assertRaises(PublicFeedbackError):
                project_public_feedback(proposal, self.observation(text), ['obs1'])

    def test_public_view_removes_terminal_controls_but_preserves_crlf(self):
        result = project_public_feedback(
            {'evidence_id': 'obs1'},
            self.observation('---INPUT---\r\n\x1b[?2004hcall()\r\n---END INPUT---\r\n'
                             '---RESULT---\r\nline1\r\n\x1b[31mline2\x1b[0m\r\n---END RESULT---'),
            ['obs1'],
        )
        self.assertEqual(result['input'], 'call()')
        self.assertEqual(result['output'], 'line1\r\nline2')
        mapping = feedback_projection_mapping(result, self.observation(
            '---INPUT---\r\n\x1b[?2004hcall()\r\n---END INPUT---\r\n'
            '---RESULT---\r\nline1\r\n\x1b[31mline2\x1b[0m\r\n---END RESULT---'))
        self.assertTrue(mapping['input']['terminal_controls_removed'])
        self.assertTrue(mapping['output']['terminal_controls_removed'])
        self.assertNotIn('call()', str(mapping))

    def test_literal_ellipsis_expected_and_analysis_text_are_not_keyword_rejected(self):
        result = project_public_feedback(
            {'evidence_id': 'obs1'},
            self.observation('---INPUT---\npython -c "print(Ellipsis)" ... literal\n---END INPUT---\n'
                             '---RESULT---\nexpected analysis is application text\n---END RESULT---'),
            ['obs1'],
        )
        self.assertIn('...', result['input'])
        self.assertIn('expected analysis', result['output'])

    def test_layout_control_rejects_instead_of_changing_text(self):
        for value in ('a\x1b[2Db', 'a\rb', 'a\x1b]8;;https://example.test\x07link'):
            with self.assertRaisesRegex(PublicFeedbackError, 'layout-affecting'):
                project_public_feedback(
                    {'evidence_id': 'obs1'},
                    self.observation('---INPUT---\nx\n---END INPUT---\n'
                                     f'---RESULT---\n{value}\n---END RESULT---'),
                    ['obs1'],
                )

    def test_empty_result_and_extra_boundary_newlines_are_distinct(self):
        empty = project_public_feedback(
            {'evidence_id': 'obs1'},
            self.observation('---INPUT---\nx\n---END INPUT---\n---RESULT---\n---END RESULT---'),
            ['obs1'],
        )
        self.assertEqual(empty['output'], '')
        blocks = labeled_blocks('---INPUT---\n\nx\n\n---END INPUT---')
        self.assertEqual(blocks['INPUT'], '\nx\n')

    def test_latest_malformed_protocol_does_not_fall_back_to_older_result(self):
        old = self.observation('---INPUT---\nold\n---END INPUT---\n'
                               '---RESULT---\nold\n---END RESULT---')[0]
        new = self.observation('---INPUT---\nnew\n---RESULT---\nbad\n---END RESULT---')[0]
        new['id'] = 'obs2'
        with self.assertRaises(PublicFeedbackError):
            project_latest_feedback([old, new], 'summary')

    def test_feedback_units_keep_symptom_separate_from_atomic_observation(self):
        feedback = {
            'kind': 'wrong_output', 'evidence_id': 'private-test-name',
            'input': 'prefix@example.com', 'output': 'original',
            'symptom': '自定义默认值查找仍未生效',
            'summary': '传入 prefix@example.com 后实际仍是 original',
        }
        units = feedback_units(feedback)
        self.assertEqual([unit['category'] for unit in units],
                         ['symptom', 'concrete_observation'])
        self.assertNotIn('private-test-name', str(units))
        self.assertNotIn('root cause', str(units))
        self.assertEqual(units[1]['observation'], {
            'kind': 'wrong_output', 'input': 'prefix@example.com',
            'output': 'original',
            'evidence_basis': 'execution',
            'summary': '传入 prefix@example.com 后实际仍是 original',
        })
        self.assertEqual(feedback_failure_key(feedback),
                         feedback_failure_key(dict(feedback, evidence_id='next')))

    def test_raw_feedback_unit_preserves_exact_observable_values_as_one_unit(self):
        feedback = {
            'kind': 'runtime_error', 'evidence_id': 'private',
            'input': 'run --name=custom', 'error': 'ValueError: bad input',
            'symptom': '指定名称后运行仍然报错',
        }
        unit = feedback_units(feedback)[1]
        self.assertEqual(unit['observation'], {
            'kind': 'runtime_error', 'input': 'run --name=custom',
            'error': 'ValueError: bad input', 'evidence_basis': 'execution',
        })

    def test_logic_feedback_keeps_static_evidence_basis_private(self):
        units = feedback_units({
            'kind': 'logic_error', 'evidence_id': 'obs1',
            'symptom': '跨月结果还是不对',
        })
        self.assertEqual(units[0]['observation']['evidence_basis'],
                         'static_observation')

    def test_private_test_selector_cannot_become_public_symptom(self):
        with self.assertRaisesRegex(PublicFeedbackError, 'private test path'):
            project_public_feedback(
                {'evidence_id': 'obs1',
                 'symptom': 'tests/test_defaults.py::test_custom 仍然失败'},
                self.observation('actual only'),
                ['obs1'],
            )


if __name__ == '__main__':
    unittest.main()
