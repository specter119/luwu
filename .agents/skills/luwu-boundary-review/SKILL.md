---
name: luwu-boundary-review
description: >-
  Use when reviewing Luwu path validation, symlinks, target writes, atomicity,
  permissions, TOCTOU races, temporary files, or recoverability. Test whether
  a change preserves filesystem and resource boundaries under hostile state.
  Do not use for generic performance or formatting review.
---

# Luwu boundary review

The goal is not merely to finish a write. The existing target, undeclared
content, path root, permissions, and recovery boundary must survive uncertainty
and hostile changes.

## Stable core and changing contract

The stable value is: **Luwu must not cross a boundary it cannot establish, and
must preserve a recoverable state when it acts.** Root rules, no-follow flags,
descriptor-relative operations, atomic replacement, and permission behavior
are current contract mechanisms; a safer mechanism may replace them.

When the contract changes, identify the boundary invariant, compare the threat
model and failure behavior before and after, and update compatibility notes and
regression tests. Do not treat one syscall, path syntax, or platform mechanism
as sacred; do reject any change that makes an unsafe boundary look successful.

Trace every path from manifest parsing through observation, preflight, temp
entry creation, replacement, cleanup, and verification. Check:

- absolute paths, traversal, source escape, parent components, and symlink use;
- descriptor-relative no-follow operations and rechecks against races;
- regular-file assumptions, target replacement rules, and permission handling;
- atomic replacement, temporary-entry cleanup, directory durability, and what
  remains recoverable after failure.

Attack symlink swaps, symlink loops, outside-root targets, missing or changing
parents, source/target replacement during inspection, non-regular files,
permission loss, and partial writes. A blocked result is correct when the
boundary cannot be established safely.

Report the protected boundary, the successful-write convenience being rejected,
concrete path/line and test evidence, failure impact, and minimum fix. Classify
implemented, partial, design-only, or unstarted behavior and require a focused
regression test for every changed shield. Use `docs/reference.md` for the
stable write contract and `docs/design.md` for its mechanism.

## Scope boundary

This stance owns filesystem and resource-boundary questions: path roots,
symlinks, descriptors, permissions, replacement, cleanup, and recoverability.
It does not own who is authorized to change a value, whether an action had
consent, what a representation means, or whether a claim is current. Hand
those findings to the ownership, consent, semantic-drift, or verifiability
stance instead of redefining their contracts here.

## Good patterns

- Re-observe staged no-follow identity at a replace boundary and classify an
  uncertain result as unknown instead of inferring it from equal bytes.
  Evidence: [M3 repair record](../../../docs/milestones/m3-repair-plan.md).
- Check journal and lock paths together before either persistent entry is
  created. Evidence: [M3 follow-up record](../../../docs/milestones/m3-followup.md).

## Bad patterns

- Treat equal target content as proof that this process performed the
  replacement. This loses provenance at the exact boundary where the writer
  may have raised.
- Create a journal lock or temporary entry before checking its full path
  relationship with declared resources. This can mutate a declared target
  before the requested operation begins.

Patterns are review evidence, not a replacement for the public write contract.
Promote a repeated pattern only in the owning document and link back here.
