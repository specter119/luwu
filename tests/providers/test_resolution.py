"""Field selection, output validation, argv/env contracts, and isolation."""

from __future__ import annotations

import os

from luwu.providers import (
    ProviderAuthority,
    ProviderReference,
    resolve_providers,
)
from tests.support.fake_rbw import (
    CANARY_ALPHA,
    CANARY_BETA,
    SENTINEL_NOTES,
    SENTINEL_USERNAME,
    FakeRbwCase,
)


class FieldSelectionTests(FakeRbwCase):
    def _resolve_one(self, ref: ProviderReference, document=None):
        self.bind_socket()
        return resolve_providers(
            [ref],
            ProviderAuthority(rbw_executable=self.install_rbw(document=document)),
            environ=self.environ(),
        )

    def test_default_field_is_the_password(self) -> None:
        resolution = self._resolve_one(ProviderReference(name="one", item="item"))

        self.assertEqual(resolution.failures, {})
        self.assertEqual(resolution.values["one"].reveal(), CANARY_ALPHA)

    def test_builtin_username_and_notes(self) -> None:
        self.bind_socket()
        resolution = resolve_providers(
            [
                ProviderReference(name="user", item="item", field="username"),
                ProviderReference(name="notes", item="item", field="notes"),
            ],
            ProviderAuthority(rbw_executable=self.install_rbw()),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {})
        self.assertEqual(resolution.values["user"].reveal(), SENTINEL_USERNAME)
        self.assertEqual(resolution.values["notes"].reveal(), SENTINEL_NOTES)

    def test_custom_field_matches_exactly_once(self) -> None:
        resolution = self._resolve_one(
            ProviderReference(name="one", item="item", field="token")
        )

        self.assertEqual(resolution.failures, {})
        self.assertEqual(resolution.values["one"].reveal(), CANARY_BETA)

    def test_custom_field_selection_failures(self) -> None:
        cases = {
            "case-sensitive mismatch": ("Token", None),
            "missing field": ("absent", None),
            "duplicated field": (
                "token",
                {
                    "data": {"password": CANARY_ALPHA},
                    "fields": [
                        {"name": "token", "value": "first"},
                        {"name": "token", "value": "second"},
                    ],
                },
            ),
        }
        self.bind_socket()
        for label, (field_name, document) in cases.items():
            with self.subTest(label=label):
                resolution = resolve_providers(
                    [ProviderReference(name="one", item="item", field=field_name)],
                    ProviderAuthority(
                        rbw_executable=self.install_rbw(document=document)
                    ),
                    environ=self.environ(),
                )

                self.assertEqual(
                    resolution.failures, {"one": "provider_output_invalid"}
                )
                self.assertEqual(resolution.values, {})


class OutputValidationTests(FakeRbwCase):
    def test_invalid_outputs_fail_with_a_fixed_code(self) -> None:
        cases = {
            "invalid utf-8": b'{"data":{"password":"\xff"}}',
            "invalid json": b"not json",
            "NUL in value": b'{"data":{"password":"a\\u0000b"},"fields":[]}',
            "empty value": b'{"data":{"password":""},"fields":[]}',
            "non-string value": b'{"data":{"password":42},"fields":[]}',
            "missing data object": b'{"fields":[]}',
            "non-object document": b"[1, 2]",
        }
        self.bind_socket()
        for label, stdout in cases.items():
            with self.subTest(label=label):
                resolution = resolve_providers(
                    [ProviderReference(name="one", item="item-one")],
                    ProviderAuthority(
                        rbw_executable=self.install_rbw(get_stdout=stdout)
                    ),
                    environ=self.environ(),
                )

                self.assertEqual(
                    resolution.failures, {"one": "provider_output_invalid"}
                )
                self.assertEqual(resolution.values, {})

    def test_no_stripping_beyond_json(self) -> None:
        self.bind_socket()
        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(
                rbw_executable=self.install_rbw(
                    get_stdout=b'{"data":{"password":" padded\\t"},"fields":[]}'
                )
            ),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {})
        self.assertEqual(resolution.values["one"].reveal(), " padded\t")


class CommandContractTests(FakeRbwCase):
    def test_exact_argv_and_child_environment(self) -> None:
        self.bind_socket()
        executable = self.install_rbw()
        environ = self.environ(
            TMPDIR="/tmp",
            DISPLAY=":0",
            WAYLAND_DISPLAY="wayland-1",
            SSH_AUTH_SOCK="/run/ssh",
            XAUTHORITY="/run/xauth",
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/dbus",
            TERM="xterm",
            PINENTRY_USER_DATA="gui",
            PATH="/usr/bin:/bin",
            LC_ALL="C",
        )

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=environ,
        )

        self.assertEqual(resolution.failures, {})
        calls = self.recorded_calls()
        self.assertEqual(
            [argv for argv, _ in calls],
            [
                ["unlocked"],
                ["get", "--raw", "--", "item-one"],
            ],
        )
        expected_env = {
            "HOME": environ["HOME"],
            "XDG_RUNTIME_DIR": environ["XDG_RUNTIME_DIR"],
            "TMPDIR": "/tmp",
            "PATH": os.defpath,
            "RBW_AGENT": "/bin/false",
        }
        for _, env in calls:
            env.pop("PWD", None)  # the /bin/sh interpreter adds PWD itself
            self.assertEqual(env, expected_env)
        forbidden = {
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "SSH_AUTH_SOCK",
            "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS",
            "TERM",
            "PINENTRY_USER_DATA",
            "LC_ALL",
        }
        for _, env in calls:
            self.assertFalse(forbidden & env.keys())

    def test_nonzero_get_exit_is_command_failed(self) -> None:
        self.bind_socket()
        executable = self.install_rbw(get_exit=3)

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {"one": "provider_command_failed"})

    def test_unknown_subcommand_is_never_sent(self) -> None:
        self.bind_socket()
        executable = self.install_rbw()

        resolve_providers(
            [ProviderReference(name="one", item="item-one", field="token")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        for argv, _ in self.recorded_calls():
            self.assertNotIn("--field", argv)


class FailureIsolationTests(FakeRbwCase):
    def test_one_failure_does_not_stop_other_providers(self) -> None:
        self.bind_socket()
        executable = self.install_rbw()
        refs = [
            ProviderReference(name="good", item="item-one"),
            ProviderReference(name="bad", item="item-two", field="absent"),
            ProviderReference(name="also-good", item="item-three", field="token"),
        ]

        resolution = resolve_providers(
            refs,
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {"bad": "provider_output_invalid"})
        self.assertEqual(resolution.values["good"].reveal(), CANARY_ALPHA)
        self.assertEqual(resolution.values["also-good"].reveal(), CANARY_BETA)
