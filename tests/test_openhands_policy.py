"""Prompt/configuration tests, not evidence of human-like behavior."""
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from simulator.openhands.policy import role_prompts, policy_record, PROMPTS, LANGUAGES


class PolicyTests(unittest.TestCase):
    def test_roles_have_explicit_language_and_configured_role_text(self):
        source = json.loads(PROMPTS.read_text())
        for language, phrase in [('zh-CN','公开对话使用简体中文'),('en','Use English for public dialogue')]:
            prompts = role_prompts(language)
            self.assertTrue(all(phrase in p for p in prompts.values()))
            for excerpt in source['role']:
                self.assertNotIn(excerpt, prompts['user'])
            self.assertNotIn('under 15 words', prompts['user'])
            self.assertNotIn('beyond 3 turns', prompts['user'])
            self.assertIn('你是与 Code Agent 对话的用户', prompts['user'])
            self.assertNotIn('GitHub user', prompts['user'])
            self.assertNotIn('reporting an issue', prompts['user'])
            self.assertIn('不读源码', prompts['user'])
            self.assertIn('不补造细节', prompts['user'])
            self.assertEqual(prompts['code'], '你是代码助手。按用户需求检查、修改并验证 /workspace/candidate 中的代码；环境离线，一轮结束如实说明结果或待回答的问题。\n'+LANGUAGES[language])

    def test_language_changes_policy_identity(self):
        self.assertNotEqual(policy_record('zh-CN'), policy_record('en'))
        with self.assertRaises(ValueError):
            role_prompts('unsupported')

    def test_shared_leak_policy_is_pinned(self):
        policy = policy_record('zh-CN')
        self.assertEqual(policy['version'], 'delegating-v25-cross-task-final-evidence')
        self.assertIn('../episode.py', policy['implementation_sha256'])
        self.assertIn('container.py', policy['implementation_sha256'])
        self.assertIn('transition_selection.py', policy['implementation_sha256'])

    def test_user_state_does_not_repeat_tool_protocol(self):
        from simulator.openhands.state import TaskState
        self.assertNotIn('protocol',TaskState().view())

    def test_changed_relay_policy_cannot_resume_old_checkpoint(self):
        from simulator.openhands.episode import OpenHandsEpisode

        config = dict(dialogue_language='zh-CN', dynamic_transition_selection=True,
                      execution_backend='ssh_sandbox', execution_image='sha256:' + 'a' * 64)
        old_policy = policy_record('zh-CN')
        old_policy['implementation_sha256']['relay.py'] = 'old-error-protocol'
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / 'private'
            private.mkdir()
            checkpoint = private / 'checkpoint.json'
            original = json.dumps(dict(schema=OpenHandsEpisode.checkpoint_schema,
                                       config=config, policy=old_policy))
            checkpoint.write_text(original)
            with patch('simulator.openhands.sandbox.pinned_image'), self.assertRaisesRegex(
                    ValueError, 'no automatic migration'):
                OpenHandsEpisode(config, folder, resume=True)
            self.assertEqual(checkpoint.read_text(), original)
