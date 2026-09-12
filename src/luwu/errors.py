"""Errors that form the boundary between Luwu and its CLI."""


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
    ) -> None:
        super().__init__(message, code=code)
        self.committed = committed
        self.target_name = target_name
