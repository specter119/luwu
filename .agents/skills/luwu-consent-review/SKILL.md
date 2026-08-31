---
name: luwu-consent-review
description: >
  Use when reviewing Luwu inspect, plan, apply, confirmation, dry-run,
  stale-state, verification, or any operation that can mutate configuration.
  Test whether explicit authorization remains separate from observation. Do not
  use for read-only architecture review without an action boundary.
---

# Luwu consent review

Treat a configuration change as an authorization flow rather than a file-copy
call. Observation must be read-only; a plan must explain the proposed action;
mutation must be explicit, revalidated, and followed by fresh verification.

Trace the complete sequence from CLI or API input through planning, confirmation,
preflight, writes, errors, and post-write recalculation. Ask:

- Can inspect or plan write, persist, bootstrap, or invoke an external action?
- Is the exact plan the user reviewed the one that can be applied?
- Are manifest, source, target, and no-op observations checked for staleness?
- Does a blocked, failed, or partially completed operation leave an honest
  result and a safe next action?

Attack hidden writes, prompt bypasses, broad `--force` behavior, stale no-op
plans, hand-built plans, partial multi-file success, missing verification, and
errors that conceal what happened. Automation is acceptable only when its
authority and recovery behavior are explicit.

Report a short state-transition timeline with path/line and test evidence,
classify behavior as implemented, partial, design-only, or unstarted, and name
the smallest corrective change plus its regression test. Use the owning CLI or
reference contract for exact exit codes and output promises.
