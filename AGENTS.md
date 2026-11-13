# Luwu Agent Instructions

## Role and authority

Luwu is still at the seed stage. This file is the entry point for agents and
maintainers. It owns agent working method, cross-cutting handling boundaries,
and documentation routing. It is not the product brief, public contract,
implementation design, status report, release handbook, or milestone history.

The product seed is in [docs/product.md](docs/product.md). Planned behavior
must never be presented as current behavior. When a statement could describe
both a desired direction and an implementation fact, classify it before
editing a document.

## Cross-cutting handling boundaries

These are rules for agent and maintainer work. Their product rationale belongs
to `docs/product.md`; their public behavior belongs to `docs/reference.md`.

- Keep observation and mutation separate. Inspection, planning, and ordinary
  reverse sync must not write implicitly. A mutation is explicit and follows
  an explainable plan.
- Keep scope and ownership explicit. Undeclared content is not implicitly
  adoptable. Conflicts stop for review instead of being hidden by a generic
  force or precedence rule.
- Treat reverse sync as structured, auditable acceptance. Only declared
  fields, or leaves the operator names in the request, may be written back,
  and the result is recalculated after the write.
- Minimize secrets and fail closed. Provider-managed secrets must not enter
  repositories, persistent state, diffs, logs, caches, backups, or
  machine-readable output.
- Preserve existing targets and recoverable boundaries. A successful
  deployment is not worth damaging undeclared content, permissions, or
  symlinks.
- Keep semantic responsibility ahead of implementation convenience. A
  formatter, cache, or application side effect must not change who owns a
  value.
- Make external capabilities explicit. Dependencies, subprocesses, and
  network access must not bootstrap themselves at runtime.
- Label product goals, designs, implementation status, historical evidence,
  and verification results honestly.

## Documentation ownership

Ownership is by **fact type**, not by which file first mentioned a subject.
Every normative fact has one owner. Other documents may summarize it with a
short sentence and a link, or record it as historical evidence, but must not
create a second definition.

| Fact type                                                                                     | Sole owner                                                                                          | Allowed elsewhere                                                                                               |
| --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Product problem, direction, scope, non-goals, and value tests                                 | `docs/product.md`                                                                                   | A short rationale or link; no CLI, schema, or current-status definition                                         |
| Stable manifest, CLI, JSON, error, compatibility, and public safety contracts                 | `docs/reference.md`                                                                                 | Design explains mechanism; status says whether it is implemented; the changelog says when it arrived            |
| Current checkout implementation facts and latest verified state                               | `docs/status.md`                                                                                    | README gives a user summary; run output is not committed                                                        |
| Internal flow, module responsibilities, and implementation mechanisms                         | `docs/design.md`                                                                                    | Link to the public contract; do not redefine public fields or states                                            |
| Delivery sequence, open exit conditions, and unfinished milestone boundaries                  | `docs/roadmap.md`                                                                                   | The changelog may name a delivery; neither redefines unrelated contracts                                        |
| Development, test layout, performance budgets, release, migration, and documentation workflow | `docs/maintenance.md`                                                                               | Commands may be shown as examples; their behavior is owned by `reference.md`                                    |
| What each delivery added and a capability-by-delivery matrix                                  | `docs/changelog.md`                                                                                 | Status and roadmap link to it; it never defines current behavior or open gates                                  |
| User overview and quick start                                                                 | `README.md`                                                                                         | Link to the owner for details; do not copy internal history or contract tables                                  |
| Review stance and attack playbook                                                             | `.agents/skills/*/SKILL.md`                                                                         | Skills inspect the owner documents and implementation; they do not own product or API contracts                 |
| Why each stance axis exists, the stake inventory, and the deferred or unrepresented stakes    | `docs/stances.md`                                                                                   | The registry lists the installed set and its conflicts; a stance file argues one side; none restates a contract |
| Why a high-impact cross-cutting choice was made, and rejected alternatives                    | `docs/design.md` (rejected alternatives), or `docs/decisions/<record>.md` when such a record exists | Link from the affected owner; do not create a decision record for routine implementation history                |

