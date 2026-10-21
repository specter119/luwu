#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Ablate version 8 profile boundaries through the production modules and real writer.

The expected values and bytes below are specified independently of the
production parser, renderer, and writer. Counterexamples model the removed
shortcut only; the experiment never includes a replacement parser or writer.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import socket
import stat
import sys
import sysconfig
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

MODULES = (
    "luwu",
    "luwu.manifest",
    "luwu.baseline",
    "luwu.formats",
    "luwu.filesystem",
    "luwu.inputs",
    "luwu.engine",
    "luwu.plan_evidence",
    "luwu.providers",
)

_PROVIDER_VALUE = "ablation-provider-value-4d21"
_PROVIDER_PLACEHOLDER = "ablation-placeholder-1c0e"
_PROVIDER_ITEM = "ablation-item"

# A minimal local stand-in for the vault: one `unlocked` probe and one `get`
# that returns a public synthetic document. The full fault-injection fake with
# its canary constants lives in tests/support/fake_rbw.py; this experiment
# stays standalone so it can run without the test tree.
_FAKE_RBW = """#!/bin/sh
record={record}
payload={payload}
printf '%s\\n' "${{1-}}" >> "$record"
case "${{1-}}" in
  unlocked)
    exit 0
    ;;
  get)
    cat "$payload/get.stdout"
    exit 0
    ;;
esac
exit 9
"""


def _sh_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def _tuple_path_case(formats: Any) -> dict[str, object]:
    document = formats.parse_document(
        b'{"a.b":{"trust":"literal-dot"},"a":{"b":{"trust":"nested"}}}',
        format="json",
    )
    literal = formats.get_leaf(document, ("a.b", "trust"))
    nested = formats.get_leaf(document, ("a", "b", "trust"))
    flattened_literal = ".".join(("a.b", "trust"))  # noqa: FLY002 - counterexample under test
    flattened_nested = ".".join(("a", "b", "trust"))  # noqa: FLY002 - counterexample under test
    assert literal == "literal-dot"
    assert nested == "nested"
    assert flattened_literal == flattened_nested
    return {
        "status": "pass",
        "tuple_paths_distinct": literal != nested,
        "dot_join_counterexample_collides": flattened_literal == flattened_nested,
    }


def _unknown_projection_case(formats: Any) -> dict[str, object]:
    original = formats.parse_document(
        b'{"chosen":1,"unknown":{"nested":[1,2],"empty":{}},"empty":{},"array":[]}',
        format="json",
    )
    selected_only_change = formats.parse_document(
        b'{"chosen":3,"unknown":{"nested":[1,2],"empty":{}},"empty":{},"array":[]}',
        format="json",
    )
    assert formats.unselected_equal(original, selected_only_change, {("chosen",)})

    counterexamples = {
        "nested_value": b'{"chosen":3,"unknown":{"nested":[1,9],"empty":{}},"empty":{},"array":[]}',
        "unknown_empty_container": b'{"chosen":3,"unknown":{"nested":[1,2]},"empty":{},"array":[]}',
        "top_empty_container": b'{"chosen":3,"unknown":{"nested":[1,2],"empty":{}},"array":[]}',
        "array_membership": b'{"chosen":3,"unknown":{"nested":[1,2],"empty":{}},"empty":{},"array":[0]}',
        "unknown_type_change": b'{"chosen":3,"unknown":[1,2],"empty":{},"array":[]}',
    }
    for label, data in counterexamples.items():
        changed = formats.parse_document(data, format="json")
        assert not formats.unselected_equal(original, changed, {("chosen",)}), label
    return {
        "status": "pass",
        "selected_change_ignored": True,
        "unknown_changes_rejected": sorted(counterexamples),
        "unknown_containers_arrays_and_empty_values_observed": True,
    }


def _span_vs_dump_case(formats: Any) -> dict[str, object]:
    source = (
        b"{\r\n"
        b"  // retain this comment\r\n"
        b'  "selected": "old",\r\n'
        b'  "unknown": [1, 2]\r\n'
        b"}\r\n"
    )
    expected = (
        b"{\r\n"
        b"  // retain this comment\r\n"
        b'  "selected": "new",\r\n'
        b'  "unknown": [1, 2]\r\n'
        b"}\r\n"
    )
    document = formats.parse_document(source, format="jsonc")
    actual = formats.patch_document(document, {("selected",): "new"})
    assert actual == expected
    reserialized = (json.dumps(document.value, indent=2) + "\n").encode("utf-8")
    assert reserialized != expected
    assert json.loads(
        actual.decode("utf-8").replace("// retain this comment\r\n", "")
    ) == {
        "selected": "new",
        "unknown": [1, 2],
    }
    return {
        "status": "pass",
        "production_patch_matches_independent_bytes": True,
        "whole_document_dump_differs": True,
        "comment_and_crlf_preserved": True,
    }


