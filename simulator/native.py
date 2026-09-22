"""Native CLI adapters with offline containers and a fixed-provider API mailbox."""
import base64
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def decode_events(kind, lines, emit):
    session = None
    final = None
    success = False
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        if kind == 'codex':
            if event.get('type') == 'thread.started':
                session = event['thread_id']
            elif event.get('type') == 'item.completed':
                item = event['item']
                if item.get('type') == 'agent_message':
                    final = item['text']
                    emit('assistant', {'text': final, 'phase': 'message'})
                elif item.get('type') != 'reasoning':
                    emit('native_tool', {'client': kind, 'event': event})
            elif event.get('type') == 'turn.completed':
                success = True
            elif event.get('type') in ('error', 'turn.failed'):
                raise RuntimeError('Codex turn failed; inspect native logs')
        else:
            session = event.get('session_id', session)
            if event.get('type') in ('assistant', 'user'):
                for block in event.get('message', {}).get('content', []):
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'text':
                        emit('assistant', {'text': block['text'], 'phase': 'message'})
                    elif block.get('type') in ('tool_use', 'tool_result'):
                        emit('native_tool', {'client': kind, 'event': block})
            elif event.get('type') == 'result':
                if event.get('is_error'):
                    raise RuntimeError('Claude turn failed; inspect native logs')
                final = event.get('result')
                success = event.get('subtype') == 'success'
    if not final or not success:
        raise ValueError('native stream has no successful final result')
    return session, final


class NativeAgent:
    def __init__(self, config, state_dir, system, history=None):
        self.config, self.directory, self.system = config, Path(state_dir), system
        self.history = history if history is not None else []
        self.deadline = None
        self.directory.mkdir(parents=True, exist_ok=True)

    def relay(self, mailbox, stop):
        allowed = {'/v1/messages', '/v1/messages/count_tokens'} if self.config['adapter'] == 'claude' else {'/v1/responses', '/v1/responses/compact'}
        while not stop.is_set():
            for path in mailbox.glob('*.request'):
                if not re.fullmatch(r'[a-f0-9]{32}\.request', path.name):
                    continue
                response_path = path.with_suffix('.response')
                if response_path.exists():
                    continue
                result = dict(status=502, content_type='application/json', body=base64.b64encode(b'{"error":"provider relay failed"}').decode())
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd) as handle:
                        packet = json.load(handle)
                    clean_path = packet['path'].split('?', 1)[0]
                    if clean_path not in allowed:
                        raise ValueError('relay path denied')
                    body = json.loads(base64.b64decode(packet['body']))
                    local_types = {'function', 'custom'} if self.config['adapter'] == 'codex' else {None, 'custom'}
                    if any(tool.get('type') not in local_types for tool in body.get('tools', [])):
                        raise ValueError('server-side tools are not allowed by the offline relay')
                    body['model'] = self.config['model']
                    key = os.environ[self.config['key_env']]
                    headers = {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + key,
                        'User-Agent': 'curl/8.0',
                    }
                    if self.config['adapter'] == 'claude':
                        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
                    suffix = packet['path'][3:]
                    req = Request(self.config['base_url'].rstrip('/') + suffix, data=json.dumps(body).encode(), headers=headers)
                    with urlopen(req, timeout=180) as response:
                        result = dict(status=response.status, content_type=response.headers.get('Content-Type','application/json'), body=base64.b64encode(response.read()).decode())
                except Exception:
                    pass  # Never expose upstream headers or credentials to the CLI.
                temporary = response_path.with_suffix('.out')
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    json.dump(result, handle)
                temporary.replace(response_path)
            stop.wait(.05)

    def turn(self, prompt, sandbox, emit, budget=30):
        kind = self.config['adapter']
        mailbox = self.directory / ('mailbox-' + uuid.uuid4().hex)
        mailbox.mkdir(mode=0o700)
        home = self.directory / 'home'
        home.mkdir(exist_ok=True)
        session = next((x.get('native_session') for x in reversed(self.history) if x.get('native_session')), None)
        if kind == 'codex':
            args = ['codex', 'exec'] + (['resume', session] if session else [])
            args += ['--json', '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox',
                     '-c', 'model_provider="isolated"', '-c', 'model_providers.isolated.name="isolated"',
                     '-c', 'model_providers.isolated.base_url="http://127.0.0.1:8789/v1"',
                     '-c', 'model_providers.isolated.wire_api="responses"',
                     '-c', 'web_search="disabled"',
                     '-c', 'model_providers.isolated.env_key="RELAY_TOKEN"', '-m', self.config['model'], '-']
        elif kind == 'claude':
            args = ['claude', '-p', '--output-format', 'stream-json', '--verbose', '--model', self.config['model'],
                    '--allowedTools', 'Bash,Read,Edit,Write,Glob,Grep', '--max-turns', str(budget)]
            if session:
                args += ['--resume', session]
        else:
            raise ValueError('unknown native adapter')
        name = 'session-native-' + uuid.uuid4().hex
        bridge = Path(__file__).with_name('native_http.py').resolve()
        launch = 'python3 /relay.py & exec ' + shlex.join(args)
        command = ['docker', 'run', '--rm', '-i', '--name', name, '--network=none', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges', '--pids-limit=256', '--memory=4g', '--cpus=2', '--read-only',
                   '--tmpfs', '/tmp:rw,size=256m', '--mount', f'type=bind,source={sandbox.workspace},target=/workspace',
                   '--mount', f'type=bind,source={home.resolve()},target=/agent-home',
                   '--mount', f'type=bind,source={mailbox.resolve()},target=/mailbox',
                   '--mount', f'type=bind,source={bridge},target=/relay.py,readonly',
                   '--env', 'HOME=/agent-home', '--env', 'CODEX_HOME=/agent-home/codex',
                   '--env', 'CLAUDE_CONFIG_DIR=/agent-home/claude', '--env', 'RELAY_TOKEN=local',
                   '--env', 'ANTHROPIC_AUTH_TOKEN=local', '--env', 'ANTHROPIC_BASE_URL=http://127.0.0.1:8789',
                   '--workdir=/workspace', '--entrypoint=/bin/sh', sandbox.image, '-lc', launch]
        stop = threading.Event()
        thread = threading.Thread(target=self.relay, args=(mailbox, stop), daemon=True)
        thread.start()
        try:
            text = (self.system + '\n\n' if not session else '') + prompt
            timeout = self.config.get('timeout',900)
            if self.deadline is not None:
                timeout = min(timeout, max(.1, self.deadline-time.monotonic()))
            result = subprocess.run(command, input=text, text=True, capture_output=True, timeout=timeout)
            for filename, content in [('stdout.jsonl', result.stdout), ('stderr.txt', result.stderr)]:
                fd = os.open(mailbox / filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    handle.write(content)
            if result.returncode:
                raise RuntimeError('native CLI failed; see private native logs')
            next_session, final = decode_events(kind, result.stdout.splitlines(), emit)
            self.history.append(dict(native_session=next_session or session, prompt=prompt, reply=final))
            return final
        finally:
            stop.set()
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
            thread.join(timeout=1)
