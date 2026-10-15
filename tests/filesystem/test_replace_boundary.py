"""Tests for replace boundary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from luwu import baseline, mutations
from luwu.baseline import write_baseline
from luwu.cli import main
from luwu.errors import ApplyError, MutationError
from luwu.manifest import load_manifest
from luwu.mutations import accept_baseline, reverse_sync
from luwu.reconcile import (
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
    reobserve_execution_record,
)
from luwu.reverse_sync import SourcePatch
from tests.support.projects import ExecutionProject, FieldProject


def _read_at(parent: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY, dir_fd=parent)
    try:
        return os.read(descriptor, 1024 * 1024)
    finally:
        os.close(descriptor)


def _run_cli(
    project: ExecutionProject,
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


class ReplaceBoundaryV4Tests(unittest.TestCase):
    def test_baseline_replace_failure_before_publish_is_not_committed(self) -> None:
        with FieldProject() as project:
            with (
                patch(
                    "luwu.baseline.os.replace",
                    side_effect=OSError("replace did not start"),
                ),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="baseline_write_failed",
                committed=False,
                outcome="not_committed",
            )
            self.assertFalse(project.baseline.exists())
            self.assertEqual(self._temporary_entries(project.baseline), [])

    def test_baseline_replace_then_raise_uses_staged_identity(self) -> None:
        with FieldProject() as project:
            staged: dict[str, bytes] = {}
            real_replace = cast(Callable[..., None], os.replace)

            def replace_then_raise(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                staged["data"] = self._read_at(source_name, src_dir_fd)
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                raise OSError("post-publish failure")

            with (
                patch("luwu.baseline.os.replace", side_effect=replace_then_raise),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="baseline_state_unknown",
                committed=True,
                outcome="committed_state_unknown",
            )
            self.assertEqual(project.baseline.read_bytes(), staged["data"])
            self.assertEqual(self._temporary_entries(project.baseline), [])

    def test_baseline_equal_independent_target_is_indeterminate(self) -> None:
        with FieldProject() as project:
            staged: dict[str, object] = {}

            def replace_with_equal_independent_target(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                data = self._read_at(source_name, src_dir_fd)
                staged["identity"] = self._identity_at(src_dir_fd, source_name)
                # Create the independent inode before releasing the staged one.
                project.baseline.write_bytes(data)
                os.unlink(source_name, dir_fd=src_dir_fd)
                parent = os.open(project.baseline.parent, os.O_RDONLY)
                try:
                    staged["target_identity"] = self._identity_at(
                        parent, project.baseline.name
                    )
                finally:
                    os.close(parent)
                raise OSError("publish boundary is ambiguous")

            with (
                patch(
                    "luwu.baseline.os.replace",
                    side_effect=replace_with_equal_independent_target,
                ),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="baseline_state_unknown",
                committed=False,
                outcome="indeterminate",
            )
            self.assertTrue(project.baseline.read_bytes())
            self.assertNotEqual(staged["identity"], staged["target_identity"])
            self.assertEqual(self._temporary_entries(project.baseline), [])

    def test_baseline_same_old_bytes_with_consumed_temp_is_indeterminate(self) -> None:
        with FieldProject() as project:
            resource = load_manifest(project.manifest).resources[0]
            project.write_baseline({"setting": 1})
            before = project.baseline.read_bytes()

            def discard_temp_before_failure(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                os.unlink(source_name, dir_fd=src_dir_fd)
                raise OSError("replace outcome is unknown")

            with (
                patch(
                    "luwu.baseline.os.replace",
                    side_effect=discard_temp_before_failure,
                ),
                self.assertRaises(MutationError) as context,
            ):
                write_baseline(
                    project.root,
                    resource,
                    before,
                    expected_data=before,
                )

            self._assert_error(
                context.exception,
                code="baseline_state_unknown",
                committed=False,
                outcome="indeterminate",
            )
            self.assertEqual(project.baseline.read_bytes(), before)
            self.assertEqual(self._temporary_entries(project.baseline), [])

    def test_baseline_unobservable_parent_after_replace_then_raise_is_indeterminate(
        self,
    ) -> None:
        with FieldProject() as project:
            original_parent = project.root
            detached_parent = project.root.parent / f"{project.root.name}-detached"
            real_replace = cast(Callable[..., None], os.replace)

            def replace_in_detached_parent_then_raise(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                original_parent.rename(detached_parent)
                original_parent.mkdir()
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                raise OSError("parent path is no longer observable")

            with (
                patch(
                    "luwu.baseline.os.replace",
                    side_effect=replace_in_detached_parent_then_raise,
                ),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="baseline_state_unknown",
                committed=False,
                outcome="indeterminate",
            )
            self.assertFalse(project.baseline.exists())
            self.assertTrue((detached_parent / project.baseline.name).exists())
            self._cleanup_detached(detached_parent)

    def test_baseline_unlock_failure_after_replace_then_raise_keeps_commit_fact(
        self,
    ) -> None:
        with FieldProject() as project:
            real_replace = cast(Callable[..., None], os.replace)

            def replace_then_raise(*args: object, **kwargs: object) -> None:
                real_replace(*args, **kwargs)
                raise OSError("post-publish failure")

            with (
                patch("luwu.baseline.os.replace", side_effect=replace_then_raise),
                patch(
                    "luwu.baseline.unlock_directory",
                    side_effect=OSError("unlock failed"),
                ),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="baseline_state_unknown",
                committed=True,
                outcome="committed_state_unknown",
            )
            self.assertTrue(project.baseline.exists())
            self.assertEqual(self._temporary_entries(project.baseline), [])

    def test_baseline_close_failure_after_replace_then_raise_keeps_commit_fact(
        self,
    ) -> None:
        with FieldProject() as project:
            resource = load_manifest(project.manifest).resources[0]
            real_open_parent = baseline.open_parent_directory
            real_close = os.close
            parent_holder: dict[str, int] = {}
            real_replace = cast(Callable[..., None], os.replace)

            def capture_parent(root: Path, path: Path) -> tuple[int, str]:
                parent, name = real_open_parent(root, path)
                parent_holder["fd"] = parent
                return parent, name

            def close_then_raise(fd: int) -> None:
                if fd == parent_holder.get("fd"):
                    real_close(fd)
                    raise OSError("close failed")
                real_close(fd)

            def replace_then_raise(*args: object, **kwargs: object) -> None:
                real_replace(*args, **kwargs)
                raise OSError("post-publish failure")

            with (
                patch(
                    "luwu.baseline.open_parent_directory", side_effect=capture_parent
                ),
                patch("luwu.baseline.os.close", side_effect=close_then_raise),
                patch("luwu.baseline.os.replace", side_effect=replace_then_raise),
                self.assertRaises(MutationError) as context,
            ):
                write_baseline(
                    project.root,
                    resource,
                    b'{"accepted": true}\n',
                    expected_data=None,
                )

            self._assert_error(
                context.exception,
                code="baseline_state_unknown",
                committed=True,
                outcome="committed_state_unknown",
            )
            self.assertTrue(project.baseline.exists())

    def test_source_replace_failure_before_publish_is_not_committed(self) -> None:
        with FieldProject() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            before = project.source.read_bytes()
            with (
                patch(
                    "luwu.mutations.os.replace",
                    side_effect=OSError("replace did not start"),
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="source_write_failed",
                committed=False,
                outcome="not_committed",
            )
            self.assertEqual(project.source.read_bytes(), before)
            self.assertEqual(self._temporary_entries(project.source), [])

    def test_source_replace_then_raise_uses_staged_identity(self) -> None:
        with FieldProject() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            staged: dict[str, bytes] = {}
            real_replace = cast(Callable[..., None], os.replace)

            def replace_then_raise(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                staged["data"] = self._read_at(source_name, src_dir_fd)
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                raise OSError("post-publish failure")

            with (
                patch("luwu.mutations.os.replace", side_effect=replace_then_raise),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="source_state_unknown",
                committed=True,
                outcome="committed_state_unknown",
            )
            self.assertEqual(project.source.read_bytes(), staged["data"])
            self.assertEqual(self._temporary_entries(project.source), [])

    def test_source_equal_independent_target_is_indeterminate(self) -> None:
        with FieldProject() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            staged: dict[str, object] = {}

            def replace_with_equal_independent_target(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                data = self._read_at(source_name, src_dir_fd)
                staged["identity"] = self._identity_at(src_dir_fd, source_name)
                os.unlink(source_name, dir_fd=src_dir_fd)
                project.source.write_bytes(data)
                parent = os.open(project.source.parent, os.O_RDONLY)
                try:
                    staged["target_identity"] = self._identity_at(
                        parent, project.source.name
                    )
                finally:
                    os.close(parent)
                raise OSError("publish boundary is ambiguous")

            with (
                patch(
                    "luwu.mutations.os.replace",
                    side_effect=replace_with_equal_independent_target,
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="source_state_unknown",
                committed=False,
                outcome="indeterminate",
            )
            self.assertNotEqual(staged["identity"], staged["target_identity"])
            self.assertEqual(self._temporary_entries(project.source), [])

    def test_source_same_old_bytes_with_consumed_temp_is_indeterminate(self) -> None:
        with FieldProject() as project:
            resource = load_manifest(project.manifest).resources[0]
            before = project.source.read_bytes()
            source_identity = project.source.stat()
            patch_data = SourcePatch(
                data=before,
                fields=("runtime",),
                source_keys=("runtime",),
            )

            def discard_temp_before_failure(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                os.unlink(source_name, dir_fd=src_dir_fd)
                raise OSError("replace outcome is unknown")

            with (
                patch(
                    "luwu.mutations.os.replace",
                    side_effect=discard_temp_before_failure,
                ),
                self.assertRaises(MutationError) as context,
            ):
                mutations._write_source(
                    project.root,
                    resource,
                    patch_data,
                    expected_identity=(source_identity.st_dev, source_identity.st_ino),
                    expected_digest=hashlib.sha256(before).hexdigest(),
                    check_inputs=lambda: None,
                )

            self._assert_error(
                context.exception,
                code="source_state_unknown",
                committed=False,
                outcome="indeterminate",
            )
            self.assertEqual(project.source.read_bytes(), before)
            self.assertEqual(self._temporary_entries(project.source), [])

    def test_source_cleanup_failure_after_indeterminate_does_not_become_not_committed(
        self,
    ) -> None:
        with FieldProject() as project:
            project.write_baseline({"setting": 1, "runtime": 1})

            def independent_target_with_temp_left(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                data = self._read_at(source_name, src_dir_fd)
                project.source.unlink()
                project.source.write_bytes(data)
                raise OSError("publish boundary is ambiguous")

            with (
                patch(
                    "luwu.mutations.os.replace",
                    side_effect=independent_target_with_temp_left,
                ),
                patch(
                    "luwu.mutations._remove_source_temporary",
                    side_effect=OSError("cleanup failed"),
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="source_state_unknown",
                committed=False,
                outcome="indeterminate",
            )
            self.assertTrue(self._temporary_entries(project.source))

    def test_source_unlock_failure_after_replace_then_raise_keeps_commit_fact(
        self,
    ) -> None:
        with FieldProject() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            real_replace = cast(Callable[..., None], os.replace)

            def replace_then_raise(*args: object, **kwargs: object) -> None:
                real_replace(*args, **kwargs)
                raise OSError("post-publish failure")

            with (
                patch("luwu.mutations.os.replace", side_effect=replace_then_raise),
                patch(
                    "luwu.mutations.unlock_directory",
                    side_effect=OSError("unlock failed"),
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self._assert_error(
                context.exception,
                code="source_state_unknown",
                committed=True,
                outcome="committed_state_unknown",
            )
            self.assertEqual(self._temporary_entries(project.source), [])

    def _assert_error(
        self,
        error: MutationError,
        *,
        code: str,
        committed: bool,
        outcome: str,
    ) -> None:
        self.assertEqual(error.code, code)
        self.assertEqual(error.committed, committed)
        self.assertEqual(error.outcome, outcome)

    @staticmethod
    def _read_at(name: str, parent: int) -> bytes:
        descriptor = os.open(name, os.O_RDONLY, dir_fd=parent)
        try:
            return os.read(descriptor, 1024 * 1024)
        finally:
            os.close(descriptor)

    @staticmethod
    def _identity_at(parent: int, name: str) -> tuple[int, int, int]:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        return info.st_dev, info.st_ino, info.st_mode & 0o170000

    @staticmethod
    def _temporary_entries(path: Path) -> list[Path]:
        return sorted(path.parent.glob(f".{path.name}.luwu-*"))

    @staticmethod
    def _cleanup_detached(path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                ReplaceBoundaryV4Tests._cleanup_detached(child)
            else:
                child.unlink()
        path.rmdir()


class ReplaceBoundaryV5Tests(unittest.TestCase):
    def test_real_replace_then_raise_preserves_known_target_and_recovery_state(
        self,
    ) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
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
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
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
        with ExecutionProject(
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
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
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

        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
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
