---
name: luwu-proof-at-write-stance
description: >-
  Use when reviewing mutation entry, per-action freshness rechecks,
  publication callbacks, plan evidence, expected-state maps, provider value
  reuse, and the final whole-batch observation in Luwu. Test whether every
  published byte is authorized by complete bytes and identities re-read at
  the boundary that publishes it, and whether observation still writes
  nothing. Do not use for budgets, parse or expansion counts, recheck
  scoping for speed, or benchmark methodology.
disable-model-invocation: true
metadata:
  stance-axis: proof-vs-cost
  opposes: luwu-bounded-cost-stance
---

# Luwu proof-at-write stance

A write is an assertion about the filesystem at one instant, and the only
instant that can carry it is the one in which the bytes are published. Luwu's
own contract concedes that a plan is not that assertion: the batch is "a finite
rolling freshness boundary, not one simultaneous snapshot for every resource at
each write" (`docs/reference.md#finite-batch-freshness-and-verification`), and
there is no replay, journal, or rollback to repair a write made on a stale
belief (`docs/design.md#rejected-alternatives`). That leaves exactly one place
to be right. This stance maximizes what is re-read there and argues against
every mechanism that lets a write stand on something remembered, predicted,
scoped away, or waived by a clock.

## Objective

Every byte Luwu publishes is authorized by a read performed at the boundary
that publishes it, and observation publishes nothing.

For one action that means all of the following are re-derived inside the
publishing callback, from the filesystem, before the entry is replaced: the
complete bytes and presence of every dependency the action writes from (source,
every ordered input layer including absent optional layers, baseline, target);
the identities and modes of every path and existing ancestor involved; the full
directory expansion that produced the resource; the rendered desired bytes the
action intends to write; and the resource's current eligibility. After the
write, the committed entry is bound to the inode the writer returned, not to
equal content.

A diff serves this objective when it widens the set re-read at a boundary,
moves a read closer to publication, replaces a predicted state with an observed
one, or converts an inferred success into an observed identity. A diff violates
it when a write becomes reachable from a value read before confirmation, from
an `expected_*` prediction, from a memo, from a plan-time verdict, or from a
timer, counter, or retry threshold that decides whether a read happens at all.
Metadata is never enough: `docs/reference.md#finite-batch-freshness-and-verification`
already settles that "modification-time-only or other metadata-only freshness
checks are insufficient", and that sentence is the floor of this objective, not
its ceiling.

## Accepts this harm

Every item below is latency or throughput that `luwu-bounded-cost-stance`
cannot get back. This stance takes it knowingly.

- **`luwu-bounded-cost-stance` pays**: the saved whole-profile observation it
  currently banks at mutation entry. Deleting `_ENTRY_WINDOW_SECONDS` in
  `src/luwu/engine.py` adds one `_observe_all` plus two `verify_manifest` passes
  to every `--yes` run, and invalidates its own saving test,
  `tests/apply/test_batch_cost.py::test_a_plan_built_for_the_mutation_is_not_observed_a_second_time`,
  which asserts the difference is exactly one observation. The no-op apply
  budget of at most 2 seconds for 200 resources
  (`docs/maintenance.md#performance-acceptance`) absorbs that cost or the budget
  moves, not the check.
- **`luwu-bounded-cost-stance` pays**: linear directory cost. Re-expanding every
  declared directory at every action boundary, instead of only the directories
  behind the current action (`verify_manifest(..., resources=...)` in
  `_assert_fresh`), makes batch cost
  actions times directories and breaks
  `tests/apply/test_batch_cost.py::test_directory_expansion_grows_linearly_with_the_directory_count`
  and the "doubling 100 to 200 resources must take no more than 3x" rule in
  `docs/maintenance.md#performance-acceptance`.
