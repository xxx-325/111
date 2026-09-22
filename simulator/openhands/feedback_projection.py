"""Deterministic public projection of Judge execution evidence."""
import hashlib
import re


class PublicFeedbackError(ValueError):
    """The verdict may be valid, but its public projection is unusable."""


_LABEL = re.compile(
    r'(?mi)^[ \t]*(?:={3,}|-{3,})[ \t]*'
    r'(END[ \t]+)?(INPUT|RESULT|ERROR)'
    r'[ \t]*(?:={3,}|-{3,})[ \t]*\r?$')

_PRESENTATION_CONTROL = re.compile(
    r'\x1b(?:\[[0-9;]*m|\[\?2004[hl]|\][02];[^\x07\x1b]*(?:\x07|\x1b\\))'
)
_UNSAFE_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def observation_output(item):
    """Return model-visible execution output without command or test source."""
    observation = item.get('observation') or {}
    content = observation.get('content') or []
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(part.get('text', '') for part in content
                         if isinstance(part, dict) and part.get('type', 'text') == 'text')
    error = observation.get('error')
    return error if isinstance(error, str) else ''


def labeled_blocks(text):
    """Extract explicit, closed execution blocks without guessing boundaries."""
    matches = list(_LABEL.finditer(text))
    result = {}
    open_block = None
    for match in matches:
        closing, label = bool(match.group(1)), match.group(2).upper()
        if not closing:
            if open_block is not None or label in result:
                raise PublicFeedbackError('public observation contains ambiguous labeled blocks')
            open_block = (label, match.end())
            continue
        if open_block is None or open_block[0] != label:
            raise PublicFeedbackError('public observation contains mismatched END marker')
        start, end = open_block[1], match.start()
        if text.startswith('\r\n', start):
            start += 2
        elif text.startswith('\n', start):
            start += 1
        if text[max(start, end - 2):end] == '\r\n':
            end -= 2
        elif end > start and text[end - 1] == '\n':
            end -= 1
        result[label] = text[start:end]
        open_block = None
    if open_block is not None:
        raise PublicFeedbackError('public observation contains an unclosed labeled block')
    return result


def _first(blocks, names):
    return next((blocks[name] for name in names if blocks.get(name)), None)


def _private_test_text(value):
    return _private_feedback_text(value)


def _private_feedback_text(value):
    """Recognize only private paths and test selectors at the boundary."""
    if not isinstance(value, str):
        return False
    return bool(
        re.search(r'(?i)/(?:reference|workspace/(?:checks|experiments))(?:/|\b)', value)
        or re.search(r'(?i)(?:^|[\s/])tests?[/\\][^\s]+', value)
        or re.search(r'(?i)::(?:test|tests?)[A-Za-z0-9_.-]*', value)
    )


def _validate_feedback_text(value, field):
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise PublicFeedbackError(f'{field} must be a non-empty observable sentence')
    if _private_feedback_text(value):
        raise PublicFeedbackError(f'{field} contains a private test path or selector')


def public_execution_value(value):
    """Remove terminal presentation controls while preserving textual bytes."""
    result = _PRESENTATION_CONTROL.sub('', value)
    if '\x1b' in result or _UNSAFE_CONTROL.search(result) or re.search(r'\r(?!\n)', result):
        raise PublicFeedbackError('layout-affecting terminal control is ambiguous')
    return result


def feedback_projection_mapping(feedback, observations):
    """Return a private, content-free mapping from raw blocks to public fields."""
    if feedback.get('kind') not in ('runtime_error', 'wrong_output'):
        return {}
    source = next((item for item in observations
                   if item.get('id') == feedback.get('evidence_id')), None)
    if source is None:
        return {}
    blocks = labeled_blocks(observation_output(source))
    result_key = 'ERROR' if feedback['kind'] == 'runtime_error' else 'RESULT'
    mapping = {}
    public_result_key = 'error' if result_key == 'ERROR' else 'output'
    for raw_key, public_key in (('INPUT', 'input'), (result_key, public_result_key)):
        raw = blocks.get(raw_key)
        public = feedback.get(public_key)
        if isinstance(raw, str) and isinstance(public, str):
            mapping[public_key] = {
                'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                'public_sha256': hashlib.sha256(public.encode()).hexdigest(),
                'raw_length': len(raw),
                'public_length': len(public),
                'terminal_controls_removed': raw != public,
            }
    return mapping


