"""Allowlisted User views; private Judge evidence remains host authority."""
import copy
from .relay import append
from ..state_machine import STATE_GUIDANCE


def initial_without_checks(state, current):
    data = state.data
    return (data.get('code_reply') is None and not data.get('checks')
            and not data.get('blockers') and not current.get('feedback')
            and not current.get('verdict')
            and not any(a['task_id'] == data['task_id'] for a in data.get('accepted', [])))


class UserViewMixin:
    """Shared boundary for real progressive turns and first-draft diagnostics."""
    def projection_context(self):
        return {}

    def augment_user_input(self, value):
        return value

    def user_input(self):
        value = super().user_input()
        current = self.projection_context()
        if not initial_without_checks(self.state, current):
            value['task_result'] = task_feedback(current)
            if value.get('communication', {}).get('stage') != 'initial_delegation':
                value['instruction'] = followup_instruction(value['task_result'])
        value = self.augment_user_input(value)
        append(self.private/'user-inputs.jsonl', value)
        return value

    def control(self, packet):
        with self.lock:
            result = super().control(packet)
            public = control_result(packet.get('operation'), result, self.state, self.projection_context())
            append(self.private/'user-tool-results.jsonl', dict(request_id=packet['request_id'],
                operation=packet.get('operation'), result=public))
            return public


def task_feedback(current):
    feedback = current.get('feedback') or {}
    outcome = feedback.get('outcome')
    if outcome == 'solved':
        result = dict(status='solved')
        if (current.get('verdict') or {}).get('verification_mode') == 'static_reference':
            result['verification'] = 'static'
        return result
    result = dict(status='pending')
    if outcome in ('unsolved', 'uncertain'):
        # Only the current host-selected observation is public.  Keep an
        # explicit field allowlist so older checkpoints containing the full
        # Judge projection cannot bypass the unit boundary.
        source = feedback.get('observation') or {}
        observation = {
            key: copy.deepcopy(source[key])
            for key in ('kind', 'input', 'output', 'error', 'summary')
            if key in source
        }
        observation['raw_result_available'] = observation.get('kind') in (
            'runtime_error', 'wrong_output')
        result.update(status=outcome, observation=observation)
    return result


def followup_instruction(result):
    observation = result.get('observation', {})
    if observation.get('raw_result_available') is True:
        return ('自然回应 Code 的上一条消息。需要附上当前实际输入和结果时，在正文相应位置写 '
                '[[运行结果]]，由宿主替换为原文。')
    if observation.get('kind') == 'logic_error':
        return ('自然回应 Code 的上一条消息。直接用用户口吻转述 task_result 中的现象；'
                '这次没有可粘贴的原始运行结果，不要使用 [[运行结果]]。')
    return '自然回应 Code 的上一条消息。'


def active_request(state):
    """Expose only the current send permit and its host-owned intent."""
    permit = state.data.get('permit')
    if not permit or state.data['phase'] != 'user' or state.data['status'] != 'running':
        return None
    return dict(permit_id=permit['id'], state=permit['state'],
                control=permit['control'], reason=STATE_GUIDANCE[permit['state']])


def state_view(state, current):
    raw = state.view()
    result = {k: copy.deepcopy(raw[k]) for k in (
        'task_id', 'state', 'control', 'phase', 'status', 'communication',
        'blockers', 'unresolved_failures', 'published_messages') if k in raw}
    # User's own observations remain available to support genuine feedback and
    # resolve failed operations. Judge and Code histories are not duplicated here.
    result['checks'] = [copy.deepcopy(c) for c in raw.get('checks', [])
                        if c.get('tool') not in ('judge_summary', 'code_report')]
    communication = result.get('communication')
    if communication:
        result['communication'] = {key: copy.deepcopy(communication[key])
                                   for key in ('stage', 'last_code_reply')}
    result['task_result'] = task_feedback(current)
    request = active_request(state)
    if request:
        result['active_request'] = request
    if initial_without_checks(state, current):
        for key in ('checks', 'task_result', 'blockers', 'unresolved_failures'):
            result.pop(key, None)
    return result


def control_result(operation, result, state, current):
    if operation == 'authorize_tools':
        if result.get('accepted'):
            return dict(accepted=True, reasons=[])
        return dict(accepted=False, reasons=[
            '本次操作未执行。这是内部控制结果，不是产品使用故障，不转述给对话另一方。可根据已有观察回应，或直接委托 Code 检查。'])
    public = {k: copy.deepcopy(result[k]) for k in (
        'accepted', 'handoff', 'paused', 'ended', 'retained_private', 'permit_id', 'next_requirement',
        'current_requirement') if k in result}
    if operation == 'accept' and result.get('next_requirement') and result.get('instruction'):
        public['instruction'] = result['instruction']
    # A transition returns the permit directly, rather than under permit_id.
    if operation == 'transition' and result.get('accepted'):
        for key in ('id', 'task_id', 'state', 'control', 'reason'):
            if key in result:
                public[key] = result[key]
    if 'state' in result and isinstance(result['state'], dict):
        public['state'] = state_view(state, current)
    if operation in ('read_state', 'verify', 'accept') and not initial_without_checks(state, current):
        public['task_result'] = task_feedback(current)
    if not result.get('accepted'):
        reasons = result.get('reasons', [])
        feedback = task_feedback(current)
        observation = feedback.get('observation', {})
        missing_raw = any(isinstance(reason, str) and
                          reason.startswith('Public feedback must preserve the exact ')
                          for reason in reasons)
        invalid_attachment = result.get('reason') == 'only raw runtime error or wrong output can be attached'
        state_mismatch = 'Request type/control does not match the proposed action.' in reasons
        request = active_request(state)
        if operation == 'send' and state_mismatch and request:
            public['active_request'] = request
            public['reason'] = ('正文与本轮已选请求不一致。按 active_request 的意图修改正文，'
                                '使用同一 permit_id 再发送；不要重新申请状态。')
        elif operation == 'send' and observation.get('raw_result_available') is False and invalid_attachment:
            public['reason'] = ('这次没有可粘贴的原始运行结果。直接用用户口吻转述 '
                                'task_result.observation.summary，不要使用 [[运行结果]]。')
        else:
            public['reason'] = result.get('reason') or (
                '发送内容引用了运行结果。请在正文相应位置写 [[运行结果]]，由宿主附上当前已审核原文。'
                if missing_raw and observation.get('raw_result_available') is True else
                '当前没有可用的发送许可；先调用 session_state 确认当前任务。'
                if state_mismatch else
                'Action rejected; check current state, intent and supported facts.')
        public['unresolved'] = state.pending_checks()
        public['blockers'] = copy.deepcopy(state.data['blockers'])
        if initial_without_checks(state, current):
            for key in ('unresolved', 'blockers'):
                if not public[key]:
                    public.pop(key)
    return public
