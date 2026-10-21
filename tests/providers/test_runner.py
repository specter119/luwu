"""Process bounds: timeouts, oversize streams, and clean group termination."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from typing import cast
from unittest.mock import patch

from luwu import providers
from luwu.errors import ProviderError
from luwu.providers import (
    ProviderAuthority,
    ProviderReference,
    resolve_providers,
)
from tests.support.fake_rbw import FakeRbwCase


def _capturing_popen(spawned: list):
    real_popen = providers.subprocess.Popen

    def capturing(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    return capturing


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


class InterruptionTests(unittest.TestCase):
    def test_keyboard_interrupt_kills_the_process_group(self) -> None:
        spawned: list = []
        runner = providers.SubprocessRunner()

        with (
            patch.object(providers.subprocess, "Popen", _capturing_popen(spawned)),
            patch.object(
                providers, "_read_process_output", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            runner.run(
                ["/bin/sh", "-c", "exec sleep 60"],
                env={},
                timeout=30,
                max_output_bytes=1024,
            )

        self.assertEqual(len(spawned), 1)
        process = spawned[0]
        process.wait(timeout=5)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)

    def test_unendable_process_is_reported_as_not_ended(self) -> None:
        spawned: list = []
        runner = providers.SubprocessRunner()

        with (
            patch.object(providers.subprocess, "Popen", _capturing_popen(spawned)),
            patch.object(
                providers,
                "_read_process_output",
                side_effect=ProviderError("stuck", code="provider_timeout"),
            ),
            patch.object(providers, "_terminate_process_group", return_value=False),
            self.assertRaises(ProviderError) as raised,
        ):
            runner.run(["/bin/true"], env={}, timeout=30, max_output_bytes=1024)

        self.assertEqual(raised.exception.code, "provider_command_failed")
        self.assertIn("did not end", str(raised.exception))
        self.assertNotIn("stuck", str(raised.exception))
        spawned[0].wait(timeout=5)


class _StubProcess:
    """The Popen surface _terminate_process_group uses: a pid and a wait."""

    def __init__(self, pid: int, wait_results: list[object]) -> None:
        self.pid = pid
        self._wait_results = wait_results

    def wait(self, timeout: float | None = None) -> int:
        result = self._wait_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, int)
        return result


class TerminateProcessGroupTests(unittest.TestCase):
    def _terminate(
        self,
        *,
        wait_results: list[object],
        killpg_results: list[object],
    ) -> bool:
        process = _StubProcess(pid=43210, wait_results=wait_results)
        calls: list[tuple[int, int]] = []
        real_results = list(killpg_results)

        def stubbed_killpg(pid: int, sig: int) -> None:
            calls.append((pid, sig))
            result = real_results.pop(0)
            if isinstance(result, BaseException):
                raise result

        with patch.object(providers.os, "killpg", stubbed_killpg):
            terminated = providers._terminate_process_group(
                cast("subprocess.Popen[bytes]", process)
            )
        self.assertEqual(calls[0], (43210, signal.SIGKILL))
        return terminated

    def test_group_gone_after_sigkill_reports_terminated(self) -> None:
        self.assertTrue(
            self._terminate(
                wait_results=[-signal.SIGKILL],
                killpg_results=[None, ProcessLookupError()],
            )
        )

    def test_sigkill_lookup_failure_still_waits_and_probes(self) -> None:
        self.assertTrue(
            self._terminate(
                wait_results=[-signal.SIGKILL],
                killpg_results=[ProcessLookupError(), ProcessLookupError()],
            )
        )

    def test_sigkill_os_error_still_waits_and_probes(self) -> None:
        self.assertTrue(
            self._terminate(
                wait_results=[-signal.SIGKILL],
                killpg_results=[PermissionError(), ProcessLookupError()],
            )
        )

    def test_unreaped_process_reports_not_terminated(self) -> None:
        self.assertFalse(
            self._terminate(
                wait_results=[subprocess.TimeoutExpired(cmd="rbw", timeout=1.0)],
                killpg_results=[None],
            )
        )

    def test_surviving_group_member_reports_not_terminated(self) -> None:
        self.assertFalse(
            self._terminate(
                wait_results=[-signal.SIGKILL],
                killpg_results=[None, None],
            )
        )

    def test_failed_group_probe_reports_not_terminated(self) -> None:
        self.assertFalse(
            self._terminate(
                wait_results=[-signal.SIGKILL],
                killpg_results=[None, PermissionError()],
            )
        )


class PatchedRunnerTests(FakeRbwCase):
    def test_patched_runner_exception_is_a_fixed_code(self) -> None:
        self.bind_socket()
        executable = self.install_rbw()

        with patch.object(
            providers.SubprocessRunner,
            "run",
            side_effect=OSError("device disappeared with item-one on it"),
        ):
            resolution = resolve_providers(
                [ProviderReference(name="one", item="item-one")],
                ProviderAuthority(rbw_executable=executable),
                environ=self.environ(),
            )

        self.assertEqual(resolution.failures, {"one": "provider_command_failed"})
        self.assertNotIn("item-one", repr(resolution))
        self.assertEqual(self.recorded_calls(), [])
