"""Process bounds: timeouts, oversize streams, and clean group termination."""

from __future__ import annotations

import time
from unittest.mock import patch

from luwu import providers
from luwu.providers import (
    ProviderAuthority,
    ProviderReference,
    resolve_providers,
)
from tests.support.fake_rbw import FakeRbwCase


class RunnerBoundsTests(FakeRbwCase):
    def test_timeout_kills_the_process_group(self) -> None:
        self.bind_socket()
        executable = self.install_rbw(get_command="exec sleep 60")

        started = time.monotonic()
        with patch.object(providers, "PROCESS_TIMEOUT_SECONDS", 0.3):
            resolution = resolve_providers(
                [ProviderReference(name="one", item="item-one")],
                ProviderAuthority(rbw_executable=executable),
                environ=self.environ(),
            )
        elapsed = time.monotonic() - started

        self.assertEqual(resolution.failures, {"one": "provider_timeout"})
        self.assertLess(elapsed, 10)

    def test_oversize_stdout_is_invalid_output(self) -> None:
        self.bind_socket()
        prefix = b'{"data":{"password":"'
        suffix = b'"},"fields":[]}'
        padded = prefix + b"x" * (4097 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(padded), 4097)
        executable = self.install_rbw(get_stdout=padded)

        with patch.object(providers, "MAX_STREAM_BYTES", 4096):
            resolution = resolve_providers(
                [ProviderReference(name="one", item="item-one")],
                ProviderAuthority(rbw_executable=executable),
                environ=self.environ(),
            )

        self.assertEqual(resolution.failures, {"one": "provider_output_invalid"})

    def test_oversize_stderr_on_unlocked_is_invalid_output(self) -> None:
        self.bind_socket()
        executable = self.install_rbw(unlocked_exit=1, unlocked_stderr=b"x" * 4097)

        with patch.object(providers, "MAX_STREAM_BYTES", 4096):
            resolution = resolve_providers(
                [ProviderReference(name="one", item="item-one")],
                ProviderAuthority(rbw_executable=executable),
                environ=self.environ(),
            )

        self.assertEqual(resolution.failures, {"one": "provider_output_invalid"})


class InjectedRunnerTests(FakeRbwCase):
    def test_injected_runner_exception_is_a_fixed_code(self) -> None:
        self.bind_socket()
        executable = self.install_rbw()

        class FailingRunner:
            def run(self, argv, *, env, timeout, max_output_bytes):
                raise OSError("device disappeared with item-one on it")

        resolution = resolve_providers(
            [ProviderReference(name="one", item="item-one")],
            ProviderAuthority(rbw_executable=executable),
            environ=self.environ(),
            runner=FailingRunner(),
        )

        self.assertEqual(resolution.failures, {"one": "provider_command_failed"})
        self.assertNotIn("item-one", repr(resolution))
