"""Lifecycle policy fixtures; real tool probes are separately opt-in."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simulator.openhands.sandbox import (
    ExecutionSandbox,
    create_isolated_network,
    pinned_image,
    writable_tree,
)
from simulator.openhands.container import SDKContainer


def subprocess_result(returncode, stderr=''):
    return SimpleNamespace(returncode=returncode, stdout='', stderr=stderr)


class SandboxLifecycleTests(unittest.TestCase):
    def test_container_hash_uses_shared_algorithm_and_non_root_execution(self):
        from simulator.openhands.judge import candidate_hash
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'example.py').write_text('assert True\n')
            sandbox = ExecutionSandbox.__new__(ExecutionSandbox)
            sandbox.name = 'isolated-judge'
            sandbox.verify = MagicMock()
            import subprocess
            run = subprocess.run
            def execute(command, **kwargs):
                self.assertEqual(command[:6], ['docker', 'exec', '--user', '1000', 'isolated-judge', 'python'])
                import sys
                return run([sys.executable, '-c', command[-1].replace('"/workspace/candidate"', repr(root))], **kwargs)
            with patch('simulator.openhands.sandbox.subprocess.run', side_effect=execute):
                self.assertEqual(sandbox.candidate_hash(), candidate_hash(path))

    def test_mutable_image_is_not_allowed(self):
        with self.assertRaises(ValueError):
            pinned_image('project:latest')

    def test_user_cannot_have_an_execution_sandbox(self):
        with self.assertRaises(ValueError):
            ExecutionSandbox('.', '.', None, 'user', 'id')

    def test_browser_cannot_fall_back_to_local(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'isolated executor'):
                SDKContainer(Path(directory)/'state', Path(directory)/'work',
                             {'model':'unused'}, 'image', 'code', None, 0, browser=True)

    def test_generated_mounts_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'link').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                writable_tree(root)

    def test_mount_role_and_image_identity_are_authoritative(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                'simulator.openhands.sandbox.pinned_image',return_value='sha256:fixture'):
            root=Path(directory)
            sandbox=ExecutionSandbox(root/'private',root/'workspace', 'sha256:fixture','code','fixture')
            details=dict(Id='id',Image='sha256:fixture',State={'Running':True},
                         Mounts=[dict(Source=str(p),Destination=t,RW=not r) for p,t,r in
                                 [*sandbox.mounts,(sandbox.auth,'/auth',True)]],
                         NetworkSettings={'Networks':{sandbox.network:{}}},
                         HostConfig=dict(Privileged=False,PidMode='',NetworkMode=sandbox.network,
                             CapAdd=['CAP_SETUID','CAP_SETGID','CAP_SYS_CHROOT'],CapDrop=['ALL'],
                             SecurityOpt=['no-new-privileges']))
            record={'container_id':'id','network_subnet':'10.240.0.0/28'}
            with patch('simulator.openhands.sandbox.inspect_container',return_value=details), patch(
                    'simulator.openhands.sandbox.docker',return_value=json.dumps([{
                        'Internal':True,'IPAM':{'Config':[{'Subnet':'10.240.0.0/28'}]}}])):
                sandbox.verify(record)
                sandbox.verify({'container_id':'id'})
                with self.assertRaisesRegex(RuntimeError,'trust boundary'):
                    sandbox.verify({'container_id':'id','network_subnet':'10.240.0.16/28'})
                details['Mounts'].append(dict(Source='/private',Destination='/private',RW=True))
                with self.assertRaisesRegex(RuntimeError,'trust boundary'):
                    sandbox.verify(record)
                details['Mounts'].pop()
                details['State']['Running']=False
                with self.assertRaisesRegex(RuntimeError,'process state lost'):
                    sandbox.verify(record)

    def test_explicit_small_subnet_does_not_use_default_address_pool(self):
        collision = subprocess_result(1, 'Pool overlaps with other one on this address space')
        created = subprocess_result(0, 'network-id')
        with patch('simulator.openhands.sandbox.subprocess.run', side_effect=[collision, created]) as run:
            subnet = create_isolated_network('session-net-fixture', 'fixture')
        self.assertRegex(subnet, r'^10\.240\.\d+\.\d+/28$')
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn('--subnet', call.args[0])

    def test_network_creation_does_not_hide_unrelated_docker_errors(self):
        with patch('simulator.openhands.sandbox.subprocess.run',
                   return_value=subprocess_result(1, 'permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                create_isolated_network('session-net-fixture', 'fixture')

    def test_judge_mounts_are_read_only_and_separate(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                'simulator.openhands.sandbox.pinned_image',return_value='sha256:fixture'):
            root=Path(directory)
            sandbox=ExecutionSandbox(root/'private',root/'judge-workspace','sha256:fixture',
                                     'judge','fixture',root/'reference')
            mounts={target:(source,readonly) for source,target,readonly in sandbox.mounts}
            self.assertTrue(mounts['/workspace/candidate'][1])
            self.assertTrue(mounts['/reference'][1])
            self.assertFalse(mounts['/workspace/checks'][1])
            self.assertNotIn('/sdk',mounts)
            self.assertNotIn('/transport',mounts)


if __name__=='__main__':
    unittest.main()
