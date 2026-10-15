"""Tests for pilot write safety."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from luwu import m6_pilot
from luwu.m6_pilot import (
    M6PilotError,
    M6WriteError,
    accept_m6_baseline,
    apply_m6_forward,
    build_m6_plan,
)
from luwu.manifest import Manifest, load_manifest


class PilotWriteSafetyV7Tests(unittest.TestCase):
    def test_parent_rebinding_after_staging_prevents_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "home"
            parent.mkdir()
            target = parent / "pi.json"
            before = b'{"model":"pi-4"}\n'
            target.write_bytes(before)
            info = target.stat()
            moved_parent = root / "home.moved"
            original_fsync = os.fsync
            calls = 0

            def rebind_after_stage(descriptor: int) -> None:
                nonlocal calls
                original_fsync(descriptor)
                calls += 1
                if calls == 1:
                    parent.rename(moved_parent)
                    parent.mkdir()
                    (parent / target.name).write_bytes(before)

            with (
                patch.object(m6_pilot.os, "fsync", side_effect=rebind_after_stage),
                self.assertRaises(M6WriteError) as raised,
            ):
                m6_pilot._write_atomic(
                    root,
                    target,
                    b'{"model":"pi-5"}\n',
                    expected_identity=(info.st_dev, info.st_ino),
                    expected_digest=hashlib.sha256(before).hexdigest(),
                )

            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual((moved_parent / target.name).read_bytes(), before)
            self.assertEqual(list(moved_parent.glob(".luwu-m6-*")), [])

    def test_staged_symlink_or_replacement_inode_is_rejected(self) -> None:
        for replacement in ("symlink", "regular"):
            with (
                self.subTest(replacement=replacement),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                parent = root / "home"
                parent.mkdir()
                target = parent / "pi.json"
                before = b'{"model":"pi-4"}\n'
                target.write_bytes(before)
                info = target.stat()
                original_create = m6_pilot.create_temporary_file
                original_fsync = os.fsync
                staged: dict[str, object] = {}

                def remember_staged(
                    parent_fd: int,
                    prefix: str,
                    _original_create=original_create,
                    _staged=staged,
                ) -> tuple[int, str]:
                    descriptor, name = _original_create(parent_fd, prefix)
                    _staged.update(parent_fd=parent_fd, name=name)
                    return descriptor, name

                def substitute_after_stage(
                    descriptor: int,
                    _original_fsync=original_fsync,
                    _staged=staged,
                    _replacement=replacement,
                    _target=target,
                ) -> None:
                    _original_fsync(descriptor)
                    name = _staged["name"]
                    parent_fd = _staged["parent_fd"]
                    assert isinstance(name, str) and isinstance(parent_fd, int)
                    os.unlink(name, dir_fd=parent_fd)
                    if _replacement == "symlink":
                        os.symlink(_target.name, name, dir_fd=parent_fd)
                    else:
                        foreign_fd = os.open(
                            name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=parent_fd,
                        )
                        try:
                            os.write(foreign_fd, b"foreign staged content")
                        finally:
                            os.close(foreign_fd)

                with (
                    patch.object(
                        m6_pilot, "create_temporary_file", side_effect=remember_staged
                    ),
                    patch.object(
                        m6_pilot.os, "fsync", side_effect=substitute_after_stage
                    ),
                    self.assertRaises(M6WriteError) as raised,
                ):
                    m6_pilot._write_atomic(
                        root,
                        target,
                        b'{"model":"pi-5"}\n',
                        expected_identity=(info.st_dev, info.st_ino),
                        expected_digest=hashlib.sha256(before).hexdigest(),
                    )

                self.assertEqual(raised.exception.state, "not_replaced")
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(list(parent.glob(".luwu-m6-*")), [])

    def test_target_mode_change_during_staging_is_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            self._accept_all(root)
            model_input = root / "inputs/global/model.json"
            model_input.write_text('{"model":"pi-5"}\n', encoding="utf-8")
            target = root / "home/pi.json"
            before = target.read_bytes()
            target.chmod(0o644)
            original_fsync = os.fsync
            calls = 0

            def change_mode_after_stage(descriptor: int) -> None:
                nonlocal calls
                original_fsync(descriptor)
                calls += 1
                if calls == 1:
                    target.chmod(0o600)

            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            with patch.object(
                m6_pilot.os, "fsync", side_effect=change_mode_after_stage
            ):
                result = apply_m6_forward(plan, confirm=True)

            self.assertEqual(result.outcome, "attempted_not_replaced")
            self.assertEqual(result.not_replaced, ("pi.json",))
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_forward_rechecks_input_authority_after_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            self._accept_all(root)
            model_input = root / "inputs/global/model.json"
            model_input.write_text('{"model":"pi-5"}\n', encoding="utf-8")
            manifest = load_manifest(root / "luwu.toml")
            plan = build_m6_plan(manifest)
            target = root / "home/pi.json"
            before = target.read_bytes()
            original_fsync = os.fsync
            calls = 0

            def change_input_after_stage(descriptor: int) -> None:
                nonlocal calls
                original_fsync(descriptor)
                calls += 1
                if calls == 1:
                    model_input.write_text('{"model":"pi-6"}\n', encoding="utf-8")

            with patch.object(
                m6_pilot.os, "fsync", side_effect=change_input_after_stage
            ):
                result = apply_m6_forward(plan, confirm=True)

            self.assertEqual(result.outcome, "attempted_not_replaced")
            self.assertEqual(result.not_replaced, ("pi.json",))
            self.assertEqual(target.read_bytes(), before)

    def test_forward_post_replace_input_change_preserves_commit_as_unverified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            self._accept_all(root)
            model_input = root / "inputs/global/model.json"
            model_input.write_text('{"model":"pi-5"}\n', encoding="utf-8")
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            original_input = model_input.read_bytes()
            target = root / "home/pi.json"
            original_sync = m6_pilot.sync_directory

            def change_input_after_replacement(parent: int) -> None:
                original_sync(parent)
                model_input.write_bytes(original_input + b" ")

            with patch.object(
                m6_pilot,
                "sync_directory",
                side_effect=change_input_after_replacement,
            ):
                result = apply_m6_forward(plan, confirm=True)

            self.assertEqual(result.outcome, "committed_but_verification_failed")
            self.assertEqual(result.committed, ("pi.json",))
            self.assertEqual(result.attempted, ("pi.json",))
            self.assertEqual(result.uncertain, ())
            self.assertEqual(result.verification_status, "failed")
            self.assertEqual(json.loads(target.read_text())["model"], "pi-5")
            self.assertNotEqual(model_input.read_bytes(), original_input)

    def test_replaced_manifest_without_loader_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            original = load_manifest(root / "luwu.toml")
            undeclared = root / "inputs/baselines/undeclared.json"
            forged_resource = replace(
                original.resources[0],
                baseline=undeclared,
                baseline_name="baselines/undeclared.json",
            )
            forged_manifest = replace(original, resources=(forged_resource,))
            handbuilt_manifest = Manifest(
                version=original.version,
                path=original.path,
                root=original.root,
                resources=original.resources,
                content_digest=original.content_digest,
                capabilities=original.capabilities,
                roots=original.roots,
            )

            for candidate in (handbuilt_manifest, forged_manifest):
                with self.subTest(manifest=candidate):
                    with self.assertRaises(M6PilotError) as raised:
                        accept_m6_baseline(
                            candidate,
                            resource_name="pi",
                            fields=("model", "provider", "changelog"),
                            value_from="desired",
                            confirm=True,
                        )

                    self.assertEqual(
                        raised.exception.code, "invalid_manifest_provenance"
                    )
            self.assertFalse(undeclared.exists())
            self.assertFalse((root / "inputs/baselines/pi.json").exists())

    def test_mutation_rejects_tampered_m6_plan_resource_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            self._accept_all(root)
            (root / "inputs/global/model.json").write_text(
                '{"model":"pi-5"}\n', encoding="utf-8"
            )
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            target = root / "home/pi.json"
            original_target = target.read_bytes()
            undeclared = root / "inputs/baselines/undeclared.json"
            forged_resource = replace(
                plan.resource.resource,
                baseline=undeclared,
                baseline_name="baselines/undeclared.json",
            )
            forged_item = replace(plan.resource, resource=forged_resource)
            forged_plan = replace(plan, resources=(forged_item,))

            with self.assertRaises(M6PilotError) as raised:
                apply_m6_forward(forged_plan, confirm=True)

            self.assertEqual(raised.exception.code, "m6_stale_plan")
            self.assertEqual(target.read_bytes(), original_target)
            self.assertFalse(undeclared.exists())

    def test_forward_rejects_manifest_change_between_fresh_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            self._accept_all(root)
            model_input = root / "inputs/global/model.json"
            model_input.write_text('{"model":"pi-5"}\n', encoding="utf-8")
            alternate_input = root / "inputs/global/alternate-model.json"
            alternate_input.write_text('{"model":"pi-6"}\n', encoding="utf-8")
            manifest_path = root / "luwu.toml"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            plan = build_m6_plan(load_manifest(manifest_path))
            target = root / "home/pi.json"
            original_target = target.read_bytes()
            original_load = m6_pilot.load_manifest
            calls = 0

            def load_during_transient_manifest_change(
                path: Path,
            ) -> m6_pilot.Manifest:
                nonlocal calls
                calls += 1
                if calls == 2:
                    manifest_path.write_text(
                        manifest_text.replace(
                            "global/model.json", "global/alternate-model.json"
                        ),
                        encoding="utf-8",
                    )
                    try:
                        return original_load(path)
                    finally:
                        manifest_path.write_text(manifest_text, encoding="utf-8")
                return original_load(path)

            with (
                patch.object(
                    m6_pilot,
                    "load_manifest",
                    side_effect=load_during_transient_manifest_change,
                ),
                self.assertRaises(M6PilotError) as raised,
            ):
                apply_m6_forward(plan, confirm=True)

            self.assertEqual(raised.exception.code, "m6_stale_plan")
            self.assertEqual(target.read_bytes(), original_target)

    def test_unlock_failure_reports_committed_uncertain_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            self._accept_all(root)
            (root / "inputs/global/model.json").write_text(
                '{"model":"pi-5"}\n', encoding="utf-8"
            )
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))

            with patch.object(
                m6_pilot, "unlock_directory", side_effect=OSError("private payload")
            ):
                result = apply_m6_forward(plan, confirm=True)

            self.assertEqual(result.outcome, "committed_state_unknown")
            self.assertEqual(result.committed, ("pi.json",))
            self.assertEqual(result.attempted, ("pi.json",))
            self.assertEqual(result.uncertain, ("pi.json",))
            self.assertEqual(result.verification_status, "unknown")
            self.assertEqual(
                json.loads((root / "home/pi.json").read_text())["model"], "pi-5"
            )

    def test_temporary_cleanup_failure_is_not_reported_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "home"
            parent.mkdir()
            target = parent / "pi.json"
            before = b'{"model":"pi-4"}\n'
            target.write_bytes(before)
            info = target.stat()
            original_unlink = os.unlink

            def reject_stale() -> None:
                raise M6PilotError("stale", code="m6_stale_plan")

            def fail_temporary_unlink(name: str, *, dir_fd: int | None = None) -> None:
                if isinstance(name, str) and name.startswith(".luwu-m6-"):
                    raise OSError("private payload")
                original_unlink(name, dir_fd=dir_fd)

            with (
                patch.object(m6_pilot.os, "unlink", side_effect=fail_temporary_unlink),
                self.assertRaises(M6WriteError) as raised,
            ):
                m6_pilot._write_atomic(
                    root,
                    target,
                    b'{"model":"pi-5"}\n',
                    expected_identity=(info.st_dev, info.st_ino),
                    expected_digest=hashlib.sha256(before).hexdigest(),
                    check_inputs=reject_stale,
                )

            self.assertEqual(raised.exception.code, "m6_cleanup_failed")
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertEqual(target.read_bytes(), before)

    def test_forward_removal_verifies_absent_against_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            source = root / "repo/pi.json.j2"
            source.write_text(
                '{"model":"{{ global_inputs.model }}",'
                '"provider":"{{ local_inputs.provider }}",'
                '"changelog":null}\n',
                encoding="utf-8",
            )
            target = root / "home/pi.json"
            live = json.loads(target.read_text())
            live["changelog"] = None
            target.write_text(json.dumps(live) + "\n", encoding="utf-8")
            self._accept_all(root)
            source.write_text(
                '{"model":"{{ global_inputs.model }}",'
                '"provider":"{{ local_inputs.provider }}"}\n',
                encoding="utf-8",
            )
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertEqual(plan.resource.forward_fields, ("changelog",))

            result = apply_m6_forward(plan, confirm=True)

            self.assertEqual(result.outcome, "committed")
            self.assertNotIn("changelog", json.loads(target.read_text()))

    def test_json_boolean_and_number_remain_distinct_for_forward_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            source = root / "repo/pi.json.j2"
            source.write_text(
                '{"model":true,"provider":"{{ local_inputs.provider }}",'
                '"changelog":"{{ local_inputs.changelog }}"}\n',
                encoding="utf-8",
            )
            target = root / "home/pi.json"
            live = json.loads(target.read_text())
            live["model"] = 1
            target.write_text(json.dumps(live) + "\n", encoding="utf-8")
            accept_m6_baseline(
                load_manifest(root / "luwu.toml"),
                resource_name="pi",
                fields=("model",),
                value_from="live",
                confirm=True,
            )
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertEqual(plan.resource.forward_fields, ("model",))

            result = apply_m6_forward(plan, confirm=True)

            self.assertEqual(result.outcome, "committed")
            self.assertIs(json.loads(target.read_text())["model"], True)

    def test_forward_verification_exception_keeps_commit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            self._accept_all(root)
            (root / "inputs/global/model.json").write_text(
                '{"model":"pi-5"}\n', encoding="utf-8"
            )
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            original = m6_pilot.build_m6_plan
            calls = 0

            def fail_verification(manifest: Manifest) -> m6_pilot.M6Plan:
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise RuntimeError("verification unavailable")
                return original(manifest)

            with patch.object(m6_pilot, "build_m6_plan", side_effect=fail_verification):
                result = apply_m6_forward(plan, confirm=True)

            self.assertEqual(result.outcome, "committed_but_verification_failed")
            self.assertEqual(result.committed, ("pi.json",))
            self.assertEqual(result.attempted, ("pi.json",))
            self.assertEqual(result.uncertain, ())
            self.assertEqual(result.verification_status, "unknown")

    def _accept_all(self, root: Path) -> None:
        accept_m6_baseline(
            load_manifest(root / "luwu.toml"),
            resource_name="pi",
            fields=("model", "provider", "changelog"),
            value_from="desired",
            confirm=True,
        )

    def _make_project(self, root: Path) -> None:
        for path in (
            root / "repo",
            root / "inputs/global",
            root / "inputs/local",
            root / "inputs/baselines",
            root / "home",
        ):
            path.mkdir(parents=True)
        (root / "repo/pi.json.j2").write_text(
            '{"model":"{{ global_inputs.model }}",'
            '"provider":"{{ local_inputs.provider }}",'
            '"changelog":"{{ local_inputs.changelog }}"}\n',
            encoding="utf-8",
        )
        (root / "inputs/global/model.json").write_text(
            '{"model":"pi-4"}\n', encoding="utf-8"
        )
        (root / "inputs/local/provider.json").write_text(
            '{"provider":"provider-1"}\n', encoding="utf-8"
        )
        (root / "inputs/local/changelog.json").write_text(
            '{"changelog":"release-1"}\n', encoding="utf-8"
        )
        (root / "home/pi.json").write_text(
            '{"model":"pi-4","provider":"provider-1","changelog":"release-1"}\n',
            encoding="utf-8",
        )
        (root / "luwu.toml").write_text(
            """version = 7

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[resources.pi]
kind = "template"
source = "pi.json.j2"
target = "pi.json"
owner = "fields"
scope = "fields"
comparison = "json"
content_sensitivity = "public"
baseline = "baselines/pi.json"
inputs = { model = "global/model.json", provider = "local/provider.json", changelog = "local/changelog.json" }

[resources.pi.fields]
model = "source"
provider = "live"
changelog = "source"
""",
            encoding="utf-8",
        )
