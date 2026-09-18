"""Local source provenance for public reproduction and private check code."""
import hashlib
import json
import re
import shlex


def record(kind, content, **source):
    value = dict(kind=kind, content=content, **source)
    value['id'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def event_sources(event):
    action = event.get('action') or {}
    source = dict(event_id=event.get('id'), path=action.get('path'))
    if event.get('tool_name') == 'file_editor' and action.get('command') in ('create','insert','str_replace'):
        return [record('private', action[k], **source) for k in ('file_text','new_str') if action.get(k)]
    if event.get('tool_name') != 'terminal' or not action.get('command'):
        return []
    command = action['command']
    source['command'] = command
    # Recognize common literal script forms; do not evaluate shell substitutions.
    heredoc = re.search(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\1(?:\n|$)", command, re.S)
    if heredoc:
        return [record('private', heredoc[2], **source)]
    try:
        words = shlex.split(command)
        for i, word in enumerate(words[:-2]):
            if re.fullmatch(r'(?:.*/)?(?:python[\d.]*|node|ruby|perl|bash|sh)', word) and words[i+1] in ('-c','-e'):
                return [record('private', words[i+2], **source)]
    except ValueError:
        pass
    return [record('unknown', command, **source)] if '\n' in command else []


def collect_sources(existing, events=(), checks_dir=None):
    records = {item['id']: item for item in existing}
    for event in events:
        for item in event_sources(event):
            records[item['id']] = item
    if checks_dir and checks_dir.exists():
        root = checks_dir.resolve()
        for path in checks_dir.rglob('*'):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root) or path.stat().st_size >= 1024*1024:
                continue
            try:
                item = record('private',path.read_text(),path=str(path.relative_to(root)))
                records[item['id']] = item
            except UnicodeDecodeError:
                pass
    return list(records.values())


def source_check(text, records, requirement, history):
    public = [record('public_requirement',str(requirement.get(k,'')),field=k) for k in ('title','body')]
    public += [record('code_known',str(m.get('text','')),message_id=m.get('id')) for m in history]
    public_lines = {line.strip() for p in public for line in p['content'].splitlines() if line.strip()}
    matches, unknown, exempt = [], [], []
    for item in records:
        for line in item['content'].splitlines():
            line = line.strip()
            if not line or line not in text:
                continue
            if line in public_lines:
                exempt.append(dict(source_id=item['id'],text=line))
                continue
            # Generic syntax alone is not evidence of private source disclosure.
            if (len(line) < 16 and not line.startswith('assert ')) or re.match(r'^(?:from\s+\S+\s+import\s|import\s)',line):
                continue
            hit = dict(source_id=item['id'],event_id=item.get('event_id'),path=item.get('path'),text=line)
            if item['kind'] == 'unknown':
                unknown.append(hit)
            else:
                matches.append(hit)
    return dict(matches=matches,unknown=unknown,public_overlap=exempt,public_sources=public)
