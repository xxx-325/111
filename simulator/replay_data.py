"""Prepare bounded, chronological evaluation windows from local native transcripts."""
import hashlib
import argparse
import json
import re
from pathlib import Path


def clean(text):
    if re.search(r'## My request(?: for Codex)?:', text):
        text = re.split(r'## My request(?: for Codex)?:', text)[-1]
    elif text.lstrip().startswith(('<recommended_plugins', '<environment_context', '# AGENTS.md', '<INSTRUCTIONS>')):
        return ''
    if text.lstrip().startswith(('<codex_internal_context', '<heartbeat>', '<turn_aborted>', '<skill>', '<command-name>', '<local-command-stdout>')):
        return ''
    for tag in ('in-app-browser-context', 'system-reminder', 'image'):
        text = re.sub(r'<' + tag + r'\b[^>]*>.*?</' + tag + '>', '', text, flags=re.S)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    text = re.split(r'\n\[route_chain\]', text)[0]
    text = re.sub(r'::[\w-]+\{[^}]*\}', '', text)
    # Preserve repository-relative paths without user/machine roots.
    text = re.sub(r'D:[/\\]Code[/\\]myagent-retail-v3-baseline(?:-archived)?[/\\]?', 'repo/', text)
    text = re.sub(r'/Users/[^/\s]+(?:/[^\s)<>`]+)?', '[local-path]', text)
    text = re.sub(r'\b(?:sk-[\w-]{12,}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})\b', '[redacted]', text)
    return text.strip()


def read_messages(path):
    messages = []
    for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
        row = json.loads(line)
        payload = row.get('payload', {})
        if row.get('type') != 'response_item' or payload.get('type') != 'message' or payload.get('role') not in ('user', 'assistant'):
            continue
        text = clean('\n'.join(block.get('text', '') for block in payload.get('content', []) if block.get('type') in ('input_text', 'output_text', 'text')))
        if text:
            messages.append(dict(role=payload['role'], text=text, line=line_number))
    return messages


def prepare(root, selections, examples_path):
    """Gold replies and selection metadata are separate from model inputs."""
    excluded = {e['source'].split(':')[0] for e in json.loads(Path(examples_path).read_text())}
    cases, references = [], []
    for spec in selections:
        prefix, cutoff = spec['session'], spec['before_user_line']
        if any(prefix.startswith(p) or p.startswith(prefix) for p in excluded):
            raise ValueError('test session overlaps expression examples')
        matches = [p for p in Path(root).rglob('rollout.jsonl') if p.parent.name.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError('source session must resolve uniquely')
        path = matches[0]
        messages = read_messages(path)
        gold = next(m for m in messages if m['line'] == cutoff and m['role'] == 'user')
        past = [m for m in messages if m['line'] < cutoff]
        if not past or past[-1]['role'] != 'assistant':
            raise ValueError('test must end immediately after an assistant reply')
        # Recent complete messages; add the original goal as an explicitly non-contiguous anchor.
        window = past[-6:]
        first = next(m for m in past if m['role'] == 'user')
        if first not in window:
            window = [first] + window
        latest_user = next(m for m in reversed(past) if m['role'] == 'user')
        if latest_user not in window:
            window.append(latest_user)
            window.sort(key=lambda m: m['line'])
        if sum(len(m['text']) for m in window) > 20000:
            raise ValueError('window too long; manually select a smaller test')
        case_id = spec['id']
        history = []
        for index, message in enumerate(window):
            if index and past.index(message) > past.index(window[index - 1]) + 1:
                history.append(dict(role='context_note', text='Earlier messages are omitted here; do not assume they contain additional authorization or facts.'))
            history.append({k: message[k] for k in ('role', 'text')})
        cases.append(dict(id=case_id, history=history))
        references.append(dict(id=case_id, category=spec['category'], source=prefix,
                               source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                               input_lines=[m['line'] for m in window], cutoff=cutoff,
                               human_reply=gold['text'], context_policy='original goal and latest user request plus latest six messages; explicit gap markers'))
    return cases, references


def main():
    from .episode import save
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--selections', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if (output / 'cases.json').exists() or (output / 'references.json').exists():
        raise FileExistsError('use a new study directory')
    cases, refs = prepare(args.root, json.loads(Path(args.selections).read_text()), Path(__file__).with_name('style_examples.json'))
    save(output / 'cases.json', cases)
    save(output / 'references.json', refs)


if __name__ == '__main__': main()
