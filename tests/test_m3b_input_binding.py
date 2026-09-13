from __future__ import annotations

import hashlib
import json
import os
import unittest
from collections.abc import Mapping
from unittest.mock import patch

from luwu import mutations, reconcile
from luwu.errors import MutationError
from luwu.manifest import load_manifest
from luwu.mutations import reverse_sync
from luwu.ownership import OwnershipResult
from tests.test_m3b import _Project


class M3bInputBindingTests(unittest.TestCase):
    def test_observation_digest_is_bound_to_bytes_passed_to_classifier(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            manifest = load_manifest(project.manifest)
            observed: dict[str, bytes] = {}
            real_classify = reconcile.classify_fields

            def classify(
                desired: bytes,
                live: bytes,
                *,
                fields: Mapping[str, str],
                baseline: bytes | None,
                resource_name: str,
                source_name: str,
                target_name: str,
            ) -> OwnershipResult:
                assert baseline is not None
                observed["baseline"] = baseline
                project.write_baseline({"setting": 1, "runtime": 99})
                return real_classify(
                    desired,
                    live,
                    fields=fields,
                    baseline=baseline,
                    resource_name=resource_name,
                    source_name=source_name,
                    target_name=target_name,
                )

            with patch("luwu.reconcile.classify_fields", side_effect=classify):
                plan = reconcile.build_plan(manifest)

            observation = plan.observations[0]
            self.assertEqual(
                observation.baseline_digest,
                hashlib.sha256(observed["baseline"]).hexdigest(),
            )
            self.assertNotEqual(
                observation.baseline_digest,
                hashlib.sha256(project.baseline.read_bytes()).hexdigest(),
            )
            serialized = json.dumps(
                reconcile.plan_to_dict(plan, command="test"), sort_keys=True
            )
            self.assertNotIn("baseline_digest", repr(observation))
            self.assertNotIn("baseline_digest", serialized)
            self.assertNotIn(observation.baseline_digest, serialized)

    def test_reverse_sync_rejects_planner_baseline_different_from_disk(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 3})
            baseline_on_disk = project.baseline.read_bytes()
            project.write_baseline({"setting": 1, "runtime": 1})
            planner_baseline = project.baseline.read_bytes()
            project.baseline.write_bytes(baseline_on_disk)
            source_before = project.source.read_bytes()

            def read_planner_snapshot(resource: object, *, root: object):
                del resource, root
                return planner_baseline, None

            with (
                patch(
                    "luwu.reconcile._read_baseline",
                    side_effect=read_planner_snapshot,
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(project.source.read_bytes(), source_before)

    def test_baseline_change_before_replace_is_stale_without_source_write(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            source_before = project.source.read_bytes()
            real_create = mutations.create_temporary_file

            def create_temp(parent: int, *, prefix: str):
                descriptor, name = real_create(parent, prefix=prefix)
                project.write_baseline({"setting": 1, "runtime": 8})
                return descriptor, name

            with (
                patch("luwu.mutations.create_temporary_file", side_effect=create_temp),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(project.source.read_bytes(), source_before)

    def test_baseline_missing_before_replace_is_stale_without_source_write(
        self,
    ) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            source_before = project.source.read_bytes()
            real_create = mutations.create_temporary_file

            def create_temp(parent: int, *, prefix: str):
                descriptor, name = real_create(parent, prefix=prefix)
                project.baseline.unlink()
                return descriptor, name

            with (
                patch("luwu.mutations.create_temporary_file", side_effect=create_temp),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(project.source.read_bytes(), source_before)

    def test_unsafe_baseline_before_replace_is_rejected_without_source_write(
        self,
    ) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            source_before = project.source.read_bytes()
            real_create = mutations.create_temporary_file

            def create_temp(parent: int, *, prefix: str):
                descriptor, name = real_create(parent, prefix=prefix)
                project.baseline.unlink()
                project.baseline.symlink_to(project.source)
                return descriptor, name

            with (
                patch("luwu.mutations.create_temporary_file", side_effect=create_temp),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(project.source.read_bytes(), source_before)

    def test_manifest_change_during_temp_write_is_stale_without_source_write(
        self,
    ) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            source_before = project.source.read_bytes()
            original_manifest = project.manifest.read_text(encoding="utf-8")
            real_create = mutations.create_temporary_file

            def create_temp(parent: int, *, prefix: str):
                descriptor, name = real_create(parent, prefix=prefix)
                project.manifest.write_text(
                    original_manifest + "\n# changed during temp write\n",
                    encoding="utf-8",
                )
                return descriptor, name

            with (
                patch("luwu.mutations.create_temporary_file", side_effect=create_temp),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(project.source.read_bytes(), source_before)

    def test_baseline_change_after_replace_reports_unknown_commit(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            real_replace = os.replace

            def replace_with_baseline_race(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                project.write_baseline({"setting": 1, "runtime": 7})
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch(
                    "luwu.mutations.os.replace",
                    side_effect=replace_with_baseline_race,
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "source_state_unknown")
            self.assertTrue(context.exception.committed)
            self.assertEqual(
                json.loads(project.source.read_text(encoding="utf-8"))["runtime"],
                2,
            )

    def test_baseline_change_during_directory_sync_is_reported_after_commit(
        self,
    ) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            real_sync = mutations.sync_directory

            def sync_with_baseline_race(parent: int) -> None:
                project.write_baseline({"setting": 1, "runtime": 6})
                real_sync(parent)

            with patch(
                "luwu.mutations.sync_directory", side_effect=sync_with_baseline_race
            ):
                result = reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(result.outcome, "committed_but_verification_failed")
            self.assertTrue(result.applied)

    def test_manifest_change_after_replace_reports_unknown_commit(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            original_manifest = project.manifest.read_text(encoding="utf-8")
            real_replace = os.replace

            def replace_with_manifest_race(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                project.manifest.write_text(
                    original_manifest + "\n# changed after replace\n",
                    encoding="utf-8",
                )
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch(
                    "luwu.mutations.os.replace",
                    side_effect=replace_with_manifest_race,
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "source_state_unknown")
            self.assertTrue(context.exception.committed)


if __name__ == "__main__":
    unittest.main()
