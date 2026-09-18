"""Load a GitHub requirement document without treating a PR as an issue."""
import json
import re
from pathlib import Path


def requirement_source(specification, repository, fetch):
    kinds = [name for name in ('issue', 'pull_request', 'commit') if name in specification]
    if len(kinds) != 1:
        raise ValueError('each task requires exactly one issue, pull_request or commit source')
    kind = kinds[0]
    if kind == 'commit':
        return None
    route = 'issues' if kind == 'issue' else 'pull'
    value = str(specification[kind])
    if value.startswith('https://'):
        match = re.fullmatch(r'https://github.com/([^/]+/[^/]+)/' + route + r'/(\d+)', value)
        if not match or match[1] != repository:
            raise ValueError('requirement source must belong to the selected repository')
        value = match[2]
    if not repository or not value.isdigit():
        raise ValueError('requirement source needs github_repository and a number or URL')
    cache = specification.get('issue_file' if kind == 'issue' else 'pull_request_file')
    data = json.loads(Path(cache).read_text()) if cache else fetch(
        repository, ('issues/' if kind == 'issue' else 'pulls/') + value)
    expected = f'https://github.com/{repository}/{route}/{value}'
    if data.get('html_url') != expected or str(data.get('number')) != value:
        raise ValueError('requirement document identity differs from configured source')
    if kind == 'issue' and 'pull_request' in data:
        raise ValueError('expected an issue, not a pull request')
    if kind == 'pull_request' and (
            not data.get('merged_at') or not isinstance(data.get('merge_commit_sha'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', data['merge_commit_sha'])):
        raise ValueError('pull_request requirement must have a confirmed merged commit')
    if not isinstance(data.get('title'), str) or not data['title'].strip():
        raise ValueError('requirement document has no title')
    body = data.get('body') or ''
    if not isinstance(body, str):
        raise ValueError('requirement document body must be text')
    return dict(kind=kind, title=data['title'], body=body, identifier=expected,
                reference=data.get('merge_commit_sha') if kind == 'pull_request' else None)
