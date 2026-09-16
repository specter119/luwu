"""The intentionally narrow, read-only provider boundary for version 6."""

from __future__ import annotations

import inspect
import math
import os
import selectors
import signal
import stat
import subprocess
import time
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from .errors import ProviderError
from .manifest import ProviderReference
from .platform_support import ensure_supported

ProviderSpec = ProviderReference

SUBPROCESS_CAPABILITY = "subprocess"
RBW_PROVIDER_TYPE = "rbw"
RBW_CWD = Path("/")
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
_PROVIDER_ALIAS_RE = r"[A-Za-z][A-Za-z0-9_]{0,63}"
_RESERVED_ALIASES = frozenset({"secrets"})


@dataclass(frozen=True, slots=True)
class ProviderAuthority:
    """Runtime-granted provider capabilities, never derived from a manifest."""

    rbw_executable: Path | None = None
    capabilities: Collection[str] = frozenset()

    def __post_init__(self) -> None:
        if self.rbw_executable is not None:
            try:
                executable = Path(self.rbw_executable)
            except (TypeError, ValueError) as exc:
                raise TypeError("rbw_executable must be a path") from exc
            object.__setattr__(self, "rbw_executable", executable)
        if isinstance(self.capabilities, str):
            raise TypeError("capabilities must be a collection of names")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    @property
    def executable(self) -> Path | None:
        """Compatibility spelling for callers that do not use the rbw name."""

        return self.rbw_executable

    @property
    def granted_capabilities(self) -> frozenset[str]:
        return frozenset(self.capabilities)

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    """Non-content identity captured around one executable invocation."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ProviderProcessResult:
    """Bytes returned by an injected runner or the bounded subprocess runner."""

    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(default=b"", repr=False)


class ProviderRunner(Protocol):
    """Injection seam for tests; production uses :class:`SubprocessRunner`."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_output_bytes: int,
        on_spawn: Callable[[], None] | None = None,
    ) -> ProviderProcessResult: ...


class ProviderResolver(Protocol):
    """Resolve one already-validated provider reference in runtime authority."""

    def resolve(
        self,
        reference: ProviderReference,
        *,
        authority: ProviderAuthority,
    ) -> str: ...


