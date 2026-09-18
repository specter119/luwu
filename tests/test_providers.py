from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from luwu.errors import ProviderError
from luwu.manifest import ProviderReference
from luwu.providers import (
    ProviderAuthority,
    ProviderProcessResult,
    RbwProviderResolver,
    resolve_provider,
)

REFERENCE = ProviderReference(
    type="rbw",
    item="database-prod",
    field="password",
)


def _secure_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    """Keep executable ancestry private without depending on a fixed uid path."""

    return tempfile.TemporaryDirectory(dir=Path.cwd())


class _CountingResolver:
    def __init__(self, value: str = "secret") -> None:
        self.calls = 0
        self.value = value

    def resolve(
        self, reference: ProviderReference, *, authority: ProviderAuthority
    ) -> str:
        self.calls += 1
        return self.value


class _RecordingRunner:
    def __init__(self, result: ProviderProcessResult) -> None:
        self.result = result
        self.argv: list[str] | None = None
        self.kwargs: dict[str, object] = {}
        self.mutate: Callable[[], None] | None = None

    def run(self, argv: Sequence[str], **kwargs: object) -> ProviderProcessResult:
        self.argv = list(argv)
        self.kwargs = kwargs
        on_spawn = kwargs.get("on_spawn")
        if callable(on_spawn):
            cast(Callable[[], object], on_spawn)()
        if self.mutate is not None:
            self.mutate()
        return self.result


