"""Safe rendering for the explicitly supported template resource."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True, slots=True)
class RenderedTemplate:
    """Rendered bytes plus the source fingerprint used by stale-plan checks."""

    data: bytes = field(repr=False)
    source_digest: str
    source_path: Path
    source_identity: tuple[int, int]


def render_template(resource: Resource, *, root: Path) -> RenderedTemplate:
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
    source_path, source, source_identity = read_source(resource, root=root)

    try:
        template_source = source.decode("utf-8")
    except UnicodeDecodeError:
        raise RenderError(
            f"source for resource {resource.name!r} is not valid UTF-8",
            code="source_encoding",
        ) from None

    environment = ImmutableSandboxedEnvironment(
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    environment.globals.clear()
    try:
        template = environment.from_string(template_source)
        rendered = template.render(**resource.variables)
    except TemplateError as exc:
        line = getattr(exc, "lineno", None)
        location = f" at line {line}" if line is not None else ""
        raise RenderError(
            f"template for resource {resource.name!r} failed{location}: "
            f"{type(exc).__name__}",
            code="template_invalid",
        ) from None
    except Exception as exc:  # noqa: BLE001 - template execution has no stable exception type
        raise RenderError(
            f"template for resource {resource.name!r} failed: {type(exc).__name__}",
            code="template_invalid",
        ) from None

    return RenderedTemplate(
        data=rendered.encode("utf-8"),
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
