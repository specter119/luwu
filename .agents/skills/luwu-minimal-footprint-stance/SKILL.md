---
name: luwu-minimal-footprint-stance
description: >-
  Use when reviewing staging entries, cleanup paths, locks, persisted state,
  interruption and failure paths, and recovery claims in Luwu. Test whether a
  run leaves no durable state of Luwu's own, damages no undeclared content,
  permission, or link, and treats a fresh plan as the recovery step. Do not use
  for whether a later person or agent can resume or roll back from a persisted
  journal and an exact reviewed plan.
disable-model-invocation: true
metadata:
  stance-axis: footprint-vs-recovery
  opposes: luwu-continuable-evidence-stance
---

# Luwu minimal footprint stance

Luwu acts on a filesystem it does not own. Everything it leaves behind is a
liability someone else has to live with: an entry nothing will sweep, a mode
nobody asked for, a record that is authoritative until it is wrong. This stance
maximizes one objective: after Luwu runs, the system holds the declared result
and nothing of Luwu's own. Recovery is not a stored history to replay; it is a
fresh observation of what is actually there. `AGENTS.md` states the boundary as
"Preserve existing targets and recoverable boundaries"; this stance reads that
as a mandate to keep Luwu's own residue at zero, because residue is the part of
a failure the operator cannot see.

## Objective

Every operation satisfies both halves, and a diff can be checked against each:

1. **Zero durable state of Luwu's own.** The set of paths that exist after the
   process exits, successfully or not, is a subset of the declared
   destinations: the target, its declared parents, the baseline, a declared
   input destination, and for `reverse-sync --undeclared` the template source.
   No journal, ledger, durable plan, consent token, lock file, cache, marker,
   backup, or recovery record is created anywhere, including under
   `XDG_STATE_HOME`, `XDG_CACHE_HOME`, a runtime directory, or beside the
   manifest. `docs/reference.md#commands` holds the current shape of this:
   six commands, and "There is no provider, journal, cache, or recovery
   command."
1. **Nothing is damaged or removed without identity proof.** Undeclared keys,
   existing file and directory modes, symlinks, and link counts survive every
   path through the code, including the failure paths. A removal is authorized
   only by a no-follow inode identity Luwu itself established, never by a name,
   a glob, a directory listing, or equal content.

A diff serves this objective when it adds no path that outlives the process
and no deletion decided by anything but identity. A diff violates it when a
new file persists past the run, when a cleanup walks or matches names, when a
failure path restores, removes, or re-permissions something to tidy up, or
when "so the next run can continue" is the reason a byte is written.

## Accepts this harm

This stance is bought with recovery. Each cost below is accepted knowingly:

- **`luwu-continuable-evidence-stance` pays**: resume entirely. An interrupted
  apply cannot be continued; the operator rebuilds a plan from current state.
  `docs/reference.md#finite-batch-freshness-and-verification`: "The batch also
  has no replay, durable plan, or execution journal. A fresh plan is required
  after an interruption or uncertain result."
- **`luwu-continuable-evidence-stance` pays**: rollback. Committed target writes
  stay committed when a later resource or a baseline write fails; the outcome
  is `partial` or `committed_but_baseline_not_advanced` and the old baseline is
  left in place. Rolling back would mean keeping a copy of live bytes Luwu does
  not own. Evidence:
  `tests/apply/test_daily_loop.py::test_a_failed_baseline_write_is_reported_without_undoing_the_target`,
  `tests/interruption/test_profile_partial_apply.py::test_partial_parent_creation_and_partial_target_write_are_reported`.
- **`luwu-continuable-evidence-stance` pays**: the exact reviewed-plan token. A
  preview a person reviewed cannot be matched byte for byte against what
  `--yes` writes, because nothing persists it: "The preview is not a persisted
  plan token; `--yes` recalculates the current inputs"
  (`docs/reference.md#commands`), and exact reviewed-plan consent tokens are
  listed as not claimed (`docs/status.md#verified-scope-and-limits`).
