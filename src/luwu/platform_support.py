"""Small, reusable platform probes for the provider and write boundaries."""

from __future__ import annotations

import inspect
import os
import platform
import sys
from dataclasses import dataclass
from typing import Any

from .errors import PlatformError

SUPPORTED_SYSTEM = "Linux"
SUPPORTED_MACHINE = "x86_64"
SUPPORTED_PYTHON_MIN = (3, 12)
SUPPORTED_PYTHON_MAX = (3, 14)


@dataclass(frozen=True, slots=True)
class PlatformStatus:
    """Metadata-only result of a capability probe."""

    supported: bool
    system: str
    machine: str
    python_version: tuple[int, int, int]
    missing: tuple[str, ...] = ()

    @property
    def missing_primitives(self) -> tuple[str, ...]:
        return self.missing

    @property
    def reason(self) -> str | None:
        return None if self.supported else "required platform capability is unavailable"

    def to_dict(self) -> dict[str, object]:
        """Return safe diagnostic metadata without paths or environment values."""

        return {
            "supported": self.supported,
            "system": self.system,
            "machine": self.machine,
            "python": ".".join(str(part) for part in self.python_version),
            "missing": list(self.missing),
        }


def probe_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    python_version: tuple[int, int, int] | None = None,
    os_module: Any | None = None,
    fcntl_module: Any | None = None,
) -> PlatformStatus:
    """Probe the exact M4a platform contract without changing system state.

    The module and identity arguments are intentionally injectable so direct
    library tests can prove that an absent primitive fails closed.
    """

    os_module = os if os_module is None else os_module
    observed_system = platform.system() if system is None else system
    observed_machine = platform.machine() if machine is None else machine
    observed_python = (
        tuple(sys.version_info[:3]) if python_version is None else python_version
    )
    missing: list[str] = []

    if observed_system != SUPPORTED_SYSTEM:
        missing.append("linux")
    if observed_machine != SUPPORTED_MACHINE:
        missing.append("x86_64")
    if not _supported_python(observed_python):
        missing.append("python-3.12-3.14")

    for name in (
        "O_NOFOLLOW",
        "O_DIRECTORY",
        "O_NONBLOCK",
        "open",
        "close",
        "read",
        "stat",
        "lstat",
        "fstat",
        "fsync",
        "replace",
        "set_blocking",
        "killpg",
        "getuid",
    ):
        if getattr(os_module, name, None) is None:
            missing.append(f"os.{name}")

    if not _supports_directory_fds(os_module):
        missing.append("directory-fd")
    if not _supports_atomic_replace(os_module):
        missing.append("atomic-replace-dir-fd")

    if fcntl_module is None:
        try:
            import fcntl as fcntl_module
        except ImportError:
            fcntl_module = None
    if fcntl_module is None or not callable(getattr(fcntl_module, "flock", None)):
        missing.append("directory-lock")

    unique_missing = tuple(dict.fromkeys(missing))
    return PlatformStatus(
        supported=not unique_missing,
        system=observed_system,
        machine=observed_machine,
        python_version=observed_python,
        missing=unique_missing,
    )


def require_supported_platform(**probe_kwargs: Any) -> PlatformStatus:
    """Return the probe or raise the fixed platform boundary error."""

    status = probe_platform(**probe_kwargs)
    if not status.supported:
        raise PlatformError(code="platform_unsupported")
    return status


def is_supported_platform(**probe_kwargs: Any) -> bool:
    """Return whether the requested platform probe is supported."""

    return probe_platform(**probe_kwargs).supported


platform_check = probe_platform
ensure_supported = require_supported_platform


def _supported_python(version: tuple[int, int, int]) -> bool:
    major_minor = version[:2]
    return (3, 12) <= major_minor <= (3, 14)


def _supports_directory_fds(os_module: Any) -> bool:
    supported = getattr(os_module, "supports_dir_fd", None)
    if supported is None:
        return False
    for name in ("open", "stat", "unlink", "rename"):
        function = getattr(os_module, name, None)
        if function is None or function not in supported:
            return False
    return True


def _supports_atomic_replace(os_module: Any) -> bool:
    replace = getattr(os_module, "replace", None)
    if not callable(replace):
        return False
    try:
        parameters = inspect.signature(replace).parameters
    except (TypeError, ValueError):
        return False
    return "src_dir_fd" in parameters and "dst_dir_fd" in parameters
