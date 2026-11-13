# C-1a — Disclose the live mode in the plan projection

Status: decided, merged, not yet implemented.
Decided: 2026-11-11, orchestrator, per the buffer rule in the installed
`hydra-crux` skill's `references/escalation.md` (state 1: a verified joint
design, no user decision needed).
Pair: `luwu-minimal-exposure-stance` vs `luwu-proof-at-write-stance`.

Scope note: a decision record owns *what was chosen, why, and which
alternatives were rejected*. It does not own the contract. The meaning of
`effective_mode`, of "skipped", and of any other published field stays in
[docs/reference.md](../reference.md); when a decision here is implemented, the
contract changes there and this record links to it. Writing the new contract
here would create a second owner.

## Conflict

A provider-bearing target whose other fields still need review sits at a group-
or world-readable mode. The plan reports `effective_mode` as the declared mode,
so the projection says the secret is at `0o0600` while the live file is at
`0o644`. Both owner documents claim the opposite: "it reports `effective_mode`
as the same mode string" ([reference](../reference.md#provider-owned-fields)) and
"the plan's effective mode shows it"
([status](../status.md#provider-fields-and-real-rbw-acceptance)).

## Resource

One fact the operator reads before deciding to apply: the mode the live file
actually has.

## Stance A says

`luwu-minimal-exposure-stance`: a secret must not stay readable while Luwu
reports a narrower mode. The report is the only surface the operator has before
authorizing the write, so a report that overstates containment spends the
stance's objective without buying anything.

## Stance B says

`luwu-proof-at-write-stance`: a resource reported as skipped must not be written.
A disclosure that adds no write preserves the "skipped means untouched"
guarantee exactly, so it costs this stance nothing.

## Buffer attempts

1. **Compute `effective_mode` from the observation instead of the declaration**,
   and add a `live_mode` field for the case where a declared mode hides the live
   one. Serves exposure fully. Costs proof nothing: no write, no new mutation
   path.
1. Split the answer across two fields but keep the current declaration-derived
   `effective_mode` for compatibility. Rejected: it would keep the misleading
   field as the primary answer, and the field's published meaning is the thing
   that is wrong.

## Option A — disclose only (chosen)

Serves: exposure fully. Costs: proof nothing. Reversible: yes, it is a
projection field.

## Option B — repair the mode whenever group or others have access

Moved to [C-1b](c-1b-mode-repair.md) because it is a different decision with a
real cost to proof.

## Adjudication

Option A is taken. It is the case `escalation.md` calls state 1: a buffer both
stances accept, needing no user decision. Recorded here so the next pass does
not re-contest it, and so the implementation has a tracked owner.

## What becomes irreversible

Nothing. Adding a projection field and correcting a field's meaning is
reversible; removing the field later would be a visible regression but not a
data-format change.

## Next action

Add `live_mode` to the resource projection, compute `effective_mode` from the
observation, update [reference](../reference.md#provider-owned-fields) to match,
and add a regression test on a provider resource with a wide live mode.
