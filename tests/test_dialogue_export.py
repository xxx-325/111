import json
import tempfile
import unittest
from pathlib import Path

from simulator.openhands.dialogue_export import dialogue_messages, export_dialogue


class DialogueExportTests(unittest.TestCase):
    def test_only_original_public_text_and_event_identity(self):
        rows = [dict(id='u1',kind='user',text='试一下'),
                dict(id='t',kind='tool_call',action='PRIVATE_TOOL'),
                dict(id='r',kind='tool_result',observation='PRIVATE_RESULT'),
                dict(id='a0',kind='assistant',phase='analysis',text='PRIVATE_ANALYSIS'),
                dict(id='a1',kind='assistant',phase='final',text='原始代码\n<script>hi</script>'),
                dict(id='u2',kind='user',text='试一下'),
                dict(id='end',kind='user',text='好的',delivery='closing_not_forwarded_to_code')]
        expected = [dict(role='user',content='试一下'),
                    dict(role='assistant',content='原始代码\n<script>hi</script>'),
                    dict(role='user',content='试一下'),dict(role='user',content='好的')]
        self.assertEqual(dialogue_messages(rows + [rows[0]]),expected)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'session.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            export_dialogue(root)
            self.assertEqual(json.loads((root/'dialogue.json').read_text()),expected)
            page=(root/'dialogue.html').read_text()
            self.assertNotIn('PRIVATE_',page)
            self.assertNotIn('<script>',page)
            self.assertIn('&lt;script&gt;',page)

    def test_empty_interrupted_run_has_no_fabricated_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(export_dialogue(temp),[])
