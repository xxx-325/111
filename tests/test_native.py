import json
import unittest
from simulator.native import decode_events
from simulator.api_agent import parse_object


class NativeTests(unittest.TestCase):
    def test_codex_keeps_tools_and_excludes_reasoning(self):
        raw = [dict(type='thread.started',thread_id='id'),
               dict(type='item.completed',item=dict(type='reasoning',text='private')),
               dict(type='item.completed',item=dict(type='command_execution',command='pytest',exit_code=0,aggregated_output='passed')),
               dict(type='item.completed',item=dict(type='agent_message',text='finished')),
               dict(type='turn.completed')]
        events=[]
        session, final = decode_events('codex', map(json.dumps,raw), lambda k,d:events.append((k,d)))
        self.assertEqual((session, final), ('id','finished'))
        self.assertIn('native_tool', [k for k,d in events])
        self.assertNotIn('private',json.dumps(events))

    def test_claude_requires_success_and_preserves_tool_result(self):
        raw=[dict(type='system',session_id='id'),dict(type='user',message=dict(content=[dict(type='tool_result',tool_use_id='1',content='failed')])),dict(type='result',subtype='success',result='fixed',is_error=False)]
        events=[]
        self.assertEqual(decode_events('claude',map(json.dumps,raw),lambda k,d:events.append((k,d))),('id','fixed'))
        self.assertIn('failed',json.dumps(events))
        with self.assertRaises(ValueError):
            decode_events('claude',map(json.dumps,raw[:-1]),lambda *a:None)

    def test_json_prose_is_accepted_but_ambiguous_objects_are_rejected(self):
        self.assertEqual(parse_object('Observed result.\n{"status":"completed"}'),{'status':'completed'})
        with self.assertRaises(ValueError):
            parse_object('prefix {"a":1} {"a":2}')
