"""Executable trust checks: path, mode, ownership, ancestry, and identity."""

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

    def _resolve_with_stubbed_bin_dir(self, stubbed: os.stat_result):
        executable = self.install_rbw()
        real_lstat = os.lstat

        def stubbed_lstat(path, *args, **kwargs):
            if Path(path) == self.bin_dir:
                return stubbed
            return real_lstat(path, *args, **kwargs)

        with patch.object(providers.os, "lstat", stubbed_lstat):
            return self._resolve(executable)

    def test_foreign_owned_ancestor_is_refused(self) -> None:
        resolution = self._resolve_with_stubbed_bin_dir(
            _stat_like(mode=stat.S_IFDIR | 0o755, uid=os.getuid() + 1)
        )

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})
        self.assertEqual(self.recorded_calls(), [])

    def test_sticky_ancestor_owned_by_another_uid_is_not_a_boundary(self) -> None:
        resolution = self._resolve_with_stubbed_bin_dir(
            _stat_like(mode=stat.S_IFDIR | stat.S_ISVTX | 0o777, uid=os.getuid() + 1)
        )

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})
        self.assertEqual(self.recorded_calls(), [])

    def test_root_owned_ancestor_without_sticky_is_not_a_boundary(self) -> None:
        resolution = self._resolve_with_stubbed_bin_dir(
            _stat_like(mode=stat.S_IFDIR | 0o777, uid=0)
        )

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})
        self.assertEqual(self.recorded_calls(), [])

    def test_group_writable_executable_is_refused(self) -> None:
        executable = self.install_rbw()
        executable.chmod(0o770)

        resolution = self._resolve(executable)

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})

    def test_missing_executable_is_refused(self) -> None:
        resolution = self._resolve(self.bin_dir / "absent")

        self.assertEqual(resolution.failures, {"one": "provider_executable_invalid"})


class ExecutableIdentityTests(FakeRbwCase):
    """The fake rbw rewrites its own script from the get hook with sh."""

    def test_replacement_between_spawn_and_end_fails_the_batch(self) -> None:
        executable = self.install_rbw(
            get_command='mv "$payload/replacement" "$0"; cat "$payload/get.stdout"'
        )
        self.bind_socket()
        (self.payload_dir / "replacement").write_text(
            "#!/bin/sh\n# replaced with different content\nexit 0\n",
            encoding="utf-8",
        )

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.values, {})
        self.assertEqual(resolution.failures, {"one": "provider_executable_changed"})

    def test_in_place_rewrite_with_restored_mtime_fails_the_batch(self) -> None:
        executable = self.install_rbw(
            get_command=(
                'touch -r "$0" "$payload/mtime.ref"; '
                'printf "#;" | dd of="$0" bs=1 count=2 conv=notrunc 2>/dev/null; '
                'touch -r "$payload/mtime.ref" "$0"; '
                'cat "$payload/get.stdout"'
            )
        )
        self.bind_socket()
        original = executable.read_bytes()

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.values, {})
        self.assertEqual(resolution.failures, {"one": "provider_executable_changed"})
        mutated = executable.read_bytes()
        self.assertEqual(len(mutated), len(original))
        self.assertNotEqual(mutated, original)

    def test_stable_identity_across_calls_keeps_values(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {})
        self.assertEqual(resolution.values["one"].reveal(), CANARY_ALPHA)


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
