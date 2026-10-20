"""Sandboxed rendering of declared template sources."""

from __future__ import annotations

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .errors import RenderError


def _render_error(message: str, *, code: str) -> RenderError:
    return RenderError(message, code=code)


def render_source(
    template_source: str,
    variables: dict[str, object],
    *,
    resource_name: str,
) -> str:
    """Render a declared template with a sandboxed, strict-undefined environment."""

    environment = ImmutableSandboxedEnvironment(
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    environment.globals.clear()

    context: dict[str, object] | None = None
    rendered: str | None = None
    failure: RenderError | None = None
    try:
        context = dict(variables)
        template = environment.from_string(template_source)
        rendered = template.render(context)
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
        context = None

    if failure is not None:
        rendered = None
        raise failure
    if rendered is None:
        raise _render_error(
            "template rendering did not produce text",
            code="template_invalid",
        )
    return rendered


def template_syntax(template_source: str) -> tuple[tuple[str, str], ...]:
    """Return the template's non-text tokens, in order.

    Two sources with equal syntax differ only in literal text, so replacing
    text cannot add, remove, or reshape an expression or a block.
    """

    environment = ImmutableSandboxedEnvironment(
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    try:
        return tuple(
            (kind, value)
            for _, kind, value in environment.lex(template_source)
            if kind != "data"
        )
    except TemplateError as exc:
        raise _render_error(
            f"template could not be tokenized: {type(exc).__name__}",
            code="template_invalid",
        ) from None