- **`luwu-continuable-evidence-stance` pays**: the residue sweep. A SIGKILL in the
  window between naming the staged inode and renaming it over the target leaves
  one private `.luwu-tmp-*` entry, and nothing cleans it
  (`docs/status.md#provider-fields-and-real-rbw-acceptance`;
  `tests/interruption/test_provider_write_residue.py::test_sigkill_at_publication_leaves_one_private_staging_entry`).
  A sweeper is refused rather than built: it would delete entries whose
  provenance no live process can establish.
- **`luwu-continuable-evidence-stance` pays**: a record of what Luwu created. A
  committed parent creation returns an observed identity that "does not prove
  exclusive creation provenance"
  (`docs/reference.md#resources-and-declared-scope`), and no list of created
  directories survives the run, so a half-finished batch cannot be reconstructed
  from Luwu's own state.
- **`luwu-continuable-evidence-stance` pays**: baseline history. A schema-2
  baseline holds only the accepted values, or the accepted digest and mode, for
  the current declaration; a changed binding identity is reported `unbased` with
  `baseline_stale` and "the old values are never reused"
  (`docs/reference.md#baselines-adoption-and-explicit-mutation`). There is no
  previous generation to return to.
- **`luwu-continuable-evidence-stance` pays**: the run itself on filesystems
  without the footprint-free primitive. A provider-bearing write refuses with
  `anonymous_staging_unsupported` instead of falling back to a named staging
  file (`src/luwu/filesystem.py`,
  `tests/filesystem/test_anonymous_write.py::test_anonymous_write_without_tmpfile_support_refuses_closed`),
  so there is no partial result to continue from and no residue either.

## Attacks

Concrete shapes in this repository that this stance attacks on sight:

- **A state directory appears.** Any write whose destination is not a declared
  target, parent, baseline, input destination, or (for `--undeclared`) template
  source: an action log "for support", a resumable batch record, a provider
  cache, or a new command to read one. Already rejected as a transaction
  engine, second ledger, durable execution journal, and provider cache in
  `docs/design.md#rejected-alternatives`; the cache "never took part in a
  decision and carried a second copy of the atomic write".
- **A named fallback for unnamed staging.** The anonymous staging path refuses
  closed when its footprint-free primitive is unavailable. A change that falls
  through to a named temporary file, or stages in a cache or runtime directory and
  renames across filesystems, trades a refusal for durable residue and a
  non-atomic publication (`docs/design.md#rejected-alternatives`, the named
  staging file entry).
- **Cleanup by name.** A sweep of staging entries at startup, at batch end, or on
  signal, in place of identity-gated cleanup, which stats no-follow, compares
  `(st_dev, st_ino)`, checks the type and link count, and returns
  a retained-foreign-entry result instead of unlinking. Observable shape: a
  glob, a directory walk, or an mtime threshold deciding a deletion.
- **Rollback by backup.** A failure path that restores the target's prior
  bytes, which requires copying them somewhere first: a new baseline payload
  field holding previous content, a sidecar `.bak`, or a tmpfile kept after
  publication. The copy baseline "never stores the copied file bytes"
  (`docs/reference.md#baselines-adoption-and-explicit-mutation`) and
  `luwu/baseline.py` keeps closed payload variants precisely so this cannot
  grow in quietly (`docs/design.md#module-responsibilities`).
- **Undo that mutates the system.** Removing a parent Luwu created, changing a
  mode back to a previously observed value, or deleting a baseline to
  "reset" a resource. Existing directory modes are never changed, a created
  parent "remains committed and its result is uncertain"
  (`docs/reference.md#resources-and-declared-scope`), and a failed repair
  leaves the old baseline (`docs/status.md#daily-loop-changes`). An undo here
  destroys state whose other users are unknown.
- **The plan projection learns to persist.** The plan projection exists only in
  memory, and plan evidence keeps
  "no mutable plan references or persistent identity cache"
  (`docs/design.md#module-responsibilities`). Serializing it, hashing it to a
  file, or accepting a plan identifier on the command line turns authorization
  into stored state that can be replayed against a system that has moved.
