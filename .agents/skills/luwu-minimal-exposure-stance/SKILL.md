---
name: luwu-minimal-exposure-stance
description: >-
  Use when reviewing repositories, persistence, caches, logs, JSON output, and
  provider interfaces in Luwu. Test whether sensitive values and external
  capability stay out of machine-readable output and fail closed when uncertain.
  Do not use for ensuring a later person or agent can tell what happened, why,
  and what safe action continues.
disable-model-invocation: true
metadata:
  stance-axis: exposure-vs-continuity
  opposes: luwu-continuable-evidence-stance
---

# Luwu minimal exposure stance

This stance aggressively restricts the footprint of secrets and provider
capabilities. It argues that safe execution means minimizing exposure surfaces
even if that leaves an operator or agent with less context when a run fails or
a system state diverges.

## Objective

Sensitive values and external capability stay out of repositories, persistence,
caches, logs and machine-readable output, and fail closed when uncertain.

## Accepts this harm

By refusing to expand exposure, this stance explicitly accepts these costs to
`luwu-continuable-evidence-stance`:

- `luwu-continuable-evidence-stance` loses the ability to distinguish a provider rotation from a hand edit, because a secret digest or keyed hash is refused (`docs/design.md#rejected-alternatives`).
- `luwu-continuable-evidence-stance` loses a persistent execution journal or exact reviewed-plan token that an interrupted run could resume from, because persisting secrets or their hashes is forbidden (`docs/reference.md#finite-batch-freshness-and-verification`).
- `luwu-continuable-evidence-stance` loses knowing which undeclared keys differ and in which direction, and must instead trust the operator's claim for `--undeclared PATH`, because storing the prior undeclared values would persist them (`docs/status.md#daily-loop-changes`, `docs/reference.md#writing-undeclared-changes-back-to-a-template-source`).
- `luwu-continuable-evidence-stance` loses provider-diagnostic commands and raw stdout/stderr in error messages, because provider output must fail closed and never enter logs or CLI projections (`docs/design.md#rejected-alternatives`).

## Attacks

This stance attacks any change that broadens exposure, including:

- Adding a provider cache or a provider registry that keeps secret values in memory or on disk longer than necessary (`docs/design.md#rejected-alternatives`).
- Persisting an execution journal or staging plan that records rendered provider values or exact text diffs (`docs/reference.md#finite-batch-freshness-and-verification`).
- Introducing a diagnostic command or verbose error output that prints `rbw` stdout/stderr or parsed secrets (`docs/design.md#rejected-alternatives`).
- Storing a digest or hash of secret bytes to distinguish rotations, which could be guessable offline (`docs/design.md#rejected-alternatives`).
- Falling back, silently skipping, or guessing when `rbw` is stopped or unauthorized, instead of immediately failing closed (`docs/status.md#provider-fields-and-real-rbw-acceptance`).
- Emitting provider-bearing bytes in the CLI JSON plan output, bypassing the requirement to emit metadata only (`docs/changelog.md`).

## Evidence to demand

- Tests simulating provider failure must verify that the exception message or exit code does not contain the value, and the capability fails closed.
- A change parsing or serializing data must demonstrate that it passes through `luwu.sensitivity` checks and omits provider fields in the written baseline.
- Any temporary storage of secret-bearing bytes must use unnamed files (`O_TMPFILE`) rather than a named staging file (`docs/design.md#rejected-alternatives`).

## Does not own

This stance does not own the traceability of delivery order, the semantic
equality of representations, or the recovery of interrupted operations. Refer
concerns about what a later person or agent needs to safely continue to
`luwu-continuable-evidence-stance`.

## Escalates when

Escalate when a proposed feature demands writing a subset of provider output to
a log for debugging, introduces a persistent cache for performance, requests a
named temporary file to survive a SIGKILL, or masks a provider failure behind a
default value.

## Stable core and changing contract

The stable core is: **Luwu must minimize sensitive information and capability
exposure, and fail closed when uncertain.**
Today's mechanisms include metadata-only projections, `O_TMPFILE` for secret
bytes, and the `rbw` provider executable boundaries
(`docs/reference.md#provider-owned-fields`). These mechanisms may change (for
example, supporting a new provider type or a format parser for secrets), but
they must never expand unreviewed information or execution power.

## Good patterns

- Staging provider-bearing bytes in an unnamed file (`O_TMPFILE`) so that a process kill cannot leave a recoverable path (`docs/reference.md#provider-owned-fields`).
- Rejecting `--undeclared PATH` with a sensitive name unless explicitly permitted, without storing the undeclared bytes to prove the conflict (`docs/reference.md#writing-undeclared-changes-back-to-a-template-source`).

## Bad patterns

- Matching provider items using substrings (like `rbw get --field`) instead of exact matching, which would expose and read the entire vault item (`docs/design.md#rejected-alternatives`, `docs/status.md#provider-fields-and-real-rbw-acceptance`).
- Creating a named staging file (like `.luwu-tmp-*`) for provider values that Luwu does not sweep, meaning a SIGKILL leaves the secret on disk (`docs/status.md#provider-fields-and-real-rbw-acceptance`, `docs/reference.md#provider-owned-fields`).

The set and its oppositions are registered in `.agents/skills/luwu-stance-registry/SKILL.md`.
