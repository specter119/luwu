"""Opaque, renderer-only secret values for one render call."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final, SupportsIndex

__all__ = ["SecretRenderContext", "SecretValue"]


_RENDERER_SECRET_TOKEN: Final = object()
_SECRET_ALIAS_PATTERN: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_RESERVED_SECRET_ALIASES: Final = frozenset(
    {
        "secrets",
        "get",
        "items",
        "keys",
        "values",
        "copy",
        "update",
        "pop",
        "popitem",
        "setdefault",
        "clear",
        "fromkeys",
    }
)

_INVALID_CONTEXT_MESSAGE = "secret render context is invalid"
_INVALID_ALIAS_MESSAGE = "secret alias is invalid"
_INVALID_VALUE_MESSAGE = "secret value is invalid"
_SERIALIZATION_MESSAGE = "secret material cannot be serialized"
_REDACTED_VALUE = "<redacted>"


class _SecretBoundaryError(Exception):
    """An internal failure at the sealed-secret boundary."""


def _valid_secret_alias(alias: object) -> bool:
    return (
        type(alias) is str
        and _SECRET_ALIAS_PATTERN.fullmatch(alias) is not None
        and alias not in _RESERVED_SECRET_ALIASES
    )


class SecretValue:
    """An immutable provider value which cannot be stringified or serialized."""

    __slots__ = ("__value", "__weakref__")

    def __init__(self, value: str) -> None:
        if type(value) is not str:
            raise TypeError(_INVALID_VALUE_MESSAGE)
        object.__setattr__(self, "_SecretValue__value", value)

    def __repr__(self) -> str:
        return "<SecretValue redacted>"

    def __str__(self) -> str:
        return _REDACTED_VALUE

    def __format__(self, _format_spec: str) -> str:
        return _REDACTED_VALUE

    def __getattribute__(self, name: str) -> object:
        if name == "_SecretValue__value":
            raise AttributeError(_INVALID_VALUE_MESSAGE)
        return object.__getattribute__(self, name)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError(_INVALID_VALUE_MESSAGE)

    def __delattr__(self, _name: str) -> None:
        raise AttributeError(_INVALID_VALUE_MESSAGE)

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def __reduce_ex__(self, _protocol: SupportsIndex) -> str | tuple[Any, ...]:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def __copy__(self) -> SecretValue:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def __deepcopy__(self, _memo: dict[int, object]) -> SecretValue:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def _open(self, token: object) -> str:
        if token is not _RENDERER_SECRET_TOKEN:
            raise _SecretBoundaryError(_INVALID_CONTEXT_MESSAGE)

        value: object = None
        invalid = False
        try:
            value = object.__getattribute__(self, "_SecretValue__value")
        except AttributeError:
            invalid = True
        if invalid or type(value) is not str:
            raise _SecretBoundaryError(_INVALID_VALUE_MESSAGE)
        return value


class SecretRenderContext:
    """A sealed alias-to-secret mapping accepted by :func:`render_template`."""

    __slots__ = ("__values", "__weakref__")

    def __init__(self, values: Mapping[str, SecretValue | str]) -> None:
        if not isinstance(values, Mapping):
            raise TypeError(_INVALID_CONTEXT_MESSAGE)

        entries: tuple[object, ...] = ()
        invalid_entries = False
        try:
            entries = tuple(values.items())
        except Exception:  # noqa: BLE001 - sanitize arbitrary mapping failures
            invalid_entries = True
        if invalid_entries:
            raise TypeError(_INVALID_CONTEXT_MESSAGE)

        normalized: dict[str, SecretValue] = {}
        for alias, value in entries:
            if not _valid_secret_alias(alias) or alias in normalized:
                raise ValueError(_INVALID_ALIAS_MESSAGE)
            if type(value) is str:
                value = SecretValue(value)
            if type(value) is not SecretValue:
                raise TypeError(_INVALID_VALUE_MESSAGE)
            normalized[alias] = value

        object.__setattr__(
            self,
            "_SecretRenderContext__values",
            MappingProxyType(normalized),
        )

    def __repr__(self) -> str:
        return "<SecretRenderContext redacted>"

    def __str__(self) -> str:
        return "<SecretRenderContext redacted>"

    def __format__(self, _format_spec: str) -> str:
        return "<SecretRenderContext redacted>"

    def __getattribute__(self, name: str) -> object:
        if name == "_SecretRenderContext__values":
            raise AttributeError(_INVALID_CONTEXT_MESSAGE)
        return object.__getattribute__(self, name)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError(_INVALID_CONTEXT_MESSAGE)

    def __delattr__(self, _name: str) -> None:
        raise AttributeError(_INVALID_CONTEXT_MESSAGE)

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def __reduce_ex__(self, _protocol: SupportsIndex) -> str | tuple[Any, ...]:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def __copy__(self) -> SecretRenderContext:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def __deepcopy__(self, _memo: dict[int, object]) -> SecretRenderContext:
        raise TypeError(_SERIALIZATION_MESSAGE)

    def _open(self, token: object) -> dict[str, str]:
        if token is not _RENDERER_SECRET_TOKEN:
            raise _SecretBoundaryError(_INVALID_CONTEXT_MESSAGE)

        opened: dict[str, str] = {}
        invalid = False
        try:
            values = object.__getattribute__(self, "_SecretRenderContext__values")
            for alias, value in values.items():
                if not _valid_secret_alias(alias) or type(value) is not SecretValue:
                    invalid = True
                    break
                opened[alias] = value._open(token)
        except Exception:  # noqa: BLE001 - sanitize arbitrary secret failures
            invalid = True
        if invalid:
            opened.clear()
            raise _SecretBoundaryError(_INVALID_CONTEXT_MESSAGE)
        return opened


def _open_secret_context(
    context: SecretRenderContext,
    token: object,
) -> dict[str, str]:
    """Open *context* for the renderer's private token only."""

    if token is not _RENDERER_SECRET_TOKEN or type(context) is not SecretRenderContext:
        raise _SecretBoundaryError(_INVALID_CONTEXT_MESSAGE)
    return context._open(token)
