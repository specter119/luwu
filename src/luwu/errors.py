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


class ApplyError(LuwuError):
    """An explicit apply could not safely complete."""

    default_code = "apply_failed"
