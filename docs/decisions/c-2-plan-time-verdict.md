# C-2 — Does a plan-time verdict substitute for a boundary-time read?

Status: open. Buffer under measurement; blocked on a broken instrument.
Opened: 2026-11-11. Owner: whoever repairs the performance harness.
Pair: `luwu-proof-at-write-stance` vs `luwu-bounded-cost-stance`.

Scope note: a decision record owns *what was chosen, why, and which alternatives
were rejected*. It does not own the contract. Freshness semantics stay in
[docs/reference.md](../reference.md#finite-batch-freshness-and-verification);
when this is decided, the contract changes there and this record links to it.

## Conflict

`apply_plan` computes `skipped_names` once and excludes those resources from
every later freshness check. `_require_plan` returns the plan-time record
unchanged when it is within `_ENTRY_WINDOW_SECONDS` (0.25 s), so elapsed time,
not evidence, decides whether the entry re-observation runs. The CLI path always
lands inside that window, which is why the contract says a `--yes` command
"observes the profile once"
([reference](../reference.md#baselines-adoption-and-explicit-mutation)).

## Resource

Whether a decision made before the write boundary may authorize a write after it.

## Stance A says

`luwu-proof-at-write-stance`: every published byte must be authorized by bytes
and identities re-read at the boundary that publishes it. A clock is not
evidence, and a plan-time verdict is a prediction about a file the process has
not looked at since.

## Stance B says

`luwu-bounded-cost-stance`: re-reading the whole profile at entry costs one
`_observe_all` plus two `verify_manifest` passes on every `--yes` run, which is
exactly the saving the no-op apply budget is built on. The stance will pay for
evidence, but it requires the cost to be measured against
[the acceptance gates](../maintenance.md#performance-acceptance) rather than
asserted.

## Buffer attempts

**Attempt 1 — one digest read per action.** Re-read a digest of each dependency
at the action boundary instead of the whole file. If it fits the gates, both
stances get what they need and the conflict dissolves; this is a measurement,
not a judgement. This is the designed buffer.

**Pass 2 additions.** The second stance survey found three instances of the
same question: provider values are resolved at plan time and reused at
publication; the elapsed-time entry window can skip the first whole-profile
re-observation; and each action expands only its own resources. Proof objected
to all three. Cost objected to unconditional whole-profile re-observation and
to `actions x directories` expansion, and accepted a measured exact-content
memo only if boundary identities are still re-read. These cases join attempt 1
rather than creating three separate decisions.

## Blocker

The measurement cannot run. `experiments/profile_benchmark.py` fails on its
first sample with `schema-8 plan resource fields mismatch`, because its
`_PLAN_RESOURCE_FIELDS` does not match the current plan projection (it lacks
`providers` and still lists `parent_actions`). The harness is not in CI, not in
`prek`, and no test references it, so the drift went unnoticed. Until it is
repaired there is no instrument for attempt 1, and the C-2 status stays
"measurement impossible" rather than "measurement pending".

The repaired instrument must also include provider-bearing scenarios. The
survey found no durable provider case in the public or realistic benchmark. The
measurement matrix is: 100 and 200 resources; no-op, one-change, and fresh
apply; multiple fields from one item; provider timeout; counts for `unlocked`,
`get`, parse, manifest signature, and full-command time; and median/max over at
least five samples. A fixed-delay fake provider measures Luwu cost, while the
real-rbw walk remains the correctness check. If nobody supplies this matrix,
C-2 keeps the current design by default and remains open.

## Option A — keep the entry window

Serves: cost. Costs: a plan-time verdict authorizes boundary-time writes, and a
change inside the window reaches publication without a re-read. Reversible: yes.

## Option B — remove the window, re-observe at entry

Serves: proof. Costs: one whole-profile observation per `--yes` run; the no-op
apply budget absorbs it or the budget moves. Reversible: yes.

## Option C — attempt 1 (one digest read per action)

Serves: both, if it fits the gates. Reversible: yes.

## Decision needed

None yet. This is a measurement. Escalate only if attempt 1 fails the gates, and
then the question is A versus B.

## What becomes irreversible

Nothing at this stage. The eventual choice changes how a `--yes` run argues for
its writes, not any stored format.