def _resource_table(
    name: str, *, kind: str, source: str, target: str, baseline: str
) -> str:
    deploy_line = 'deploy = "copy"\n' if kind == "copy" else ""
    common = (
        f"{deploy_line}source = {json.dumps(source)}\n"
        f"target = {json.dumps(target)}\n"
        f"baseline = {json.dumps(baseline)}\n"
        'content_sensitivity = "public"\n'
        'parents = "require"\n'
        'parent_mode = "0700"\n'
    )
    if kind == "copy":
        return f"[resources.{name}]\n{common}"
    return (
        f"[resources.{name}]\n{common}"
        'format = "json"\n'
        f'[resources.{name}.fields.value]\npath = ["value"]\nowner = "source"\n'
    )


def _manifest_text(
    *,
    source_roots: tuple[str, ...],
    package_names: tuple[str, ...],
    package_tables: str,
    resource_tables: str,
    excludes: tuple[str, ...] = (),
) -> str:
    exclude_text = f"\nexclude = [{', '.join(json.dumps(item) for item in excludes)}]"
    return (
        "version = 8\n"
        '\n[roots]\nsource = "source"\ninputs = "inputs"\ntarget = "targets"\n'
        f"\n[profiles.bench]\npackages = [{', '.join(json.dumps(item) for item in package_names)}]"
        f"{exclude_text}\n"
        f"\n{package_tables}\n"
        f"\n[inputs]\n\n{resource_tables}"
    )


def _directory_membership_case(manifest_module: Any) -> dict[str, object]:
    from luwu.errors import ManifestError

    with tempfile.TemporaryDirectory(prefix="luwu-test-dir-ablation-") as directory:
        root = Path(directory)
        source = root / "source"
        inputs = root / "inputs"
        target = root / "targets"
        tree = source / "tree"
        for path in (tree, inputs / "baselines", target / "tree"):
            path.mkdir(parents=True, exist_ok=True)
        (tree / "first.txt").write_text("public-one\n", encoding="utf-8")
        resource = (
            "[resources.bundle]\n"
            'deploy = "copy"\nsource = "tree"\ntarget = "tree"\n'
            'baseline = "baselines/tree"\ncontent_sensitivity = "public"\n'
            'parents = "require"\nparent_mode = "0700"\n'
        )
        package = '[packages.files]\ndependencies = []\nresources = ["bundle"]'
        manifest_path = root / "luwu.toml"
        manifest_path.write_text(
            _manifest_text(
                source_roots=("tree",),
                package_names=("files",),
                package_tables=package,
                resource_tables=resource,
            ),
            encoding="utf-8",
        )
        loaded = manifest_module.load_manifest(manifest_path, profile="bench")
        assert tuple(item.name for item in loaded.resources) == ("bundle:first.txt",)
        (tree / "second.txt").write_text("public-two\n", encoding="utf-8")
        try:
            manifest_module.verify_manifest(loaded)
        except ManifestError as error:
            assert error.code == "stale"
        else:
            raise AssertionError(
                "directory membership change did not stale the loaded manifest"
            )
        refreshed = manifest_module.load_manifest(manifest_path, profile="bench")
        assert tuple(item.name for item in refreshed.resources) == (
            "bundle:first.txt",
            "bundle:second.txt",
        )
    return {
        "status": "pass",
        "initial_members": 1,
        "membership_change_invalidated_snapshot": True,
        "refreshed_members": 2,
    }


