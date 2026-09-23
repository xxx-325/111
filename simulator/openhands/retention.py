"""Compact completed scenario runs after preserving their review evidence."""
import gzip
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from ..episode import save
from .memory_episode import read_rows, snapshot_hash


def compact_decision(record):
    result = dict(record)
    for field in ('events', 'observations'):
        if field in result:
            result[field + '_ids'] = [row['id'] for row in result.pop(field)]
    job = dict(result.get('job', {}))
    job.pop('public_turn', None)
    result['job'] = job
    return result


def review_summary(saved):
    tasks = []
    for task in saved.get('tasks', []):
        task = {k: v for k, v in task.items() if k != 'patch'}
        if task.get('scenario'):
            task['scenario'] = {k: v for k, v in task['scenario'].items() if k != 'inherited_facts'}
        tasks.append(task)
    progress = saved.get('progressive', {})
    return dict(schema='episode-review-v1', status=saved['state']['status'],
                pause_reason=saved['state'].get('pause_reason'), base=saved.get('base'),
                tasks=tasks, disclosure=progress.get('tasks', {}),
                decisions={key: compact_decision(value)
                           for key, value in progress.get('decisions', {}).items()},
                accepted=saved['state'].get('accepted', []),
                transitions=saved['state'].get('transitions', []),
                selections=saved.get('transition_selections', {}),
                budget=saved.get('budget'), compression=saved.get('compression', []))


