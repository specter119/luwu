---
name: luwu-primitive-reuse
description: >-
  Use BEFORE writing or copying code that opens, stats, writes, replaces,
  locks, or syncs a file or directory; parses JSON or TOML; computes a baseline
  or digest; or decides whether a name is secret-bearing. Also use when a
  shared function lacks an option you need, when a task says "independent",
  "additive", or "do not touch the old path", when you fix a bug in one such
  routine, and when reviewing a diff for a second copy. Find the one
  implementation and extend it. Do not use to judge whether a write is safe
  (boundary) or how files are named (coherence).
---

# Luwu primitive reuse

Stance: **the author, not the reviewer, prevents duplication.** A copy is born
when someone needs a capability, does not find the owner, and writes it
inline. Each copy then passes its own tests, so review of a diff cannot see it.

## When a duplicate gets written

Check yourself against these moments. They are the only ones that have
produced copies here.

1. **You need a capability and did not look.** You write `os.open`,
   `os.replace`, `json.loads`, or a secret-name check because it is quicker
   than finding the existing one.
1. **The shared function is almost right.** It lacks one option, so you inline
   a variant. Add the option instead.
1. **A task says "independent", "additive", or "leave the old one alone".**
   That is a promise about contracts. It is not permission to copy the writer.
1. **You fix a bug in one routine.** Its siblings carry the same bug. Search
   for them in the same change.
1. **Parallel or isolated work.** A subagent never saw the other module. The
   guard test below catches what context cannot.

## Do this before you write

- Read [design: shared primitives](../../../docs/design.md#shared-primitives)
  and use the named module. Search by behavior, not by name:
  `rg "os\.(open|replace|fsync)|json\.loads|tomllib\.loads" src`.
- If the owner lacks something, extend it with a parameter or a second
  function and add one test per caller.
- Run `python -m unittest tests.tooling.test_primitive_ownership`. It fails
  when a module outside the owner calls a primitive, and names the owner.
  Adding a ceiling to `OWNERS` is recording debt. It needs a reason in the
  change and is never the first answer.

## Review a diff for a copy

- Same syscalls in the same order as an existing function means one function.
- A predicate or constant table defined twice (`_SENSITIVE_*`, mode checks):
  feed both the same names and compare.
- A guard that looks redundant: disable it in a private copy and run the
  suite. If nothing fails, add the missing fault-injection test or delete the
  guard. Do not keep it on faith.
- A deleted copy's tests moved to the shared module's tests.

## Limits of the guard

The guard sees calls to named primitives. It does not see a re-implemented
algorithm that uses other calls, so it does not replace the steps above. The
ceilings in `OWNERS` list known debt, for example the descriptor walks in
`manifest.py`. Tests per copy and rising coverage are not evidence that
nothing is duplicated.

Report the primitive, every place it exists, where the copies disagree, and the
smallest change that leaves one implementation.
