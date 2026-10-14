# Luwu Implementation Status

Status: M5 revalidation and M6 technical acceptance are complete as of
2026-10-14. The bounded version 7 Pi/Codex pilot is ready for the user's first
trial; live trial results and feedback remain pending. Automatic replay,
rollback, and strong consistency against unrelated writers remain outside the
verified scope.

This document owns the current implementation snapshot. It does not define
product direction, public command or manifest behavior, internal mechanisms,
or historical closure evidence. See [product](product.md),
[reference](reference.md), [design](design.md), and the
[milestone records](milestones/) for those subjects.

## Current capability matrix

| Area                      | Current status                | Authority and evidence                                                                                                                                                                                                         |
| ------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| M1 preview                | Implemented                   | The single-resource template and symbolic loop is covered by the M1 contract in [reference](reference.md) and the M1 closure record                                                                                            |
| M2 observation            | Implemented                   | Multi-resource observation, literal copy, and strict JSON comparison remain read-only under the M2 contract                                                                                                                    |
| M3a ownership observation | Implemented                   | Public JSON field observation, baseline classification, ownership decisions, and undeclared-change reporting are implemented within the v3 contract                                                                            |
| M3b selective mutation    | Implemented                   | Explicit baseline acceptance and literal-JSON reverse sync are implemented within the v4 contract                                                                                                                              |
| M3c execution             | Implemented                   | Public whole-file execution, metadata-only records, partial outcomes, and read-only recovery are implemented within the v5 contract                                                                                            |
| M4 provider execution     | Implemented                   | The independent v6 provider, secret-target, metadata-cache, and platform-fail-closed paths are implemented within the v6 contract                                                                                              |
| M5 batch execution        | Implemented                   | The complete installed-artifact matrix, current profiling/provider reports, interruption tests, and safety regressions are revalidated in [M5](milestones/m5.md)                                                               |
| M6 bidirectional pilot    | Technical acceptance complete | The application-shaped Pi/Codex loop, write-boundary regressions, complete format/conflict acceptance, uncertainty observation, and isolated Dotter handoff are verified in [M6](milestones/m6.md); user trial remains pending |

The exact fields, states, error codes, output projections, compatibility
behavior, and unsupported cases are owned only by [reference](reference.md).
The rows above are status claims, not a second contract.

M7-M9 remain planned and wait for M6 user feedback. The delivery sequence is
in [roadmap](roadmap.md#next-delivery-sequence). Start with the
[isolated trial guide](../tests/fixtures/m6/README.md) and review the
[per-target handoff](maintenance.md#m6-isolated-rehearsal-and-dotter-handoff)
before selecting a real target. The installed rehearsal uses normal HOME
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
