import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from witdem_langfuse.quota import SharedBudget


class QuotaTest(unittest.TestCase):
    def test_concurrent_reservations_share_one_slot_and_restart_keeps_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.sqlite"
            budgets = [SharedBudget(path, "org", clock=lambda: 1000) for _ in range(8)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda b: b.acquire(), budgets))
            self.assertEqual(results.count(0), 1)
            self.assertEqual(results.count(1002), 7)
            budgets[0].defer_until(1120)
            restarted = SharedBudget(path, "org", clock=lambda: 1010)
            self.assertEqual(restarted.acquire(), 1120)
            restarted.defer_until(1050)
            self.assertEqual(restarted.acquire(), 1120)
            self.assertEqual(SharedBudget(path, "org", clock=lambda: 1120).acquire(), 0)

    def test_distinct_groups_and_conflicting_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.sqlite"
            self.assertEqual(SharedBudget(path, "one", clock=lambda: 1000).acquire(), 0)
            self.assertEqual(SharedBudget(path, "two", clock=lambda: 1000).acquire(), 0)
            with self.assertRaises(ValueError):
                SharedBudget(path, "one", requests_per_minute=300)
