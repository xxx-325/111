import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from simulator.openhands.judge import bind_submission, validate_verdict, candidate_hash
from simulator.openhands.feedback_projection import project_public_feedback
from simulator.openhands.disclosure import release_after
from simulator.openhands.progressive import (
    ProgressiveEpisode,
    attach_pending_judge_policy_notice,
    bind_current_judge_evidence,
    plan_turn_disclosure,
)
from simulator.openhands.state import TaskState, TransitionError
from simulator.openhands.budget import Budget
from simulator.openhands.issue_stages import validate
from simulator.openhands.progressive_report import render


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.job = dict(id='judge-task-1-r1', task_id='task-1', revision=1, candidate_version='hash',
                        code_reply={'id':'code1','text':'done'})
        self.payload = dict(task_id='task-1', candidate_version='hash', outcome='unsolved', reason='failure',
                            evidence_ids=['obs1'], public_feedback={
                                'kind':'logic_error','evidence_id':'obs1',
                                'symptom':'结果仍然错误'})
        self.plan=validate({'items':[
            dict(id='s1',category='symptom',text='visible',source_quote='bug',requires=[],related=[]),
            dict(id='c1',category='cause',text='hidden',source_quote='hint',requires=[],related=['s1'])]},
            {'title':'bug','body':'hint'})

    def test_current_version_and_real_evidence(self):
        validate_verdict(self.payload, self.job, [{'id':'obs1'}], self.plan)
        for change in ({'candidate_version':'old'}, {'task_id':'future'}, {'evidence_ids':['invented']},
                       {'evidence_ids':[]}, {'clarification_stage':3}):
            with self.assertRaises(ValueError):
                validate_verdict(dict(self.payload, **change), self.job, [{'id':'obs1'}], self.plan)

    def test_judge_model_output_excludes_host_owned_identity(self):
        from simulator.openhands.judge_tools import (VerdictAction,
            VerdictRevisionAction, FeedbackRevisionAction)
        verdict_fields=set(VerdictAction.model_json_schema()['properties'])
        self.assertEqual(verdict_fields, {
            'kind','outcome','reason','feedback','feedback_detail',
            'requested_fragment_id'})
        self.assertEqual(set(FeedbackRevisionAction.model_json_schema()['properties']),
                         {'kind','feedback','feedback_detail'})
        self.assertEqual(set(VerdictRevisionAction.model_json_schema()['properties']),
                         {'kind','outcome','reason','feedback','feedback_detail'})
        payload=bind_submission(dict(outcome='uncertain',reason='missing evidence',
            feedback='',requested_fragment_id='c1'),self.job,[])
        self.assertEqual(payload['task_id'],'task-1')
        self.assertEqual(payload['candidate_version'],'hash')
        self.assertEqual(payload['evidence_ids'],[])
        self.assertEqual(payload['public_feedback'],{})
        self.assertEqual(payload['requested_fragment_ids'],['c1'])

        observations=[{'id':'real-uuid','tool':'terminal','observation':{'content':[{
            'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END RESULT---'}],
            'command':'cd /workspace/candidate && python check.py','exit_code':0}}]
        payload=bind_submission(dict(outcome='unsolved',reason='wrong result',
            feedback='命令输出仍然不正确',feedback_detail='',
            requested_fragment_id=''),self.job,observations)
        self.assertEqual(payload['evidence_ids'],['real-uuid'])
        self.assertEqual(payload['public_feedback']['input'],'x')
        self.assertEqual(payload['public_feedback']['output'],'y')

    def test_policy_notice_binds_once_to_next_normal_judge_command(self):
        saved={'judge_policy_notice': {'text': 'approved policy update'}}
        prompt={}
        self.assertTrue(attach_pending_judge_policy_notice(saved,prompt,'turn-j1'))
        self.assertEqual(prompt['policy_update'],'approved policy update')
        self.assertEqual(saved['judge_policy_notice']['assigned_command_id'],'turn-j1')
        saved['judge_policy_notice']['delivered_command_id']='turn-j1'
        next_prompt={}
        self.assertFalse(attach_pending_judge_policy_notice(saved,next_prompt,'turn-j2'))
        self.assertNotIn('policy_update',next_prompt)

    def test_bound_submission_validates_as_canonical_feedback(self):
        observations=[{'id':'real-uuid','tool':'terminal','observation':{'content':[{
            'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END RESULT---'}],
            'command':'cd /workspace/candidate && python check.py','exit_code':0}}]
        payload=bind_submission(dict(outcome='unsolved',reason='wrong result',
            feedback='命令输出仍然不正确',feedback_detail='',
            requested_fragment_id=''),self.job,observations)
        validate_verdict(payload,self.job,observations,self.plan)
        for feedback in (dict(payload['public_feedback'],output='model copy'),
                         dict(payload['public_feedback'],extra='untrusted')):
            with self.assertRaises(ValueError):
                validate_verdict(dict(payload,public_feedback=feedback),self.job,observations,self.plan)

    def test_first_judge_verdict_does_not_request_feedback_revision(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            root=Path(root)
            (root/'judge-workspace/candidate').mkdir(parents=True)
            (root/'workspace/candidate').mkdir(parents=True)
            job=dict(self.job,candidate_version=candidate_hash(root/'workspace/candidate'))
            episode.progress['job']=job
            episode.saved.update(tasks=[dict(title='bug',body='hint')],public=[],control_results={})
            observations=[{'id':'obs1','tool':'terminal','observation':{'content':[{
                'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END RESULT---'}],
                'command':'cd /workspace/candidate && python check.py','exit_code':0}}]
            episode.judge_events=MagicMock(return_value=([],observations))
            episode.agents={'judge':MagicMock()}
            safe=dict(allowed=True,conclusion_valid=True,feedback_safe=True,reasons=[],semantic={},source_check={})
            packet=dict(request_id='verdict1',operation='judge_verdict',payload=dict(
                outcome='unsolved',reason='wrong result',
                feedback='命令输出仍然不正确',feedback_detail='',
                requested_fragment_id=''))
            with patch('simulator.openhands.progressive.review_verdict',return_value=safe):
                result=episode.judge_control(packet)
            self.assertTrue(result['accepted'])
            self.assertFalse(result['feedback_revision_required'])
            record=episode.progress['decisions'][job['id']]
            self.assertEqual(record['status'],'ready')
            self.assertEqual(record['payload']['public_feedback']['output'],'y')

    def test_invalid_solved_validation_is_retained_for_one_verdict_correction(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            root = Path(root)
            (root / 'judge-workspace/candidate').mkdir(parents=True)
            (root / 'workspace/candidate').mkdir(parents=True)
            job = dict(self.job, candidate_version=candidate_hash(root / 'workspace/candidate'))
            episode.progress['job'] = job
            episode.saved.update(tasks=[dict(title='bug', body='hint')], public=[], control_results={})
            observations = [{
                'id': 'pytest-failed', 'tool': 'terminal',
                'observation': {'command': 'pytest -q', 'exit_code': 1},
                'exit_code': 1,
            }]
            episode.judge_events = MagicMock(return_value=([], observations))
            episode.agents = {'judge': MagicMock()}
            packet = dict(
                request_id='invalid-solved', operation='judge_verdict',
                payload=dict(outcome='solved', reason='looks fixed',
                             feedback='', requested_fragment_id=''),
            )
            with patch('simulator.openhands.progressive.review_verdict') as review:
                result = episode.judge_control(packet)
            record = episode.progress['decisions'][job['id']]
            self.assertTrue(result['accepted'])
            self.assertTrue(result['verdict_revision_required'])
            self.assertEqual(record['status'], 'verdict_pending')
            self.assertIn('successful validation exit',
                          record['verdict_rejections'][0]['reason'])
            self.assertEqual(record['disclosure']['before'], ['s1'])
            self.assertEqual(record['disclosure']['after'], ['s1'])
            review.assert_not_called()

    def test_valid_conclusion_with_bad_latest_block_requests_feedback_revision(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            root = Path(root)
            (root / 'judge-workspace/candidate').mkdir(parents=True)
            (root / 'workspace/candidate').mkdir(parents=True)
            job = dict(self.job, candidate_version=candidate_hash(root / 'workspace/candidate'))
            episode.progress['job'] = job
            episode.saved.update(tasks=[dict(title='bug', body='hint')], public=[], control_results={})
            observations = [{'id': 'obs1', 'tool': 'terminal', 'observation': {
                'content': [{'type': 'text', 'text':
                    '---INPUT---\nx\n---END INPUT---\n---RESULT---\ny'}],
                'command': 'python check.py', 'exit_code': 0}}]
            episode.judge_events = MagicMock(return_value=([], observations))
            episode.agents = {'judge': MagicMock()}
            review = dict(allowed=True, conclusion_valid=True, feedback_safe=True,
                          reasons=[], semantic={}, source_check={})
            packet = dict(request_id='bad-feedback', operation='judge_verdict', payload=dict(
                outcome='unsolved', reason='wrong result', feedback='', requested_fragment_id=''))
            with patch('simulator.openhands.progressive.review_verdict', return_value=review):
                result = episode.judge_control(packet)
            record = episode.progress['decisions'][job['id']]
            self.assertTrue(result['accepted'])
            self.assertTrue(result['feedback_revision_required'])
            self.assertEqual(record['status'], 'feedback_pending')
            self.assertEqual(record['submitted_payload'], packet['payload'])
            self.assertEqual(record['payload']['outcome'], 'unsolved')
            self.assertEqual(record['payload']['public_feedback'], {})

    def test_host_projects_raw_feedback_from_labeled_execution(self):
        observations=[{'id':'obs1','observation':{'command':'private reproduction command',
            'content':[{'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END RESULT---'}]}}]
        proposal={'evidence_id':'obs1'}
        self.assertEqual(project_public_feedback(proposal,observations,['obs1']),{
            'kind':'wrong_output','evidence_id':'obs1','input':'x','output':'y'})
        with self.assertRaises(ValueError):
            project_public_feedback(dict(proposal,evidence_id='obs2'),observations,['obs1'])
        observations[0]['observation']['content'][0]['text']='no labeled public blocks'
        with self.assertRaises(ValueError):
            project_public_feedback(proposal,observations,['obs1'])

    def test_late_judge_audit_is_invalid_and_does_not_release(self):
        from simulator.openhands.relay import RequestContext
        import time
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            root=Path(root)
            (root/'judge-workspace/candidate').mkdir(parents=True)
            (root/'workspace/candidate').mkdir(parents=True)
            job=dict(self.job,candidate_version=candidate_hash(root/'workspace/candidate'))
            episode.progress['job']=job
            episode.saved.update(tasks=[dict(title='bug',body='hint')],public=[],control_results={})
            observations=[{'id':'obs1','tool':'terminal','observation':{'content':[{
                'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END RESULT---'}],
                'command':'python check.py','exit_code':0}}]
            episode.judge_events=MagicMock(return_value=([],observations))
            episode.agents={'judge':MagicMock()}
            cancel=root/'cancel'
            def late_review(*args, **kwargs):
                cancel.touch()
                return dict(conclusion_valid=True,feedback_safe=True,reasons=[])
            packet=dict(request_id='late',operation='judge_verdict',payload=dict(
                outcome='unsolved',reason='wrong result',feedback='',requested_fragment_id=''),
                _request_context=RequestContext(time.time()+30,cancel))
            with patch('simulator.openhands.progressive.review_verdict',side_effect=late_review):
                result=episode.judge_control(packet)
            self.assertFalse(result['accepted'])
            record=episode.progress['decisions'][job['id']]
            self.assertEqual(record['status'],'invalid')
            self.assertIn('TimeoutError',record['error'])
            self.assertEqual(episode.current()['released'],['s1'])
            self.assertIsNone(episode.current()['verdict'])
    def test_private_judge_ids_never_enter_user_state_operations(self):
        packet={'payload':{'evidence_ids':['raw1'],'resolves':['raw1'],
            'dismisses':['raw2'],'clears_blockers':['raw1'],
            'blocker':{'evidence_ids':['raw1']}}}
        current={'applied_job':'summary1','simulated_experience':{'evidence_ids':['raw1','raw2']}}
        bind_current_judge_evidence(packet,current)
        self.assertEqual(packet['payload']['evidence_ids'],['summary1'])
        self.assertEqual(packet['payload']['resolves'],[])
        self.assertEqual(packet['payload']['dismisses'],[])
        self.assertEqual(packet['payload']['clears_blockers'],[])
        self.assertEqual(packet['payload']['blocker']['evidence_ids'],['summary1'])

    def test_failure_success_uncertainty_and_clarification(self):
        self.assertEqual(release_after(self.plan,['s1'],self.payload),['s1','c1'])
        self.assertEqual(release_after(self.plan,['s1'],dict(outcome='solved')),['s1'])
        self.assertEqual(release_after(self.plan,['s1'],dict(outcome='uncertain')),['s1'])
        query=dict(self.payload,outcome='uncertain',requested_fragment_ids=['c1'])
        validate_verdict(query,self.job,[{'id':'obs1'}],self.plan)
        with self.assertRaises(ValueError):
            validate_verdict(dict(query,outcome='solved'),self.job,[{'id':'obs1'}],self.plan)

    def episode(self, root):
        episode = ProgressiveEpisode.__new__(ProgressiveEpisode)
        episode.root=Path(root)
        episode.private=Path(root)
        (episode.private/'judgments').mkdir(exist_ok=True)
        episode.lock=threading.RLock()
        episode.state=TaskState()
        episode.state.data['code_reply']={'id':'code1','text':'done'}
        episode.saved=dict(revision=1)
        episode.progress=dict(tasks={'task-1':dict(plan=self.plan,released=['s1'],verdict=None,feedback=None,
                                                       requirement_document={'title':'Real issue title','body':'private body'})},
                              job=self.job,decisions={},control_results={})
        episode.config={'tasks':[{'user_run_commands':[{'command':'hidden command'}]}]}
        episode.persist=MagicMock()
        return episode

    def verdict_pending_episode(self, root):
        episode = self.episode(root)
        root = Path(root)
        for name in ('workspace/candidate', 'judge-workspace/candidate'):
            (root / name).mkdir(parents=True)
        job = dict(self.job, candidate_version=candidate_hash(root / 'workspace/candidate'))
        observations = [{'id': 'obs1', 'tool': 'terminal', 'observation': {
            'content': [{'type': 'text', 'text':
                '---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END RESULT---'}],
            'command': 'python check.py', 'exit_code': 0}}]
        payload = dict(task_id='task-1', candidate_version=job['candidate_version'],
            outcome='uncertain', reason='misclassified observed mismatch',
            evidence_ids=['obs1'], public_feedback={}, requested_fragment_ids=[])
        record = dict(job=job, payload=payload, reviewed_payload=payload.copy(),
            accepted=False, status='verdict_pending', events=[],
            observations=observations,
            disclosure={'before': ['s1'], 'after': ['s1'], 'added': [],
                        'requirement': {'title': 'bug', 'body': 'visible'}},
            feedback_disclosure={'failure_key': None, 'units': [],
                                 'released_unit_ids': [], 'added': []},
            verdict_rejections=[{'reason': 'observed mismatch must be unsolved'}],
            verdict_revisions=[])
        episode.progress.update(job=job, decisions={job['id']: record},
            feedback_revision={'kind': 'verdict', 'verdict_id': job['id'],
                               'version': 1,
                               'reason': 'observed mismatch must be unsolved',
                               'event_start': 0},
            control_results={})
        episode.saved.update(tasks=[dict(title='bug', body='hint')], public=[],
                             control_results={})
        episode.agents = {'judge': MagicMock()}
        episode.agents['judge'].events.return_value = [
            {'tool_name': 'revise_verdict'}]
        return episode, record

    def test_visibility_and_no_early_run_command_disclosure(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            self.assertNotIn('hidden',str(episode.requirement()))
            self.assertEqual(episode.user_run_commands(),[])
            episode.current()['released']=['s1','c1']
            self.assertIn('hidden',str(episode.requirement()))
            self.assertEqual(len(episode.user_run_commands()),1)

    def test_judge_summary_not_raw_logs_and_idempotent_release(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            target, disclosure = plan_turn_disclosure(episode.current(), self.payload)
            record=dict(job=self.job,payload=self.payload,accepted=True,
                        observations=['PRIVATE TEST SOURCE'],
                        disclosure={'after': target}, feedback_disclosure=disclosure)
            episode.apply_decision(record)
            episode.apply_decision(record)
            self.assertEqual(episode.current()['released'],['s1'])
            self.assertEqual(len(episode.current()['released_feedback_unit_ids']), 1)
            self.assertEqual(len(episode.state.data['checks']),1)
            self.assertEqual(len(episode.current()['release_history']),1)
            self.assertNotIn('PRIVATE TEST SOURCE',json.dumps(episode.state.view()))
            with self.assertRaises(TransitionError):episode.acceptance_gate()

    def test_uncertain_pauses_without_releasing_or_accepting(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            episode.apply_decision(dict(job=self.job,payload=dict(self.payload,outcome='uncertain'),accepted=True))
            self.assertEqual(episode.current()['released'],['s1'])
            self.assertEqual(episode.state.data['status'],'paused')
            self.assertEqual(episode.state.data['accepted'],[])

    def test_solved_authority_invalidated_by_code_changes(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            code=Path(root)/'workspace/candidate'
            code.mkdir(parents=True)
            file=code/'a.py';file.write_text('original')
            episode.current()['verdict']=dict(outcome='solved',revision=1,candidate_version=candidate_hash(code))
            episode.acceptance_gate()
            file.write_text('changed')
            with self.assertRaises(TransitionError):episode.acceptance_gate()

    def publish_solved_followup(self, episode, root, state):
        candidate = Path(root) / 'workspace/candidate'
        candidate.mkdir(parents=True, exist_ok=True)
        (candidate / 'a.py').write_text('original')
        version = candidate_hash(candidate)
        episode.current()['verdict'] = dict(
            outcome='solved', revision=1, candidate_version=version,
            required_tests=None,
        )
        episode.current()['applied_job'] = 'judge-task-1-r1'
        episode.progress['job'] = None
        episode.state.data['checks'] = [{
            'id': 'judge-task-1-r1', 'revision': 1, 'tool': 'judge_summary',
            'source': 'Judge', 'result': 'passed',
            'summary': {'outcome': 'solved'},
        }]
        permit = episode.state.transition({
            'task_id': 'task-1', 'state': state,
            'control': 'REFINE' if state == 'PLAN' else 'CONTINUE',
            'reason': 'Ask a task-scoped follow-up',
            'evidence_ids': ['judge-task-1-r1'],
        })
        payload, attachment = episode.prepare_send_payload({
            'task_id': 'task-1', 'permit_id': permit['id'],
            'text': 'Explain the implementation strategy.',
            'evidence_ids': ['judge-task-1-r1'],
        })
        self.assertIsNone(attachment)
        message = episode.state.send(payload)
        message['published'] = True
        episode.state.record_published_transition(message['transition_id'])
        episode.after_message_published(message)
        episode.saved['revision'] = 2
        episode.state.data.update(
            phase='user', code_reply={'id': 'code2', 'text': 'Here is the explanation.'}
        )
        return candidate, message

    def test_read_only_post_solved_reply_carries_verdict_without_judge_check(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            _, message = self.publish_solved_followup(
                episode, root, 'UNDERSTAND'
            )
            episode.saved['closing'] = False
            episode.agents = {'code': MagicMock()}
            checks_before = list(episode.state.data['checks'])

            episode.before_user_turn()

            verdict = episode.current()['verdict']
            self.assertEqual(verdict['revision'], 2)
            self.assertEqual(episode.state.data['checks'], checks_before)
            self.assertEqual(episode.current()['verdict_carries'], [{
                'schema': 'solved-verdict-carry-v1',
                'task_id': 'task-1',
                'message_id': message['id'],
                'source_evidence_id': 'judge-task-1-r1',
                'origin_revision': 1,
                'current_revision': 2,
                'state': 'UNDERSTAND',
                'candidate_version': verdict['candidate_version'],
            }])
            episode.agents['code'].pause.assert_not_called()
            self.assertTrue(episode.carry_post_solved_verdict())
            self.assertEqual(len(episode.current()['verdict_carries']), 1)

    def snapshot_episode(self, root):
        episode = self.episode(root)
        episode.progress['job'] = None
        episode.saved['tasks'] = [dict(kind='issue', title='bug', body='hint')]
        episode.config['repository'] = root
        episode.agents = {'code': MagicMock(), 'judge': MagicMock()}
        episode.agents['judge'].events.return_value = []
        episode.start_judge = MagicMock()
        episode.judgment_task = MagicMock(return_value=episode.saved['tasks'][0])
        candidate = Path(root) / 'workspace/candidate'
        mirror = Path(root) / 'judge-workspace/candidate'
        for directory in (candidate, mirror):
            (directory / 'docs').mkdir(parents=True)
        (candidate / 'docs/prompts.md').write_text('complete document\n')
        (mirror / 'docs/prompts.md').write_text('truncated\n')
        episode.agents['judge'].sandbox.candidate_hash.side_effect = lambda: candidate_hash(mirror)
        return episode, candidate, mirror

    def test_container_view_mismatch_rejects_even_when_host_trees_match(self):
        with tempfile.TemporaryDirectory() as root:
            episode, candidate, mirror = self.snapshot_episode(root)
            episode.agents['judge'].sandbox.candidate_hash.side_effect = None
            episode.agents['judge'].sandbox.candidate_hash.return_value = 'stale-container'
            with self.assertRaisesRegex(RuntimeError, 'Judge 容器内候选与宿主快照不同步'):
                episode.before_user_turn()
            self.assertEqual(candidate_hash(candidate), candidate_hash(mirror))
            episode.agents['judge'].turn.assert_not_called()
            self.assertIsNone(episode.progress['job'])

    def test_scenario_judge_receives_public_tool_view_after_edits(self):
        with tempfile.TemporaryDirectory() as root:
            episode, candidate, mirror = self.snapshot_episode(root)
            scenario = dict(repository_edits=[dict(path='docs/prompts.md', before='complete document\n',
                                                   after='controlled candidate\n')])
            episode.saved['tasks'][0]['scenario'] = scenario
            episode.judgment_task.return_value.update(scenario_context={'current_facts': []})
            episode.current()['fact_triggers'] = {'external1': dict(trigger='Code tries the old path')}
            episode.saved['public'] = [dict(id='u', kind='user', timestamp=1, text='Try this'),
                dict(id='call', kind='tool_call', timestamp=2, call_id='x', tool_name='terminal'),
                dict(id='obs', kind='tool_result', timestamp=3, call_id='x', tool_name='terminal',
                     observation={'PRIVATE': 'not delivered'}),
                dict(id='code1', kind='assistant', phase='final', timestamp=4, text='It failed')]
            (episode.private / 'code').mkdir()
            (episode.private / 'code/provider.jsonl').write_text(json.dumps(dict(kind='request', input={'messages': [
                dict(role='assistant', tool_calls=[dict(id='x', function=dict(name='terminal',
                     arguments=json.dumps({'command': 'python old.py'})))]),
                dict(role='tool', tool_call_id='x', content='old route failed')]})))
            episode.budget = MagicMock()
            episode.agents['judge'].turn.side_effect = RuntimeError('test Judge reached')
            with self.assertRaisesRegex(RuntimeError, 'test Judge reached'):
                episode.before_user_turn()
            prompt = json.loads(episode.agents['judge'].turn.call_args.args[0])
            self.assertEqual(prompt['public_turn'][2]['text'], 'old route failed')
            self.assertNotIn('PRIVATE', json.dumps(prompt))
            self.assertEqual((mirror / 'docs/prompts.md').read_text(), 'controlled candidate\n')
            self.assertEqual(episode.progress['job']['public_turn'], prompt['public_turn'])

    def test_new_judge_review_refreshes_stale_snapshot_and_binds_its_hash(self):
        with tempfile.TemporaryDirectory() as root:
            episode, candidate, mirror = self.snapshot_episode(root)
            episode.current()['verdict'] = dict(outcome='unsolved', revision=0)
            (mirror / 'deleted.md').write_text('old file')
            def check_start():
                self.assertEqual((mirror / 'docs/prompts.md').read_bytes(),
                                 (candidate / 'docs/prompts.md').read_bytes())
                self.assertFalse((mirror / 'deleted.md').exists())
                self.assertEqual(candidate_hash(mirror), candidate_hash(candidate))
            episode.start_judge.side_effect = check_start
            episode.agents['judge'].turn.side_effect = RuntimeError('test Judge reached')
            with self.assertRaisesRegex(RuntimeError, 'test Judge reached'):
                episode.before_user_turn()
            episode.start_judge.assert_called_once_with()
            episode.agents['judge'].turn.assert_called_once()
            self.assertEqual(episode.progress['job']['candidate_version'],
                             candidate_hash(mirror))
            episode.agents['code'].pause.assert_called_once_with()
            episode.agents['code'].unpause.assert_called_once_with()

    def test_skipped_snapshot_sync_rejects_review_before_judge_starts(self):
        with tempfile.TemporaryDirectory() as root:
            episode, _, _ = self.snapshot_episode(root)
            episode.sync_judge = MagicMock()
            with self.assertRaisesRegex(RuntimeError, 'judge 镜像与候选不同步'):
                episode.before_user_turn()
            episode.sync_judge.assert_called_once_with()
            episode.start_judge.assert_not_called()
            episode.agents['judge'].turn.assert_not_called()
            self.assertIsNone(episode.progress['job'])
            self.assertEqual(episode.progress['decisions'], {})
            episode.agents['code'].unpause.assert_called_once_with()

    def test_snapshot_guard_catches_rsync_same_size_and_mtime_stale_content(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            episode, candidate, mirror = self.snapshot_episode(root)
            for directory, content in ((candidate, 'new\n'), (mirror, 'old\n')):
                source = directory / 'docs/prompts.md'
                source.write_text(content)
                os.utime(source, (1700000000, 1700000000))
            with self.assertRaisesRegex(RuntimeError, 'judge 镜像与候选不同步'):
                episode.before_user_turn()
            episode.start_judge.assert_not_called()
            episode.agents['judge'].turn.assert_not_called()
            self.assertIsNone(episode.progress['job'])

    def test_cached_verdict_or_decision_does_not_start_a_new_review(self):
        for cached in ('verdict', 'decision'):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as root:
                episode, _, _ = self.snapshot_episode(root)
                episode.sync_judge = MagicMock()
                if cached == 'verdict':
                    episode.current()['verdict'] = dict(outcome='unsolved', revision=1)
                else:
                    record = {'job': self.job}
                    episode.progress.update(job=self.job, decisions={self.job['id']: record})
                    episode.resolve_decision = MagicMock(return_value=record)
                    episode.apply_decision = MagicMock()
                episode.before_user_turn()
                episode.sync_judge.assert_not_called()
                episode.start_judge.assert_not_called()
                episode.agents['judge'].turn.assert_not_called()
                if cached == 'decision':
                    episode.apply_decision.assert_called_once_with(record)

    def test_consecutive_read_only_followups_reuse_original_judge_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            self.publish_solved_followup(episode, root, 'UNDERSTAND')
            self.assertTrue(episode.carry_post_solved_verdict())
            checks_before = list(episode.state.data['checks'])
            for revision, state in ((3, 'PLAN'), (4, 'RETRIEVE')):
                permit = episode.state.transition(dict(task_id='task-1', state=state,
                    control='REFINE', reason='Discuss the current implementation'))
                payload, _ = episode.prepare_send_payload(dict(task_id='task-1',
                    permit_id=permit['id'], text='Explain the current approach'))
                message = episode.state.send(payload)
                message['published'] = True
                episode.after_message_published(message)
                episode.saved['revision'] = revision
                episode.state.data.update(phase='user',
                    code_reply={'id': f'code{revision}', 'text': 'Explanation only'})
                self.assertTrue(episode.carry_post_solved_verdict())
                self.assertTrue(episode.carry_post_solved_verdict())
                episode.acceptance_gate()
            self.assertEqual(episode.state.data['checks'], checks_before)
            self.assertEqual(len(episode.current()['verdict_carries']), 3)

    def test_second_carry_requires_matching_prior_record(self):
        invalidations = (
            ('task_id', 'task-0'), ('candidate_version', 'old-hash'),
            ('source_evidence_id', 'other-judge'), ('current_revision', 0),
            ('schema', 'unknown'), ('state', 'OPERATE'),
        )
        for key, value in invalidations:
            with self.subTest(field=key), tempfile.TemporaryDirectory() as root:
                episode = self.episode(root)
                self.publish_solved_followup(episode, root, 'UNDERSTAND')
                self.assertTrue(episode.carry_post_solved_verdict())
                permit = episode.state.transition(dict(task_id='task-1', state='PLAN',
                    control='REFINE', reason='Discuss the current implementation'))
                payload, _ = episode.prepare_send_payload(dict(task_id='task-1',
                    permit_id=permit['id'], text='Explain this approach'))
                message = episode.state.send(payload)
                message['published'] = True
                episode.after_message_published(message)
                episode.saved['revision'] = 3
                episode.state.data.update(phase='user',
                    code_reply={'id': 'code3', 'text': 'Explanation only'})
                episode.current()['verdict_carries'][0][key] = value
                self.assertFalse(episode.carry_post_solved_verdict())
                with self.assertRaises(TransitionError):
                    episode.acceptance_gate()

    def test_changed_candidate_or_operate_followup_requires_judge(self):
        for state, change_candidate in (('PLAN', True), ('PLAN', 'mode'), ('OPERATE', False)):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as root:
                episode = self.episode(root)
                candidate, _ = self.publish_solved_followup(episode, root, state)
                if change_candidate == 'mode':
                    source = candidate / 'a.py'
                    source.chmod(source.stat().st_mode ^ 0o111)
                elif change_candidate:
                    (candidate / 'a.py').write_text('changed')
                episode.saved['closing'] = False
                episode.progress['job'] = None
                episode.agents = {'code': MagicMock()}
                episode.sync_judge = MagicMock(
                    side_effect=RuntimeError('normal Judge path reached')
                )

                with self.assertRaisesRegex(RuntimeError, 'normal Judge path reached'):
                    episode.before_user_turn()

                pending = episode.current()['post_solved_followup']
                self.assertEqual(pending['status'], 'judge_required')
                self.assertEqual(pending['current_revision'], 2)
                self.assertEqual(episode.current()['verdict']['revision'], 1)
                episode.sync_judge.assert_called_once_with()
                episode.agents['code'].pause.assert_called_once_with()
                episode.agents['code'].unpause.assert_called_once_with()

    def test_stale_task_cannot_carry_solved_verdict(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            self.publish_solved_followup(episode, root, 'RETRIEVE')
            episode.current()['post_solved_followup']['task_id'] = 'task-0'
            self.assertFalse(episode.carry_post_solved_verdict())
            self.assertEqual(
                episode.current()['post_solved_followup']['status'],
                'judge_required',
            )
            self.assertEqual(episode.current()['verdict']['revision'], 1)

    def acceptance_episode(self, root, state):
        episode = self.episode(root)
        candidate = Path(root) / 'workspace/candidate'
        candidate.mkdir(parents=True, exist_ok=True)
        version = candidate_hash(candidate)
        episode.current()['verdict'] = dict(
            outcome='solved', revision=1, candidate_version=version,
            required_tests=None,
        )
        episode.current()['applied_job'] = 'judge-task-1-r1'
        episode.progress['job'] = None
        episode.state.data['checks'] = [{
            'id': 'judge-task-1-r1', 'revision': 1, 'tool': 'judge_summary',
            'source': 'Judge', 'result': 'passed',
            'summary': {'outcome': 'solved'},
        }]
        permit = episode.state.transition({
            'task_id': 'task-1', 'state': state,
            'control': 'REFINE' if state in ('PLAN', 'UNDERSTAND') else 'CONTINUE',
            'reason': 'Use the selected post-solved action',
            'evidence_ids': ['judge-task-1-r1'],
        })
        episode.saved.update(
            control_results={}, transition_selections={}, public=[], code_sources=[],
            tasks=[{'kind': 'issue', 'title': 'bug', 'body': 'hint'}],
            last_code_reply='done', closing=False,
        )
        episode.collect_user_sources = MagicMock()
        episode.guard = MagicMock()
        episode.guard.review.return_value = {
            'allowed': True, 'reasons': [], 'warnings': [],
        }
        return episode, permit

    def test_post_solved_plan_or_understand_permit_can_accept_without_followup(self):
        for state in ('PLAN', 'UNDERSTAND'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as root:
                episode, permit = self.acceptance_episode(root, state)
                packet = {
                    'request_id': 'accept-followup', 'operation': 'accept',
                    'payload': {'task_id': 'task-1', 'reason': 'done'},
                }
                result = episode._control(packet)
                repeated = episode._control(packet)
                self.assertTrue(result['accepted'])
                self.assertTrue(result['ended'])
                self.assertEqual(repeated, result)
                self.assertEqual(episode.state.data['phase'], 'ended')
                self.assertIsNone(episode.state.data['permit'])

    def test_post_solved_evaluate_permit_can_accept(self):
        with tempfile.TemporaryDirectory() as root:
            episode, _ = self.acceptance_episode(root, 'EVALUATE')
            result = episode._control({
                'request_id': 'accept-evaluate', 'operation': 'accept',
                'payload': {'task_id': 'task-1', 'reason': 'Judge passed'},
            })
            self.assertTrue(result['accepted'])
            self.assertTrue(result['ended'])
            self.assertEqual(episode.state.data['phase'], 'ended')

    def test_solved_acceptance_without_a_current_permit_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            episode, _ = self.acceptance_episode(root, 'UNDERSTAND')
            episode.state.data['permit'] = None
            result = episode._control({
                'request_id': 'accept-without-selection', 'operation': 'accept',
                'payload': {'task_id': 'task-1', 'reason': 'Judge passed'},
            })
            self.assertTrue(result['accepted'])
            self.assertTrue(result['ended'])

    def test_carry_waits_for_active_user_turn_and_no_inflight_judge(self):
        blockers = (
            ('phase', lambda episode: episode.state.data.update(phase='code')),
            ('status', lambda episode: episode.state.data.update(status='paused')),
            ('job', lambda episode: episode.progress.update(job={'id': 'inflight'})),
            ('feedback', lambda episode: episode.progress.update(
                feedback_revision={'verdict_id': 'judge-task-1-r1'}
            )),
        )
        for label, block in blockers:
            with self.subTest(blocker=label), tempfile.TemporaryDirectory() as root:
                episode = self.episode(root)
                self.publish_solved_followup(episode, root, 'UNDERSTAND')
                block(episode)
                self.assertFalse(episode.carry_post_solved_verdict())
                self.assertEqual(
                    episode.current()['post_solved_followup']['status'], 'pending'
                )
                self.assertEqual(episode.current()['verdict']['revision'], 1)

    def test_carried_verdict_does_not_hide_later_inflight_judge(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            self.publish_solved_followup(episode, root, 'UNDERSTAND')
            self.assertTrue(episode.carry_post_solved_verdict())
            episode.progress['job'] = {
                'id': 'judge-inflight', 'command_id': 'turn-judge-inflight'
            }
            episode.saved['closing'] = False
            episode.agents = {'code': MagicMock()}

            with self.assertRaisesRegex(RuntimeError, 'uncertain interrupted Judge call'):
                episode.before_user_turn()

            episode.agents['code'].pause.assert_called_once_with()
            episode.agents['code'].unpause.assert_called_once_with()

    def test_carry_rejects_unpublished_stale_reply_or_source_evidence(self):
        invalidations = (
            ('unpublished', lambda episode: episode.state.data['messages'][-1].update(
                published=False
            )),
            ('transition', lambda episode: episode.state.data['messages'][-1].update(
                transition_id='other-transition'
            )),
            ('same-code-reply', lambda episode: episode.state.data['code_reply'].update(
                id='code1'
            )),
            ('source-evidence', lambda episode: episode.current().update(
                applied_job='other-judge'
            )),
        )
        for label, invalidate in invalidations:
            with self.subTest(invalidation=label), tempfile.TemporaryDirectory() as root:
                episode = self.episode(root)
                self.publish_solved_followup(episode, root, 'RETRIEVE')
                invalidate(episode)
                self.assertFalse(episode.carry_post_solved_verdict())
                self.assertEqual(
                    episode.current()['post_solved_followup']['status'],
                    'judge_required',
                )
                self.assertEqual(episode.current()['verdict']['revision'], 1)

    def test_all_role_prices_required(self):
        config=dict(progressive_issues=True,max_cost=1,user={'model':'m'},code={'model':'m'},
                    pricing=dict(source='fixture',currency='USD',user=dict(model='m',input_per_million=1,output_per_million=1),
                                 code=dict(model='m',input_per_million=1,output_per_million=1)))
        with self.assertRaises(ValueError):Budget(config)

    def test_judge_role_not_user_tool_adapter(self):
        from simulator.openhands.tool_wording import tool_specs
        self.assertEqual([t.name for t in tool_specs('judge')], ['terminal','file_editor','task_tracker'])
        self.assertEqual([t.name for t in tool_specs('code')], ['terminal','file_editor','task_tracker'])
        self.assertNotEqual([t.name for t in tool_specs('judge')], [t.name for t in tool_specs('user')])

    def test_stale_saved_decision_cannot_release_next_issue(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            with self.assertRaises(ValueError):
                episode.apply_decision(dict(job=dict(self.job,task_id='task-2'),payload=self.payload,accepted=True))

    def test_judge_tools_expose_no_user_control_operations(self):
        from simulator.openhands.judge_tools import JUDGE_TOOLS
        definitions=[t.create(None)[0] for t in JUDGE_TOOLS]
        self.assertEqual(
            {d.name for d in definitions},
            {'submit_verdict', 'revise_verdict', 'revise_feedback'},
        )
        self.assertEqual({d.executor.operation for d in definitions},
                         {'judge_verdict', 'judge_verdict_revision',
                          'judge_feedback_revision'})

    def test_only_ssh_judge_evidence_receives_pipefail_policy(self):
        episode = ProgressiveEpisode.__new__(ProgressiveEpisode)
        episode.progress = {'job': {'event_start': 0, 'revision': 1}}
        episode.agents = {'judge': MagicMock()}
        episode.agents['judge'].events.return_value = [{
            'kind': 'ObservationEvent',
            'id': 'terminal-1',
            'tool_name': 'terminal',
            'observation': {
                'command': 'pytest -q | tail -20',
                'metadata': {'exit_code': 0},
                'content': [{'text': 'passed'}],
            },
        }]
        episode.config = {'execution_backend': 'shared_diagnostic'}
        _, observations = episode.judge_events()
        self.assertNotIn('pipeline_exit_policy', observations[0])
        episode.config = {'execution_backend': 'ssh_sandbox'}
        _, observations = episode.judge_events()
        self.assertEqual(observations[0]['pipeline_exit_policy'], 'pipefail')

    def test_pending_feedback_is_not_applied(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            record=dict(job=self.job,payload=self.payload,accepted=False,status='feedback_pending')
            episode.apply_decision(record)
            self.assertEqual(episode.current()['released'],['s1'])
            self.assertIsNone(episode.current()['verdict'])
            self.assertEqual(episode.state.data['status'],'paused')

    def test_feedback_revision_changes_only_public_feedback(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            (Path(root)/'judgments').mkdir(exist_ok=True)
            episode.saved.update(tasks=[dict(title='bug',body='hint')],public=[],control_results={})
            record=dict(job=self.job,payload=self.payload.copy(),accepted=False,status='feedback_pending',
                        events=[],observations=[{'id':'obs1','tool':'terminal','observation':{'content':[{
                            'type':'text','text':'---INPUT---\nx\n---END INPUT---\n---RESULT---\nmarkers missing\n---END RESULT---'}],
                            'command':'cd /workspace/candidate && python check.py','exit_code':0}}],disclosure={'requirement':{'title':'bug','body':'visible'}},
                        feedback_version=0,feedback_revisions=[],feedback_rejections=[{'reason':'source detail'}])
            episode.progress.update(decisions={self.job['id']:record},
                feedback_revision={'verdict_id':self.job['id'],'version':1,'reason':'source detail','event_start':0},
                control_results={})
            episode.agents={'judge':MagicMock()}
            episode.agents['judge'].events.return_value=[]
            safe=dict(allowed=True,conclusion_valid=True,feedback_safe=True,reasons=[],semantic={},source_check={})
            packet=dict(request_id='rev1',operation='judge_feedback_revision',payload=dict(
                feedback='命令输出仍然不正确',
                feedback_detail='实际输出缺少需求规定的圆括号。'))
            with patch('simulator.openhands.progressive.review_verdict',return_value=safe):
                result=episode.judge_control(packet)
            self.assertTrue(result['accepted'])
            self.assertEqual(record['status'],'ready')
            self.assertEqual(record['payload']['outcome'],'unsolved')
            self.assertEqual(record['payload']['reason'],'failure')
            self.assertEqual(record['payload']['evidence_ids'],['obs1'])
            self.assertEqual(record['payload']['public_feedback']['input'],'x')
            self.assertEqual(record['payload']['public_feedback']['output'],'markers missing')
            self.assertEqual(record['payload']['public_feedback']['summary'],
                             '实际输出缺少需求规定的圆括号。')
            self.assertEqual(record['payload']['public_feedback']['symptom'],
                             '命令输出仍然不正确')
            self.assertIsNone(episode.progress['feedback_revision'])
            self.assertEqual(episode.judge_control(packet),result)

    def test_grounded_verdict_correction_is_tool_free_idempotent_and_continues(self):
        with tempfile.TemporaryDirectory() as root:
            episode, record = self.verdict_pending_episode(root)
            observations = json.dumps(record['observations'], sort_keys=True)
            safe = dict(allowed=True, conclusion_valid=True, verdict_valid=True,
                        grounded=True, feedback_safe=True, reasons=[], semantic={},
                        source_check={})
            packet = dict(request_id='verdict-rev1',
                operation='judge_verdict_revision', payload=dict(
                    outcome='unsolved', reason='the observed result violates the issue',
                    feedback='结果仍然不正确', feedback_detail='实际结果为 y。'))
            with patch('simulator.openhands.progressive.review_verdict',
                       return_value=safe) as audit:
                result = episode.judge_control(packet)
            self.assertTrue(result['accepted'])
            self.assertEqual(record['status'], 'ready')
            self.assertEqual(record['payload']['outcome'], 'unsolved')
            self.assertEqual(json.dumps(record['observations'], sort_keys=True), observations)
            self.assertEqual(record['job']['candidate_version'],
                             candidate_hash(Path(root) / 'workspace/candidate'))
            self.assertEqual(audit.call_count, 1)
            self.assertIsNone(episode.progress['feedback_revision'])
            self.assertEqual(episode.judge_control(packet), result)
            episode.apply_decision(record)
            self.assertEqual(episode.state.data['status'], 'running')
            self.assertEqual(episode.current()['verdict']['outcome'], 'unsolved')

    def test_grounded_verdict_correction_rejects_new_inspection(self):
        with tempfile.TemporaryDirectory() as root:
            episode, record = self.verdict_pending_episode(root)
            episode.agents['judge'].events.return_value = [
                {'tool_name': 'terminal'}, {'tool_name': 'revise_verdict'}]
            result = episode.judge_control(dict(request_id='verdict-rev-tool',
                operation='judge_verdict_revision', payload=dict(
                    outcome='unsolved', reason='observed mismatch',
                    feedback='结果仍然不正确', feedback_detail='实际结果为 y。')))
            self.assertFalse(result['accepted'])
            self.assertEqual(record['status'], 'invalid')
            self.assertIn('may not run additional inspection', record['error'])
            self.assertEqual(record['verdict_revisions'], [])

    def test_grounded_verdict_correction_has_one_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            episode, record = self.verdict_pending_episode(root)
            episode.progress['feedback_revision'] = None
            record['verdict_attempts'] = 1
            episode.start_judge = MagicMock()
            episode.request_verdict_revision(record)
            self.assertEqual(record['status'], 'invalid')
            self.assertIn('after one attempt', record['error'])
            episode.agents['judge'].turn.assert_not_called()

    def test_feedback_revision_cannot_run_more_inspection(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            (Path(root)/'judgments').mkdir(exist_ok=True)
            episode.saved.update(tasks=[dict(title='bug',body='hint')],public=[],control_results={})
            record=dict(job=self.job,payload=self.payload.copy(),accepted=False,status='feedback_pending',
                        events=[],observations=[{'id':'obs1','tool':'terminal','observation':{
                            'content':[{'type':'text','text':'observed result'}],
                            'command':'cd /workspace/candidate && python check.py','exit_code':0}}],disclosure={'requirement':{'title':'bug','body':'visible'}},
                        feedback_version=0,feedback_revisions=[],feedback_rejections=[{'reason':'source detail'}])
            episode.progress.update(decisions={self.job['id']:record},
                feedback_revision={'verdict_id':self.job['id'],'version':1,'reason':'source detail','event_start':0},
                control_results={})
            episode.agents={'judge':MagicMock()}
            episode.agents['judge'].events.return_value=[{'tool_name':'terminal'}]
            packet=dict(request_id='rev1',operation='judge_feedback_revision',payload=dict(
                feedback=''))
            result=episode.judge_control(packet)
            self.assertFalse(result['accepted'])
            self.assertEqual(record['status'],'invalid')
            self.assertIn('may not run additional inspection',record['error'])
            self.assertEqual(record['feedback_revisions'],[])

    def test_feedback_revision_cannot_override_invalid_conclusion(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            episode.saved.update(tasks=[dict(title='bug',body='hint')],public=[],control_results={})
            original_payload=self.payload.copy()
            record=dict(job=self.job,payload=original_payload,accepted=False,
                        status='feedback_pending',events=[],observations=[{
                            'id':'obs1','tool':'terminal','observation':{
                                'content':[{'type':'text','text':'observed result'}],
                                'command':'python check.py','exit_code':0}}],
                        disclosure={'requirement':{'title':'bug','body':'visible'}},
                        feedback_version=0,feedback_revisions=[],
                        feedback_rejections=[{'reason':'unsafe'}])
            episode.progress.update(decisions={self.job['id']:record},
                feedback_revision={'verdict_id':self.job['id'],'version':1,
                                   'reason':'unsafe','event_start':0},
                control_results={})
            episode.agents={'judge':MagicMock()}
            episode.agents['judge'].events.return_value=[]
            review=dict(allowed=False,conclusion_valid=False,feedback_safe=True,
                        reasons=['conclusion'],semantic={},source_check={})
            packet=dict(request_id='rev1',operation='judge_feedback_revision',
                        payload={'feedback':'still fails'})
            with patch('simulator.openhands.progressive.review_verdict',return_value=review):
                result=episode.judge_control(packet)
            self.assertFalse(result['accepted'])
            self.assertEqual(record['status'],'invalid')
            self.assertIn('conclusion audit failed',record['error'])
            self.assertIs(record['payload'],original_payload)

    def test_empty_feedback_without_blocks_stays_pending_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            episode.saved.update(tasks=[dict(title='bug',body='hint')],public=[],control_results={})
            record=dict(job=self.job,payload=self.payload.copy(),accepted=False,status='feedback_pending',
                        events=[],observations=[{'id':'obs1','tool':'terminal','observation':{
                            'content':[{'type':'text','text':'exit_code=1'}],
                            'command':'cd /workspace/candidate && python check.py','exit_code':0}}],
                        disclosure={'requirement':{'title':'bug','body':'visible'}},
                        feedback_version=0,feedback_revisions=[],feedback_attempts=1,
                        feedback_rejections=[{'reason':'source detail'}])
            episode.progress.update(decisions={self.job['id']:record},
                feedback_revision={'verdict_id':self.job['id'],'version':1,
                                   'reason':'source detail','event_start':0},
                control_results={})
            episode.agents={'judge':MagicMock()}
            episode.agents['judge'].events.return_value=[]
            packet=dict(request_id='rev1',operation='judge_feedback_revision',payload={'feedback':''})
            result=episode.judge_control(packet)
            self.assertTrue(result['accepted'])
            self.assertTrue(result['handoff'])
            self.assertFalse(result['feedback_safe'])
            self.assertEqual(record['status'],'feedback_pending')
            self.assertEqual(record['feedback_attempts'],1)
            self.assertEqual(record['feedback_revisions'],[{
                'version':1,
                'payload':{'feedback':''},
                'rejected_reason':'source detail',
                'schema_error':'unsolved requires observable public feedback',
            }])
            self.assertIsNone(episode.progress['feedback_revision'])

    def test_missing_or_non_string_feedback_stays_correctable(self):
        for index, payload in enumerate(({}, {'feedback':None}, {'feedback':7})):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as root:
                episode=self.episode(root)
                episode.saved.update(tasks=[dict(title='bug',body='hint')],public=[],control_results={})
                record=dict(job=self.job,payload=self.payload.copy(),accepted=False,
                            status='feedback_pending',events=[],observations=[],
                            disclosure={'requirement':{'title':'bug','body':'visible'}},
                            feedback_version=0,feedback_revisions=[],feedback_attempts=1,
                            feedback_rejections=[{'reason':'unsafe'}])
                episode.progress.update(decisions={self.job['id']:record},
                    feedback_revision={'verdict_id':self.job['id'],'version':1,
                                       'reason':'unsafe','event_start':0},
                    control_results={})
                episode.agents={'judge':MagicMock()}
                episode.agents['judge'].events.return_value=[]
                result=episode.judge_control(dict(request_id='bad-'+str(index),
                    operation='judge_feedback_revision',payload=payload))
                self.assertTrue(result['accepted'])
                self.assertEqual(record['status'],'feedback_pending')
                self.assertEqual(record['feedback_revisions'][0]['payload'],payload)
                self.assertIn('string feedback field',
                              record['feedback_revisions'][0]['schema_error'])

    def test_feedback_revision_prompt_reports_closed_block_availability(self):
        cases = (
            ('plain output', False),
            ('---INPUT---\nx\n---END INPUT---\n---RESULT---\ny\n---END RESULT---', True),
        )
        for index, (output, expected) in enumerate(cases):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as root:
                episode=self.episode(root)
                record=dict(job=self.job,payload=self.payload.copy(),accepted=False,
                            status='feedback_pending',feedback_version=0,
                            feedback_revisions=[],feedback_rejections=[{'reason':'unsafe'}],
                            observations=[{'id':'obs1','tool':'terminal','observation':{
                                'content':[{'type':'text','text':output}],
                                'command':'python check.py','exit_code':0}}])
                episode.progress.update(decisions={self.job['id']:record},
                                        feedback_revision=None,control_results={})
                episode.start_judge=MagicMock()
                episode.agents={'judge':MagicMock()}
                prompts=[]
                def stop_after_prompt(message, **kwargs):
                    prompts.append(json.loads(message))
                    episode.progress['feedback_revision']=None
                episode.agents['judge'].turn.side_effect=stop_after_prompt
                episode.request_feedback_revision(record)
                self.assertEqual(
                    prompts[0]['closed_execution_blocks_available'], expected)
                self.assertNotIn('plain output',json.dumps(prompts[0]))
                self.assertIn('具体可观察差异', prompts[0]['instruction'])
                self.assertNotIn('feedback 留空', prompts[0]['instruction'])

    def test_feedback_revision_attempt_limit_counts_wrong_tool_calls(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            (Path(root)/'judgments').mkdir(exist_ok=True)
            record=dict(job=self.job,payload=self.payload.copy(),accepted=False,status='feedback_pending',
                        feedback_version=0,feedback_revisions=[],feedback_rejections=[{'reason':'unsafe'}])
            episode.progress.update(decisions={self.job['id']:record},feedback_revision=None,control_results={})
            episode.start_judge=MagicMock()
            episode.agents={'judge':MagicMock()}
            episode.agents['judge'].events.return_value=[]
            calls=[]
            def wrong_turn(*args,**kwargs):
                calls.append(kwargs['command_id'])
                episode.judge_control(dict(request_id='wrong-'+str(len(calls)),operation='judge_evidence',payload={}))
            episode.agents['judge'].turn.side_effect=wrong_turn
            episode.request_feedback_revision(record)
            episode.request_feedback_revision(record)
            episode.request_feedback_revision(record)
            self.assertEqual(len(calls),1)
            self.assertEqual(record['feedback_attempts'],1)
            self.assertEqual(record['status'],'verdict_pending')
            self.assertIn('no candidate-only', record['verdict_rejections'][0]['reason'])

    def test_no_reference_snapshot_mount_for_code(self):
        from simulator.openhands.container import SDKContainer
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            workspace=root/'workspace';workspace.mkdir()
            with patch('simulator.openhands.container.subprocess.run') as run, \
                 patch('simulator.openhands.container.subprocess.Popen'), \
                 patch.object(SDKContainer, 'wait'), patch('simulator.openhands.container.Relay'), \
                 patch('simulator.openhands.container.inspect_container',
                       return_value={'Id': 'fixture-id', 'State': {'Paused': False}}):
                run.return_value.returncode=1
                code=SDKContainer(root/'code',workspace,{'model':'m','execution_backend':'shared_diagnostic',
                    'execution_image':'sha256:' + '0' * 64},'image','code','system',99999999)
                code.start()
                command=run.call_args_list[1].args[0]
                self.assertNotIn('/reference',' '.join(command))
                code.log.close()
                reference=root/'ref';reference.mkdir()
                judge=SDKContainer(root/'judge',workspace,{'model':'m','execution_backend':'shared_diagnostic',
                    'execution_image':'sha256:' + '0' * 64},'image','judge','system',99999999,
                                   reference=reference,readonly_candidate=True)
                run.reset_mock();judge.start()
                command=run.call_args_list[1].args[0]
                self.assertTrue(any('dst=/reference,readonly' in arg for arg in command))
                self.assertTrue(any('dst=/workspace/candidate,readonly' in arg for arg in command))
                judge.log.close()

    def test_report_escapes_private_html(self):
        with tempfile.TemporaryDirectory() as root:
            saved=dict(state={'status':'paused','transitions':[]}, public=[dict(kind='user',id='u1',text='<script>bad()</script>')])
            render(root,saved)
            page=(Path(root)/'index.html').read_text()
            self.assertNotIn('<script>bad()',page)
            self.assertIn('&lt;script&gt;',page)

    def test_user_input_has_only_released_information_and_safe_feedback(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            episode.saved['tasks']=[dict(title='FULL ISSUE PRIVATE',patch='REFERENCE PATCH')]
            serialized=json.dumps(episode.user_input())
            for private in ('hidden','FULL ISSUE PRIVATE','REFERENCE PATCH','related','source_quote','private body'):
                self.assertNotIn(private,serialized)

    def test_saved_release_set_survives_serialization_and_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as root:
            episode=self.episode(root)
            target, disclosure = plan_turn_disclosure(episode.current(), self.payload)
            record=dict(job=self.job,payload=self.payload,accepted=True,
                        disclosure={'after': target}, feedback_disclosure=disclosure)
            episode.apply_decision(record)
            restored=json.loads(json.dumps(episode.progress))
            episode.progress=restored
            episode.apply_decision(record)
            self.assertEqual(episode.current()['released'],['s1'])
            self.assertEqual(len(episode.current()['released_feedback_unit_ids']), 1)
            self.assertEqual(len(episode.current()['release_history']),1)

    def test_judge_feedback_releases_one_current_unit_and_resets_new_failure(self):
        from simulator.openhands.user_projection import task_feedback

        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            base = Path(root)
            (base / 'workspace/candidate').mkdir(parents=True)
            observation = {
                'kind': 'wrong_output', 'evidence_id': 'private-test-name',
                'input': 'prefix@example.com', 'output': 'original',
                'symptom': '自定义默认值查找仍未生效',
                'summary': '传入 prefix@example.com 后实际仍是 original',
            }
            record = dict(
                job=dict(self.job, revision=1),
                payload=dict(self.payload, revision=1, public_feedback=observation),
                accepted=True,
            )
            target, disclosure = plan_turn_disclosure(
                episode.current(), record['payload'])
            record.update(disclosure={'after': target},
                          feedback_disclosure=disclosure)
            episode.apply_decision(record)
            current = episode.current()
            self.assertEqual(len(current['released_feedback_unit_ids']), 1)
            self.assertEqual(task_feedback(current)['observation']['kind'],
                             'logic_error')
            self.assertNotIn('private-test-name', json.dumps(task_feedback(current)))
            self.assertNotIn('root cause', json.dumps(task_feedback(current)))

            episode.saved['revision'] = 2
            repeated = dict(record, job=dict(self.job, id='judge-task-1-r2', revision=2),
                            payload=dict(record['payload'], evidence_ids=['obs2']))
            target, disclosure = plan_turn_disclosure(
                episode.current(), repeated['payload'])
            repeated.update(disclosure={'after': target},
                            feedback_disclosure=disclosure)
            episode.apply_decision(repeated)
            self.assertEqual(len(current['released_feedback_unit_ids']), 2)
            visible = task_feedback(current)['observation']
            self.assertEqual(visible['kind'], 'wrong_output')
            self.assertEqual(visible['input'], 'prefix@example.com')
            self.assertEqual(visible['output'], 'original')

            # A repeated failure with no newly releasable unit must retain the
            # latest visible observation instead of publishing an empty one.
            episode.saved['revision'] = 2
            repeated_again = dict(repeated, job=dict(self.job, id='judge-task-1-r2b', revision=2))
            target, disclosure = plan_turn_disclosure(
                episode.current(), repeated_again['payload'])
            repeated_again.update(disclosure={'after': target},
                                  feedback_disclosure=disclosure)
            episode.apply_decision(repeated_again)
            self.assertEqual(task_feedback(current)['observation']['output'], 'original')

            episode.saved['revision'] = 3
            changed = dict(record, job=dict(self.job, id='judge-task-1-r3', revision=3),
                           payload=dict(record['payload'], evidence_ids=['obs3'],
                                        public_feedback=dict(
                                            observation,
                                            output='changed',
                                            symptom='另一个输出问题')))
            target, disclosure = plan_turn_disclosure(
                episode.current(), changed['payload'])
            changed.update(disclosure={'after': target},
                           feedback_disclosure=disclosure)
            episode.apply_decision(changed)
            self.assertEqual(len(current['released_feedback_unit_ids']), 1)
            self.assertEqual(task_feedback(current)['observation']['kind'], 'logic_error')

            episode.saved['revision'] = 4
            solved = dict(record, job=dict(self.job, id='judge-task-1-r4', revision=4),
                          payload=dict(record['payload'], outcome='solved',
                                       evidence_ids=['obs4'], public_feedback={}))
            episode.apply_decision(solved)
            self.assertEqual(current['released_feedback_unit_ids'], [])
            self.assertEqual(task_feedback(current), {'status': 'solved'})