class ProviderTests(unittest.TestCase):
    def _authority(self, root: Path) -> tuple[ProviderAuthority, Path]:
        executable = root / "rbw"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        return ProviderAuthority(executable, {"subprocess"}), executable

    def test_missing_or_ungranted_authority_never_calls_fake_resolver(self) -> None:
        for authority in (None, ProviderAuthority(capabilities=set())):
            resolver = _CountingResolver()
            with self.subTest(authority=authority):
                with self.assertRaises(ProviderError) as context:
                    resolve_provider(REFERENCE, authority=authority, resolver=resolver)
                self.assertEqual(context.exception.code, "capability_required")
                self.assertNotIn("secret", str(context.exception))
                self.assertEqual(resolver.calls, 0)

    def test_fake_resolver_is_injectable_after_runtime_authorization(self) -> None:
        resolver = _CountingResolver("secret\n")
        value = resolve_provider(
            REFERENCE,
            authority=ProviderAuthority(capabilities={"subprocess"}),
            resolver=resolver,
        )

        self.assertEqual(value, "secret")
        self.assertEqual(resolver.calls, 1)

    def test_rbw_argv_environment_and_normalization(self) -> None:
        with _secure_temporary_directory() as directory:
            authority, executable = self._authority(Path(directory))
            runner = _RecordingRunner(ProviderProcessResult(0, b"secret\r\n", b""))
            value = RbwProviderResolver(runner=runner, timeout=1.25).resolve(
                REFERENCE,
                authority=authority,
            )

            self.assertEqual(value, "secret")
            self.assertEqual(
                runner.argv,
                [str(executable), "get", "--field", "password", "database-prod"],
            )
            self.assertEqual(runner.kwargs["cwd"], Path("/"))
            self.assertEqual(runner.kwargs["env"], {"PATH": os.defpath})
            self.assertEqual(runner.kwargs["timeout"], 1.25)

    def test_provider_output_failures_have_no_detail_boundary(self) -> None:
        cases = (
            (
                ProviderProcessResult(0, b"\xff", b"stderr-secret"),
                "provider_output_encoding",
            ),
            (ProviderProcessResult(0, b"secret\x00value", b""), "provider_output_nul"),
            (ProviderProcessResult(0, b"", b""), "provider_empty"),
            (
                ProviderProcessResult(7, b"secret", b"stderr-secret"),
                "provider_command_failed",
            ),
        )
        with _secure_temporary_directory() as directory:
            authority, _ = self._authority(Path(directory))
            for result, code in cases:
                with self.subTest(code=code):
                    with self.assertRaises(ProviderError) as context:
                        RbwProviderResolver(runner=_RecordingRunner(result)).resolve(
                            REFERENCE,
                            authority=authority,
                        )
                    self.assertEqual(context.exception.code, code)
                    self.assertNotIn("secret", str(context.exception))

    def test_process_result_repr_does_not_expose_provider_streams(self) -> None:
        result = ProviderProcessResult(0, b"provider-secret", b"error-secret")

        self.assertNotIn("provider-secret", repr(result))
        self.assertNotIn("error-secret", repr(result))

    def test_output_limit_and_timeout_are_fixed_errors(self) -> None:
        with _secure_temporary_directory() as directory:
            authority, _ = self._authority(Path(directory))
            with self.assertRaises(ProviderError) as oversize:
                RbwProviderResolver(
                    runner=_RecordingRunner(ProviderProcessResult(0, b"12345", b"")),
                    max_output_bytes=4,
                ).resolve(REFERENCE, authority=authority)
            self.assertEqual(oversize.exception.code, "provider_output_oversize")

            class TimeoutRunner:
                def run(
                    self, argv: Sequence[str], **kwargs: object
                ) -> ProviderProcessResult:
                    del argv, kwargs
                    raise TimeoutError("secret-timeout-detail")

            with self.assertRaises(ProviderError) as timeout:
                RbwProviderResolver(runner=TimeoutRunner()).resolve(
                    REFERENCE,
                    authority=authority,
                )
            self.assertEqual(timeout.exception.code, "provider_timeout")
            self.assertNotIn("secret-timeout-detail", str(timeout.exception))

    def test_leading_option_is_rejected_before_runner_start(self) -> None:
        reference = ProviderReference(type="rbw", item="--bad", field="password")
        with _secure_temporary_directory() as directory:
            authority, _ = self._authority(Path(directory))
            runner = _RecordingRunner(ProviderProcessResult(0, b"secret", b""))
            with self.assertRaises(ProviderError) as context:
                RbwProviderResolver(runner=runner).resolve(
                    reference,
                    authority=authority,
                )
            self.assertEqual(context.exception.code, "provider_invalid_reference")
            self.assertIsNone(runner.argv)

    def test_executable_identity_change_is_detected_after_spawn(self) -> None:
        with _secure_temporary_directory() as directory:
            root = Path(directory)
            authority, executable = self._authority(root)
            runner = _RecordingRunner(ProviderProcessResult(0, b"secret", b""))

            def mutate() -> None:
                executable.write_bytes(b"changed executable")

            runner.mutate = mutate
            with self.assertRaises(ProviderError) as context:
                RbwProviderResolver(runner=runner).resolve(
                    REFERENCE,
                    authority=authority,
                )
            self.assertEqual(context.exception.code, "provider_executable_changed")
            self.assertNotIn("changed executable", str(context.exception))

    def test_symlink_executable_is_not_accepted(self) -> None:
        with _secure_temporary_directory() as directory:
            root = Path(directory)
            _, executable = self._authority(root)
            link = root / "rbw-link"
            link.symlink_to(executable)
            with self.assertRaises(ProviderError) as context:
                RbwProviderResolver(
                    runner=_RecordingRunner(ProviderProcessResult(0, b"secret", b""))
                ).resolve(
                    REFERENCE,
                    authority=ProviderAuthority(link, {"subprocess"}),
                )
            self.assertEqual(context.exception.code, "provider_executable_invalid")

    def test_real_runner_executes_only_the_fake_rbw_script(self) -> None:
        with _secure_temporary_directory() as directory:
            executable = Path(directory) / "fake-rbw"
            executable.write_text(
                "#!/bin/sh\nprintf 'fake-secret\\n'\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            value = RbwProviderResolver(timeout=1).resolve(
                REFERENCE,
                authority=ProviderAuthority(executable, {"subprocess"}),
            )
            self.assertEqual(value, "fake-secret")


if __name__ == "__main__":
    unittest.main()
