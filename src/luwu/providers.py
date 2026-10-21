"""Resolve provider-owned values through a bounded, authorized subprocess.

This module is the only place that starts a provider process. A provider value
is sealed: it has no printable form and is opened only by the code that writes
it into a document.
"""

from __future__ import annotations

import os
import resource
import selectors
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .errors import ProviderError
from .semantic import parse_strict_json

PROVIDER_ERROR_CODES = frozenset(
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
)

PROCESS_TIMEOUT_SECONDS = 5.0
MAX_STREAM_BYTES = 1024 * 1024

_MAX_ARGV_BYTES = 256
_REDACTED = "<redacted>"
_BUILTIN_FIELDS = frozenset({"password", "username", "notes"})
_PASSTHROUGH_ENVIRON = (
    "HOME",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "TMPDIR",
)
_LOCKED_MARKER = b"agent is locked"
_PR_SET_DUMPABLE = 4


@dataclass(frozen=True, slots=True)
class ProviderReference:
    """One declared rbw lookup: metadata only, no value."""

    name: str
    item: str
    field: str | None = None
    profile: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderAuthority:
    """Runtime permission to run one rbw executable, given on the command line."""

    rbw_executable: Path


@dataclass(frozen=True, slots=True, eq=False, init=False)
class ProviderValue:
    """A resolved provider value with no printable form.

    The constructor is the single validation point for provider output: the
    value must be non-empty text without NUL. Equality and hashing stay at the
    default identity semantics, so content is never compared or exposed.
    """

    _value: str = field(repr=False)

    def __init__(self, value: object) -> None:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("a provider value is non-empty text without NUL")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """Return the plaintext; only the writer that embeds it may call this."""

        return self._value

    def __repr__(self) -> str:
        return _REDACTED

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    """The metadata-only outcome of one resolve_providers call."""

    values: Mapping[str, ProviderValue] = field(repr=False)
    failures: Mapping[str, str] = field(repr=False)

    def __repr__(self) -> str:
        resolved = ", ".join(sorted(self.values))
        failed = ", ".join(
            f"{name}: {self.failures[name]}" for name in sorted(self.failures)
        )
        return f"ProviderResolution(values=[{resolved}], failures=[{failed}])"


@dataclass(frozen=True, slots=True)
class ProviderProcessResult:
    """Bytes returned by a runner; the streams never appear in repr."""

    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(default=b"", repr=False)


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    """Non-content identity captured around one executable invocation."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


class ProviderRunner(Protocol):
    """Injection seam for tests; production uses :class:`SubprocessRunner`."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        max_output_bytes: int,
    ) -> ProviderProcessResult: ...


