"""Adopting several copy resources from one plan."""

from __future__ import annotations

from unittest.mock import patch

from luwu import engine as engine_module
from luwu.cli import build_parser
from luwu.engine import EngineError, adopt_resources, apply_plan
from luwu.filesystem import FilesystemError
from tests.support.profile_case import ProfileEngineCase

_NAMES = ("alpha", "beta", "gamma")


class AdoptBatchTests(ProfileEngineCase):
    def setUp(self) -> None:
        super().setUp()
        self._configure_copy_batch(_NAMES)
        for name in _NAMES:
            (self.source / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
            (self.home / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")

    def _baseline(self, name: str):
        return self.inputs / "baselines" / f"{name}.json"

    def _statuses(self) -> dict[str, str]:
        return {
            item["name"]: item["status"] for item in self._plan().to_dict()["resources"]
        }

    def test_preview_lists_every_baseline_and_writes_nothing(self) -> None:
        before = self._snapshot()

        preview = adopt_resources(self._plan(), resources=_NAMES, confirm=False)

        self.assertEqual(preview.outcome, "confirmation_required")
        self.assertEqual(
            [(a.kind, a.resource) for a in preview.actions],
            [("baseline_write", name) for name in _NAMES],
        )
        self.assertEqual(self._snapshot(), before)

    def test_all_resources_are_adopted_from_one_observation(self) -> None:
        self.assertEqual(set(self._statuses().values()), {"adopt_required"})

        with patch(
            "luwu.engine._mutation_record", wraps=engine_module._mutation_record
        ) as record:
            result = adopt_resources(self._plan(), resources=_NAMES, confirm=True)

        self.assertEqual(record.call_count, 1)
        self.assertEqual(
            (result.outcome, result.verification_status), ("committed", "verified")
        )
        self.assertTrue(all(self._baseline(name).exists() for name in _NAMES))
        self.assertEqual(set(self._statuses().values()), {"in_sync"})
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "no_changes")

    def test_a_failed_write_stops_the_batch_and_keeps_earlier_baselines(self) -> None:
        original = engine_module.write_baseline
        calls: list[str] = []

        def fail_second(manifest, resource, **kwargs):
            calls.append(resource.name)
            if len(calls) == 2:
                raise FilesystemError(
                    "filesystem inputs are stale", code="stale_inputs"
                )
            return original(manifest, resource, **kwargs)

        with patch("luwu.engine.write_baseline", side_effect=fail_second):
            result = adopt_resources(self._plan(), resources=_NAMES, confirm=True)

        self.assertEqual(result.outcome, "partial")
        self.assertEqual(calls, ["alpha", "beta"])
        self.assertTrue(self._baseline("alpha").exists())
        self.assertFalse(self._baseline("beta").exists())
        self.assertFalse(self._baseline("gamma").exists())

    def test_a_resource_that_cannot_be_adopted_stops_the_batch_before_any_write(
        self,
    ) -> None:
        with self.assertRaises(EngineError) as raised:
            adopt_resources(
                self._plan(), resources=("alpha", "missing-name"), confirm=True
            )
        self.assertEqual(raised.exception.code, "operation_invalid")
        self.assertFalse(self._baseline("alpha").exists())

    def test_a_blocked_resource_stops_the_batch_before_any_write(self) -> None:
        (self.home / "beta.txt").unlink()
        (self.home / "beta.txt").mkdir()

        with self.assertRaises(EngineError) as raised:
            adopt_resources(self._plan(), resources=("alpha", "beta"), confirm=True)

        self.assertEqual(raised.exception.code, "operation_invalid")
        self.assertFalse(self._baseline("alpha").exists())

    def test_a_repeated_name_is_refused(self) -> None:
        with self.assertRaises(EngineError) as raised:
            adopt_resources(self._plan(), resources=("alpha", "alpha"), confirm=True)
        self.assertEqual(raised.exception.code, "operation_invalid")
        self.assertFalse(self._baseline("alpha").exists())

    def test_a_single_name_behaves_like_the_single_resource_command(self) -> None:
        result = adopt_resources(self._plan(), resources=("beta",), confirm=True)
        self.assertEqual(result.outcome, "committed")
        self.assertTrue(self._baseline("beta").exists())
        self.assertFalse(self._baseline("alpha").exists())

    def test_the_cli_accepts_a_repeated_resource_flag(self) -> None:
        args = build_parser().parse_args(
            ["adopt", "--profile", "work", "--resource", "alpha", "--resource", "beta"]
        )
        self.assertEqual(args.resource, ["alpha", "beta"])
