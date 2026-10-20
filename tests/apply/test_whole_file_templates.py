"""Whole-file templates render input values into text that Luwu writes."""

from __future__ import annotations

import json

from luwu.engine import (
    EngineError,
    accept_baseline,
    adopt_resource,
    apply_plan,
    reverse_inputs,
)
from tests.support.profile_case import ProfileEngineCase

_HEAD = """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["shell"]
exclude = []
[packages.shell]
dependencies = []
resources = [{resources}]
[inputs.theme]
layers = [{{ file = "local/shared.json", path = ["theme"], format = "json" }}]
reverse_to = {{ file = "local/shared.json", path = ["theme"], format = "json" }}
"""

_GREETING = """
[resources.greeting]
source = "templates/greeting.sh.j2"
target = "greeting.sh"
baseline = "baselines/greeting.json"
inputs = ["theme"]
"""

_FIRST = """
[resources.first]
source = "templates/first.json.j2"
target = "first.json"
baseline = "baselines/first.json"
[resources.first.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
"""


class WholeFileTemplateTests(ProfileEngineCase):
    def setUp(self) -> None:
        super().setUp()
        (self.source / "templates/greeting.sh.j2").write_text(
            "echo {{ inputs.theme }}\n", encoding="utf-8"
        )
        (self.source / "templates/first.json.j2").write_text(
            '{"theme":{{ inputs.theme | tojson }}}\n', encoding="utf-8"
        )
        self.shared = self.inputs / "local/shared.json"
        self._set_theme("v1")
        self.target = self.home / "greeting.sh"

    def _set_theme(self, value: str) -> None:
        self.shared.write_text(json.dumps({"theme": value}) + "\n", encoding="utf-8")

    def _configure(self, *resources: str) -> None:
        names = ", ".join(
            json.dumps(item.split(".")[1].split("]")[0]) for item in resources
        )
        self.manifest_path.write_text(
            _HEAD.format(resources=names) + "".join(resources), encoding="utf-8"
        )

    def _status(self, name: str = "greeting") -> tuple[str, str]:
        item = next(
            entry
            for entry in self._plan().to_dict()["resources"]
            if entry["name"] == name
        )
        return item["status"], item["action"]

    def test_an_absent_target_is_created_without_adoption_and_gets_a_baseline(
        self,
    ) -> None:
        self._configure(_GREETING)
        self.assertEqual(self._status(), ("missing", "create"))
        self.assertFalse((self.inputs / "baselines/greeting.json").exists())

        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(
            (result.outcome, result.verification_status), ("committed", "verified")
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), "echo v1\n")
        self.assertTrue((self.inputs / "baselines/greeting.json").exists())
        self.assertEqual(self._status(), ("in_sync", "noop"))
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "no_changes")

        self._set_theme("v2")
        self.assertEqual(self._status(), ("drifted", "forward"))

    def test_a_target_that_appears_after_the_plan_is_never_replaced(self) -> None:
        self._configure(_GREETING)
        plan = self._plan()
        self.target.write_text("echo mine\n", encoding="utf-8")

        result = apply_plan(plan, confirm=True)

        self.assertNotEqual(result.outcome, "committed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "echo mine\n")
        self.assertEqual(self._status(), ("adopt_required", "adopt"))

    def test_an_unusable_baseline_file_never_lets_an_absent_target_be_created(
        self,
    ) -> None:
        self._configure(_GREETING)
        baseline = self.inputs / "baselines/greeting.json"
        baseline.write_text("{}", encoding="utf-8")

        self.assertIn(self._status()[0], {"blocked", "adopt_required"})
        result = apply_plan(self._plan(), confirm=True)

        self.assertNotEqual(result.outcome, "committed")
        self.assertFalse(self.target.exists())
        self.assertEqual(baseline.read_text(encoding="utf-8"), "{}")

    def test_rendered_bytes_are_adopted_deployed_and_redeployed(self) -> None:
        self._configure(_GREETING)
        self.assertEqual(
            adopt_resource(self._plan(), resource="greeting", confirm=True).outcome,
            "committed",
        )
        self.assertFalse(self.target.exists())
        self.assertEqual(self._status(), ("missing", "create"))

        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "echo v1\n")
        self.assertEqual(self._status(), ("in_sync", "noop"))

        self._set_theme("v2")
        self.assertEqual(self._status(), ("drifted", "forward"))
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "echo v2\n")
        self.assertEqual(self._status(), ("in_sync", "noop"))

    def test_baseline_records_the_rendered_digest_not_the_template(self) -> None:
        import hashlib

        self._configure(_GREETING)
        adopt_resource(self._plan(), resource="greeting", confirm=True)
        baseline = json.loads(
            (self.inputs / "baselines/greeting.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            baseline["values"]["whole_file"]["digest"],
            hashlib.sha256(b"echo v1\n").hexdigest(),
        )

    def test_live_only_edit_is_a_conflict_and_apply_leaves_it(self) -> None:
        self._configure(_GREETING)
        adopt_resource(self._plan(), resource="greeting", confirm=True)
        apply_plan(self._plan(), confirm=True)
        self.target.write_text("echo hand-edited\n", encoding="utf-8")
        self.assertEqual(self._status(), ("conflict", "review"))
        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(
            (result.outcome, result.error_code), ("not_attempted", "review_required")
        )
        self.assertEqual(
            [(action.kind, action.reason) for action in result.actions],
            [("resource_skip", "conflict")],
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), "echo hand-edited\n")

    def test_an_undefined_variable_blocks_the_resource(self) -> None:
        self._configure(_GREETING)
        (self.source / "templates/greeting.sh.j2").write_text(
            "echo {{ inputs.nothing }}\n", encoding="utf-8"
        )
        item = self._plan().to_dict()["resources"][0]
        self.assertEqual(
            (item["status"], item["error_code"]), ("blocked", "resource_blocked")
        )

    def test_a_template_that_is_not_utf8_blocks_the_resource(self) -> None:
        self._configure(_GREETING)
        (self.source / "templates/greeting.sh.j2").write_bytes(b"\xff\xfe")
        self.assertEqual(self._status()[0], "blocked")

    def test_reverse_sync_refuses_to_change_a_whole_file_consumer(self) -> None:
        self._configure(_FIRST, _GREETING)
        accept_baseline(
            self._plan(),
            resource="first",
            fields=("theme",),
            value_from="desired",
            confirm=True,
        )
        adopt_resource(self._plan(), resource="greeting", confirm=True)
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        (self.home / "first.json").write_text('{"theme":"v9"}\n', encoding="utf-8")
        before = self.shared.read_bytes()
        for confirm in (False, True):
            with (
                self.subTest(confirm=confirm),
                self.assertRaises(EngineError) as raised,
            ):
                reverse_inputs(
                    self._plan(), resource="first", fields=("theme",), confirm=confirm
                )
            self.assertEqual(raised.exception.code, "reverse_consumer_change")
        self.assertEqual(self.shared.read_bytes(), before)

    def test_directory_leaves_mix_symlinks_and_rendered_templates(self) -> None:
        tree = self.source / "tree"
        tree.mkdir()
        (tree / "plain.conf").write_text("plain\n", encoding="utf-8")
        (tree / "rc.sh.j2").write_text("echo {{ inputs.theme }}\n", encoding="utf-8")
        self.manifest_path.write_text(
            _HEAD.format(resources='"tree"')
            + """
[resources.tree]
source = "tree"
target = ".config/tree"
baseline = "baselines/tree"
inputs = ["theme"]
parents = "create"
""",
            encoding="utf-8",
        )
        statuses = {
            entry["name"]: (entry["status"], entry["target"])
            for entry in self._plan().to_dict()["resources"]
        }
        self.assertEqual(
            statuses,
            {
                "tree:plain.conf": ("missing", ".config/tree/plain.conf"),
                "tree:rc.sh.j2": ("missing", ".config/tree/rc.sh"),
            },
        )
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        out = self.home / ".config/tree"
        self.assertEqual((out / "rc.sh").read_text(encoding="utf-8"), "echo v1\n")
        self.assertTrue((out / "plain.conf").is_symlink())
        self.assertFalse((out / "rc.sh").is_symlink())
        self.assertEqual(self._status("tree:rc.sh.j2"), ("in_sync", "noop"))
        self.assertEqual(self._status("tree:plain.conf"), ("in_sync", "noop"))
