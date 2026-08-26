# Luwu

Luwu is an early-stage configuration orchestrator for dotfiles, developer tools, and agent configuration.

It is intended to make configuration drift explainable and repairable without pretending that every difference should be overwritten. Luwu will explore explicit resource semantics, semantic drift, field ownership, controlled reverse sync, and narrow external providers.

The project is currently a product seed and is not ready for general use. The implementation is deliberately allowed to grow from validated experiments rather than from a fully predetermined architecture.

## Documentation

- [Agent and maintainer entry point](AGENTS.md)
- [Product seed](docs/product.md)

Future design, reference, maintenance, status, and decision documents will be added only when they have a clear owner and an independent purpose. The documentation map and single-source-of-truth rules are defined in AGENTS.md.

## Acknowledgements

Luwu is an independent implementation inspired by the concepts and workflow of [Dotter](https://github.com/SuperCuber/dotter). It does not copy or include Dotter's source code.
