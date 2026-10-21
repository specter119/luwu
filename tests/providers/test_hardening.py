"""Core-dump hardening is verified in subprocesses.

harden_process permanently lowers RLIMIT_CORE and clears the dumpable flag of
the calling process, so each check runs under ``sys.executable -c`` with the
repository root as cwd and ``src`` on PYTHONPATH.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_SUCCESS_SCRIPT = """
import ctypes
import resource
import sys

from tests.support.fake_rbw import CANARY_ALPHA, FakeRbwCase
from luwu.providers import ProviderAuthority, ProviderReference, resolve_providers

# Raise the soft limit first so a later drop to (0, 0) is observable even
# where the inherited soft limit already starts at 0.
_, hard = resource.getrlimit(resource.RLIMIT_CORE)
if hard == resource.RLIM_INFINITY or hard > 1:
    resource.setrlimit(resource.RLIMIT_CORE, (1, hard))

case = FakeRbwCase("setUp")
case.setUp()
case.bind_socket()
executable = case.install_rbw()
resolution = resolve_providers(
    [ProviderReference(name="one", item="item-one")],
    ProviderAuthority(rbw_executable=executable),
    environ=case.environ(),
)
assert resolution.failures == {}, resolution.failures
assert resolution.values["one"].reveal() == CANARY_ALPHA
limits = resource.getrlimit(resource.RLIMIT_CORE)
assert limits == (0, 0), limits
if sys.platform.startswith("linux"):
    libc = ctypes.CDLL(None, use_errno=True)
    dumpable = libc.prctl(3, 0, 0, 0, 0)  # PR_GET_DUMPABLE
    assert dumpable == 0, dumpable
case.doCleanups()
print("OK")
"""

_FAILURE_SCRIPT = """
import resource

from tests.support.fake_rbw import FakeRbwCase
from luwu.providers import ProviderAuthority, ProviderReference, resolve_providers


def _failing_setrlimit(which, limits):
    raise OSError("simulated setrlimit failure")


resource.setrlimit = _failing_setrlimit

case = FakeRbwCase("setUp")
case.setUp()
case.bind_socket()
executable = case.install_rbw()
resolution = resolve_providers(
    [ProviderReference(name="one", item="item-one")],
    ProviderAuthority(rbw_executable=executable),
    environ=case.environ(),
)
assert resolution.values == {}, resolution.values
assert resolution.failures == {"one": "provider_command_failed"}, resolution.failures
recorded = case.recorded_calls()
assert recorded == [], f"fake rbw must never spawn when hardening fails: {recorded}"
case.doCleanups()
print("OK")
"""

_EMPTY_REFS_SCRIPT = """
import resource

import luwu.providers as providers

calls = []
real_harden = providers.harden_process


def _recording_harden():
    calls.append(True)
    real_harden()


providers.harden_process = _recording_harden

before = resource.getrlimit(resource.RLIMIT_CORE)
resolution = providers.resolve_providers([], None)
assert resolution.values == {}
assert resolution.failures == {}
assert calls == [], "harden_process ran for an empty batch"
assert resource.getrlimit(resource.RLIMIT_CORE) == before
print("OK")
"""


def _run_script(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=REPO,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": os.pathsep.join((str(REPO / "src"), str(REPO))),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class HardeningTests(unittest.TestCase):
    def assert_script_ok(self, script: str) -> None:
        result = _run_script(script)
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("OK", result.stdout)

    def test_resolve_applies_core_dump_hardening(self) -> None:
        self.assert_script_ok(_SUCCESS_SCRIPT)

    def test_hardening_failure_fails_every_ref_without_spawning(self) -> None:
        self.assert_script_ok(_FAILURE_SCRIPT)

    def test_empty_refs_do_not_harden(self) -> None:
        self.assert_script_ok(_EMPTY_REFS_SCRIPT)


if __name__ == "__main__":
    unittest.main()
