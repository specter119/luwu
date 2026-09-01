# Luwu

Luwu is an early-stage configuration orchestrator for dotfiles, developer tools, and agent configuration.

It is intended to make configuration drift explainable and repairable without pretending that every difference should be overwritten. Luwu will explore explicit resource semantics, semantic drift, field ownership, controlled reverse sync, and narrow external providers.

The project is currently a developer-confidence preview, not a general-use tool. The first vertical slice supports local `.j2` templates and source symlinks: inspect and plan are read-only, and apply requires explicit confirmation. Resource kind is inferred from the source suffix unless explicitly overridden. The implementation is deliberately allowed to grow from validated experiments rather than from a fully predetermined architecture.

## Try the preview

The preview requires Python 3.12+ and uv:

The project fixture lives under `tests/fixtures/m1`. It is used by the isolated CLI E2E check and can be copied to a temporary directory for the manual loop, so `apply` does not modify fixture data:

```text
uv sync
demo_dir="$(mktemp -d)"
trap 'rm -rf "$demo_dir"' EXIT
cp -a tests/fixtures/m1/. "$demo_dir"/
uv run luwu plan --manifest "$demo_dir/luwu.toml"
uv run luwu apply --manifest "$demo_dir/luwu.toml" --yes
uv run luwu inspect --manifest "$demo_dir/luwu.toml"
```

Read [the current reference](docs/reference.md) for the manifest, CLI, JSON, and safety contract. This preview does not include providers, secrets, reverse sync, baselines, or automatic adoption of existing files.

Run the deterministic test suite with:

```text
uv run --locked python -m unittest discover -s tests -v
```

## Documentation

- [Agent and maintainer entry point](AGENTS.md)
- [Product seed](docs/product.md)
- [Delivery roadmap](docs/roadmap.md)
- [Current implementation design](docs/design.md)
- [Current command and manifest reference](docs/reference.md)
- [M1 milestone closure record](docs/milestones/m1.md)
- [Implementation status](docs/status.md)

Future design, reference, maintenance, status, and decision documents will be added only when they have a clear owner and an independent purpose. The documentation map and single-source-of-truth rules are defined in AGENTS.md.

## Acknowledgements

Luwu is an independent implementation inspired by the concepts and workflow of [Dotter](https://github.com/SuperCuber/dotter). It does not copy or include Dotter's source code.
