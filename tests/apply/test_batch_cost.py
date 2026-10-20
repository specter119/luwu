"""How much repeated verification one apply batch performs."""

from __future__ import annotations

from unittest.mock import patch

from luwu import engine as engine_module
from luwu import manifest as manifest_module
from luwu.engine import apply_plan
from luwu.manifest import ManifestError, verify_manifest
from tests.support.profile_case import ProfileEngineCase

_HEADER = """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["notes"]
exclude = []
[packages.notes]
dependencies = []
resources = [{names}]
"""


class BatchCostTests(ProfileEngineCase):
    def _configure_directories(self, count: int, leaves: int = 3) -> None:
        names = [f"dir{index}" for index in range(count)]
        body = _HEADER.format(names=", ".join(f'"{name}"' for name in names))
        for name in names:
            directory = self.source / name
            directory.mkdir()
            for leaf in range(leaves):
                (directory / f"leaf{leaf}.conf").write_text(f"{name} {leaf}\n")
            body += (
                f'[resources.{name}]\nsource = "{name}"\n'
                f'target = ".config/{name}"\nparents = "create"\n'
            )
        self.manifest_path.write_text(body, encoding="utf-8")

    def test_limited_verification_ignores_directories_it_was_not_asked_about(
        self,
    ) -> None:
        self._configure_directories(2)
        manifest = self._load()
        (self.source / "dir1" / "extra.conf").write_text("new", encoding="utf-8")

        verify_manifest(manifest, resources=frozenset({"dir0:leaf0.conf"}))
        with self.assertRaises(ManifestError):
            verify_manifest(manifest, resources=frozenset({"dir1:leaf0.conf"}))
        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

    def test_a_resource_name_only_selects_the_directory_that_produced_it(self) -> None:
        self._configure_directories(2)
        manifest = self._load()
        (self.source / "dir1" / "extra.conf").write_text("new", encoding="utf-8")

        verify_manifest(manifest, resources=frozenset({"dir0"}))
        with self.assertRaises(ManifestError):
            verify_manifest(manifest, resources=frozenset({"dir1"}))

    def _expansions_for(self, count: int) -> int:
        self._configure_directories(count)
        with patch(
            "luwu.manifest._expand_directory", wraps=manifest_module._expand_directory
        ) as expand:
            result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.outcome, "committed")
        return expand.call_count

    def test_directory_expansion_grows_linearly_with_the_directory_count(self) -> None:
        small = self._expansions_for(3)
        for path in sorted(self.home.rglob("*"), reverse=True):
            path.unlink() if not path.is_dir() or path.is_symlink() else path.rmdir()
        for path in sorted(self.source.glob("dir*"), reverse=True):
            for leaf in path.iterdir():
                leaf.unlink()
            path.rmdir()
        large = self._expansions_for(9)
        # Expanding every directory before each action would make this cubic in
        # the count, not three times the small run.
        self.assertLessEqual(large, small * 3 + 3)

    def test_a_directory_changed_after_its_last_action_fails_the_final_check(
        self,
    ) -> None:
        self._configure_directories(2, leaves=1)
        original = engine_module.create_link
        calls: list[str] = []

        def create_then_touch_first(root, path, value, **kwargs):
            identity = original(root, path, value, **kwargs)
            calls.append(path.name)
            if len(calls) == 2:
                (self.source / "dir0" / "late.conf").write_text("x", encoding="utf-8")
            return identity

        with patch("luwu.engine.create_link", side_effect=create_then_touch_first):
            result = apply_plan(self._plan(), confirm=True)

        self.assertNotEqual(result.verification_status, "verified")
        self.assertNotEqual(result.outcome, "committed")

    def test_a_directory_changed_before_its_own_action_stops_that_action(self) -> None:
        self._configure_directories(2, leaves=1)
        original = engine_module.create_link
        calls: list[str] = []

        def create_then_touch_next(root, path, value, **kwargs):
            identity = original(root, path, value, **kwargs)
            calls.append(path.name)
            if len(calls) == 1:
                (self.source / "dir1" / "late.conf").write_text("x", encoding="utf-8")
            return identity

        with patch("luwu.engine.create_link", side_effect=create_then_touch_next):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual((result.outcome, result.error_code), ("partial", "stale_plan"))
        self.assertEqual(len(calls), 1)
