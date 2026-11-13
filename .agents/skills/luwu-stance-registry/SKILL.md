---
name: luwu-stance-registry
description: >-
  Use to find which stance owns a question in Luwu, which stances oppose each
  other, and which conflicts are open or already decided. Read before assigning
  a stance review or recording a resolution. Do not use as a review stance
  itself.
disable-model-invocation: true
---

# Luwu stance registry

The stances below are first-principles objectives, not checklists. They are
deliberately opposed: each demands something another one pays for. A review that
produces no conflict has not been run properly.

A stance is not a domain review skill. A review skill answers "how do I check
this surface well" and can be satisfied. A stance answers "what is this project
optimizing for, and what will it lose" and can never be satisfied. Stances point
at the `luwu-*-review` skills for detailed checks; they do not restate them.

The stake map these stances were derived from is in
[`docs/stances.md`](../../../docs/stances.md). Every opposition below is backed
by a sacrifice the project has already recorded — that is what makes the axis
real rather than invented.

## Stances

| Stance                             | Axis                                          | Objective (one line)                                                                                              | Accepts                                                                                 |
| ---------------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `luwu-proof-at-write-stance`       | proof-vs-cost                                 | Nothing is written unless the authorizing bytes and identities were re-read and re-verified at the write boundary | Rechecks cost latency; no shared-layer cache may skip a proof                           |
| `luwu-bounded-cost-stance`         | proof-vs-cost                                 | A profile-scale run stays inside measured budgets, durability and verification counted in                         | Scoping a recheck to the current action's directories weakens whole-profile proof       |
| `luwu-minimal-exposure-stance`     | exposure-vs-continuity                        | Sensitive values and external capability stay out of every output, cache, and log; fail closed when uncertain     | A later actor cannot see which undeclared keys differ, so the operator must assert it   |
| `luwu-continuable-evidence-stance` | exposure-vs-continuity, footprint-vs-recovery | A later person or agent can tell what happened, why, and what safe action continues                               | Wants a persisted plan, journal, and digests, which cost exposure and durable footprint |
| `luwu-minimal-footprint-stance`    | footprint-vs-recovery                         | No durable state of Luwu's own; a fresh plan is the recovery step                                                 | An interrupted run cannot be resumed or rolled back                                     |

## Oppositions

Each row is a pair that must be reviewed together. Reviewing only one side of a
row is not a stance review.

| Pair                                                                  | Contended resource                                                   | What each side demands                                                                                                                                                                                                                               | Recorded sacrifice on this axis                                                                                                                                                                                                   |
| --------------------------------------------------------------------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `luwu-proof-at-write-stance` vs `luwu-bounded-cost-stance`            | Latency and throughput of one apply over a whole profile             | Proof: re-read complete bytes, re-expand declared directories, re-render desired bytes at every action boundary. Cost: scope the recheck to the current action, cache a shared layer parse keyed on bytes, stop re-rendering once a directory exists | Cost won twice: per-action expansion is scoped, and after the first directory is created the rendered desired bytes of later actions are no longer compared                                                                       |
| `luwu-minimal-exposure-stance` vs `luwu-continuable-evidence-stance`  | User-visible surface and the correctness of the operator's next step | Exposure: metadata-only results, no cache, journal, digest, or provider diagnostic. Evidence: print which undeclared keys differ and in which direction, a matched persisted plan, a rotation-versus-edit digest                                     | Exposure won: there is no undeclared-value record, so `--undeclared PATH` rests on the operator's claim. Evidence won where it costs no value: `replaces_live_on_apply`, skip reason codes, `committed_but_baseline_not_advanced` |
| `luwu-minimal-footprint-stance` vs `luwu-continuable-evidence-stance` | Reversibility and the durable state a failed run leaves behind       | Footprint: no journal, ledger, durable plan, replay, or rollback; leave committed writes in place and report `partial`. Evidence: persist a journal and exact reviewed-plan token so an interrupted run resumes or rolls back, and sweep residue     | Footprint won: no transaction engine or journal, a fresh plan is the recovery step, and no exact reviewed-plan consent token. A SIGKILL can still leave a private `.luwu-tmp-*` file that nothing sweeps                          |

`luwu-continuable-evidence-stance` sits in two rows. It is the one stance that
opposes two others, so a pass must review it against both.

## Latest survey

