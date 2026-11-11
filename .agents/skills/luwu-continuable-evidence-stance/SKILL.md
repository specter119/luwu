---
name: luwu-continuable-evidence-stance
description: >-
  Use when reviewing Luwu mutation previews and results, outcome and reason
  codes, plan projections, undeclared write-back, provider failure states,
  interruption and recovery surfaces, and any change to what a later actor
  can learn from a run. Test whether a later person or agent can tell what
  happened, why, and what safe action continues, from honest machine-readable
  results and continuous provenance. Do not use for whether a value, key, or
  capability may be exposed in output, or whether Luwu may keep durable state
  of its own; those questions belong to the exposure and footprint stances.
disable-model-invocation: true
metadata:
  stance-axis: exposure-vs-continuity+footprint-vs-recovery
  opposes: luwu-minimal-exposure-stance, luwu-minimal-footprint-stance
---

# Luwu continuable-evidence stance

Every run is a message to a later actor who was not there: a person or an
agent that must decide the next safe action from what the run left behind.
This stance maximizes that message. It is one side of two oppositions —
user-visible surface against `luwu-minimal-exposure-stance`, and
reversibility of an interrupted run against
`luwu-minimal-footprint-stance` — so a pass that reports no conflict with
either opponent has not run this stance. It argues the evidence side in its
strongest form and does not average the two.

## Objective

A later person or agent, holding only honest machine-readable results and
the repository state, must be able to answer three questions without memory
of the run: what happened (which actions committed, which were refused,
which are uncertain), why (which boundary failed, and what distinguishes
this world from the neighboring worlds that would need a different next
step), and what safe action continues (which command, on which resource, is
safe to issue now). A result that forces the actor to guess — to re-observe
and hope, to trust a claim the run never recorded, or to read a maintainer's
prose to learn state no command can see — is a defect even when the run
itself was correct and safe. The interrupted run is inside this objective:
an actor continuing after a kill must be able to reconstruct what the dead
process did and classify every resource as committed, uncertain, or
untouched. Provenance is part of the same demand — the chain from source,
live, baseline, provider, and chosen action to resulting commit must
survive the process that wrote it (docs/product.md value test 5 and the
contract consequences on honest mutation outcomes and continuous
provenance).

A reviewer applies this by pointing at a diff and asking two questions:
which neighboring worlds did the change stop distinguishing, and which
observable did the change move from a result into a document. "None" on both
counts serves this stance; any other answer violates it.

## Accepts this harm

- `luwu-minimal-exposure-stance` loses the undeclared-value barrier: this
  stance demands the plan record which undeclared keys differ and in which
  direction — the undeclared-projection baseline digest that
  docs/design.md#rejected-alternatives itself names — so a derivative of
  undeclared document content would persist where
  docs/reference.md#baselines-adoption-and-explicit-mutation forbids it
  ("stores only declared accepted public scalar values").
- `luwu-minimal-exposure-stance` loses secret-material containment: a
  rotation-versus-edit discriminator for provider fields is a digest of
  secret bytes, rejected in docs/design.md#rejected-alternatives as
  "guessable offline or need[ing] a persisted key"; this stance still wants
  the question answered and will spend exposure surface to answer it.
