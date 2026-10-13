# Luwu Implementation Status

Status: M5 core optimization is implemented. Its current source benchmark,
profiling, provider-latency, and process-interruption evidence is present, and
the installed wheel passes the full suite; the installed-artifact benchmark
matrix is still pending, so current M5 revalidation remains partial as of
2026-10-13. M6 is in development with a version 7 pilot slice; its complete
technical acceptance and user-trial gate are not closed. Automatic replay,
rollback, and strong consistency against unrelated writers remain outside the
verified scope.

This document owns the current implementation snapshot. It does not define
product direction, public command or manifest behavior, internal mechanisms,
or historical closure evidence. See [product](product.md),
[reference](reference.md), [design](design.md), and the
[milestone records](milestones/) for those subjects.

## Current capability matrix

| Area                      | Current status | Authority and evidence                                                                                                                                                         |
| ------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| M1 preview                | Implemented    | The single-resource template and symbolic loop is covered by the M1 contract in [reference](reference.md) and the M1 closure record                                            |
| M2 observation            | Implemented    | Multi-resource observation, literal copy, and strict JSON comparison remain read-only under the M2 contract                                                                    |
| M3a ownership observation | Implemented    | Public JSON field observation, baseline classification, ownership decisions, and undeclared-change reporting are implemented within the v3 contract                            |
| M3b selective mutation    | Implemented    | Explicit baseline acceptance and literal-JSON reverse sync are implemented within the v4 contract                                                                              |
| M3c execution             | Implemented    | Public whole-file execution, metadata-only records, partial outcomes, and read-only recovery are implemented within the v5 contract                                            |
| M4 provider execution     | Implemented    | The independent v6 provider, secret-target, metadata-cache, and platform-fail-closed paths are implemented within the v6 contract                                              |
| M5 batch execution        | Partial        | Indexed validation, digest-based stale checks, bounded journal publication, fixed source benchmark rows, current profiling/provider reports, and process-kill rows are implemented; the installed-artifact benchmark matrix remains open in [M5](milestones/m5.md) |
| M6 pilot slice            | Partial        | Version 7 roots, public input provenance, bounded JSON/TOML patching, field-aware forward apply, baseline acceptance, selected reverse input sync, dual-format rehearsal, uncertainty observation, and isolated Dotter handoff are implemented; the complete acceptance table and user-trial gate remain open |

The exact fields, states, error codes, output projections, compatibility
behavior, and unsupported cases are owned only by [reference](reference.md).
The rows above are status claims, not a second contract.

M5 and M6 remain on the active revalidation path; M7-M9 remain planned. Their
delivery sequence is in [roadmap](roadmap.md#next-delivery-sequence). The
current M6 implementation slice is covered by focused tests and a sanitized
Pi JSON/Codex TOML closed-loop fixture using normal HOME containment, absent
target creation, two bidirectional cycles, post-write uncertainty, fresh
read-only observation, and an isolated Dotter handoff. The complete acceptance
table is not yet a closure claim: the retained evidence does not cover every
two-sided conflict and format-specific deletion/null combination as a single
acceptance artifact, and it does not authorize a real target. The motivating
real-dotfiles pilot and M5's dated historical evidence remain recorded in
[M5](milestones/m5.md#closure-evidence-2026-09-22).

## Current re-audit boundary — 2026-10-13

The current work repairs the benchmark fixture and sample classification,
records current profiling and provider latency, adds subprocess SIGKILL rows,
and retains the installed full-suite result. It does not claim the interrupted
installed benchmark matrix. It also hardens the M6 version 7 slice without
changing v1-v6 contracts; M6 technical completion will be recorded only after
the remaining acceptance evidence is retained and reviewed.

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
