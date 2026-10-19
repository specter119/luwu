# Luwu Implementation Status

Status: the single manifest contract (version 8) is implemented and technically
verified in isolated, synthetic environments. Manifest versions 1 through 7
were retired and are no longer loadable. Droid workspace trust is not
supported by decision. Real-application and real-target use is unverified, and
the daily edit-and-apply loop still has known gaps (below).

This document owns the current implementation snapshot. It does not define
product direction, public command or manifest behavior, internal mechanisms,
or delivery history. See [product](product.md), [reference](reference.md),
[design](design.md), [roadmap](roadmap.md), and the [changelog](changelog.md)
for those subjects.

## Current capability matrix

| Capability                   | Status                  | Open limits                                                                                        |
| ---------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------- |
| Template and copy resources  | Implemented             | Structured templates only (JSON, JSONC, TOML); no text templates and no symlink resources          |
| Directory resources          | Implemented             | Enumerated into copy leaves; a new leaf needs explicit adoption                                    |
| Profiles and package closure | Implemented             | Selection is explicit; no platform detection                                                       |
| Layered public inputs        | Implemented             | Public scalar values only                                                                          |
| Observation                  | Implemented             | An unsafe live path blocks that resource; the other resources are still observed                   |
| Ownership and baselines      | Implemented             | Undeclared content is never adopted implicitly                                                     |
| Accept, adopt, reverse sync  | Implemented             | One resource per command; a changed declaration needs an explicit re-accept                        |
| Profile-wide apply           | Implemented             | Any blocked, review, or unbased resource stops the whole batch; no per-resource apply              |
| Batch performance            | Implemented             | Budgets are in [maintenance](maintenance.md#performance-acceptance); rerun them on the target host |
| Providers and secrets        | Not implemented         | Retired with version 6; planned in the [roadmap](roadmap.md)                                       |
| Droid workspace trust        | Unsupported by decision | No stable public file schema is known                                                              |

The exact fields, states, error codes, output projections, compatibility
behavior, and unsupported cases are owned only by [reference](reference.md).
The rows above are status claims, not a second contract.

## Known daily-loop gaps

These behaviors were found by adversarial review against the synthetic
fixtures and are open. The [roadmap](roadmap.md) owns the plan to close them.

- A successful apply does not advance the baseline, so a routine edit-and-apply
  cycle needs a manual accept before the next apply.
- One resource that needs review stops the whole profile, and there is no
  way to apply a single resource.
- A key written by the application outside the declared fields blocks that
  resource until the declaration or the content is corrected by hand.
- Several no-follow directory walks in `manifest.py` and `engine.py` duplicate
  `luwu/filesystem.py`. The guard test caps them, and they are not yet merged.
- The sensitive-name check is deliberately conservative: a declared or
  parsed key such as `env_key` is rejected even when it holds no secret.
- Adopting a copy records the live bytes as the baseline, so the next apply
  reports the source as changed and replaces those live bytes. Live edits
  present at adoption are lost unless the operator saves them first.

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