def _dependency_exclusion_case(manifest_module: Any) -> dict[str, object]:
    from luwu.errors import ManifestError

    with tempfile.TemporaryDirectory(prefix="luwu-test-exclude-ablation-") as directory:
        root = Path(directory)
        for path in (root / "source", root / "inputs", root / "targets"):
            path.mkdir()
        (root / "source" / "app.json.j2").write_bytes(b'{"value":"public-app"}\n')
        resource_tables = (
            _resource_table(
                "app_resource",
                kind="template",
                source="app.json.j2",
                target="app.json",
                baseline="baselines/app.json",
            )
            + "\n"
            + _resource_table(
                "excluded_resource",
                kind="copy",
                source="does-not-exist.txt",
                target="excluded.txt",
                baseline="baselines/excluded.json",
            )
        )
        package_tables = (
            '[packages.base]\ndependencies = []\nresources = ["excluded_resource"]\n'
            '[packages.app]\ndependencies = ["base"]\nresources = ["app_resource"]'
        )
        manifest_path = root / "luwu.toml"
        manifest_path.write_text(
            _manifest_text(
                source_roots=("app.json.j2",),
                package_names=("app",),
                package_tables=package_tables,
                resource_tables=resource_tables,
                excludes=("base",),
            ),
            encoding="utf-8",
        )
        try:
            manifest_module.load_manifest(manifest_path, profile="bench")
        except ManifestError as error:
            assert error.code == "package_excluded_dependency"
        else:
            raise AssertionError(
                "selected package silently lost an excluded dependency"
            )
        assert not (root / "source" / "does-not-exist.txt").exists()
    return {
        "status": "pass",
        "required_dependency_exclusion_rejected": True,
        "excluded_resource_source_remained_unobserved": True,
    }


_ACTION_FIELDS = {"kind", "resource", "path", "state", "fields"}


def _action_rows(payload: object) -> dict[str, dict[str, object]]:
    if not isinstance(payload, dict):
        raise TypeError("partial apply result is not a metadata object")
    actions = payload.get("actions")
    if not isinstance(actions, list):
        raise TypeError("production result has no schema-8 actions list")
    rows: dict[str, dict[str, object]] = {}
    for row in actions:
        if not isinstance(row, dict) or not _ACTION_FIELDS <= set(row):
            raise AssertionError("production action does not match schema-8 fields")
        if row.get("kind") != "target_write":
            continue
        resource = row.get("resource")
        if not isinstance(resource, str) or resource in rows:
            raise AssertionError("production result has an invalid or duplicate action")
        if not isinstance(row.get("state"), str):
            raise TypeError("production action has no string state")
        rows[resource] = row
    return rows


def _assert_partial_action_states(outcomes: dict[str, str]) -> None:
    expected = {
        "first": "committed",
        "second": "not_replaced",
        "third": "not_attempted",
    }
    if outcomes != expected:
        raise AssertionError(
            f"partial apply action states mismatch: got {outcomes}, expected {expected}"
        )


