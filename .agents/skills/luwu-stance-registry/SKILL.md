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
`.evidence/stance-discovery.md`. Every opposition below is backed by a sacrifice
the project has already recorded — that is what makes the axis real rather than
invented.

## Stances

| Stance | Axis | Objective (one line) | Accepts |
| --- | --- | --- | --- |
| `luwu-proof-at-write-stance` | proof-vs-cost | Nothing is written unless the authorizing bytes and identities were re-read and re-verified at the write boundary | Rechecks cost latency; no shared-layer cache may skip a proof |
| `luwu-bounded-cost-stance` | proof-vs-cost | A profile-scale run stays inside measured budgets, durability and verification counted in | Scoping a recheck to the current action's directories weakens whole-profile proof |
| `luwu-minimal-exposure-stance` | exposure-vs-continuity | Sensitive values and external capability stay out of every output, cache, and log; fail closed when uncertain | A later actor cannot see which undeclared keys differ, so the operator must assert it |
| `luwu-continuable-evidence-stance` | exposure-vs-continuity, footprint-vs-recovery | A later person or agent can tell what happened, why, and what safe action continues | Wants a persisted plan, journal, and digests, which cost exposure and durable footprint |
| `luwu-minimal-footprint-stance` | footprint-vs-recovery | No durable state of Luwu's own; a fresh plan is the recovery step | An interrupted run cannot be resumed or rolled back |

## Oppositions

Each row is a pair that must be reviewed together. Reviewing only one side of a
row is not a stance review.

| Pair | Contended resource | What each side demands | Recorded sacrifice on this axis |
| --- | --- | --- | --- |
| `luwu-proof-at-write-stance` vs `luwu-bounded-cost-stance` | Latency and throughput of one apply over a whole profile | Proof: re-read complete bytes, re-expand declared directories, re-render desired bytes at every action boundary. Cost: scope the recheck to the current action, cache a shared layer parse keyed on bytes, stop re-rendering once a directory exists | Cost won twice: per-action expansion is scoped, and after the first directory is created the rendered desired bytes of later actions are no longer compared |
| `luwu-minimal-exposure-stance` vs `luwu-continuable-evidence-stance` | User-visible surface and the correctness of the operator's next step | Exposure: metadata-only results, no cache, journal, digest, or provider diagnostic. Evidence: print which undeclared keys differ and in which direction, a matched persisted plan, a rotation-versus-edit digest | Exposure won: there is no undeclared-value record, so `--undeclared PATH` rests on the operator's claim. Evidence won where it costs no value: `replaces_live_on_apply`, skip reason codes, `committed_but_baseline_not_advanced` |
| `luwu-minimal-footprint-stance` vs `luwu-continuable-evidence-stance` | Reversibility and the durable state a failed run leaves behind | Footprint: no journal, ledger, durable plan, replay, or rollback; leave committed writes in place and report `partial`. Evidence: persist a journal and exact reviewed-plan token so an interrupted run resumes or rolls back, and sweep residue | Footprint won: no transaction engine or journal, a fresh plan is the recovery step, and no exact reviewed-plan consent token. A SIGKILL can still leave a private `.luwu-tmp-*` file that nothing sweeps |

`luwu-continuable-evidence-stance` sits in two rows. It is the one stance that
opposes two others, so a pass must review it against both.

## Deferred axes

Axes the stake map found and backed with recorded sacrifices, left unforged so
the set stays inside its working range. Both should be forged before the set is
treated as covering Luwu.

| Axis | Stakes | Why deferred | Forge when |
| --- | --- | --- | --- |
| authority-vs-completion | `S5` answerable authority vs `S6` operational completion | The set already carries five stances in three oppositions | The next pass touches `accept`, `reverse-sync`, batch adopt, or `public_names` |
| equivalence-vs-noise | `S8` proven equivalence vs `S9` quiet signal | Same | The next pass touches format parsing, drift classification, or a formatter |

## Unrepresented stakes

Decision themes that move a real resource but have no stance on at least one
side. Recorded so a pass does not mistake silence for agreement.

| Stake | Where it appears | Why no stance holds it |
| --- | --- | --- |
| Platform reach | Fallback walks, `O_TMPFILE`, Linux-first qualification | Reach is spent repeatedly and defended by no value test; `S7` is unopposed on this axis |
| Strong consistency against non-cooperating writers | Advisory locks only; parallel writers rejected | Neither side has a stake, so the limit is restated rather than argued |
| Machine adaptation | No platform detection; the problem statement starts from differing machines | The objective has no owner |
| The human-facing surface | "People and agents served equally"; a quiet, bookish voice | Every contract surface is a metadata-only JSON projection; no value test defends legibility |
| The cost of the guard apparatus | Eight review surfaces, ablations, ceilings, ownership table | No stake defends review cost, so a new guard is cheaper to add than to justify |
| One implementation per primitive | Shared-primitives rule and `OWNERS` ceilings | Genuinely unopposed; `luwu-primitive-reuse` checks it. A check, not a stance |
| Present-tense coherence | Documentation ownership table | Unopposed; `luwu-coherence-review` checks it |
| Honest labelling | Milestone completion rule | Its only opponent is delivery velocity, which appears as a preference, never as a protected objective |

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

