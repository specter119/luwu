# C-1b — Does a mode-only repair count as a write?

Status: open, awaiting a user decision.
Opened: 2026-11-11. Owner: whoever takes the next stance pass.
Pair: `luwu-minimal-exposure-stance` vs `luwu-proof-at-write-stance`.

Scope note: a decision record owns *what was chosen, why, and which
alternatives were rejected*. It does not own the contract. The meaning of
`effective_mode`, of "skipped", and of any other published field stays in
[docs/reference.md](../reference.md); when a decision here is implemented, the
contract changes there and this record links to it. Writing the new contract
here would create a second owner.

## Conflict

A provider-bearing target sits at `0o644` with a plaintext secret while another
field of the resource still needs review. The resource is reported skipped, so
no write happens, and the wide mode survives until those fields settle
([status](../status.md#provider-fields-and-real-rbw-acceptance) records this as a
known limit). Today the mode-only repair is gated on `action == "noop"`
(`src/luwu/engine.py`, the `provider_aliases and action == "noop"` condition).

## Resource

The meaning of "skipped": whether it promises *nothing was written* or only
*nothing the declaration asked for was written*.

## Stance A says

`luwu-minimal-exposure-stance`: a secret must not stay readable at a wide mode
just because an unrelated field is under review. The repair writes the same bytes
(`write_data = target.data`) and only narrows the mode, so the exposure is real
and the cost is small.

## Stance B says

`luwu-proof-at-write-stance`: a resource the plan reports as skipped must not be
written at all. Repairing the mode makes the plan's own status untrue, and the
operator's authorization was for "leave it alone". Once a skipped resource may be
written, every audit of "what did this apply touch" needs the plan *and* the
observation.

## Buffer attempts

- **Attempt 1 (disclosure only)** — taken, see [C-1a](c-1a-disclosure-buffer.md).
  It makes the exposure visible but leaves it standing. This is why C-1b is
  still open: disclosure does not satisfy exposure.
- **Attempt 2 (pending-repair disclosure)** — proposed by the second stance
  survey. When repair is deferred, a fixed result reason names the wide mode as
  pending repair. Exposure and continuity accept the status, but it does not
  narrow the mode or decide whether repair is a write. This remains a companion
  to C-1b, not a resolution.
- **Attempt 3 (repair whenever group or others have access)** — serves exposure
  fully, costs proof the "skipped means untouched" guarantee.
- **Attempt 4 (repair only when the resource is otherwise clean)** — today's
  behaviour. Serves proof, leaves the exposure.

Options 3 and 4 are the two horns; there is no joint design that both stances
accept, which is what makes this state 2 rather than state 1.

## Option A — keep today's gate (repair only when clean)

Serves: proof. Costs: a secret can stay world-readable across every non-noop
status (reverse candidate, conflict, unbased, awaiting adoption, provider
failure). Reversible: yes, by taking B. Contract impact: none, the current
"skipped" meaning is unchanged.

## Option B — repair whenever group or others have access

Serves: exposure. Costs: "skipped means untouched" loses its second clause, and
the plan status alone no longer tells the operator what the run touched.
Reversible: no, once tools or agents read the changed meaning. Contract impact:
[reference](../reference.md#provider-owned-fields) must state
that a skipped resource may still receive a mode-only write.

## Option C — refuse to skip a resource whose live mode is wider than declared

Serves: both, at the cost of throughput: the batch stops instead of proceeding.
Reversible: yes. Not yet tried; recorded because it is the one design that does
not need the "skipped" meaning to change.

## Decision needed

Does a mode-only repair count as a write for the purpose of "skipped means
untouched"? Yes (B) or no (A/C)?

## What becomes irreversible

The published meaning of "skipped" and of `effective_mode`, which other tools
and agents may already read. No option locks a data format.
