---
name: luwu-adversarial-review
description: >
  Use when reviewing or changing Luwu reconciliation, manifests, providers,
  persistence, filesystem writes, CLI contracts, or related documentation, and
  the question is whether a convenience or feature preserves Luwu's underlying
  value trade-offs. Do not use for generic style review, ordinary lint fixes,
  or a release checklist without a Luwu boundary question.
---

# Luwu adversarial review

Review the implementation as a set of value claims, not as a collection of
happy-path features. Read `AGENTS.md`, the owning document, affected code, and
tests. Treat code and tests as evidence of current behavior; treat product and
design prose as direction unless verified.

## Value tensions

For every proposed change, test both sides of these tensions:

- **Accountability over convenience**: declared ownership and scope must remain
  visible; never auto-adopt live or undeclared content behind precedence.
- **Explicit consent over automation**: observation, planning, and mutation are
  separate; an action must be explainable before it is authorized and checked
  again before it writes.
- **Boundary integrity over overwrite success**: uncertainty, symlinks,
  traversal, races, and undeclared content stop the operation; atomic and
  recoverable writes matter more than completing a copy.
- **Semantic truth over byte noise**: distinguish meaningful drift from narrow,
  justified formatting differences, without turning a formatter into an owner.
- **Confidentiality over diagnostic richness**: secrets, rendered values, and
  unnecessary state must not enter output, logs, persistence, caches, or diffs;
  external capabilities stay explicit and fail closed.
- **Verifiability and continuity over breadth and magic**: prefer a small,
  explicit contract with stable observations and reasons over an expansive
  feature whose partial-success, migration, or rollback behavior is unknown.
  Current M1 limits are validation scope, not automatically permanent product
  rules.

## Review method

1. State the changed boundary and the owning document.
2. For each relevant tension, record the protected value, the convenience or
   capability being traded away, and concrete code/test evidence.
3. Attack the boundary: hidden writes, inferred ownership, broad force flags,
   stale plans, TOCTOU races, symlink escapes, secret persistence, implicit
   network/subprocess access, lossy normalization, partial multi-file writes,
   and machine-readable leakage.
4. Classify each claim as **implemented**, **partial**, **design-only**, or
   **unstarted**. Never promote a roadmap statement to a capability.
5. Report findings with path/line evidence, failure scenario, violated value,
   and the smallest corrective change. Require a regression test when a
   boundary is changed.

Use `docs/product.md` for value tests, `docs/design.md` for current rationale,
`docs/reference.md` for stable contracts, and `docs/status.md` for verified
scope. Resolve conflicts through document ownership; do not average them.

## Output

Return: a short decision, a value-tension ledger, prioritized findings, missing
tests or contract updates, and explicit unknowns. A passing happy path is not
evidence that a safety or ownership boundary survived.
