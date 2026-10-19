---
name: luwu-duplication-review
description: >-
  Use when a Luwu change adds or edits a filesystem, atomic-write, baseline,
  format, validation, or name-classification routine, adds a manifest version
  or an "independent" capability, or when reviewing whether the same primitive
  exists in more than one place. Test whether each primitive has one
  implementation that every caller imports. Do not use to judge write safety
  itself (boundary) or how the repository is named and laid out (coherence).
---

# Luwu duplication review

A duplicated primitive is a defect that no single test, contract, or diff
shows. Each copy passes its own tests and honors its own contract. The cost
appears later, when the copies disagree and nobody can say which one is right.

## Why this went unnoticed for several rounds

This records the mechanism, not blame. Every round followed the rules it had.

- **Review looked at the diff, not the inventory.** The other review stances
  (boundary, consent, ownership, confidentiality) ask whether a change keeps
  its own contract. A new, correct, fail-closed copy of an atomic writer
  passes all of them. No stance asked "does another one exist".
- **"Independent and additive" was read as an implementation rule.** The
  reference described each new manifest version as an independent capability,
  not an upgrade. That is a compatibility promise about contracts. In practice
  it licensed copying the engine, so three engines (versions 1-6, the pilot,
  and version 8) carried eight atomic-write implementations and about 900
  duplicated lines.
- **Frozen versions made the shared copy feel unsafe.** Once an older version
  was "verified", editing a shared primitive risked invalidating that
  verification. Copy-and-adapt looked cheaper and safer than extend-and-retest.
- **Each copy had its own tests and ablations.** Green tests per copy gave
  assurance that the copies were fine, and none of them compared copies. Test
  count and coverage grew while the duplication grew with them.
- **Names hid the relationship.** Modules carried delivery labels (`m6_*`,
  `m7_*`), so searching for a concept never landed on both copies side by side.
- **The duplicates drifted.** Five predicates for "is this name secret-bearing"
  disagreed on `apikey`, `access_key`, dashes, and camel case. A value one layer
  rejected, another admitted.

## Stable core

**One behavior, one implementation, and every caller imports it.** The
primitives that must stay single are the no-follow filesystem and atomic write
module, the sensitive-name predicate, strict JSON parsing and equality, and the
baseline envelope. The owner and module names are in
[design](../../../docs/design.md#shared-primitives).

## Attack playbook

Run these before accepting a change that touches a primitive:

- Search by behavior, not by name:
  `rg "os\.replace|O_NOFOLLOW|fsync|casefold|isalnum" src`. Two modules
  matching the same operation is a finding.
- Compare function shapes across modules: two functions with the same
  parameters and the same sequence of syscalls are one function.
- Look for a predicate or constant table defined twice (`_SENSITIVE_*`,
  fragment tuples, mode checks). Feed both the same names and compare.
- For a new manifest version or "independent" capability, ask which primitives
  it reuses. The answer must be "all of them", and the contract must differ,
  not the writer.
- For a guard that looks redundant, disable it in a private copy and run the
  suite. If nothing fails, either add the missing fault-injection test or
  delete the guard; do not keep it on faith.
- Check that a deleted copy's tests moved to the shared module's tests instead
  of vanishing with it.

## Good patterns

- Extend the shared module with a parameter or a second function and add a test
  per caller. Evidence: `write_file` and `create_parent` in
  `src/luwu/filesystem.py` serve inputs, baselines, and targets.
- One predicate, one table-driven test that pins its behavior. Evidence:
  `src/luwu/sensitivity.py` and `tests/formats/test_sensitivity.py`.
- Retire an old contract instead of keeping it alive beside a new one.
  Evidence: the retirement entry in [changelog](../../../docs/changelog.md).

## Bad patterns

- A new `*_filesystem.py`, `*_baseline.py`, or `*_formats.py` beside an
  existing one "so the older version is untouched".
- Re-implementing an open, stage, and replace sequence inline in a feature
  because the shared function lacked one option.
- Treating more tests as evidence that nothing is duplicated.

Report the primitive, every place it exists, where the copies disagree, and the
smallest change that leaves one implementation. The write-safety judgment itself
belongs to `luwu-boundary-review`, and layout and naming to
`luwu-coherence-review`.
