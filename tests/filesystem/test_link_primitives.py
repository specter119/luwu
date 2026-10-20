"""Tests for symlink observation and creation."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luwu.filesystem import (
    FilesystemError,
    create_link,
    observe_link,
)


class LinkPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source.conf"
        self.source.write_text("source\n", encoding="utf-8")
        self.link = self.home / "app.conf"

    def test_observe_link_names_absent_link_and_foreign_entries(self) -> None:
        absent = observe_link(self.home, self.link)
        self.assertEqual(
            (absent.identity, absent.link_target, absent.foreign), (None, None, False)
        )

        self.link.symlink_to(self.source)
        link = observe_link(self.home, self.link)
        self.assertEqual((link.link_target, link.foreign), (str(self.source), False))
        self.assertIsNotNone(link.identity)

        self.link.unlink()
        self.link.write_text("regular", encoding="utf-8")
        regular = observe_link(self.home, self.link)
        self.assertEqual((regular.link_target, regular.foreign), (None, True))

        self.link.unlink()
        self.link.mkdir()
        directory = observe_link(self.home, self.link)
        self.assertTrue(directory.foreign)

    def test_observe_link_does_not_follow_a_dangling_link(self) -> None:
        self.link.symlink_to(self.root / "missing")
        observed = observe_link(self.home, self.link)
        self.assertEqual(observed.link_target, str(self.root / "missing"))

    def test_create_link_makes_a_symlink_and_returns_its_identity(self) -> None:
        expected = observe_link(self.home, self.link)
        identity = create_link(
            self.home, self.link, str(self.source), expected=expected
        )
        self.assertEqual(os.readlink(self.link), str(self.source))
        observed = observe_link(self.home, self.link)
        self.assertEqual(observed.identity, identity)

    def test_create_link_never_replaces_an_entry_that_appeared_later(self) -> None:
        expected = observe_link(self.home, self.link)
        self.link.write_text("keep me", encoding="utf-8")
        with self.assertRaises(FilesystemError) as raised:
            create_link(self.home, self.link, str(self.source), expected=expected)
        self.assertEqual(raised.exception.code, "stale_inputs")
        self.assertEqual(self.link.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(self.link.is_symlink())

    def test_create_link_survives_an_exist_race_after_the_recheck(self) -> None:
        expected = observe_link(self.home, self.link)

        def racing_symlink(link_value: str, name: str, *, dir_fd: int) -> None:
            (self.home / name).write_text("raced", encoding="utf-8")
            raise FileExistsError(name)

        with (
            patch("luwu.filesystem.os.symlink", side_effect=racing_symlink),
            self.assertRaises(FilesystemError) as raised,
        ):
            create_link(self.home, self.link, str(self.source), expected=expected)
        self.assertEqual(raised.exception.code, "stale_inputs")
        self.assertFalse(raised.exception.committed)
        self.assertEqual(self.link.read_text(encoding="utf-8"), "raced")

    def test_create_link_reports_an_uncertain_result_when_the_link_reads_back_wrong(
        self,
    ) -> None:
        expected = observe_link(self.home, self.link)
        real_symlink = os.symlink

        def wrong_symlink(link_value: str, name: str, *, dir_fd: int) -> None:
            real_symlink(str(self.root / "other"), name, dir_fd=dir_fd)

        with (
            patch("luwu.filesystem.os.symlink", side_effect=wrong_symlink),
            self.assertRaises(FilesystemError) as raised,
        ):
            create_link(self.home, self.link, str(self.source), expected=expected)
        self.assertEqual(raised.exception.code, "write_unknown")
        self.assertTrue(raised.exception.committed)

    def test_create_link_requires_an_absent_observation_and_an_absolute_value(
        self,
    ) -> None:
        absent = observe_link(self.home, self.link)
        for bad in ("relative/path", "", "/with\x00nul"):
            with self.subTest(value=bad), self.assertRaises(FilesystemError) as raised:
                create_link(self.home, self.link, bad, expected=absent)
            self.assertEqual(raised.exception.code, "invalid_content")
        self.link.symlink_to(self.source)
        present = observe_link(self.home, self.link)
        with self.assertRaises(FilesystemError) as raised:
            create_link(self.home, self.link, str(self.source), expected=present)
        self.assertEqual(raised.exception.code, "invalid_observation")

    def test_create_link_refuses_a_parent_swapped_for_a_symlink(self) -> None:
        nested = self.home / "nested"
        nested.mkdir()
        target = nested / "app.conf"
        expected = observe_link(self.home, target)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        nested.rmdir()
        nested.symlink_to(elsewhere)
        with self.assertRaises(FilesystemError):
            create_link(self.home, target, str(self.source), expected=expected)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_create_link_runs_the_input_recheck_before_linking(self) -> None:
        expected = observe_link(self.home, self.link)

        def refuse() -> None:
            raise FilesystemError("stale", code="stale_inputs")

        with self.assertRaises(FilesystemError):
            create_link(
                self.home,
                self.link,
                str(self.source),
                expected=expected,
                check_inputs=refuse,
            )
        self.assertFalse(os.path.lexists(self.link))


if __name__ == "__main__":
    unittest.main()
