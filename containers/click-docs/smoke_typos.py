"""Exercise the installed spelling CLI and hook without network or candidate code."""
import json
import os
import subprocess
import tempfile
from pathlib import Path


def run(*command, expected=0):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != expected:
        raise RuntimeError(f'{command}: exit={result.returncode}\n{result.stdout}\n{result.stderr}')
    return result.stdout.strip()


assert os.getuid() == 1000
version = run('typos', '--version')
with tempfile.TemporaryDirectory(prefix='typos-smoke-') as directory:
    os.chdir(directory)
    source = Path('example.txt')
    source.write_text('Please recieve this message.\n')
    run('typos', '--isolated', 'example.txt', expected=2)
    run('typos', '--isolated', '--write-changes', 'example.txt')
    assert source.read_text() == 'Please receive this message.\n'
    run('typos', '--isolated', 'example.txt')
    run('git', 'init', '-q')
    source.write_text('Please recieve this message.\n')
    run('git', 'add', 'example.txt')
    run('pre-commit', 'run', '--config', '/opt/typos-pre-commit.yaml',
        'typos', '--all-files', expected=1)
    assert source.read_text() == 'Please receive this message.\n'
    run('pre-commit', 'run', '--config', '/opt/typos-pre-commit.yaml',
        'typos', '--all-files')
print(json.dumps(dict(version=version, uid=os.getuid(), detection=True,
                      correction=True, cached_hook=True)))
