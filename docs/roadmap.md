# Luwu Delivery Roadmap

Status: directional roadmap; M1-M4 are closed within their recorded scopes.

This document turns the product seed into a small number of delivery
milestones. It defines sequence and boundaries, not detailed contracts or the
current implementation snapshot. Product direction remains in
[product.md](product.md); verified implementation facts remain in
[status.md](status.md); each completed milestone keeps its own closure record
under [milestones/](milestones/).

The roadmap is allowed to change when evidence changes the product direction. A change to an active milestone's scope should be explicit; a closed milestone is not rewritten to absorb new work.

## Milestone map

| Milestone | Focus                                        | Completion outcome                                                                                                                      |
| --------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| M1        | Developer confidence preview                 | Closed: a safe, forward-only single-resource loop is observable, explainable, and explicitly applicable.                                |
| M2        | Resource and semantic drift foundation       | Closed: declared resources and supported format experiments are compared without claiming unsupported equivalence.                      |
| M3        | Ownership and auditable reconciliation       | Closed: baselines, ownership, conflicts, controlled reverse sync, and recoverable plans have explicit contracts.                        |
| M4        | Providers, secrets, and operational maturity | Closed: external providers, secret boundaries, persistence, portability, and release behavior are verified without hidden capabilities. |

## M1: Developer confidence preview

M1 was the initial closed functional slice. Its fixed scope and exit evidence
are recorded in [milestones/m1.md](milestones/m1.md). It intentionally did not
imply multi-resource orchestration, reverse sync, baselines, providers, or
secret persistence.

## M2: Resource and semantic drift foundation

M2 expanded the resource model after the single-resource boundary had proven
useful. It established multiple-resource observation, an observed literal-copy
kind, semantic observation of a supported format, and safe handling of partial
or blocked plans. M2 comparison experiments were read-only until ownership and
write semantics were accepted in M3. The exact behavior belongs in
[reference](reference.md), not in this roadmap.

## M3: Ownership and auditable reconciliation

M3 added time and responsibility to reconciliation: accepted baselines,
declared field ownership, conflict classification, selective reverse sync, and
durable or recoverable plans. Multi-resource apply and rollback semantics were
separated by explicit partial-success and recovery boundaries.

## M4: Providers, secrets, and operational maturity

M4 added a narrow rbw provider, secret-aware inputs, persistence and cache
boundaries, explicit subprocess authority, and the platform/release matrix.
The current v6 contract remains narrow; it does not imply network providers or
automatic recovery.

## Roadmap completion rule

A milestone closes only when its implementation, public contract, tests or verification evidence, and documentation agree. A capability described here is planned until its milestone closure record and current status document provide evidence that it exists.