def _partial_outcome_case(
    manifest_module: Any, baseline_module: Any, filesystem_module: Any, engine: Any
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="luwu-test-partial-ablation-") as directory:
        root = Path(directory)
        source_root = root / "source"
        inputs_root = root / "inputs"
        target_root = root / "targets"
        for path in (source_root, inputs_root, target_root, inputs_root / "baselines"):
            path.mkdir(parents=True, exist_ok=True)
        resources_text: list[str] = []
        expected: dict[str, bytes] = {}
        names = ("first", "second", "third")
        expected_target_mode = 0o644
        for name in names:
            data = f"public-{name}\n".encode()
            source = source_root / f"{name}.txt"
            source.write_bytes(data)
            source.chmod(expected_target_mode)
            expected[name] = data
            resources_text.append(
                _resource_table(
                    name,
                    kind="copy",
                    source=f"{name}.txt",
                    target=f"{name}.txt",
                    baseline=f"baselines/{name}.json",
                )
            )
        package = '[packages.work]\ndependencies = []\nresources = ["first", "second", "third"]'
        manifest_path = root / "luwu.toml"
        manifest_path.write_text(
            _manifest_text(
                source_roots=(),
                package_names=("work",),
                package_tables=package,
                resource_tables="\n".join(resources_text),
            ),
            encoding="utf-8",
        )
        manifest = manifest_module.load_manifest(manifest_path, profile="bench")
        resource_by_name = {resource.name: resource for resource in manifest.resources}
        for name in names:
            baseline = baseline_module.make_copy_baseline(
                manifest,
                resource_by_name[name],
                accepted_data=expected[name],
                accepted_mode=expected_target_mode,
            )
            path = resource_by_name[name].baseline
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(baseline)
            path.chmod(0o600)
        manifest = manifest_module.load_manifest(manifest_path, profile="bench")
        plan = engine.build_plan(manifest)

        real_writer = filesystem_module.write_file
        fail_target = target_root / "second.txt"
        injected = {"count": 0}

        def fail_second_target(*args: object, **kwargs: object) -> tuple[int, int]:
            path_value = args[1] if len(args) > 1 else kwargs.get("path")
            if isinstance(path_value, Path) and path_value == fail_target:
                injected["count"] += 1
                raise filesystem_module.FilesystemError(
                    "injected pre-replacement failure",
                    code="write_failed",
                    state="not_replaced",
                    committed=False,
                )
            return real_writer(*args, **kwargs)

        patches = [patch.object(filesystem_module, "write_file", fail_second_target)]
        for name, value in vars(engine).items():
            if value is real_writer:
                patches.append(patch.object(engine, name, fail_second_target))
        for item in patches:
            item.start()
        try:
            result = engine.apply_plan(plan, confirm=True)
        finally:
            for item in reversed(patches):
                item.stop()
        payload = result.to_dict()
        rows = _action_rows(payload)
        if set(rows) != set(names):
            raise AssertionError("partial apply result omitted or invented a resource")
        outcomes = {name: row["state"] for name, row in rows.items()}
        if not all(isinstance(state, str) for state in outcomes.values()):
            raise AssertionError("production action has no string state")
        if injected["count"] == 0:
            if payload.get("outcome") != "partial" or outcomes["first"] != "committed":
                raise AssertionError(
                    "the early failure did not preserve the committed first action"
                )
            if (
                outcomes["second"] != "not_attempted"
                or outcomes["third"] != "not_attempted"
            ):
                raise AssertionError("the early failure mislabeled later actions")
            if (target_root / "first.txt").read_bytes() != expected["first"]:
                raise AssertionError(
                    "the first target does not match the fixture after partial apply"
                )
            if (
                stat.S_IMODE((target_root / "first.txt").stat().st_mode)
                != expected_target_mode
            ):
                raise AssertionError(
                    "the first target mode does not match the independent fixture"
                )
            if (target_root / "second.txt").exists() or (
                target_root / "third.txt"
            ).exists():
                raise AssertionError(
                    "later targets were written after an early failure"
                )
            return {
                "status": "fail",
                "error": "plan freshness stopped the batch before the injected second-target failure",
                "error_code": payload.get("error_code"),
                "injected_failure_reached": False,
                "outcome": payload.get("outcome"),
                "action_states": outcomes,
                "first_target_committed": True,
                "later_targets_absent": True,
                "first_target_mode_matches_fixture": True,
            }
        if injected["count"] != 1:
            raise AssertionError(
                "the production writer was intercepted an unexpected number of times"
            )
        if (
            payload.get("outcome") != "partial"
            or payload.get("error_code") != "write_failed"
        ):
            raise AssertionError(
                "the injected pre-replacement failure was not reported as partial"
            )
        _assert_partial_action_states(outcomes)
        try:
            _assert_partial_action_states({**outcomes, "second": "not_attempted"})
        except AssertionError:
            pass
        else:
            raise AssertionError(
                "partial outcome oracle accepted not_attempted for the failed second action"
            )
        if (
            not (target_root / "first.txt").is_file()
            or (target_root / "first.txt").read_bytes() != expected["first"]
        ):
            raise AssertionError(
                "first committed target does not match its independent fixture"
            )
        if (
            stat.S_IMODE((target_root / "first.txt").stat().st_mode)
            != expected_target_mode
        ):
            raise AssertionError(
                "the first target mode does not match the independent fixture"
            )
        if (target_root / "second.txt").exists() or (
            target_root / "third.txt"
        ).exists():
            raise AssertionError("failed or unattempted targets were written")
        return {
            "status": "pass",
            "outcome": payload.get("outcome"),
            "error_code": payload.get("error_code"),
            "outcomes": outcomes,
            "writer_failure_injected_once": injected["count"] == 1,
            "not_attempted_negative_control_rejected": True,
            "first_target_committed": True,
            "failed_and_unattempted_targets_absent": True,
            "first_target_mode_matches_fixture": True,
        }


_PROVIDER_TEMPLATE = (
    '{"service":{"apiKey":"' + _PROVIDER_PLACEHOLDER + '","user":"ablation-user"}}\n'
)
_PROVIDER_REVERSE_TEMPLATE = (
    '{"service":{"apiKey":"'
    + _PROVIDER_PLACEHOLDER
    + '","user":"ablation-user","theme":{{ inputs.theme | tojson }}}}\n'
)


