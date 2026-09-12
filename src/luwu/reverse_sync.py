"""Pure, narrow reverse-sync patching for literal JSON sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .baseline import _MISSING, encode_public_json, parse_public_object
from .errors import MutationError


@dataclass(frozen=True, slots=True)
class SourcePatch:
    """A metadata description plus the new source bytes for one write."""

    data: bytes
    fields: tuple[str, ...]
    source_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": "write_source_input",
            "fields": list(self.fields),
            "source_keys": list(self.source_keys),
        }


def build_source_patch(
    source: bytes,
    live: bytes,
    *,
    fields: Mapping[str, str],
    owners: Mapping[str, str],
    reverse_sync: Mapping[str, str],
    selected_fields: tuple[str, ...],
) -> SourcePatch:
    """Patch only explicitly mapped top-level JSON source keys."""

    if any(
        token in source.decode("utf-8", errors="ignore") for token in ("{{", "{%", "{#")
    ):
        raise MutationError(
            "reverse-sync does not rewrite dynamic templates",
            code="reverse_sync_unsupported",
        )
    source_value = parse_public_object(source, code="reverse_sync_unsupported")
    live_value = parse_public_object(live, code="reverse_sync_unsupported")
    if not selected_fields:
        raise MutationError(
            "at least one field must be selected", code="field_required"
        )

    patched = dict(source_value)
    source_keys: list[str] = []
    for name in selected_fields:
        if name not in fields:
            raise MutationError("field is not declared", code="field_not_declared")
        if owners.get(name) not in {"live", "merge"}:
            raise MutationError("field is not live-owned", code="field_not_selectable")
        source_key = reverse_sync.get(name)
        if source_key is None:
            raise MutationError(
                "field has no reverse-sync mapping", code="reverse_sync_unmapped"
            )
        source_keys.append(source_key)
        value = live_value.get(name, _MISSING)
        if value is _MISSING:
            patched.pop(source_key, None)
        else:
            patched[source_key] = value
    return SourcePatch(
        data=encode_public_json(patched),
        fields=tuple(selected_fields),
        source_keys=tuple(source_keys),
    )
