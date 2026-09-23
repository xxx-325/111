"""Deterministic, non-prefix disclosure of source-backed issue fragments."""

from .feedback_projection import feedback_failure_key, feedback_units


def check_released(plan, released):
    known={item['id']:item for item in plan['items']}
    if not isinstance(released,list) or any(not isinstance(i,str) or i not in known for i in released) or len(released)!=len(set(released)):
        raise ValueError('Invalid released fragment set')
    if any(dep not in released for i in released for dep in known[i]['requires']):
        raise ValueError('Released fragments are missing prerequisites')


def include(plan, released, requested):
    check_released(plan,released)
    known={item['id']:item for item in plan['items']}
    seen=set(released)
    if any(identifier not in known for identifier in requested):
        raise ValueError('Unknown disclosure fragment')
    def next_missing(identifier):
        if identifier in seen:
            return None
        for dependency in known[identifier]['requires']:
            missing = next_missing(dependency)
            if missing is not None:
                return missing
        return identifier
    for identifier in requested:
        missing = next_missing(identifier)
        if missing is not None:
            seen.add(missing)
            break
    return [item['id'] for item in plan['items'] if item['id'] in seen]


def initial_release(plan):
    first=next(item['id'] for item in plan['items'] if item['category']=='symptom')
    return include(plan,[],[first])


def release_after(plan, released, verdict, *, triggered_only=()):
    check_released(plan,released)
    requested=verdict.get('requested_fragment_ids',[])
    if verdict['outcome']=='solved':
        return list(released)
    if requested:
        return include(plan,released,requested)
    if verdict['outcome']=='unsolved':
        # Validated plans are ordered within symptom/cause groups, never merged.
        item=next((item for item in plan['items']
                   if item['id'] not in released and item['id'] not in triggered_only),None)
        if item:
            return include(plan,released,[item['id']])
    return list(released)


def release_feedback(feedback, released_ids=(), previous_failure_key=None,
                      *, explicit_question=False):
    """Release at most one unit of one repeated Judge failure.

    Feedback units are independent from issue fragments.  A new failure starts
    with its generic symptom; a repeated failure can reveal one further unit.
    Solved and uncertain verdicts do not reveal feedback units.
    """
    if not isinstance(feedback, dict) or feedback.get('outcome') in ('solved', 'uncertain'):
        return dict(failure_key=None, units=[], released_unit_ids=[], added=[])
    # Callers may pass either the stored outcome wrapper or the projected
    # public observation itself.
    observation = feedback.get('observation', feedback)
    units = feedback_units(observation)
    failure_key = feedback_failure_key(observation)
    if not units or not failure_key:
        return dict(failure_key=None, units=[], released_unit_ids=[], added=[])
    same_failure = failure_key == previous_failure_key
    known = {unit['id'] for unit in units}
    prior = [identifier for identifier in released_ids if identifier in known]
    released = [] if not same_failure else list(prior)
    if not released:
        released = [units[0]['id']]
    elif same_failure or explicit_question:
        next_unit = next((unit['id'] for unit in units if unit['id'] not in released), None)
        if next_unit:
            released.append(next_unit)
    added = [identifier for identifier in released if identifier not in prior]
    return dict(failure_key=failure_key, units=units,
                released_unit_ids=released, added=added)


def released_feedback_unit(release):
    """Return the latest currently visible unit, even without new disclosure."""
    units = release.get('units') or []
    visible = release.get('released_unit_ids') or []
    if not units or not visible:
        return None
    wanted = visible[-1]
    return next((unit for unit in units if unit.get('id') == wanted), None)
