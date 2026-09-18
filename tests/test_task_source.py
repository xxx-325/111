import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from simulator.task_source import requirement_source
from simulator.tasks import prepare


class RequirementSourceTests(unittest.TestCase):
    def setUp(self):
        self.document = dict(number=12, html_url='https://github.com/a/b/pull/12',
                             title='Add output capture', body='Keep both streams.\n```python\nprint(1)\n```',
                             merged_at='2026-01-01', merge_commit_sha='a' * 40)

    def test_pr_preserves_document_and_source_type(self):
        fetch = Mock(return_value=self.document)
        result = requirement_source({'pull_request': 12}, 'a/b', fetch)
        self.assertEqual(result['kind'], 'pull_request')
        self.assertEqual(result['body'], self.document['body'])
        self.assertEqual(result['reference'], 'a' * 40)
        fetch.assert_called_once_with('a/b', 'pulls/12')

    def test_cached_pr_never_fetches_and_prepare_uses_merged_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'pr.json'
            cache.write_text(json.dumps(self.document))
            config = dict(repository=str(root), github_repository='a/b', base='base',
                          tasks=[dict(pull_request=12, pull_request_file=str(cache))])
            with patch('simulator.tasks.github', side_effect=AssertionError('cache should suffice')), \
                    patch('simulator.tasks.git', side_effect=['a' * 40, 'b' * 40, 'c' * 40]):
                _, base, tasks = prepare(config, root, include_patch=False)
            self.assertEqual(base, 'c' * 40)
            self.assertEqual(tasks[0]['kind'], 'pull_request')
            self.assertEqual(tasks[0]['patch'], '')
            self.assertEqual(tasks[0]['body'], self.document['body'])

    def test_rejects_unmerged_mismatched_and_wrong_type(self):
        for change in ({'merged_at': None}, {'merge_commit_sha': None}, {'number': 13},
                       {'html_url': 'https://github.com/a/c/pull/12'}):
            with self.assertRaises(ValueError):
                requirement_source({'pull_request': 12}, 'a/b', Mock(return_value=dict(self.document, **change)))
        issue = dict(self.document, html_url='https://github.com/a/b/issues/12', pull_request={})
        with self.assertRaises(ValueError):
            requirement_source({'issue': 12}, 'a/b', Mock(return_value=issue))
        for spec in ({'issue': 12, 'commit': 'a'}, {'pull_request': 'https://github.com/a/c/pull/12'}, {}):
            with self.assertRaises(ValueError):
                requirement_source(spec, 'a/b', Mock())

    def test_issue_and_commit_keep_distinct_authorities(self):
        issue = dict(number=1, html_url='https://github.com/a/b/issues/1', title='Feature', body='Need it')
        result = requirement_source({'issue': 1}, 'a/b', Mock(return_value=issue))
        self.assertEqual(result['kind'], 'issue')
        self.assertIsNone(result['reference'])
        self.assertIsNone(requirement_source({'commit': 'a'}, 'a/b', Mock()))


if __name__ == '__main__':
    unittest.main()
