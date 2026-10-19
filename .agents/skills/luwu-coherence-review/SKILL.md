---
name: luwu-coherence-review
description: >-
  Use when reviewing how Luwu tests, fixtures, documents, experiments, or
  committed artifacts are organized, or when a change adds files named after a
  delivery, milestone, or ticket. Test whether a capability can be understood
  from its current form in one place, without replaying delivery order. Do not
  use to judge whether evidence is sufficient or how src modules are split.
---

# Luwu coherence review

Delivery is a sequence of events. The repository describes a product. When the
order in which work arrived becomes the way the repository is organized, a
reader must reconstruct history to find out what is true now.

## Stable core and changing contract

The stable value is: **a reader must be able to understand a capability from
its current form, in one place, without replaying the order in which it was
delivered.** The test layout, the documentation owners, the changelog, and the
rules for run output are current expressions of that value; a different layout
may serve it better.

When the organization changes, identify which question a reader asks first
(what does this capability do now, and what protects it), and compare how many
places and how much chronology the old and new layouts require to answer it.
Do not treat a particular directory scheme as sacred; do reject a layout whose
primary key is when something was delivered.

Read `AGENTS.md` and the owner of the fact before judging. Then ask:

- To find the behavior of one capability, must the reader first learn which
  delivery introduced it?
- Is one capability split across parallel files that differ only by delivery
  label, so a later change has to guess which file to append to?
- Does a new behavior append to the place its capability already lives, or
  does it open a new file for its own delivery?
- Is a committed result, such as a benchmark sample, profile, or acceptance
  report, standing in for a check that could be rerun?
- Does a document describe what is true now, or narrate how it became true?

Attack file and class names that carry a delivery label, duplicated helpers
that exist because each delivery copied its own (judged by
`luwu-duplication-review`), fixtures named for the
delivery that needed them, and documents that restate dated outcomes as if
they were current. Also attack the opposite failure: a merge that hides a
real compatibility difference. Behavior that differs by manifest version is a
genuine dimension of the capability and stays visible; a delivery label is not.
A version number can hide a delivery label: if a file or class is named for a
version only because one delivery shipped it, and it mixes several
capabilities (planning, authorization, reverse sync, write failures), the
version is acting as the milestone. Split it by capability and keep the version
as a dimension inside each.

Report the capability, the places a reader has to visit, the chronology they
must know, and the smallest reorganization that lets one place answer the
question. Mark whether the finding concerns tests, fixtures, documents,
experiments, or artifacts.

## Scope boundary

This stance owns whether delivery chronology has become repository structure.
It does not own how `src/` modules are divided (design decides that from the
product's contracts), whether evidence is sufficient (verifiability), exact
workflow rules (`docs/maintenance.md`), or public field and state semantics
(`docs/reference.md`). A name frozen into a public identifier, such as an error
code or a documented anchor, is a compatibility matter: change it only through
the reference compatibility process, not as a side effect of tidying.

## Good patterns

- Append a test to the module for its capability. Evidence: the layout
  rule in [maintenance](../../../docs/maintenance.md#test-layout).
- Record what a delivery added in one small changelog while the current
  contract, mechanism, and status live in their owners. Evidence:
  [changelog](../../../docs/changelog.md) and the ownership table in
  `AGENTS.md`.

## Bad patterns

- Add `test_<delivery>_*.py` beside the capability's existing module, or a
  `tests/fixtures/<delivery>/` tree. Evidence: the earlier layout is visible
  with `git ls-tree -r --name-only bb80335 tests`.
- Commit benchmark, profile, or acceptance output under a delivery directory.
  The output is a snapshot of one machine, goes stale, and cannot be reviewed
  as a check. Keep the command, not the capture.

Patterns are evidence about organization. The workflow rules stay in
`docs/maintenance.md`, and the document owners stay in `AGENTS.md`.
