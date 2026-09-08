# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Small pre-implementation counterexamples for the M3a design choices.

Run with ``uv run experiments/m3_ablation.py``. This is a design experiment,
not evidence that the production classifier or a future writer is correct.
"""

from __future__ import annotations

MISSING = object()
UNBASED = object()


def decision(baseline, desired, live, owner, *, remove=""):
    if owner == "ignore":
        return "ignored"
    if remove == "baseline":
        baseline = live
    if baseline is UNBASED:
        return "unbased"
    if remove == "missing":
        baseline, desired, live = (
            None if value is MISSING else value for value in (baseline, desired, live)
        )
    if desired == live:
        return "unchanged" if desired == baseline else "converged"
    if remove == "ownership":
        owner = "merge"
    if live == baseline:
        return "forward_candidate" if owner in {"source", "merge"} else "review"
    if desired == baseline:
        return "reverse_candidate" if owner in {"live", "merge"} else "review"
    return "forward_candidate" if remove == "conflict" else "review"


CASES = (
    ("unchanged", 0, 0, 0, "source", "unchanged"),
    ("converged", 0, 1, 1, "source", "converged"),
    ("source change", 0, 1, 0, "source", "forward_candidate"),
    ("live change", 0, 0, 1, "live", "reverse_candidate"),
    ("source owner live drift", 0, 0, 1, "source", "review"),
    ("live owner source drift", 0, 1, 0, "live", "review"),
    ("two sided conflict", 0, 1, 2, "source", "review"),
    ("merge conflict", 0, 1, 2, "merge", "review"),
    ("null creation", MISSING, None, MISSING, "source", "forward_candidate"),
    ("null deletion", None, None, MISSING, "live", "reverse_candidate"),
    ("no baseline agreement", UNBASED, 0, 0, "source", "unbased"),
    ("no baseline drift", UNBASED, 0, 1, "live", "unbased"),
    ("ignore", 0, 1, 2, "ignore", "ignored"),
)


def main():
    for removal in ("", "baseline", "ownership", "missing", "conflict"):
        failures = [
            label
            for label, baseline, desired, live, owner, expected in CASES
            if decision(baseline, desired, live, owner, remove=removal) != expected
        ]
        if removal:
            assert failures, f"ablation did not distinguish {removal}"
            print(f"remove {removal}: {len(failures)} counterexample(s)")
        else:
            assert not failures, failures
            print(f"reference: {len(CASES)} scenarios passed")

    # Selected subtrees are atomic. A generic key-path loop contributes no
    # behavior when every selected path contains exactly one literal key.
    document = {"editor": {"font": 12}, "a.b": [2, 1], "a/b": None}
    for key in (*document, "absent"):
        cursor = document
        for component in (key,):
            cursor = cursor.get(component, MISSING)
        assert cursor == document.get(key, MISSING)
    print("remove generic paths: 4 top-level selections unchanged")

    desired = {"known": 0, "runtime": 1}
    live = {"known": 0, "runtime": 2}
    undeclared_changed = {
        key: value for key, value in desired.items() if key != "known"
    } != {key: value for key, value in live.items() if key != "known"}
    assert undeclared_changed
    print("remove undeclared signal: 1 hidden-change counterexample")
    print("observation scenarios require no store, patch builder, or writer")


if __name__ == "__main__":
    main()
