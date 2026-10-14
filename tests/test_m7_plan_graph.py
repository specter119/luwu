"""Regression tests for parsed graph integrity in M7 plans."""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from luwu import m7_engine
from luwu.m7_engine import (
    M7EngineError,
    Plan,
    accept_baseline,
    apply_plan,
    build_plan,
)
from luwu.m7_manifest import load_manifest


class M7PlanGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="luwu-m7-plan-graph-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.target = self.root / "home" / "config" / "settings.json"
        self.local_input = self.inputs / "local" / "values.json"
        self.baseline = self.inputs / "baselines" / "settings.json"
        (self.source / "templates").mkdir(parents=True)
        self.local_input.parent.mkdir(parents=True)
        self.baseline.parent.mkdir(parents=True)
        self.target.parent.mkdir(parents=True)

        (self.source / "templates" / "settings.json.j2").write_text(
            '{"alpha":{{ inputs.alpha | tojson }},"beta":{{ inputs.beta | tojson }}}\n',
            encoding="utf-8",
        )
        self.local_input.write_bytes(b'{"alpha":"alpha-v1","beta":"beta-v1"}\n')
        self.manifest = self.root / "luwu.toml"
        self.manifest.write_text(
            """
version = 8

[roots]
source = "source"
inputs = "inputs"
target = "home"

[profiles.work]
packages = ["app"]
exclude = []

[packages.app]
dependencies = []
resources = ["settings"]

[inputs.alpha]
layers = [{ file = "local/values.json", path = ["alpha"], format = "json" }]

[inputs.beta]
layers = [{ file = "local/values.json", path = ["beta"], format = "json" }]

[resources.settings]
kind = "template"
source = "templates/settings.json.j2"
target = "config/settings.json"
format = "json"
scope = "fields"
baseline = "baselines/settings.json"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.settings.fields.alpha]
path = ["alpha"]
owner = "source"
input = "alpha"

[resources.settings.fields.beta]
path = ["beta"]
owner = "source"
input = "beta"
""",
            encoding="utf-8",
        )

        initial = accept_baseline(
            self._build_plan(),
            resource="settings",
            fields=("alpha", "beta"),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(initial.outcome, "committed")
        applied = apply_plan(self._build_plan(), confirm=True)
        self.assertEqual(applied.outcome, "committed")

        self.local_input.write_bytes(b'{"alpha":"alpha-v2","beta":"beta-v1"}\n')
        self.plan = self._build_plan()
        projected = self.plan.to_dict()
        resources = projected.get("resources")
        self.assertIsInstance(resources, list)
        assert isinstance(resources, list)
        settings = next(
            item
            for item in resources
            if isinstance(item, dict) and item.get("name") == "settings"
        )
        self.assertEqual(settings.get("status"), "drifted")
        self.assertEqual(settings.get("action"), "forward")

    def _build_plan(self) -> Plan:
        return build_plan(load_manifest(self.manifest, profile="work"))

    def _settings_document(self):
        resource = next(
            item for item in self.plan._resources if item.resource.name == "settings"
        )
        document = resource.desired_document
        self.assertIsNotNone(document)
        assert document is not None
        return document

    def _snapshot_bytes(self) -> tuple[bytes, bytes, bytes]:
        return (
            self.target.read_bytes(),
            self.local_input.read_bytes(),
            self.baseline.read_bytes(),
        )

    def _assert_plan_invalid_without_writes(self) -> None:
        before = self._snapshot_bytes()
        result = None
        error = None
        with (
            patch.object(
                m7_engine, "write_file", wraps=m7_engine.write_file
            ) as write_file,
            patch.object(
                m7_engine, "create_parent", wraps=m7_engine.create_parent
            ) as create_parent,
        ):
            try:
                result = apply_plan(self.plan, confirm=True)
            except M7EngineError as caught:
                error = caught

        error_code = (
            error.code
            if error is not None
            else None
            if result is None
            else result.error_code
        )
        outcome = (
            "raised"
            if error is not None
            else None
            if result is None
            else result.outcome
        )
        with self.subTest(check="plan error"):
            self.assertEqual(error_code, "m7_plan_invalid")
        with self.subTest(check="no writer calls"):
            self.assertEqual(write_file.call_count, 0)
        with self.subTest(check="no parent creation"):
            self.assertEqual(create_parent.call_count, 0)
        with self.subTest(check="target input baseline bytes"):
            self.assertEqual(self._snapshot_bytes(), before)
        if error is None and result is not None:
            with self.subTest(check="mutation not attempted"):
                self.assertEqual(outcome, "not_attempted")

    def _alpha_graph_edges(self):
        root = self._settings_document()._root
        children = root.children
        self.assertIsInstance(children, dict)
        assert isinstance(children, dict)
        members = root.members
        self.assertIsNotNone(members)
        assert members is not None
        alpha_index = next(
            index for index, member in enumerate(members) if member.key == "alpha"
        )
        alpha_member = members[alpha_index]
        original = children["alpha"]
        self.assertIs(alpha_member.value, original)
        return children, members, alpha_index, alpha_member, original

    def _split_equal_alpha_child(self):
        children, members, alpha_index, alpha_member, original = (
            self._alpha_graph_edges()
        )
        clone = copy.deepcopy(original)
        self.assertEqual(clone, original)
        self.assertIsNot(clone, original)

        children["alpha"] = clone
        self.assertIs(members[alpha_index].value, alpha_member.value)
        self.assertIsNot(children["alpha"], alpha_member.value)
        self.assertEqual(children["alpha"], alpha_member.value)
        return clone

    def _merge_equal_alpha_child(self):
        children, members, alpha_index, alpha_member, original = (
            self._alpha_graph_edges()
        )
        clone = copy.deepcopy(original)
        self.assertEqual(clone, original)
        self.assertIsNot(clone, original)

        # First make the parser's two references independent, equal copies,
        # then merge both references onto the equal copy.
        children["alpha"] = clone
        self.assertIsNot(children["alpha"], alpha_member.value)
        self.assertEqual(children["alpha"], alpha_member.value)
        members[alpha_index] = replace(alpha_member, value=clone)

        self.assertIs(children["alpha"], members[alpha_index].value)
        self.assertIsNot(children["alpha"], original)
        self.assertEqual(children["alpha"], original)
        return clone

    def _assert_apply_commits_expected_bytes(self) -> None:
        before_input = self.local_input.read_bytes()
        before_baseline = self.baseline.read_bytes()
        result = apply_plan(self.plan, confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(
            self.target.read_bytes(), b'{"alpha":"alpha-v2","beta":"beta-v1"}\n'
        )
        self.assertEqual(self.local_input.read_bytes(), before_input)
        self.assertEqual(self.baseline.read_bytes(), before_baseline)

    def test_splitting_equal_shared_parser_child_is_accepted(self) -> None:
        self._split_equal_alpha_child()

        self._assert_apply_commits_expected_bytes()

    def test_merging_equal_parser_child_copies_is_accepted(self) -> None:
        self._merge_equal_alpha_child()

        self._assert_apply_commits_expected_bytes()

    def test_split_child_value_change_is_rejected_before_writes(self) -> None:
        clone = self._split_equal_alpha_child()
        clone.value = "tampered-alpha"

        self._assert_plan_invalid_without_writes()

    def test_split_child_span_change_is_rejected_before_writes(self) -> None:
        clone = self._split_equal_alpha_child()
        self.assertIsNotNone(clone.start)
        self.assertIsNotNone(clone.end)
        assert clone.start is not None
        assert clone.end is not None
        clone.start += 1
        clone.end += 1

        self._assert_plan_invalid_without_writes()

    def test_merged_child_value_change_is_rejected_before_writes(self) -> None:
        clone = self._merge_equal_alpha_child()
        clone.value = "tampered-alpha"

        self._assert_plan_invalid_without_writes()

    def test_merged_child_span_change_is_rejected_before_writes(self) -> None:
        clone = self._merge_equal_alpha_child()
        self.assertIsNotNone(clone.start)
        self.assertIsNotNone(clone.end)
        assert clone.start is not None
        assert clone.end is not None
        clone.start += 1
        clone.end += 1

        self._assert_plan_invalid_without_writes()
