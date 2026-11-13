---
name: luwu-bounded-cost-stance
description: >-
  Use when reviewing profile-wide apply, directory expansion, shared-input parsing, and performance evidence in Luwu. Test whether a profile-scale run stays inside measured budgets with durability and verification counted in, without actions times directories growth. Do not use for exact-byte and identity proof at each write boundary.
disable-model-invocation: true
metadata:
  stance-axis: proof-vs-cost
  opposes: luwu-proof-at-write-stance
---

# Luwu bounded-cost stance

Whole-profile latency and throughput are the contested resource. Spend work
on the action being executed, not on repeatedly proving unrelated resources.
This stance maximizes S2 from `.evidence/stance-discovery.md#stakes` and takes
the cost side of its proof-vs-cost opposition.

## Objective

Keep the complete profile command within the measured time and growth gates
in [performance acceptance](../../../docs/maintenance.md#performance-acceptance),
including startup, preflight, durable publication, baseline advancement, and
final verification. Reject a diff that makes each action expand every declared
directory or reparse identical shared input content per consumer. Reduce
repeated work in the delivered command, not merely in a timed fragment.

## Accepts this harm

- **`luwu-proof-at-write-stance` pays**: detection of an unrelated directory
  change before the current write. Prefer `verify_manifest(resources=...)`
  in `src/luwu/manifest.py`; accept discovering that change at its own action
  or the final observation, after earlier writes committed.
  `tests/apply/test_batch_cost.py` explicitly tests this delayed detection.
- **`luwu-proof-at-write-stance` pays**: a second whole-profile observation at
  mutation entry for a newly built, immediately consumed mutation plan.
  Defend `build_mutation_plan` reuse in `src/luwu/cli.py` and the saved
  observation tested by
  `test_a_plan_built_for_the_mutation_is_not_observed_a_second_time`.
- **`luwu-proof-at-write-stance` pays**: an independent rendered-desired-byte
  comparison for later actions under the recorded post-parent-creation
  concession in [daily-loop limits](../../../docs/status.md#daily-loop-changes).
  Claim that concession for cost, not as an apology. It is recorded evidence,
  not proof that today's `_assert_fresh` skips the comparison: the inspected
  `src/luwu/engine.py` compares `old.desired_data` with `current.desired_data`
  without a parent-created exemption.

## Attacks

1. An action callback drops its resource filter and expands unrelated source
   trees at every publication. Demand evidence from the directory-growth test
   in `tests/apply/test_batch_cost.py`.
1. Each binding or freshness pass reparses the same complete shared layer,
   multiplying parse cost by consumers and checks. Demand a count that shows
   the exact-content memo still covers every consumer.
1. CLI apply builds an observation and immediately pays for another complete
   observation of the same entry state. Demand the mutation-plan reuse test
   above rather than an assumed saving.
1. A new proof pass revisits every previously created parent for every later
   action. Demand counts on nested fresh deployment, not a flat no-op timing.
1. An apparent speedup times target replacement alone, leaving parent syncs,
   baseline writes, or final observation outside the clock. Evidence:
   [performance acceptance](../../../docs/maintenance.md#performance-acceptance)
   and [the execution flow](../../../docs/design.md#end-to-end-flow).
1. Flat public fixtures pass while directory-heavy and shared-layer profiles
   accumulate repeated work, or failed and blocked runs disappear from the
   samples. Demand measurements from the realistic profile, the benchmark, and
   [performance acceptance](../../../docs/maintenance.md#performance-acceptance).
1. A per-call timeout is presented as a command budget. The failure is a
   command whose worst-case delay grows with the number of external calls even
   though every individual call is bounded.
1. A resource-scoped check hides a full-profile computation. The failure is a
   check described as paying only for the current action while it still hashes,
   parses, or walks every unrelated declaration.

## Evidence to demand

- Before/after runs of `experiments/profile_benchmark.py` through the real
  command and installed artifact, on the same durable filesystem and host.
  Apply every scenario, sample, maximum, and scaling gate from
  [maintenance](../../../docs/maintenance.md#performance-acceptance);
  do not substitute a relative speedup for passing the budgets.
- Operation counts from `experiments/profile_realistic.py`, plus targeted
  counts for any newly repeated work. Vary actions and directory declarations
  independently; include nested missing parents and shared input consumers.
  Identify which count stopped multiplying and where its time went.
- The existing `tests/apply/test_batch_cost.py` checks for scoped expansion
  and entry-observation reuse. Timings establish cost, these tests establish
  the intended work shape; neither replaces the other.
- Check postconditions and interpreter provenance, not just exit status.
  [Status](../../../docs/status.md#verified-scope-and-limits) limits what
  synthetic evidence establishes; [migration qualification](../../../docs/roadmap.md#m9-real-project-migration-qualification)
  requires budgets to be rechecked on the selected real profile.

## Does not own

Route exact bytes, identity continuity, and how close each proof must be to
publication to `luwu-proof-at-write-stance`. Its demands for more frequent
re-reading do not answer this stance's cost objection. Detailed filesystem
and authorization checks belong to `luwu-boundary-review` and
`luwu-consent-review`; do not reproduce their checklists here.

## Escalates when

- Increasing directory declarations increases expansions performed by an
  unrelated action, or adding later actions repeatedly revisits the growing
  set of completed parents.
- A measured scenario fails its owned budget or growth gate, or only passes
  after durability, verification, blocked resources, or failed samples vanish.
- A proposed recheck adds profile-wide work inside an action loop without
  before/after whole-command evidence.
- A recorded cost concession is presented as current behavior even though
  its execution path still performs the supposedly removed work.

## Stable core and changing contract

The stable value is bounded whole-profile cost, with the complete operation
paid for. Today's mechanisms are resource-scoped directory expansion,
exact-byte/format/public-name layer memoization for one manifest lifetime,
short-lived mutation-entry reuse, and finite rolling verification. Modules,
reuse windows, flags, and budget values may change; none defines the value.
Replace mechanisms to remove repeated work, not to move it outside measurement.

The [product](../../../docs/product.md#product-non-goals) does not freeze
the implementation language or module shape.
[Design](../../../docs/design.md#module-responsibilities) owns mechanisms,
[reference](../../../docs/reference.md#finite-batch-freshness-and-verification)
owns the rolling freshness contract, and
[AGENTS.md](../../../AGENTS.md#documentation-ownership) assigns each fact
its owner. This skill argues a review objective, not a new public promise.

## Good patterns

- Keep unrelated directory expansion at batch boundaries. The scoped
  `verify_manifest` implementation and
  `test_limited_verification_ignores_directories_it_was_not_asked_about` in
  `tests/apply/test_batch_cost.py` embody the recorded cost victory in
  `.evidence/stance-discovery.md#oppositions`. Preserve that locality instead
  of charging every action for every tree.
- Parse identical shared-layer content once. `src/luwu/inputs.py::_parse_layer`
  reuses the document by bytes, format, and public names; keep consumer count
  from becoming parse count.
- Treat the post-parent-creation desired-byte concession recorded in
  [status](../../../docs/status.md#daily-loop-changes) as a cost-side
  precedent. Demand an explicit measured decision about that comparison,
  rather than silently treating an extra rendering proof as free.

## Bad patterns

- Call unfiltered `verify_manifest` inside every action to obtain stronger
  whole-profile proof. The counterexample is the scoped implementation and
  growth assertion in `tests/apply/test_batch_cost.py`; the repair is to pass
  the current resources, not raise the timing ceiling.
- Discard the shared-layer memo and parse once per binding per recheck.
  `src/luwu/inputs.py` documents exactly this multiplication; restore
  content-keyed reuse instead of shrinking the workload.
- Claim a budget win from tmpfs, a warmed in-process fragment, or disabled
  syncs. [Performance acceptance](../../../docs/maintenance.md#performance-acceptance)
  requires durable, whole-command measurement; rerun that measurement rather
  than redefining what an apply costs.

The set and its oppositions are registered in `.agents/skills/luwu-stance-registry/SKILL.md`.
