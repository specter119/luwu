---
name: luwu-semantic-drift-review
description: >
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