- **`luwu-bounded-cost-stance` pays**: the rendered-bytes shortcut. Restoring the
  `desired_data` comparison that `_assert_fresh` currently waives for any
  resource with inputs once `_refresh_expected_ancestry` has seeded
  `expected_inputs` (`docs/status.md#daily-loop-changes`, third known limit)
  means a render, parse, and patch per remaining action per boundary. That
  lands on the one-change apply budget of at most 3 seconds, measurable as the
  `onechange` scenario medians of `experiments/profile_benchmark.py`.
- **`luwu-bounded-cost-stance` pays**: any apply latency bound it can state for
  provider resources. Re-fetching a provider at each action boundary, rather
  than once per command
  (`_resolve_provider_state` in `src/luwu/engine.py`,
  `docs/reference.md#provider-owned-fields`), adds an `rbw get` subprocess with
  a 5 second timeout per action, and that latency belongs to the vault and the
  agent, not to Luwu's measurements.
- **`luwu-bounded-cost-stance` pays**: the right to stop observing resources the
  batch will not write. Re-adjudicating every resource at every boundary,
  instead of carrying `skipped=skipped_names` through `_assert_forward_fresh`,
  makes a profile full of review resources pay a full observation per action
  for zero writes.
- **`luwu-bounded-cost-stance` pays**: the shared-layer memo as a tuning knob.
  `_LAYER_DOCUMENTS` in `src/luwu/inputs.py` survives only because its key is
  the exact bytes, format and public names and its lifetime is the manifest's.
  Rekeying it on a path, size, timestamp, or inode identity, or letting it
  outlive the manifest, is refused even where it measurably cuts the parse
  count.
- **`luwu-bounded-cost-stance` pays**: durability as a source of headroom.
  `docs/maintenance.md#performance-acceptance` counts "process startup,
  preflight, target durability, and verification" inside the budget; a `fsync`
  of child and parent before publication is not a cost this stance will trade,
  so the opponent must find its margin elsewhere.

## Attacks

1. **A clock authorizes the entry re-observation.** A mutation entry returns a
   plan-time record unchanged because it is young enough, instead of because
   the dependencies were re-read. The failure is a freshness decision whose
   input is elapsed time rather than evidence; detection by a later check does
   not restore the first write.
1. **A directory creation disables the last rendered-bytes check.** Creating a
   parent seeds predicted ancestry so broadly that later resources stop
   comparing the bytes they are about to publish. The failure is a check that
   silently becomes conditional on earlier batch history, including for
   resources Luwu never touched, while the plan still presents one proof rule.
1. **Provider bytes are published on a pre-confirmation read.** A provider value
   is fetched while the plan is built and then reused by freshness checks and
   publication. The failure is a write whose authorizing value has no read at
   its own boundary; a provider rotation during apply can publish the older
   value while the result describes the field as unchanged.
1. **Per-action re-expansion is narrower than the batch it authorizes.** A
   freshness check expands only the directories behind the current action, so
   an added leaf in another declared directory is deliberately invisible until
   that directory's action or the final observation. The failure is a write
   authorized by a scope smaller than the profile whose declared state it
   changes, with earlier writes left committed.
1. **A plan-time verdict outlives the boundary.** A resource is classified once
   as skipped or review-required and that verdict is carried through later
   publication checks. The failure is a stale classification that decides
   eligibility after the condition behind it has changed, without the operator
   seeing a new decision point.
1. **A prediction stands in for a read.** Expected filesystem state is advanced
   from a computed guess instead of from an identity returned by a successful
   Luwu action. The failure is a later boundary treating remembered state as
   observed state, especially after replacement, creation, or a path whose
   writer never returned an identity.
1. **Work creeps into the publication window.** Rendering, parsing, a
   subprocess, descriptor allocation, or another blocking call sits between the
   final dependency check and the atomic publication. The failure is an
   authorization window that only excludes cooperating writers while the
   staged bytes no longer provably correspond to the checked inputs.
1. **A boundary check reuses plan-time provider bytes.** The check re-reads
   files and identities but treats a provider value captured while planning as
   if it had been read at the publication boundary. The failure is a mixed
   proof whose strongest component is mistaken for the whole.

