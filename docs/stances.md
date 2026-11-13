# Luwu stance map

Status: this is the discovery gate for the stance set. Every stance in
`.agents/skills/*-stance/` must trace to an opposition row here; a pair that
cannot be entered here is not an opposition. It was written before the stance
files, from the owner documents, and it is kept as the record of *why* each axis
is real. The set itself, its open conflicts, and its decisions are owned by
[the stance registry](../.agents/skills/luwu-stance-registry/SKILL.md).

This map is not a review, not a finding list, and not a skill. Evidence is cited
as `path#section`; no test counts or run output are recorded here.
What this is: a map of the first-principles objectives Luwu is optimizing for,
and which of them actually fight each other. It is not a review, not a
finding list, and not a skill. Evidence is cited as `path#section`; no test
counts or run output are recorded here.

How the required checks appear below: for every stake, check (1) "a concrete
change this stake demands" is the *demands* cell of its opposition row (or the
bullet under `## Unopposed stakes`), check (2) "the stake it makes worse and
the resource it costs" is the paired stake plus the *contended resource*
column, and check (3) "a change the opponent demands which this stake resists"
is the opposing *demands* cell. A stake with no answer to (2) is in
`## Unopposed stakes` and is deliberately left unpaired.

Choices made where the request left them open:

- `S6` merges "the daily loop must complete" (M7a) and "the old manager must
  become removable" (M9) into one stake. Split, they push the same direction on
  every capability decision found here, which would manufacture a fake
  opposition.
- One stance per opposition, named for the axis rather than for one side, so a
  stance cannot be reduced to the surface its winning side defends. Five
  oppositions survived the checks, so five stances are recommended.
- The eight existing `.agents/skills/*/SKILL.md` files were used as the
  project's own map of surfaces, not as evidence for a stake. Every stake below
  has at least one non-skill owner document, test, or contract clause; `S11` is
  the only one where a skill supplies the sharpest wording, and its value is
  independently owned by `AGENTS.md`.
- Platform reach, strong consistency, machine adaptation, the human-facing
  surface, guard maintenance cost, and delivery velocity are reported as
  missing stakes rather than paired with a plausible-sounding opponent.

## Stakes