def project_public_feedback(proposal, observations=(), evidence_ids=()):
    """Build canonical feedback from one current execution observation."""
    if not isinstance(proposal, dict):
        raise PublicFeedbackError('invalid public feedback')
    if not proposal:
        return {}
    if set(proposal) - {'evidence_id', 'summary', 'symptom'}:
        raise PublicFeedbackError('public feedback selector has unsupported fields')
    _validate_feedback_text(proposal.get('symptom'), 'feedback')
    _validate_feedback_text(proposal.get('summary'), 'feedback_detail')
    evidence_id = proposal.get('evidence_id')
    if not isinstance(evidence_id, str) or evidence_id not in evidence_ids:
        raise PublicFeedbackError('public feedback must bind one verdict evidence ID')
    observation = next((item for item in observations if item.get('id') == evidence_id), None)
    if observation is None:
        raise PublicFeedbackError('public feedback references unknown observation')
    blocks = labeled_blocks(observation_output(observation))
    input_value = blocks.get('INPUT')
    error_value = blocks.get('ERROR')
    output_value = blocks.get('RESULT')
    has_error = 'ERROR' in blocks
    has_output = 'RESULT' in blocks
    if input_value is not None and has_error != has_output:
        result_value = error_value if has_error else output_value
        if has_error and not error_value:
            raise PublicFeedbackError('ERROR block must contain an observable error')
        if _private_test_text(input_value) or _private_test_text(result_value):
            raise PublicFeedbackError('private test path or assertion cannot become public feedback')
        input_value = public_execution_value(input_value)
        result_value = public_execution_value(result_value)
        kind = 'runtime_error' if has_error else 'wrong_output'
        result_key = 'error' if has_error else 'output'
        result = {'kind': kind, 'evidence_id': evidence_id,
                  'input': input_value, result_key: result_value}
        symptom = proposal.get('symptom')
        if isinstance(symptom, str) and symptom.strip():
            result['symptom'] = symptom
        summary = proposal.get('summary')
        if kind == 'wrong_output' and isinstance(summary, str) and summary.strip():
            result['summary'] = summary
        return result

    symptom = proposal.get('symptom')
    summary = proposal.get('summary')
    if ((isinstance(symptom, str) and symptom.strip())
            or (isinstance(summary, str) and summary.strip())):
        result = {'kind': 'logic_error', 'evidence_id': evidence_id}
        if isinstance(symptom, str) and symptom.strip():
            result['symptom'] = symptom
        if isinstance(summary, str) and summary.strip():
            result['summary'] = summary
        return result
    raise PublicFeedbackError(
        'bound execution must print INPUT and RESULT blocks, or provide one observable summary')


def validate_public_feedback(feedback, observations=(), evidence_ids=()):
    """Verify canonical feedback against the host-owned execution observation."""
    if not isinstance(feedback, dict):
        raise PublicFeedbackError('invalid public feedback')
    if not feedback:
        return {}
    kind = feedback.get('kind')
    evidence_id = feedback.get('evidence_id')
    if kind == 'logic_error':
        allowed = {'kind', 'evidence_id', 'symptom', 'summary'}
        if set(feedback) - allowed or not (feedback.get('symptom') or feedback.get('summary')):
            raise PublicFeedbackError('invalid canonical public feedback')
        selector = {'evidence_id': evidence_id}
        if 'symptom' in feedback:
            selector['symptom'] = feedback['symptom']
        if 'summary' in feedback:
            selector['summary'] = feedback['summary']
    elif kind in ('runtime_error', 'wrong_output'):
        result_key = 'error' if kind == 'runtime_error' else 'output'
        required = {'kind', 'evidence_id', 'input', result_key}
        allowed = required | {'symptom'} | ({'summary'} if kind == 'wrong_output' else set())
        if not required <= set(feedback) or set(feedback) - allowed:
            raise PublicFeedbackError('invalid canonical public feedback')
        if 'symptom' in feedback:
            _validate_feedback_text(feedback['symptom'], 'feedback')
        selector = {'evidence_id': evidence_id}
        if 'symptom' in feedback:
            selector['symptom'] = feedback['symptom']
        if 'summary' in feedback:
            _validate_feedback_text(feedback['summary'], 'feedback_detail')
            selector['summary'] = feedback['summary']
    else:
        raise PublicFeedbackError('invalid canonical public feedback')
    projected = project_public_feedback(selector, observations, evidence_ids)
    if feedback != projected:
        raise PublicFeedbackError('public feedback does not match bound execution')
    return feedback


def project_latest_feedback(observations, summary='', symptom=''):
    """Bind public feedback to host-owned observations without model-visible IDs."""
    eligible = public_feedback_observations(observations)
    identifiers = [item['id'] for item in eligible]
    # Ordinary shell output after a complete execution block must not replace
    # the concrete result. A newer labeled block that is malformed is kept
    # fail-closed instead of silently falling back to an older result.
    for item in reversed(eligible):
        raw = observation_output(item)
        try:
            project_public_feedback(
                {'evidence_id': item['id']}, eligible, identifiers
            )
        except PublicFeedbackError:
            if _LABEL.search(raw):
                raise
            continue
        selector = {'evidence_id': item['id']}
        if symptom:
            selector['symptom'] = symptom
        if summary:
            selector['summary'] = summary
        return project_public_feedback(selector, eligible, identifiers)
    if (summary or symptom) and eligible:
        selector = {'evidence_id': eligible[-1]['id']}
        if symptom:
            selector['symptom'] = symptom
        if summary:
            selector['summary'] = summary
        return project_public_feedback(selector, eligible, identifiers)
    return {}