class SubprocessRunner:
    """Run one absolute provider command with bounded binary pipe reads."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_output_bytes: int,
        on_spawn: Callable[[], None] | None = None,
    ) -> ProviderProcessResult:
        _validate_runner_limits(timeout, max_output_bytes)
        ensure_supported()
        try:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=dict(env),
                start_new_session=True,
                close_fds=True,
                text=False,
            )
        except (OSError, ValueError):
            raise ProviderError(code="provider_spawn_failed") from None

        try:
            if on_spawn is not None:
                on_spawn()
            return _read_process_output(
                process,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )
        except ProviderError:
            if not _terminate_process_group(process):
                raise ProviderError(code="provider_termination_failed") from None
            raise
        except (OSError, ValueError):
            if not _terminate_process_group(process):
                raise ProviderError(code="provider_termination_failed") from None
            raise ProviderError(code="provider_unavailable") from None


class RbwProviderResolver:
    """The single supported provider adapter: ``rbw get --field FIELD ITEM``."""

    def __init__(
        self,
        *,
        runner: ProviderRunner | Callable[..., Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        _validate_runner_limits(timeout, max_output_bytes)
        self._runner = runner if runner is not None else SubprocessRunner()
        self._timeout = float(timeout)
        self._max_output_bytes = max_output_bytes

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def max_output_bytes(self) -> int:
        return self._max_output_bytes

    def resolve(
        self,
        reference: ProviderReference,
        *,
        authority: ProviderAuthority | None,
    ) -> str:
        authority = _require_authority(authority)
        _validate_reference(reference)
        ensure_supported()
        executable = authority.rbw_executable
        if executable is None:
            raise ProviderError(code="provider_executable_invalid")
        identity = _inspect_executable(executable)
        argv = [str(executable), "get", "--field", reference.field, reference.item]
        on_spawn = lambda: _verify_executable_identity(executable, identity)
        try:
            raw_result = _invoke_runner(
                self._runner,
                argv,
                cwd=RBW_CWD,
                env=minimal_environment(),
                timeout=self._timeout,
                max_output_bytes=self._max_output_bytes,
                on_spawn=on_spawn,
            )
            result = _coerce_process_result(raw_result)
        except ProviderError:
            raise
        except subprocess.TimeoutExpired:
            raise ProviderError(code="provider_timeout") from None
        except TimeoutError:
            raise ProviderError(code="provider_timeout") from None
        except OSError:
            raise ProviderError(code="provider_spawn_failed") from None
        except Exception:  # noqa: BLE001 - provider errors have one fixed boundary
            raise ProviderError(code="provider_unavailable") from None

        _verify_executable_identity(executable, identity)
        if len(result.stdout) > self._max_output_bytes or len(result.stderr) > (
            self._max_output_bytes
        ):
            raise ProviderError(code="provider_output_oversize")
        if result.returncode != 0:
            raise ProviderError(code="provider_command_failed")
        return _normalise_provider_output(
            result.stdout,
            max_output_bytes=self._max_output_bytes,
        )


RbwResolver = RbwProviderResolver


def resolve_provider(
    reference: ProviderReference,
    *,
    authority: ProviderAuthority | None = None,
    resolver: ProviderResolver | Callable[..., Any] | None = None,
) -> str:
    """Resolve one reference through an injected resolver or the rbw adapter."""

    authority = _require_authority(authority)
    _validate_reference(reference)
    active_resolver = resolver if resolver is not None else RbwProviderResolver()
    try:
        raw_value = _invoke_resolver(
            active_resolver,
            reference,
            authority=authority,
        )
    except ProviderError:
        raise
    except subprocess.TimeoutExpired:
        raise ProviderError(code="provider_timeout") from None
    except TimeoutError:
        raise ProviderError(code="provider_timeout") from None
    except Exception:  # noqa: BLE001 - provider errors have one fixed boundary
        raise ProviderError(code="provider_unavailable") from None
    return _normalise_provider_output(
        raw_value,
        max_output_bytes=(
            active_resolver.max_output_bytes
            if isinstance(active_resolver, RbwProviderResolver)
            else DEFAULT_MAX_OUTPUT_BYTES
        ),
    )


def minimal_environment() -> dict[str, str]:
    """Return the small non-inherited environment used by rbw."""

    return {"PATH": os.defpath}


def _require_authority(authority: ProviderAuthority | None) -> ProviderAuthority:
    if authority is None or not isinstance(authority, ProviderAuthority):
        raise ProviderError(code="capability_required")
    if not authority.allows(SUBPROCESS_CAPABILITY):
        raise ProviderError(code="capability_required")
    return authority


def _validate_reference(reference: ProviderReference) -> None:
    if not isinstance(reference, ProviderReference):
        raise ProviderError(code="provider_invalid_reference")
    if reference.type != RBW_PROVIDER_TYPE:
        raise ProviderError(code="provider_type")
    if reference.alias is not None and not _valid_alias(reference.alias):
        raise ProviderError(code="provider_invalid_reference")
    for value in (reference.item, reference.field):
        if not _valid_opaque_argv_value(value):
            raise ProviderError(code="provider_invalid_reference")


def _valid_alias(value: object) -> bool:
    if not isinstance(value, str):
        return False
    import re

    return re.fullmatch(_PROVIDER_ALIAS_RE, value) is not None and (
        value.casefold() not in _RESERVED_ALIASES
    )


def _valid_opaque_argv_value(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("-"):
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= 256 and not any(
        unicodedata.category(character).startswith("C") for character in value
    )


def _validate_runner_limits(timeout: object, max_output_bytes: object) -> None:
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    if (
        not isinstance(max_output_bytes, int)
        or isinstance(max_output_bytes, bool)
        or max_output_bytes <= 0
    ):
        raise ValueError("max_output_bytes must be a positive integer")


def _inspect_executable(path: Path) -> ExecutableIdentity:
    try:
        if not path.is_absolute():
            raise ValueError
        executable = path.absolute()
        current_uid = os.getuid()
        info = os.lstat(executable)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError
        if info.st_uid != current_uid or not info.st_mode & stat.S_IXUSR:
            raise ValueError
        for parent in (executable.parent, *executable.parent.parents):
            parent_info = os.lstat(parent)
            if not stat.S_ISDIR(parent_info.st_mode):
                raise ValueError
            if parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError
    except (OSError, TypeError, ValueError):
        raise ProviderError(code="provider_executable_invalid") from None
    return _identity_from_stat(info)


def inspect_executable_identity(path: Path) -> ExecutableIdentity:
    """Return the same trusted executable identity used by the rbw adapter."""

    return _inspect_executable(Path(path))


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
        current = os.lstat(path.absolute())
        if not stat.S_ISREG(current.st_mode):
            raise ValueError
        observed = _identity_from_stat(current)
    except (OSError, TypeError, ValueError):
        raise ProviderError(code="provider_executable_changed") from None
    if observed != expected:
        raise ProviderError(code="provider_executable_changed")


def _invoke_runner(
    runner: ProviderRunner | Callable[..., Any],
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    max_output_bytes: int,
    on_spawn: Callable[[], None],
) -> Any:
    method = getattr(runner, "run", None)
    if method is None:
        if not callable(runner):
            raise TypeError
        method = runner
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": env,
        "timeout": timeout,
        "max_output_bytes": max_output_bytes,
        "on_spawn": on_spawn,
    }
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    if parameters and not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        kwargs = {name: value for name, value in kwargs.items() if name in parameters}
    return cast(Callable[..., Any], method)(argv, **kwargs)


def _invoke_resolver(
    resolver: ProviderResolver | Callable[..., Any],
    reference: ProviderReference,
    *,
    authority: ProviderAuthority,
) -> Any:
    method = getattr(resolver, "resolve", None)
    if method is None:
        if not callable(resolver):
            raise TypeError
        method = resolver
    return cast(Callable[..., Any], method)(reference, authority=authority)


def _coerce_process_result(value: Any) -> ProviderProcessResult:
    if isinstance(value, ProviderProcessResult):
        result = value
    elif isinstance(value, subprocess.CompletedProcess):
        result = ProviderProcessResult(
            returncode=value.returncode,
            stdout=value.stdout,
            stderr=value.stderr or b"",
        )
    elif isinstance(value, tuple) and len(value) in {2, 3}:
        result = ProviderProcessResult(
            returncode=value[0],
            stdout=value[1],
            stderr=value[2] if len(value) == 3 else b"",
        )
    else:
        raise ProviderError(code="provider_output_invalid")
    if (
        not isinstance(result.returncode, int)
        or isinstance(result.returncode, bool)
        or not isinstance(result.stdout, (bytes, bytearray))
        or not isinstance(result.stderr, (bytes, bytearray))
    ):
        raise ProviderError(code="provider_output_invalid")
    return ProviderProcessResult(
        returncode=result.returncode,
        stdout=bytes(result.stdout),
        stderr=bytes(result.stderr),
    )


def _normalise_provider_output(value: Any, *, max_output_bytes: int) -> str:
    if isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ProviderError(code="provider_output_encoding") from None
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise ProviderError(code="provider_output_invalid")
    if len(raw) > max_output_bytes:
        raise ProviderError(code="provider_output_oversize")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ProviderError(code="provider_output_encoding") from None
    if "\x00" in text:
        raise ProviderError(code="provider_output_nul")
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise ProviderError(code="provider_empty")
    return text


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
                raise ProviderError(code="provider_timeout")
            events = selector.select(remaining)
            if not events:
                raise ProviderError(code="provider_timeout")
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
                    raise ProviderError(code="provider_output_oversize")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(code="provider_timeout")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise ProviderError(code="provider_timeout") from None
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
