import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.openhands.judge import assessment_basis, review_verdict
from simulator.openhands.progressive import ProgressiveEpisode
from simulator.openhands import prepare_progressive


class StaticReferenceModeTests(unittest.TestCase):
    def test_static_basis_names_limits_without_claiming_execution(self):
        static = assessment_basis('static_reference')
        self.assertIn('静态', static)
        self.assertIn('未运行', static)
        self.assertIn('不把静态推断写成实际报错或已测试', static)
        self.assertEqual(assessment_basis('default'), '依据当前候选的实际检查判断。')
        with self.assertRaises(ValueError):
            assessment_basis('relaxed')

    def test_mode_reaches_judgment_task_and_review_input(self):
        episode = object.__new__(ProgressiveEpisode)
        episode.config = {'verification_mode': 'static_reference'}
        episode.saved = {'tasks': [{'title': 'original', 'body': 'private', 'kind': 'commit'}]}
        episode.state = type('State', (), {'data': {'task_index': 0}})()
        episode.current = lambda: {'requirement_document': {'title': '需求', 'body': '静态文档迁移'}}
        task = episode.judgment_task()
        self.assertEqual(task['verification_mode'], 'static_reference')

        payload = {'outcome': 'solved', 'reason': 'source comparison', 'public_feedback': {}}
        job = {'candidate_version': 'candidate-hash', 'code_reply': 'done'}
        with patch('simulator.openhands.judge.obvious_leak', return_value=None), \
                patch('simulator.openhands.judge.collect_sources', return_value=[]), \
                patch('simulator.openhands.judge.source_check', return_value={'matches': []}), \
                patch('simulator.openhands.judge.call_json',
                      return_value={'verdict_valid': True, 'grounded': True,
                                    'feedback_safe': True,
                                    'required_failure_observed': False,
                                    'reasons': []}) as call:
            result = review_verdict(None, task, job, payload, [], [],
                                    {'title': '当前需求', 'body': '迁移文档'}, {'items': []})
        self.assertTrue(result['allowed'])
        review_input = call.call_args.args[2]
        self.assertEqual(review_input['assessment_basis'], assessment_basis('static_reference'))
        self.assertIn('未运行', review_input['assessment_basis'])


class ContinuePreparationTests(unittest.TestCase):
    class Budget:
        deadline = None

        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self):
            return {'calls': 0}

    @staticmethod
    def task(index):
        return {'title': f'task {index}', 'body': f'body {index}', 'kind': 'commit',
                'identifier': f'commit-{index}', 'reference': f'ref-{index}', 'base': f'base-{index}'}

    def run_main(self, audit_result, prepared_results=None):
        prepared_results = list(prepared_results or [])
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root/'config.json'
        source = root/'source.json'
        output = root/'output'
        env = root/'env'
        config.write_text(json.dumps({'user': {'model': 'fixture'}}))
        env.write_text('')
        tasks = [self.task(i) for i in range(1, 4)]
        frozen = {'id': 'requirement-1', 'issue': {'title': 'task 1', 'body': 'body 1'},
                  'plan': {'schema': 'fixture', 'items': []},
                  'review': {'allowed': False, 'reasons': ['old review']}}
        source.write_text(json.dumps({'status': 'rejected', 'cases': [frozen]}))
        argv = ['prepare_progressive', '--config', str(config), '--env-file', str(env),
                '--output', str(output), '--review-source', str(source), '--continue-after-review']
        calls = []

        def prepare_commit(_relay, task, _repo):
            calls.append(task['title'])
            return prepared_results.pop(0)

        patches = (
            patch.object(sys, 'argv', argv),
            patch.object(prepare_progressive, 'load_environment'),
            patch.object(prepare_progressive, 'Budget', self.Budget),
            patch.object(prepare_progressive, 'Relay', return_value=object()),
            patch.object(prepare_progressive, 'prepare', return_value=(Path('/repo'), 'base', tasks)),
            patch.object(prepare_progressive, 'audit_issue', return_value=audit_result),
            patch.object(prepare_progressive, 'prepare_commit', side_effect=prepare_commit),
            patch.object(prepare_progressive, 'render_preparation'),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5] as audit, patches[6] as generate, patches[7]:
            prepare_progressive.main()
        report = json.loads((output/'report.json').read_text())
        return directory, report, calls, audit, generate

    def test_approved_frozen_prefix_continues_in_order_without_regeneration(self):
        passed = {'allowed': True, 'reasons': [], 'audit_version': 'v6'}
        generated = [
            {'plan': {'items': ['second']}, 'review': {'allowed': True, 'reasons': []}},
            {'plan': {'items': ['third']}, 'review': {'allowed': True, 'reasons': []}},
        ]
        directory, report, calls, audit, generate = self.run_main(passed, generated)
        self.addCleanup(directory.cleanup)
        self.assertEqual(report['status'], 'candidate_pass')
        self.assertEqual(calls, ['task 2', 'task 3'])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(audit.call_count, 1)
        self.assertEqual(report['cases'][0]['plan'], {'schema': 'fixture', 'items': []})
        self.assertEqual(report['cases'][0]['source'], '原始拆解重审，未重新生成')
        self.assertEqual([case['id'] for case in report['cases']],
                         ['requirement-1', 'requirement-2', 'requirement-3'])

    def test_rejected_frozen_prefix_stops_before_any_new_preparation(self):
        rejected = {'allowed': False, 'reasons': ['still unsupported'], 'audit_version': 'v6'}
        directory, report, calls, audit, generate = self.run_main(rejected)
        self.addCleanup(directory.cleanup)
        self.assertEqual(report['status'], 'rejected')
        self.assertEqual(calls, [])
        generate.assert_not_called()
        self.assertEqual(audit.call_count, 1)
        self.assertEqual(len(report['cases']), 1)
        self.assertEqual(report['cases'][0]['review'], rejected)

    def test_rejection_in_new_suffix_stops_later_tasks(self):
        passed = {'allowed': True, 'reasons': [], 'audit_version': 'v6'}
        generated = [
            {'plan': {'items': ['second']},
             'review': {'allowed': False, 'reasons': ['bad second task']}},
        ]
        directory, report, calls, _audit, generate = self.run_main(passed, generated)
        self.addCleanup(directory.cleanup)
        self.assertEqual(report['status'], 'rejected')
        self.assertEqual(calls, ['task 2'])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual([case['id'] for case in report['cases']],
                         ['requirement-1', 'requirement-2'])


if __name__ == '__main__':
    unittest.main()
