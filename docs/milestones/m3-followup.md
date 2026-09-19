# M3 Follow-up: Current Worktree Review and Repairs

Date: 2026-09-13. Status: implementation and verification complete.

This document is a historical review and repair record. The current contract
is owned by [reference](../reference.md), and the current implementation
snapshot is owned by [status](../status.md). The follow-up rechecked the
[M3 frozen scope](m3.md) instead of carrying forward an earlier completion
claim.

## Trigger

The initial worktree had 190 passing unit tests and the original M3 ablation
scenarios, but the review found three untested boundaries:

- creating a fixed journal lock could create a declared target before
  execution;
- reverse sync could write after the baseline used for classification had
  changed;
- recovery could report `confirmed` while a fresh plan reported drift.

M4 remained future work during this repair.

## Frozen repair plan

The repair kept the existing narrow architecture:

1. Check the journal and its lock together against manifest, source, target,
   aliases, and ancestor relationships before either persistent file is
   created.
1. Bind the planner to the baseline bytes actually used for classification.
   Recheck that private authorization evidence around the source write and
   preserve committed or unknown outcomes after a post-write change.
1. Add real temporary-directory regressions and metadata-only CLI assertions
   for the counterexamples, including zero-write and redaction behavior.
1. Require committed, unchanged, and unknown recovery results to agree with a
   fresh plan. A standalone `not-attempted` resource cannot produce an overall
   confirmation.

The public meaning of the resulting states is maintained in
[reference](../reference.md), not in this historical plan.

## Review and ablation

Independent logic, confidentiality, consent, boundary, ownership, semantic,
and verifiability reviews reproduced the lock-path and stale-baseline
failures. A later review reproduced the recovery aggregation failure. The
minimal ablation probes removed each guard in turn and reproduced the
corresponding bad outcome. The probes used the real writer path, temporary
directories, and no user configuration.

The implementation deliberately did not add a registry, transaction engine,
persistent snapshot, or generic recovery layer. Existing writer checks and
private in-memory evidence were sufficient.

## Historical slice boundaries

| Slice                      | Write set                                                             | Acceptance                                                                               |
| -------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Journal path boundary      | `plan_record.py` and execution tests                                  | Conflict is rejected before persistent writes; normal CAS behavior remains valid         |
| Reverse-sync input binding | `mutations.py`, selected `reconcile.py` planner paths, mutation tests | Baseline/manifest changes block authorization; post-write changes retain honest outcomes |
| Recovery aggregation       | `reconcile.py` recovery paths and recovery tests                      | Content drift is not misreported as confirmed; recovery remains read-only                |
| Integration                | Journal preflight, documentation, ablation, and CLI integration tests | Cross-slice boundaries and full repository gates are checked                             |

These write sets were for one historical implementation pass. They are not a
permanent subagent or agent-scheduling policy.

## Completion evidence

The repaired worktree added 23 tests. The follow-up suite, the M3 ablations,
static checks, build checks, isolated hooks, and the temporary M1 CLI loop
passed at closure. Exact commands, test counts, artifact paths, and hook
environment details are retained here as historical evidence rather than
copied into [status](../status.md).
