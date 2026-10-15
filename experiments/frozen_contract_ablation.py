# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fast guard-on/off probes for the final M3 repair plan.

These are design probes over temporary data.  The production regression suite
must still exercise the real writers, journal, CLI, and recovery paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def selective_patch(
    source: bytes, *, start: int, end: int, replacement: bytes
) -> bytes:
    return source[:start] + replacement + source[end:]


def canonical_rewrite(source: bytes) -> bytes:
    value = json.loads(source)
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def publication_state(
    *,
    old_identity: tuple[int, int],
    staged_identity: tuple[int, int],
    target_identity: tuple[int, int] | None,
    temporary_identity: tuple[int, int] | None,
    replace_raised: bool,
    guard: bool,
) -> str:
    if not replace_raised:
        return "replaced"
    if not guard:
        return "replaced" if target_identity is not None else "not_replaced"
    if target_identity == staged_identity and temporary_identity is None:
        return "replaced"
    if target_identity == old_identity and temporary_identity == staged_identity:
        return "not_replaced"
    return "indeterminate"


def schema_accepts(document: dict[str, object], *, guard: bool) -> bool:
    if not guard:
        return isinstance(document.get("resources"), list)
    resources = document.get("resources")
    if not isinstance(resources, list) or not resources:
        return False
    ordinals = [item.get("ordinal") for item in resources if isinstance(item, dict)]
    if ordinals != list(range(len(resources))):
        return False
    if document.get("next_ordinal") != len(resources):
        return False
    allowed = {"missing", "unsafe", "regular", "symlink", "other"}
    for resource in resources:
        if not isinstance(resource, dict):
            return False
        for path in resource.get("paths", []):
            if not isinstance(path, dict):
                return False
            condition = path.get("postcondition")
            if not isinstance(condition, dict) or condition.get("type") not in allowed:
                return False
    return True


def span_probe(*, guard: bool) -> bool:
    source = b'{\r\n  "setting":1.00,\n  "runtime":1,\n  "undeclared":"\\u0061"\r\n}'
    marker = b'"runtime":1'
    start = source.index(marker) + len(b'"runtime":')
    end = start + 1
    result = (
        selective_patch(source, start=start, end=end, replacement=b"2")
        if guard
        else canonical_rewrite(source).replace(b'"runtime":1', b'"runtime":2')
    )
    return result[:start] == source[:start] and result[start + 1 :] == source[end:]


def publication_probe(*, guard: bool) -> bool:
    with TemporaryDirectory(prefix="luwu-m3-ablation-") as directory:
        root = Path(directory)
        target = root / "target"
        staged = root / "staged"
        target.write_bytes(b"old")
        staged.write_bytes(b"new")
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        staged_identity = (staged.stat().st_dev, staged.stat().st_ino)
        try:
            os.replace(staged, target)
            raise OSError("injected after replace")
        except OSError:
            target_identity = (target.stat().st_dev, target.stat().st_ino)
            temporary_identity = (
                None
                if not staged.exists()
                else (
                    staged.stat().st_dev,
                    staged.stat().st_ino,
                )
            )
        actual = publication_state(
            old_identity=old_identity,
            staged_identity=staged_identity,
            target_identity=target_identity,
            temporary_identity=temporary_identity,
            replace_raised=True,
            guard=guard,
        )
        if actual != "replaced":
            return False

        # Equal content from an independent writer is not evidence of this replace.
        target.unlink()
        target.write_bytes(b"new")
        independent_identity = (target.stat().st_dev, target.stat().st_ino)
        return (
            publication_state(
                old_identity=old_identity,
                staged_identity=staged_identity,
                target_identity=independent_identity,
                temporary_identity=None,
                replace_raised=True,
                guard=guard,
            )
            == "indeterminate"
        )


def schema_probe(*, guard: bool) -> bool:
    invalid_documents = (
        {"resources": [], "next_ordinal": 0},
        {"resources": [{"ordinal": 1, "paths": []}], "next_ordinal": 2},
        {
            "resources": [
                {
                    "ordinal": 0,
                    "paths": [{"postcondition": {"type": "invented"}}],
                }
            ],
            "next_ordinal": 1,
        },
    )
    return all(
        not schema_accepts(document, guard=guard) for document in invalid_documents
    )


def main() -> None:
    assert span_probe(guard=True)
    assert not span_probe(guard=False)
    print(
        "span preservation: guard passes; removing local spans rewrites protected bytes"
    )

    assert publication_probe(guard=True)
    assert not publication_probe(guard=False)
    print(
        "publication identity: guard distinguishes staged replace from equal external bytes"
    )

    assert schema_probe(guard=True)
    assert not schema_probe(guard=False)
    print("record value domain: guard rejects empty/gapped/unknown records")
    print(
        "No formatter, transaction class, second ledger, recovery engine, or schema registry required"
    )


if __name__ == "__main__":
    main()
