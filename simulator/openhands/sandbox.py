"""Host-owned Docker execution boundary; no model command is executed here."""
import base64
import hashlib
import inspect
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from ..episode import save
from .snapshot_volume import upload_snapshot


VERSION = 'ssh-sandbox-v2-judge-volumes'
CAPABILITIES = ['SETUID', 'SETGID', 'SYS_CHROOT']
NETWORK_BLOCK = (10 << 24) | (240 << 16)
NETWORK_PREFIX = 28
NETWORK_COUNT = 1 << (NETWORK_PREFIX - 16)

# The candidate tree is bind-mounted as source, so no distribution is installed
# and importlib.metadata cannot resolve its name or version -- which Sphinx
# autodoc needs for the docs tasks. Publish the minimal dist-info offline: no
# network, no build backend, no candidate modification.
_CANDIDATE_METADATA_SOURCE = '''\
import pathlib
import re
import site
import sys

root = pathlib.Path("/workspace/candidate")
pyproject = root / "pyproject.toml"
if not pyproject.exists():
    sys.exit(0)
text = pyproject.read_text()
name = re.search(r'^name\\s*=\\s*"([^"]+)"', text, re.M)
version = re.search(r'^version\\s*=\\s*"([^"]+)"', text, re.M)
if not name or not version:
    sys.exit(0)
targets = [p for p in site.getsitepackages() if pathlib.Path(p).is_dir()]
if not targets:
    sys.exit(0)
dist = pathlib.Path(targets[0]) / (name.group(1) + "-" + version.group(1) + ".dist-info")
dist.mkdir(parents=True, exist_ok=True)
(dist / "METADATA").write_text(
    "Metadata-Version: 2.1\\nName: " + name.group(1) + "\\nVersion: " + version.group(1) + "\\n"
)
(dist / "INSTALLER").write_text("sandbox-candidate-metadata\\n")
(dist / "RECORD").write_text("")
'''
_CANDIDATE_METADATA_COMMAND = (
    "import base64;exec(base64.b64decode('"
    + base64.b64encode(_CANDIDATE_METADATA_SOURCE.encode()).decode()
    + "').decode())"
)


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


def bind_source(mount):
    """Docker Desktop may report its VM prefix for a macOS bind source."""
    source = mount['Source']
    if sys.platform == 'darwin' and source.startswith('/host_mnt/'):
        return source.removeprefix('/host_mnt')
    return source


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
        self.volumes = {
            target: self.name + ('-candidate' if target == '/workspace/candidate' else '-reference')
            for _, target, readonly in self.mounts if role == 'judge' and readonly
        }
        self.expected = dict(version=VERSION, name=self.name, network=self.network,
                             image=self.image, role=role, uid=1000,
                             snapshot_volumes=self.volumes,
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
        for source, target, _ in self.mounts:
            if target in self.volumes:
                volume = self.volumes[target]
                if subprocess.run(['docker', 'volume', 'inspect', volume], capture_output=True).returncode == 0:
                    raise RuntimeError('unexpected existing snapshot volume')
                docker('volume', 'create', '--label', 'simulator.execution=' + VERSION, volume)
                upload_snapshot(source, volume, self.image)
        network_subnet = create_isolated_network(self.network, self.network)
        save(self.record, {**self.expected, 'status': 'creating',
                          'network_subnet': network_subnet})
        args = ['run', '-d', '--name', self.name, '--network', self.network,
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '512', '--memory', '2g', '--cpus', '2']
        for capability in CAPABILITIES:
            args += ['--cap-add', capability]
        for source, target, readonly in [*self.mounts, (self.auth, '/auth', True)]:
            mount = (f'type=volume,src={self.volumes[target]},dst={target},volume-nocopy'
                     if target in self.volumes else f'type=bind,src={source},dst={target}')
            args += ['--mount', mount + (',readonly' if readonly else '')]
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
        self.provision_candidate_metadata()
        record = {**self.expected, 'status': 'ready', 'host_key': host_key,
                  'network_subnet': network_subnet,
                  'container_id': inspect_container(self.name)['Id']}
        self.verify(record)
        save(self.record, record)
        return record

    def provision_candidate_metadata(self):
        """Publish candidate distribution metadata so offline docs builds resolve it.

        The candidate is bind-mounted as source, so importlib.metadata has no
        name or version to report and Sphinx autodoc aborts. This writes the
        minimal ``*.dist-info`` the build reads, without network, build backend
        or any change to the candidate tree itself.
        """
        result = subprocess.run(
            ['docker', 'exec', self.name, 'python', '-c', _CANDIDATE_METADATA_COMMAND],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                'candidate distribution metadata provisioning failed: '
                + (result.stderr or '').strip()
            )

    def verify(self, record=None):
        record = record or json.loads(self.record.read_text())
        details = inspect_container(self.name)
        cfg = details['HostConfig']
        expected_mounts = {('volume' if t in self.volumes else 'bind',
                            self.volumes.get(t, str(p)), t, not r)
                           for p, t, r in [*self.mounts, (self.auth, '/auth', True)]}
        actual_mounts = {(m.get('Type', 'bind'), m.get('Name') if m.get('Type') == 'volume' else bind_source(m),
                          m['Destination'], m['RW']) for m in details['Mounts']}
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

    def candidate_hash(self):
        """Read the candidate through the same mount and UID as the agent."""
        from .judge import candidate_hash

        self.verify()
        script = ('import hashlib\nfrom pathlib import Path\n'
                  + inspect.getsource(candidate_hash)
                  + '\nprint(candidate_hash("/workspace/candidate"))\n')
        result = subprocess.run(
            ['docker', 'exec', '--user', '1000', self.name, 'python', '-c', script],
            capture_output=True, text=True, timeout=60, check=True,
        )
        value = result.stdout.strip()
        if not re.fullmatch(r'[0-9a-f]{64}', value):
            raise RuntimeError('invalid container candidate fingerprint')
        return value

    def sync_snapshots(self):
        """Update Judge's volumes with its processes frozen, then verify reads."""
        if self.role != 'judge':
            raise ValueError('only Judge has snapshot volumes')
        self.verify()
        self.pause()
        # Keep the reader frozen on any upload failure. Never assess partial data.
        for source, target, _ in self.mounts:
            if target in self.volumes:
                upload_snapshot(source, self.volumes[target], self.image)
        self.unpause()

    def pause(self):
        if not inspect_container(self.name)['State']['Paused']:
            docker('pause', self.name)

    def unpause(self):
        self.verify()
        if inspect_container(self.name)['State']['Paused']:
            docker('unpause', self.name)
