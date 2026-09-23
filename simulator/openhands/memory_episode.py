"""Export a stopped run as model-visible history and an independent snapshot."""
import argparse
import hashlib
import html
import json
import shutil
from pathlib import Path

from ..episode import save

SCHEMA = 'memory-episode-v1'
DIALOGUE_SCHEMA = 'model-visible-dialogue-v1'
HASH_ALGORITHM = 'relative-path-executable-content-v1'
IGNORED = {'.git', '__pycache__', '.pytest_cache', '.venv', '.mypy_cache', '.ruff_cache'}


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(c, dict) and c.get('type') == 'text'
                                       and isinstance(c.get('text'), str) for c in value):
        return '\n'.join(c['text'] for c in value)
    raise ValueError('Only recorded text content is supported by this export')


def read_rows(path):
    with Path(path).open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def delivered_tools(journal):
    """Use the first provider-bound view, before later context condensation."""
    calls, results = {}, {}
    for record in read_rows(journal):
        if record.get('kind') != 'request':
            continue
        for message in record.get('input', {}).get('messages', []):
            if message.get('role') == 'assistant':
                for call in message.get('tool_calls') or []:
                    function = call.get('function', {})
                    calls.setdefault(call['id'], (function.get('name'),
                                                   json.loads(function['arguments'])))
            elif message.get('role') == 'tool':
                results.setdefault(message['tool_call_id'], text_content(message.get('content')))
    return calls, results


def visible_events(events, journal):
    """Raw SDK metadata is never a substitute for a delivered tool response."""
    calls, results = delivered_tools(journal)
    seen, pending, output = set(), {}, []
    for event in events:
        identifier = event['id']
        if identifier in seen:
            raise ValueError('Duplicate public event id')
        seen.add(identifier)
        kind = event.get('kind')
        item = dict(schema=DIALOGUE_SCHEMA, id=identifier, kind=kind,
                    sequence=len(output) + 1, timestamp=event['timestamp'])
        if kind in ('user', 'assistant'):
            if kind == 'assistant' and event.get('phase') not in (None, 'final', 'commentary'):
                continue
            if event.get('delivery') == 'closing_not_forwarded_to_code':
                continue
            item['text'] = text_content(event.get('text'))
            if kind == 'assistant':
                item['phase'] = event.get('phase', 'final')
        elif kind in ('tool_call', 'tool_result'):
            call_id, name = event.get('call_id'), event.get('tool_name')
            if not call_id or not name or name in ('think', 'finish'):
                raise ValueError('Invalid public tool identity')
            item.update(call_id=call_id, tool_name=name)
            if kind == 'tool_call':
                if call_id in pending or call_id not in calls or calls[call_id][0] != name:
                    raise ValueError('Tool call has no matching provider-bound record')
                item['action'] = calls[call_id][1]
                pending[call_id] = name
            else:
                if pending.pop(call_id, None) != name or call_id not in results:
                    raise ValueError('Tool result is unmatched or was never sent to Code')
                item['text'] = results[call_id]
        else:
            raise ValueError('Unknown public event kind')
        output.append(item)
    if pending:
        raise ValueError('Public history ends with unfinished tool calls')
    if not output:
        raise ValueError('No public history to export')
    return output