## Evidence to demand

- A fault-injection test that changes the dependency between the recheck and
  the publication, not only before the batch. The existing shape is
  `tests/apply/test_batch_cost.py` patching `luwu.engine.create_link` to touch a
  source mid-batch, and the `stale_inputs` paths in
  `tests/filesystem/test_observe_and_write.py`.
- An ablation, per `docs/maintenance.md#boundary-ablations`: disable the recheck
  in a private copy, run
  `uv run --script experiments/profile_ablation.py --python .venv/bin/python`
  and the suite, and show which test notices. A guard no test notices gets a
  fault-injection test before it is kept; a guard whose removal nothing
  observes is a hole, not a saving.
- Complete-byte comparison visible in the diff: `FileObservation.data` and
  presence compared, not only `identity`, `mode`, or a digest.
- For any narrowing: the named set passed to `check_inputs` printed against the
  dependency list of the staged bytes, showing the two are equal.
- For any removal justified by speed: the measured matrix from
  `experiments/profile_benchmark.py` with the guard on, every sample, median and
  maximum, on the same host and filesystem, per
  `docs/maintenance.md#performance-acceptance`. A relative speedup, a flame
  graph share, or a zero exit code is not acceptance.
- For a provider-bearing write: a stated answer to what a rotation between the
  plan build and the publication writes, backed by a test rather than by the
  contract sentence that fetching happens once.
- An entry in `docs/status.md` for every accepted narrowing, written as a cost
  decision with its argument, not filed as a limit with no owner.

## Does not own

- Budgets, their numbers, parse and expansion counts, which directories a
  per-action check expands, benchmark and flame methodology, and whether a
  profile-scale run is usable at all belong to `luwu-bounded-cost-stance`
  (`docs/maintenance.md#performance-acceptance`,
  `experiments/profile_benchmark.py`, `experiments/profile_flame.py`). This
  stance never sets a budget; it refuses to pay one with unread bytes.
- Whether evidence is persisted at all, a journal, a durable plan, a reviewed-plan
  consent token, or a sweep for residue belongs to
  `luwu-minimal-footprint-stance` and `luwu-continuable-evidence-stance`. This
  stance wants the read at the boundary, not a record of it.
- What may appear in output, logs, caches, or baselines belongs to
  `luwu-minimal-exposure-stance`.
- Detailed checks stay with the domain review skills: `luwu-boundary-review` for
  descriptors, symlinks, modes, atomicity and recovery; `luwu-consent-review`
  for whether an authorization existed at all; `luwu-ownership-review` for who
  may change a value; `luwu-semantic-drift-review` for what equality means;
  `luwu-verifiability-review` for how a claim is labelled.

## Escalates when

- A diff makes a write reachable from a value the process read before
  confirmation and never re-reads, and the argument offered is a measurement.
- A recheck is removed, narrowed, or made conditional, and no test or ablation
  fails when the guard is disabled.
- A threshold of any kind (elapsed time, a retry count, a batch position, a
  "first action" special case) decides whether a re-observation happens.
- The set re-read inside a publication callback is smaller than the set the
  staged bytes were derived from.
- An `expected_*` entry is seeded for a path no Luwu action returned an identity
  for.
- The last complete-byte read and the publication are separated by work that can
  block, or the parent lock is not held across both.
- A reported outcome treats an unverified or uncertain state as committed, or a
  final whole-batch observation stops covering resources the batch did not
  write.
- Restoring the proof is the only design that satisfies both sides and it
  measurably breaks a published budget. Then the conflict is essential: state
  both stances in full, name the buffer designs already tried, and take it to
  the user rather than settling it with a default.

## Stable core and changing contract

The stable value is: **authority to write is a fact about the bytes and
identities that exist at the instant of publication, and it must be established
there rather than inherited.** Uncertainty at that instant is reported, never
assumed away.

