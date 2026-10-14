# Luwu Implementation Status

Status: M5 revalidation and M6 technical acceptance are complete as of
2026-10-14. The bounded version 7 Pi/Codex pilot is ready for the user's first
trial; live trial results and feedback remain pending. The complete M7 version
8 scope is authorized and its technical core is implemented. Main-session
source, package, ablation, performance, and applicable code checks have passed.
No stable Droid workspace-trust file
schema has been verified, so M7 is not marked complete. M7a's version 7
implementation and isolated acceptance remain complete; its evidence is
recorded in [M7](milestones/m7.md). The M6 trial is not a blocker for M7.
Automatic replay, rollback, and strong consistency against unrelated writers
remain outside the verified scope.

This document owns the current implementation snapshot. It does not define
product direction, public command or manifest behavior, internal mechanisms,
or historical closure evidence. See [product](product.md),
[reference](reference.md), [design](design.md), and the
[milestone records](milestones/) for those subjects.

## Current capability matrix

| Area                      | Current status                            | Authority and evidence                                                                                                                                                                                                         |
| ------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| M1 preview                | Implemented                               | The single-resource template and symbolic loop is covered by the M1 contract in [reference](reference.md) and the M1 closure record                                                                                            |
| M2 observation            | Implemented                               | Multi-resource observation, literal copy, and strict JSON comparison remain read-only under the M2 contract                                                                                                                    |
| M3a ownership observation | Implemented                               | Public JSON field observation, baseline classification, ownership decisions, and undeclared-change reporting are implemented within the v3 contract                                                                            |
| M3b selective mutation    | Implemented                               | Explicit baseline acceptance and literal-JSON reverse sync are implemented within the v4 contract                                                                                                                              |
| M3c execution             | Implemented                               | Public whole-file execution, metadata-only records, partial outcomes, and read-only recovery are implemented within the v5 contract                                                                                            |
| M4 provider execution     | Implemented                               | The independent v6 provider, secret-target, metadata-cache, and platform-fail-closed paths are implemented within the v6 contract                                                                                              |
| M5 batch execution        | Implemented                               | The complete installed-artifact matrix, current profiling/provider reports, interruption tests, and safety regressions are revalidated in [M5](milestones/m5.md)                                                               |
| M6 bidirectional pilot    | Technical acceptance complete             | The application-shaped Pi/Codex loop, write-boundary regressions, complete format/conflict acceptance, uncertainty observation, and isolated Dotter handoff are verified in [M6](milestones/m6.md); user trial remains pending |
| M7a TOML inline comments  | Implemented; isolated acceptance complete | Current version 7 contract and mechanism are in [reference](reference.md#m6-pilot-extension-manifest-version-7) and [design](design.md#version-7-pilot-mechanism); evidence is in [M7](milestones/m7.md)                       |
| M7 full version 8 scope   | Technical core implemented; closure open  | The complete authorized scope is in [reference](reference.md#m7-full-dotfiles-coverage-manifest-version-8); release evidence and remaining closure constraints are in [M7](milestones/m7.md)                                   |

The exact fields, states, error codes, output projections, compatibility
behavior, and unsupported cases are owned only by [reference](reference.md).
The rows above are status claims, not a second contract.

The complete M7 scope is authorized and proceeds without waiting for M6 trial
feedback. M7a implementation and isolated acceptance remain complete; M7
version 8 remains open only for its unresolved
Droid workspace-trust schema boundary. Its manifest, format,
input, baseline, filesystem, engine, and CLI technical core is implemented.
The final release source, wheel, sdist, production ablation, and applicable
code checks passed after the allocation cleanup. The installed performance
matrix also passed the applicable budgets. No stable Droid workspace-trust
file schema is available; this remaining closure constraint is recorded in the
[M7 record](milestones/m7.md).
The [M7 record](milestones/m7.md#final-source-package-and-code-check-release--2026-10-14)
records the release evidence and remaining schema constraint. The public behavior is owned by
[reference](reference.md#m7-full-dotfiles-coverage-manifest-version-8).
The engine source exposes `effective_mode`, uses
`m7_reverse_consumer_change` for a reverse write that would change another
declared consumer, gives undeclared resource drift an actionable reason under
the existing `m7_review_required` code, and now integrates independent
`m7_plan_evidence` for the complete plan projection; the approved removal of
caller-origin references and the repeated origin-graph check is now present in
the current engine. Current tests cover isolated
app-shaped OpenCode, Codex, and Droid settings workflows, but no application
runtime has loaded or verified them. Luwu does not enforce application-specific
enum domains under the [version 8 contract](reference.md#m7-full-dotfiles-coverage-manifest-version-8).
Droid workspace trust has no stable public file schema in the reviewed
evidence, and actual application runtimes have not been tested. The release
checks and remaining closure constraints are recorded in the M7 milestone;
overall M7 completion remains open.
M8-M9 remain planned under
the [roadmap](roadmap.md#next-delivery-sequence). Start with the
[isolated trial guide](../tests/fixtures/m6/README.md) and review the
[per-target handoff](maintenance.md#m6-isolated-rehearsal-and-dotter-handoff)
before selecting a real target; this M6 operator workflow is separate from M7
development. The installed rehearsal uses normal HOME
containment and synthetic public values; it does not launch either application
or real Dotter and does not authorize a live deployment.

## Current re-audit boundary — 2026-10-14

The benchmark now validates every resource and postcondition independently
of command exit status. The failed first scaling run is retained alongside
the complete passing final matrix. M6 authorization is rechecked around
replacement and during final verification; partial and uncertain writes
retain their observed outcomes. Independent byte expectations, format
negative controls, source/wheel/sdist suites, and copyable CLI trial steps
support the current acceptance record. Versions 1-6 retain their contracts.

## Verified scope and limits

The latest recorded M5 closure is in
[milestones/m5.md](milestones/m5.md). Earlier M4, M3 repair and execution evidence
is preserved in the linked M3 milestone records. Those records contain dated
commands, test counts, temporary artifact paths, and historical counterexamples;
they are not current status and are not repeated here.

The implementation does not claim:

- automatic replay, rollback, or recovery mutation;
- exact reviewed-plan consent tokens;
- strong consistency against writers that ignore Luwu's advisory locks;
- network providers or a broad platform matrix beyond the verified contract.

When code or tests change, refresh this snapshot from current evidence. Do not
carry a historical completion statement forward without rerunning or
explicitly revalidating the relevant gate.