| Field          | Binding                                                                                                                                                                                  |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Target         | `0cdc498` (`feature/m8-providers`), diff `83f1dda..0cdc498`                                                                                                                              |
| Working tree   | Current checkout at `1509b30` plus staged and unstaged stance-set changes; code is unchanged from the target                                                                             |
| Scope          | All five stances and all three installed oppositions                                                                                                                                     |
| Reviewers      | One independent local subagent per stance                                                                                                                                                |
| Prior decision | C-1a is resolved: disclose the live mode and take `effective_mode` from observation; do not relitigate it                                                                                |
| Result         | 28 findings: 3 under C-2 measurement, 6 matching recorded sacrifices, 3 unresolved evidence or design questions, 5 independent defects, 10 accepted survey buffers, and 1 C-1b companion |
| State          | Survey recorded; no product-code fix adopted or verified                                                                                                                                 |

## Deferred axes

Axes the stake map found and backed with recorded sacrifices, left unforged so
the set stays inside its working range. Both should be forged before the set is
treated as covering Luwu.

| Axis                    | Stakes                                                   | Why deferred                                              | Forge when                                                                     |
| ----------------------- | -------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------ |
| authority-vs-completion | `S5` answerable authority vs `S6` operational completion | The set already carries five stances in three oppositions | The next pass touches `accept`, `reverse-sync`, batch adopt, or `public_names` |
| equivalence-vs-noise    | `S8` proven equivalence vs `S9` quiet signal             | Same                                                      | The next pass touches format parsing, drift classification, or a formatter     |

## Unrepresented stakes

Decision themes that move a real resource but have no stance on at least one
side. Recorded so a pass does not mistake silence for agreement.

| Stake                                              | Where it appears                                                            | Why no stance holds it                                                                                |
| -------------------------------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Platform reach                                     | Fallback walks, `O_TMPFILE`, Linux-first qualification                      | Reach is spent repeatedly and defended by no value test; `S7` is unopposed on this axis               |
| Strong consistency against non-cooperating writers | Advisory locks only; parallel writers rejected                              | Neither side has a stake, so the limit is restated rather than argued                                 |
| Machine adaptation                                 | No platform detection; the problem statement starts from differing machines | The objective has no owner                                                                            |
| The human-facing surface                           | "People and agents served equally"; a quiet, bookish voice                  | Every contract surface is a metadata-only JSON projection; no value test defends legibility           |
| The cost of the guard apparatus                    | Eight review surfaces, ablations, ceilings, ownership table                 | No stake defends review cost, so a new guard is cheaper to add than to justify                        |
| One implementation per primitive                   | Shared-primitives rule and `OWNERS` ceilings                                | Genuinely unopposed; `luwu-primitive-reuse` checks it. A check, not a stance                          |
| Present-tense coherence                            | Documentation ownership table                                               | Unopposed; `luwu-coherence-review` checks it                                                          |
| Honest labelling                                   | Milestone completion rule                                                   | Its only opponent is delivery velocity, which appears as a preference, never as a protected objective |

## Contract surfaces

A resolution that cannot name its file here is incomplete.

- `docs/product.md` — values, value tests, non-goals
- `docs/reference.md` — manifest, CLI, JSON, error, and write contracts
- `docs/design.md` — mechanisms, shared primitives, rejected alternatives, deliberate limits
- `docs/status.md` — current capability matrix and open limits
- `docs/roadmap.md` — delivery sequence and open exit conditions
- `docs/maintenance.md` — workflow, performance acceptance, duplicate-implementation rule
- `AGENTS.md` — agent working method and documentation ownership

## Resolved without escalation

Buffer-space merges both stances accept. Recorded so the next pass does not
relitigate them.

| Conflict                                                                           | Resolution                                                                                                                                        | Serves                                                           | Evidence                                                             |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------- |
| Proof vs cost on shared input parsing                                              | Parse a shared input layer once per distinct content, keyed on bytes                                                                              | both — the proof still covers the bytes it was made on           | `src/luwu/inputs.py`; `docs/design.md`                               |
| Exposure vs continuity on skip reasons                                             | Skip reasons and outcome kinds are published; values, hashes, and diffs are not                                                                   | both                                                             | `docs/reference.md` freshness and verification                       |
| **C-1a** — the plan must not report a narrow `effective_mode` for a wide live file | Disclose the live mode in the projection and take `effective_mode` from the observation. Adds no write, so "skipped means untouched" is untouched | both — exposure gets the missing fact, proof keeps the guarantee | [decision record](../../../docs/decisions/c-1a-disclosure-buffer.md) |

## Open essential conflicts

Conflicts awaiting a user decision, escalated one per message with both stances
stated in their strongest form, the buffer attempts already tried, the option
costs, and what becomes irreversible.

A conflict is essential only when both stances need the same resource, at least
two joint designs were tried and failed for concrete reasons, cost alone is not
the objection, and the choice is irreversible or contract-changing.