Today's mechanism is a specific stack, and all of it is replaceable: the
verify-observe-verify cycle and `_ENTRY_WINDOW_SECONDS` in `_require_plan`; the
detached plan evidence of `src/luwu/plan_evidence.py`; `_assert_fresh` and
`_assert_forward_fresh`; the `check_inputs` callback inside `write_file`,
`create_link`, and `create_parent`; `_matches_own_write`, `_matches_own_link`,
and `_verify_created_parent`; the per-manifest byte-keyed memo in
`src/luwu/inputs.py`; the final whole-batch `_assert_fresh`; and `O_TMPFILE`
plus `linkat` for provider-bearing bytes. A lock-held snapshot, a `renameat2`
exchange, a kernel change notification, or a different staging primitive may
replace any of them, and the names, flags, and limits above will move.

What may not move: the write is authorized by a read taken at its own boundary,
over complete bytes rather than metadata; a committed entry is bound to the
identity its writer returned; and a boundary that cannot be re-established
blocks instead of publishing.

## Good patterns

- Bind a commit to the writer's return, not to equal content. `_matches_own_write`
  in `src/luwu/engine.py` compares the returned inode, the exact bytes, their
  digest, the resolved mode and the ancestry, and the prediction is then
  replaced with a fresh `observe_file` result. `docs/design.md#rejected-alternatives`
  rejects "treating equal content as proof of a Luwu replacement".
- Recheck inside the writer, not only before it. `apply_plan` passes a
  `recheck` closure into `write_file` and `create_link`, and
  `src/luwu/filesystem.py` runs it under the parent lock and re-verifies root,
  ancestry and the named entry immediately afterwards. Evidence:
  `tests/apply/test_batch_cost.py::test_a_directory_changed_before_its_own_action_stops_that_action`.
- Re-observe what a syscall created before trusting it. `_verify_created_parent`
  re-reads the directory's identity and mode and refuses to read `mkdir`'s
  return as proof of creation provenance
  (`docs/reference.md#resources-and-declared-scope`).
- Key a memo on the thing being proven. `_LAYER_DOCUMENTS` in
  `src/luwu/inputs.py` is keyed on exact bytes, format and public names, bound
  to the manifest's lifetime, and never on a path or timestamp, so changed bytes
  always miss. This is the recorded buffer where both stances won; see
  `.agents/skills/luwu-stance-registry/SKILL.md`, "Resolved without escalation".
- Keep the contract sentence that does the work: "metadata-only freshness
  checks are insufficient" in
  `docs/reference.md#finite-batch-freshness-and-verification` is the lever a
  reviewer should quote whenever a cheaper comparison is proposed.

## Bad patterns

- A time window that decides whether the entry re-observation runs
  (`_ENTRY_WINDOW_SECONDS`). The clock knows nothing about the filesystem. This
  stance treats it as a recorded sacrifice, not as a settled question.
- A guard disabled by an unrelated condition: the `desired_data` comparison in
  `_assert_fresh` switched off for every resource with inputs because some other
  resource's parent was created (`_refresh_expected_ancestry`). Recorded in
  `docs/status.md#daily-loop-changes` as a limit, which is how an unargued
  weakening survives review.
- A value fetched once and reused by the check that authorizes publication
  (`_resolve_provider_state`), so the proof at the boundary covers everything
  about the write except the secret it writes.
- A plan-time classification carried into the write boundary
  (`skipped=skipped_names`), so eligibility is proven once and publication
  happens many times.
- Advancing an `expected_*` map from a computation rather than from a successful
  action's returned identity, which converts the freshness check into a
  self-consistency check.
- Comparing identity, size, mode, or mtime where the contract requires complete
  bytes, including the `digest`-only shortcut: a digest the process computed
  from bytes it did not re-read proves nothing about the file it is about to
  replace.

Patterns are review evidence, not a contract. Promote one only in the owning
document and link back here.

The set and its oppositions are registered in `.agents/skills/luwu-stance-registry/SKILL.md`.
