"""Small correctness checks for the M5 benchmark harness."""

from __future__ import annotations

import json
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import experiments.m5_benchmark as benchmark
import luwu
from experiments.m5_benchmark import (
    _assert_prepared_scenario,
    _measure,
    _probe_python,
    _timed_sample,
    _write_fixture,
    main,
)


def _source_root_for_current_import() -> Path | None:
    checkout_source = Path(__file__).resolve().parents[1] / "src"
    try:
        Path(luwu.__file__).resolve().relative_to(checkout_source.resolve())
    except ValueError:
        return None
    return checkout_source


def _fake_row(size: int, scenario: str) -> dict[str, object]:
    return {
        "size": size,
        "scenario": scenario,
        "first": 0.1,
        "samples": [0.1] * 5,
        "warm_samples": [0.1] * 5,
        "median": 0.1,
        "maximum": 0.1,
        "actions": [],
    }


def _fake_runtime(version: str) -> dict[str, str]:
    return {
        "python_version": version,
        "python_prefix": sys.prefix,
        "python_site_packages": str(Path(sysconfig.get_path("purelib")).resolve()),
        "luwu_file": "/installed/luwu/__init__.py",
        "luwu_version": "0.1.0",
    }


class M5BenchmarkHarnessTests(unittest.TestCase):
    def test_prepared_scenario_rejects_incomplete_resource_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _write_fixture(
                Path(directory),
                2,
                python=Path(sys.executable),
                source_root=_source_root_for_current_import(),
            )
            invalid_action_sets = (
                (
                    "missing",
                    (("resource_0000", "create"),),
                    "missing=\\['resource_0001'\\]",
                ),
                (
                    "duplicate",
                    (
                        ("resource_0000", "create"),
                        ("resource_0000", "create"),
                    ),
                    "duplicate=\\['resource_0000'\\]",
                ),
                (
                    "extra",
                    (
                        ("resource_0000", "create"),
                        ("resource_0001", "create"),
                        ("unexpected", "create"),
                    ),
                    "extra=\\['unexpected'\\]",
                ),
            )
            for label, actions, message in invalid_action_sets:
                with (
                    self.subTest(label=label),
                    patch.object(benchmark, "_plan_actions", return_value=actions),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    _assert_prepared_scenario(fixture, "fresh")

    def test_timed_sample_rejects_zero_exit_without_durable_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real_run_capture = benchmark._run_capture

            def fake_success(fixture, *args):
                if args[0] != "apply":
                    return real_run_capture(fixture, *args)
                record_path = Path(args[args.index("--record") + 1])
                resources = [
                    {"name": Path(name).stem, "state": "committed"}
                    for name in fixture.target_names
                ]
                payload = {
                    "state": "committed",
                    "outcome": "committed",
                    "preview": {
                        "resources": [
                            {"name": Path(name).stem, "action": "create"}
                            for name in fixture.target_names
                        ]
                    },
                    "target_names": fixture.target_names,
                    "changed_targets": fixture.target_names,
                    "journal": {
                        "path": str(record_path),
                        "created": True,
                        "manifest_version": 5,
                        "state": "committed",
                        "resources": resources,
                    },
                }
                return CompletedProcess(
                    args=fixture.command(*args),
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )

            with (
                patch.object(benchmark, "_run_capture", side_effect=fake_success),
                self.assertRaisesRegex(RuntimeError, "journal was not created"),
            ):
                _timed_sample(
                    base,
                    2,
                    "fresh",
                    python=Path(sys.executable),
                    source_root=_source_root_for_current_import(),
                )

    def test_timed_sample_accepts_complete_successful_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            elapsed, actions = _timed_sample(
                Path(directory),
                2,
                "fresh",
                python=Path(sys.executable),
                source_root=_source_root_for_current_import(),
            )

        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(
            actions,
            (("resource_0000", "create"), ("resource_0001", "create")),
        )

    def test_recovery_sample_checks_setup_create_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            elapsed, actions = _timed_sample(
                Path(directory),
                2,
                "recovery",
                python=Path(sys.executable),
                source_root=_source_root_for_current_import(),
            )

        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(
            actions,
            (("resource_0000", "noop"), ("resource_0001", "noop")),
        )

    def test_one_and_all_change_have_exact_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            one = _measure(
                base,
                1,
                "one-change",
                5,
                python=Path(sys.executable),
                source_root=_source_root_for_current_import(),
            )
            all_changed = _measure(
                base,
                1,
                "all-changed",
                5,
                python=Path(sys.executable),
                source_root=_source_root_for_current_import(),
            )

        self.assertEqual(
            one["actions"], [{"name": "resource_0000", "action": "create"}]
        )
        self.assertEqual(
            all_changed["actions"],
            [{"name": "resource_0000", "action": "create"}],
        )
        one_warm_samples = one["warm_samples"]
        all_changed_warm_samples = all_changed["warm_samples"]
        assert isinstance(one_warm_samples, list)
        assert isinstance(all_changed_warm_samples, list)
        self.assertEqual(len(one_warm_samples), 5)
        self.assertEqual(len(all_changed_warm_samples), 5)

    def test_python_probe_reports_the_target_interpreter_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _probe_python(
                Path(sys.executable),
                scratch=Path(directory),
                source_root=_source_root_for_current_import(),
                installed=False,
            )

        self.assertEqual(runtime["python_version"], sys.version.split()[0])
        self.assertEqual(runtime["python_prefix"], sys.prefix)
        self.assertEqual(
            Path(runtime["python_site_packages"]).resolve(),
            Path(sysconfig.get_path("purelib")).resolve(),
        )
        self.assertEqual(runtime["luwu_version"], luwu.__version__)
        self.assertEqual(
            Path(runtime["luwu_file"]).resolve(), Path(luwu.__file__).resolve()
        )

    def test_installed_probe_rejects_checkout_source_import(self) -> None:
        checkout_file = (
            Path(__file__).resolve().parents[1] / "src" / "luwu" / "__init__.py"
        )
        completed = CompletedProcess(
            args=[str(sys.executable)],
            returncode=0,
            stdout=json.dumps(
                {
                    "python_version": "3.12.0",
                    "python_prefix": "/target/venv",
                    "python_site_packages": (
                        "/target/venv/lib/python3.12/site-packages"
                    ),
                    "luwu_file": str(checkout_file),
                    "luwu_version": "0.1.0",
                }
            ),
            stderr="",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("experiments.m5_benchmark.subprocess.run", return_value=completed),
            self.assertRaisesRegex(RuntimeError, "checkout source tree"),
        ):
            _probe_python(
                Path(sys.executable),
                scratch=Path(directory),
                source_root=None,
                installed=True,
            )

    def test_installed_probe_rejects_other_editable_imports(self) -> None:
        completed = CompletedProcess(
            args=[str(sys.executable)],
            returncode=0,
            stdout=json.dumps(
                {
                    "python_version": "3.12.0",
                    "python_prefix": "/target/venv",
                    "python_site_packages": (
                        "/target/venv/lib/python3.12/site-packages"
                    ),
                    "luwu_file": "/other-checkout/src/luwu/__init__.py",
                    "luwu_version": "0.1.0",
                }
            ),
            stderr="",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("experiments.m5_benchmark.subprocess.run", return_value=completed),
            self.assertRaisesRegex(
                RuntimeError, "outside the target Python site-packages"
            ),
        ):
            _probe_python(
                Path(sys.executable),
                scratch=Path(directory),
                source_root=None,
                installed=True,
            )

    def test_main_preserves_venv_python_symlink_for_probe_and_measure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            python_link = base / "venv" / "bin" / "python"
            python_link.parent.mkdir(parents=True)
            python_link.symlink_to(sys.executable)
            expected_python = python_link.expanduser().absolute()
            with (
                patch(
                    "experiments.m5_benchmark._probe_python",
                    return_value=_fake_runtime("3.12.7"),
                ) as probe,
                patch(
                    "experiments.m5_benchmark._measure",
                    return_value=_fake_row(1, "plan"),
                ) as measure,
            ):
                main(
                    [
                        "--scratch",
                        str(base),
                        "--python",
                        str(python_link),
                        "--size",
                        "1",
                        "--scenario",
                        "plan",
                        "--output",
                        str(base / "benchmark.json"),
                    ]
                )

            self.assertEqual(probe.call_args.args[0], expected_python)
            self.assertEqual(measure.call_args.kwargs["python"], expected_python)
            self.assertNotEqual(probe.call_args.args[0], python_link.resolve())
            self.assertNotEqual(
                measure.call_args.kwargs["python"], python_link.resolve()
            )

    def test_output_checkpoints_completed_rows_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / "benchmark.json"
            runtime = _fake_runtime("3.12.7")
            with (
                patch(
                    "experiments.m5_benchmark._probe_python",
                    return_value=runtime,
                ),
                patch(
                    "experiments.m5_benchmark._measure",
                    side_effect=[_fake_row(1, "plan"), RuntimeError("interrupted")],
                ),
                self.assertRaisesRegex(RuntimeError, "interrupted"),
            ):
                main(
                    [
                        "--scratch",
                        str(base),
                        "--size",
                        "1",
                        "--size",
                        "2",
                        "--scenario",
                        "plan",
                        "--output",
                        str(output),
                    ]
                )

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertIs(payload["complete"], False)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["size"], 1)
        self.assertEqual(payload["environment"]["python_version"], "3.12.7")

    def test_completed_matrix_marks_output_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / "benchmark.json"
            runtime = _fake_runtime("3.13.2")
            with (
                patch(
                    "experiments.m5_benchmark._probe_python",
                    return_value=runtime,
                ),
                patch(
                    "experiments.m5_benchmark._measure",
                    return_value=_fake_row(1, "plan"),
                ),
            ):
                main(
                    [
                        "--scratch",
                        str(base),
                        "--size",
                        "1",
                        "--scenario",
                        "plan",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertIs(payload["complete"], True)
        self.assertEqual(payload["environment"]["python_version"], "3.13.2")
        self.assertEqual(len(payload["results"]), 1)


if __name__ == "__main__":
    unittest.main()
