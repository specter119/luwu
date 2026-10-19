"""Tests that each shared primitive keeps a single implementation."""

from __future__ import annotations

import ast
import unittest
from collections import Counter
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "luwu"

# Calls that implement a primitive. ``None`` means unlimited in that module.
# A finite number is recorded debt: the module still carries its own copy, and
# the ceiling stops the copy from growing. Lower it when a copy is removed.
# A module that is not listed may not call the primitive at all.
OWNERS: dict[str, dict[str, int | None]] = {
    "os.open": {"filesystem.py": None, "manifest.py": 7, "engine.py": 1},
    "os.replace": {"filesystem.py": None},
    "os.rename": {},
    "os.fsync": {"filesystem.py": None},
    "os.fchmod": {"filesystem.py": None},
    "os.mkdir": {"filesystem.py": None},
    "os.unlink": {"filesystem.py": None},
    "os.link": {},
    "os.symlink": {},
    "shutil.copyfile": {},
    "shutil.move": {},
    "tempfile.mkstemp": {},
    "tempfile.mkdtemp": {},
    "json.loads": {"semantic.py": None, "baseline.py": 1, "formats.py": 1},
    "tomllib.loads": {"formats.py": None, "manifest.py": 1},
}
OWNER_HINT = {
    "os.open": "luwu.filesystem (open_parent_directory, open_directory, observe_file)",
    "os.replace": "luwu.filesystem.write_file",
    "os.fsync": "luwu.filesystem.write_file and sync_directory",
    "json.loads": "luwu.semantic strict JSON parsing",
    "tomllib.loads": "luwu.formats strict TOML parsing",
}
OTHER_HINT = "luwu.filesystem; extend it with a parameter or a second function"

# Method names that write bytes on any receiver; the filesystem module owns them.
WRITING_METHODS = {"write_bytes", "write_text"}

# Name fragments that classify a name as secret-bearing live only in sensitivity.
SENSITIVE_LITERALS = {"apikey", "accesskey", "privatekey"}


def _calls(path: Path) -> Counter[str]:
    found: Counter[str] = Counter()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        function = node.func
        if function.attr in WRITING_METHODS:
            found[f".{function.attr}"] += 1
        if isinstance(function.value, ast.Name):
            found[f"{function.value.id}.{function.attr}"] += 1
    return found


class PrimitiveOwnershipTests(unittest.TestCase):
    def test_primitives_are_called_only_from_their_owner(self) -> None:
        problems: list[str] = []
        for path in sorted(SOURCE.glob("*.py")):
            counts = _calls(path)
            for primitive, modules in OWNERS.items():
                used = counts[primitive]
                allowed = modules.get(path.name, 0)
                if allowed is not None and used > allowed:
                    owner = OWNER_HINT.get(primitive, OTHER_HINT)
                    problems.append(
                        f"{path.name} calls {primitive} {used}x (allowed {allowed}). "
                        f"Use {owner}, or extend it. Do not add a private copy."
                    )
            for method in WRITING_METHODS:
                if counts[f".{method}"] and path.name != "filesystem.py":
                    problems.append(
                        f"{path.name} calls .{method}(). Write through "
                        "luwu.filesystem.write_file so the write is atomic."
                    )
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_secret_name_fragments_are_defined_only_in_sensitivity(self) -> None:
        problems: list[str] = []
        for path in sorted(SOURCE.glob("*.py")):
            if path.name == "sensitivity.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and node.value in SENSITIVE_LITERALS:
                    problems.append(
                        f"{path.name}:{node.lineno} repeats a secret-name fragment. "
                        "Call luwu.sensitivity.is_sensitive_name instead."
                    )
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_recorded_debt_still_exists(self) -> None:
        stale = [
            f"{module} no longer calls {primitive} {ceiling}x; lower OWNERS"
            for primitive, modules in OWNERS.items()
            for module, ceiling in modules.items()
            if ceiling is not None and _calls(SOURCE / module)[primitive] < ceiling
        ]
        self.assertEqual(stale, [], "\n" + "\n".join(stale))


if __name__ == "__main__":
    unittest.main()
