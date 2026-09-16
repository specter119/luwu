from __future__ import annotations

import dataclasses
import json
import pickle
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Self

from luwu.errors import RenderError
from luwu.manifest import (
    _PUBLIC_VARIABLES_TOKEN,
    _PublicVariables,
    load_manifest,
)
from luwu.rendering import render_template
from luwu.secrets import SecretRenderContext, SecretValue

SENTINEL = "m4-secret-sentinel"


class M4SecretRenderTests(unittest.TestCase):
    def test_secret_is_available_only_under_the_restricted_namespace(self) -> None:
        with _Project("{{ profile }}|{{ secrets.password }}\n") as project:
            rendered = render_template(
                project.manifest.resources[0],
                root=project.root,
                secrets=SecretRenderContext(
                    {"password": SecretValue(SENTINEL)},
                ),
            )

            self.assertEqual(rendered.data, f"developer|{SENTINEL}\n".encode())
            self.assertNotIn(SENTINEL, repr(rendered))
            self.assertNotIn(SENTINEL, repr(dataclasses.asdict(rendered)))
            self.assertNotIn("data", dataclasses.asdict(rendered))
            with self.assertRaises(TypeError) as error:
                json.dumps(rendered)
            self.assertNotIn(SENTINEL, str(error.exception))
            with self.assertRaises(TypeError) as error:
                pickle.dumps(rendered)
            self.assertNotIn(SENTINEL, str(error.exception))

    def test_public_value_does_not_fallback_into_secret_namespace(self) -> None:
        with _Project("{{ secrets.password }}\n") as project:
            resource = replace(
                project.manifest.resources[0],
                variables=_PublicVariables(
                    {"password": SENTINEL},
                    token=_PUBLIC_VARIABLES_TOKEN,
                ),
                variables_sensitivity="public",
            )

            with self.assertRaises(RenderError) as context:
                render_template(resource, root=project.root)

            self.assertEqual(context.exception.code, "template_invalid")
            self.assertNotIn(SENTINEL, str(context.exception))
            self.assertIsNone(context.exception.__cause__)
            self.assertNotIn(SENTINEL, repr(context.exception.__context__))

    def test_missing_alias_is_a_fixed_error(self) -> None:
        with _Project("{{ secrets.missing }}\n") as project:
            with self.assertRaises(RenderError) as context:
                render_template(
                    project.manifest.resources[0],
                    root=project.root,
                    secrets=SecretRenderContext(
                        {"password": SecretValue(SENTINEL)},
                    ),
                )

            self.assertEqual(context.exception.code, "secret_missing")
            self.assertEqual(str(context.exception), "secret alias is unavailable")
            self.assertNotIn(SENTINEL, repr(context.exception))
            self.assertIsNone(context.exception.__cause__)

    def test_namespace_root_and_alias_methods_cannot_be_used_as_values(self) -> None:
        for expression, code in (
            ("{{ secrets }}", "secret_namespace"),
            ("{{ secrets.items() }}", "secret_missing"),
        ):
            with self.subTest(expression=expression), _Project(expression) as project:
                with self.assertRaises(RenderError) as context:
                    render_template(
                        project.manifest.resources[0],
                        root=project.root,
                        secrets=SecretRenderContext(
                            {"password": SecretValue(SENTINEL)},
                        ),
                    )
                self.assertEqual(context.exception.code, code)
                self.assertNotIn(SENTINEL, str(context.exception))

    def test_public_secrets_name_is_rejected_as_a_namespace_collision(self) -> None:
        with _Project("{{ profile }}\n") as project:
            resource = replace(
                project.manifest.resources[0],
                variables=_PublicVariables(
                    {"secrets": "public-value"},
                    token=_PUBLIC_VARIABLES_TOKEN,
                ),
                variables_sensitivity="public",
            )

            with self.assertRaises(RenderError) as context:
                render_template(
                    resource,
                    root=project.root,
                    secrets=SecretRenderContext(
                        {"password": SecretValue(SENTINEL)},
                    ),
                )

            self.assertEqual(context.exception.code, "secret_collision")
            self.assertEqual(
                str(context.exception),
                "secret namespace collides with public variables",
            )
            self.assertNotIn(SENTINEL, repr(context.exception))

    def test_reserved_and_invalid_aliases_fail_without_echoing_the_alias(self) -> None:
        invalid_aliases = (
            "secrets",
            "items",
            "get",
            "1password",
            "密钥",
            "a" * 65,
        )
        for alias in invalid_aliases:
            with self.subTest(alias=alias):
                with self.assertRaises(ValueError) as context:
                    SecretRenderContext({alias: SecretValue(SENTINEL)})
                self.assertEqual(str(context.exception), "secret alias is invalid")
                self.assertNotIn(alias, str(context.exception))
                self.assertNotIn(SENTINEL, repr(context.exception))

    def test_forged_context_value_is_rejected_without_sentinel_leak(self) -> None:
        forged = object.__new__(SecretRenderContext)
        object.__setattr__(
            forged,
            "_SecretRenderContext__values",
            MappingProxyType({"password": object()}),
        )
        with _Project("{{ secrets.password }}\n") as project:
            with self.assertRaises(RenderError) as context:
                render_template(
                    project.manifest.resources[0],
                    root=project.root,
                    secrets=forged,
                )

            self.assertEqual(context.exception.code, "secret_context")
            self.assertEqual(str(context.exception), "secret render context is invalid")
            self.assertNotIn(SENTINEL, repr(context.exception))
            self.assertIsNone(context.exception.__cause__)

    def test_secret_objects_are_redacted_at_object_boundaries(self) -> None:
        secret = SecretValue(SENTINEL)
        context = SecretRenderContext({"password": secret})

        self.assertNotIn(SENTINEL, repr(secret))
        self.assertNotIn(SENTINEL, repr(context))
        self.assertNotIn(SENTINEL, str(secret))
        self.assertNotIn(SENTINEL, str(context))

        for value in (secret, context):
            with self.subTest(operation="json", value=type(value).__name__):
                with self.assertRaises(TypeError) as error:
                    json.dumps(value)
                self.assertNotIn(SENTINEL, str(error.exception))
            with self.subTest(operation="pickle", value=type(value).__name__):
                with self.assertRaises(TypeError) as error:
                    pickle.dumps(value)
                self.assertNotIn(SENTINEL, str(error.exception))

        @dataclasses.dataclass(frozen=True)
        class BusinessObject:
            value: SecretValue
            context: SecretRenderContext

        business = BusinessObject(secret, context)
        self.assertNotIn(SENTINEL, repr(business))
        with self.assertRaises(TypeError) as error:
            dataclasses.asdict(business)
        self.assertNotIn(SENTINEL, str(error.exception))

    def test_render_errors_do_not_retain_secret_exception_context(self) -> None:
        with _Project("{{ secrets.password['not-an-integer'] }}\n") as project:
            with self.assertRaises(RenderError) as context:
                render_template(
                    project.manifest.resources[0],
                    root=project.root,
                    secrets=SecretRenderContext(
                        {"password": SecretValue(SENTINEL)},
                    ),
                )

            error = context.exception
            self.assertEqual(error.code, "template_invalid")
            self.assertNotIn(SENTINEL, str(error))
            self.assertNotIn(SENTINEL, repr(error))
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            self.assertNotIn(SENTINEL, repr(error.__context__))


class _Project:
    def __init__(self, template: str) -> None:
        self._template = template
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Self:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        source = self.root / "templates/settings.conf.j2"
        source.parent.mkdir()
        source.write_text(self._template, encoding="utf-8")
        (self.root / "live").mkdir()
        (self.root / "luwu.toml").write_text(
            """version = 1

[resources.settings]
kind = "template"
source = "templates/settings.conf.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
variables_sensitivity = "public"

[resources.settings.variables]
profile = "developer"
""",
            encoding="utf-8",
        )
        self.manifest = load_manifest(self.root / "luwu.toml")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        assert self._temporary_directory is not None
        self._temporary_directory.cleanup()