| id    | Objective (what the project would sacrifice other things to keep)                                                                                         | Evidence (path + section)                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `S1`  | Nothing is written unless the exact bytes and identities that authorize it were re-read and re-verified at the write boundary; observation never writes.  | `AGENTS.md#cross-cutting-handling-boundaries`; `docs/product.md#value-tests` (Q4); `docs/design.md#end-to-end-flow` (verify-observe-verify); `docs/reference.md#finite-batch-freshness-and-verification` ("metadata-only freshness checks are insufficient")                                                                                                                                                                                                  |
| `S2`  | A profile-scale run stays inside measured budgets with durability and verification counted in, and cost must not grow as actions x directories.           | `docs/maintenance.md#performance-acceptance` (budget table, 3x growth rule, "cannot be omitted to improve timing"); `docs/design.md` (per-action expansion scoped so batch cost is not "actions times directories"; `inputs.py` parses a shared layer once per distinct content)                                                                                                                                                                              |
| `S3`  | Sensitive values and external capability stay out of repositories, persistence, caches, logs and machine-readable output, and fail closed when uncertain. | `AGENTS.md` ("Minimize secrets and fail closed", "Make external capabilities explicit"); `docs/product.md#product-non-goals`; `docs/reference.md#provider-owned-fields`; `docs/reference.md#finite-batch-freshness-and-verification` (results never expose values, bytes, diffs, hashes); `docs/design.md#rejected-alternatives` (no provider cache, registry, or diagnostic command; no secret digest)                                                       |
| `S4`  | A later person or agent can tell what happened, why, and what safe action continues, from honest machine-readable results and continuous provenance.      | `docs/product.md#value-tests` (Q5) and "Contract consequences" (honest mutation outcomes, continuous provenance); `docs/reference.md#finite-batch-freshness-and-verification` (action kinds, `reason`, `replaces_live_on_apply`, `committed_but_baseline_not_advanced`)                                                                                                                                                                                       |
| `S5`  | Who may change a value stays declared and answerable; undeclared or conflicting content is never adopted by a default, force, or precedence rule.         | `docs/product.md#product-non-goals`; `AGENTS.md` ("Keep scope and ownership explicit", "reverse sync as structured, auditable acceptance"); `docs/reference.md#baselines-adoption-and-explicit-mutation`; `docs/reference.md#writing-undeclared-changes-back-to-a-template-source`; `docs/design.md#rejected-alternatives` (one-flag undeclared write; writing back to a provider)                                                                            |
| `S6`  | The routine loop must finish on a real profile without hand-edited files or deleted baselines, and the previous manager must become genuinely removable.  | `docs/roadmap.md#m7a-daily-loop-usability` ("the daily loop breaks outside them"); `docs/status.md#daily-loop-changes` (baseline advancement, per-resource apply, `undeclared = "preserve"`, `public_names`, adopt preview); `docs/roadmap.md#m9-real-project-migration-qualification` ("zero remaining dependency"); `docs/reference.md#commands` (batch adopt, partial apply)                                                                               |
| `S7`  | Act without damaging undeclared content, permissions or links, and without adding durable state of Luwu's own: a fresh plan is the recovery step.         | `AGENTS.md` ("Preserve existing targets and recoverable boundaries"); `docs/design.md#rejected-alternatives` (no transaction engine, ledger, journal, replay or rollback; no hardlink/reflink; no named staging file); `docs/reference.md#finite-batch-freshness-and-verification` ("no replay, durable plan, or execution journal"; no rollback); `docs/reference.md#commands` (a target symlink is refused, existing modes preserved)                       |
| `S8`  | Two representations are equal only on format or parser evidence, and unsupported syntax is preserved or refused rather than normalized away.              | `docs/product.md#a-boundary-for-future-implementation` and "equivalence is evidence-bounded"; `docs/reference.md#structured-formats-and-field-comparison`; `docs/reference.md#writing-undeclared-changes-back-to-a-template-source` (the prove-every-condition list); `docs/design.md#rejected-alternatives` (no generic JSON editor, no schema registry, no TOML AST framework)                                                                              |
| `S9`  | Only consequential differences reach the operator; incidental text, formatting, and converged state must not present themselves as drift.                 | `docs/product.md#what-luwu-is-becoming` (semantic drift, "quiet") and `#value-tests` (Q2); `docs/product.md#a-boundary-for-future-implementation` (a formatter is valuable only when it reduces false drift); `docs/design.md#observation-and-semantic-comparison` and the converged-template baseline-only write                                                                                                                                             |
| `S10` | One implementation per primitive, enforced by a test whose ceilings may only go down.                                                                     | `docs/design.md#shared-primitives` ("a second copy ... is a defect"); `tests/tooling/test_primitive_ownership.py` (`OWNERS` ceilings, "lower OWNERS"); `docs/maintenance.md#preventing-duplicate-implementations`; `docs/design.md#rejected-alternatives` (retired manifest versions: three engines let the copies drift)                                                                                                                                     |
| `S11` | A capability is understandable from its current form in one place, without replaying the order in which it was delivered.                                 | `AGENTS.md#documentation-ownership` (one owner per fact type; do not repair a conflict in a non-owner) and `#scope-and-language-rules` (run output is not repository content); `docs/maintenance.md#test-layout`, `#run-output`; sharpest wording in `.agents/skills/luwu-coherence-review/SKILL.md`                                                                                                                                                          |
| `S12` | Every claim is labelled as planned, implemented, historical, or verified-by-what, and no completion statement is carried forward unrevalidated.           | `AGENTS.md#role-and-authority` ("planned behavior must never be presented as current behavior"); `docs/roadmap.md#roadmap-completion-rule`; `docs/status.md` header and "Do not carry a completion statement forward"; `docs/roadmap.md#m8-providers-and-mixed-inputs-on-version-8` ("Mock success alone could not have closed this milestone"); `docs/status.md#provider-fields-and-real-rbw-acceptance` (observed / accepted without a run / not confirmed) |

