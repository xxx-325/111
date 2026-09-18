import unittest
from simulator.openhands.policy import role_prompts, policy_record


class CodePromptModeTests(unittest.TestCase):
    def test_default_mode_removes_only_code_override(self):
        old = role_prompts('zh-CN')
        new = role_prompts('zh-CN', 'sdk_default')
        self.assertIsNone(new['code'])
        self.assertEqual(old['user'], new['user'])
        self.assertIsNone(policy_record('zh-CN', code_prompt_mode='sdk_default')['prompt_sha256']['code'])
        self.assertNotEqual(policy_record('zh-CN'), policy_record('zh-CN', code_prompt_mode='sdk_default'))

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            role_prompts('zh-CN', 'unknown')
