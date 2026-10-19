# Luwu Delivery Roadmap

Status: M1-M5 are delivered. M6 and M7 are closed as technical deliveries on
the single version 8 contract; their evidence comes from synthetic fixtures
only. M7a (daily-loop usability) is the next milestone and gates real use.
M7b, M8, and M9 are planned.

This document owns delivery sequence, open exit conditions, and boundaries for
work that is not finished. It does not define detailed contracts or the current
implementation snapshot. Product direction is in [product.md](product.md),
verified implementation facts are in [status.md](status.md), and what each
delivered milestone added is in the [changelog](changelog.md). A delivered
milestone is not rewritten here to absorb new work.

The roadmap may change when evidence changes the product direction. A change
to an active milestone's scope should be explicit.

## Milestone map

| Milestone | Focus                                   | Outcome                                                                                                           |
| --------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| M1-M5     | Preview through batch performance       | Delivered; see the [changelog](changelog.md).                                                                     |
| M6        | First bidirectional pilot               | Closed: the pilot contract was retired and its loop lives in version 8; the user trial moves to M7a.              |
| M7        | Full dotfiles coverage                  | Closed: technical core delivered; Droid workspace trust is unsupported by decision.                               |
| M7a       | Daily-loop usability                    | Planned, next: the routine edit, review, and apply loop works on a real profile without manual file surgery.      |
| M7b       | Text templates and symlink resources    | Planned: non-structured templates and symlink resources exist on the version 8 contract.                          |
| M8        | Providers and mixed inputs on version 8 | Planned: public, local, and provider inputs work together without persistent secret intermediates.                |
| M9        | Real-project migration qualification    | Planned: a selected dotfiles profile has measured coverage, a tested handoff, and explicit residual dependencies. |

The milestone identifiers do not allocate manifest versions. Before changing
public behavior, define the compatible extension or version transition in
[reference](reference.md), explain mechanisms in [design](design.md), and
update [status](status.md) only from implementation evidence.

## M6: First bidirectional pilot (closed)

The pilot proved one connected loop: declared global and local inputs and a
template, reviewed deployment, an application change, ownership and conflict
observation, a selected reverse-sync preview, explicit confirmation, input
update, recalculation, and safe redeployment. That loop is now an ordinary
property of the version 8 contract. The pilot's separate manifest version,
trial fixtures, and rehearsal runner were retired with versions 1 through 7.

The pilot's user trial was never run. It is not closed by retirement; it is
replaced by the M7a exit, which runs the same kind of trial on version 8.
Confidentiality boundary: the loop covers public values only. Classify the
whole read/write flow before allowing a real target, use an isolated
application profile or leave the target with its current manager when the
public boundary cannot be established, and never strip credentials from a
production file to force it into Luwu. Providers belong to M8.

## M7: Full dotfiles coverage (closed)

