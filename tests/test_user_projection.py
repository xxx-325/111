import json
import unittest
from unittest.mock import MagicMock, patch
from simulator.openhands.user_projection import (
    control_result,
    followup_instruction,
    state_view,
    task_feedback,
)
from simulator.openhands.state import TaskState
from simulator.openhands.policy import role_prompts
from simulator.openhands.control_tools import HostExecutor, HostObservation, SendAction


class UserProjectionTests(unittest.TestCase):
    def setUp(self):
        self.state = TaskState()
        self.current = dict(feedback=dict(outcome='solved', observation={'input':'2012-12-25 PRIVATE'}, evidence_id='judge-private'))
        self.state.data['checks'] = [dict(id='judge-private',tool='judge_summary',result='passed',summary='PRIVATE'),
                                    dict(id='own',tool='terminal',result='failed',observation='real error')]
        self.state.data['accepted'] = [dict(task_id='task-1',reason='PRIVATE',evidence_ids=['judge-private'])]

    def test_all_views_exclude_solved_report(self):
        self.assertEqual(task_feedback(self.current),{'status':'solved'})
        result=dict(accepted=True,state=self.state.view(),decision={'reason':'PRIVATE'},
                    external_check=self.current['feedback'],reasons=['PRIVATE'])
        for operation in ('read_state','verify','accept','transition','send','pause'):
            projected=control_result(operation,result,self.state,self.current)
            self.assertNotIn('PRIVATE',json.dumps(projected))
            self.assertNotIn('2012-12-25',json.dumps(projected))
        self.assertIn('real error',json.dumps(state_view(self.state,self.current)))

    def test_static_solved_feedback_identifies_verification_basis(self):
        current = dict(feedback=dict(outcome='solved'),
                       verdict=dict(verification_mode='static_reference'))
        self.assertEqual(task_feedback(current),
                         {'status': 'solved', 'verification': 'static'})

    def test_accept_projection_keeps_only_next_task_instruction(self):
        result = control_result('accept', {
            'accepted': True,
            'next_requirement': {'title': 'Next issue'},
            'instruction': 'Handle the next released requirement.',
            'private_plan': 'do not expose',
        }, self.state, self.current)
        self.assertEqual(result['instruction'], 'Handle the next released requirement.')
        self.assertNotIn('private_plan', result)

    def test_unsolved_feedback_preserved_and_no_acceptance(self):
        for outcome in ('unsolved','uncertain'):
            result=task_feedback(dict(feedback=dict(outcome=outcome,observation={
                'kind':'logic_error','evidence_id':'e1','summary':'still skips years'})))
            self.assertEqual(result['status'],outcome)
            self.assertEqual(result['observation']['summary'],'still skips years')
            self.assertFalse(result['observation']['raw_result_available'])
            self.assertNotIn('evidence_id',result['observation'])

    def test_feedback_declares_raw_result_availability(self):
        for kind, available in (
            ('runtime_error', True), ('wrong_output', True), ('logic_error', False)
        ):
            result = task_feedback(dict(feedback=dict(
                outcome='unsolved', observation={'kind': kind, 'summary': 'visible'})))
            self.assertIs(result['observation']['raw_result_available'], available)
            instruction = followup_instruction(result)
            if available:
                self.assertIn('[[运行结果]]', instruction)
            else:
                self.assertIn('直接用用户口吻转述', instruction)
                self.assertIn('不要使用 [[运行结果]]', instruction)

    def test_repeat_projection_no_side_effects_or_rewriting(self):
        result={'accepted':True,'permit_id':'permit'}
        self.assertEqual(control_result('transition',result,self.state,self.current),result)
        self.assertEqual(control_result('transition',result,self.state,self.current),result)
        self.assertIn('PRIVATE',json.dumps(self.current))

    def test_transition_warning_stays_private(self):
        warning='Request type/control classification may not match the proposed action.'
        result={'accepted':True,'permit_id':'permit','classification_warning':warning}
        projected=control_result('transition',result,self.state,self.current)
        observation=HostObservation(result=projected)
        visible=json.loads(observation.to_llm_content[0].text)
        self.assertNotIn('classification_warning', visible)
        self.assertNotIn('reasons',visible)

        ordinary=control_result('transition',{'accepted':True,'permit_id':'next'},
                                self.state,self.current)
        self.assertNotIn('classification_warning',ordinary)

    def test_send_handoff_returns_warning_observation_with_pause(self):
        warning='Request type/control classification may not match the proposed action.'
        result={'accepted':True,'handoff':True,'message_id':'m1',
                'classification_warning':warning}
        conversation=MagicMock()
        action=SendAction(task_id='task-1',permit_id='permit',text='message')
        with patch('simulator.openhands.control_tools.request_control',return_value=result):
            observation=HostExecutor('send')(action,conversation)
        conversation.pause.assert_called_once_with()
        self.assertEqual(observation.result['classification_warning'],warning)

    def test_code_prompt_unchanged(self):
        expected='你是代码助手。按用户需求检查、修改并验证 /workspace/candidate 中的代码；环境离线，一轮结束如实说明结果或待回答的问题。\n公开对话使用简体中文，代码、标识符和原始报错保持原文。'
        self.assertEqual(role_prompts('zh-CN')['code'],expected)

    def test_real_and_diagnostic_share_input_and_tool_boundary(self):
        from simulator.openhands.progressive import ProgressiveEpisode
        from simulator.openhands.role_diagnostic import FirstDelegation
        from simulator.openhands.user_projection import UserViewMixin
        self.assertIs(FirstDelegation.user_input,UserViewMixin.user_input)
        self.assertIs(ProgressiveEpisode.user_input,UserViewMixin.user_input)
        self.assertIs(FirstDelegation.control,ProgressiveEpisode.control)
        with self.assertRaises(RuntimeError):
            FirstDelegation.__new__(FirstDelegation).run()

    def test_minimal_diagnostic_initial_input_and_state(self):
        import tempfile
        from pathlib import Path
        from simulator.openhands.role_diagnostic import FirstDelegation
        with tempfile.TemporaryDirectory() as root:
            episode=FirstDelegation.__new__(FirstDelegation)
            episode.private=Path(root)
            episode.state=TaskState()
            requirement={'title':'当前需求','body':'日期生成结果有问题。'}
            episode.saved={'tasks':[dict(kind='issue',**requirement)]}
            value=episode.user_input()
            self.assertEqual(value['current_requirement'],requirement)
            self.assertNotIn('task_result',value)
            result=control_result('read_state',dict(accepted=True,state=episode.state.view(),current_requirement=requirement),episode.state,{})
            self.assertNotIn('task_result',result)
            self.assertTrue(result['accepted'])  # This is a tool receipt, not task acceptance.
            for field in ('task_result','checks','accepted','unresolved_failures','blockers'):
                self.assertNotIn(field,result['state'])
            for forbidden in ('boltons','daterange','12 月','step=', '/workspace', '2020'):
                self.assertNotIn(forbidden,json.dumps([value,result],ensure_ascii=False))

    def test_actual_failure_or_blocker_is_not_hidden_in_first_phase(self):
        for change in ({'checks':[dict(id='failed',tool='terminal',result='failed',observation='real failure')]},
                       {'blockers':[dict(id='blocked',reason='real blocker')]}):
            state=TaskState()
            state.data.update(change)
            result=state_view(state,{})
            self.assertIn('task_result',result)
            self.assertIn('real ',json.dumps(result))

    def test_followup_retains_pending_acceptance_information(self):
        state=TaskState()
        state.data['code_reply']={'id':'code','text':'I need details','stopped':True}
        result=state_view(state,{})
        self.assertEqual(result['task_result'],{'status':'pending'})
        self.assertEqual(result['published_messages'], 0)

        from simulator.openhands.episode import OpenHandsEpisode
        episode=OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.state=state
        episode.saved={'tasks':[{'kind':'issue','title':'Bug','body':'Description'}]}
        self.assertEqual(episode.user_input()['instruction'],
                         '自然回应 Code 的上一条消息。若要引用 task_result 的运行结果，在正文相应位置写 '
                         '[[运行结果]]，由宿主替换为原文。')

    def test_raw_preservation_rejection_projects_safe_placeholder_hint(self):
        current = dict(feedback=dict(outcome='unsolved', observation={
            'kind': 'wrong_output', 'input': 'x', 'output': 'y'}))
        result = {'accepted': False, 'allowed': False, 'reasons': [
            'private semantic detail',
            'Public feedback must preserve the exact output from current execution evidence.',
        ]}
        projected = control_result('send', result, self.state, current)
        self.assertIn('[[运行结果]]', projected['reason'])
        self.assertNotIn('private semantic detail', json.dumps(projected))

    def test_logic_error_attachment_rejection_projects_summary_hint(self):
        current = dict(feedback=dict(outcome='unsolved', observation={
            'kind': 'logic_error', 'summary': 'override is ignored'}))
        result = {'accepted': False,
                  'reason': 'only raw runtime error or wrong output can be attached'}
        projected = control_result('send', result, self.state, current)
        self.assertIn('直接用用户口吻转述', projected['reason'])
        self.assertIn('不要使用 [[运行结果]]', projected['reason'])
        self.assertNotIn('only raw runtime', projected['reason'])

    def test_other_private_review_reasons_remain_generic(self):
        result = {'accepted': False, 'allowed': False,
                  'reasons': ['private semantic detail']}
        projected = control_result('send', result, self.state, self.current)
        self.assertEqual(projected['reason'],
                         'Action rejected; check current state, intent and supported facts.')
        self.assertNotIn('private semantic detail', json.dumps(projected))

    def test_rejection_does_not_expose_classification_or_private_reasons(self):
        result={'accepted':False,'allowed':False,
                'warnings':['safe internal warning'],
                'reasons':['private semantic detail']}
        projected=control_result('transition',result,self.state,self.current)
        self.assertFalse(projected['accepted'])
        self.assertNotIn('classification_warning',projected)
        self.assertNotIn('private semantic detail',json.dumps(projected))

    def test_state_mismatch_projects_safe_actionable_reason(self):
        result = {'accepted': False, 'allowed': False, 'reasons': [
            'private semantic detail',
            'Request type/control does not match the proposed action.',
        ]}
        permit = self.state.transition(dict(task_id='task-1', state='OPERATE',
            control='CONTINUE', reason='Run the candidate'))
        projected = control_result('send', result, self.state, self.current)
        self.assertEqual(projected['active_request']['permit_id'], permit['id'])
        self.assertEqual(projected['active_request']['state'], 'OPERATE')
        self.assertIn('不要重新申请', projected['reason'])
        self.assertIn('修改正文', projected['reason'])
        self.assertNotIn('private semantic detail', json.dumps(projected))

    def test_active_request_survives_state_read_but_not_publication(self):
        state = TaskState()
        self.assertNotIn('active_request', state_view(state, {}))
        permit = state.transition(dict(task_id='task-1', state='OPERATE',
            control='CONTINUE', reason='PRIVATE proposal annotation', evidence_ids=[]))
        active = state_view(state, {})['active_request']
        self.assertEqual(active['permit_id'], permit['id'])
        self.assertEqual(active['control'], 'CONTINUE')
        self.assertNotIn('PRIVATE', json.dumps(active))
        self.assertNotIn('evidence_ids', active)
        state.send(dict(task_id='task-1', permit_id=permit['id'], text='Run it'))
        self.assertNotIn('active_request', state_view(state, {}))