- **Observation that writes.** Planning creating a missing baseline, touching a
  marker so the next run knows where it stopped, or taking a lock through a
  lock file. Today the directory lock uses an already-open directory
  descriptor, so no lock path exists to leak or go stale; a lock file would be
  a persistent entry whose path relationship to a declared target has to be
  proven first (`AGENTS.md`, keep observation and mutation separate).
- **Finishing the job through a boundary.** Replacing a target symlink,
  accepting a target whose link count is not one, or adopting undeclared
  content so a write can complete. A target symlink is refused rather than
  followed or replaced (`docs/reference.md#commands`), and hardlink and reflink
  deployment were rejected because an application write would silently change
  the source (`docs/design.md#rejected-alternatives`).
- **Cleanup that covers only SIGKILL.** A named staging entry is treated as
  residue-free because the documented failure is process death, while a
  catchable interruption after publication can leave the same secret-bearing
  entry. The failure is a cleanup guarantee narrower than the set of exits that
  can reach the window.

## Evidence to demand

- For any new persistent path: the root it lives under, the identity check that
  gates its creation, and a failure-path test asserting it is absent after the
  run. Model it on
  `tests/filesystem/test_anonymous_write.py::test_anonymous_replace_leaves_no_extra_entry`
  and `::test_sigkill_during_anonymous_write_leaves_no_entry`, which compare the
  parent directory's entry set, not just the target.
- For any new removal or permission change: the inode comparison that
  authorizes it plus a negative test that a foreign entry at the same name is
  retained, as `_cleanup_stage_entry` returns `foreign_entry_retained`.
- For any recovery claim: proof that re-observation already covers it before a
  stored-state mechanism is considered. Apply's baseline-only write repairs a
  converged resource after an interrupted apply or a reverse sync
  (`docs/design.md#module-responsibilities`,
  `tests/apply/test_daily_loop.py::test_apply_repairs_the_baseline_of_a_converged_resource`).
- For an interruption path: a test that kills or fails the process at the
  specific window and then inspects the tree, as
  `tests/interruption/test_provider_write_residue.py` does for publication
  failure, post-replace verification failure, and SIGKILL.
- For every residue a change can still leave: an open-limit line in
  `docs/status.md`, stated as a limit. An unrecorded residue is worse than a
  recorded one.
- For a mode or parent change: exact declared modes, umask independence, and
  preservation of existing modes, as covered by
  `tests/filesystem/test_profile_parent_and_mode.py`.
  `.agents/skills/luwu-boundary-review/SKILL.md` holds the detailed per-syscall
  path and TOCTOU checklist; this stance does not restate it.

## Does not own

This stance does not own whether a later actor can continue. Route to
`luwu-continuable-evidence-stance`: resume and rollback procedures, what a
result must report, reason codes and outcome kinds, continuous provenance from
source through commit, and the question of whether a reviewed plan can be
proven identical to what was written. If the answer to a finding is "a later
person would need this recorded", it is that stance's finding, not this one's.
Questions about what a persisted byte would reveal belong to
`luwu-minimal-exposure-stance`; authorization and confirmation belong to the
consent surface.

## Escalates when

Escalate the tradeoff rather than deciding it when any of these is observable:

- A stated requirement cannot be met without a path that outlives the process,
  for example continuing a batch whose earlier writes already committed.
- The only available way to clear residue a kill can leave is to delete an
  entry whose creator cannot be identified from live state.
- Correctness depends on restoring content Luwu does not own, which requires
  keeping a copy of it somewhere.
- The platform lacks the primitive that makes a footprint-free write possible
  and the proposal answers with a fallback that names the staged bytes.
- Completing a declared action requires modifying an entry outside the declared
  destinations: an existing mode, a symlink, a hard-linked file.
- Two writers must coordinate across process lifetimes, which an advisory lock
  on a held descriptor cannot express.
- A reviewer finds a residue or a durable artifact that no owner document
  records as a limit.

Escalation follows `AGENTS.md#adversarial-stance-review`: state both stances at
full strength, name the joint designs already tried, and stop for a user
decision when the choice changes a contract irreversibly.

## Stable core and changing contract

