from __future__ import annotations

import io
import json
import os
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from luwu.cli import main
from luwu.errors import ApplyError
from luwu.reconcile import (
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
    reobserve_execution_record,
)
from tests.test_m3c_execution import _ExecutionProject


class M3cReplaceBoundaryTests(unittest.TestCase):
    def test_real_replace_then_raise_preserves_known_target_and_recovery_state(
        self,
    ) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            real_replace = cast(Callable[..., None], os.replace)

            def replace_then_raise(*args: object, **kwargs: object) -> None:
                real_replace(*args, **kwargs)
                if str(args[0]).startswith(".alpha.conf.luwu-"):
                    raise OSError("post-publish boundary failure")

            with (
                patch("luwu.reconcile.os.replace", side_effect=replace_then_raise),
                self.assertRaises(ApplyError) as context,
            ):
                execute_execution_plan(
                    build_plan(project.manifest), record_path, confirm=True
                )

            error = context.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertTrue(error.committed)
            execution = cast(dict[str, object], error.execution)
            self.assertEqual(execution["changed_targets"], ["targets/alpha.conf"])
            self.assertEqual(
                cast(list[dict[str, object]], execution["resources"])[0]["state"],
                "unknown",
            )
            self.assertEqual(
                project.targets["alpha"].read_text(encoding="utf-8"),
                "alpha-desired\n",
            )
            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "recovery_required")
            self.assertEqual(
                cast(list[dict[str, object]], journal["resources"])[0]["state"],
                "unknown",
            )
            self.assertEqual(
                reobserve_execution_record(record_path)["outcome"],
                "recovery_required",
            )
            self.assertEqual(self._temporary_entries(project.targets["alpha"]), [])

    def test_equal_external_target_is_indeterminate_and_not_a_changed_target(
        self,
    ) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            real_replace = os.replace

            def replace_with_equal_external_target(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                if not source_name.startswith(".alpha.conf.luwu-"):
                    real_replace(
                        source_name,
                        target_name,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )
                    return
                self._write_independent_target(
                    source_name, target_name, src_dir_fd, dst_dir_fd
                )
                raise OSError("independent equal target")

            with (
                patch(
                    "luwu.reconcile.os.replace",
                    side_effect=replace_with_equal_external_target,
                ),
                self.assertRaises(ApplyError) as context,
            ):
                execute_execution_plan(
                    build_plan(project.manifest), record_path, confirm=True
                )

            error = context.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertFalse(error.committed)
            execution = cast(dict[str, object], error.execution)
            self.assertEqual(execution["changed_targets"], [])
            self.assertEqual(
                cast(list[dict[str, object]], execution["resources"])[0]["state"],
                "unknown",
            )
            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "recovery_required")
            self.assertEqual(
                cast(list[dict[str, object]], journal["resources"])[0]["state"],
                "unknown",
            )
            self.assertEqual(
                project.targets["alpha"].read_text(encoding="utf-8"),
                "alpha-desired\n",
            )
            recovered = reobserve_execution_record(record_path)
            self.assertEqual(recovered["outcome"], "recovery_required")
            resource = cast(list[dict[str, object]], recovered["resources"])[0]
            self.assertEqual(resource["reobserved_state"], "matches_postcondition")
            self.assertEqual(self._temporary_entries(project.targets["alpha"]), [])

    def test_three_resources_stop_after_indeterminate_and_recover_read_only(
        self,
    ) -> None:
        with _ExecutionProject(
            ("alpha", "beta", "zeta"),
            targets={
                "alpha": "alpha-old\n",
                "beta": "beta-old\n",
                "zeta": "zeta-old\n",
            },
        ) as project:
            record_path = project.root / "journal.json"
            real_replace = os.replace
            calls = 0

            def replace_alpha_then_indeterminate_beta(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                nonlocal calls
                target_write = source_name.startswith(
                    (".alpha.conf.luwu-", ".beta.conf.luwu-", ".zeta.conf.luwu-")
                )
                if not target_write:
                    real_replace(
                        source_name,
                        target_name,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )
                    return
                calls += 1
                if calls == 1:
                    real_replace(
                        source_name,
                        target_name,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )
                    return
                if calls == 2:
                    self._write_independent_target(
                        source_name, target_name, src_dir_fd, dst_dir_fd
                    )
                    raise OSError("beta publication is indeterminate")
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch(
                    "luwu.reconcile.os.replace",
                    side_effect=replace_alpha_then_indeterminate_beta,
                ),
                self.assertRaises(ApplyError) as context,
            ):
                execute_execution_plan(
                    build_plan(project.manifest), record_path, confirm=True
                )

            error = context.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertTrue(error.committed)
            execution = cast(dict[str, object], error.execution)
            self.assertEqual(execution["changed_targets"], ["targets/alpha.conf"])
            self.assertEqual(
                [
                    item["state"]
                    for item in cast(list[dict[str, object]], execution["resources"])
                ],
                ["committed", "unknown", "not-attempted"],
            )
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-desired\n")
            self.assertEqual(project.targets["zeta"].read_text(), "zeta-old\n")
            journal = inspect_execution_record(record_path)
            self.assertEqual(
                [
                    item["state"]
                    for item in cast(list[dict[str, object]], journal["resources"])
                ],
                ["committed", "unknown", "not-attempted"],
            )
            recovered = reobserve_execution_record(record_path)
            self.assertEqual(recovered["outcome"], "recovery_required")
            self.assertEqual(
                [
                    item["reobserved_state"]
                    for item in cast(list[dict[str, object]], recovered["resources"])
                ],
                ["confirmed", "matches_postcondition", "not-attempted"],
            )
            self.assertEqual(calls, 2)

    def test_indeterminate_boundary_is_visible_in_json_and_human_cli(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "json-journal.json"
            with patch(
                "luwu.reconcile.os.replace",
                side_effect=self._indeterminate_side_effect(),
            ):
                status, stdout, stderr = _run_cli(project, record_path, as_json=True)
            payload = cast(dict[str, Any], json.loads(stdout))
            self.assertEqual(status, 2)
            self.assertEqual(payload["error"]["code"], "recovery_required")
            self.assertFalse(payload["execution"]["committed"])
            self.assertEqual(payload["execution"]["changed_targets"], [])
            self.assertEqual(payload["execution"]["resources"][0]["state"], "unknown")
            self.assertEqual(payload["journal"]["state"], "recovery_required")
            self.assertEqual(stderr, "")
            self.assertNotIn(project.source_values["alpha"], stdout)

        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "human-journal.json"
            with patch(
                "luwu.reconcile.os.replace",
                side_effect=self._indeterminate_side_effect(),
            ):
                status, stdout, stderr = _run_cli(project, record_path, as_json=False)
            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertIn("error[recovery_required]", stderr)
            self.assertIn("state=unknown", stderr)
            self.assertNotIn(project.source_values["alpha"], stderr)

    def _indeterminate_side_effect(self):
        real_replace = os.replace

        def replace_with_equal_external_target(
            source_name: str,
            target_name: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
        ) -> None:
            if not source_name.startswith(".alpha.conf.luwu-"):
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                return
            self._write_independent_target(
                source_name, target_name, src_dir_fd, dst_dir_fd
            )
            raise OSError("indeterminate publication")

        return replace_with_equal_external_target

    def _write_independent_target(
        self,
        source_name: str,
        target_name: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        data = _read_at(src_dir_fd, source_name)
        descriptor = os.open(target_name, os.O_RDONLY, dir_fd=dst_dir_fd)
        # Keep the old inode alive so unlink/recreate cannot reuse its identity.
        with os.fdopen(descriptor, "rb") as old_target:
            os.unlink(target_name, dir_fd=dst_dir_fd)
            descriptor = os.open(
                target_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=dst_dir_fd,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                target_info = os.fstat(handle.fileno())
                for info in (
                    os.fstat(old_target.fileno()),
                    os.stat(source_name, dir_fd=src_dir_fd, follow_symlinks=False),
                ):
                    self.assertNotEqual(
                        (target_info.st_dev, target_info.st_ino),
                        (info.st_dev, info.st_ino),
                    )

    @staticmethod
    def _temporary_entries(path: Path) -> list[Path]:
        return sorted(path.parent.glob(f".{path.name}.luwu-*"))


def _read_at(parent: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY, dir_fd=parent)
    try:
        return os.read(descriptor, 1024 * 1024)
    finally:
        os.close(descriptor)


def _run_cli(
    project: _ExecutionProject,
    record_path: Path,
    *,
    as_json: bool,
) -> tuple[int, str, str]:
    arguments = [
        "apply",
        "--manifest",
        str(project.manifest_path),
        "--record",
        str(record_path),
        "--yes",
    ]
    if as_json:
        arguments.append("--json")
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = main(arguments)
    return status, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
