"""Budget fixtures exercise accounting, not provider billing equivalence."""
import tempfile
import unittest
from pathlib import Path

from simulator.openhands.budget import Budget


def config():
    price = dict(model='m', input_per_million=1, output_per_million=1)
    return dict(max_cost=.1, user={'model': 'm'}, code={'model': 'm'}, pricing={
        'source': 'test fixture', 'currency': 'USD', 'user': price, 'code': price})


class BudgetTests(unittest.TestCase):
    def test_pending_reservations_share_budget(self):
        budget = Budget(config())
        identifier = budget.before('user', {})
        with self.assertRaises(TimeoutError):
            budget.before('code', {})
        budget.after('user', dict(prompt_tokens=100, completion_tokens=100), identifier)
        budget.before('code', {})
        self.assertEqual(budget.snapshot()['attempts'], 2)

    def test_per_call_journal_wins_over_stale_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'budget.json'
            budget = Budget(config(), journal=path)
            old = budget.snapshot()
            identifier = budget.before('user', {})
            budget.after('user', dict(prompt_tokens=500, completion_tokens=100), identifier)
            restored = Budget(config(), old, path)
            self.assertEqual(restored.snapshot()['calls'], 1)
            self.assertAlmostEqual(restored.snapshot()['cost'], .0006)

    def test_uncertain_call_survives_crash_and_blocks_cost_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'budget.json'
            budget = Budget(config(), journal=path)
            budget.before('user', {})
            restored = Budget(config(), journal=path)
            with self.assertRaises(RuntimeError):
                restored.before('code', {})

    def test_invalid_prices_and_missing_old_journal_fail_closed(self):
        for value in (float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                Budget({**config(), 'max_cost': value})
        with self.assertRaises(RuntimeError):
            Budget(config(), {'attempts': 1})
