# Luwu Implementation Status

Status: the single manifest contract (version 8) is implemented and technically
verified in isolated, synthetic environments. Manifest versions 1 through 7
were retired and are no longer loadable. Droid workspace trust is not
supported by decision. Real-application and real-target use is unverified. The
daily-loop fixes and the text-template and symlink resources are implemented
but have not yet had a real trial (below).

This document owns the current implementation snapshot. It does not define
product direction, public command or manifest behavior, internal mechanisms,
or delivery history. See [product](product.md), [reference](reference.md),
[design](design.md), [roadmap](roadmap.md), and the [changelog](changelog.md)
for those subjects.

## Current capability matrix

| Capability                   | Status                  | Open limits                                                                                        |
| ---------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------- |
| Structured templates         | Implemented             | JSON, JSONC, and TOML fields only                                                                  |
| Whole-file text templates    | Implemented             | Rendered bytes use the copy baseline; a new target needs explicit adoption                         |
| Symlink resources            | Implemented             | Default deploy; an existing target that is not the declared link blocks the resource               |
| Copy resources               | Implemented             | Explicit `deploy = "copy"`; an existing target needs explicit adoption                             |
| Directory sources            | Implemented             | Expanded into per-file leaves; a new leaf needs explicit adoption if it is copied                  |
| Profiles and package closure | Implemented             | Selection is explicit; no platform detection                                                       |
| Layered public inputs        | Implemented             | Public scalar values only                                                                          |
| Observation                  | Implemented             | An unsafe live path blocks that resource; the other resources are still observed                   |
| Ownership and baselines      | Implemented             | Undeclared content is never adopted implicitly                                                     |
| Accept, adopt, reverse sync  | Implemented             | One resource per command; a changed declaration needs an explicit re-accept                        |
| Undeclared write-back        | Implemented             | Literal template subtrees only; every other case is refused                                        |
| Profile-wide apply           | Implemented             | Review resources are skipped and the outcome is `partial`; batch-level blockers stop the batch     |
| Batch performance            | Implemented             | Budgets are in [maintenance](maintenance.md#performance-acceptance); rerun them on the target host |
| Providers and secrets        | Not implemented         | Retired with version 6; planned in the [roadmap](roadmap.md)                                       |
| Droid workspace trust        | Unsupported by decision | No stable public file schema is known                                                              |

The exact fields, states, error codes, output projections, compatibility
behavior, and unsupported cases are owned only by [reference](reference.md).
The rows above are status claims, not a second contract.

## Daily-loop changes awaiting a real trial

These changes closed the gaps that adversarial review found against the
synthetic fixtures. They are implemented and covered by the tests and
ablations, but the [roadmap](roadmap.md#m7a-daily-loop-usability) keeps M7a
open until a trial on real public application files. Treat them as unproven on
a real profile.

- A confirmed apply advances the baseline of the resources it wrote.
- Apply skips a resource that needs review and writes the others.
- `undeclared = "preserve"` keeps application-written keys without adopting
  them.
- `public_names` admits a named key that the sensitive-name check rejects.
- The adopt preview says when applying would replace live bytes.

## Verified scope and limits

Verification is the test suite under `tests/` plus the ablations in
`experiments/`, which disable selected guards in production modules and check
that a test or probe notices. Benchmarks in `experiments/` measure
performance only; they do not verify correctness. The commands are in
[maintenance](maintenance.md). Their output is not committed; rerun them
against the checkout and installed artifact before relying on a result.

The implementation does not claim:

- automatic replay, rollback, or recovery mutation;
- exact reviewed-plan consent tokens;
- strong consistency against writers that ignore Luwu's advisory locks;
- providers, network access, or a broad platform matrix beyond the verified
  contract;
- that any application read a synthetic fixture, or that any real target may be
  handed off. The per-target procedure is in
  [maintenance](maintenance.md#isolated-rehearsal-and-dotter-handoff) and the
  [isolated profile fixture guide](../tests/fixtures/dotfiles_profile/README.md).

When code or tests change, refresh this snapshot from current evidence. Do not
carry a completion statement forward without rerunning or explicitly
revalidating the relevant gate.
