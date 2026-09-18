"""Public reproductions retain their source after private execution."""
import json
import unittest
from simulator.openhands.provenance import collect_sources, record, source_check


class ProvenanceTests(unittest.TestCase):
    def test_public_and_mixed_code(self):
        public='start_day = date(year=2012, month=12, day=25)'
        private='assert actual_dates == expected_dates'
        records=[record('private',public+'\n'+private,event_id='e1')]
        result=source_check(public,records,{'body':public},[])
        self.assertFalse(result['matches'])
        result=source_check(public+'\n'+private,records,{'body':public},[])
        self.assertEqual([m['text'] for m in result['matches']],[private])
        self.assertTrue(source_check(private,records,{},[])['matches'])
        self.assertFalse(source_check(private,records,{},[{'id':'m','text':private}])['matches'])

    def test_script_forms_and_roundtrip(self):
        snippet='assert actual_dates == expected_dates'
        for action,tool in [({'command':"python -c '"+snippet+"'"},'terminal'),
            ({'command':"python <<'PY'\n"+snippet+'\nPY'},'terminal'),
            ({'command':'create','path':'/workspace/checks/check.py','file_text':snippet},'file_editor')]:
            records=collect_sources([], [dict(id='e',tool_name=tool,action=action)])
            self.assertEqual(records,json.loads(json.dumps(records)))
            self.assertTrue(source_check(snippet,records,{},[])['matches'])
            self.assertFalse(source_check('AssertionError: dates differ',records,{},[])['matches'])
            self.assertTrue(source_check('Traceback\n  '+snippet+'\nAssertionError',records,{},[])['matches'])

    def test_generic_and_unknown(self):
        self.assertFalse(source_check('from datetime import date',[record('private','from datetime import date')],{},[])['matches'])
        records=collect_sources([], [dict(id='e',tool_name='terminal',action={'command':'unrecognized command\nlong uncertain content'})])
        self.assertTrue(source_check('long uncertain content',records,{},[])['unknown'])
