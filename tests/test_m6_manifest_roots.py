"""Focused tests for the opt-in M6 root declaration skeleton."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luwu.errors import ManifestError
from luwu.manifest import M6_MANIFEST_VERSION, load_manifest


class M6ManifestRootsTests(unittest.TestCase):
    def test_version_seven_captures_distinct_root_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_roots(root)
            self._write_manifest(root)

            manifest = load_manifest(root / "luwu.toml")

            self.assertEqual(manifest.version, M6_MANIFEST_VERSION)
            self.assertIsNotNone(manifest.roots)
            assert manifest.roots is not None
            self.assertEqual(manifest.roots.source.path, root / "repo")
            self.assertEqual(manifest.roots.inputs.path, root / "inputs")
            self.assertEqual(manifest.roots.target.path, root / "home")
            self.assertEqual(manifest.resources[0].source, root / "repo/pi.json.j2")
            self.assertEqual(manifest.resources[0].target, root / "home/pi.json")

    def test_overlapping_roots_are_rejected_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            (root / "repo" / "nested").mkdir()
            (root / "home").mkdir()
            manifest = self._manifest_text(
                source_root="repo",
                input_root="repo/nested",
                target_root="home",
            )
            (root / "luwu.toml").write_text(manifest, encoding="utf-8")

            with self.assertRaisesRegex(ManifestError, "non-overlapping"):
                load_manifest(root / "luwu.toml")

    def test_symlinked_root_is_rejected_before_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real-repo").mkdir()
            (root / "inputs").mkdir()
            (root / "home").mkdir()
            (root / "repo-link").symlink_to(
                root / "real-repo", target_is_directory=True
            )
            (root / "luwu.toml").write_text(
                self._manifest_text(
                    source_root="repo-link",
                    input_root="inputs",
                    target_root="home",
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as raised:
                load_manifest(root / "luwu.toml")
            self.assertEqual(raised.exception.code, "m6_root_unreadable")

    def test_target_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_roots(root)
            (root / "luwu.toml").write_text(
                self._manifest_text(target="../outside.json"),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as raised:
                load_manifest(root / "luwu.toml")
            self.assertEqual(raised.exception.code, "path_boundary")

    def _make_roots(self, root: Path) -> None:
        for name in ("repo", "inputs", "home"):
            (root / name).mkdir()

    def _write_manifest(self, root: Path) -> None:
        (root / "luwu.toml").write_text(self._manifest_text(), encoding="utf-8")

    def _manifest_text(
        self,
        *,
        source_root: str = "repo",
        input_root: str = "inputs",
        target_root: str = "home",
        target: str = "pi.json",
    ) -> str:
        return f'''version = 7

[roots]
source = "{source_root}"
inputs = "{input_root}"
target = "{target_root}"

[resources.pi]
kind = "template"
source = "pi.json.j2"
target = "{target}"
owner = "fields"
scope = "fields"
comparison = "json"
content_sensitivity = "public"
baseline = "baselines/pi.json"

[resources.pi.fields]
model = "merge"
provider = "merge"
changelog = "source"
'''


if __name__ == "__main__":
    unittest.main()
