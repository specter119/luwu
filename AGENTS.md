# Luwu Agent Instructions

## Document role

Luwu is still at the seed stage. This file is the root entry point for agents and maintainers. It defines cross-cutting boundaries, documentation ownership, and a lightweight working method.

It is not the complete product brief, architecture, API reference, testing handbook, release guide, or roadmap. Do not turn a currently attractive implementation idea into a permanent rule here. Route each concrete topic to its owning document.

The product seed is in docs/product.md. Any document must distinguish its intended design from the current implementation; planned behavior must never be presented as existing behavior.

## Product summary

Luwu is a configuration orchestrator for dotfiles, developer tools, and agent configuration. It should make meaningful drift explainable, make responsibility visible, and let people choose an auditable next action. The product problem, scope, non-goals, and value tests belong to [docs/product.md](docs/product.md), not to this summary.

## Non-negotiable boundaries

- Observation and mutation are separate. Inspection, planning, and ordinary reverse sync must not write implicitly. A mutation must be explicit and preceded by an explainable plan.
- Scope and ownership are explicit. Undeclared content is not implicitly adoptable; conflicts stop for review instead of being hidden behind a generic force or precedence rule.
- Reverse sync is a structured, auditable acceptance process, not a file copy. Only declared fields may be written back, and the result must be recalculated after that write.
- Secrets are minimized and fail closed. Provider-managed secrets must not enter the repository, persistent state, diffs, logs, caches, backups, or machine-readable output.
- Writes must respect the existing target and retain a recoverable boundary. A successful deployment is not worth damaging undeclared content, permissions, or symlinks.
- Semantic responsibility comes before implementation convenience. A formatter, cache, or application side effect must not change who owns a value.
- External capabilities are explicit. Dependencies, subprocesses, and network access must not silently bootstrap themselves at runtime.
- Product goals, designs, implementation status, and verification results must be labeled honestly.

## Shared vocabulary

These terms form a shared mental model; they do not prescribe a class hierarchy or storage format:

- global and local: shared inputs and machine-private inputs;
- provider: a controlled external source of values, with its own provenance and sensitivity;
- live: configuration currently used or changed by an application or user;
- baseline: the last state explicitly accepted as a comparison point;
- desired: the state calculated from the current declared inputs;
- source, live, merge, provider, and ignore: ownership vocabulary for deciding how a field may change.

Document ownership and configuration ownership are different. A document owner is an accountability pointer for maintaining and routing a document; it is not proof of authority or a source of truth. Configuration ownership must eventually affect allowed transitions and be enforced by the resource or field contract, planning, validation, and tests.

Concrete reconciliation algorithms, field-path syntax, formatting rules, persistence layouts, and provider protocols belong in their owning documents.

## Current starting point, not a permanent blueprint

The seed implementation uses Python with uv, explicit `.j2` templates, and narrow provider boundaries; rbw is a first provider experiment. These are starting assumptions, not product invariants or a complete future architecture. Detailed contracts belong in the document that owns the subject and should be added when implementation and evidence make them useful.

## Documentation map and single ownership

Every normative fact has one owner. Other documents may summarize it briefly, but must link to the owner instead of copying a rule, field table, or competing explanation.

| Document                   | Owns                                                              | Does not own                                                   |
| -------------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------- |
| AGENTS.md                  | agent routing, global boundaries, and document governance         | full product narrative, internal algorithms, command reference |
| README.md                  | user-facing overview, use cases, and quick start                  | agent workflow, internal contracts, decision history           |
| docs/product.md            | product problem, scope, non-goals, and value tests                | implementation types, CLI fields, concrete algorithms          |
| docs/roadmap.md             | coarse delivery sequence and milestone boundaries                 | detailed contracts, current status, milestone closure records   |
| docs/decisions/<record>.md | why one high-impact choice was made and its consequences          | the current contract or an interface inventory                 |
| docs/milestones/<milestone>.md | fixed milestone scope, exit checklist, and closure record       | current implementation status, product direction, detailed contracts |
| docs/design.md             | how the current internal design works                             | product vision, user guide, complete public contract           |
| docs/reference.md          | stable manifest, CLI, JSON, error, and compatibility contracts    | rationale, speculative designs, maintenance process            |
| docs/maintenance.md        | development, testing, release, migration, and dependency workflow | product goals or complete algorithm definitions                |
| docs/status.md             | what is implemented, exploratory, or not started                  | product scope or design authority                              |

The map is a route, not a demand to create empty files. Create a specialist document only when a topic has an independent audience, change rate, or review boundary. A specialist document may include a short status or scope note when it prevents a real ambiguity; do not add `Owner`, `Scope`, or `Does not define` metadata as a ritual. If a status is used, it must distinguish early direction from accepted and verified behavior.

README.md is for users, AGENTS.md is for agents, and decision records explain history rather than silently defining current behavior. If two documents disagree, do not average them: identify the owner, record the conflict, and repair the documentation.

## Agent working method

1. Read this file first, then read the owning document and the affected code, schema, fixtures, and tests required by the task.
1. Inspect the actual state before proposing a change. Treat planned behavior as unverified until code, tests, or a status document provide evidence.
1. Identify the owning layer and document. Update that owner when behavior changes; update this file only when a cross-cutting boundary or the documentation map changes.
1. Keep behavior, verification, schema or migration, and documentation aligned without duplicating normative rules. Create a decision record only for a high-impact choice.
1. Report actual changes and verification, including important checks not run and relevant security, migration, or documentation impact. Do not commit, push, or change branch policy unless asked.

If docs/status.md does not exist, do not infer that planned capabilities are implemented; use evidence from code and tests, and establish that status document when implementation starts.

## Shan Hai Jing note

《山海经·西山经》中的陆吾，负责守护和管理昆仑一方，并“掌管天之九部及帝之囿时”。Luwu 借用的是这种“先守边界、再辨归属”的精神；形象和产品气质可以在后续设计中重新诠释，不把古籍中的形貌直接当作 UI 约束。
