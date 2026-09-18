"""Deterministic host state authority. Text is not a completion signal."""
import copy
import uuid

from ..state_machine import CORE_STATES, CONTROL_EVENTS


class TransitionError(ValueError):
    pass


class TaskState:
    def __init__(self, data=None):
        self.data = data or dict(version=4, task_index=0, task_id='task-1', state=None, code_reply=None,
                                control=None, phase='user', accepted=[], checks=[], blockers=[],
                                permit=None, messages=[], transitions=[], published_state_counts={}, status='running')

    def view(self):
        value = copy.deepcopy(self.data)
        value['communication'] = self.communication()
        value.pop('messages', None)
        value.pop('transitions', None)
        value['published_messages'] = sum(
            item.get('published') is True and bool(item.get('transition_id'))
            for item in self.data.get('messages', [])
        )
        value['unresolved_failures'] = [{'id': c['id'], 'tool': c.get('tool'), 'exit_code': c.get('exit_code')}
                                         for c in self.data['checks'] if c['result'] == 'failed' and not c.get('resolved_by')]
        return value

    def communication(self):
        reply = self.data.get('code_reply')
        return dict(task_id=self.data['task_id'], requirement_owner='User Agent',
                    sender='User Agent', recipient='Code Agent', implementer='Code Agent',
                    stage='followup' if reply else 'initial_delegation',
                    code_has_spoken_for_current_task=reply is not None,
                    last_code_reply=copy.deepcopy(reply))

    def pending_checks(self):
        return [{'id': c['id'], 'tool': c.get('tool'), 'revision': c.get('revision'),
                 'exit_code': c.get('exit_code'), 'observation': c.get('observation', c.get('summary'))}
                for c in self.data['checks'] if c['result'] == 'failed' and not c.get('resolved_by')]

    def identity(self, payload):
        if self.data['phase'] != 'user' or self.data['status'] != 'running':
            raise TransitionError('not an active user turn')
        if payload.get('task_id') != self.data['task_id']:
            raise TransitionError('stale or unreleased task')

    def evidence(self, identifiers):
        known = {c['id']: c for c in self.data['checks']}
        if not isinstance(identifiers, list) or any(i not in known for i in identifiers):
            raise TransitionError('evidence must reference recorded observations')
        return [known[i] for i in identifiers]

    def transition(self, payload):
        self.identity(payload)
        target = payload.get('state') or self.data['state']
        control = payload.get('control')
        if target not in CORE_STATES or control not in CONTROL_EVENTS:
            raise TransitionError('unknown task state or control')
        if not str(payload.get('reason', '')).strip():
            raise TransitionError('transition needs a reason')
        evidence = self.evidence(payload.get('evidence_ids', []))
        for identifier in payload.get('resolves', []):
            failure = next((c for c in self.data['checks'] if c['id'] == identifier), None)
            if not failure or failure['result'] != 'failed' or not any((c['result'] == 'passed' or c.get('exit_code') == 0) and c['revision'] >= failure['revision'] and c['id'] != identifier for c in evidence):
                raise TransitionError('failure resolution requires a later recorded successful check and semantic review')
        for identifier in payload.get('dismisses', []):
            if not any(c['id'] == identifier and c['result'] == 'failed' for c in self.data['checks']):
                raise TransitionError('dismisses must reference a recorded failed observation')
        blocker = payload.get('blocker')
        if blocker:
            if not all(blocker.get(k) for k in ('reason', 'affected_operation', 'evidence_ids')):
                raise TransitionError('blocker needs reason, affected_operation and actual evidence_ids')
            self.evidence(blocker['evidence_ids'])
        for identifier in payload.get('clears_blockers', []):
            if not any(b['id'] == identifier for b in self.data['blockers']) or not any(c.get('exit_code') == 0 or c['result'] == 'passed' for c in evidence):
                raise TransitionError('clearing a known blocker requires successful recorded evidence')
        entry = dict(id=str(uuid.uuid4()), task_id=self.data['task_id'], previous=self.data['state'],
                     state=target, control=control, reason=payload['reason'], evidence_ids=payload.get('evidence_ids', []))
        latest_judge = next((row for row in reversed(self.data.get('checks', []))
                             if row.get('tool') == 'judge_summary'
                             and isinstance(row.get('summary'), dict)
                             and row['summary'].get('outcome') in ('solved', 'unsolved')), None)
        if latest_judge and latest_judge['summary']['outcome'] == 'solved':
            entry['post_solved'] = True
        # Semantic review must run before this mutation; this class validates mechanics only.
        for check in self.data['checks']:
            if check['id'] in payload.get('resolves', []):
                check['resolved_by'] = entry['id']
            if check['id'] in payload.get('dismisses', []):
                check.update(resolved_by=entry['id'], disposition='unrelated_technical_error_after_semantic_review')
        self.data.update(state=target, control=control, permit=entry)
        self.data['blockers'] = [b for b in self.data['blockers'] if b['id'] not in payload.get('clears_blockers', [])]
        if blocker:
            self.data['blockers'].append(dict(blocker, id=str(uuid.uuid4())))
        self.data['transitions'].append(entry)
        return entry

    def send(self, payload):
        self.identity(payload)
        permit = self.data['permit']
        if not permit or payload.get('permit_id') != permit['id']:
            raise TransitionError('request a transition before sending')
        if not isinstance(payload.get('text'), str) or not payload['text'].strip():
            raise TransitionError('empty public message')
        self.evidence(payload.get('evidence_ids', []))
        message = dict(id=str(uuid.uuid4()), task_id=self.data['task_id'], text=payload['text'],
                       transition_id=permit['id'], delivered=False, published=False)
        self.data['messages'].append(message)
        self.data.update(phase='code', permit=None)
        return message

    def record_published_transition(self, transition_id):
        """Count a selected state once, only after its public message exists."""
        entry = next((row for row in self.data['transitions']
                      if row['id'] == transition_id), None)
        if not entry:
            raise TransitionError('published message has no recorded transition')
        if entry.get('counted_for_selection'):
            return
        counts = self.data.setdefault('published_state_counts', {})
        counts[entry['state']] = counts.get(entry['state'], 0) + 1
        entry['counted_for_selection'] = True

    def accept(self, payload):
        self.identity(payload)
        if not payload.get('reason'):
            raise TransitionError('acceptance needs a basis')
        if any(c['result'] == 'failed' and not c.get('resolved_by') for c in self.data['checks']):
            raise TransitionError('unresolved observed failure')
        if self.data['blockers']:
            raise TransitionError('unresolved blocker; pause instead')
        selected = self.evidence(payload.get('evidence_ids', []))
        independently_observed = [c for c in self.data['checks'] if c.get('tool') != 'code_report']
        result = dict(task_id=self.data['task_id'], reason=payload['reason'],
                      basis=('assistant_report' if all(c.get('tool') == 'code_report' for c in selected) else 'observations') if selected else ('user_acceptance_with_unlinked_observations' if independently_observed else 'user_acceptance_without_independent_check'),
                      evidence_ids=payload.get('evidence_ids', []),
                      checks_passed=any(c['result'] == 'passed' for c in selected))
        self.data['accepted'].append(result)
        self.data.update(phase='accepted', permit=None)
        return result

    def release_next(self):
        if self.data['phase'] != 'accepted':
            raise TransitionError('current task has not been accepted')
        self.data['task_index'] += 1
        self.data.update(task_id=f"task-{self.data['task_index']+1}", phase='user', state=None,
                         checks=[], blockers=[], permit=None, code_reply=None,
                         published_state_counts={})

    def pause(self, payload):
        self.identity(payload)
        if not payload.get('reason'):
            raise TransitionError('pause needs a reason')
        self.data.update(status='paused', pause_reason=payload['reason'], permit=None)
