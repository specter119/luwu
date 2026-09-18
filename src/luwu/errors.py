"""Errors that form the boundary between Luwu and its CLI."""

from typing import ClassVar


class LuwuError(Exception):
    """An expected, user-actionable Luwu failure."""

    default_code = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class ManifestError(LuwuError):
    """The manifest is absent, malformed, or outside the supported contract."""

    default_code = "invalid_manifest"


class RenderError(LuwuError):
    """A declared template cannot be rendered safely."""

    default_code = "template_error"


class ProviderError(LuwuError):
    """A provider failure with a fixed, metadata-only public boundary."""

    default_code = "provider_unavailable"
    _MESSAGES: ClassVar[dict[str, str]] = {
        "capability_required": "provider capability is not authorized",
        "provider_invalid_reference": "provider reference is invalid",
        "provider_type": "provider type is unsupported",
        "provider_executable_invalid": "provider executable is not permitted",
        "provider_executable_changed": "provider executable identity changed",
        "provider_spawn_failed": "provider process could not be started",
        "provider_timeout": "provider process timed out",
        "provider_termination_failed": "provider process termination is unconfirmed",
        "provider_output_oversize": "provider output exceeded the limit",
        "provider_output_encoding": "provider output is not valid UTF-8",
        "provider_output_nul": "provider output contains NUL",
        "provider_output_invalid": "provider output is invalid",
        "provider_empty": "provider returned an empty value",
        "provider_command_failed": "provider command failed",
        "provider_unavailable": "provider is unavailable",
    }

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        del message
        safe_code = code if code in self._MESSAGES else self.default_code
        super().__init__(self._MESSAGES[safe_code], code=safe_code)


class PlatformError(LuwuError):
    """A required filesystem or process primitive is unavailable."""

    default_code = "platform_unsupported"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        del message
        super().__init__(
            "platform does not support the required operation",
            code=self.default_code,
        )


PlatformUnsupportedError = PlatformError


class MutationError(LuwuError):
    """An explicit M3 mutation could not safely complete."""

    default_code = "mutation_failed"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        committed: bool = False,
        outcome: str | None = None,
        operation: str | None = None,
        resource: str | None = None,
        fields: tuple[str, ...] = (),
        write_path: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.committed = committed
        self.outcome = outcome
        self.operation = operation
        self.resource = resource
        self.fields = fields
        self.write_path = write_path

    def attach_context(
        self,
        *,
        operation: str,
        resource: str,
        fields: tuple[str, ...],
        write_path: str | None,
    ) -> None:
        """Attach only mutation labels needed by the public error boundary."""

        self.operation = operation
        self.resource = resource
        self.fields = fields
        self.write_path = write_path

    def metadata(self) -> dict[str, object]:
        """Return metadata-only context safe for JSON and human CLI output."""

        return {
            "operation": self.operation,
            "resource": self.resource,
            "fields": list(self.fields),
            "write": self.write_path,
            "committed": self.committed,
            "outcome": self.outcome,
        }


class ApplyError(LuwuError):
    """An explicit apply could not safely complete."""

    default_code = "apply_failed"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        committed: bool = False,
        target_name: str | None = None,
        execution: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.committed = committed
        self.target_name = target_name
        self.execution = execution
