import unittest
from unittest.mock import patch

from simulator.openhands.requirement_scope import prepare_document, validate_scope, VERSION
from simulator.openhands.issue_stages import InvalidDecomposition
from simulator.openhands.fragment_text import fragment_text


class RequirementScopeTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(title='Add capture', body='Capture output.\nChecklist: run tox.\nFuture: tee mode.')

    def test_selects_exact_current_quotes_before_decomposition(self):
        with patch('simulator.openhands.requirement_scope.call_json', side_effect=[
                {'quotes': ['Capture output.']}, {'allowed': True, 'reasons': []}]) as call, \
                patch('simulator.openhands.requirement_scope.prepare_issue', return_value={'plan': {}}) as split:
            result = prepare_document(None, self.original, 'pull_request')
        self.assertEqual(split.call_args.args[1]['body'], 'Capture output.')
        self.assertNotIn('Checklist', str(split.call_args))
        self.assertEqual(call.call_args.args[2]['original'], self.original)
        self.assertEqual(validate_scope(result['scope'], self.original)['body'], 'Capture output.')

    def test_issue_preserves_full_original(self):
        with patch('simulator.openhands.requirement_scope.call_json') as call, \
                patch('simulator.openhands.requirement_scope.prepare_issue') as split:
            prepare_document(None, self.original, 'issue')
        call.assert_not_called()
        split.assert_called_once_with(None, self.original)

    def test_invalid_or_rejected_selection_never_reaches_splitter(self):
        for draft, review in [({'quotes': ['Invented.']}, {}),
                              ({'quotes': ['Capture output.']}, {'allowed': False, 'reasons': ['missing constraint']})]:
            with patch('simulator.openhands.requirement_scope.call_json', side_effect=[draft, review]), \
                    patch('simulator.openhands.requirement_scope.prepare_issue') as split:
                with self.assertRaises(InvalidDecomposition):
                    prepare_document(None, self.original, 'pull_request')
                split.assert_not_called()

    def test_modified_cached_document_is_rejected(self):
        record = dict(version=VERSION, quotes=['Capture output.'],
                      document=dict(title='Current requested behavior', body='Future: tee mode.'),
                      review=dict(allowed=True, reasons=[]))
        with self.assertRaises(ValueError):
            validate_scope(record, self.original)

    def test_released_fenced_reproduction_remains_verbatim(self):
        block = '```python\nprint("hello")\n```'
        text = fragment_text(dict(category='symptom', text='复现', source_quote=block))
        self.assertIn(block, text)


if __name__ == '__main__':
    unittest.main()
