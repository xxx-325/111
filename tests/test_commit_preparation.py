import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.tasks import prepare
from simulator.openhands.commit_preparation import prepare_commit, validate_projection


class ContinuousCommitTests(unittest.TestCase):
    def test_order_must_include_every_parent_without_skip_or_reorder(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            refs = []
            for i in range(4):
                (repo/'sample').write_text(str(i))
                subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                                'user.email=fixture@example.invalid', 'commit', '-qm', str(i)], check=True)
                refs.append(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip())
            config = dict(repository=str(repo), base=refs[0], continuous_commits=True,
                          tasks=[{'commit': sha} for sha in refs[1:]])
            _, _, tasks = prepare(config, repo, include_patch=False)
            self.assertEqual([t['reference'] for t in tasks], refs[1:])
            for selected in (refs[2:], refs[1:2]+refs[3:], list(reversed(refs[1:]))):
                with self.assertRaises(ValueError):
                    prepare(dict(config,tasks=[{'commit': sha} for sha in selected]),repo,include_patch=False)

    def test_projection_is_private_and_bound_before_split(self):
        task = dict(reference='ref', base='parent', title='Change')
        document = dict(title='需求', body='文档需要换成新的格式。')
        with patch('simulator.openhands.commit_preparation.reference_patch',return_value='private diff'), \
                patch('simulator.openhands.commit_preparation.project_commit',return_value=document) as infer, \
                patch('simulator.openhands.commit_preparation.prepare_issue',return_value={'plan': {}}) as split:
            result = prepare_commit(None, task, None)
            self.assertEqual(infer.call_args.args[0]['patch'], 'private diff')
            split.assert_called_once_with(None, document)
            self.assertEqual(validate_projection(result['projection'],task,None), document)
            bad = dict(result['projection'],reference='different')
            with self.assertRaises(ValueError):
                validate_projection(bad,task,None)
        self.assertEqual(result['projection']['patch_sha256'],hashlib.sha256(b'private diff').hexdigest())


if __name__ == '__main__':
    unittest.main()