def write_trace(source, destination):
    """One evidence archive; repeated full provider contexts are not retained."""
    digest = hashlib.sha256()
    with gzip.open(destination, 'wb') as target:
        def emit(kind, **value):
            raw = (json.dumps(dict(kind=kind, **value), ensure_ascii=False) + '\n').encode()
            digest.update(raw)
            target.write(raw)

        for role in ('user', 'code', 'judge'):
            directory = source / 'private' / role
            journal = directory / 'provider.jsonl'
            contexts = set()
            if journal.exists():
                for row in read_rows(journal):
                    if row.get('kind') == 'request':
                        body = row.get('input', {})
                        context = dict(system=[m for m in body.get('messages', [])
                                               if m.get('role') in ('system', 'developer')],
                                       tools=body.get('tools', []))
                        key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
                        if key not in contexts:
                            emit('instructions', role=role, id=key, value=context)
                            contexts.add(key)
                        row = {k: v for k, v in row.items() if k != 'input'}
                        row['context_id'] = key
                        row['request_sha256'] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                        row['options'] = {k: v for k, v in body.items() if k not in ('messages', 'tools')}
                    emit('provider', role=role, value=row)
            events = directory / 'outbox/events.jsonl'
            if events.exists():
                for event in read_rows(events):
                    # Code tool text/actions are already in the public export.
                    if role != 'code' or event.get('kind') not in ('ActionEvent', 'ObservationEvent', 'MessageEvent'):
                        emit('event', role=role, value=event)
            for name in ('worker.log',):
                path = directory / name
                if path.exists():
                    emit('file', path=str(path.relative_to(source)), text=path.read_text())
            # Original terminal logs explain projection and clipping failures.
            outputs = directory / 'execution/outputs'
            if outputs.exists():
                for path in sorted(outputs.rglob('*')):
                    if path.is_symlink():
                        raise ValueError('Review evidence contains a symbolic link')
                    if path.is_file():
                        emit('file', path=str(path.relative_to(source)), text=path.read_text())
        for name in ('controls.jsonl', 'approvals.jsonl'):
            path = source / 'private' / name
            if path.exists():
                for row in read_rows(path):
                    emit('control', source=name, value=row)
        for path in sorted((source / 'private/disclosure').glob('*/preparation.json')):
            emit('preparation', path=str(path.relative_to(source)), value=json.loads(path.read_text()))
        saved = json.loads((source / 'private/checkpoint.json').read_text())
        seen_observations = set()
        for record in saved.get('progressive', {}).get('decisions', {}).values():
            for observation in record.get('observations', []):
                if observation['id'] not in seen_observations:
                    emit('judge_observation', value=observation)
                    seen_observations.add(observation['id'])
        checks = source / 'judge-workspace/checks'
        if checks.exists():
            for path in sorted(checks.rglob('*')):
                if '__pycache__' in path.parts or '.pytest_cache' in path.parts:
                    continue
                if path.is_symlink():
                    raise ValueError('Judge checks contain a symbolic link')
                if path.is_file():
                    import base64
                    emit('check_file', path=str(path.relative_to(checks)),
                         data_base64=base64.b64encode(path.read_bytes()).decode())
        experiments = source / 'judge-workspace/experiments'
        for path in sorted(experiments.glob('*')):
            if path.is_file() and not path.is_symlink() and path.suffix in ('.json', '.xml', '.log', '.txt'):
                emit('file', path=str(path.relative_to(source)), text=path.read_text())
    verified = hashlib.sha256()
    with gzip.open(destination, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            verified.update(block)
    if verified.digest() != digest.digest():
        raise ValueError('Review archive verification failed')
    return digest.hexdigest()


def owned_resources(source):
    """Only exact run-recorded containers; active workers prevent cleanup."""
    ids, volumes, networks = set(), set(), set()
    for role in ('user', 'code', 'judge'):
        directory = source / 'private' / role
        active = directory / 'outbox/active.json'
        if active.exists() and json.loads(active.read_text()).get('status') != 'stopped':
            raise ValueError('Cannot compact an active or interrupted worker')
        for relative, field in (('inbox/config.json', 'control_container_id'),
                                ('execution/environment.json', 'container_id')):
            path = directory / relative
            if path.exists():
                row = json.loads(path.read_text())
                identifier = row.get(field)
                if identifier:
                    if not re.fullmatch(r'[a-f0-9]{64}', identifier):
                        raise ValueError('Invalid recorded container identity')
                    ids.add(identifier)
                if field == 'container_id':
                    volumes.update(row.get('snapshot_volumes', {}).values())
                    if row.get('network'):
                        networks.add(row['network'])
    if any(not value.startswith(('session-tool-', 'session-net-')) for value in volumes | networks):
        raise ValueError('Invalid recorded Docker resource identity')
    if not ids:
        return dict(containers=[], volumes=[], networks=[])
    result = subprocess.run(['docker', 'inspect', *sorted(ids)], check=True,
                            capture_output=True, text=True, timeout=30)
    containers = json.loads(result.stdout)
    for row in containers:
        owned = any(m.get('Type') == 'bind' and Path(m['Source'].removeprefix('/host_mnt'))
                    .resolve().is_relative_to(source) for m in row.get('Mounts', []))
        state = row['State']
        if (row['Id'] not in ids or not owned
                or not row['Name'].lstrip('/').startswith(('session-tool-', 'session-oh-'))
                or (state['Running'] and not state.get('Paused'))):
            raise ValueError('Container is active or not owned by this completed run')
        if any(m['Name'] not in volumes for m in row.get('Mounts', []) if m.get('Type') == 'volume'):
            raise ValueError('Container has an unrecorded volume')
    return dict(containers=sorted(ids), volumes=sorted(volumes), networks=sorted(networks))


def compact_completed_run(source, package):
    """Verify the export and preserve evidence before deleting generated copies."""
    source, package = Path(source).resolve(), Path(package).resolve()
    checkpoint = source / 'private/checkpoint.json'
    raw_checkpoint = checkpoint.read_bytes()
    saved = json.loads(raw_checkpoint)
    if (saved['state']['status'] != 'completed' or saved.get('in_flight')
            or saved.get('progressive', {}).get('job') or saved.get('progressive', {}).get('feedback_revision')):
        raise ValueError('Only a completed run without pending work can be compacted')
    if source == package or source in package.parents or package in source.parents:
        raise ValueError('Package must be separate from the run')
    manifest = json.loads((package / 'manifest.json').read_text())
    from .memory_episode import visible_events
    exported = list(read_rows(package / 'dialogue.jsonl'))
    if (manifest.get('schema') != 'memory-episode-v1'
            or manifest['dialogue']['path'] != 'dialogue.jsonl'
            or manifest['snapshot']['path'] != 'snapshot'
            or hashlib.sha256((package / 'dialogue.jsonl').read_bytes()).hexdigest() != manifest['dialogue']['sha256']
            or exported != visible_events(read_rows(source / 'session.jsonl'), source / 'private/code/provider.jsonl')
            or snapshot_hash(source / 'workspace/candidate', export_only=True) != manifest['snapshot']['sha256']
            or snapshot_hash(package / 'snapshot') != manifest['snapshot']['sha256']):
        raise ValueError('Export does not match the source run')
    resources = owned_resources(source)
    review = package / 'private'
    review.mkdir(exist_ok=True, mode=0o700)
    save(review / 'review.json', review_summary(saved))
    trace_hash = write_trace(source, review / 'trace.jsonl.gz')
    # Tool-written files may be named directly in a Judge observation. Keep
    # those cited experiment files, never entire disposable repository copies.
    evidence_text = json.dumps(saved.get('progressive', {}).get('decisions', {}))
    experiment_root = source / 'judge-workspace/experiments'
    if experiment_root.exists():
        for path in sorted(experiment_root.rglob('*')):
            relative = path.relative_to(experiment_root)
            if path.is_file() and '/workspace/experiments/' + relative.as_posix() in evidence_text:
                if path.is_symlink():
                    raise ValueError('Cited experiment evidence contains a symbolic link')
                destination = review / 'experiments' / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    if checkpoint.read_bytes() != raw_checkpoint:
        raise ValueError('Run changed during compaction')
    # Resolve exact generated targets. Never delete the source root or external preparations.
    targets = [source / name for name in ('private', 'workspace', 'judge-workspace', 'user-workspace',
               'session.jsonl', 'dialogue.json', 'dialogue.html', 'index.html') if (source / name).exists()]
    if any(path.is_symlink() or not path.resolve().is_relative_to(source) for path in targets):
        raise ValueError('Cleanup target is outside the run')
    receipt = dict(schema='episode-retention-v1', status='prepared', package=str(package),
                   removed=[str(path.relative_to(source)) for path in targets],
                   trace_sha256=trace_hash, resources=resources, resumable=False)
    save(review / 'retention.json', receipt)
    save(source / 'retention.json', receipt)
    for kind, command in (('containers', ['docker', 'rm', '--force']),
                          ('volumes', ['docker', 'volume', 'rm']),
                          ('networks', ['docker', 'network', 'rm'])):
        for identifier in resources[kind]:
            subprocess.run(command + [identifier], check=True, capture_output=True, timeout=30)
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    receipt['status'] = 'completed'
    save(review / 'retention.json', receipt)
    save(source / 'retention.json', receipt)
    return receipt


def compact_preparation(directory, *, cloned_source=None):
    """Preserve a verified, single archive of finished preparation inputs/outputs."""
    directory = Path(directory).resolve()
    journal = directory / 'provider.jsonl'
    if journal.exists():
        destination = directory / 'provider.jsonl.gz'
        digest = hashlib.sha256()
        with journal.open('rb') as source, gzip.open(destination, 'wb') as target:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
                target.write(block)
        with gzip.open(destination, 'rb') as stream:
            verified = hashlib.file_digest(stream, 'sha256').hexdigest()
        if verified != digest.hexdigest():
            raise ValueError('Preparation archive verification failed')
        journal.unlink()
    if cloned_source is not None:
        cloned_source = Path(cloned_source)
        if cloned_source.is_symlink() or cloned_source.resolve() != directory / 'source':
            raise ValueError('Only the preparation-owned clone can be removed')
        shutil.rmtree(cloned_source)
