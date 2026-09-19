# M3 Frozen-Contract Repair Plan

Status: closed. The repaired v3/v4/v5 implementation and final gate were
completed from the historical `83ca72e` worktree.

This record captures the boundary decisions that closed the M3 repair. It is
not a second public contract. Current behavior is defined in
[reference](../reference.md); current status is defined in
[status](../status.md).

## Gaps that blocked closure

The initial implementation had three evidence gaps:

- literal-JSON reverse sync could re-encode more source bytes than the selected
  fields justified;
- a replace call that changed the target before raising could be misclassified
  from equal bytes rather than staged identity;
- the journal record accepted values outside the executioner's closed
  condition domain.

## Accepted repair decisions

The repair retained three small guards instead of introducing general-purpose
abstractions:

1. Selective reverse sync scans top-level literal-JSON member spans and patches
   only selected values plus required local separators. Unsupported or
   ambiguous spans are rejected before writing. Preview metadata contains
   operation names and separator changes, not values or diffs.
1. The baseline, source, and target writers re-observe no-follow staged
   identity after a replace-boundary failure. The result distinguishes
   `not_replaced`, `replaced`, and `indeterminate`; equal content alone never
   proves a Luwu replacement.
1. Plan records share one closed validation path across create, decode, read,
   and inspect. Resources are non-empty and contiguous, condition types are
   closed, and numeric values reject booleans and out-of-range values.

The exact public states, error projections, and compatibility promises belong
to [reference](../reference.md).

## Historical implementation slices

| Slice                    | Write set                                                               | Acceptance focus                                                                                |
| ------------------------ | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| A: literal-JSON patch    | `src/luwu/reverse_sync.py`, selective-patch tests                       | Selected values change; unselected and undeclared bytes remain byte-for-byte stable             |
| B: replace boundary      | `src/luwu/baseline.py`, `src/luwu/mutations.py`, replace-boundary tests | Replace-before/after failures, staged identity, equal external content, and cleanup uncertainty |
| C: execution propagation | `src/luwu/reconcile.py`, execution tests                                | Three-state propagation, journal outcomes, recovery, and later `not-attempted` resources        |
| D: record closure        | `src/luwu/plan_record.py`, schema tests                                 | Closed condition domain, non-empty records, contiguous ordinals, and consistent read paths      |

The main integration pass owned the ablation, owning-document updates, and
cross-slice verification. These historical write sets do not define permanent
agent or subagent scope.

## Ablation and verification

The guard-on/guard-off ablation reproduced a real counterexample for each
accepted guard. Production regressions then covered the complete writer
timelines, CLI and journal projections, read-only recovery, old fixtures, and
v1-v4 compatibility. The final closure recorded 274 passing unit tests plus
the M3 ablations, static checks, build checks, isolated CLI loop, and isolated
hook gate.

The repair intentionally did not add a transaction engine, second ledger,
generic JSON editor, schema registry, automatic recovery, rollback, or a
strong-consistency promise against unrelated writers.