## Oppositions

| Pair (axis)                                              | Contended resource                                                 | Change the first side demands (the second resists it)                                                                                                                                             | Change the second side demands (the first resists it)                                                                                                                                                                                   | Sacrifice the project already recorded on this axis                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| -------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `S1` proof-at-write vs `S2` bounded-loop-cost            | latency (and throughput of one apply over a whole profile)         | Re-read complete bytes of every dependency, re-expand every declared directory, and re-render the desired side at every action boundary, plus `fsync` of child and parent before each publication | Scope the per-action recheck to the directories behind the current action, cache a shared layer parse keyed on bytes, and drop the per-action desired-bytes re-render once a directory has been created                                 | Cost won twice: per-action expansion is scoped (`docs/design.md`, CLI paragraph) and the rendered desired bytes of later actions are no longer compared after the first directory creation (`docs/status.md#daily-loop-changes`). Proof won on mtime-only checks, on parallel writers, and on counting durability inside the budget (`docs/maintenance.md#performance-acceptance`)                                                                                                                            |
| `S3` minimal-exposure vs `S4` continuable-evidence       | user-visible surface (and correctness of the operator's next step) | Keep results metadata-only, keep no cache, journal, digest or provider diagnostic, and let an error name only the failing boundary                                                                | Print what a later actor needs: which undeclared keys differ and in which direction, a persisted plan the apply can be matched against, and a digest that distinguishes a rotation from a hand edit                                     | Exposure won: no undeclared-value record, so `--undeclared PATH` relies on the operator's claim (`docs/status.md#daily-loop-changes`, `docs/reference.md#writing-undeclared-changes-back-to-a-template-source`); a secret digest and a provider cache were rejected (`docs/design.md#rejected-alternatives`). Evidence won where it costs no value: plan evidence is not masked, `replaces_live_on_apply`, skip reason codes, `committed_but_baseline_not_advanced`                                           |
| `S5` answerable-authority vs `S6` operational-completion | operator attention (and migration cost of moving a real profile)   | One resource per `accept` or `reverse-sync`, every undeclared leaf named at invocation, a conflict stops the resource, and no exact-name allowance that softens a sensitive-key refusal           | Adopt several resources from one observation, skip the resources that need review and write the rest, advance baselines after apply, keep application-written keys with `undeclared = "preserve"`, admit a named key via `public_names` | Completion won inside one resource: apply still writes a forward field while a sibling field needs review (`docs/status.md#daily-loop-changes`). Authority won on the generic rule: no force or precedence flag, no whole-file reverse copy, no automatic Dotter conversion (`docs/reference.md#compatibility`)                                                                                                                                                                                               |
| `S7` minimal-footprint vs `S4` recoverability            | reversibility (and durability of what a failed run leaves behind)  | Keep no journal, ledger, durable plan or replay; create no persistent entry whose path relationship to a declared target was not checked; leave committed writes in place and report `partial`    | Persist an execution journal and an exact reviewed-plan token so an interrupted run can be resumed or rolled back, and sweep residue left by a kill                                                                                     | Footprint won: no transaction engine or journal, "a fresh plan is the recovery step" (`docs/design.md#rejected-alternatives`), no exact reviewed-plan consent token (`docs/status.md#verified-scope-and-limits`), and a SIGKILL between naming and renaming can leave a private `.luwu-tmp-*` file that nothing sweeps (`docs/status.md#provider-fields-and-real-rbw-acceptance`)                                                                                                                             |
| `S8` proven-equivalence vs `S9` quiet-signal             | correctness (and the operator attention a false difference costs)  | Refuse or block anything the declared parser cannot prove: unsupported spans, Jinja blocks, non-literal ancestors, a format with no parser, a value a re-render does not reproduce                | Suppress differences that do not matter: normalize whitespace, ordering, final newline and comments, and let a formatter or a wider parser subset keep the plan quiet on a real profile                                                 | Equivalence won: no generic formatter or schema registry, a lexical TOML patcher with byte-level negative controls, re-render proof before an undeclared write (`docs/design.md#rejected-alternatives`, `docs/reference.md#writing-undeclared-changes-back-to-a-template-source`). Quiet won where evidence exists: structured semantic equality, and a converged template with a stale baseline gets a baseline-only write instead of a visible drift (`docs/design.md#observation-and-semantic-comparison`) |

Notes on two secondary contentions found while checking these pairs, recorded
because they move the same resources in the opposite direction from the rows
above:

- `S3` lost to exactness on `rbw get --raw`: `rbw get --field` was rejected
  because its custom-field match is a substring match, so Luwu reads the whole
  item (`docs/design.md#rejected-alternatives`,
  `docs/status.md#provider-fields-and-real-rbw-acceptance`). Exact matching
  here is an instance of `S8`, not of `S4`.
- `S5` constrains the mechanism `S9` would reach for: "a formatter, cache, or
  application side effect must not change who owns a value"
  (`AGENTS.md#cross-cutting-handling-boundaries`). On that decision `S5` and
  `S8` push the same direction, which is why they are not paired with each
  other.

## Unopposed stakes

- `S10` one implementation per primitive. Demanded change: add a parameter or a
  second function to `luwu/filesystem.py`, `luwu/sensitivity.py`,
  `luwu/providers.py` or `luwu/semantic.py` instead of inlining a variant, and
  lower a `OWNERS` ceiling when a copy goes. Nothing in the repository argues
  for a second copy: the nearest candidate is contract stability (keeping
  manifest versions 1 through 7 loadable), and that candidate already lost
  explicitly because three engines with separate copies let the copies drift.
  The remaining cost of extending a shared primitive is blast radius across its
  callers, and no stake here maximizes caller isolation; "independent" and
  "additive" are named as promises about contracts, not permission to copy.
- `S11` present-tense coherence. Demanded change: append a behavior to the file
  and document that already own its capability, and keep a delivery label out
  of a name. The plausible opponent is traceability of delivery order, and
  nothing maximizes it: `AGENTS.md` gives chronology one home in
  `docs/changelog.md` and forbids rewriting it, so the two do not contend a
  named resource. The one real counter-pressure, a frozen public identifier
  such as an error code or a documented anchor, is a compatibility process, not
  an objective.
- `S12` honest labelling. Demanded change: require a real-rbw walk before
  closing M8, record which assumptions were accepted without a run, and refuse
  to close a milestone until implementation, contract, tests and documents
  agree. Its only plausible opponent is delivery velocity, which appears once
  as a historical preference ("fast iteration",
  `docs/product.md#current-starting-point`) and never as something the project
  protects at a cost. Pairing it would be an axis named after a cost. Note that
  `S12` is adjacent to `S4` but not the same stake: `S4` governs what a run
  reports, `S12` governs what the repository claims.

## Missing stakes

Decision themes that recur in the owner documents, where one side of the
decision has a stake and the other side has no representative at all. These are
the gaps a stance set cannot cover by rewording an existing stake.

1. **Platform reach.** `openat2` with a component-walk fallback, `O_TMPFILE`
   with no named fallback, a refused executable under a group-writable
   ancestor, a rejected native Windows backend, and Linux-first qualification
   (`docs/design.md`, `docs/roadmap.md#m9-real-project-migration-qualification`,
   `docs/status.md#provider-fields-and-real-rbw-acceptance`). Reach is spent
   repeatedly and defended by no value test or non-goal, so `S7` is unopposed
   on this axis even though the fallback walk shows someone cared.
1. **Strong consistency against non-cooperating writers.** Advisory locks only,
   parallel writers rejected, no snapshot (`docs/reference.md#commands`,
   `docs/design.md#rejected-alternatives`,
   `docs/status.md#verified-scope-and-limits`). Neither strong consistency nor
   concurrency has a stake, so the limit is restated rather than argued.
1. **Machine adaptation.** Profiles have explicit selection and no platform
   detection (`docs/status.md#current-capability-matrix`), while the product
   problem statement starts from "different machines have different boundaries"
   (`docs/product.md#the-problem`). The objective "one declaration fits several
   machines without manual selection" has no owner.
1. **The human-facing surface.** `docs/product.md#what-luwu-is-becoming` asks
   for people and agents to be served equally and for a quiet, bookish voice,
   but every contract surface is a metadata-only JSON projection and no value
   test defends legibility or discoverability for a person. `S4` as written is
   satisfied by machine output alone.
1. **The cost of the guard apparatus itself.** Review cost is a named resource
   this project spends freely: eight review surfaces, ablations, ceilings,
   whole-byte rechecks, and a documentation ownership table. No stake defends
   review cost, diff size, or the maintenance cost of the guards, so no
   opposition can register when a new guard is cheaper to add than to justify.
1. **A winning side with no stake, once.** The recorded limit "after the first
   directory is created, the per-action freshness check no longer compares the
   rendered desired bytes of later actions"
   (`docs/status.md#daily-loop-changes`) is the one sacrifice found whose winner
   is not argued anywhere: it is listed as a limit, not as a cost decision, and
   `S2` only covers it by inference. Either `S2` owns it explicitly or the
   limit is an unjustified weakening of `S1`.

## Candidate stance set

| Stance                             | Opposition it belongs to                                                      |
| ---------------------------------- | ----------------------------------------------------------------------------- |
| `luwu-proof-cost-stance`           | `S1` proof-at-write vs `S2` bounded-loop-cost (latency)                       |
| `luwu-exposure-continuity-stance`  | `S3` minimal-exposure vs `S4` continuable-evidence (user-visible surface)     |
| `luwu-authority-completion-stance` | `S5` answerable-authority vs `S6` operational-completion (operator attention) |
| `luwu-footprint-recovery-stance`   | `S7` minimal-footprint vs `S4` recoverability (reversibility)                 |
| `luwu-equivalence-noise-stance`    | `S8` proven-equivalence vs `S9` quiet-signal (correctness)                    |

The table above is the authoring-time recommendation: one stance per axis,
owning both of its sides. The installed set splits each axis into two partisan
stances instead, one per side, because a stance that owns both sides cannot lose
anything and never produces a conflict. The installed names are recorded in
[the registry](../.agents/skills/luwu-stance-registry/SKILL.md); this table is
kept as the record of which axes were considered and which were deferred, not
as a description of the current set.

Three further stakes are deliberately not in the set: an unopposed stake needs a
check, not a stance. `S10` is enforced by
`tests/tooling/test_primitive_ownership.py` and the `luwu-primitive-reuse`
skill, `S11` by the `luwu-coherence-review` skill, and `S12` by the
`luwu-verifiability-review` skill.

## Domain checks

Domain review skills and stances share project evidence but answer different
questions. A domain review skill owns the surface checklist: what to inspect,
which evidence to demand, and where its scope ends. A stance owns an objective:
what the project protects, who pays for it, and when the conflict must stop for
a user decision. The same surface can inform several stances, and one stance
can reuse several domain checks; this is not a one-to-one hierarchy.

The existing `luwu-*-review` skills are domain checks. Their detailed surface
maps remain useful, but they must not claim to be stances. The installed
stances link to the checks instead of copying their checklists. A check that
starts arguing for a value is evidence that the stance set is missing that
value; it is not permission to turn the check into a stance by relabelling it.
