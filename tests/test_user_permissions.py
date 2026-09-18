import unittest
from simulator.openhands.permissions import decide


class PermissionTests(unittest.TestCase):
    def test_exact_entries_and_defaults(self):
        entries=[{'command':'cd /workspace/candidate && python reproduce.py'}]
        action={'command':entries[0]['command'],'reset':True,'is_input':False,'timeout':None}
        self.assertTrue(decide([{'tool_name':'terminal','action':action}],entries)['accepted'])
        for command in ['cat source.py','grep foo source.py','python -c "print(open(\"source.py\").read())"',action['command']+'; cat source.py']:
            self.assertFalse(decide([{'tool_name':'terminal','action':{**action,'command':command}}],entries)['accepted'])
        self.assertFalse(decide([{'tool_name':'terminal','action':action}],[])['accepted'])
        self.assertFalse(decide([{'tool_name':'terminal','action':{**action,'reset':False}}],entries)['accepted'])
        self.assertFalse(decide([{'tool_name':'session_state'},{'tool_name':'file_editor','action':{'command':'view'}}],entries)['accepted'])