def _provider_fixture(root: Path) -> dict[str, object]:
    """A private fake rbw tree, its call record, and one bound agent socket."""

    bin_dir = root / "provider-bin"
    payload = root / "provider-payload"
    runtime = root / "provider-runtime"
    for directory in (bin_dir, payload, runtime / "rbw"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    (payload / "get.stdout").write_bytes(
        json.dumps(
            {
                "name": _PROVIDER_ITEM,
                "data": {"password": _PROVIDER_VALUE, "username": "ablation-user"},
                "fields": [{"name": "apiKey", "value": _PROVIDER_VALUE}],
            }
        ).encode("utf-8")
    )
    record = root / "provider-record.log"
    executable = bin_dir / "rbw"
    executable.write_text(
        _FAKE_RBW.format(record=_sh_quote(record), payload=_sh_quote(payload)),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    agent.bind(str(runtime / "rbw" / "socket"))
    return {
        "executable": executable,
        "environ": {
            "HOME": str(root / "provider-home"),
            "XDG_RUNTIME_DIR": str(runtime),
        },
        "record": record,
        "socket": agent,
    }


def _provider_calls(record: Path) -> list[str]:
    if not record.exists():
        return []
    return [line for line in record.read_text(encoding="utf-8").splitlines() if line]


def _write_provider_manifest(
    root: Path,
    *,
    resources: tuple[str, ...] = ("app",),
    public_field: bool = True,
    reverse_field: bool = False,
) -> Path:
    source = root / "source"
    for directory in (
        source / "templates",
        root / "inputs" / "baselines",
        root / "target",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for name in resources:
        (source / "templates" / f"{name}.json.j2").write_text(
            _PROVIDER_REVERSE_TEMPLATE if reverse_field else _PROVIDER_TEMPLATE,
            encoding="utf-8",
        )
        blocks.append(
            f"""
[resources.{name}]
source = "templates/{name}.json.j2"
target = ".config/{name}.json"
format = "json"
baseline = "baselines/{name}.json"
parents = "create"
parent_mode = "0700"
mode = 0o600
"""
        )
        if public_field:
            blocks.append(
                f"""
[resources.{name}.fields.user]
path = ["service", "user"]
owner = "source"
"""
            )
        if reverse_field:
            blocks.append(
                f"""
[resources.{name}.fields.theme]
path = ["service", "theme"]
owner = "merge"
input = "theme"
"""
            )
        blocks.append(
            f"""
[resources.{name}.fields.apiKey]
path = ["service", "apiKey"]
owner = "provider"
provider = "db"
"""
        )
    inputs_block = ""
    if reverse_field:
        local = root / "inputs" / "local"
        local.mkdir(parents=True, exist_ok=True)
        (local / "theme.json").write_text(
            json.dumps({"theme": "ablation-theme"}) + "\n", encoding="utf-8"
        )
        inputs_block = (
            '\n[inputs.theme]\nlayers = [{ file = "local/theme.json", '
            'path = ["theme"], format = "json" }]\n'
            'reverse_to = { file = "local/theme.json", path = ["theme"], '
            'format = "json" }\n'
        )
    manifest_path = root / "luwu.toml"
    manifest_path.write_text(
        "version = 8\n"
        '\n[roots]\nsource = "source"\ninputs = "inputs"\ntarget = "target"\n'
        '\n[profiles.work]\npackages = ["app"]\nexclude = []\n'
        f"\n[packages.app]\ndependencies = []\nresources = ["
        f"{', '.join(json.dumps(name) for name in resources)}]\n"
        "\n[providers.db]\n"
        'type = "rbw"\n'
        f'item = "{_PROVIDER_ITEM}"\n'
        'field = "apiKey"\n'
        f"{inputs_block}"
        f"\n{''.join(blocks)}",
        encoding="utf-8",
    )
    return manifest_path


def _provider_profile(
    root: Path,
    *,
    resources: tuple[str, ...] = ("app",),
    public_field: bool = True,
    reverse_field: bool = False,
) -> dict[str, object]:
    manifest_path = _write_provider_manifest(
        root,
        resources=resources,
        public_field=public_field,
        reverse_field=reverse_field,
    )
    fixture = _provider_fixture(root)
    fixture["manifest"] = manifest_path
    return fixture


def _provider_authority(fixture: dict[str, object]) -> object:
    from luwu.providers import ProviderAuthority

    return ProviderAuthority(rbw_executable=fixture["executable"])


def _provider_environ(fixture: dict[str, object]) -> dict[str, str]:
    environ = fixture["environ"]
    assert isinstance(environ, dict)
    return environ


def _provider_record(fixture: dict[str, object]) -> Path:
    record = fixture["record"]
    assert isinstance(record, Path)
    return record


def _provider_single_fetch_case(manifest_module: Any, engine: Any) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="luwu-test-provider-ablation-"
    ) as directory:
        root = Path(directory)
        fixture = _provider_profile(
            root, resources=("app", "extra"), public_field=False
        )
        record = _provider_record(fixture)
        try:
            with patch.dict(os.environ, _provider_environ(fixture)):
                manifest = manifest_module.load_manifest(
                    fixture["manifest"], profile="work"
                )
                authority = _provider_authority(fixture)
                plan = engine.build_plan(manifest, provider_authority=authority)
                planned = _provider_calls(record)
                for item in plan.to_dict()["resources"]:
                    assert item["providers"] == [
                        {
                            "provider": "db",
                            "item": _PROVIDER_ITEM,
                            "field": "apiKey",
                            "state": "ok",
                        }
                    ], item
                # Plan evidence now binds provider bytes in memory, so the
                # printable surface is what must stay clean.
                plan_record = engine._PLAN_REGISTRY.get(plan)
                assert plan_record is not None
                resolution = plan_record.provider_state
                assert resolution is not None
                digest = hashlib.sha256(_PROVIDER_VALUE.encode()).hexdigest()
                printed = (
                    plan,
                    plan_record,
                    plan_record.resources[0],
                    resolution,
                    resolution.values,
                )
                for value in printed:
                    for rendered in (repr(value), str(value)):
                        if _PROVIDER_VALUE in rendered or digest in rendered:
                            raise AssertionError(
                                f"{type(value).__name__} printed the provider value"
                            )
                mutation = engine.build_mutation_plan(
                    manifest, provider_authority=authority
                )
                confirmed = _provider_calls(record)
                result = engine.apply_plan(mutation, confirm=True)
                applied = _provider_calls(record)
        finally:
            fixture["socket"].close()
        if planned != ["unlocked", "get"]:
            raise AssertionError(f"one profile fetch expected, got {planned}")
        if confirmed != ["unlocked", "get", "unlocked", "get"]:
            raise AssertionError(f"one mutation-plan fetch expected, got {confirmed}")
        if applied != confirmed:
            raise AssertionError("apply fetched the provider again")
        if result.outcome != "committed":
            raise AssertionError(f"apply outcome was {result.outcome}")
        for name in ("app", "extra"):
            data = (root / "target/.config" / f"{name}.json").read_bytes()
            if _PROVIDER_VALUE.encode() not in data:
                raise AssertionError(f"target {name} lacks the provider value")
        return {
            "status": "pass",
            "profile_fetches": 1,
            "mutation_plan_fetches": 1,
            "apply_fetched_again": False,
            "printable_surface_is_clean": True,
            "targets_written": 2,
        }


def _provider_baseline_exclusion_case(
    manifest_module: Any, engine: Any
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="luwu-test-provider-ablation-"
    ) as directory:
        root = Path(directory)
        fixture = _provider_profile(root)
        try:
            with patch.dict(os.environ, _provider_environ(fixture)):
                manifest = manifest_module.load_manifest(
                    fixture["manifest"], profile="work"
                )
                authority = _provider_authority(fixture)
                plan = engine.build_plan(manifest, provider_authority=authority)
                accepted = engine.accept_baseline(
                    plan,
                    resource="app",
                    fields=("user",),
                    value_from="desired",
                    confirm=True,
                )
                result = engine.apply_plan(
                    engine.build_mutation_plan(manifest, provider_authority=authority),
                    confirm=True,
                )
        finally:
            fixture["socket"].close()
        if accepted.outcome != "committed" or result.outcome != "committed":
            raise AssertionError("the public field was not accepted and applied")
        baseline = (root / "inputs/baselines/app.json").read_bytes()
        if _PROVIDER_VALUE.encode() in baseline:
            raise AssertionError("the baseline stored the provider value")
        digest = hashlib.sha256(_PROVIDER_VALUE.encode()).hexdigest().encode()
        if digest in baseline:
            raise AssertionError("the baseline stored the provider digest")
        values = json.loads(baseline)["values"]
        if sorted(values) != ["user"]:
            raise AssertionError(f"the baseline values are {sorted(values)}")
        target = (root / "target/.config/app.json").read_bytes()
        if _PROVIDER_VALUE.encode() not in target:
            raise AssertionError("the applied target lacks the provider value")
        return {
            "status": "pass",
            "baseline_values": sorted(values),
            "baseline_holds_value_or_digest": False,
            "target_holds_value": True,
        }


def _provider_nonfetching_case(manifest_module: Any, engine: Any) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="luwu-test-provider-ablation-"
    ) as directory:
        root = Path(directory)
        fixture = _provider_profile(root, reverse_field=True)
        record = _provider_record(fixture)
        target = root / "target/.config/app.json"
        try:
            with patch.dict(os.environ, _provider_environ(fixture)):
                manifest = manifest_module.load_manifest(
                    fixture["manifest"], profile="work"
                )
                observation = engine.build_plan(manifest, fetch_providers=False)
                item = observation.to_dict()["resources"][0]
                if item["providers"][0]["state"] != "not_fetched":
                    raise AssertionError("a non-fetching plan contacted the provider")
                if item["status"] != "unbased":
                    raise AssertionError(
                        f"non-fetching classification was {item['status']}"
                    )
                mutation = engine.build_mutation_plan(manifest, fetch_providers=False)
                try:
                    engine.apply_plan(mutation, confirm=True)
                except engine.EngineError as error:
                    if error.code != "plan_invalid":
                        raise AssertionError(
                            f"non-fetching apply failed with {error.code}"
                        ) from None
                else:
                    raise AssertionError("a non-fetching plan drove a target write")
                # accept runs on the same non-fetching plan the CLI builds.
                accept_plan = engine.build_mutation_plan(
                    manifest, fetch_providers=False
                )
                accepted = engine.accept_baseline(
                    accept_plan,
                    resource="app",
                    fields=("theme", "user"),
                    value_from="desired",
                    confirm=True,
                )
                if accepted.outcome != "committed":
                    raise AssertionError("accept did not commit")
                if _provider_calls(record):
                    raise AssertionError("accept ran the provider")
                # One fetch deploys the profile so a live-owned field exists;
                # the reverse-sync that follows must not fetch.
                deployed = engine.apply_plan(
                    engine.build_mutation_plan(
                        manifest, provider_authority=_provider_authority(fixture)
                    ),
                    confirm=True,
                )
                if deployed.outcome != "committed":
                    raise AssertionError("the deployment did not commit")
                fetches = _provider_calls(record)
                document = json.loads(target.read_text(encoding="utf-8"))
                document["service"]["theme"] = "live-theme-choice"
                target.write_text(json.dumps(document) + "\n", encoding="utf-8")
                reverse_plan = engine.build_mutation_plan(
                    manifest, fetch_providers=False
                )
                reversed_ = engine.reverse_inputs(
                    reverse_plan, resource="app", fields=("theme",), confirm=True
                )
        finally:
            fixture["socket"].close()
        if reversed_.outcome != "committed":
            raise AssertionError("reverse-sync did not commit")
        calls = _provider_calls(record)
        if calls != fetches:
            raise AssertionError(f"reverse-sync ran the provider: {calls}")
        if (root / "target/.config/extra.json").exists():
            raise AssertionError("a non-fetching plan wrote an unrelated target")
        return {
            "status": "pass",
            "non_fetching_state": "not_fetched",
            "apply_refused_with": "plan_invalid",
            "accept_and_reverse_fetched": False,
            "target_absent_before_the_deploy": True,
        }


def _provider_mode_enforced_case(
    manifest_module: Any, engine: Any
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="luwu-test-provider-ablation-"
    ) as directory:
        root = Path(directory)
        fixture = _provider_profile(root, public_field=False)
        target = root / "target/.config/app.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            json.dumps(
                {
                    "service": {
                        "apiKey": _PROVIDER_VALUE,
                        "user": "ablation-user",
                    }
                }
            ).encode("utf-8")
        )
        target.chmod(0o644)
        before = target.read_bytes()
        try:
            with patch.dict(os.environ, _provider_environ(fixture)):
                manifest = manifest_module.load_manifest(
                    fixture["manifest"], profile="work"
                )
                authority = _provider_authority(fixture)
                plan = engine.build_plan(manifest, provider_authority=authority)
                item = plan.to_dict()["resources"][0]
                if (item["status"], item["action"]) != ("drifted", "forward"):
                    raise AssertionError(
                        f"a wide live mode planned {item['status']}/{item['action']}"
                    )
                if item["forward_fields"] != ["mode"]:
                    raise AssertionError(
                        f"forward fields were {item['forward_fields']}"
                    )
                result = engine.apply_plan(
                    engine.build_mutation_plan(manifest, provider_authority=authority),
                    confirm=True,
                )
        finally:
            fixture["socket"].close()
        if result.outcome != "committed":
            raise AssertionError(f"the mode-only apply was {result.outcome}")
        mode = stat.S_IMODE(target.stat().st_mode)
        if mode != 0o600:
            raise AssertionError(f"the live mode stayed {mode:04o}")
        if target.read_bytes() != before:
            raise AssertionError("a mode-only forward rewrote the target bytes")
        return {
            "status": "pass",
            "planned": "drifted/forward",
            "forward_fields": ["mode"],
            "mode_after_apply": "0600",
            "bytes_unchanged": True,
        }


