# Luwu Delivery Roadmap

Status: M1-M5 are delivered. M6 technical acceptance is complete and its user
trial is pending. The complete M7 scope is implemented as manifest version 8
and technically verified; its closure remains open on the Droid
workspace-trust schema. M8-M9 are planned.

This document owns delivery sequence, open exit conditions, and boundaries for
work that is not finished. It does not define detailed contracts or the current
implementation snapshot. Product direction is in [product.md](product.md),
verified implementation facts are in [status.md](status.md), and what each
delivered milestone added is in the [changelog](changelog.md). A delivered
milestone is not rewritten here to absorb new work.

The roadmap may change when evidence changes the product direction. A change
to an active milestone's scope should be explicit.

## Milestone map

| Milestone | Focus                                        | Outcome                                                                                                                           |
| --------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| M1-M5     | Preview through batch performance            | Delivered; see the [changelog](changelog.md).                                                                                     |
| M6        | First usable bidirectional pilot             | Technical acceptance complete: the bounded Pi/Codex loop and trial handoff are ready; the user trial is pending.                  |
| M7        | Full dotfiles coverage                       | Technical core implemented and verified; closure open on Droid workspace-trust schema evidence.                                   |
| M8        | Real provider and mixed-input integration    | Planned: public, local, and provider inputs work together without persistent secret intermediates.                                |
| M9        | Real-project migration qualification         | Planned: a selected dotfiles profile has measured coverage, a tested handoff, and explicit residual dependencies.                 |

The milestone identifiers do not allocate manifest versions. Before changing
public behavior, define the compatible extension or version transition in
[reference](reference.md), explain mechanisms in [design](design.md), and
update [status](status.md) only from implementation evidence.

## M6: First usable bidirectional pilot

Deliver the core value in one connected workflow: declared global and local
inputs and a template, reviewed deployment, an application change, ownership
and conflict observation, a selected reverse-sync preview, explicit
confirmation, input update, recalculation, and safe redeployment. A reduced
literal-JSON model or disconnected forward and reverse commands cannot close
this milestone. It is a bounded trial on selected Pi and Codex public
configuration, not replacement of all Dotter packages.

The pilot's acceptance cases, for both application shapes, are:

1. Fresh deployment into an isolated HOME, a clean observation, and a no-op
   redeploy.
1. An application-side change observed as the reverse candidate; preview
   changes nothing, confirmation patches only selected declared inputs,
   verification recalculates, and redeployment keeps the choice.
1. A source-owned change applied forward without overwriting unrelated
   live-owned state.
1. A two-sided conflict, stale baseline or input, and wrong-owner change stop
   before a write with enough explanation for a decision.
1. An unrelated new live key is preserved under an explicit policy or stops for
   review; it is never deleted to claim a round trip.
1. Comments, absent versus null, field deletion, unsupported syntax, and
   unrelated local input content are handled without silent normalization.
1. Interruption or post-write verification failure is reported honestly with a
   tested next inspection step and no automatic replay.

Confidentiality boundary: the pilot covers public values only. Classify the
whole read/write flow before allowing a real target, use an isolated
application profile or leave the target with its current manager when the
public boundary cannot be established, and never strip credentials from a
production file to force it into the pilot. Real providers belong to M8.

Open exit condition: the user trial. Technical acceptance makes the pilot ready
for trial; it does not show the trial succeeded. The user's feedback (setup
friction, latency, clarity of ownership and conflict explanations,
preservation of application changes, recovery, and missing high-value fields,
supplied as redacted metadata rather than configuration dumps) reviews M8-M9
scope. The checklist, copyable commands, and handoff procedure are in the
[isolated trial guide](../tests/fixtures/pilot_v7/README.md) and
[maintenance](maintenance.md#isolated-rehearsal-and-dotter-handoff). This
checkpoint was requested by the user, not inferred from a skill.

## M7: Full dotfiles coverage

The complete scope is one milestone, not slices waiting for M6 feedback. It
covers explicit profiles and package closure and exclusion, layered public
inputs with provenance, template, copy, and directory resources, declared
parent and file modes, explicit adoption, JSONC and nested TOML paths, and
selected multi-resource mutation. The application-mapping acceptance targets
OpenCode v1.19.0 JSONC, Codex v0.162.1 project and hook configuration, and
Droid 0.240.0 settings, exercised with synthetic files only. Exact paths,
values, and ownership limits belong to the
[version 8 contract](reference.md#full-dotfiles-coverage-manifest-version-8).

Open exit condition: no stable public file schema for Droid workspace trust
has been verified. SDK trust methods do not establish file compatibility, and
no application runtime has loaded the fixtures. M7 therefore cannot claim
unconditional completion, and Luwu does not claim a trust-file mapping.
Closing it needs a reviewed file schema and tests, or an explicit decision to
leave the capability unsupported.

The applicable M6 bidirectional guarantees and the performance budgets in
[maintenance](maintenance.md#performance-acceptance) remain acceptance
constraints. Machine exclusions such as WSL mihomo non-ownership are explicit
profile data. A universal Dotter importer, broad YAML support, and all
historical reverse-sync slots remain outside automatic scope commitments.

## M8: Real provider and mixed-input integration

Provisional scope; implementation starts only after the M6 feedback checkpoint
and review of the selected M7 dependencies.

Connect public and machine-local inputs with provider-owned secrets in one
explainable rendering/deployment workflow. Establish bounded rbw environment
and socket discovery, executable authority, and explicit failure behavior.
Integrate one representative enterprise gateway consumer without generating
secret-bearing public intermediate files. Keep provider-owned fields out of
baselines and reverse-sync destinations.

Exit: isolated fake-provider fault tests and a separately authorized real-rbw
acceptance exercise cover locked/unavailable vaults, executable/environment
changes, permissions, output confidentiality and current-state recovery.
Public local choices survive a provider-backed redeploy. If real integration
is blocked, retain partial status; mock success cannot close this milestone.
User-service reloads and other provisioning remain explicit external stages,
not arbitrary hooks hidden inside observation or apply.

## M9: Real-project migration qualification

Provisional scope; the M6 trial feedback and subsequent deliveries determine
the profile and coverage that will be qualified.

Qualify a selected `specter119/dotfiles` profile against its actual package
closure and machine exclusions, rather than the count of globally mapped files.
Account for every selected resource, generator/hook responsibility and each of
the pilot's 15 reverse-sync slots as replaced, deliberately retained, or
unsupported. Any new capability needed to remove a residual dependency gets
an explicit scope decision; M9 is not an unlimited feature backlog.

Exit: rehearse fresh deployment, existing-Dotter handoff, routine application
changes, source changes, conflict review, interruption and manual recovery in
an isolated environment using the installed artifact. Then perform a separately
authorized limited live canary and record its observation period and results.
Provide a handoff/runbook that prevents double management and explains how to
return responsibility to the previous manager without automatic rollback or
secret backups. Recheck the performance budgets and all adopted platform assumptions.

Linux is the first qualification target. WSL exclusions must be exercised;
native Windows support requires its own passing matrix and is not implied by
path tests. A scoped migration may close with explicit residuals, but a claim
that Dotter or the old reverse-sync script can be removed requires zero
remaining dependency on that component for the selected profile. Broad YAML
support, automatic rollback/replay and strong consistency against unrelated
writers remain outside this sequence unless separately scoped.

## Roadmap completion rule

A milestone closes only when its implementation, public contract, tests, and documentation agree. A capability described here is planned until the current status document and the tests provide evidence that it exists.
