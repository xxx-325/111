import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from simulator.openhands.judge import candidate_hash
from simulator.openhands.progressive import ProgressiveEpisode
from simulator.openhands.reaudit_continuation import continue_run


class ContinuationSafetyTests(unittest.TestCase):
    def source(self, root):
        source = Path(root) / 'source'
        private = source / 'private'
        (private / 'judgments').mkdir(parents=True)
        (source / 'workspace/candidate').mkdir(parents=True)
        (source / 'judge-workspace/candidate').mkdir(parents=True)
        version = candidate_hash(source / 'workspace/candidate')
        job = dict(id='judge-task-1-r1', task_id='task-1', revision=1,
                   candidate_version=version, code_reply={'id': 'code1', 'text': 'done'})
        payload = dict(task_id='task-1', candidate_version=version,
                       outcome='uncertain', reason='observed mismatch was misclassified',
                       evidence_ids=[], public_feedback={}, requested_fragment_ids=[])
        record = dict(job=job, payload=payload, reviewed_payload=payload.copy(),
                      observations=[], events=[], accepted=True, status='ready',
                      review={'allowed': False},
                      disclosure={'before': [], 'after': [], 'added': [],
                                  'requirement': {'title': 'bug', 'body': 'visible'}},
                      feedback_disclosure={'failure_key': None, 'units': [],
                                           'released_unit_ids': [], 'added': []})
        saved = dict(
            schema=ProgressiveEpisode.checkpoint_schema,
            state={'status': 'paused', 'phase': 'user', 'task_id': 'task-1',
                   'task_index': 0,
                   'pause_reason': 'Judge uncertain; inspect private evidence before continuing',
                   'checks': [{'id': job['id'], 'tool': 'judge_summary'}]},
            in_flight=None,
            progressive={'job': None, 'decisions': {job['id']: record},
                         'tasks': {'task-1': {'plan': {'items': []}, 'released': [],
                            'applied_job': job['id'], 'verdict': {
                                'outcome': 'uncertain', 'revision': 1,
                                'candidate_version': version},
                            'release_history': [{'job_id': job['id'],
                                'reason': 'uncertain', 'before': [], 'after': [],
                                'added': []}]}},
                         'feedback_revision': None, 'control_results': {}},
            config={'max_seconds': 120, 'user': {}, '_reviewed_policy': {},
                    '_progressive_policy': {
                        'judge.py': 'old-policy',
                        'judge_tools.py': 'old-tools-policy',
                        'progressive.py': 'old-progressive-policy'}},
            tasks=[{'title': 'bug', 'body': 'full issue'}], public=[],
            elapsed_seconds=0, budget={})
        (private / 'checkpoint.json').write_text(json.dumps(saved))
        (private / 'budget.json').write_text('{}')
        for role in ('user', 'code', 'judge'):
            role_root = private / role
            (role_root / 'outbox').mkdir(parents=True)
            (role_root / 'inbox').mkdir()
            (role_root / 'outbox/active.json').write_text(json.dumps({'status': 'stopped'}))
            (role_root / 'inbox/config.json').write_text(json.dumps({
                'conversation_id': role + '-conversation'}))
            if role != 'user':
                (role_root / 'execution').mkdir()
                (role_root / 'execution/environment.json').write_text('{}')
        return source, job

    def test_active_or_uncertain_run_cannot_be_imported(self):
        for state, pending in [('running', None), ('paused', {'role': 'code'})]:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp)/'source'
                (source/'private').mkdir(parents=True)
                checkpoint = source/'private/checkpoint.json'
                raw = json.dumps({'state': {'status': state}, 'in_flight': pending})
                checkpoint.write_text(raw)
                output = Path(tmp)/'output'
                with patch('simulator.openhands.reaudit_continuation.review_verdict') as audit:
                    with self.assertRaises(ValueError):
                        continue_run(source, output)
                    audit.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(checkpoint.read_text(), raw)

    def test_accepted_submission_cannot_be_reaudited(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'
            (source/'private').mkdir(parents=True)
            (source/'private/checkpoint.json').write_text(json.dumps({
                'state': {'status': 'paused'}, 'in_flight': None,
                'progressive': {'job': {'id': 'j1'}, 'decisions': {'j1': {'accepted': True}}}}))
            with patch('simulator.openhands.reaudit_continuation.review_verdict') as audit:
                with self.assertRaises(ValueError):
                    continue_run(source, Path(tmp)/'output')
                audit.assert_not_called()

    def test_grounded_invalid_verdict_resumes_as_one_shot_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, job = self.source(tmp)
            output = Path(tmp) / 'output'
            review = dict(allowed=False, conclusion_valid=False, grounded=True,
                          verdict_valid=False, feedback_safe=True,
                          reasons=['observed failure requires unsolved'])
            budget = MagicMock(deadline=999999, snapshot=MagicMock(return_value={}))
            resumed = MagicMock()
            resumed.run.return_value = 'continued'
            with patch('simulator.openhands.reaudit_continuation.subprocess.run') as docker, \
                 patch('simulator.openhands.reaudit_continuation.Budget', return_value=budget), \
                 patch('simulator.openhands.reaudit_continuation.Relay'), \
                 patch('simulator.openhands.reaudit_continuation.review_verdict',
                       return_value=review) as audit, \
                 patch('simulator.openhands.reaudit_continuation.ProgressiveEpisode',
                       return_value=resumed):
                docker.return_value.returncode = 1
                self.assertEqual(continue_run(source, output), 'continued')
            audit.assert_called_once()
            resumed.run.assert_called_once_with()
            checkpoint = json.loads((output / 'private/checkpoint.json').read_text())
            record = checkpoint['progressive']['decisions'][job['id']]
            self.assertEqual(record['status'], 'verdict_pending')
            self.assertFalse(record['accepted'])
            self.assertEqual(record['payload']['outcome'], 'uncertain')
            self.assertEqual(record['observations'], [])
            self.assertEqual(record['verdict_revisions'], [])
            self.assertIsNone(checkpoint['progressive']['feedback_revision'])
            self.assertEqual(checkpoint['progressive']['job'], job)
            current = checkpoint['progressive']['tasks']['task-1']
            self.assertNotIn('applied_job', current)
            self.assertIsNone(current['verdict'])
            self.assertEqual(current['release_history'], [])
            self.assertEqual(checkpoint['state']['checks'], [])
            self.assertFalse((output / 'private/code/execution/environment.json').exists())
            self.assertFalse((output / 'private/judge/execution/environment.json').exists())

    def test_progressive_only_policy_can_be_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = self.source(tmp)
            checkpoint_path = source / 'private/checkpoint.json'
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint['config'].pop('_reviewed_policy')
            checkpoint_path.write_text(json.dumps(checkpoint))
            output = Path(tmp) / 'output'
            review = dict(allowed=False, conclusion_valid=False, grounded=True,
                          verdict_valid=False, feedback_safe=True,
                          reasons=['observed failure requires unsolved'])
            budget = MagicMock(deadline=999999, snapshot=MagicMock(return_value={}))
            resumed = MagicMock()
            resumed.run.return_value = 'continued'
            with patch('simulator.openhands.reaudit_continuation.subprocess.run') as docker, \
                 patch('simulator.openhands.reaudit_continuation.Budget', return_value=budget), \
                 patch('simulator.openhands.reaudit_continuation.Relay'), \
                 patch('simulator.openhands.reaudit_continuation.review_verdict',
                       return_value=review), \
                 patch('simulator.openhands.reaudit_continuation.ProgressiveEpisode',
                       return_value=resumed):
                docker.return_value.returncode = 1
                self.assertEqual(continue_run(source, output), 'continued')
            resumed.run.assert_called_once_with()

    def test_ungrounded_reaudit_stays_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = self.source(tmp)
            output = Path(tmp) / 'output'
            review = dict(allowed=False, conclusion_valid=False, grounded=False,
                          verdict_valid=False, feedback_safe=True,
                          reasons=['critical evidence is missing'])
            budget = MagicMock(deadline=999999, snapshot=MagicMock(return_value={}))
            with patch('simulator.openhands.reaudit_continuation.subprocess.run') as docker, \
                 patch('simulator.openhands.reaudit_continuation.Budget', return_value=budget), \
                 patch('simulator.openhands.reaudit_continuation.Relay'), \
                 patch('simulator.openhands.reaudit_continuation.review_verdict',
                       return_value=review), \
                 patch('simulator.openhands.reaudit_continuation.ProgressiveEpisode') as resumed, \
                 patch('simulator.openhands.reaudit_continuation.export_dialogue'):
                docker.return_value.returncode = 1
                self.assertEqual(continue_run(source, output), 'reaudit_rejected')
            resumed.assert_not_called()
