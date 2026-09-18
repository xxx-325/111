"""Explicit SDK event projection. User private tools never enter public sessions."""
import re


def public_event(event):
    kind = event.get('kind')
    if event.get('tool_name') == 'think':
        return None
    if kind == 'ActionEvent':
        if event.get('tool_name') == 'finish':
            return dict(kind='assistant', role='assistant', phase='final',
                        text=(event.get('action') or {}).get('message', ''))
        return dict(kind='tool_call', role='assistant', tool_name=event.get('tool_name'),
                    call_id=event.get('tool_call_id'), action=event.get('action'))
    if kind == 'ObservationEvent':
        if event.get('tool_name') == 'finish':
            return None
        return dict(kind='tool_result', role='tool', tool_name=event.get('tool_name'),
                    call_id=event.get('tool_call_id'), observation=event.get('observation'))
    if event.get('tool_call_id') and event.get('tool_name') and kind in ('AgentErrorEvent', 'UserRejectObservation'):
        return dict(kind='tool_result', role='tool', tool_name=event['tool_name'],
                    call_id=event['tool_call_id'], observation={k: event[k] for k in (
                        'error', 'rejection_reason', 'classification', 'kind') if k in event})
    if kind == 'MessageEvent' and event.get('source') == 'agent':
        content = event.get('llm_message', {}).get('content', [])
        return dict(kind='assistant', role='assistant', phase='final',
                    text='\n'.join(c['text'] for c in content if c.get('type') == 'text'))
    return None


def latest_user_final(events):
    """Return the last non-empty ordinary User-agent final from one SDK turn."""
    for event in reversed(events or ()):
        item = public_event(event)
        if (item and event.get('kind') == 'MessageEvent'
                and item.get('phase') == 'final' and item.get('text', '').strip()):
            return {'event_id': event['id'], 'text': item['text']}
    return None


def private_observation(event, revision, *, pipeline_exit_policy=None):
    control_names = ('session_control', 'session_state', 'request_transition', 'send_reply', 'accept_task', 'pause_task', 'verify_task', 'finish', 'think')
    if event.get('tool_name') in control_names:
        return None
    if event.get('kind') == 'AgentErrorEvent' and event.get('tool_name'):
        return dict(id=event['id'], revision=revision, tool=event['tool_name'], result='failed',
                    observation={'error': event.get('error'), 'classification': event.get('classification')}, exit_code=None)
    if event.get('kind') != 'ObservationEvent':
        return None
    observation = event.get('observation', {})
    metadata = observation.get('metadata', {}) or {}
    exit_code = observation.get('exit_code')
    if exit_code is None:
        exit_code = metadata.get('exit_code')
    # An exit code describes a command result, not necessarily an unmet requirement.
    result = 'failed' if observation.get('is_error') or (isinstance(exit_code, int) and exit_code not in (0, -1)) else 'observed'
    if event.get('tool_name') == 'terminal':
        output = '\n'.join(c.get('text', '') for c in observation.get('content', []))
        # Shell pipelines can mask pytest's exit status with tail/head's zero.
        if re.search(r'^=+.*\b[1-9]\d* failed\b.*=+$', output, re.M):
            result = 'failed'
        elif exit_code == 0 and (re.search(r'^=+.*\b[1-9]\d* passed\b.*=+$', output, re.M) or
                                 re.search(r'^\d+ passed and 0 failed\.$', output, re.M)):
            result = 'passed'
    projected = dict(id=event['id'], revision=revision, tool=event.get('tool_name'), result=result,
                     observation=observation, exit_code=exit_code)
    if pipeline_exit_policy and event.get('tool_name') == 'terminal':
        projected['pipeline_exit_policy'] = pipeline_exit_policy
    return projected
