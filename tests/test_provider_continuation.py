"""Replay only an unforwarded provider response, preserving earlier tool work."""
import json
from unittest.mock import patch

import pytest

from simulator.episode import save
from simulator.openhands.progressive import ProgressiveEpisode
from simulator.openhands.provider_503_continuation import (
    _verify_source, clone_provider_503_continuation,
)


@pytest.fixture
def retained_run(tmp_path):
    root = tmp_path / 'source'
    for directory in ('private/code/outbox', 'private/code/inbox',
                      'private/code/sdk/conversation/events', 'workspace/candidate'):
        (root / directory).mkdir(parents=True)
    save(root / 'private/checkpoint.json', dict(
        schema=ProgressiveEpisode.checkpoint_schema,
        state=dict(status='paused', phase='code', task_id='task-10',
                   accepted=['task-1'], pause_reason='RuntimeError: SDK turn failed'),
        in_flight=dict(role='code', id='turn-failed', public_start=9),
        budget=dict(pending={'old-503': 0}, completion_tokens=8192),
        elapsed_seconds=3450, config={}, policy={}, public=[{'text': 'Original'}],
        offsets={'code': 10},
    ))
    save(root / 'private/code/outbox/active.json',
         dict(status='stopped', command_id='turn-failed'))
    save(root / 'private/code/outbox/turn-failed.json', dict(
        status='error', error_type='ConversationRunError', traceback='request_id: cut'))
    save(root / 'private/code/inbox/config.json', dict(conversation_id='same'))
    events = root / 'private/code/sdk/conversation/events'
    save(events / 'event-00001.json', dict(kind='ActionEvent', tool_call_id='done', action={'command': 'touch file'}))
    save(events / 'event-00002.json', dict(kind='ObservationEvent', tool_call_id='done', observation={'exit_code': 0}))
    save(events / 'event-00003.json', dict(kind='ConversationErrorEvent'))
    (root / 'workspace/candidate/file').write_text('already completed')
    write_journal(root, [
        dict(kind='provider_failure', id='old-503', status=503),
        dict(kind='request', id='cut'),
        dict(kind='response', id='cut', output=dict(choices=[dict(finish_reason='length')])),
        dict(kind='provider_failure', id='cut', error_code='PROVIDER_OUTPUT_TRUNCATED',
             stage='response_validation', error_type='RelayOutputLimitError'),
    ])
    return root


def write_journal(root, records):
    (root / 'private/code/provider.jsonl').write_text(
        ''.join(json.dumps(record) + '\n' for record in records))


def test_truncation_with_historical_503_is_replayable(retained_run):
    checkpoint, _, _, failure = _verify_source(retained_run)
    assert failure['id'] == 'cut'
    assert checkpoint['budget']['pending'] == {'old-503': 0}


def test_503_remains_supported(retained_run):
    write_journal(retained_run, [dict(kind='request', id='old-503'),
                               dict(kind='provider_failure', id='old-503', status=503)])
    save(retained_run / 'private/code/outbox/turn-failed.json', dict(
        status='error', error_type='ConversationRunError', traceback='old-503'))
    assert _verify_source(retained_run)[-1]['status'] == 503


@pytest.mark.parametrize('fault', ['wrong_stage', 'wrong_request', 'later_request',
                                  'later_action', 'unpaired', 'fatal', 'pending', 'running'])
def test_uncertain_or_unrelated_execution_is_rejected(retained_run, fault):
    root = retained_run
    journal = [json.loads(line) for line in (root / 'private/code/provider.jsonl').read_text().splitlines()]
    if fault == 'wrong_stage':
        journal[-1]['stage'] = 'response_audit'
    elif fault == 'wrong_request':
        save(root / 'private/code/outbox/turn-failed.json', dict(
            status='error', error_type='ConversationRunError', traceback='another request'))
    elif fault == 'later_request':
        journal.append(dict(kind='request', id='later'))
    elif fault == 'later_action':
        save(root / 'private/code/sdk/conversation/events/event-00004.json', dict(kind='ActionEvent'))
    elif fault == 'unpaired':
        (root / 'private/code/sdk/conversation/events/event-00002.json').unlink()
    elif fault == 'fatal':
        (root / 'private/code/sdk/remote-tools').mkdir()
        save(root / 'private/code/sdk/remote-tools/fatal.json', {})
    elif fault == 'pending':
        path = root / 'private/checkpoint.json'
        saved = json.loads(path.read_text())
        saved['budget']['pending']['cut'] = 0
        save(path, saved)
    else:
        save(root / 'private/code/outbox/active.json', dict(status='running', command_id='turn-failed'))
    write_journal(root, journal)
    with pytest.raises(ValueError):
        _verify_source(root)


def test_clone_preserves_history_budget_and_sends_no_message(retained_run, tmp_path):
    original = (retained_run / 'private/checkpoint.json').read_bytes()
    target = tmp_path / 'continued'
    with patch('simulator.openhands.provider_503_continuation._resolve_policy',
               return_value=({}, {}, {})), patch(
                   'simulator.openhands.provider_503_continuation._clear_stale_runtime', return_value=[]):
        clone_provider_503_continuation(retained_run, target)
    saved = json.loads((target / 'private/checkpoint.json').read_text())
    before = json.loads(original)
    for key in ('budget', 'elapsed_seconds', 'public', 'offsets'):
        assert saved[key] == before[key]
    assert saved['in_flight'] == dict(role='code', id='turn-failed-rtruncated', public_start=9)
    command = json.loads((target / 'private/code/inbox/turn-failed-rtruncated.json').read_text())
    assert command['message'] is None
    assert (retained_run / 'private/checkpoint.json').read_bytes() == original
    assert (target / 'private/code/outbox/turn-failed.json').read_bytes() == (
        retained_run / 'private/code/outbox/turn-failed.json').read_bytes()
    assert saved['provider_503_continuation']['provider_failure_error_code'] == 'PROVIDER_OUTPUT_TRUNCATED'
    with pytest.raises(ValueError, match='already exists'):
        clone_provider_503_continuation(retained_run, target)