The stable value is: **Luwu leaves the system holding the declared result and
nothing of its own, damages nothing it did not create, and recovers by
observing reality again.**

Today's mechanism is specific and replaceable: unnamed `O_TMPFILE` staging
published through `/proc/self/fd` with `linkat` and an immediate rename for
provider-bearing writes; a `.luwu-tmp-*` entry beside the target for every
other staged write, created with `O_EXCL` and removed only on inode identity;
`flock` on a held directory descriptor instead of a lock file; one-directory-
at-a-time parent creation whose committed result is reported as uncertain on
failure; schema-2 baselines with closed payload variants; a finite rolling
freshness boundary with `partial` and `committed_but_baseline_not_advanced` and
no rollback; and six commands with no journal, cache, or recovery command
(`docs/design.md#module-responsibilities`,
`docs/reference.md#finite-batch-freshness-and-verification`,
`docs/reference.md#commands`).

Any of those may change: `renameat2`, a different staging primitive, another
locking scheme, a new baseline schema, a different outcome vocabulary. What may
not change without an explicit user decision and a recorded sacrifice is the
core: no durable state of Luwu's own, no removal or relaxation without identity
proof, and a fresh plan as the recovery step.

## Good patterns

- Identity-gated cleanup: `_cleanup_stage_entry` in `src/luwu/filesystem.py`
  stats the name with no-follow, compares `(st_dev, st_ino)` and the link
  count, and returns `foreign_entry_retained` rather than unlinking something
  it cannot prove it made.
- Staging nothing names: `_create_anonymous_stage` probes `O_TMPFILE` and
  `/proc/self/fd` and raises `anonymous_staging_unsupported` before a byte is
  written, so an interrupted write leaves no entry at all. Evidence:
  `tests/filesystem/test_anonymous_write.py::test_proc_probe_failure_refuses_before_writing`.
- Self-repair through re-observation instead of a journal: a converged
  template or copy with a stale baseline gets a baseline-only write on the next
  apply, so an interrupted apply or a reverse sync repairs itself
  (`docs/design.md#module-responsibilities`,
  `docs/reference.md#baselines-adoption-and-explicit-mutation`).
- Failure reported without undoing: a failed baseline write keeps the committed
  target and says so, instead of reversing it. Evidence:
  `tests/apply/test_daily_loop.py::test_a_failed_baseline_write_is_reported_without_undoing_the_target`.
- Authorization that dies with the process: plan evidence and provider
  resolution live only in memory and are never serialized, and a shared input
  layer parse is keyed on exact bytes so "nothing outlives the operation"
  (`docs/design.md#module-responsibilities`, `src/luwu/inputs.py`).
- The recorded sacrifice on this axis, kept as precedent: no transaction
  engine, ledger, durable execution journal, or automatic replay and rollback,
  because "A fresh plan is the recovery step"
  (`docs/design.md#rejected-alternatives`).

## Bad patterns

- A named staging file in a cache or runtime directory, proposed to keep
  SIGKILL residue out of the target tree: the cross-filesystem rename is not
  atomic, and the unnamed file gives the same absence of residue without that
  cost (`docs/design.md#rejected-alternatives`).
- Hardlink or reflink deployment, or accepting a target whose link count is not
  one: an application write would silently change the source file
  (`docs/design.md#rejected-alternatives`,
  `docs/reference.md#resources-and-declared-scope`).
- A provider cache or registry on disk: the earlier one never took part in a
  decision and carried a second copy of the atomic write
  (`docs/design.md#rejected-alternatives`).
- Inferring provenance instead of proving identity: treating equal content as
  proof that Luwu performed the replacement
  (`docs/design.md#rejected-alternatives`). A name-pattern sweeper is the same
  defect pointed at deletion.
- Writing every undeclared difference with one flag, or copying a live file
  wholesale into a template source: the operator names each leaf, and
  everything a re-render cannot reproduce is refused
  (`docs/reference.md#writing-undeclared-changes-back-to-a-template-source`,
  `docs/design.md#rejected-alternatives`).

The set and its oppositions are registered in `.agents/skills/luwu-stance-registry/SKILL.md`.
