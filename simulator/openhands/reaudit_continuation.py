"""Explicit one-shot audit upgrade of a stopped, rejected Judge submission."""
import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from ..episode import save, load_environment
from .budget import Budget
from .relay import Relay
from .judge import candidate_hash, validate_verdict, review_verdict
from .reviewed_episode import ReviewedEpisode, reset_cloned_execution
from .progressive import ProgressiveEpisode
from .dialogue_export import export_dialogue

REVIEWED_SCHEMA = ReviewedEpisode.checkpoint_schema
PROGRESSIVE_SCHEMA = ProgressiveEpisode.checkpoint_schema


def continue_run(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    saved = json.loads((source/'private/checkpoint.json').read_text())
    if saved['state']['status'] != 'paused' or saved.get('in_flight'):
        raise ValueError('Only a stopped, paused run can be explicitly upgraded')
    pause_reason = saved['state'].get('pause_reason', '')
    manual_rejection = pause_reason.startswith('Assistant rejected Judge feedback:')
    applied_uncertain = pause_reason.startswith('Judge uncertain;')
    job = saved['progressive']['job']
    if job is None and (manual_rejection or applied_uncertain):
        current = saved['progressive']['tasks'][saved['state']['task_id']]
        job = saved['progressive']['decisions'][current['applied_job']]['job']
    record = saved['progressive']['decisions'][job['id']]
    retained_uncertain = (
        applied_uncertain
        and record.get('accepted') is True
        and record.get('payload', {}).get('outcome') == 'uncertain'
    )
    if not manual_rejection and not retained_uncertain and (
        record['accepted']
        or record.get('error') != 'ValueError: Judge verdict/feedback audit failed'
    ):
        raise ValueError('Not an audit-only rejection')
    for role in ('user', 'code', 'judge'):
        root = source/'private'/role
        active = json.loads((root/'outbox/active.json').read_text())
        if active['status'] != 'stopped':
            raise ValueError('SDK worker is not stopped')
        cfg = json.loads((root/'inbox/config.json').read_text())
        name = 'session-oh-'+cfg['conversation_id']+('-'+cfg['container_suffix'] if cfg.get('container_suffix') else '')
        inspect = subprocess.run(['docker', 'inspect', name], capture_output=True)
        if inspect.returncode == 0 and json.loads(inspect.stdout)[0]['State']['Running']:
            raise ValueError('Source container is still running')
    for path in ('workspace/candidate', 'judge-workspace/candidate'):
        if candidate_hash(source/path) != job['candidate_version']:
            raise ValueError('Candidate changed since judgment')
    config = copy.deepcopy(saved['config'])
    for group in ('_reviewed_policy', '_progressive_policy'):
        for name, old_hash in config.get(group, {}).items():
            digest = hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
            if group == '_progressive_policy' and name in {
                'judge.py', 'judge_tools.py', 'progressive.py'
            }:
                config[group][name] = digest
            elif digest != old_hash:
                raise ValueError('Unrelated policy changed: '+name)
    if json.loads((source/'private/budget.json').read_text()).get('pending'):
        raise ValueError('Uncertain model call retained; no automatic retry')
    shutil.copytree(source, output)
    private = output/'private'
    reset_cloned_execution(private)
    save(private/'pre-upgrade-checkpoint.json', saved)
    audit = private/'audit-upgrade'
    audit.mkdir()
    started = time.monotonic()
    budget = Budget(dict(config, max_seconds=max(0, config['max_seconds']-saved['elapsed_seconds'])),
                    saved['budget'], private/'budget.json')
    task = saved['tasks'][saved['state']['task_index']]
    current = saved['progressive']['tasks'][job['task_id']]
    validate_verdict(record['payload'], job, record['observations'], current['plan'])
    relay = Relay(config.get('judge', config['user']), audit/'mailbox', audit/'provider.jsonl',
                  deadline=budget.deadline, budget=budget, role='judge')
    save(audit/'started.json', {'source': str(source), 'job': job['id'], 'retry': False})
    try:
        review = review_verdict(relay, task, job, record['reviewed_payload'], record['events'],
            record['observations'], record['disclosure']['requirement'], current['plan'],
            public_history=[p for p in saved['public'] if p['kind'] in ('user', 'assistant')])
        save(audit/'result.json', review)
    except Exception as error:
        save(audit/'error.json', {'type': type(error).__name__, 'message': str(error)})
        raise
    finally:
        saved['elapsed_seconds'] += time.monotonic()-started
        saved['budget'] = budget.snapshot()
        save(private/'checkpoint.json', saved)
    verdict_pending = (
        review.get('grounded') is True
        and review.get('verdict_valid') is False
    )
    if not review['allowed'] and not verdict_pending:
        export_dialogue(output)
        return 'reaudit_rejected'
    record['previous_review'] = record['review']
    record['review'] = review
    if verdict_pending:
        record.update(
            accepted=False,
            status='verdict_pending',
            verdict_rejections=[dict(
                source='explicit_reaudit',
                reason='; '.join(review.get('reasons', []))
                or 'grounded Judge outcome requires correction',
            )],
            verdict_revisions=[],
        )
        if retained_uncertain:
            current = saved['progressive']['tasks'][job['task_id']]
            saved['progressive']['job'] = job
            current.pop('applied_job', None)
            current['verdict'] = None
            current.pop('feedback', None)
            current.pop('simulated_experience', None)
            current['release_history'] = [
                item for item in current.get('release_history', [])
                if item.get('job_id') != job['id']
            ]
            saved['state']['checks'] = [
                item for item in saved['state'].get('checks', [])
                if item.get('id') != job['id']
            ]
    else:
        record.update(accepted=True, status='ready')
    record.pop('error', None)
    saved['config'] = config
    if manual_rejection:
        # Preserve prior decisions, but request fresh human review under the
        # explicitly revised observation-disclosure policy. Do not release twice.
        shutil.move(str(private/'assistant-gates'), str(audit/'prior-assistant-gates'))
        current.pop('assistant_reviewed_job', None)
    saved['audit_upgrade'] = {'source': str(source), 'job': job['id'],
        'old_policy': json.loads((source/'private/checkpoint.json').read_text())['config']['_progressive_policy'],
        'new_policy': config['_progressive_policy'], 'authority': 'Explicit user-requested audit repair and continuation'}
    for role in ('user', 'code', 'judge'):
        cfgpath = private/role/'inbox/config.json'
        cfg = json.loads(cfgpath.read_text())
        cfg['container_suffix'] = hashlib.sha256(str(output).encode()).hexdigest()[:12]
        save(cfgpath, cfg)
    save(private/'judgments'/f"{job['id']}.json", record)
    save(private/'checkpoint.json', saved)
    episode_type = {
        REVIEWED_SCHEMA: ReviewedEpisode,
        PROGRESSIVE_SCHEMA: ProgressiveEpisode,
    }.get(saved.get('schema'))
    if episode_type is None:
        raise ValueError('unsupported checkpoint schema for audit continuation')
    episode = episode_type(config, output, resume=True)
    return episode.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'env-file'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    load_environment(args.env_file)
    print(continue_run(args.source, args.output))
