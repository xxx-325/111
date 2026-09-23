"""Controlled extensions of a contiguous commit chain; never reference patches."""
import copy
import hashlib
import json
from pathlib import Path

SCHEMA = 'continuous-commit-scenario-v1'


def load_scenario(config):
    path = config.get('scenario_file')
    if not path:
        return None
    if (not config.get('continuous_commits') or not config.get('progressive_issues')
            or config.get('swe_chain_evo') is not None):
        raise ValueError('Controlled scenarios require progressive continuous commits')
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    if value.get('schema') != SCHEMA or not isinstance(value.get('commits'), list):
        raise ValueError('Unknown continuous commit scenario')
    return value, hashlib.sha256(raw).hexdigest()


def expand_tasks(tasks, scenario):
    """Keep every original goal and insert zero or more dependent extensions."""
    value, digest = scenario
    if [row.get('commit') for row in value['commits']] != [t['reference'] for t in tasks]:
        raise ValueError('Scenario must cover the exact ordered commit chain')
    expanded, facts = [], {}
    for source_index, (task, row) in enumerate(zip(tasks, value['commits'])):
        if task['kind'] != 'commit':
            raise ValueError('Only original commit tasks may be expanded')
        variants = [dict(task)]
        extensions = row.get('extensions', [])
        if not isinstance(extensions, list):
            raise ValueError('extensions must be a list')
        for extension in extensions:
            if not all(isinstance(extension.get(k), str) and extension[k].strip()
                       for k in ('title', 'body')):
                raise ValueError('Extension requires a concrete title and body')
            variants.append(dict(kind='scenario_extension', title=extension['title'],
                                 body=extension['body'], identifier='', reference=None,
                                 base=task['base'], patch=''))
        for offset, variant in enumerate(variants):
            settings = row if offset == 0 else extensions[offset - 1]
            inherited = copy.deepcopy(list(facts.values()))
            current_facts = copy.deepcopy(settings.get('facts', []))
            if not isinstance(current_facts, list):
                raise ValueError('facts must be a list')
            for fact in current_facts:
                if (not isinstance(fact, dict) or not all(
                        isinstance(fact.get(k), str) and fact[k].strip()
                        for k in ('id', 'text', 'scope', 'trigger', 'type'))
                        or fact['type'] not in ('M1', 'M2', 'M3', 'M4', 'M5', 'M6')
                        or fact['id'] in facts):
                    raise ValueError('Each controlled fact needs a unique id, type, text, scope and trigger')
                supersedes = fact.get('supersedes', [])
                if (not isinstance(supersedes, list)
                        or any(not isinstance(ref, str) or ref not in facts for ref in supersedes)):
                    raise ValueError('A correction must reference an earlier fact')
                facts[fact['id']] = dict(fact, source_commit=task['reference'],
                                       task_id='task-' + str(len(expanded) + 1))
            edits = copy.deepcopy(settings.get('repository_edits', []))
            if not isinstance(edits, list) or len({e.get('path') for e in edits}) != len(edits):
                raise ValueError('repository_edits must have distinct paths')
            for edit in edits:
                path = Path(edit.get('path', ''))
                if (not path.parts or path.is_absolute() or '..' in path.parts or '.git' in path.parts
                        or 'before' not in edit
                        or not isinstance(edit.get('before'), (str, type(None)))
                        or not isinstance(edit.get('after'), str)
                        or edit.get('type') not in ('M3', 'M4', 'M5')
                        or not isinstance(edit.get('reason'), str) or not edit['reason'].strip()):
                    raise ValueError('Repository perturbation requires safe path, exact before/after, type and reason')
            variant['scenario'] = dict(schema=SCHEMA, sha256=digest,
                source_commit=task['reference'], source_base=task['base'],
                source_task_index=source_index,
                original=offset == 0, facts=current_facts, repository_edits=edits,
                inherited_facts=inherited)
            expanded.append(variant)
    return expanded


def add_fact_fragments(plan, document, scenario):
    """Attach controlled facts outside the source-commit projection audit."""
    plan, document = copy.deepcopy(plan), dict(document)
    triggers = {}
    facts = scenario.get('inherited_facts', []) + scenario.get('facts', [])
    by_id = {fact['id']: fact for fact in facts}
    for index, fact in enumerate(facts, 1):
        identifier = f'external{index}'
        if any(item['id'] == identifier for item in plan['items']):
            raise ValueError('Controlled fragment identity collision')
        text = fact['scope'] + ': ' + fact['text']
        if fact.get('supersedes'):
            old = '; '.join(by_id[ref]['scope'] + ': ' + by_id[ref]['text']
                            for ref in fact['supersedes'])
            text += '\nThis updates the earlier condition only in the stated scope: ' + old
        plan['items'].append(dict(id=identifier, category='symptom', text=text,
                                 source_quote=text, requires=[], related=[]))
        document['body'] += '\n\n' + text
        triggers[identifier] = dict(fact_id=fact['id'], trigger=fact['trigger'], scope=fact['scope'],
                                    supersedes=fact.get('supersedes', []))
    # validate() canonicalizes symptom/cause order.
    from .issue_stages import validate
    return validate({'items': plan['items']}, document), document, triggers


def apply_repository_edits(candidate, edits):
    """Compare all preconditions before writing; no shell patch or Git fallback."""
    candidate = Path(candidate).resolve()
    changes = []
    for edit in edits:
        relative = Path(edit['path'])
        if not relative.parts or relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
            raise ValueError('Perturbation path escapes the candidate')
        path = candidate / edit['path']
        if candidate not in path.resolve().parents or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Perturbation path escapes the candidate')
        old = path.read_text() if path.exists() else None
        if old != edit['before']:
            raise ValueError('Perturbation precondition differs from accumulated candidate: ' + edit['path'])
        changes.append((path, edit['after']))
    for path, content in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def scenario_judge_context(scenario):
    """The reference commit validates its original goal, never synthetic facts."""
    return dict(
        current_facts=scenario.get('facts', []),
        inherited_facts=scenario.get('inherited_facts', []),
        instruction=('These are controlled scenario facts, not claims from the original commit. '
                     'Apply each only within its scope. Corrections override earlier facts only '
                     'where their scopes overlap; preserve the other cases. Reference code covers '
                     'only the original commit goal, not extensions. A trigger is a disclosure '
                     'condition, not an observed execution result. Never fabricate a failed run. '
                     'Facts that remain untriggered need not appear in the dialogue. Do not '
                     'invent failures or prolong a solved task just to disclose every fact.'),
    )
