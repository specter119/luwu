# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ablate TOML comment handling through the production patcher.

Run from the repository root with ``uv run --script experiments/toml_comment_ablation.py``.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from luwu import m6_formats
from luwu.m6_formats import M6FormatError, parse_m6_document, patch_m6_document

CASES = (
    (
        b'model = "a#b"  # keep\r\n',
        b'model = "new"  # keep\r\n',
        True,
    ),
    (b"model = 'a#b'\t# keep\n", b'model = "new"\t# keep\n', True),
    (
        b'model = "a\\"#b" # keep\n',
        b'model = "new" # keep\n',
        True,
    ),
    (b"model = 12 # keep\n", b'model = "new" # keep\n', False),
)


def main() -> None:
    naive_breaks = 0
    for source, expected, quoted_hash in CASES:
        actual = patch_m6_document(source, format="toml", updates={"model": "new"})
        assert actual == expected
        assert tomllib.loads(actual.decode())["model"] == "new"
        assert parse_m6_document(actual, format="toml")["model"] == "new"
        if quoted_hash:
            try:
                tomllib.loads(source.decode().split("#", 1)[0])
            except tomllib.TOMLDecodeError:
                naive_breaks += 1
    assert naive_breaks == 3

    # The prior scanner validated the complete assignment suffix as one scalar.
    assert not m6_formats._is_supported_toml_scalar('"old" # keep', "old")

    reserialize_source = "# keep 雪\r\nmodel = \"old\" # note\r\nprovider = 'local'\r\n"
    expected_reserialized_source = (
        "# keep 雪\r\nmodel = \"new\" # note\r\nprovider = 'local'\r\n"
    ).encode()
    values = tomllib.loads(reserialize_source)
    values["model"] = "new"
    rewritten = "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in values.items()
    )
    assert tomllib.loads(rewritten) == values
    assert rewritten.encode() != expected_reserialized_source
    assert "#" not in rewritten

    delete_source = (
        b"# header\r\nmodel = \"old\" # remove\r\nprovider = 'local'\t# retain\r\n"
    )
    expected_delete = b"# header\r\nprovider = 'local'\t# retain\r\n"
    deleted = patch_m6_document(delete_source, format="toml", deletes=("model",))
    assert deleted == expected_delete
    assert "model" not in tomllib.loads(deleted.decode())
    assert "model" not in parse_m6_document(deleted, format="toml")

    rejected = (
        b"[section]\nmodel = 1\n",
        b"model = [1, 2]\n",
        b'"model" = 1\n',
        b"model.value = 1\n",
        b'model = """multi\nline"""\n',
    )
    for source in rejected:
        try:
            parse_m6_document(source, format="toml")
        except M6FormatError:
            continue
        raise AssertionError("unsupported TOML shape was accepted")

    print(
        json.dumps(
            {
                "experiment": "toml_comment_ablation",
                "status": "pass",
                "production_update_cases": len(CASES),
                "naive_split_breaks_quoted_cases": naive_breaks,
                "production_deletion": "pass",
                "unsupported_shapes_rejected": len(rejected),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