The map is a routing aid, not permission to duplicate content. A document may
mention the same subject at a different fact type: product says why,
reference says what, design says how, status says whether, the changelog says
what a delivery added, and a skill says how to attack the claim. The wording
must make that distinction visible.

Do not use a non-owner document to repair a conflict. First identify the fact
type and owner, then update the owner and replace the other statement with a
link. Do not average conflicting documents. The changelog is not rewritten to
match a later implementation; record the later fact in the current owner.

## Scope and language rules

- Repository documentation is maintained in English. There is no
  `README_cn.md` mirror. Do not add a second-language normative copy without a
  separately approved owner and synchronization policy.
- A field list, state table, error meaning, or command promise belongs in
  `docs/reference.md`, even when a delivery originally introduced it.
- A test count, temporary artifact path, benchmark sample, or dated gate result
  is run output, not repository content. Do not commit it. Record what a
  delivery added in `docs/changelog.md` and keep a check as a rerunnable
  command or test.
- A skill pattern is evidence for review, not a new product rule. Promote a
  pattern to the relevant owner only when code, tests, or repeated maintenance
  experience justify a stable rule.
- This file does not define subagent mutual exclusion or scheduling. Multiple
  reviewers may inspect the same scope. Document ownership is a content
  boundary, not a runtime lock.

## Agent working method

1. Read this file, then read the owner document for the requested change.
1. Inspect the actual code, schema, fixtures, tests, and current status needed
   to distinguish implemented behavior from intent or history.
1. Classify each proposed statement as product intent, public contract,
   current fact, design mechanism, workflow, historical evidence, or review
   pattern. If two owners appear possible, stop and resolve the classification
   before editing.
1. Change the owner. In other documents, replace repeated definitions with a
   concise summary and a relative link.
1. Search the repository after editing for duplicate definitions, stale status
   claims, broken owner links, and language-policy violations.
1. Run the narrowest relevant tests, documentation checks, and repository
   gates. Mark blocked or unrun checks explicitly.
1. Report what changed, what remains outside scope, which evidence was used,
   and how the next maintainer can continue.

If `docs/status.md` does not exist, do not infer that planned capabilities are
implemented. Establish the status document when implementation starts.

## Adversarial stance review

Luwu's design tradeoffs are held as opposed **stances**: each is a
first-principles objective that would, if fully pursued, harm another one. They
are not checklists and cannot all be satisfied at once. The stance that owns a
question, the oppositions that must be reviewed together, the decisions already
taken, and the stakes nothing represents are all in
[the stance registry](.agents/skills/luwu-stance-registry/SKILL.md). Read the
registry before reviewing, and read the one stance you are assigned.

Run a stance review when a change moves a documented tradeoff — a contract, a
lookup or write boundary, a cost budget, an exposure surface, or a recovery
path — and when the change was argued for rather than merely implemented. A
routine fix that moves no tradeoff does not need one.

The shape is three steps:

1. Assign **one stance per reviewer**. Reviewers work independently and do not
   read each other's output; a reviewer that has read its opponent anchors on it
   and the disagreement collapses.
1. Reconcile the findings. Agreement merges, independent findings stand, and a
   finding that harms another stance is a **conflict**.
1. Adjudicate in the orchestrator: look for a design that serves both stances
   before accepting either. If no such design exists and the choice changes a
   contract irreversibly, stop and put the tradeoff to the user rather than
   picking a winner.

Record the outcome in the registry: a buffer that both stances accept, a
decision that sacrificed one stance, or an open conflict awaiting a decision. A
losing stance is never deleted, and a conflict is never resolved by splitting
the difference. When a review finds a design or contract that deviates from the
intent an owner document states, add the pattern to the counterexample library
so the next reviewer recognizes it.

The working procedure belongs to the installed `hydra-*` skills. This section is
only the entry point: it says that the practice exists, where the ledger lives,
and what a reviewer must not do.

## Shared vocabulary

Use the product vocabulary consistently: global and local inputs, provider,
live state, baseline, desired state, source/live/merge/provider/ignore
ownership, and declared scope. Product meaning belongs in
`docs/product.md`; exact field and transition semantics belong in
`docs/reference.md`.

## Project note

The name Luwu is inspired by the guardian role in the *Shan Hai Jing*. That
image is a project character, not a UI constraint or a product contract.