The delivery covers explicit profiles, package closure and exclusion, layered
public inputs with provenance, template, copy, and directory resources,
declared parent and file modes, explicit adoption, JSONC and nested TOML paths,
and selected multi-resource mutation. The application-mapping targets are
OpenCode v1.19.0 JSONC, Codex v0.162.1 project and hook configuration, and
Droid 0.240.0 settings, exercised with synthetic files only. Exact paths,
values, and ownership limits belong to the
[contract](reference.md#manifest-and-profile-contract).

Decision: Droid workspace trust is unsupported. No stable public file schema
has been verified, and SDK trust methods do not establish file compatibility,
so Luwu makes no trust-file mapping. Reopening this needs a reviewed file
schema and tests.

Residual limit carried forward: no application runtime has loaded the
fixtures, and adversarial review showed the daily loop breaks outside them.
M7 is therefore a technical delivery, not a claim that a real profile works.
M7a owns that gap.

## M7a: Daily-loop usability

Make the routine loop (edit the source or the application, review, apply)
work on a real profile without deleting baselines or editing files by hand.
Open gaps, from [status](status.md#known-daily-loop-gaps):

1. A successful apply leaves the baseline stale, so the next cycle needs a
   manual accept. Define explicit, confirmed baseline advancement for the
   fields an apply just wrote.
1. One resource needing review stops the whole profile. Define per-resource
   apply so unrelated resources still deploy, without weakening the rule that
   conflicts stop for review.
1. A key written by the application outside the declared fields blocks the
   resource. Define an explicit, reviewable policy for preserving undeclared
   keys; undeclared content must still never be adopted implicitly.
1. The sensitive-name check rejects keys such as `env_key` that hold no
   secret. Define an explicit, reviewed allowance that keeps the fail-closed
   default.
1. Adopting a copy then applying replaces the live bytes. Make that outcome
   visible in the adopt preview before confirmation.

Exit: a trial on the user's real, public application files in an isolated
profile, run on the installed artifact, covering a fresh deployment, a
routine source change, a routine application change with reverse sync, and a
conflict. Feedback is supplied as redacted metadata (setup friction, latency,
clarity of ownership and conflict explanations, preservation of application
changes, recovery, and missing high-value fields). The setup commands are in
the [isolated profile fixture guide](../tests/fixtures/dotfiles_profile/README.md)
and [maintenance](maintenance.md#isolated-rehearsal-and-dotter-handoff).

## M7b: Text templates and symlink resources

Version 8 has only structured templates and whole-file copies. Real
dotfiles also contain text templates (for example shell fragments and
unstructured configuration) and symlinks into the source tree. Add both to the
version 8 contract with the same boundaries: explicit declaration, no implicit
adoption, no-follow writes, and a baseline that identifies what was accepted.
A symlink resource must refuse to follow or replace an undeclared link.

Exit: contract text in [reference](reference.md), tests for each new resource
kind including hostile-path cases, and a fixture shape for each.

## M8: Providers and mixed inputs on version 8

Provisional scope; implementation starts after M7a feedback.

The earlier provider implementation (manifest version 6) was retired with its
manifest version and is available only in git history. Rebuild the capability
on version 8 instead of reviving a second manifest version. Connect public and
machine-local inputs with provider-owned secrets in one explainable
rendering and deployment workflow. Establish bounded rbw environment and
socket discovery, executable authority, and explicit failure behavior. Keep
provider-owned fields out of baselines and reverse-sync destinations, and
generate no secret-bearing public intermediate files.

Exit: isolated fake-provider fault tests and a separately authorized real-rbw
acceptance exercise cover locked or unavailable vaults, executable and
environment changes, permissions, output confidentiality, and current-state
recovery. Public local choices survive a provider-backed redeploy. If real
integration is blocked, retain partial status; mock success cannot close this
milestone. User-service reloads and other provisioning remain explicit external
stages, not hooks hidden inside observation or apply.

## M9: Real-project migration qualification

Provisional scope; M7a feedback and subsequent deliveries determine the
profile and coverage that will be qualified.

Qualify a selected `specter119/dotfiles` profile against its actual package
closure and machine exclusions, rather than the count of globally mapped files.
Account for every selected resource, generator or hook responsibility, and each
of the old reverse-sync script's 15 slots as replaced, deliberately retained,
or unsupported. Any new capability needed to remove a residual dependency gets
an explicit scope decision; M9 is not an unlimited feature backlog.

Exit: rehearse fresh deployment, existing-Dotter handoff, routine application
changes, source changes, conflict review, interruption, and manual recovery in
an isolated environment using the installed artifact. The handoff and return
procedure is the version 8 procedure in
[maintenance](maintenance.md#isolated-rehearsal-and-dotter-handoff). Then
perform a separately authorized limited live canary and record its observation
period and results. Provide a runbook that prevents double management and
explains how to return responsibility to the previous manager without automatic
rollback or secret backups. Recheck the performance budgets and all adopted
platform assumptions.

Linux is the first qualification target. WSL exclusions must be exercised;
native Windows support requires its own passing matrix and is not implied by
path tests. A scoped migration may close with explicit residuals, but a claim
that Dotter or the old reverse-sync script can be removed requires zero
remaining dependency on that component for the selected profile. Broad YAML
support, automatic rollback or replay, and strong consistency against
unrelated writers remain outside this sequence unless separately scoped.

## Roadmap completion rule

A milestone closes only when its implementation, public contract, tests, and documentation agree. A capability described here is planned until the current status document and the tests provide evidence that it exists.
