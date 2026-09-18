"""Resolve private task specifications before any agent starts."""
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen
from .task_source import requirement_source


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def github(repository, resource):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'session-simulator'}
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with urlopen(Request(f'https://api.github.com/repos/{repository}/{resource}', headers=headers), timeout=60) as response:
        return json.load(response)


def github_name(value):
    match = re.fullmatch(r'(?:https://github.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?', value)
    if not match:
        raise ValueError('repository must be an owner/name or GitHub HTTPS URL')
    return match[1]


def snapshot(repo, revision, destination):
    """Extract regular files only. Reject links instead of following them on host."""
    raw = subprocess.check_output(['git', '-C', str(repo), 'archive', '--format=tar', revision])
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
                raise ValueError('unsafe archive path')
            if member.isdir():
                (destination / path).mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError('symlink/submodule archives need a prepared compatible base image')


def prepare(config, private, include_patch=True):
    source = config['repository']
    local = Path(source).expanduser()
    repository = config.get('github_repository')
    if local.is_dir():
        repo = local.resolve()
    else:
        repository = github_name(source)
        repo = private / 'source'
        subprocess.run(['git', 'clone', '--mirror', f'https://github.com/{repository}.git', str(repo)], check=True, capture_output=True)
    if config.get('swe_chain_evo') is not None:
        from .swe_chain_evo import load_chain
        base, tasks = load_chain(config, repo)
        return repo, base, tasks
    tasks = []
    for specification in config['tasks']:
        document = requirement_source(specification, repository, github)
        reference = specification.get('reference') or (document or {}).get('reference')
        if reference and '/pull/' in str(reference):
            match = re.fullmatch(r'https://github.com/([^/]+/[^/]+)/pull/(\d+)', reference)
            if not match or match[1] != repository:
                raise ValueError('reference PR must belong to the selected repository')
            pr = github(repository, 'pulls/' + match[2])
            if not pr.get('merged_at') or not pr.get('merge_commit_sha'):
                raise ValueError('reference PR must be merged')
            reference = pr['merge_commit_sha']
        if 'commit' in specification:
            reference = specification['commit']
        patch = ''
        base = None
        if reference:
            reference = git(repo, 'rev-parse', '--verify', str(reference) + '^{commit}')
            if document and document['kind'] == 'pull_request' and reference != document['reference']:
                raise ValueError('PR reference differs from its recorded merged commit')
            base = git(repo, 'rev-parse', reference + '^1')
            if include_patch:
                patch = subprocess.check_output(['git', '-C', str(repo), 'diff', base, reference], text=True)
        if document:
            title, body, identifier = (document[key] for key in ('title', 'body', 'identifier'))
        else:
            if not reference:
                raise ValueError('each task needs issue or commit')
            title = git(repo, 'show', '-s', '--format=%s', reference)
            body = 'Infer the observable user requirement from the private reference patch.'
            identifier = reference
        task = dict(kind=document['kind'] if document else 'commit', title=title, body=body, identifier=identifier, reference=reference, patch=patch, base=base)
        if specification.get('requirement_source'):
            linked = requirement_source(specification['requirement_source'], repository, github)
            if not linked:
                raise ValueError('linked requirement must be an issue or PR document')
            task['linked_requirement'] = {key: linked[key] for key in ('title', 'body', 'identifier', 'kind')}
        tasks.append(task)
    if not tasks:
        raise ValueError('at least one task is required')
    base = config.get('base') or tasks[0]['base']
    if not base:
        raise ValueError('base is required when the first task has no explicit reference')
    base = git(repo, 'rev-parse', '--verify', base + '^{commit}')
    if config.get('continuous_commits'):
        previous = base
        for task in tasks:
            if task['kind'] != 'commit' or task['base'] != previous:
                raise ValueError('continuous commits must include every first-parent task in order')
            previous = task['reference']
    return repo, base, tasks
