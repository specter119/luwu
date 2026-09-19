# M3 Execution and Conflict Closure

Date: 2026-09-13 to 2026-09-14. Status: implementation, independent review,
and final verification complete.

This is a historical closure record for the execution and conflict repairs.
The current public behavior is owned by [reference](../reference.md), and the
current implementation snapshot is owned by [status](../status.md).

## Review findings

The execution review identified three failures that the earlier 213-test
baseline did not cover:

1. Target replacement facts could be confused with journal publication facts.
1. A journal containing only `not-attempted` resources could be summarized as
   confirmed.
1. Reverse sync could proceed with a selected live candidate while another
   unselected field required conflict review.

M4 remained a later roadmap item. The repair did not expand M3 into provider,
secret, automatic recovery, or unrelated-writer consistency work.

## Accepted repair shape

The implementation added a metadata-only in-memory execution context to
preserve `plan_id`, known committed targets, and ordered resource states even
when journal diagnostics fail. The journal remains the record of persisted
facts; in-memory execution evidence is not presented as a durable record.

Recovery now requires every resource to be currently confirmed. Unknown and
not-attempted resources continue to require recovery. Reverse sync rejects a
resource-level conflict or unsupported ownership direction before building a
patch, while explicit baseline acceptance remains a separate action.

The stable state names, error meanings, and public JSON shape are maintained in
[reference](../reference.md). The descriptions here explain why the repair
was made, not how callers should interpret a current response.

## Historical implementation slices

| Slice                      | Write set                                                     | Acceptance focus                                                                  |
| -------------------------- | ------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Execution facts            | `errors.py`, selected `reconcile.py` helpers, execution tests | Preserve target outcomes across initial, per-resource, and final journal failures |
| Authorization and recovery | reverse-sync guards, recovery aggregation, focused tests      | Block unselected conflicts and do not confirm standalone not-attempted resources  |
| CLI failure projection     | `cli.py`, CLI tests                                           | Expose metadata-only cumulative outcomes without values or exception payloads     |
| Integration                | Plan, owning documents, ablations, and repository gates       | Check all boundaries together without adding a second ledger or recovery engine   |

The historical packages were integrated by a main agent. Their write sets are
not a permanent scope system.

## Review and ablation evidence

Independent logic, consent, ownership, semantic, confidentiality, boundary,
and verifiability reviews found production counterexamples. Four focused
ablation probes reproduced the false-success outcomes when the relevant guard
was removed. Production fault injection then covered journal phases, partial
success, replace-before/after failures, cleanup uncertainty, CLI redaction,
old records, and read-only recovery.

The repair retained the existing journal schema and state model. It rejected a
second durable ledger, a general transaction engine, a policy registry, and
automatic replay or rollback.

## Closure evidence

The historical closure added 30 tests. The full 243-test suite at that
checkpoint, three M3 ablations, static checks, package builds, an isolated M1
CLI loop, and the complete isolated hook gate passed. The detailed commands,
temporary artifact paths, and environment notes remain in this record so that
the current status page does not become a duplicate log.
