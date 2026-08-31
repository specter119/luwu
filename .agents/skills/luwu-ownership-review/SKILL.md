---
name: luwu-ownership-review
description: >
  Use when reviewing Luwu ownership, scope, provenance, adoption, precedence,
  provider or live values, reverse sync, or conflict handling. Test whether a
  change keeps configuration authority explicit and auditable. Do not use for
  generic code style or filesystem-hardening review without an ownership
  question.
---

# Luwu ownership review

Treat configuration as a responsibility map, not merely as content to copy.
Declared ownership and scope must be visible; live, undeclared, or conflicting
content must not become accepted through an implicit precedence rule.

Review the owning contract plus the affected loader, planner, mutation path,
provider boundary, and tests. Ask:

- Who is allowed to change this value, and where is that authority declared?
- Does the change distinguish source, live, local, provider, baseline, merge,
  and ignored content?
- Can a convenient default silently adopt or erase undeclared content?
- If ownership changes, is the transition explicit, reviewable, and tested?

Attack auto-adoption, whole-file reverse copies, hidden application side
effects, generic force or precedence flags, ambiguous field ownership, and
conflicts that are reported as ordinary drift. Unknown authority is a finding,
not permission to guess.

Classify each claim as implemented, partial, design-only, or unstarted. Report
the protected authority, the convenience being sacrificed, path/line evidence,
the failure scenario, the smallest corrective change, and the regression test.
Use `docs/product.md` for the responsibility questions and the owning contract
for the exact transition rules.
