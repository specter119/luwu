---
name: luwu-confidentiality-review
description: >
  Use when reviewing Luwu secrets, providers, template inputs, logs, JSON
  output, persistence, caches, diffs, subprocesses, network access, or debug
  surfaces. Test whether capability and information exposure remain minimal and
  fail closed. Do not use for generic authentication or style review unrelated
  to Luwu data flow.
---

# Luwu confidentiality review

Follow sensitive values and capabilities through their entire lifetime. The
safe result is not only protected storage: secrets and rendered values should
avoid the repository, persistent state, diffs, logs, caches, backups, and
machine-readable output unless a contract explicitly requires otherwise.

Trace manifest inputs, provider reads, rendering, exceptions, plans, JSON,
temporary files, persistence, and external calls. Ask:

- Can a secret or rendered value appear in output, error text, metadata, or a
  file created during apply?
- Are network, subprocess, loader, and provider capabilities explicit rather
  than silently available?
- Does failure reveal less, and does an unavailable provider fail closed?
- Is debug visibility worth the new exposure, or can metadata explain the state?

Attack verbose diffs, exception reprs, serialized plans, cache and backup
paths, template globals, environment leakage, implicit network/subprocess
access, and “temporary” secret persistence. Never weaken a security boundary
just to make a UI or workflow convenient.

Report the value flow, exposure boundary, path/line and test evidence, rejected
diagnostic convenience, minimum fix, and regression test. Classify implemented,
partial, design-only, or unstarted. Use the owning reference and provider
contract for exact secrecy promises.