| Conflict | Resolution | Serves | Evidence |
| --- | --- | --- | --- |
| Proof vs cost on shared input parsing | Parse a shared input layer once per distinct content, keyed on bytes | both — the proof still covers the bytes it was made on | `src/luwu/inputs.py`; `docs/design.md` |
| Exposure vs continuity on skip reasons | Skip reasons and outcome kinds are published; values, hashes, and diffs are not | both | `docs/reference.md` freshness and verification |

## Open essential conflicts

Conflicts awaiting a user decision, escalated one per message with both stances
stated in their strongest form, the buffer attempts already tried, the option
costs, and what becomes irreversible.

A conflict is essential only when both stances need the same resource, at least
two joint designs were tried and failed for concrete reasons, cost alone is not
the objection, and the choice is irreversible or contract-changing.

| Conflict | Pair | Status | Decision record |
| --- | --- | --- | --- |
| C-1 mode repair versus "skipped means untouched" | `luwu-minimal-exposure-stance` vs `luwu-proof-at-write-stance` | open — awaiting decision | `.evidence/stance-review/ADJUDICATION.md` |
| C-2 plan-time verdict versus boundary-time read | `luwu-proof-at-write-stance` vs `luwu-bounded-cost-stance` | buffer under measurement | `.evidence/stance-review/ADJUDICATION.md` |

C-1 has a disclosure-only buffer that costs neither side anything and should be
taken regardless of the decision. C-2 dissolves if one digest read per action
fits the performance gates; it is a measurement, not a judgement.

## Decisions that sacrificed a stance

The losing stance stays installed. Recording the sacrifice is how the next
reviewer sees what the current design costs.

| Decision | Sacrificed stance | What was given up | Why |
| --- | --- | --- | --- |
| Per-action desired-bytes re-render dropped once a directory exists | `luwu-proof-at-write-stance` | Whole-profile proof of rendered desired bytes for later actions | Loop cost; recorded as a limit rather than argued |
| No undeclared-value record; `--undeclared PATH` | `luwu-continuable-evidence-stance` | The operator-visible answer to "which undeclared keys differ, and which way" | Sensitive-value exposure |
| No journal, durable plan, replay, or rollback | `luwu-continuable-evidence-stance` | Resume or rollback after an interrupted run; a sweep for SIGKILL residue | Minimal footprint; a fresh plan is the recovery step |
| Manifest versions 1-7 retired | *(unrepresented)* | Compatibility for older manifests | Three engines with copied primitives let the copies drift |
| Droid workspace trust unsupported | *(unrepresented)* | Real Droid profiles using Luwu for workspace trust | No stable public file schema could be verified |

The last two rows have no losing stance because the winning side has no stance
either. They are listed so the gap is visible, not to claim a decision the set
made.

## Evidence

Only passes that changed something.

| Date | Pass | Conflict found | Effect |
| --- | --- | --- | --- |
| 2026-11-11 | Stake discovery from owner documents and review-skill scope clauses | 12 stakes, 5 backed oppositions, 6 ownerless themes | Axes established |
| 2026-11-11 | Set forged: 5 partisan stances, 3 oppositions | Two axes deferred; one stance in two oppositions | Set installed |
| 2026-11-11 | First real pass: 5 independent reviewers over `feature/m8-providers` | 33 findings — 7 buffers, 2 essential conflicts (C-1, C-2), 15 unargued limits, 5 stale records | C-1 escalated; C-2 under measurement; three counterexamples harvested; exposure stance flagged as thin |

## Counterexamples harvested

Patterns from the pass that generalize beyond Luwu. Full entries, including the
defender's argument for each, live in the forge's counterexample library
(`.agents/skills/hydra-stance-forge/references/counterexamples.md` in the
hydra-meta-skill repository). This table is the local index.

| Pattern | Caught by | Local status |
| --- | --- | --- |
| A document promises a compensating control the code does not implement | `luwu-minimal-exposure-stance` | open — C-1 |
| A time window stands in for an evidence check | `luwu-proof-at-write-stance` | open — C-2 |
| A limit is stated as a fact rather than an argument | all five | open — 15 instances |