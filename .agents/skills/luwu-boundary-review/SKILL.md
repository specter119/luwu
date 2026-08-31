---
name: luwu-boundary-review
description: >
  Use when reviewing Luwu path validation, symlinks, target writes, atomicity,
  permissions, TOCTOU races, temporary files, or recoverability. Test whether a
  change preserves filesystem and resource boundaries under hostile state. Do
  not use for generic performance or formatting review.
---

# Luwu boundary review

The goal is not merely to finish a write. The existing target, undeclared
content, path root, permissions, and recovery boundary must survive uncertainty
and hostile changes.

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