def _current_runtime(source_root: Path, luwu_module: Any) -> dict[str, object]:
    modules: dict[str, dict[str, str]] = {}
    for name in MODULES:
        module = sys.modules.get(name)
        module_path = getattr(module, "__file__", None)
        if not isinstance(module_path, str):
            raise TypeError(f"execution process did not load module {name}")
        path = Path(module_path).resolve()
        try:
            path.relative_to(source_root.resolve())
        except ValueError as error:
            raise RuntimeError(
                f"execution process imported {name} outside the source checkout"
            ) from error
        modules[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {
        "python_version": sys.version,
        "python_prefix": sys.prefix,
        "python_site_packages": str(Path(sysconfig.get_path("purelib")).resolve()),
        "luwu_version": luwu_module.__version__,
        "modules": modules,
    }


def _run_case(function: Callable[..., object], *args: object) -> dict[str, object]:
    try:
        result = function(*args)
    except Exception as error:  # noqa: BLE001 - retain each independent case result
        return {
            "status": "fail",
            "error_type": type(error).__name__,
            "error_code": getattr(error, "code", None),
            "error": str(error),
        }
    if isinstance(result, dict) and result.get("status") in {"pass", "fail"}:
        return result
    if not isinstance(result, dict):
        return {"status": "fail", "error": "case did not return a passing result"}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    python = args.python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("--python must name an executable file")
    script_args = sys.argv[1:] if argv is None else argv
    if python != Path(sys.executable).absolute():
        os.execv(
            str(python),
            [str(python), str(Path(__file__).resolve()), *script_args],
        )
    if python != Path(sys.executable).absolute():
        parser.error("execution did not enter the requested Python interpreter")
    source_root = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source_root))
    try:
        for name in MODULES:
            importlib.import_module(name)
    except ImportError as error:
        parser.error(
            f"profile production runtime is incomplete; engine ablation is pending: {error}"
        )
    from luwu import (
        baseline as baseline_module,
    )
    from luwu import (
        engine as engine_module,
    )
    from luwu import (
        filesystem as filesystem_module,
    )
    from luwu import (
        formats as formats_module,
    )
    from luwu import (
        manifest as manifest_module,
    )

    cases = {
        "tuplepath_vs_dotcollision": _run_case(_tuple_path_case, formats_module),
        "unknown_projection": _run_case(_unknown_projection_case, formats_module),
        "span_vs_dump": _run_case(_span_vs_dump_case, formats_module),
        "directory_membership": _run_case(_directory_membership_case, manifest_module),
        "dependency_exclusion": _run_case(_dependency_exclusion_case, manifest_module),
        "partial_outcome": _run_case(
            _partial_outcome_case,
            manifest_module,
            baseline_module,
            filesystem_module,
            engine_module,
        ),
        "provider_single_fetch": _run_case(
            _provider_single_fetch_case, manifest_module, engine_module
        ),
        "provider_baseline_exclusion": _run_case(
            _provider_baseline_exclusion_case, manifest_module, engine_module
        ),
        "provider_nonfetching_apply": _run_case(
            _provider_nonfetching_case, manifest_module, engine_module
        ),
        "provider_mode_enforced": _run_case(
            _provider_mode_enforced_case, manifest_module, engine_module
        ),
    }
    current_runtime = _current_runtime(source_root, sys.modules["luwu"])
    source_modules = current_runtime["modules"]
    if not isinstance(source_modules, dict):
        parser.error("execution process returned no module hashes")
    report = {
        "experiment": "profile_ablation",
        "execution_mode": "source_only",
        "status": "pass"
        if all(case.get("status") == "pass" for case in cases.values())
        else "fail",
        "python": {
            "requested_executable": str(python),
            "executable": sys.executable,
            "version": current_runtime["python_version"],
            "prefix": current_runtime["python_prefix"],
            "site_packages": current_runtime["python_site_packages"],
            "luwu_version": current_runtime["luwu_version"],
            "imports": source_modules,
        },
        "environment": {
            "platform": sys.platform,
            "platform_release": platform.platform(),
            "sandbox_mode": os.environ.get("LUWU_SANDBOX_MODE", "unspecified"),
        },
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": cases,
    }
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
