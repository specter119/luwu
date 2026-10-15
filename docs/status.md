# Luwu Implementation Status

Status: manifest versions 1 through 8 are implemented and technically
verified in isolated, synthetic environments. The bounded version 7 Pi/Codex
pilot is ready for the user's first trial; trial results and feedback are
pending. Version 8 is not marked complete because no stable Droid
workspace-trust file schema has been verified. Automatic replay, rollback,
and strong consistency against unrelated writers remain outside the verified
scope.

This document owns the current implementation snapshot. It does not define
product direction, public command or manifest behavior, internal mechanisms,
or delivery history. See [product](product.md), [reference](reference.md),
[design](design.md), [roadmap](roadmap.md), and the [changelog](changelog.md)
for those subjects.

## Current capability matrix

| Capability                       | Manifest versions | Status                                       | Open limits                                                                                               |
| -------------------------------- | ----------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Template, symbolic, copy         | 1, 2, 8           | Implemented                                  | No formatting equivalence beyond the declared comparison strategies                                       |
| Multi-resource observation       | 2, 8              | Implemented                                  | A blocked resource is reported; it does not hide the others                                               |
| Structured comparison            | 2, 7, 8           | Implemented                                  | Strict JSON; bounded TOML in 7; JSONC and nested TOML in 8; YAML is unsupported                           |
| Ownership and baselines          | 3-8               | Implemented                                  | Undeclared content is not adopted implicitly                                                              |
| Reverse sync and accept          | 4, 7, 8           | Implemented                                  | Declared fields only; version 8 mutates one resource at a time without joint cross-resource selection     |
| Whole-file execution and journal | 5, 6              | Implemented                                  | No replay or rollback; recovery is read-only                                                              |
| Providers and secrets            | 6                 | Implemented                                  | `rbw` only through explicit subprocess authority; no network providers                                    |
| Batch performance                | 5, 8              | Implemented                                  | Budgets are in [maintenance](maintenance.md#performance-acceptance); rerun them on the target environment |
| Roots and local inputs           | 7, 8              | Implemented                                  | Public values only; the user trial of version 7 is pending                                                |
| Profiles and directories         | 8                 | Implemented                                  | No application runtime has loaded the synthetic OpenCode, Codex, or Droid fixtures                        |
| Droid workspace trust            | none              | Not implemented; no stable file schema known | Blocks an unconditional version 8 completion claim; see [roadmap](roadmap.md)                             |

The exact fields, states, error codes, output projections, compatibility
behavior, and unsupported cases are owned only by [reference](reference.md).
The rows above are status claims, not a second contract.

## Verified scope and limits

Verification is the test suite under `tests/`, the ablations and benchmarks in
`experiments/`, and the commands in [maintenance](maintenance.md). Their
output is not committed; rerun them against the checkout and installed
artifact before relying on a result.

The implementation does not claim:

- automatic replay, rollback, or recovery mutation;
- exact reviewed-plan consent tokens;
- strong consistency against writers that ignore Luwu's advisory locks;
- network providers or a broad platform matrix beyond the verified contract;
- that any application read a synthetic fixture, or that any real target may be
  handed off. The per-target procedure is in
  [maintenance](maintenance.md#isolated-rehearsal-and-dotter-handoff) and the
  [isolated trial guide](../tests/fixtures/pilot_v7/README.md).

When code or tests change, refresh this snapshot from current evidence. Do not
carry a completion statement forward without rerunning or explicitly
revalidating the relevant gate.
