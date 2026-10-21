"""Executable trust checks: path, mode, ownership, ancestry, and identity."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from luwu import providers
from luwu.providers import (
    ProviderAuthority,
    ProviderProcessResult,
    ProviderReference,
    resolve_providers,
)
from tests.support.fake_rbw import FakeRbwCase, default_stdout


def _stat_like(*, mode: int, uid: int) -> os.stat_result:
    return cast(os.stat_result, SimpleNamespace(st_mode=mode, st_uid=uid))


class ExecutableTrustTests(FakeRbwCase):
    def _resolve(self, executable: Path, item: str = "item-one"):
        self.bind_socket()
        return resolve_providers(
            [ProviderReference(name="one", item=item)],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

    def test_private_tree_below_sticky_tmp_is_accepted(self) -> None:
        executable = self.install_rbw()

        resolution = self._resolve(executable)

        self.assertEqual(resolution.failures, {})
        self.assertIn("one", resolution.values)

    def test_symlinked_executable_is_refused(self) -> None:
        real = self.install_rbw()
        link = self.root / "linked-rbw"
        link.symlink_to(real)

        resolution = self._resolve(link)

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})
        self.assertEqual(self.recorded_calls(), [])

    def test_relative_executable_path_is_refused(self) -> None:
        self.install_rbw()

        resolution = self._resolve(Path("bin/rbw"))

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})
        self.assertEqual(self.recorded_calls(), [])

    def test_group_writable_ancestor_is_refused(self) -> None:
        executable = self.install_rbw()
        self.bin_dir.chmod(0o775)

        resolution = self._resolve(executable)

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})
        self.assertEqual(self.recorded_calls(), [])

    def test_world_writable_ancestor_is_refused(self) -> None:
        executable = self.install_rbw()
        self.bin_dir.chmod(0o707)

        resolution = self._resolve(executable)

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})

    def test_group_writable_executable_is_refused(self) -> None:
        executable = self.install_rbw()
        executable.chmod(0o770)

        resolution = self._resolve(executable)

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})

    def test_missing_executable_is_refused(self) -> None:
        resolution = self._resolve(self.bin_dir / "absent")

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})


class ExecutableIdentityTests(FakeRbwCase):
    def test_replacement_between_spawn_and_end_fails_the_batch(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()

        class ReplacingRunner:
            def run(self, argv, *, env, timeout, max_output_bytes):
                if argv[1] == "get":
                    executable.write_text(
                        "#!/bin/sh\n# replaced with different content\nexit 0\n",
                        encoding="utf-8",
                    )
                    return ProviderProcessResult(0, default_stdout())
                return ProviderProcessResult(0, b"")

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
            runner=ReplacingRunner(),
        )

        self.assertEqual(resolution.values, {})
        self.assertEqual(resolution.failures, {"one": "provider_executable_changed"})

    def test_stable_identity_across_calls_keeps_values(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()

        class QuietRunner:
            def run(self, argv, *, env, timeout, max_output_bytes):
                if argv[1] == "get":
                    return ProviderProcessResult(0, default_stdout())
                return ProviderProcessResult(0, b"")

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
            runner=QuietRunner(),
        )

        self.assertEqual(resolution.failures, {})
        self.assertIn("one", resolution.values)


class ExecutablePredicateTests(FakeRbwCase):
    def test_root_owned_executable_is_accepted(self) -> None:
        info = _stat_like(mode=stat.S_IFREG | 0o755, uid=0)

        self.assertTrue(providers._executable_file_acceptable(info, 1000))

    def test_current_uid_owned_executable_is_accepted(self) -> None:
        info = _stat_like(mode=stat.S_IFREG | 0o700, uid=1000)

        self.assertTrue(providers._executable_file_acceptable(info, 1000))

    def test_other_uid_executable_is_refused(self) -> None:
        info = _stat_like(mode=stat.S_IFREG | 0o755, uid=1234)

        self.assertFalse(providers._executable_file_acceptable(info, 1000))

    def test_non_owner_executable_bit_missing_is_refused(self) -> None:
        info = _stat_like(mode=stat.S_IFREG | 0o600, uid=1000)

        self.assertFalse(providers._executable_file_acceptable(info, 1000))

    def test_socket_predicate_requires_socket_and_current_uid(self) -> None:
        sock = _stat_like(mode=stat.S_IFSOCK | 0o700, uid=1000)
        regular = _stat_like(mode=stat.S_IFREG | 0o700, uid=1000)
        foreign = _stat_like(mode=stat.S_IFSOCK | 0o700, uid=0)

        self.assertTrue(providers._agent_socket_acceptable(sock, 1000))
        self.assertFalse(providers._agent_socket_acceptable(regular, 1000))
        self.assertFalse(providers._agent_socket_acceptable(foreign, 1000))
