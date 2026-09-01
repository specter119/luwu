---
name: luwu-verifiability-review
description: >
  Use when reviewing Luwu feature scope, status, manifests, JSON or error
  contracts, documentation ownership, tests, migrations, rollback, or claims
  that a capability is complete. Test whether the behavior is small, explicit,
  and continuable by the next maintainer or agent. Do not use for generic
  project management or release checklists without a Luwu contract question.
---

# Luwu verifiability review

Prefer a narrow contract whose observations, reasons, errors, and recovery path
can be checked by people and agents. Feature breadth is not progress when
partial success, migration, rollback, or platform behavior is undefined.

## Stable core and changing contract

The stable value is: **a future actor must be able to know what happened, why,
and what safe action can continue the work.** Manifest versions, JSON shapes,
status labels, documentation ownership, and M1 scope are contract expressions
for a stage; they may change when evidence supports a better rule.

When the contract changes, compare what the old and new rules let a person or
agent observe and recover, define migration or compatibility behavior, and
update tests and status. Do not freeze a narrow contract as architecture; do
reject expansion that makes behavior untestable, ambiguous, or discontinuous.

Read `AGENTS.md` and the owning document before judging status. Trace the
feature through implementation, public output, fixtures, tests, and status
documentation. Ask:

- Is this behavior implemented, or only described as a direction?
- Are unsupported cases explicit instead of silently generalized?
- Do machine-readable results let the next actor continue without hidden
  context?
- Are migration, rollback, partial-success, versioning, and platform limits
  defined before expanding scope?
- Does each changed normative fact have one documentation owner?

Attack happy-path-only tests, status inflation, duplicated conflicting docs,
unstated defaults, unstable error contracts, multi-file partial writes,
unversioned schema changes, and M1 limitations accidentally treated as eternal
architecture.

Return a maturity matrix of implemented, partial, design-only, and unstarted
claims, with path/line evidence, missing contract or test, and the smallest
next practice. Use `docs/status.md` for current facts, `docs/reference.md` for
stable contracts, and `docs/product.md` for future intent.
