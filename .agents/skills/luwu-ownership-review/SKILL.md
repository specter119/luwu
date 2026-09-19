---
name: luwu-ownership-review
description: >-
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

## Stable core and changing contract

The stable value is: **Luwu must keep who may change a value answerable.**
Fields such as `owner`, `scope`, provider roles, and reverse-sync rules are
contract expressions of that value, not the value itself. They may change when
evidence warrants it.

When a contract changes, compare the old and new allowed transitions, show how
authority remains explicit, version or migrate the rule when necessary, and
update tests and status honestly. Do not defend a current field shape merely
because it is current; do reject any change that makes authority implicit.

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

## Scope boundary

This stance owns responsibility and authority: declared owner, scope,
provenance, adoption, baseline role, provider/live role, and reverse-sync
eligibility. It does not own the filesystem mechanics, authorization prompt,
secret exposure, semantic equivalence, or current-document status. Route those
findings to the boundary, consent, confidentiality, semantic-drift, or
verifiability stance.

## Good patterns

- Treat an absent baseline as `unbased` and a change outside declared fields
  as a separate undeclared signal; neither silently grants a candidate.
  Evidence: the M3 contract in `docs/reference.md`.
- Permit reverse sync only through an explicit declared literal mapping and
  preserve unselected or undeclared source content. Evidence:
  [M3 repair record](../../../docs/milestones/m3-repair-plan.md).

## Bad patterns

- Use a generic force or precedence rule to adopt undeclared live content or
  to erase a conflict.
- Re-read a convenient baseline and use it as authorization when the planner
  classified a different baseline. Evidence:
  [M3 follow-up record](../../../docs/milestones/m3-followup.md).

Patterns explain how to attack authority mistakes; the resource and field
contract remains owned by `docs/product.md` and `docs/reference.md`.
