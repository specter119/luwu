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
