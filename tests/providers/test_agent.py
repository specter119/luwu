"""Agent socket precheck, runtime directory checks, and unlocked probe."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from luwu import providers
from luwu.providers import (
    ProviderAuthority,
    ProviderReference,
    resolve_providers,
)
from tests.support.fake_rbw import CANARY_ALPHA, FakeRbwCase


def _stat_like(*, mode: int, uid: int) -> os.stat_result:
    return cast(os.stat_result, SimpleNamespace(st_mode=mode, st_uid=uid))


class AgentSocketTests(FakeRbwCase):
    def _resolve(self, *refs: ProviderReference):
        return resolve_providers(
            refs,
            ProviderAuthority(rbw_executable=self.install_rbw()),
            environ=self.environ(),
        )

    def test_missing_socket_fails_before_any_spawn(self) -> None:
        resolution = self._resolve(ProviderReference(name="one", item="item-one"))

        self.assertEqual(resolution.failures, {"one": "provider_agent_unavailable"})
        self.assertEqual(self.recorded_calls(), [])

    def test_regular_file_at_socket_path_fails(self) -> None:
        runtime = self.runtime_root / "rbw"
        runtime.mkdir(mode=0o700)
        (runtime / "socket").write_text("not a socket", encoding="utf-8")

        resolution = self._resolve(ProviderReference(name="one", item="item-one"))

        self.assertEqual(resolution.failures, {"one": "provider_agent_unavailable"})
        self.assertEqual(self.recorded_calls(), [])

    def test_bound_socket_and_unlocked_agent_resolve(self) -> None:
        self.bind_socket()

        resolution = self._resolve(ProviderReference(name="one", item="item-one"))

        self.assertEqual(resolution.failures, {})
        self.assertEqual(resolution.values["one"].reveal(), CANARY_ALPHA)
        calls = self.recorded_calls()
        self.assertEqual([argv[0] for argv, _ in calls], ["unlocked", "get"])

    def test_fallback_runtime_dir_uses_tmpdir_and_uid(self) -> None:
        executable = self.install_rbw()
        fallback = self.root / "tmpfallback"
        self.bind_socket(fallback / f"rbw-{os.getuid()}")

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ={"HOME": str(self.root), "TMPDIR": str(fallback)},
        )

        self.assertEqual(resolution.failures, {})
        self.assertIn("one", resolution.values)

    def test_group_writable_runtime_dir_fails(self) -> None:
        self.bind_socket()
        (self.runtime_root / "rbw").chmod(0o770)

        resolution = self._resolve(ProviderReference(name="one", item="item-one"))

        self.assertEqual(resolution.failures, {"one": "provider_agent_unavailable"})
        self.assertEqual(self.recorded_calls(), [])

    def test_foreign_owned_runtime_dir_fails(self) -> None:
        self.bind_socket()
        foreign = _stat_like(mode=stat.S_IFDIR | 0o700, uid=os.getuid() + 1)
        runtime = self.runtime_root / "rbw"
        real_lstat = os.lstat

        def stubbed_lstat(path, *args, **kwargs):
            if Path(path) == runtime:
                return foreign
            return real_lstat(path, *args, **kwargs)

        with patch.object(providers.os, "lstat", stubbed_lstat):
            resolution = self._resolve(ProviderReference(name="one", item="item-one"))

        self.assertEqual(resolution.failures, {"one": "provider_agent_unavailable"})
        self.assertEqual(self.recorded_calls(), [])

    def test_relative_tmpdir_falls_back_to_tmp(self) -> None:
        if Path(f"/tmp/rbw-{os.getuid()}").exists():
            self.skipTest("a real fallback runtime directory exists")
        probed: list[str] = []
        real_lstat = os.lstat

        def recording_lstat(path, *args, **kwargs):
            probed.append(str(path))
            return real_lstat(path, *args, **kwargs)

        with patch.object(providers.os, "lstat", recording_lstat):
            resolution = resolve_providers(
                [ProviderReference(name="one", item="item-one")],
                ProviderAuthority(rbw_executable=self.install_rbw()),
                environ={"HOME": str(self.root), "TMPDIR": "relative/dir"},
            )

        self.assertEqual(resolution.failures, {"one": "provider_agent_unavailable"})
        self.assertIn(f"/tmp/rbw-{os.getuid()}", probed)
        self.assertFalse(any(path.startswith("relative") for path in probed))
        self.assertEqual(self.recorded_calls(), [])

    def test_relative_xdg_runtime_dir_falls_back(self) -> None:
        executable = self.install_rbw()

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ={"HOME": str(self.root), "XDG_RUNTIME_DIR": "relative/dir"},
        )

        self.assertEqual(resolution.failures, {"one": "provider_agent_unavailable"})


class UnlockedProbeTests(FakeRbwCase):
    def _resolve(self, *, unlocked_exit: int, unlocked_stderr: bytes):
        self.bind_socket()
        return resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(
                rbw_executable=self.install_rbw(
                    unlocked_exit=unlocked_exit,
                    unlocked_stderr=unlocked_stderr,
                )
            ),
            environ=self.environ(),
        )

    def test_locked_agent(self) -> None:
        resolution = self._resolve(
            unlocked_exit=1, unlocked_stderr=b"rbw: error: agent is locked\n"
        )

        self.assertEqual(resolution.failures, {"one": "provider_locked"})
        self.assertEqual([argv[0] for argv, _ in self.recorded_calls()], ["unlocked"])

    def test_other_unlocked_failures_are_command_failed(self) -> None:
        self.bind_socket()
        for exit_code, stderr in (
            (1, b"rbw: agent not running\n"),
            (2, b"unexpected boom\n"),
        ):
            with self.subTest(stderr=stderr):
                resolution = resolve_providers(
                    [ProviderReference(name="one", item="item-one")],
                    ProviderAuthority(
                        rbw_executable=self.install_rbw(
                            unlocked_exit=exit_code,
                            unlocked_stderr=stderr,
                        )
                    ),
                    environ=self.environ(),
                )

                self.assertEqual(
                    resolution.failures, {"one": "provider_command_failed"}
                )

    def test_unlocked_stderr_is_never_surfaced(self) -> None:
        resolution = self._resolve(
            unlocked_exit=1,
            unlocked_stderr=b"rbw: detail with item-one and provider names\n",
        )

        self.assertNotIn("item-one", repr(resolution))
        self.assertNotIn("detail", repr(resolution))
