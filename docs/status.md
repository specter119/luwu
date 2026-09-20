# Luwu Implementation Status

Status: M4 complete within the frozen public v1-v6 contracts. Automatic replay,
rollback, and strong consistency against unrelated writers remain outside the
verified scope.

This document owns the current implementation snapshot. It does not define
product direction, public command or manifest behavior, internal mechanisms,
or historical closure evidence. See [product](product.md),
[reference](reference.md), [design](design.md), and the
[milestone records](milestones/) for those subjects.

## Current capability matrix

| Area                      | Current status | Authority and evidence                                                                                                                              |
| ------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| M1 preview                | Implemented    | The single-resource template and symbolic loop is covered by the M1 contract in [reference](reference.md) and the M1 closure record                 |
| M2 observation            | Implemented    | Multi-resource observation, literal copy, and strict JSON comparison remain read-only under the M2 contract                                         |
| M3a ownership observation | Implemented    | Public JSON field observation, baseline classification, ownership decisions, and undeclared-change reporting are implemented within the v3 contract |
| M3b selective mutation    | Implemented    | Explicit baseline acceptance and literal-JSON reverse sync are implemented within the v4 contract                                                   |
| M3c execution             | Implemented    | Public whole-file execution, metadata-only records, partial outcomes, and read-only recovery are implemented within the v5 contract                 |
| M4 provider execution     | Implemented    | The independent v6 provider, secret-target, metadata-cache, and platform-fail-closed paths are implemented within the v6 contract                   |

The exact fields, states, error codes, output projections, compatibility
behavior, and unsupported cases are owned only by [reference](reference.md).
The rows above are status claims, not a second contract.

M5-M9 are planned, not implemented; their delivery sequence is in
[roadmap](roadmap.md#next-delivery-sequence). The motivating real-dotfiles
pilot and its batch-performance limitation are recorded in
[M5](milestones/m5.md#motivation-isolated-pilot-on-2026-09-20).

## Verified scope and limits

The latest recorded M4 closure is in
[milestones/m4.md](milestones/m4.md). Earlier M3 repair and execution evidence
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