class SubprocessRunner:
    """Run one absolute provider command with bounded binary pipe reads."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        max_output_bytes: int,
    ) -> ProviderProcessResult:
        try:
            process = subprocess.Popen(
                [str(word) for word in argv],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=dict(env),
                start_new_session=True,
                close_fds=True,
                text=False,
            )
        except (OSError, ValueError):
            raise ProviderError(
                "the provider command could not start", code="provider_command_failed"
            ) from None
        try:
            return _read_process_output(
                process,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )
        except ProviderError:
            if not _terminate_process_group(process):
                raise ProviderError(
                    "the provider process did not end",
                    code="provider_command_failed",
                ) from None
            raise
        except (OSError, ValueError):
            if not _terminate_process_group(process):
                raise ProviderError(
                    "the provider process did not end",
                    code="provider_command_failed",
                ) from None
            raise ProviderError(
                "the provider command failed", code="provider_command_failed"
            ) from None


def valid_argv_text(value: object) -> bool:
    """Return whether `value` is safe to pass to the provider as one argv word."""

    if not isinstance(value, str) or not value or value.startswith("-"):
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if len(encoded) > _MAX_ARGV_BYTES:
        return False
    return not any(unicodedata.category(char).startswith("C") for char in value)


def harden_process() -> None:
    """Disable core dumps for this process; a failure fails the whole batch.

    Core dumps would copy provider values to disk, so this is a confidentiality
    control: when it cannot be applied, no provider may run. A platform without
    prctl skips the dumpable flag but still applies the core size limit.
    """

    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        raise ProviderError(
            "core dumps could not be disabled", code="provider_command_failed"
        ) from None
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except Exception:  # noqa: BLE001 - hardening failure must fail closed
        raise ProviderError(
            "core dumps could not be disabled", code="provider_command_failed"
        ) from None
    if result != 0:
        raise ProviderError(
            "core dumps could not be disabled", code="provider_command_failed"
        )


def resolve_providers(
    refs: Sequence[ProviderReference],
    authority: ProviderAuthority | None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: ProviderRunner | None = None,
) -> ProviderResolution:
    """Resolve each reference and report per-provider failures by fixed code.

    One provider's failure never stops the others. Authority, executable, and
    process-hardening failures are batch-wide; socket, lock, and agent failures
    apply to every reference that shares the failed profile.
    """

    references = list(refs)
    values: dict[str, ProviderValue] = {}
    failures: dict[str, str] = {}
    if not references:
        return ProviderResolution(values=values, failures=failures)
    if authority is None:
        failures = {ref.name: "provider_authority_required" for ref in references}
        return ProviderResolution(values=values, failures=failures)

    source_environ = os.environ if environ is None else environ
    active_runner = runner if runner is not None else SubprocessRunner()

    pending: list[ProviderReference] = []
    for ref in references:
        words = (ref.item, ref.field, ref.profile)
        if any(word is not None and not valid_argv_text(word) for word in words):
            failures[ref.name] = "provider_command_failed"
        else:
            pending.append(ref)
    if not pending:
        return ProviderResolution(values=values, failures=failures)

    executable = authority.rbw_executable
    try:
        identity = _inspect_executable(executable)
        harden_process()
        _verify_executable_identity(executable, identity)
    except ProviderError as error:
        for ref in pending:
            failures[ref.name] = _failure_code(error)
        return ProviderResolution(values=values, failures=failures)

    groups: dict[str | None, list[ProviderReference]] = {}
    for ref in pending:
        groups.setdefault(ref.profile, []).append(ref)

    for profile, group in groups.items():
        code = _check_agent_socket(profile, source_environ)
        if code is None:
            code = _check_unlocked(active_runner, executable, profile, source_environ)
        if code is not None:
            for ref in group:
                failures[ref.name] = code
            continue
        for ref in group:
            try:
                values[ref.name] = _fetch_value(
                    active_runner, executable, profile, source_environ, ref
                )
            except ProviderError as error:
                failures[ref.name] = _failure_code(error)

    try:
        _verify_executable_identity(executable, identity)
    except ProviderError:
        for ref in pending:
            values.pop(ref.name, None)
            failures[ref.name] = "provider_executable_changed"

    return ProviderResolution(values=values, failures=failures)


def _failure_code(error: ProviderError) -> str:
    return (
        error.code if error.code in PROVIDER_ERROR_CODES else "provider_command_failed"
    )


def _inspect_executable(path: Path) -> ExecutableIdentity:
    try:
        if not path.is_absolute():
            raise ValueError
        info = os.lstat(path)
        if not _executable_file_acceptable(info, os.getuid()):
            raise ValueError
        _check_ancestors(path)
    except (OSError, ValueError):
        raise ProviderError(
            "the provider executable is not trusted",
            code="provider_executable_invalid",
        ) from None
    return _identity_from_stat(info)


def _executable_file_acceptable(info: os.stat_result, current_uid: int) -> bool:
    """Whether one lstat result describes a runnable, untainted executable.

    Root ownership is accepted alongside current-uid ownership so a system
    path such as /usr/bin/rbw is usable.
    """

    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid in (0, current_uid)
        and bool(info.st_mode & stat.S_IXUSR)
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _check_ancestors(path: Path) -> None:
    """Reject group- or world-writable ancestors of the executable.

    A sticky root-owned directory such as /tmp is a trusted boundary: the
    walk stops there, so a private tree below it stays usable.
    """

    for directory in (path.parent, *path.parent.parents):
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError
        if info.st_uid == 0 and info.st_mode & stat.S_ISVTX:
            return
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError


def _identity_from_stat(info: os.stat_result) -> ExecutableIdentity:
    return ExecutableIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
    )


def _verify_executable_identity(path: Path, expected: ExecutableIdentity) -> None:
    try:
        current = os.lstat(path)
        if not stat.S_ISREG(current.st_mode):
            raise ValueError
        observed = _identity_from_stat(current)
    except (OSError, ValueError):
        raise ProviderError(
            "the provider executable changed", code="provider_executable_changed"
        ) from None
    if observed != expected:
        raise ProviderError(
            "the provider executable changed", code="provider_executable_changed"
        )


def _runtime_directory(profile: str | None, environ: Mapping[str, str]) -> Path:
    name = "rbw" if profile is None else f"rbw-{profile}"
    xdg = environ.get("XDG_RUNTIME_DIR", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / name
    return Path(environ.get("TMPDIR") or "/tmp") / f"{name}-{os.getuid()}"


def _agent_socket_acceptable(info: os.stat_result, current_uid: int) -> bool:
    """Whether one lstat result is a socket owned by the current uid."""

    return stat.S_ISSOCK(info.st_mode) and info.st_uid == current_uid


def _check_agent_socket(profile: str | None, environ: Mapping[str, str]) -> str | None:
    socket_path = _runtime_directory(profile, environ) / "socket"
    try:
        info = os.lstat(socket_path)
    except OSError:
        return "provider_agent_unavailable"
    if not _agent_socket_acceptable(info, os.getuid()):
        return "provider_agent_unavailable"
    return None


def _child_environ(profile: str | None, environ: Mapping[str, str]) -> dict[str, str]:
    child = {name: environ[name] for name in _PASSTHROUGH_ENVIRON if name in environ}
    child["PATH"] = os.defpath
    if profile is not None:
        child["RBW_PROFILE"] = profile
    child["RBW_AGENT"] = "/bin/false"
    return child


def _run_command(
    runner: ProviderRunner,
    argv: Sequence[str],
    env: Mapping[str, str],
) -> ProviderProcessResult:
    try:
        return runner.run(
            argv,
            env=env,
            timeout=PROCESS_TIMEOUT_SECONDS,
            max_output_bytes=MAX_STREAM_BYTES,
        )
    except ProviderError:
        raise
    except Exception:  # noqa: BLE001 - an injected runner fails closed, by code
        raise ProviderError(
            "the provider command failed", code="provider_command_failed"
        ) from None


def _check_unlocked(
    runner: ProviderRunner,
    executable: Path,
    profile: str | None,
    environ: Mapping[str, str],
) -> str | None:
    env = _child_environ(profile, environ)
    try:
        result = _run_command(runner, [str(executable), "unlocked"], env)
    except ProviderError as error:
        return _failure_code(error)
    if result.returncode == 0:
        return None
    if _LOCKED_MARKER in result.stderr:
        return "provider_locked"
    return "provider_command_failed"


def _fetch_value(
    runner: ProviderRunner,
    executable: Path,
    profile: str | None,
    environ: Mapping[str, str],
    ref: ProviderReference,
) -> ProviderValue:
    env = _child_environ(profile, environ)
    result = _run_command(
        runner, [str(executable), "get", "--raw", "--", ref.item], env
    )
    if result.returncode != 0:
        raise ProviderError("the provider command failed")
    document, parse_error = parse_strict_json(result.stdout)
    if parse_error is not None:
        raise _invalid_output()
    try:
        return ProviderValue(_select_field(document, ref.field))
    except ValueError:
        raise _invalid_output() from None


def _invalid_output() -> ProviderError:
    return ProviderError(
        "the provider output was invalid", code="provider_output_invalid"
    )


def _select_field(document: object, field_name: str | None) -> object:
    if not isinstance(document, dict):
        raise _invalid_output()
    selected: object
    if field_name is None or field_name in _BUILTIN_FIELDS:
        if field_name == "notes":
            selected = document.get("notes")
        else:
            data = document.get("data")
            if not isinstance(data, dict):
                raise _invalid_output()
            selected = data.get("password" if field_name is None else field_name)
    else:
        fields = document.get("fields")
        if not isinstance(fields, list):
            raise _invalid_output()
        matches = [
            entry.get("value")
            for entry in fields
            if isinstance(entry, dict) and entry.get("name") == field_name
        ]
        if len(matches) != 1:
            raise _invalid_output()
        selected = matches[0]
    return selected


def _read_process_output(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    max_output_bytes: int,
) -> ProviderProcessResult:
    selector = selectors.DefaultSelector()
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    streams = (("stdout", process.stdout), ("stderr", process.stderr))
    try:
        for name, stream in streams:
            if stream is None:
                continue
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("the provider timed out", code="provider_timeout")
            events = selector.select(remaining)
            if not events:
                raise ProviderError("the provider timed out", code="provider_timeout")
            for key, _ in events:
                stream = key.fileobj
                name = key.data
                current_size = len(buffers[name])
                read_size = min(64 * 1024, max_output_bytes - current_size + 1)
                try:
                    chunk = os.read(key.fd, read_size)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                buffers[name].extend(chunk)
                if len(buffers[name]) > max_output_bytes:
                    raise ProviderError(
                        "the provider output was invalid",
                        code="provider_output_invalid",
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("the provider timed out", code="provider_timeout")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise ProviderError(
                "the provider timed out", code="provider_timeout"
            ) from None
        return ProviderProcessResult(
            returncode=returncode,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
        )
    finally:
        selector.close()
        for _, stream in streams:
            if stream is not None:
                stream.close()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    terminated = True
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        terminated = False
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        terminated = False
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            terminated = False
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return terminated and process.poll() is not None
    except OSError:
        return False
    return False
