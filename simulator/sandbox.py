"""Offline Docker tools. No host commands or provider credentials in containers."""
import json
import subprocess
import uuid


class Sandbox:
    def __init__(self, workspace, image, timeout=120):
        self.workspace = workspace.resolve()
        self.image = image
        self.timeout = timeout

    def execute(self, command):
        name = 'session-tool-' + uuid.uuid4().hex
        args = ['docker', 'run', '--rm', '--name', name, '--network=none',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                '--pids-limit=128', '--memory=2g', '--cpus=2',
                '--tmpfs', '/tmp:rw,nosuid,size=256m',
                '--mount', f'type=bind,source={self.workspace},target=/workspace',
                '--workdir=/workspace', '--env', 'HOME=/tmp',
                '--entrypoint=/bin/sh', self.image, '-lc', command]
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=self.timeout)
            return dict(exit_code=result.returncode, stdout=result.stdout[-24000:], stderr=result.stderr[-8000:])
        except subprocess.TimeoutExpired:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
            return dict(exit_code=124, stdout='', stderr='Command timed out; container terminated.')


TOOLS = [{'type': 'function', 'function': {
    'name': 'shell', 'description': 'Execute a shell command in the current offline project sandbox. Read, edit files and run tests here.',
    'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command'], 'additionalProperties': False}
}}]


def tool_result(sandbox, call):
    if call['function']['name'] != 'shell':
        raise ValueError('unknown tool')
    arguments = json.loads(call['function']['arguments'])
    if not isinstance(arguments.get('command'), str):
        raise ValueError('shell command must be a string')
    return sandbox.execute(arguments['command'])
