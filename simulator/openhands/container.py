"""Persistent offline container and SDK worker lifecycle, not an agent loop."""
import json
import shutil
import subprocess
import time
import uuid
import posixpath
from pathlib import Path, PurePosixPath

from ..episode import save
from .relay import Relay
from .sandbox import ExecutionSandbox, inspect_container


def candidate_pythonpath(value):
    """Validate one container path without accepting host paths or path lists."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or '\x00' in value or ':' in value:
        raise ValueError('candidate_pythonpath must be a non-empty container path')
    normalized = posixpath.normpath(value)
    path = PurePosixPath(normalized)
    root = PurePosixPath('/workspace/candidate')
    if normalized != value or not path.is_absolute() or (path != root and root not in path.parents):
        raise ValueError('candidate_pythonpath must be a normalized path inside /workspace/candidate')
    return value


class SDKContainer:
    def __init__(self, directory, workspace, config, image, role, system, deadline,
                 control=None, browser=False, condenser_max_size=120, budget=None, neutral_tools=True,
                 reference=None, readonly_candidate=False, pause_check=None):
        self.directory, self.workspace = Path(directory).resolve(), Path(workspace).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.deadline, self.process = deadline, None
        self.pause_check = pause_check
        self.backend = config.get('execution_backend', 'ssh_sandbox')
        if self.backend not in ('ssh_sandbox', 'shared_diagnostic'):
            raise ValueError('unknown execution backend; no local fallback')
        if self.backend == 'ssh_sandbox' and browser:
            raise ValueError('browser has no isolated executor; refusing local fallback')
        for name in ('inbox', 'outbox', 'sdk', 'mailbox', 'runtime/simulator/openhands'):
            (self.directory / name).mkdir(parents=True, exist_ok=True)
        runtime = self.directory / 'runtime/simulator'
        # Mount only runtime implementation, never the project or research corpus.
        for name in ('__init__.py', 'worker.py', 'control_tools.py', 'tool_wording.py', 'judge_tools.py'):
            shutil.copy2(Path(__file__).parent / name, runtime / 'openhands' / name)
        if self.backend == 'ssh_sandbox':
            for name in ('remote_tools.py','remote_safety.py'):
                shutil.copy2(Path(__file__).parent / name, runtime / 'openhands' / name)
        shutil.copy2(Path(__file__).parents[1] / '__init__.py', runtime / '__init__.py')
        shutil.copy2(Path(__file__).parents[1] / 'state_machine.py', runtime / 'state_machine.py')
        shutil.copy2(Path(__file__).parents[1] / 'native_http.py', runtime / 'native_http.py')
        requested_pythonpath = candidate_pythonpath(config.get('candidate_pythonpath'))
        cfg_path = self.directory / 'inbox/config.json'
        if not cfg_path.exists():
            save(cfg_path, dict(model=config['model'], role=role, system=system, control=bool(control),
                                browser=browser, condenser_max_size=condenser_max_size, neutral_tools=neutral_tools,
                                conversation_id=str(uuid.uuid4()), temperature=config.get('temperature', .3),
                                candidate_pythonpath=requested_pythonpath, execution_backend=self.backend,
                                execution_image=config.get('execution_image')))
        self.relay = Relay(config, self.directory / 'mailbox', self.directory / 'provider.jsonl', control, deadline, budget, role)
        cfg = json.loads(cfg_path.read_text())
        persisted_pythonpath = candidate_pythonpath(cfg.get('candidate_pythonpath'))
        if persisted_pythonpath != requested_pythonpath:
            raise ValueError('candidate_pythonpath differs from the persisted SDK container configuration')
        if cfg.get('execution_backend') != self.backend or cfg.get('execution_image') != config.get('execution_image'):
            raise ValueError('execution configuration differs; old checkpoints are not migrated')
        suffix = cfg.get('container_suffix', '')
        if suffix and (len(suffix) != 12 or any(c not in '0123456789abcdef' for c in suffix)):
            raise ValueError('Invalid explicit imported-container suffix')
        self.name = 'session-oh-' + cfg['conversation_id'] + ('-' + suffix if suffix else '')
        self.image = image
        self.reference = Path(reference).resolve() if reference else None
        self.readonly_candidate = readonly_candidate
        self.sandbox = None
        if self.backend == 'ssh_sandbox' and role != 'user':
            sandbox_identity = cfg['conversation_id'] + ('-' + suffix if suffix else '')
            self.sandbox = ExecutionSandbox(self.directory, self.workspace, config.get('execution_image'),
                role, sandbox_identity, self.reference)
        self.control_workspace = self.directory / 'control-workspace'
        (self.control_workspace / 'candidate').mkdir(parents=True, exist_ok=True)

    def start(self, resume=False):
        if (self.directory/'sdk/remote-tools/fatal.json').exists():
            raise RuntimeError('uncertain tool execution retained; automatic resume denied')
        cfg_path = self.directory / 'inbox/config.json'
        cfg = json.loads(cfg_path.read_text())
        if self.sandbox:
            cfg['remote_tools'] = self.sandbox.worker_config()
            save(cfg_path, cfg)
        existing = subprocess.run(['docker', 'inspect', self.name], capture_output=True)
        if existing.returncode == 0:
            active = self.directory / 'outbox/active.json'
            if not resume or (active.exists() and json.loads(active.read_text()).get('status') != 'stopped'):
                raise RuntimeError('Existing container retained; inspect it before resuming an uncertain worker')
            details = json.loads(existing.stdout)[0]
            if details['State']['Running']:
                raise RuntimeError('Existing SDK container is still running; concurrent resume denied')
        args = ['docker', 'run', '-d', '--name', self.name, '--network', self.sandbox.network if self.sandbox else 'none',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '512',
                '--memory', '3g', '--cpus', '2', '--shm-size', '256m']
        control_mounts = [
            (self.control_workspace if self.backend == 'ssh_sandbox' else self.workspace, '/workspace', False),
            (self.directory / 'runtime', '/opt/runtime', True),
            (self.directory / 'inbox', '/inbox', True),
            (self.directory / 'outbox', '/outbox', False),
            (self.directory / 'sdk', '/sdk', False),
            (self.directory / 'mailbox', '/mailbox', False),
        ]
        if self.sandbox:
            control_mounts.append((self.sandbox.client, '/transport', True))
        for source, target, readonly in control_mounts:
            args += ['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if readonly else '')]
        if self.readonly_candidate and self.backend != 'ssh_sandbox':
            args += ['--mount', f'type=bind,src={self.workspace / "candidate"},dst=/workspace/candidate,readonly']
        if self.reference and self.backend != 'ssh_sandbox':
            args += ['--mount', f'type=bind,src={self.reference},dst=/reference,readonly']
        args += [self.image]
        if existing.returncode:
            subprocess.run(args, check=True, capture_output=True)
            cfg['control_container_id'] = inspect_container(self.name)['Id']
            save(cfg_path, cfg)
        else:
            details = json.loads(existing.stdout)[0]
            if details['Image'] != self.image or details['Id'] != cfg.get('control_container_id'):
                raise RuntimeError('control image identity changed')
            expected_mounts = {(str(p), t, not r) for p, t, r in control_mounts}
            host = details['HostConfig']
            if self.backend == 'ssh_sandbox' and (
                    {(m['Source'],m['Destination'],m['RW']) for m in details['Mounts']} != expected_mounts or
                    host.get('NetworkMode') != (self.sandbox.network if self.sandbox else 'none') or
                    host.get('Privileged') or host.get('PidMode') or host.get('CapAdd') or
                    set(host.get('CapDrop') or []) != {'ALL'} or
                    'no-new-privileges' not in host.get('SecurityOpt', [])):
                raise RuntimeError('control execution boundary changed; refusing resume')
            subprocess.run(['docker', 'start', self.name], check=True, capture_output=True)
        ready = self.directory / 'outbox/ready.json'
        if ready.exists():
            ready.unlink()
        if self.sandbox:
            self.sandbox.unpause()
        self.relay.start()
        self.log = (self.directory / 'worker.log').open('a')
        self.process = subprocess.Popen(['docker', 'exec', self.name, 'python', '-m', 'simulator.openhands.worker'],
                                        stdout=self.log, stderr=self.log)
        self.wait(ready)

    def wait(self, path):
        if self.pause_check:
            self.pause_check()
        while not path.exists():
            if self.pause_check:
                self.pause_check()
            if self.process and self.process.poll() is not None:
                raise RuntimeError('SDK worker exited; inspect private worker.log')
            if time.monotonic() >= self.deadline:
                self.pause()
                raise TimeoutError('Time budget exhausted; container paused, uncertain work not retried')
            time.sleep(.15)
        if self.pause_check:
            self.pause_check()
        return json.loads(path.read_text())

    def turn(
        self,
        message=None,
        condense=False,
        command_id=None,
        resume_pending_control=None,
    ):
        identifier = command_id or f'turn-{time.time_ns()}'
        if not identifier.startswith('turn-') or '/' in identifier:
            raise ValueError('invalid command id')
        path = self.directory / 'inbox' / (identifier + '.json')
        if not path.exists():
            save(
                path,
                dict(
                    message=message,
                    condense=condense,
                    resume_pending_control=resume_pending_control,
                ),
            )
        result = self.wait(self.directory / 'outbox' / path.name)
        if result.get('status') == 'error':
            raise RuntimeError('SDK turn failed; original error retained in ' + str(path.name))
        return result

    def events(self):
        path = self.directory / 'outbox/events.jsonl'
        if not path.exists():
            return []
        text = path.read_text()
        # A concurrently written trailing fragment is not a complete event yet.
        return [json.loads(line) for line in text.split('\n')[:-1] if line]

    def pause(self):
        if self.sandbox:
            self.sandbox.pause()
        if not inspect_container(self.name)['State']['Paused']:
            subprocess.run(['docker', 'pause', self.name], check=True, capture_output=True)

    def unpause(self):
        if inspect_container(self.name)['State']['Paused']:
            subprocess.run(['docker', 'unpause', self.name], check=True, capture_output=True)
        if self.sandbox:
            self.sandbox.unpause()

    def close(self):
        self.relay.close()
        if self.sandbox and self.sandbox.record.exists():
            record = json.loads(self.sandbox.record.read_text())
            if record.get('status') == 'ready':
                self.sandbox.pause()
        if self.process:
            inspected = subprocess.run(['docker', 'inspect', self.name], capture_output=True)
            paused = inspected.returncode == 0 and json.loads(inspected.stdout)[0]['State']['Paused']
            if paused:
                # Preserve uncertain commands frozen in place; never unpause to stop them.
                self.process.terminate()
            else:
                subprocess.run(['docker', 'stop', '-t', '3', self.name], capture_output=True)
            self.process.wait(timeout=10)
            self.log.close()
        # Containers and mounted state remain available for inspection.
