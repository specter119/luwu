from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Self

from luwu.manifest import load_manifest


class FieldProject:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.baseline = self.root / "baseline.json"
        self.manifest = self.root / "luwu.toml"
        self.source.write_text(
            '{"setting": 1, "runtime": 1, "undeclared": "source"}',
            encoding="utf-8",
        )
        self.target.write_text(
            '{"setting": 1, "runtime": 2, "undeclared": "live"}',
            encoding="utf-8",
        )
        self.manifest.write_text(
            """version = 4

[resources.settings]
kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
baseline = "baseline.json"

[resources.settings.fields]
setting = "source"
runtime = "live"
undeclared = "ignore"

[resources.settings.reverse_sync]
format = "literal-json"

[resources.settings.reverse_sync.fields]
runtime = "runtime"
""",
            encoding="utf-8",
        )

    def write_baseline(self, values: dict[str, object]) -> None:
        self.baseline.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "resource": "settings",
                    "source": "templates/settings.json.j2",
                    "target": "live/settings.json",
                    "owners": {
                        "setting": "source",
                        "runtime": "live",
                        "undeclared": "ignore",
                    },
                    "values": values,
                }
            ),
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


class ExecutionProject:
    def __init__(
        self, names: tuple[str, ...], *, targets: dict[str, str | None]
    ) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sources").mkdir()
        (self.root / "targets").mkdir()
        self.sources: dict[str, Path] = {}
        self.targets: dict[str, Path] = {}
        self.source_values: dict[str, str] = {}
        for name in names:
            value = f"{name}-desired\n"
            source = self.root / "sources" / f"{name}.conf.j2"
            target = self.root / "targets" / f"{name}.conf"
            source.write_text(value)
            self.sources[name] = source
            self.targets[name] = target
            self.source_values[name] = value
            target_value = targets[name]
            if target_value is not None:
                target.write_text(target_value)

        resources = []
        for name in reversed(names):
            resources.append(
                f"""[resources.{name}]
kind = "template"
source = "sources/{name}.conf.j2"
target = "targets/{name}.conf"
owner = "source"
scope = "whole-file"
content_sensitivity = "public"
"""
            )
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text("version = 5\n\n" + "\n".join(resources))
        self.manifest = load_manifest(self.manifest_path)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


class LegacyCliProject:
    def __init__(self, version: int) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source_value = '{"value": 2}\n'
        self.target_value = '{"value": 1}\n'
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.manifest_path = self.root / "luwu.toml"
        self.source.write_text(self.source_value, encoding="utf-8")
        self.target.write_text(self.target_value, encoding="utf-8")
        if version == 1:
            resource = """source = "templates/settings.json.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
"""
        elif version == 2:
            resource = """kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "source"
scope = "whole-file"
"""
        else:
            field_owner = "source" if version == 3 else "live"
            reverse_sync = (
                "\n[resources.settings.reverse_sync]\n"
                'format = "literal-json"\n\n'
                "[resources.settings.reverse_sync.fields]\n"
                'value = "value"\n'
                if version == 4
                else ""
            )
            resource = f'''kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
baseline = "baseline.json"

[resources.settings.fields]
value = "{field_owner}"
{reverse_sync}'''
            (self.root / "baseline.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "resource": "settings",
                        "source": "templates/settings.json.j2",
                        "target": "live/settings.json",
                        "owners": {"value": field_owner},
                        "values": {"value": 1},
                    }
                ),
                encoding="utf-8",
            )
        self.manifest_path.write_text(
            f"version = {version}\n\n[resources.settings]\n{resource}",
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()
