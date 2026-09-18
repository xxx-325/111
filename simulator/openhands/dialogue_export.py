"""Export only original public User messages and Code final replies."""
import argparse
import html
import json
from pathlib import Path

from ..episode import save


def dialogue_messages(events):
    seen = set()
    result = []
    for event in events:
        identifier = event['id']
        if identifier in seen:
            continue
        seen.add(identifier)
        if event.get('kind') == 'user':
            role = 'user'
        elif event.get('kind') == 'assistant' and event.get('phase') == 'final':
            role = 'assistant'
        else:
            continue
        text = event.get('text')
        if isinstance(text, str) and text.strip():
            result.append(dict(role=role, content=text))
    return result


def export_dialogue(root):
    root = Path(root)
    source = root / 'session.jsonl'
    events = [json.loads(line) for line in source.read_text().splitlines() if line.strip()] if source.exists() else []
    messages = dialogue_messages(events)
    save(root / 'dialogue.json', messages)
    cards = ''.join(
        '<article class="' + m['role'] + '"><h2>' +
        ('User' if m['role'] == 'user' else 'Code Agent') +
        '</h2><pre>' + html.escape(m['content']) + '</pre></article>' for m in messages)
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>User 与 Code Agent 对话</title><style>
body{margin:0;background:#f5f6f8;color:#202631;font:16px/1.7 system-ui,sans-serif}
main{max-width:850px;margin:32px auto;padding:0 18px}article{padding:20px 24px;
margin:20px 0;border:1px solid #e0e4ea;border-radius:12px;background:white}
article.user{border-left:4px solid #5486bd}h2{font-size:14px;margin:0 0 12px;color:#536579}
pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
</style><main>''' + cards + '</main></html>'
    (root / 'dialogue.html').write_text(document, encoding='utf-8')
    return messages


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    export_dialogue(parser.parse_args().run)
