import os
import tempfile
import unittest
from pathlib import Path
from simulator.sandbox import Sandbox


@unittest.skipUnless(os.environ.get('SIMULATOR_DOCKER_TEST_IMAGE'), 'set SIMULATOR_DOCKER_TEST_IMAGE for real sandbox checks')
class DockerTests(unittest.TestCase):
    def test_workspace_write_host_secret_and_network_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            workspace=root/'workspace'
            workspace.mkdir()
            secret=root/'private-canary'
            secret.write_text('private')
            sandbox=Sandbox(workspace,os.environ['SIMULATOR_DOCKER_TEST_IMAGE'],10)
            command="""python3 - <<'PY'
import os, socket
from pathlib import Path
Path('changed').write_text('real write')
assert not Path('/private-canary').exists()
assert not Path('/workspace/../private-canary').exists()
assert not Path('/var/run/docker.sock').exists()
assert 'DEEPSEEK_API_KEY' not in os.environ
s=socket.socket(); s.settimeout(1)
assert s.connect_ex(('1.1.1.1',443)) != 0
print('isolation checks passed')
PY"""
            result=sandbox.execute(command)
            self.assertEqual(result['exit_code'],0,result)
            self.assertEqual((workspace/'changed').read_text(),'real write')

    def test_timeout_terminates_command(self):
        with tempfile.TemporaryDirectory() as directory:
            result=Sandbox(Path(directory),os.environ['SIMULATOR_DOCKER_TEST_IMAGE'],1).execute('sleep 20')
            self.assertEqual(result['exit_code'],124)
