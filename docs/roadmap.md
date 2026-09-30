# Luwu Delivery Roadmap

Status: M1-M5 are closed within their recorded scopes. M6-M9 are planned;
M6 is the next development priority.

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
| M5        | Batch performance and interruption safety    | Closed: everyday batch operations meet the recorded latency budgets without weakening validation or recovery.                           |
| M6        | First usable bidirectional pilot             | Planned: real Pi and Codex public configuration round-trips safely; deliver for user trial.                                             |
| M7        | Feedback-led dotfiles coverage               | Provisional: expand the proven workflow to more resources, profiles and formats after M6 user feedback.                                 |
| M8        | Real provider and mixed-input integration    | Planned: public/local/provider inputs work together without persistent secret intermediates.                                            |
| M9        | Real-project migration qualification         | Planned: a selected dotfiles profile has measured coverage, a tested handoff, and explicit residual dependencies.                       |

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

## Next delivery sequence

The [2026-09-20 isolated pilot and M5 record](milestones/m5.md) motivate the
remaining four planned milestones. This is a dependency sequence, not a
calendar estimate or a promise of feature parity with every Dotter
installation. M5 is closed; implement M6's complete bidirectional pilot next.
Deliver M6 for the user's first trial and wait for their feedback before
starting M7-M9 implementation. M7-M9 are provisional directions: revise their
scope and acceptance cases from that feedback before proceeding. M8 integrates
with the resulting ownership rules; M9 qualifies the combined workflow. Do not
advance automatically from passing M6 tests to later implementation or to live
deployment.

The milestone identifiers do not allocate manifest versions. Before changing
public behavior, define the compatible extension or version transition in
[reference](reference.md), explain mechanisms in [design](design.md), and
update [status](status.md) only from implementation evidence. Closed M1-M5
records retain their historical meaning.

## M5: Batch performance and interruption safety

M5 made the existing batch capability practical before adding more resources
or formats. It delivered reproducible scaling measurements, removed repeated
global validation and journal-processing costs, and retained the current
stale-state, confidentiality, durable-write and partial-outcome guarantees.
The closure evidence and residual limits are owned by the
[M5 delivery record](milestones/m5.md).

M5 does not add roots, profiles, parsers, providers or automatic recovery.
It closes on measured end-to-end commands and interrupted execution, not a
faster helper benchmark or an improved progress display.

## M6: First usable bidirectional pilot

Deliver the core value in one connected real workflow, including the minimum
roots, declared local inputs and format support it requires. The
[M6 pilot record](milestones/m6.md) owns its bounded scope, end-to-end acceptance
cases and user handoff. This milestone must not stop at forward deployment or
an artificial literal-JSON demonstration.

M6 is the first planned user-trial release. It does not imply that the entire
dotfiles repository can migrate. Deliver an installed artifact, explicit
target allowlist, ownership handoff instructions and known limitations, then
collect user feedback before expanding the product.

## M7: Feedback-led dotfiles coverage

Provisional scope, to be revised after M6 user feedback:

- named profiles, deterministic package selection and global/local overlays;
- inspectable directory expansion, literal copy, explicit parent/mode handling
  and adoption support beyond the pilot;
- JSONC/OpenCode and additional structured mappings such as Codex project/hook
  trust state and Droid trust state, prioritized by observed usage;
- better handling and explanations for the undeclared-change and conflict
  cases encountered in the trial, without silent adoption or data loss.

Exit criteria will select the next concrete workflow set from that feedback.
Every added format or resource must retain the M6 bidirectional guarantees
where applicable and M5 performance budgets. Include machine exclusions such
as WSL mihomo non-ownership. A universal Dotter importer, broad YAML support
and all historical reverse-sync slots are not automatic scope commitments.

## M8: Real provider and mixed-input integration

Provisional scope; implementation starts only after the M6 feedback checkpoint
and review of the selected M7 dependencies.

Connect public and machine-local inputs with provider-owned secrets in one
explainable rendering/deployment workflow. Establish bounded rbw environment
and socket discovery, executable authority, and explicit failure behavior.
Integrate one representative enterprise gateway consumer without generating
secret-bearing public intermediate files. Keep provider-owned fields out of
baselines and reverse-sync destinations.

Exit: isolated fake-provider fault tests and a separately authorized real-rbw
acceptance exercise cover locked/unavailable vaults, executable/environment
changes, permissions, output confidentiality and current-state recovery.
Public local choices survive a provider-backed redeploy. If real integration
is blocked, retain partial status; mock success cannot close this milestone.
User-service reloads and other provisioning remain explicit external stages,
not arbitrary hooks hidden inside observation or apply.

## M9: Real-project migration qualification

Provisional scope; the M6 trial feedback and subsequent deliveries determine
the profile and coverage that will be qualified.

Qualify a selected `specter119/dotfiles` profile against its actual package
closure and machine exclusions, rather than the count of globally mapped files.
Account for every selected resource, generator/hook responsibility and each of
the pilot's 15 reverse-sync slots as replaced, deliberately retained, or
unsupported. Any new capability needed to remove a residual dependency gets
an explicit scope decision; M9 is not an unlimited feature backlog.

Exit: rehearse fresh deployment, existing-Dotter handoff, routine application
changes, source changes, conflict review, interruption and manual recovery in
an isolated environment using the installed artifact. Then perform a separately
authorized limited live canary and record its observation period and results.
Provide a handoff/runbook that prevents double management and explains how to
return responsibility to the previous manager without automatic rollback or
secret backups. Recheck M5 budgets and all adopted platform assumptions.

Linux is the first qualification target. WSL exclusions must be exercised;
native Windows support requires its own passing matrix and is not implied by
path tests. A scoped migration may close with explicit residuals, but a claim
that Dotter or the old reverse-sync script can be removed requires zero
remaining dependency on that component for the selected profile. Broad YAML
support, automatic rollback/replay and strong consistency against unrelated
writers remain outside this sequence unless separately scoped.

## Roadmap completion rule

A milestone closes only when its implementation, public contract, tests or verification evidence, and documentation agree. A capability described here is planned until its milestone closure record and current status document provide evidence that it exists.
