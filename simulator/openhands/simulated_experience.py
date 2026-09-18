"""Explicit provenance for simulated user observations, never fake tool events."""
import copy


def build_experience(record, observation=None):
    payload, job = record['payload'], record['job']
    observation = (payload.get('public_feedback', {})
                   if observation is None else observation)
    kind = observation.get('kind')
    if (not record.get('accepted') or payload['outcome'] != 'unsolved'
            or kind not in ('runtime_error', 'wrong_output', 'logic_error')
            or not payload.get('evidence_ids')):
        return None
    claims = {
        'runtime_error': ['verbatim_input', 'verbatim_error'],
        'wrong_output': ['verbatim_input', 'verbatim_output'],
        'logic_error': ['brief_logic_feedback'],
    }[kind]
    return dict(executor='Judge', revision=job['revision'], candidate_version=job['candidate_version'],
                evidence_ids=list(payload['evidence_ids']), summary_id=job['id'],
                observation=copy.deepcopy(observation), allowed_claims=claims,
                operation_details_allowed=False)


def visible_experience(current):
    experience = current.get('simulated_experience')
    verdict = current.get('verdict') or {}
    if not experience or any(experience[k] != verdict.get(k) for k in ('revision', 'candidate_version')):
        return None
    if current.get('active_revision', experience['revision']) != experience['revision']:
        return None
    observation = copy.deepcopy(experience['observation'])
    observation.pop('evidence_id', None)
    return dict(kind='user_visible_failure', observation=observation,
                allowed_claims=experience['allowed_claims'], operation_details_allowed=False)
