"""One-shot paired dialogue replay; no repository execution or regeneration."""
import argparse
import hashlib
import json
import random
import re
from pathlib import Path

from .api_agent import API, parse_object
from .episode import load_environment, save
from .expression import ACTION_STATE, replay_messages
from . import replay_grounding as grounding


def run(cases, config, output, api_factory=API, modes=('baseline',)):
    if not modes or len(set(modes)) != len(modes) or any(m not in ('baseline', 'grounded') for m in modes):
        raise ValueError('invalid replay modes')
    if len({c['id'] for c in cases}) != len(cases): raise ValueError('duplicate cases')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    jobs = [(c, mode, enabled, repeat) for c in cases for mode in modes for enabled in (False, True) for repeat in (1, 2)]
    random.Random(20260914).shuffle(jobs)
    api = api_factory(config)
    save(output / 'manifest.json', dict(config={k: config[k] for k in ('adapter', 'model', 'base_url', 'key_env') if k in config},
         cases_sha256=hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
         requests=len(jobs), task_requests=len(cases) if 'grounded' in modes else 0, modes=list(modes),
         tools=False, retries=0, protocol='replay-v2', status='running'))
    results, mapping = [], []
    for index, (case, mode, enabled, repeat) in enumerate(jobs, 1):
        identifier = f'r{index:03d}'
        mapping.append(dict(id=identifier, mode=mode, examples_enabled=enabled, repeat=repeat))
        results.append(dict(id=identifier, case_id=case['id'], status='not_started'))
    def persist():
        save(output / 'results.json', results)
        save(output / 'groups.json', mapping)
        save(output / 'blind-results.json', [{k: r[k] for k in ('id','case_id','status','action','message','error_type') if k in r} for r in results])
    persist()
    tasks = {}
    if 'grounded' in modes:
        for case in cases:
            rec = dict(status='pending', input=grounding.task_messages(case['history']))
            tasks[case['id']] = rec
            save(output / 'task-records.json', tasks)
            try:
                rec['raw'] = api.complete(rec['input'], tools=False)
                if rec['raw'].get('tool_calls'): raise ValueError('unexpected task tool call')
                rec['task'] = grounding.validate_task(parse_object(rec['raw']['content']), case['history'])
                rec['conditions'] = grounding.conditions(rec['task'])
                rec['status'] = 'ready'
            except Exception as error:
                rec.update(status='failed', error_type=type(error).__name__)
            finally:
                save(output / 'task-records.json', tasks)
            print('task', case['id'], rec['status'], flush=True)
    for record, (case, mode, enabled, repeat) in zip(results, jobs):
        identifier = record['id']
        if mode == 'grounded' and tasks[case['id']]['status'] != 'ready':
            record.update(status='failed', error_type='TaskPreparationFailed')
            persist()
            continue
        task = tasks[case['id']]['task'] if mode == 'grounded' else None
        messages = grounding.response_messages(case['history'], enabled, task) if task else replay_messages(case['history'], enabled)
        record.update(status='pending', input=messages)
        persist()
        try:
            raw = api.complete(messages, tools=False)
            record['raw'] = raw
            if raw.get('tool_calls'):
                raise ValueError('unexpected tool call')
            parsed = parse_object(raw['content'])
            if parsed.get('action') not in ACTION_STATE or not isinstance(parsed.get('message'), str) or not parsed['message'].strip():
                raise ValueError('invalid action/message')
            if re.search(r'sk-[\w-]{12,}|-----BEGIN .*PRIVATE KEY|<system>|<codex_internal_context', parsed['message']):
                record['safety'] = 'failed'
                raise ValueError('unsafe output')
            record['safety'] = 'basic_screen_passed; semantic_review_required'
            record.update(action=parsed['action'], message=parsed['message'], status='generated')
            if task:
                record['transition'] = grounding.check_decision(parsed, task)
                record['decision'] = parsed['decision']
                if record['transition']['violations']:
                    record.update(status='failed', error_type='DecisionViolation')
        except Exception as error:
            # Keep first output/error. No retry, no execution, no invented replacement.
            record.update(status='failed', error_type=type(error).__name__)
        finally:
            persist()
        print(identifier, case['id'], record['status'], flush=True)
    save(output / 'manifest.json', dict(config={k: config[k] for k in ('adapter', 'model', 'base_url', 'key_env') if k in config},
         cases_sha256=hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
         requests=len(jobs), task_requests=len(cases) if 'grounded' in modes else 0, modes=list(modes),
         tools=False, retries=0, protocol='replay-v2', status='complete'))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--output', required=True)
    parser.add_argument('--modes', nargs='+', choices=('baseline','grounded'), default=['baseline'])
    args = parser.parse_args()
    load_environment(args.env_file)
    config = json.loads(Path(args.config).read_text())
    run(json.loads(Path(args.cases).read_text()), config.get('user', config), args.output, modes=tuple(args.modes))


if __name__ == '__main__':
    main()