| Conflict                                          | Pair                                                           | Status                                      | Owner and next action                                         | Decision record                                         |
| ------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------- |
| C-1b mode repair versus "skipped means untouched" | `luwu-minimal-exposure-stance` vs `luwu-proof-at-write-stance` | open — awaiting decision                    | the user; no agent may pick this horn                         | [C-1b](../../../docs/decisions/c-1b-mode-repair.md)     |
| C-2 plan-time verdict versus boundary-time read   | `luwu-proof-at-write-stance` vs `luwu-bounded-cost-stance`     | measurement blocked — the harness is broken | repair `experiments/profile_benchmark.py`, then run attempt 1 | [C-2](../../../docs/decisions/c-2-plan-time-verdict.md) |

C-1a (disclose only) is merged and sits in the table above; it needs no
decision. C-1b is the remaining horn and does need one. C-2 dissolves if one
digest read per action fits the performance gates; it is a measurement, not a
judgement, and it cannot be taken until the harness runs.

## Survey findings

Pass 2 was a survey. The rows below record proposals and evidence; they do not
claim that code changed or that a conflict is resolved.

### Accepted survey buffers

Both sides of each row confirmed the concrete design during bounded contest.

| Surface                                             | Proposed buffer                                                                                                                                                 | Confirmed by                        |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| Repeated provider item reads                        | Group references by vault item within one resolution: one `get` and one parse, then select each field; preserve per-reference failures                          | proof and cost                      |
| Provider manifest signatures                        | Precompute the provider signature or compute the referenced subset, while retaining complete verification at command boundaries                                 | proof and cost                      |
| Provider provenance in mutation results             | Add allowlisted provider, item, field, state, and fixed error codes to preview/result; no values, hashes, diffs, stderr, argv, paths, or exception text         | exposure and continuity             |
| Provider batch latency                              | Apply one batch deadline; distinguish an in-flight `provider_timeout` from `not_attempted_batch_deadline`, and fail closed                                      | cost and continuity                 |
| Provider access before eligibility                  | Validate source, path, placeholder, and resource eligibility before provider resolution; blocked resources report `not_fetched` plus their fixed resource error | exposure and continuity             |
| Provider replacement without residue-free atomicity | Fail closed with `atomic_replacement_unavailable` instead of creating a named staging entry for provider-bearing replacement                                    | exposure, continuity, and footprint |
| Existing staging residue                            | Report a structured candidate with path, dev, ino, type, mode, and size, marked `provenance=unverified`; never auto-sweep or call a path-only cleanup safe      | continuity and footprint            |
| Provider stderr                                     | Drain stderr without retaining it for `get`; retain only a bounded rolling marker for `unlocked`                                                                | exposure and continuity             |
| Provider failure codes                              | Split fixed codes for agent missing versus untrusted, unlocked failure, get failure, and hardening failure, without exposing stderr                             | exposure and continuity             |

Mode repair visibility is a C-1b companion, not a resolved buffer: it makes
deferred work visible without deciding whether repair is a write.

### Open evidence and design

| Question                     | Current position                                                                                                                                                                                                               | Owner and end condition                                                                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Provider-bearing parse cache | Proof and exposure rejected a cache keyed by secret content. A joint design may cache only a non-secret parse skeleton, keep one sealed value, use an opaque token, and compare exact staged bytes after fresh boundary reads. | the next performance pass; demonstrate no secret or hash in the cache and a measured saving inside the gates                              |
| Provider lock race           | The target checks `unlocked` once and then invokes `get`; whether a lock between them starts unlock or pinentry is unconfirmed.                                                                                                | the maintainer with real `rbw`; add a race test or fail closed when the guarantee is unavailable                                          |
| Provider performance matrix  | No durable benchmark covers provider-bearing no-op, one-change, fresh apply, repeated item references, or timeout behavior.                                                                                                    | the maintainer who repairs the C-2 harness; add 100/200-resource scenarios and counts for `unlocked`, `get`, parse, and full-command time |

### Independent defects

These findings do not oppose another stance; they are concrete implementation or
tooling defects for a later fix pass.

| Finding                                                                                                                                  | Evidence                                                                      | Affected stance                |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------ |
| Structured parsing constructs path tuples even when no provider exemption exists                                                         | `src/luwu/formats.py` object, array, and patch walks                          | bounded cost                   |
| Exit code 2 collapses preview, refusal, partial, and uncertain mutation outcomes                                                         | `src/luwu/cli.py`; `docs/reference.md#commands`                               | continuable evidence           |
| A catchable interruption after named staging publication can leave secret-bearing residue, while the documented limit names only SIGKILL | `src/luwu/filesystem.py`; `tests/interruption/test_provider_write_residue.py` | minimal footprint and exposure |
| The real-rbw acceptance `workdir` follows a symlink and can `chmod` the undeclared target directory                                      | `experiments/rbw_acceptance.py`; `tests/tooling/test_rbw_acceptance.py`       | minimal footprint              |
| The acceptance `workdir` is created with the ambient mode and only then changed to `0700`; failure leaves a wider directory              | `experiments/rbw_acceptance.py`; `tests/tooling/test_rbw_acceptance.py`       | minimal footprint              |

