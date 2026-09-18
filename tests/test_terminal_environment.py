import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.openhands.container import SDKContainer, candidate_pythonpath
from simulator.openhands.tool_wording import tool_specs


class TerminalEnvironmentTests(unittest.TestCase):
    def test_candidate_pythonpath_is_terminal_only_for_code_and_judge(self):
        value = '/workspace/candidate/src'
        before = os.environ.get('PYTHONPATH')
        for role in ('code', 'judge'):
            specs = tool_specs(role, candidate_pythonpath=value)
            terminal = next(tool for tool in specs if tool.name == 'terminal')
            self.assertEqual(terminal.params, {'env': {'PYTHONPATH': value}})
            self.assertTrue(all(not tool.params for tool in specs if tool.name != 'terminal'))
        self.assertEqual(tool_specs('user', candidate_pythonpath=value), [])
        self.assertEqual(os.environ.get('PYTHONPATH'), before)

    def test_unconfigured_tools_keep_existing_sdk_defaults(self):
        for role in ('code', 'judge'):
            self.assertTrue(all(not tool.params for tool in tool_specs(role)))

    def test_rejects_host_paths_lists_and_noncanonical_paths(self):
        self.assertEqual(candidate_pythonpath('/workspace/candidate'), '/workspace/candidate')
        self.assertEqual(candidate_pythonpath('/workspace/candidate/src'), '/workspace/candidate/src')
        for value in ('src', '/tmp/src', '/workspace/candidate/../private',
                      '/workspace/candidate/src/', '/workspace/candidate/src:/opt/other', '', 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate_pythonpath(value)

    def test_container_persists_validated_path_without_mutating_process_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            before = os.environ.get('PYTHONPATH')
            SDKContainer(root / 'private', workspace, {
                'model': 'fixture',
                'candidate_pythonpath': '/workspace/candidate/src',
                'execution_backend': 'shared_diagnostic',
                'execution_image': 'sha256:' + '0' * 64,
            }, 'fixture-image', 'code', None, 999999999)
            saved = json.loads((root / 'private/inbox/config.json').read_text())
            self.assertEqual(saved['candidate_pythonpath'], '/workspace/candidate/src')
            self.assertEqual(os.environ.get('PYTHONPATH'), before)

    def test_resume_rejects_a_stale_or_changed_terminal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            private = root / 'private'
            SDKContainer(private, workspace, {
                'model': 'fixture',
                'candidate_pythonpath': '/workspace/candidate/src',
                'execution_backend': 'shared_diagnostic',
                'execution_image': 'sha256:' + '0' * 64,
            }, 'fixture-image', 'code', None, 999999999)
            with self.assertRaisesRegex(ValueError, 'differs from the persisted'):
                SDKContainer(private, workspace, {
                    'model': 'fixture',
                    'candidate_pythonpath': '/workspace/candidate/lib',
                    'execution_backend': 'shared_diagnostic',
                    'execution_image': 'sha256:' + '0' * 64,
                }, 'fixture-image', 'code', None, 999999999)

    def test_import_suffix_separates_execution_sandbox_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'; workspace.mkdir()
            private = root / 'private'
            inbox = private / 'inbox'; inbox.mkdir(parents=True)
            (inbox / 'config.json').write_text(json.dumps({
                'model':'fixture', 'role':'code', 'system':None, 'control':False,
                'browser':False, 'condenser_max_size':120, 'neutral_tools':False,
                'conversation_id':'conversation', 'container_suffix':'0123456789ab',
                'temperature':.3, 'candidate_pythonpath':None,
                'execution_backend':'ssh_sandbox',
                'execution_image':'sha256:' + '0' * 64,
            }))
            config={'model':'fixture', 'execution_backend':'ssh_sandbox',
                    'execution_image':'sha256:' + '0' * 64}
            with patch('simulator.openhands.container.ExecutionSandbox') as sandbox, patch(
                    'simulator.openhands.container.Relay'):
                SDKContainer(private, workspace, config, 'fixture-image', 'code', None,
                             999999999)
            self.assertEqual(sandbox.call_args.args[4], 'conversation-0123456789ab')

    def test_worker_runtime_bundle_imports_shared_state_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            private = root / 'private'
            SDKContainer(private, workspace, {
                'model': 'fixture',
                'execution_backend': 'shared_diagnostic',
                'execution_image': 'sha256:' + '0' * 64,
            }, 'fixture-image', 'code', None, 999999999)
            runtime = private / 'runtime'
            result = subprocess.run(
                [sys.executable, '-c',
                 'from simulator.openhands.worker import CONTROL_TOOLS; '
                 'from simulator.state_machine import REQUEST_SEMANTICS; '
                 'assert CONTROL_TOOLS and REQUEST_SEMANTICS["controls"]["REFINE"]'],
                cwd=runtime,
                env={**os.environ, 'PYTHONPATH': str(runtime)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
