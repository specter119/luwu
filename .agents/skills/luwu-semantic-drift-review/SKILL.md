---
name: luwu-semantic-drift-review
description: >-
  Use when reviewing Luwu drift detection, rendering, normalization,
  formatting, parsers, structured comparison, or reverse synchronization. Test
  whether the implementation distinguishes meaningful configuration changes
  from presentation noise without changing ownership. Do not use for generic
  formatter or parser implementation review without a drift question.
---

# Luwu semantic-drift review

Compare the meaning of the declared desired resource with the live state, not
blindly with source bytes. But keep equivalence narrow and justified: a
formatter or parser must not become an implicit owner or silently discard
unsupported syntax.

## Stable core and changing contract

The stable value is: **Luwu must distinguish consequential configuration
meaning from representation noise without changing authority.** Status names,
normalization rules, parser subsets, and reverse-sync syntax are contract
expressions of that value and may change with evidence.

When the contract changes, state the equivalence relation and action boundary,
test both false-drift and false-in-sync cases, preserve unsupported information,
and document any migration or compatibility impact. Do not preserve a current
normalizer for its own sake; do reject silent semantic loss or ownership drift.

For each comparison, identify the representation, normalization, and action.
Ask:

- Is the desired side rendered or otherwise interpreted at the correct layer?
- Which differences are formatting, meaningful drift, application state, or
  undeclared content?
- Does normalization preserve information and avoid false in-sync results?
- Can reverse sync update only declared inputs or fields instead of copying
  live content wholesale?

Attack line-ending, whitespace, final-newline, comments, ordering, duplicate,
unknown-field, encoding, template, parser-loss, and formatter-rewrite cases.
Demand evidence for both false positives and false negatives.

Report the equivalence contract, the authority it preserves, path/line and test
evidence, the convenience traded away, and the minimum corrective change. Mark
implemented, partial, design-only, or unstarted. Use `docs/product.md` for the
semantic intent and `docs/reference.md` for current comparison states.

## Scope boundary

This stance owns meaning versus representation: equivalence relations,
normalization, parser subsets, formatting noise, information loss, and
semantic reverse-sync edits. It does not own who is authorized to change a
field, whether a mutation was confirmed, filesystem safety, sensitive-value
exposure, or documentation freshness. Route those findings to the ownership,
consent, boundary, confidentiality, or verifiability stance.

## Good patterns

- Define a narrow strict-JSON equivalence relation that preserves object
  members and array order, distinguishes booleans from numbers, and reports
  unsupported syntax as blocked. Evidence: the M2 contract in
  `docs/reference.md`.
- Patch only selected literal-JSON spans so unselected and undeclared bytes are
  preserved. Evidence:
  [M3 repair record](../../../docs/milestones/m3-repair-plan.md).

## Bad patterns

- Re-encode a complete source object to update one selected field. This can
  silently change whitespace, escaping, number spelling, or undeclared
  content.
- Treat a generic formatter or parser as permission to discard unsupported
  syntax or to call a semantically different document in sync.

Patterns describe comparison risks. Parser names, supported syntax, and public
status values remain owned by the reference contract.
