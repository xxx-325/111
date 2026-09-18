import unittest

from simulator.openhands.judge import validate_verdict_core


class JudgePipelineEvidenceTests(unittest.TestCase):
    job = {
        'task_id': 'task-1',
        'candidate_version': 'candidate',
    }
    plan = {'items': []}

    @staticmethod
    def observation(identifier, command, exit_code, pipeline_exit_policy=None):
        result = {
            'id': identifier,
            'tool': 'terminal',
            'exit_code': exit_code,
            'observation': {'command': command, 'exit_code': exit_code},
        }
        if pipeline_exit_policy:
            result['pipeline_exit_policy'] = pipeline_exit_policy
        return result

    def validate(self, observations):
        validate_verdict_core(
            {
                'task_id': 'task-1',
                'candidate_version': 'candidate',
                'outcome': 'solved',
                'reason': 'Validation passed',
                'evidence_ids': [item['id'] for item in observations],
                'requested_fragment_ids': [],
            },
            self.job,
            observations,
            self.plan,
        )

    def test_sphinx_build_piped_to_tail_cannot_support_solved(self):
        with self.assertRaisesRegex(ValueError, 'successful validation exit'):
            self.validate([
                self.observation(
                    'sphinx',
                    'sphinx-build -W docs docs/_build 2>&1 | tail -80',
                    1,
                )
            ])

    def test_pytest_piped_to_tail_cannot_support_solved(self):
        with self.assertRaisesRegex(ValueError, 'successful validation exit'):
            self.validate([
                self.observation('pytest', 'python -m pytest -q 2>&1 | tail -40', 1)
            ])

    def test_unprotected_zero_pipeline_exit_cannot_support_solved(self):
        with self.assertRaisesRegex(ValueError, 'no trusted exit policy'):
            self.validate([
                self.observation('pytest', 'pytest -q 2>&1 | tail -40', 0)
            ])

    def test_host_pipefail_zero_pipeline_exit_supports_solved(self):
        self.validate([
            self.observation(
                'pytest', 'pytest -q 2>&1 | tail -40', 0, 'pipefail'
            )
        ])

    def test_non_validation_pipeline_does_not_block_solved(self):
        self.validate([
            self.observation('inspect', 'grep -R TODO docs | head -20', 141)
        ])

    def test_exit_zero_allows_separate_log_view(self):
        self.validate([
            self.observation('real', 'pytest -q >/tmp/pytest.log 2>&1', 0),
            self.observation('view', 'tail -40 /tmp/pytest.log', 0),
        ])

    def test_independent_validation_exit_zero_passes(self):
        self.validate([
            self.observation('real', 'sphinx-build -W docs docs/_build', 0)
        ])

    def test_nonzero_validation_exit_cannot_support_solved(self):
        with self.assertRaisesRegex(ValueError, 'successful validation exit'):
            self.validate([
                self.observation('real', 'pytest -q', 1)
            ])

    def test_later_full_pytest_success_supersedes_earlier_pytest_failure(self):
        self.validate([
            self.observation(
                'cache-failure',
                'python -m pytest tests/test_testing.py -q 2>&1 | tail -15',
                1,
                'pipefail',
            ),
            self.observation(
                'targeted-retry',
                'python -m pytest tests/test_testing.py -q -p no:cacheprovider '
                '2>&1 | tail -15',
                0,
                'pipefail',
            ),
            self.observation(
                'full-retry',
                'python -m pytest -q -p no:cacheprovider 2>&1 | tail -15',
                0,
                'pipefail',
            ),
        ])

    def test_equivalent_pytest_retry_supersedes_environment_failure(self):
        self.validate([
            self.observation(
                'cache-failure',
                'python -m pytest tests/test_stream_lifecycle.py -q '
                '2>&1 | tail -15',
                1,
                'pipefail',
            ),
            self.observation(
                'retry',
                'python -m pytest tests/test_stream_lifecycle.py -q '
                '-p no:cacheprovider 2>&1 | tail -15',
                0,
                'pipefail',
            ),
        ])

    def test_different_pytest_filter_does_not_supersede_failure(self):
        with self.assertRaisesRegex(ValueError, 'successful validation exit'):
            self.validate([
                self.observation('failed', "pytest tests/test_a.py -k 'first'", 1),
                self.observation('retry', "pytest tests/test_a.py -k 'second'", 0),
            ])

    def test_equivalent_pytest_filter_retry_supersedes_failure(self):
        self.validate([
            self.observation('failed', "pytest tests/test_a.py -k 'case' -q", 1),
            self.observation(
                'retry',
                "pytest -p no:cacheprovider -k 'case' tests/test_a.py",
                0,
            ),
        ])

    def test_targeted_success_does_not_supersede_earlier_pytest_failure(self):
        with self.assertRaisesRegex(ValueError, 'successful validation exit'):
            self.validate([
                self.observation('failed', 'pytest tests/test_a.py -q', 1),
                self.observation('other', 'pytest tests/test_b.py -q', 0),
            ])

    def test_targeted_success_does_not_supersede_full_pytest_failure(self):
        with self.assertRaisesRegex(ValueError, 'successful validation exit'):
            self.validate([
                self.observation('failed', 'pytest -q', 1),
                self.observation('targeted', 'pytest tests/test_a.py -q', 0),
            ])

    def test_untrusted_full_pipeline_does_not_supersede_pytest_failure(self):
        with self.assertRaisesRegex(ValueError, 'successful validation exit'):
            self.validate([
                self.observation('failed', 'pytest tests/test_a.py -q', 1),
                self.observation('full', 'pytest -q 2>&1 | tail -10', 0),
            ])


if __name__ == '__main__':
    unittest.main()
