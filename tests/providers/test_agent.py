"""Agent socket precheck, unlocked probe, and per-profile grouping."""

from __future__ import annotations

from luwu.providers import (
    ProviderAuthority,
    ProviderReference,
    resolve_providers,
)
from tests.support.fake_rbw import (
    CANARY_ALPHA,
    CANARY_BETA,
    FakeRbwCase,
)


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
        import os

        executable = self.install_rbw()
        fallback = self.root / "tmpfallback"
        runtime = fallback / f"rbw-{os.getuid()}"
        runtime.mkdir(parents=True, mode=0o700)
        import socket as socket_module

        agent = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        self.addCleanup(agent.close)
        agent.bind(str(runtime / "socket"))

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ={"HOME": str(self.root), "TMPDIR": str(fallback)},
        )

        self.assertEqual(resolution.failures, {})
        self.assertIn("one", resolution.values)

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


class ProfileGroupingTests(FakeRbwCase):
    def test_profiles_are_checked_and_resolved_per_group(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()
        self.bind_socket("work")

        refs = [
            ProviderReference(name="plain", item="item-one"),
            ProviderReference(name="work", item="item-two", profile="work"),
        ]
        resolution = resolve_providers(
            refs,
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {})
        self.assertEqual(set(resolution.values), {"plain", "work"})
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 4)
        unlocked_envs = [env for argv, env in calls if argv[0] == "unlocked"]
        get_envs = {argv[3]: env for argv, env in calls if argv[0] == "get"}
        self.assertEqual(len(unlocked_envs), 2)
        self.assertNotIn("RBW_PROFILE", get_envs["item-one"])
        self.assertEqual(get_envs["item-two"]["RBW_PROFILE"], "work")
        self.assertEqual(
            sorted("RBW_PROFILE" in env for env in unlocked_envs), [False, True]
        )

    def test_missing_profile_socket_fails_only_that_group(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()

        refs = [
            ProviderReference(name="plain", item="item-one"),
            ProviderReference(name="work", item="item-two", profile="work"),
        ]
        resolution = resolve_providers(
            refs,
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {"work": "provider_agent_unavailable"})
        self.assertEqual(resolution.values["plain"].reveal(), CANARY_ALPHA)

    def test_profile_group_can_use_custom_fields(self) -> None:
        executable = self.install_rbw()
        self.bind_socket("work")

        resolution = resolve_providers(
            [
                ProviderReference(
                    name="work", item="item-two", field="token", profile="work"
                )
            ],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {})
        self.assertEqual(resolution.values["work"].reveal(), CANARY_BETA)