### Prior decisions confirmed

The survey re-found six existing sacrifices and did not reopen them: whole
item reads for exact field matching, unmasked in-memory plan evidence, no
rotation-versus-edit digest, no journal, no persisted reviewed-plan token, and
cost-scoped per-action freshness. The
provider boundary, entry-window, and full-directory questions remain in C-2
because their joint design is still unmeasured.

## Decisions that sacrificed a stance

The losing stance stays installed. Recording the sacrifice is how the next
reviewer sees what the current design costs.

| Decision                                                           | Sacrificed stance                  | What was given up                                                            | Why                                                       |
| ------------------------------------------------------------------ | ---------------------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------- |
| Per-action desired-bytes re-render dropped once a directory exists | `luwu-proof-at-write-stance`       | Whole-profile proof of rendered desired bytes for later actions              | Loop cost; recorded as a limit rather than argued         |
| No undeclared-value record; `--undeclared PATH`                    | `luwu-continuable-evidence-stance` | The operator-visible answer to "which undeclared keys differ, and which way" | Sensitive-value exposure                                  |
| No journal, durable plan, replay, or rollback                      | `luwu-continuable-evidence-stance` | Resume or rollback after an interrupted run; a sweep for SIGKILL residue     | Minimal footprint; a fresh plan is the recovery step      |
| Manifest versions 1-7 retired                                      | *(unrepresented)*                  | Compatibility for older manifests                                            | Three engines with copied primitives let the copies drift |
| Droid workspace trust unsupported                                  | *(unrepresented)*                  | Real Droid profiles using Luwu for workspace trust                           | No stable public file schema could be verified            |

The last two rows have no losing stance because the winning side has no stance
either. They are listed so the gap is visible, not to claim a decision the set
made.

## Evidence

Only passes that changed something.

| Date       | Pass                                                                                                              | Conflict found                                                                                                                                       | Effect                                                                                                 |
| ---------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| 2026-11-11 | Stake discovery from owner documents and review-skill scope clauses                                               | 12 stakes, 5 backed oppositions, 6 ownerless themes                                                                                                  | Axes established                                                                                       |
| 2026-11-11 | Set forged: 5 partisan stances, 3 oppositions                                                                     | Two axes deferred; one stance in two oppositions                                                                                                     | Set installed                                                                                          |
| 2026-11-11 | First real pass: 5 independent reviewers over `feature/m8-providers`                                              | 33 findings — 7 buffers, 2 essential conflicts (C-1, C-2), 15 unargued limits, 5 stale records                                                       | C-1 escalated; C-2 under measurement; three counterexamples harvested; exposure stance flagged as thin |
| 2026-11-13 | Second pass: 5 independent local reviewers over `0cdc498`, with bounded contests and O4 de-seeded before briefing | 28 findings — 10 accepted survey buffers, 3 open evidence/design questions, 5 independent defects, 6 prior sacrifices, 3 C-2 cases, 1 C-1b companion | Survey recorded; no product-code fix adopted or verified                                               |

## Counterexamples harvested

Patterns from the pass that generalize beyond Luwu. Full entries, including the
defender's argument for each, live in the counterexample library of the
`hydra-crux` skill (`.agents/skills/hydra-crux/references/counterexamples.md` in
the hydra-crux repository). This table is the local index. The rows added on
2026-11-13 are local survey candidates; their full external-library entries are
pending a separate hydra-crux update.

| Pattern                                                                | Caught by                                                       | Local status                |
| ---------------------------------------------------------------------- | --------------------------------------------------------------- | --------------------------- |
| A document promises a compensating control the code does not implement | `luwu-minimal-exposure-stance`                                  | open — C-1                  |
| A time window stands in for an evidence check                          | `luwu-proof-at-write-stance`                                    | open — C-2                  |
| A limit is stated as a fact rather than an argument                    | all five                                                        | open — 15 instances         |
| A per-call timeout is presented as a command budget                    | `luwu-bounded-cost-stance`                                      | survey — accepted buffer    |
| A resource-scoped check hides a full-profile computation               | `luwu-bounded-cost-stance`                                      | survey — accepted buffer    |
| A mutation result omits provenance its preview already carries         | `luwu-continuable-evidence-stance`                              | survey — accepted buffer    |
| A named staging residue is treated as if only SIGKILL can leave it     | `luwu-minimal-footprint-stance`; `luwu-minimal-exposure-stance` | survey — independent defect |
| A cache key contains the secret it is meant to deduplicate             | `luwu-minimal-exposure-stance`                                  | survey — open design        |
