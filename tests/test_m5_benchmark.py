"""Small correctness checks for the M5 benchmark harness."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from experiments.m5_benchmark import _measure


class M5BenchmarkHarnessTests(unittest.TestCase):
    def test_one_and_all_change_have_exact_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            one = _measure(
                base,
                1,
                "one-change",
                5,
                python=Path(sys.executable),
                source_root=Path(__file__).parents[1] / "src",
            )
            all_changed = _measure(
                base,
                1,
                "all-changed",
                5,
                python=Path(sys.executable),
                source_root=Path(__file__).parents[1] / "src",
            )

        self.assertEqual(
            one["actions"], [{"name": "resource_0000", "action": "create"}]
        )
        self.assertEqual(
            all_changed["actions"],
            [{"name": "resource_0000", "action": "create"}],
        )
        self.assertEqual(len(one["warm_samples"]), 5)
        self.assertEqual(len(all_changed["warm_samples"]), 5)


if __name__ == "__main__":
    unittest.main()
