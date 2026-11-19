"""Check persisted runtime budget semantics against the pinned Harbor code."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
if HERE.name == "console-src":
    PROJECT = HERE.parent / "payload/Projects/QuantumManyBodyVariational"
sys.path.insert(0, str(PROJECT))
from revisions.seekgnomes_v1_2 import budget


class BudgetProfileTests(unittest.TestCase):
    def test_token_mode_and_closing(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            ledger = budget.Ledger(Path(directory) / "budget.json", "constructor",
                                       output_tokens=18000, active_seconds=None)
            first, cap = ledger.reserve_api({})
            self.assertEqual(cap, 16000)
            ledger.settle_api(first, {"completion_tokens": 16000})
            second, cap = ledger.reserve_api({})
            self.assertEqual(cap, 2000)
            ledger.settle_api(second, {"completion_tokens": 2000})
            self.assertEqual(ledger.exhausted(), "output_budget_exhausted")
            self.assertTrue(ledger.begin_closing(ledger.exhausted()))
            self.assertFalse(ledger.begin_closing("duplicate"))
            self.assertEqual(ledger.remaining()["output"], 50000)
            self.assertIsNone(ledger.remaining()["active_seconds"])
            recovered = budget.Ledger(ledger.path, "constructor", output_tokens=18000,
                                      active_seconds=None, resume=True)
            self.assertEqual(recovered.remaining(), ledger.remaining())

    def test_time_mode_and_pause(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            clock = [100]
            with patch.object(budget.time, "monotonic", side_effect=lambda: clock[0]):
                ledger = budget.Ledger(Path(directory) / "budget.json", "constructor",
                                           output_tokens=None, active_seconds=120)
                ledger.resume()
                clock[0] += 90
                ledger.pause()
                clock[0] += 1000
                self.assertEqual(ledger.remaining()["active_seconds"], 30)
                self.assertIsNone(ledger.remaining()["output"])
                ledger.resume()
                clock[0] += 30
                self.assertEqual(ledger.exhausted(), "active_seconds_budget_exhausted")
                self.assertTrue(ledger.begin_closing(ledger.exhausted()))
                self.assertEqual(ledger.remaining()["active_seconds"], 120)
                self.assertEqual(ledger.remaining()["output"], 50000)
                row = json.loads(ledger.path.read_text())
                self.assertIsNone(row["configured_limits"]["output"])
                recovered = budget.Ledger(ledger.path, "constructor", output_tokens=None,
                                          active_seconds=120, resume=True)
                self.assertEqual(recovered.remaining(), ledger.remaining())


if __name__ == "__main__":
    unittest.main()
