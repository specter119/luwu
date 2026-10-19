# Luwu

Luwu is an early-stage configuration orchestrator for dotfiles, developer
tools, and agent configuration.

It is intended to make configuration drift explainable and repairable without
pretending that every difference should be overwritten. Luwu explores explicit
resource semantics, semantic drift, field ownership, controlled reverse sync,
and narrow external providers.

The project is an early developer preview with a bounded public Pi/Codex
bidirectional pilot. Inspect and plan are read-only; writes require explicit
confirmation. Start with the isolated examples below and consult the
[current verified scope](docs/status.md) before selecting a real target.

## Try the preview

The preview requires Python 3.12+ and uv:

The project fixture lives under `tests/fixtures/quickstart_v1_template`. It is used by the
isolated CLI E2E check and can be copied to a temporary directory for the
manual loop, so `apply` does not modify fixture data:

```text
uv sync
demo_dir="$(mktemp -d)"
trap 'rm -rf "$demo_dir"' EXIT
cp -a tests/fixtures/quickstart_v1_template/. "$demo_dir"/
uv run luwu plan --manifest "$demo_dir/luwu.toml"
uv run luwu apply --manifest "$demo_dir/luwu.toml" --yes
uv run luwu inspect --manifest "$demo_dir/luwu.toml"
```

Read [the current reference](docs/reference.md) for the manifest, CLI, JSON,
and safety contract, and [implementation status](docs/status.md) for the
verified scope. The reference and status documents are the authority for
current capabilities; this page intentionally stays at user-entry-point
level.

For the public Pi JSON and Codex TOML bidirectional loop, use the
[isolated pilot trial guide](tests/fixtures/pilot_v7/README.md) and the
[per-target Dotter handoff workflow](docs/maintenance.md#isolated-rehearsal-and-dotter-handoff).
The trial uses a small field allowlist and requires the complete target and
input flow to be public. It does not qualify a whole dotfiles migration.

For the full dotfiles profile, start with the
[version 8 reference](docs/reference.md#full-dotfiles-coverage-manifest-version-8),
check [current implementation status](docs/status.md), and use the
[isolated profile fixture guide](tests/fixtures/dotfiles_profile/README.md).

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
