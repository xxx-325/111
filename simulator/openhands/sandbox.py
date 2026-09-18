"""Host-owned Docker execution boundary; no model command is executed here."""
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from ..episode import save


VERSION = 'ssh-sandbox-v1'
CAPABILITIES = ['SETUID', 'SETGID', 'SYS_CHROOT']
NETWORK_BLOCK = (10 << 24) | (240 << 16)
NETWORK_PREFIX = 28
NETWORK_COUNT = 1 << (NETWORK_PREFIX - 16)


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def create_isolated_network(name, identity):
    """Create a small explicit subnet without consuming Docker's default pools."""
    seed = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:2], 'big') % NETWORK_COUNT
    for offset in range(NETWORK_COUNT):
        index = (seed + offset) % NETWORK_COUNT
        address = NETWORK_BLOCK + index * (1 << (32 - NETWORK_PREFIX))
        subnet = '.'.join(str((address >> shift) & 255) for shift in (24, 16, 8, 0))
        cidr = f'{subnet}/{NETWORK_PREFIX}'
        result = subprocess.run(
            ['docker', 'network', 'create', '--internal', '--subnet', cidr,
             '--label', 'simulator.execution=' + VERSION, name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return cidr
        error = (result.stderr or result.stdout).lower()
        if 'overlap' not in error:
            raise RuntimeError('execution network creation failed: ' + (result.stderr or result.stdout).strip())
    raise RuntimeError('execution network subnet range exhausted')


def inspect_container(name):
    return json.loads(docker('inspect', name))[0]


def pinned_image(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('execution_image must be a locally available immutable image digest')
    if docker('image', 'inspect', value, '--format', '{{.Id}}') != value:
        raise ValueError('execution image identity mismatch')
    return value


def writable_tree(directory):
    """Grant the sandbox UID access only to generated, explicitly mounted data."""
    directory.mkdir(parents=True, exist_ok=True)
    for path in [directory, *directory.rglob('*')]:
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('unsafe generated execution workspace')
        path.chmod(path.stat().st_mode | (0o777 if path.is_dir() else 0o666))


class ExecutionSandbox:
    def __init__(self, directory, workspace, image, role, conversation_id, reference=None):
        if role not in ('code', 'judge'):
            raise ValueError('User cannot own an execution sandbox')
        self.root = Path(directory).resolve() / 'execution'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.record = self.root / 'environment.json'
        self.workspace, self.role = Path(workspace).resolve(), role
        self.image = pinned_image(image)
        self.name = 'session-tool-' + conversation_id
        self.network = 'session-net-' + conversation_id
        self.control_name = 'session-oh-' + conversation_id
        self.client = self.root / 'client'
        self.auth = self.root / 'auth'
        self.mounts = [(self.workspace / 'candidate', '/workspace/candidate', role == 'judge')]
        for name in ('home', 'outputs'):
            target = '/home/sandbox' if name == 'home' else '/workspace/tool-output'
            self.mounts.append((self.root / name, target, False))
        if role == 'judge':
            if not reference:
                raise ValueError('Judge execution requires its current reference directory')
            self.mounts.append((Path(reference).resolve(), '/reference', True))
            for name in ('checks', 'experiments'):
                self.mounts.append((self.workspace / name, '/workspace/' + name, False))
        self.expected = dict(version=VERSION, name=self.name, network=self.network,
                             image=self.image, role=role, uid=1000,
                             mounts=[dict(source=str(p), target=t, readonly=r) for p, t, r in self.mounts])

    def prepare(self):
        if self.record.exists():
            record = json.loads(self.record.read_text())
            if any(record.get(k) != v for k, v in self.expected.items()):
                raise ValueError('execution environment policy mismatch; no migration')
            if record.get('status') != 'ready':
                raise RuntimeError('uncertain execution setup; inspect retained sandbox')
            self.verify(record)
            return record
        save(self.record, {**self.expected, 'status': 'creating'})
        self.client.mkdir(mode=0o700)
        self.auth.mkdir(mode=0o700)
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.client/'id_ed25519')], check=True)
        (self.auth/'authorized_keys').write_bytes((self.client/'id_ed25519.pub').read_bytes())
        (self.auth/'authorized_keys').chmod(0o644)
        for source, _, readonly in self.mounts:
            if not readonly:
                writable_tree(source)
            elif not source.is_dir():
                raise ValueError('read-only execution mount does not exist')
        network_subnet = create_isolated_network(self.network, self.network)
        save(self.record, {**self.expected, 'status': 'creating',
                          'network_subnet': network_subnet})
        args = ['run', '-d', '--name', self.name, '--network', self.network,
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '512', '--memory', '2g', '--cpus', '2']
        for capability in CAPABILITIES:
            args += ['--cap-add', capability]
        for source, target, readonly in [*self.mounts, (self.auth, '/auth', True)]:
            args += ['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if readonly else '')]
        docker(*args, self.image)
        host_key = None
        for _ in range(100):
            result = subprocess.run(['docker', 'exec', self.name, 'cat', '/etc/ssh/ssh_host_ed25519_key.pub'], capture_output=True, text=True)
            if result.returncode == 0:
                host_key = ' '.join(result.stdout.strip().split()[:2])
                break
            if not inspect_container(self.name)['State']['Running']:
                raise RuntimeError('execution sshd failed; inspect its container logs')
            time.sleep(.1)
        if not host_key:
            raise RuntimeError('execution host identity unavailable')
        record = {**self.expected, 'status': 'ready', 'host_key': host_key,
                  'network_subnet': network_subnet,
                  'container_id': inspect_container(self.name)['Id']}
        self.verify(record)
        save(self.record, record)
        return record

    def verify(self, record=None):
        record = record or json.loads(self.record.read_text())
        details = inspect_container(self.name)
        cfg = details['HostConfig']
        expected_mounts = {(str(p), t, not r) for p, t, r in [*self.mounts, (self.auth, '/auth', True)]}
        actual_mounts = {(m['Source'], m['Destination'], m['RW']) for m in details['Mounts']}
        networks = json.loads(docker('network', 'inspect', self.network))[0]
        members = {c['Name'] for c in networks.get('Containers', {}).values()}
        subnets = {c.get('Subnet') for c in networks.get('IPAM', {}).get('Config', [])}
        if (details['Id'] != record['container_id'] or details['Image'] != self.image or
                actual_mounts != expected_mounts or cfg['Privileged'] or cfg.get('PidMode') or
                cfg.get('NetworkMode') != self.network or set(cfg.get('CapDrop') or []) != {'ALL'} or
                {c.removeprefix('CAP_') for c in cfg.get('CapAdd') or []} != set(CAPABILITIES) or
                'no-new-privileges' not in cfg.get('SecurityOpt', []) or not networks['Internal'] or
                (record.get('network_subnet') is not None and
                 subnets != {record['network_subnet']}) or
                set(details['NetworkSettings']['Networks']) != {self.network} or
                not members.issubset({self.name, self.control_name})):
            raise RuntimeError('execution container trust boundary changed')
        if not details['State']['Running']:
            raise RuntimeError('execution process state lost; refusing to recreate persistent shell')

    def worker_config(self):
        record = self.prepare()
        return dict(host=self.name, port=2222, private_key='/transport/id_ed25519',
                    known_host_key=record['host_key'], state_dir='/sdk/remote-tools')

    def pause(self):
        if not inspect_container(self.name)['State']['Paused']:
            docker('pause', self.name)

    def unpause(self):
        self.verify()
        if inspect_container(self.name)['State']['Paused']:
            docker('unpause', self.name)
