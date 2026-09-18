"""Verify minimal role-specific SDK tool sets."""
import unittest
from simulator.openhands.tool_wording import tool_specs
from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.policy import policy_record


class ToolWordingTests(unittest.TestCase):
    def test_user_does_not_see_unusable_source_tools(self):
        self.assertEqual([tool.name for tool in tool_specs('user')], [])
        self.assertEqual([tool.name for tool in tool_specs('code')],
                         ['terminal', 'file_editor', 'task_tracker'])
        self.assertEqual([tool.name for tool in tool_specs('judge')],
                         ['terminal', 'file_editor', 'task_tracker'])

    def test_variant_inputs_and_identity(self):
        e = OpenHandsEpisode.__new__(OpenHandsEpisode)
        instructions = {}
        for variant in ('baseline','delegate','neutral'):
            e.config = {'delegation_variant':variant}
            instructions[variant] = e.initial_instruction()
            specs=tool_specs('user',variant=='neutral')
            self.assertEqual([t.name for t in specs],[])
        self.assertEqual(instructions['delegate'],instructions['neutral'])
        self.assertEqual(instructions['baseline'],instructions['delegate'])
        self.assertNotIn('先检查',instructions['neutral'])
        self.assertNotEqual(policy_record('zh-CN','baseline'),policy_record('zh-CN','neutral'))
