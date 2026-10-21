# Luwu Delivery Roadmap

Status: M1-M5 are delivered. M6 and M7 are closed as technical deliveries on
the single version 8 contract; their evidence comes from synthetic fixtures
only. M7a (daily-loop usability) and M7b are closed. M8 is implemented
against a fake provider and waits for a real rbw acceptance, and M9 is
planned.

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
| M7a       | Daily-loop usability                    | Closed: the routine loop ran on a real dotfiles repository; findings reopen the affected item.                    |
| M7b       | Text templates and symlink resources    | Closed: whole-file text templates and symlink resources exist on the version 8 contract.                          |
| M8        | Providers and mixed inputs on version 8 | Implemented, pending real rbw acceptance: provider fields work with public and local inputs, no secret files.     |
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

## M7a: Daily-loop usability (closed)

Make the routine loop (edit the source or the application, review, apply)
work on a real profile without deleting baselines or editing files by hand.
The five gaps found by adversarial review each have an implementation (see
[status](status.md#daily-loop-changes) and the
[contract](reference.md#resources-and-declared-scope)): baseline advancement
after apply, per-resource apply, preserving undeclared keys, an exact-name
sensitive-name allowance, and an adopt preview that shows replaced live bytes.

The maintainer accepted the migration of a real dotfiles repository onto
version 8 (the daily-loop pull request and its sandbox rehearsal, with
public values only) as the trial and closed M7a. The evidence is that pull
request's description, not a committed report. It did not cover every scenario
of the original exit (fresh deployment, routine source change, routine
application change with reverse sync, and a conflict, each on the installed
artifact), so a later finding on any of them reopens the affected item. The
setup commands are in the
[isolated profile fixture guide](../tests/fixtures/dotfiles_profile/README.md)
and [maintenance](maintenance.md#isolated-rehearsal-and-dotter-handoff).

## M7b: Text templates and symlink resources (closed)

Whole-file text templates and symlink resources were added to version 8 with
the same boundaries as other resources: explicit declaration, no implicit
adoption, no-follow writes, and a baseline that identifies what was accepted
(a symlink resource needs none, because its content is its declaration). A
symlink resource refuses to follow or replace an undeclared link. Hardlink and
reflink deployment are rejected; see the
[design](design.md#rejected-alternatives).

## M8: Providers and mixed inputs on version 8

The earlier provider implementation (manifest version 6) was retired with its
manifest version and is available only in git history. Rebuild the capability
on version 8 instead of reviving a second manifest version. Connect public and
machine-local inputs with provider-owned secrets in one explainable
rendering and deployment workflow. Establish bounded rbw environment and
socket discovery, executable authority, and explicit failure behavior. Keep
provider-owned fields out of baselines and reverse-sync destinations, and
generate no secret-bearing public intermediate files.

Scope decision: M8 supports provider-owned scalar fields of a structured
template (JSON, JSONC, TOML) and one provider type, rbw. A provider value is
written into the document after rendering and never passes through the
template engine. Whole-file secret text (a rendered `.j2` carrying a secret) is
deferred, so formats that are not structured, such as `.netrc` or a bare token
file, cannot have a secret managed until a later scope decision adds a format
parser or a separate secret-text resource. A persisted digest of secret bytes,
a provider registry, and a provider cache are rejected; see the
[design](design.md#rejected-alternatives). The contract is in
[reference](reference.md#provider-owned-fields) and the current state in
[status](status.md#provider-fields-awaiting-real-rbw-acceptance).

Exit: two parts with separate evidence. Isolated fake-rbw tests prove Luwu's
own behavior: locked or unavailable vaults, executable and environment
changes, permissions, output confidentiality, current-state recovery, and
public local choices that survive a provider-backed redeploy. A separately
authorized real-rbw run proves only what a fake cannot: that the installed rbw
accepts the argument form, returns the JSON keys, reports a locked agent, and
lays out its socket as the contract assumes, and that a stopped agent, with or
without a socket file left behind, is not restarted. The fake-rbw tests, confidentiality scans, and the manual script
`experiments/rbw_acceptance.py` exist; the real run is the open gate. If real
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
