"""Safe rendering for the explicitly supported template resource."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, SupportsIndex

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .errors import RenderError
from .filesystem import (
    FileChangedError,
    NotRegularFileError,
    open_parent_directory,
    read_regular_file_at,
)
from .manifest import Resource, _PublicVariables
from .secrets import (
    _RENDERER_SECRET_TOKEN,
    SecretRenderContext,
    _open_secret_context,
    _SecretBoundaryError,
)


class _MissingSecretAlias(Exception):
    """A template requested an alias absent from the current context."""


class _SecretNamespaceError(Exception):
    """A template attempted to use the namespace itself as a value."""


class _SecretNamespace:
    """Jinja-facing namespace with no public mapping or introspection API."""

    __slots__ = ("__values",)

    def __init__(self, values: dict[str, str]) -> None:
        object.__setattr__(self, "_SecretNamespace__values", values)

    def __getitem__(self, alias: object) -> str:
        if type(alias) is not str:
            raise _MissingSecretAlias
        values = object.__getattribute__(self, "_SecretNamespace__values")
        try:
            return values[alias]
        except KeyError:
            raise _MissingSecretAlias from None

    def __getattr__(self, alias: str) -> str:
        if alias.startswith("_"):
            raise AttributeError("secret namespace attribute is unavailable")
        return self[alias]

    def __repr__(self) -> str:
        return "<SecretNamespace redacted>"

    def __str__(self) -> str:
        raise _SecretNamespaceError

    def __format__(self, _format_spec: str) -> str:
        raise _SecretNamespaceError

    def __bool__(self) -> bool:
        raise _SecretNamespaceError

    def __getattribute__(self, name: str) -> object:
        if name == "_SecretNamespace__values":
            raise AttributeError("secret namespace attribute is unavailable")
        return object.__getattribute__(self, name)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise _SecretNamespaceError


def _render_error(message: str, *, code: str) -> RenderError:
    """Build an error without retaining the exception that carried secret data."""

    return RenderError(message, code=code)


def _render_source(
    template_source: str,
    variables: dict[str, object],
    *,
    secrets: SecretRenderContext | None,
    resource_name: str,
) -> str:
    """Render source while clearing the temporary opened secret mapping."""

    environment = ImmutableSandboxedEnvironment(
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    environment.globals.clear()

    opened: dict[str, str] | None = None
    context: dict[str, object] | None = None
    namespace: _SecretNamespace | None = None
    rendered: str | None = None
    failure: RenderError | None = None
    try:
        context = dict(variables)
        if secrets is not None:
            opened = _open_secret_context(secrets, _RENDERER_SECRET_TOKEN)
            namespace = _SecretNamespace(opened)
            context["secrets"] = namespace
        template = environment.from_string(template_source)
        rendered = template.render(context)
    except _SecretBoundaryError:
        failure = _render_error(
            "secret render context is invalid",
            code="secret_context",
        )
    except _MissingSecretAlias:
        failure = _render_error(
            "secret alias is unavailable",
            code="secret_missing",
        )
    except _SecretNamespaceError:
        failure = _render_error(
            "secret namespace access is invalid",
            code="secret_namespace",
        )
    except TemplateError as exc:
        line = getattr(exc, "lineno", None)
        location = f" at line {line}" if line is not None else ""
        failure = _render_error(
            f"template for resource {resource_name!r} failed{location}: "
            f"{type(exc).__name__}",
            code="template_invalid",
        )
    except Exception as exc:  # noqa: BLE001 - template execution has no stable exception type
        failure = _render_error(
            f"template for resource {resource_name!r} failed: {type(exc).__name__}",
            code="template_invalid",
        )
    finally:
        if opened is not None:
            opened.clear()
        if context is not None:
            context.pop("secrets", None)
        namespace = None

    if failure is not None:
        rendered = None
        raise failure
    if rendered is None:
        raise _render_error(
            "template rendering did not produce text",
            code="template_invalid",
        )
    return rendered


class _RenderedTemplateStorage:
    __slots__ = ("_data",)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RenderedTemplate(_RenderedTemplateStorage):
    """Rendered bytes plus the source fingerprint used by stale-plan checks."""

    source_digest: str
    source_path: Path
    source_identity: tuple[int, int]

    def __init__(
        self,
        data: bytes,
        source_digest: str,
        source_path: Path,
        source_identity: tuple[int, int],
    ) -> None:
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "source_digest", source_digest)
        object.__setattr__(self, "source_path", source_path)
        object.__setattr__(self, "source_identity", source_identity)

    @property
    def data(self) -> bytes:
        """Return bytes for the in-process writer without exposing them in metadata."""

        return object.__getattribute__(self, "_data")

    def __repr__(self) -> str:
        return (
            "RenderedTemplate("
            f"source_digest={self.source_digest!r}, "
            f"source_path={self.source_path!r}, "
            f"source_identity={self.source_identity!r})"
        )

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("rendered templates cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> str | tuple[Any, ...]:
        raise TypeError("rendered templates cannot be serialized")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RenderedTemplate):
            return NotImplemented
        return (
            self.data == other.data
            and self.source_digest == other.source_digest
            and self.source_path == other.source_path
            and self.source_identity == other.source_identity
        )

    __hash__ = None


def render_template(
    resource: Resource,
    *,
    root: Path,
    secrets: SecretRenderContext | None = None,
) -> RenderedTemplate:
    """Render one template from data already validated by the manifest loader."""

    if resource.variables and resource.variables_sensitivity != "public":
        raise RenderError(
            f"variables for resource {resource.name!r} lack public sensitivity",
            code="variables_sensitivity",
        )
    if not isinstance(resource.variables, _PublicVariables):
        raise RenderError(
            f"variables for resource {resource.name!r} are not classified as public",
            code="variables_sensitivity",
        )
    public_variables = dict(resource.variables)
    if "secrets" in public_variables:
        raise RenderError(
            "secret namespace collides with public variables",
            code="secret_collision",
        )
    source_path, source, source_identity = read_source(resource, root=root)

    try:
        template_source = source.decode("utf-8")
    except UnicodeDecodeError:
        raise RenderError(
            f"source for resource {resource.name!r} is not valid UTF-8",
            code="source_encoding",
        ) from None

    rendered = _render_source(
        template_source,
        public_variables,
        secrets=secrets,
        resource_name=resource.name,
    )

    encoding_failure: RenderError | None = None
    try:
        data = rendered.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failure = RenderError(
            f"rendered output for resource {resource.name!r} is not valid UTF-8",
            code="rendered_encoding",
        )
    rendered = ""
    if encoding_failure is not None:
        raise encoding_failure

    return RenderedTemplate(
        data=data,
        source_digest=hashlib.sha256(source).hexdigest(),
        source_path=source_path,
        source_identity=source_identity,
    )


def read_source(
    resource: Resource,
    *,
    root: Path,
) -> tuple[Path, bytes, tuple[int, int]]:
    try:
        resolved_source = resource.source.resolve(strict=False)
        resolved_source.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise RenderError(
            f"source for resource {resource.name!r} escaped the manifest directory",
            code="source_boundary",
        ) from None

    try:
        parent_descriptor, source_name = open_parent_directory(root, resolved_source)
        try:
            source, info = read_regular_file_at(parent_descriptor, source_name)
        finally:
            os.close(parent_descriptor)
    except FileNotFoundError:
        raise RenderError(
            f"source for resource {resource.name!r} does not exist",
            code="source_missing",
        ) from None
    except NotRegularFileError:
        raise RenderError(
            f"source for resource {resource.name!r} is not a regular file",
            code="source_not_regular",
        ) from None
    except FileChangedError:
        raise RenderError(
            f"source for resource {resource.name!r} changed during inspection",
            code="source_changed",
        ) from None
    except (OSError, NotImplementedError, RuntimeError) as exc:
        raise RenderError(
            f"cannot read source for resource {resource.name!r}: "
            f"{getattr(exc, 'strerror', None) or type(exc).__name__}",
            code="source_unreadable",
        ) from None
    return resolved_source, source, (info.st_dev, info.st_ino)
