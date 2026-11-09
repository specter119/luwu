"""Authority and argv-word checks gate every provider resolution."""

from __future__ import annotations

from pathlib import Path

from luwu.providers import (
    ProviderAuthority,
    ProviderReference,
    resolve_providers,
)
from tests.support.fake_rbw import CANARY_ALPHA, FakeRbwCase


class AuthorityTests(FakeRbwCase):
    def test_empty_refs_spawn_nothing_and_touch_nothing(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()
        for authority in (
            None,
            ProviderAuthority(rbw_executable=executable),
        ):
            with self.subTest(authority=authority):
                resolution = resolve_providers([], authority, environ=self.environ())

                self.assertEqual(resolution.values, {})
                self.assertEqual(resolution.failures, {})
        self.assertEqual(self.recorded_calls(), [])

    def test_missing_authority_fails_every_reference_before_any_spawn(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()
        refs = [
            ProviderReference(name="one", item="item-one"),
            ProviderReference(name="two", item="item-two"),
        ]

        resolution = resolve_providers(refs, None, environ=self.environ())

        self.assertEqual(resolution.values, {})
        self.assertEqual(
            resolution.failures,
            {
                "one": "provider_authority_required",
                "two": "provider_authority_required",
            },
        )
        self.assertEqual(self.recorded_calls(), [])
        self.assertTrue(executable.exists())

    def test_all_invalid_argv_never_inspects_the_executable(self) -> None:
        refs = [ProviderReference(name="bad", item="-rf")]

        resolution = resolve_providers(
            refs,
            ProviderAuthority(rbw_executable=Path("/nonexistent/rbw")),
            environ=self.environ(),
        )

        self.assertEqual(resolution.values, {})
        self.assertEqual(resolution.failures, {"bad": "provider_command_failed"})

    def test_argv_injection_word_fails_closed_without_a_spawn(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()
        refs = [
            ProviderReference(name="dash-item", item="--output"),
            ProviderReference(name="good", item="item-one"),
        ]

        resolution = resolve_providers(
            refs,
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures["dash-item"], "provider_command_failed")
        self.assertEqual(resolution.values["good"].reveal(), CANARY_ALPHA)
        argv_words = [word for argv, _ in self.recorded_calls() for word in argv]
        self.assertNotIn("--output", argv_words)

    def test_control_characters_and_empty_words_are_rejected(self) -> None:
        executable = self.install_rbw()
        self.bind_socket()
        refs = [
            ProviderReference(name="newline", item="item\nname"),
            ProviderReference(name="empty-field", item="item-one", field=""),
        ]

        resolution = resolve_providers(
            refs,
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(
            resolution.failures,
            {
                "newline": "provider_command_failed",
                "empty-field": "provider_command_failed",
            },
        )
        self.assertEqual(resolution.values, {})
