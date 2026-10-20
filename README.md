# Luwu

Luwu is an early-stage configuration orchestrator for dotfiles, developer
tools, and agent configuration.

It is intended to make configuration drift explainable and repairable without
pretending that every difference should be overwritten. Luwu explores explicit
resource semantics, semantic drift, field ownership, controlled reverse sync,
and narrow external providers.

The project is an early developer preview. A manifest declares profiles,
packages, layered public inputs, and resources. `inspect` and `plan` are
read-only; writes require explicit confirmation. Start with the isolated
example below and consult the [current verified scope](docs/status.md) before
selecting a real target.

## Try the preview

The preview requires Python 3.12+ and uv. The synthetic profile fixture lives
under `tests/fixtures/dotfiles_profile`. Copy it to a temporary directory so
`apply` does not modify fixture data, and point HOME there so nothing outside
the copy is touched:

```text
uv sync
demo_dir="$(mktemp -d)"
trap 'rm -rf "$demo_dir"' EXIT
cp -a tests/fixtures/dotfiles_profile/. "$demo_dir"/
export HOME="$demo_dir/home"
uv run luwu plan --manifest "$demo_dir/luwu.toml" --profile synthetic
```

A fresh profile needs an explicit `accept` for structured templates before
`apply --yes` writes them, and an explicit `adopt` for a copy or text
template whose target already exists. Symlink resources, the default
deployment, need none, a copy or text template with an absent target is
created by apply, and a confirmed apply advances the baselines it wrote. The
[isolated profile fixture guide](tests/fixtures/dotfiles_profile/README.md)
walks through the complete loop, including reverse sync.

Read [the current reference](docs/reference.md) for the manifest, CLI, JSON,
and safety contract, and [implementation status](docs/status.md) for the
verified scope and open limits. The reference and status documents are the
authority for current capabilities; this page intentionally stays at
user-entry-point level. The [per-target Dotter handoff
workflow](docs/maintenance.md#isolated-rehearsal-and-dotter-handoff) applies
before a real target is managed.

Run the deterministic test suite with:

```text
uv run --locked python -m unittest discover -s tests -t . -v
```

## Documentation

- [Product direction](docs/product.md)
- [Current command and manifest reference](docs/reference.md)
- [Implementation status](docs/status.md)
- [Current internal design](docs/design.md)
- [Development and release workflow](docs/maintenance.md)
- [Delivery roadmap](docs/roadmap.md)
- [Changelog](docs/changelog.md)
- [Agent and maintainer entry point](AGENTS.md)

Documentation ownership and single-source-of-truth rules are defined in
`AGENTS.md`.

## Acknowledgements

Luwu is an independent implementation inspired by the concepts and workflow of
[Dotter](https://github.com/SuperCuber/dotter). It does not copy or include
Dotter's source code.
