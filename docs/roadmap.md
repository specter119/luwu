# Luwu Delivery Roadmap

Status: directional roadmap

This document turns the current product seed into a small number of delivery milestones. It defines sequence and boundaries, not detailed contracts or current implementation status. The product direction remains in [product.md](product.md); verified implementation facts remain in [status.md](status.md); each completed milestone keeps its own closure record under [milestones/](milestones/).

The roadmap is allowed to change when evidence changes the product direction. A change to an active milestone's scope should be explicit; a closed milestone is not rewritten to absorb new work.

## Milestone map

| Milestone | Focus                                        | Completion outcome                                                                                                                                   |
| --------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| M1        | Developer confidence preview                 | A safe, forward-only single-resource loop is observable, explainable, and explicitly applicable.                                                     |
| M2        | Resource and semantic drift foundation       | Multiple declared resources and explicitly supported resource/format experiments can be compared without claiming equivalence beyond their evidence. |
| M3        | Ownership and auditable reconciliation       | Baselines, field ownership, conflicts, controlled reverse sync, and recoverable multi-resource plans have explicit contracts.                        |
| M4        | Providers, secrets, and operational maturity | External providers, secret boundaries, persistence, portability, and release-quality operational behavior are verified without hidden capabilities.  |

## M1: Developer confidence preview

M1 is the current closed functional slice. Its fixed scope and exit evidence are recorded in [milestones/m1.md](milestones/m1.md). It intentionally does not imply multi-resource orchestration, reverse sync, baselines, providers, or secret persistence.

## M2: Resource and semantic drift foundation

M2 expands the resource model only after the single-resource boundary has proven useful. It establishes the contracts and tests for multiple resources, an observed literal-copy kind beyond the M1 slice, semantic observation of a supported format, and safe handling of partial or blocked plans. M2 comparison experiments are read-only until ownership and write semantics are accepted in M3. The exact supported formats and behavior belong in the M2 reference/design documents, not in this roadmap.

## M3: Ownership and auditable reconciliation

M3 adds time and responsibility to reconciliation: accepted baselines, declared field ownership, conflict classification, selective reverse sync, and durable or recoverable plans. Multi-resource apply and rollback semantics belong here only with an explicit partial-success and recovery contract.

## M4: Providers, secrets, and operational maturity

M4 adds narrow external providers such as rbw, secret-aware inputs, persistence and cache boundaries, explicit subprocess/network capabilities, and the platform/release matrix. Provider-managed secrets must remain outside repositories, diffs, logs, caches, backups, and machine-readable output.

## Roadmap completion rule

A milestone closes only when its implementation, public contract, tests or verification evidence, and documentation agree. A capability described here is planned until its milestone closure record and current status document provide evidence that it exists.