def snapshot_hash(root, *, export_only=False):
    """Same path/executable/content hash as dialogue-benchmark task artifacts."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Snapshot root must be an existing directory')
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        parts = path.relative_to(root).parts
        ignored = IGNORED if export_only else {'.git', '__pycache__', '.pytest_cache'}
        if any(x in ignored for x in parts) or (export_only and path.suffix == '.pyc'):
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Snapshot contains a link or special file')
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b'\0')
            digest.update(b'x' if path.stat().st_mode & 0o111 else b'-')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def control_config(config):
    result = {key: config[key] for key in ('image', 'execution_image', 'execution_backend')}
    allowed = ('model', 'base_url', 'key_env', 'temperature', 'candidate_pythonpath',
               'max_input_tokens', 'max_output_tokens', 'request_timeout')
    for role in ('code', 'judge'):
        result[role] = {key: config[role][key] for key in allowed if key in config[role]}
    return result


def render_events(events):
    cards = []
    for event in events:
        text = (json.dumps(event['action'], ensure_ascii=False, indent=2)
                if event['kind'] == 'tool_call' else event['text'])
        title = event.get('tool_name', event['kind'])
        body = '<pre>' + html.escape(text) + '</pre>'
        if event['kind'].startswith('tool_'):
            body = '<details><summary>' + html.escape(title) + '</summary>' + body + '</details>'
        else:
            body = '<h2>' + html.escape(title) + '</h2>' + body
        cards.append('<article>' + body + '</article>')
    return ('<!doctype html><meta charset="utf-8"><title>Development dialogue</title>'
            '<style>body{max-width:1000px;margin:30px auto;font:16px/1.6 system-ui}'
            'article{padding:16px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;'
            'overflow-wrap:anywhere}h2,summary{font-size:15px}</style>' + ''.join(cards))


def scenario_navigation(checkpoint, events):
    """Private source navigation, never evidence that a hidden fact was spoken."""
    starts = {message['id']: message['task_id']
              for message in checkpoint.get('state', {}).get('messages', [])}
    by_task, active = {}, None
    for event in events:
        active = starts.get(event['id'], active)
        if active:
            by_task.setdefault(active, []).append(event['id'])
    rows = []
    for index, task in enumerate(checkpoint.get('tasks', []), 1):
        scenario = task.get('scenario')
        if not scenario:
            continue
        rows.append(dict(task_id=f'task-{index}', source_commit=scenario['source_commit'],
                         original=scenario['original'],
                         changed_paths=[edit['path'] for edit in scenario['repository_edits']],
                         public_event_ids=by_task.get(f'task-{index}', [])))
    return dict(schema='scenario-navigation-v1', tasks=rows)


def export_episode(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    checkpoint = json.loads((source / 'private/checkpoint.json').read_text())
    if checkpoint.get('state', {}).get('status') == 'running' or checkpoint.get('in_flight'):
        raise ValueError('Export requires a stopped run without an in-flight turn')
    if output == source or source in output.parents or output in source.parents:
        raise ValueError('Export must be separate from the original run')
    events = visible_events(read_rows(source / 'session.jsonl'), source / 'private/code/provider.jsonl')
    config = control_config(checkpoint['config'])
    candidate = source / 'workspace/candidate'
    before = snapshot_hash(candidate, export_only=True)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copytree(candidate, output / 'snapshot', ignore=shutil.ignore_patterns(*IGNORED, '*.pyc'))
    if (snapshot_hash(candidate, export_only=True) != before
            or snapshot_hash(output / 'snapshot') != before):
        raise ValueError('Candidate changed during export')
    raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in events).encode()
    (output / 'dialogue.jsonl').write_bytes(raw)
    save(output / 'dialogue.json', dict(schema=DIALOGUE_SCHEMA, events=events))
    (output / 'dialogue.html').write_text(render_events(events))
    save(output / 'control-config.json', config)
    manifest = dict(schema=SCHEMA,
                    dialogue=dict(path='dialogue.jsonl', sha256=hashlib.sha256(raw).hexdigest(),
                                  cutoff_event_id=events[-1]['id']),
                    snapshot=dict(path='snapshot', sha256=snapshot_hash(output / 'snapshot'),
                                  hash_algorithm=HASH_ALGORITHM),
                    control_config=dict(path='control-config.json'))
    save(output / 'manifest.json', manifest)
    navigation = scenario_navigation(checkpoint, events)
    if navigation['tasks']:
        (output / 'private').mkdir(mode=0o700)
        save(output / 'private/scenario-index.json', navigation)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compact-source', action='store_true',
                        help='After verified export, discard completed-run temporary state; no resume afterward')
    args = parser.parse_args()
    export_episode(args.source, args.output)
    if args.compact_source:
        from .retention import compact_completed_run
        compact_completed_run(args.source, args.output)
    print(args.output / 'manifest.json')


if __name__ == '__main__':
    main()
