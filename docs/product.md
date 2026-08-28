# Luwu Product Seed

Status: seed

## The problem

Existing dotfiles tools are good at deploying repository content into live configuration. Real configuration, however, is not determined by the repository alone. Applications add their own state, users make machine-specific choices, secrets come from external providers, and different machines have different boundaries. A text-only comparison cannot tell whether a difference matters or who is entitled to accept or repair it.

Luwu is not primarily another tool for copying files over existing files. It coordinates configuration across sources and over time: it should understand declared resources, identify meaningful drift, explain responsibility, and let the user choose the next action.

## What Luwu is becoming

Luwu is a configuration orchestrator for dotfiles, developer tools, and agent configuration. It should:

- make the relationship between shared configuration, machine-local configuration, provider values, and live configuration visible;
- make the semantics of template, link, copy, and related resources explicit instead of inferred from cache state or guesswork;
- understand drift semantically rather than treating incidental text differences as meaningful by default;
- use field ownership to make reverse sync selective and accountable;
- stop when evidence is incomplete or sources conflict, and explain why;
- give people and agents equally reliable observations so maintenance can continue without hidden context;
- remain restrained around secrets, user files, and external capabilities.

Luwu should feel quiet, careful, slightly bookish, and willing to admit uncertainty. It may have a sense of humor, but humor must never hide risk.

## Product scope

The initial problem space includes:

- declarative orchestration for dotfiles, developer tools, and agent configuration;
- explicit resource and template boundaries;
- observation, explanation, and classification of semantic drift;
- responsibility across source, local, provider, and live values;
- controlled reverse sync based on a known baseline and declared ownership;
- auditable, recoverable plans that people and agents can continue to process;
- narrow support for secrets and other external providers.

This is a product direction, not a promise that the first implementation will support every item.

## Product non-goals

Luwu is not intended to:

- replace responsibility between sources with one universal precedence rule;
- adopt existing files, migrate old structures, or resolve conflicts without evidence and explicit consent;
- become a universal configuration database or an in-application configuration editor;
- hide arbitrary shell execution, implicit network access, or runtime bootstrap inside templates or ordinary apply operations;
- save, copy, or expose secrets merely to remove a confirmation step;
- turn the current implementation language, module shape, or command-line form into the definition of the product.

Non-goals may change when real user problems change, but such a change must be explicit rather than smuggled in as a temporary exception.

## A boundary for future implementation

Semantic comparison may use parsing and normalization, but a generic formatter is not a product goal. It is valuable only when it reduces false drift or enables a safe structured patch. It must not rewrite Jinja templates wholesale or silently discard unsupported YAML features. The exact parser, preservation strategy, and supported subset belong in a future design or reference document.

For a template resource, compare the rendered configuration rather than the template source. Reverse sync should update declared inputs or fields, never copy a live file wholesale into a Jinja template. Provider-owned secrets remain read-only inputs; non-secret machine-local values may be accepted only when their ownership is declared.

## Value tests

A direction is worth pursuing when it makes these questions easier to answer:

1. Where did this configuration come from, and who owns it now?
1. Is the observed change real drift, formatting, application state, or undeclared content?
1. What is the impact and risk of accepting or repairing it?
1. Can the user see the reason before acting and recover after failure?
1. Can the next agent or maintainer continue from the result without hidden context?

A feature that only makes overwriting more convenient, while making these questions harder to answer, is not automatically progress for Luwu.

## How the product should evolve

This is a product seed, not a completed master design. Early implementation may keep several hypotheses alive and use fixtures, real usage, and maintenance experience to remove them. A choice should become a stable design or decision record only after it has been validated, has a broad impact, or is difficult to reverse.

This document owns what Luwu is trying to become and why it is valuable. Current internal behavior belongs in design documents, stable user or machine contracts belong in reference documents, and actual implementation status belongs in a status document. Do not use this document to pre-answer every later question.

## Current starting point

The seed implementation starts with Python and uv, explicit `.j2` templates, and semantic drift. Controlled reverse sync and an rbw provider are product exploration directions and future hypotheses, not current capabilities. This reflects the current preference for fast iteration and a manageable, reviewable dependency supply chain; language choice is not treated as an intrinsic security guarantee, and cross-platform support remains a validation target rather than a completed matrix. These are starting assumptions for exploration, not the product's final form.
