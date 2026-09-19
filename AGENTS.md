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
  fields may be written back, and the result is recalculated after the write.
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

| Fact type                                                                      | Sole owner                                             | Allowed elsewhere                                                                                                       |
| ------------------------------------------------------------------------------ | ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| Product problem, direction, scope, non-goals, and value tests                  | `docs/product.md`                                      | A short rationale or link; no CLI, schema, or current-status definition                                                 |
| Stable manifest, CLI, JSON, error, compatibility, and public safety contracts  | `docs/reference.md`                                    | Design explains mechanism; status says whether it is implemented; milestones record the historical decision or evidence |
| Current checkout implementation facts and latest verified state                | `docs/status.md`                                       | README gives a user summary; milestones preserve dated historical snapshots                                             |
| Internal flow, module responsibilities, and implementation mechanisms          | `docs/design.md`                                       | Link to the public contract; do not redefine public fields or states                                                    |
| Delivery sequence and milestone boundaries                                     | `docs/roadmap.md`                                      | Milestones may expand their own historical scope; neither roadmap nor milestones redefine unrelated contracts           |
| Development, test, release, migration, and documentation workflow              | `docs/maintenance.md`                                  | Commands may be shown as examples; their behavior is owned by `reference.md`                                            |
| Fixed milestone scope, review decisions, counterexamples, and closure evidence | `docs/milestones/*.md`                                 | Status links to the record; after closure, stable behavior is owned by `reference.md`                                   |
| User overview and quick start                                                  | `README.md`                                            | Link to the owner for details; do not copy internal history or contract tables                                          |
| Review stance and attack playbook                                              | `.agents/skills/*/SKILL.md`                            | Skills inspect the owner documents and implementation; they do not own product or API contracts                         |
| Why a high-impact cross-cutting choice was made                                | `docs/decisions/<record>.md` when such a record exists | Link from the affected owner; do not create a decision record for routine implementation history                        |

The map is a routing aid, not permission to duplicate content. A document may
mention the same subject at a different fact type: product says why,
reference says what, design says how, status says whether, a milestone says
what was true at a date, and a skill says how to attack the claim. The wording
must make that distinction visible.

Do not use a non-owner document to repair a conflict. First identify the fact
type and owner, then update the owner and replace the other statement with a
link or a clearly dated historical note. Do not average conflicting
documents. For a closed milestone, do not rewrite history to match a later
implementation; record the later fact in the current owner.

## Scope and language rules

- Repository documentation is maintained in English. There is no
  `README_cn.md` mirror. Do not add a second-language normative copy without a
  separately approved owner and synchronization policy.
- A field list, state table, error meaning, or command promise belongs in
  `docs/reference.md`, even when a milestone originally introduced it.
- A test count, temporary artifact path, or dated gate result belongs in the
  relevant milestone record when it is historical. `docs/status.md` links to
  it instead of replaying the log.
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

## Shared vocabulary

Use the product vocabulary consistently: global and local inputs, provider,
live state, baseline, desired state, source/live/merge/provider/ignore
ownership, and declared scope. Product meaning belongs in
`docs/product.md`; exact field and transition semantics belong in
`docs/reference.md`.

## Project note

The name Luwu is inspired by the guardian role in the *Shan Hai Jing*. That
image is a project character, not a UI constraint or a product contract.