- `luwu-minimal-exposure-stance` loses error minimalism: carrying provider
  failure detail (rbw's standard error, or a stale-socket discriminator)
  into results opens an output surface the fixed-code contract keeps closed
  (docs/reference.md#provider-owned-fields, "The child's standard error is
  discarded").
- `luwu-minimal-footprint-stance` loses the no-durable-state rule: a
  persisted reviewed-plan token and an execution journal are durable Luwu
  state beside the manifest, sources, inputs, and baselines, rejected in
  docs/design.md#rejected-alternatives ("a transaction engine, second
  ledger, durable execution journal, or automatic replay and rollback") and
  unclaimed in docs/status.md#verified-scope-and-limits.
- `luwu-minimal-footprint-stance` loses kill-and-forget recovery: sweeping
  `.luwu-tmp-*` residue means handling secret-bearing paths that no
  declared write currently binds, contradicting "a fresh plan is the
  recovery step" (docs/design.md#rejected-alternatives); the residue window
  is asserted, not repaired, in
  tests/interruption/test_provider_write_residue.py.

## Attacks

- Undeclared direction is operator folklore. The only source-root write,
  `reverse-sync --undeclared PATH`, runs on a plan that keeps no record
  of undeclared values, so nothing in the preview or result says whether
  the live side or the source side changed; a later actor auditing the run
  sees a `source_write` action and must trust the invocation's claim
  (docs/reference.md#writing-undeclared-changes-back-to-a-template-source,
  "The plan cannot tell whether the application or the source changed an
  undeclared value"; docs/status.md#daily-loop-changes).
- Rotation and hand edit produce the same result. A provider field that
  differs from the provider value is `source_changed` whether the provider
  rotated (forward is safe) or a person edited the live file (forward may
  destroy work); the safe continuing action differs and the result does not
  (docs/reference.md#provider-owned-fields, "a hand edit of the live file
  and a rotation at the provider look the same").
- An interrupted apply leaves nothing to continue from. There is no
  journal, durable plan, or replay
  (docs/reference.md#finite-batch-freshness-and-verification,
  "A fresh plan is required after an interruption or uncertain result"), so
  a SIGKILL mid-batch produces no result at all: the per-action committed
  and uncertain states existed only inside the dead process, and the next
  actor must re-observe the whole profile blind
  (tests/interruption/test_profile_partial_apply.py).
- Residue recovery is a prose sentence. A SIGKILL inside the publication
  window leaves a secret-bearing `.luwu-tmp-*` file inside the declared
  target directory; no result, code, or command names it and Luwu does not
  sweep it, so a later agent that has not read the reference paragraph
  leaves the secret on disk indefinitely
  (docs/reference.md#provider-owned-fields, Limits;
  docs/status.md#provider-fields-and-real-rbw-acceptance;
  tests/interruption/test_provider_write_residue.py).
- The reviewed plan is not the applied plan. The `apply` subparser accepts
  no plan artifact and re-plans on invocation (src/luwu/cli.py), the preview
  "is not a persisted plan token" (docs/reference.md#commands), and the
  plan evidence lives only in memory (src/luwu/plan_evidence.py), so across
  processes the operator's review is matched to nothing; a later actor
  cannot prove which reviewed bytes were written
  (docs/status.md#verified-scope-and-limits, consent tokens unclaimed).
- One code covers every provider failure cause. `provider_command_failed`
  is emitted for a stale socket, a broken agent, and any other nonzero
  `rbw unlocked` exit, and the child's standard error is discarded
  (docs/reference.md#provider-owned-fields); the maintainer needed a manual
  walk (experiments/rbw_acceptance.py) to learn which world a stale socket
  is, and the next actor facing the code cannot choose between "restart
  the agent" and "investigate the vault".
- Deferred mode repair is invisible pending work. While a provider resource
  is a reverse candidate, in conflict, unbased, awaiting adoption, or
  blocked by a provider failure, the mode-only repair is not taken and the
  plan shows only the effective mode with no field marking the repair as
  known-pending, so a later actor sees an unexplained wide mode on a secret
  file (docs/reference.md#provider-owned-fields;
  docs/status.md#provider-fields-and-real-rbw-acceptance).
- Exit code 2 flattens three worlds. A preview that wrote nothing
  (`confirmation_required`), a refused batch (`not_attempted`), and a
  half-written profile (`partial`) all exit 2
  (`return 0 if result.outcome in {"committed", "no_changes"} else 2` in
  src/luwu/cli.py; docs/reference.md#commands), so an actor keying on the
  exit status cannot tell "review this" from "your profile is half-applied"
  without parsing the result body.

## Evidence to demand

- The world list: for any change to results or errors, the enumeration of
  neighboring states the output must distinguish, and one test per world in
  the style of
  tests/apply/test_profile_commit_outcomes.py
  (`test_writer_errors_distinguish_not_replaced_and_committed`).
- The continuation procedure: from the emitted result alone, the next safe
  command for every outcome and reason code. If the answer needs a document
  paragraph rather than a result field, the change is incomplete.
- Interruption continuity: for each kill window (staging, publication,
  baseline write) a test that the next command's result reconstructs the
  world, as tests/interruption/test_provider_write_residue.py does for the
  staging kill that converges.
- Provenance carried forward: any field that decided an action appears in
  the result, so the next actor can re-derive the classification — the
  existing bar is action kinds, `reason`, `providers[].state`, and
  `replaces_live_on_apply` (docs/reference.md#finite-batch-freshness-and-verification).
- A doc sentence that describes recoverable state must name the command or
  result field that observes it; if none exists, the observable is demanded
  or the sacrifice is recorded in the stance registry.

## Does not own

- Whether any value, key name, digest, stderr byte, or capability may
  appear in an output at all: `luwu-minimal-exposure-stance` owns that, and
  `.agents/skills/luwu-confidentiality-review/SKILL.md` carries the detailed
  checks. This stance demands that a discriminator exist; it does not get
  to publish it.
- Whether Luwu may add any durable state of its own — journal, ledger,
  plan token, digest store, residue sweeper: `luwu-minimal-footprint-stance`
  owns that. This stance demands the continuity the artifact would give and
  records the loss when the artifact is refused.
- Whether a specific write was authorized and freshly verified at the
  write boundary: the proof side of that axis and
  `.agents/skills/luwu-consent-review/SKILL.md` own the check; this stance
  owns only whether the outcome was reported so the next actor can act.
- Who may write which value and whether undeclared content may be adopted:
  `.agents/skills/luwu-ownership-review/SKILL.md` owns the check; the
  authority-versus-completion axis is deferred in the stance registry.

## Escalates when

- A change makes two worlds that need different continuing actions produce
  the same outcome, reason code, field value, or exit status (the
  rotation/hand-edit collapse is the live example).
- A known reachable state is describable in prose but observable by no
  command's result (the `.luwu-tmp-*` residue shape).
- A kill window exists with no test showing that the next actor can
  reconstruct the world from results.
- A continuity gap can only be closed by a new persisted artifact or a new
  output surface: escalate with both opposing stances in one message; this
  stance never decides exposure or footprint questions alone.

## Stable core and changing contract

The stable value is: a later person or agent can tell what happened, why,
and what safe action continues, from honest results, with provenance that
survives the run that produced it. Today's mechanism is version 8's:
outcome kinds (`confirmation_required`, `not_attempted`, `partial`,
`committed`, `committed_but_baseline_not_advanced`, `no_changes`),
`verification_status`, per-action kinds and fixed `reason` codes,
`replaces_live_on_apply`, the `providers[].state` projection with
`not_fetched`, exit codes 0 and 2, and the in-memory plan evidence of
src/luwu/plan_evidence.py. These names, codes, and modules may change
without weakening the core: a future version may split
`provider_command_failed`, add a persisted reviewed-plan token, or record an
undeclared-projection digest — docs/design.md#rejected-alternatives records
those as mechanism refusals, not refusals of continuity. When a mechanism
changes, the question this stance asks stays fixed: after the change, what
can the next actor tell, and from what?

## Good patterns

- The adopt preview says applying will replace live bytes:
  `replaces_live_on_apply` in the plan projection and the adopt preview
  (docs/reference.md#baselines-adoption-and-explicit-mutation), asserted in
  tests/apply/test_daily_loop.py
  (`test_adopt_preview_says_when_apply_will_replace_the_live_bytes`).
- A failed baseline repair leaves the old baseline and says so:
  tests/apply/test_daily_loop.py
  (`test_a_failed_repair_leaves_the_old_baseline_and_says_so`,
  `test_a_failed_baseline_write_is_reported_without_undoing_the_target`),
  with `committed_but_baseline_not_advanced` naming the state in
  docs/reference.md#finite-batch-freshness-and-verification.
- Partial work is reported per action, not smoothed: parent creation and
  target writes that half-commit are carried as distinct actions
  (tests/interruption/test_profile_partial_apply.py,
  `test_partial_parent_creation_and_partial_target_write_are_reported`).
- Continuity without a journal: a converged resource with a stale baseline
  gets a baseline-only write on the next apply, so an interrupted apply
  repairs itself from fresh observation
  (docs/reference.md#baselines-adoption-and-explicit-mutation;
  tests/apply/test_daily_loop.py,
  `test_apply_repairs_the_baseline_of_a_converged_resource`).
- A provider-bearing plan says whether it fetched: `providers[].state`
  including `not_fetched` (docs/reference.md#provider-owned-fields), so a
  later actor can tell a plan that cannot be applied from one that can
  (tests/apply/test_provider_fields.py).
- Fixed reasons that name the next action: the `review_required` reason
  "inspect declared scope or correct it manually, then re-plan"
  (docs/reference.md#structured-formats-and-field-comparison) and the
  `stale_plan` message "plan inputs changed; build a new plan"
  (docs/reference.md#commands) point forward instead of only naming the
  boundary.

## Bad patterns

- Trusting the invocation instead of recording direction: the undeclared
  write-back keeps no record of which side changed, so `--undeclared PATH`
  rests on the operator's claim — the recorded sacrifice on this axis
  (docs/status.md#daily-loop-changes; docs/design.md#rejected-alternatives
  names the missing undeclared-projection digest).
- Documenting recovery that no command can perform: the `.luwu-tmp-*`
  residue is described as "delete it by hand" in
  docs/reference.md#provider-owned-fields, and
  docs/status.md#provider-fields-and-real-rbw-acceptance records the same
  limit as a file "that nothing sweeps", so recovery knowledge lives in
  prose rather than in a result or a check.
- Discarding the only cause evidence: provider standard error is dropped
  and stale socket, broken agent, and unknown failure collapse into
  `provider_command_failed` (docs/reference.md#provider-owned-fields;
  docs/status.md#provider-fields-and-real-rbw-acceptance).
- A review that dies with its process: the preview is not a persisted plan
  token and `apply` re-derives, so an operator's cross-process review is
  matched to nothing (docs/reference.md#commands;
  docs/status.md#verified-scope-and-limits).

The set and its oppositions are registered in `.agents/skills/luwu-stance-registry/SKILL.md`.