def public_feedback_observations(observations):
    """Return current candidate observations that may support public feedback."""
    eligible = []
    for item in observations:
        observation = item.get('observation') or {}
        command = observation.get('command', '')
        if (item.get('tool') != 'terminal' or observation.get('timeout')
                or observation.get('exit_code') == -1
                or '/reference' in command or '/workspace/experiments' in command):
            continue
        eligible.append(item)
    return eligible


def has_public_feedback_source(observations):
    return bool(public_feedback_observations(observations))


def has_projectable_execution_blocks(observations):
    """Report whether empty feedback can use one canonical execution block."""
    try:
        feedback = project_latest_feedback(observations)
    except PublicFeedbackError:
        return False
    return feedback.get('kind') in ('runtime_error', 'wrong_output')


def authorized_raw_values(feedback):
    """Exact user-visible values whose execution provenance outranks source overlap."""
    if not isinstance(feedback, dict):
        return []
    kind = feedback.get('kind')
    if kind not in ('runtime_error', 'wrong_output'):
        return []
    result_key = 'error' if kind == 'runtime_error' else 'output'
    return [feedback.get('input', ''), feedback.get(result_key, '')]


def authorized_feedback_values(feedback):
    """Return exact fields from one host-projected, reviewed feedback object."""
    if not isinstance(feedback, dict):
        return []
    values = []
    symptom = feedback.get('symptom')
    if isinstance(symptom, str):
        values.append(symptom)
    if feedback.get('kind') == 'logic_error':
        value = feedback.get('summary')
        if isinstance(value, str) and value != symptom:
            values.append(value)
        return values
    values.extend(authorized_raw_values(feedback))
    summary = feedback.get('summary')
    if feedback.get('kind') == 'wrong_output' and isinstance(summary, str):
        values.append(summary)
    return values


def feedback_failure_key(feedback):
    """Return a stable identity from execution values, or the logic symptom."""
    if not isinstance(feedback, dict):
        return None
    kind = feedback.get('kind')
    if kind in ('runtime_error', 'wrong_output'):
        result_key = 'error' if kind == 'runtime_error' else 'output'
        values = (feedback.get('input'), feedback.get(result_key))
        if all(isinstance(value, str) for value in values):
            return hashlib.sha256(
                ('execution\0' + kind + '\0' + '\0'.join(values)).encode()
            ).hexdigest()
    symptom = feedback.get('symptom') or feedback.get('summary')
    if not isinstance(symptom, str) or not symptom.strip():
        return None
    normalized = ' '.join(symptom.split()).casefold()
    return hashlib.sha256(
        normalized.encode()
    ).hexdigest()


def feedback_units(feedback):
    """Split reviewed Judge feedback into ordered user-visible units.

    The first unit is the reviewed symptom.  A concrete observation is kept
    separate so the host can release it only after the same failure recurs (or
    the Code Agent asks for more detail).  Execution feedback remains one
    atomic unit: input and its actual result/error are never split.
    """
    key = feedback_failure_key(feedback)
    if not key:
        return []
    symptom = feedback.get('symptom') or feedback.get('summary')
    symptom_id = f'feedback-{key[:16]}-symptom'
    observation_id = f'feedback-{key[:16]}-observation'
    units = [dict(
        id=symptom_id,
        category='symptom',
        observation={'kind': 'logic_error', 'summary': symptom,
                     'evidence_basis': feedback.get(
                         'evidence_basis', 'static_observation')},
    )]
    kind = feedback.get('kind')
    if kind == 'runtime_error':
        observation = {
            'kind': kind,
            'input': feedback.get('input', ''),
            'error': feedback.get('error', ''),
            'evidence_basis': feedback.get('evidence_basis', 'execution'),
        }
    elif kind == 'wrong_output':
        observation = {
            'kind': kind,
            'input': feedback.get('input', ''),
            'output': feedback.get('output', ''),
            'evidence_basis': feedback.get('evidence_basis', 'execution'),
        }
        summary = feedback.get('summary')
        if isinstance(summary, str) and summary and summary != symptom:
            observation['summary'] = summary
    else:
        summary = feedback.get('summary')
        if isinstance(summary, str) and summary and summary != symptom:
            observation = {'kind': kind, 'summary': summary,
                           'evidence_basis': feedback.get(
                               'evidence_basis', 'static_observation')}
        else:
            observation = None
    if observation is not None:
        units.append(dict(id=observation_id, category='concrete_observation',
                          observation=observation))
    return units


def feedback_units_failure_key(units):
    """Derive the private failure identity from stored units, not new state."""
    first = next((unit for unit in units or ()
                  if unit.get('category') == 'concrete_observation'), None)
    if first is None:
        first = next((unit for unit in units or ()
                      if unit.get('category') == 'symptom'), None)
    return feedback_failure_key((first or {}).get('observation', {}))


def filter_authorized_overlap(overlap, values):
    """Keep private-source matches unless exact current execution authorizes them."""
    result = dict(overlap)
    result['matches'] = [match for match in overlap.get('matches', []) if not (
        match.get('text') and any(match['text'] in value for value in values)
        and not _private_test_text(match['text']))]
    return result
