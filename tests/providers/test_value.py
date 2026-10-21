"""Sealed values: nothing printable or leaking."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from luwu import providers
from luwu.errors import ProviderError
from luwu.providers import (
    PROVIDER_ERROR_CODES,
    ProviderAuthority,
    ProviderProcessResult,
    ProviderReference,
    ProviderResolution,
    ProviderValue,
    resolve_providers,
)
from tests.support.fake_rbw import CANARY_ALPHA, FakeRbwCase


class SealedValueTests(unittest.TestCase):
    def test_repr_and_str_are_redacted(self) -> None:
        value = ProviderValue(CANARY_ALPHA)

        for rendered in (repr(value), str(value), f"{value}"):
            self.assertEqual(rendered, "<redacted>")
            self.assertNotIn(CANARY_ALPHA, rendered)

    def test_constructor_rejects_non_text_empty_and_nul_values(self) -> None:
        for bad in (b"bytes", "", "with\x00nul", 42):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ProviderValue(bad)

    def test_reveal_returns_the_plaintext(self) -> None:
        self.assertEqual(ProviderValue(CANARY_ALPHA).reveal(), CANARY_ALPHA)


class ResolutionHygieneTests(FakeRbwCase):
    def test_resolution_repr_shows_only_names_and_codes(self) -> None:
        self.bind_socket()
        executable = self.install_rbw()
        resolution = resolve_providers(
            [
                ProviderReference(name="good", item="item-one"),
                ProviderReference(name="bad", item="item-two", field="absent"),
            ],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        rendered = repr(resolution)
        self.assertIn("good", rendered)
        self.assertIn("bad: provider_output_invalid", rendered)
        self.assertNotIn(CANARY_ALPHA, rendered)
        self.assertNotIn("item-one", rendered)
        self.assertNotIn("item-two", rendered)
        self.assertNotIn(CANARY_ALPHA, str(resolution))
        self.assertNotIn(CANARY_ALPHA, repr(resolution.values))

    def test_runner_exception_text_never_reaches_the_resolution(self) -> None:
        self.bind_socket()
        executable = self.install_rbw()
        marker = "leak-marker-9e0b"

        def leaky_run(_self, argv, *, env, timeout, max_output_bytes):
            if argv[1] == "get":
                raise ProviderError(f"secret context {marker}")
            return ProviderProcessResult(0, b"")

        with patch.object(providers.SubprocessRunner, "run", leaky_run):
            resolution = resolve_providers(
                [ProviderReference(name="one", item="item-one")],
                ProviderAuthority(rbw_executable=executable),
                environ=self.environ(),
            )

        self.assertEqual(resolution.failures, {"one": "provider_command_failed"})
        self.assertNotIn(marker, repr(resolution))

    def test_stderr_secret_never_appears_in_failures(self) -> None:
        self.bind_socket()
        executable = self.install_rbw(
            unlocked_exit=1,
            unlocked_stderr=f"rbw died near {CANARY_ALPHA}\n".encode(),
        )

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
        )

        self.assertEqual(resolution.failures, {"one": "provider_command_failed"})
        self.assertNotIn(CANARY_ALPHA, repr(resolution))

    def test_process_result_repr_hides_streams(self) -> None:
        result = ProviderProcessResult(1, CANARY_ALPHA.encode(), b"stderr")

        self.assertNotIn(CANARY_ALPHA, repr(result))
        self.assertNotIn("stderr", repr(result))

    def test_failure_codes_are_fixed(self) -> None:
        self.assertEqual(
            PROVIDER_ERROR_CODES,
            frozenset(
                {
                    "provider_authority_required",
                    "provider_executable_invalid",
                    "provider_executable_changed",
                    "provider_agent_unavailable",
                    "provider_locked",
                    "provider_timeout",
                    "provider_output_invalid",
                    "provider_command_failed",
                }
            ),
        )

    def test_empty_resolution_repr_is_metadata_only(self) -> None:
        resolution = ProviderResolution(values={}, failures={})

        self.assertEqual(repr(resolution), "ProviderResolution(values=[], failures=[])")
