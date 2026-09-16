from __future__ import annotations

import inspect
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from luwu import provider_cache
from luwu.provider_cache import (
    CACHE_CAPABILITIES,
    CACHE_PROVIDER_TYPE,
    CacheInspection,
    ExecutableIdentity,
    ProviderCacheError,
    inspect_cache,
    refresh_cache,
)


class ProviderCacheTests(unittest.TestCase):
    def test_refresh_is_closed_metadata_only_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            identity = _identity()

            result = _refresh(path, identity=identity, now=100.0, ttl_seconds=10.0)

            self.assertTrue(result.published)
            self.assertTrue(result.durability_confirmed)
            self.assertTrue(result.cleanup_confirmed)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(document),
                {
                    "schema_version",
                    "provider_type",
                    "capabilities",
                    "executable_identity",
                    "status",
                    "observed_at",
                    "expires_at",
                },
            )
            serialized = path.read_text(encoding="utf-8")
            for forbidden in (
                "resource",
                "item",
                "field",
                "value",
                "payload",
                "content_hash",
                "provider_reference",
                "secret-sentinel",
            ):
                self.assertNotIn(forbidden, serialized)

            inspected = inspect_cache(path, executable_identity=identity, now=109.999)
            self.assertEqual(inspected.status, "fresh")
            self.assertEqual(inspected.entry, result.entry)
            self.assertIsInstance(inspected, CacheInspection)

    def test_expiry_is_diagnostic_and_does_not_delete_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            _refresh(path, now=100.0, ttl_seconds=5.0)

            inspected = inspect_cache(path, now=105.0)

            self.assertEqual(inspected.status, "expired")
            self.assertIsNotNone(inspected.entry)
            self.assertTrue(path.exists())

    def test_executable_identity_mismatch_is_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            _refresh(path, now=100.0, ttl_seconds=60.0)
            different_identity = ExecutableIdentity(2, 3, 0o755, 99, 101)

            inspected = inspect_cache(
                path, executable_identity=different_identity, now=101.0
            )

            self.assertEqual(inspected.status, "identity_mismatch")
            self.assertIsNotNone(inspected.entry)
            self.assertTrue(path.exists())

    def test_missing_cache_is_reported_without_a_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"

            inspected = inspect_cache(path, now=100.0)

            self.assertEqual(inspected.status, "missing")
            self.assertFalse(path.exists())

    def test_corrupt_and_duplicate_documents_are_not_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            _refresh(path)

            path.write_text("not-json", encoding="utf-8")
            first = inspect_cache(path, now=100.0)
            self.assertEqual(first.status, "corrupt")
            self.assertEqual(path.read_text(encoding="utf-8"), "not-json")

            path.write_text(
                '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
            )
            second = inspect_cache(path, now=100.0)
            self.assertEqual(second.status, "corrupt")
            self.assertIn('"schema_version": 1', path.read_text(encoding="utf-8"))

    def test_closed_schema_rejects_unknown_and_sensitive_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            valid = _refresh(path).entry.to_dict()
            valid["unexpected"] = "secret-sentinel"
            path.write_text(json.dumps(valid), encoding="utf-8")
            os.chmod(path, 0o600)

            self.assertEqual(inspect_cache(path).status, "corrupt")
            with self.assertRaises(ProviderCacheError) as raised:
                _refresh(path, status="secret-sentinel")
            self.assertEqual(raised.exception.code, "cache_schema")
            self.assertNotIn("secret-sentinel", str(raised.exception))

    def test_fixed_provider_contract_rejects_boolean_and_invalid_ttl_values(
        self,
    ) -> None:
        invalid_calls: tuple[dict[str, Any], ...] = (
            {"provider_type": "other"},
            {"capabilities": []},
            {"ttl_seconds": -1.0},
            {"ttl_seconds": float("nan")},
            {"now": float("inf")},
        )
        for overrides in invalid_calls:
            with (
                self.subTest(overrides=overrides),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "provider-cache.json"
                with self.assertRaises(ProviderCacheError) as raised:
                    _refresh(path, **overrides)
                self.assertEqual(raised.exception.code, "cache_schema")
                self.assertFalse(path.exists())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            with self.assertRaises(ProviderCacheError):
                _refresh(path, ttl_seconds=True)

    def test_refresh_api_has_no_value_or_reference_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            fixed = {
                "provider_type": CACHE_PROVIDER_TYPE,
                "capabilities": CACHE_CAPABILITIES,
                "executable_identity": _identity(),
                "status": "ok",
                "ttl_seconds": 60.0,
                "now": 100.0,
            }
            for forbidden in (
                "resource_name",
                "resource_path",
                "item",
                "field",
                "value",
                "payload",
                "content_hash",
                "provider_reference",
            ):
                with self.subTest(forbidden=forbidden), self.assertRaises(TypeError):
                    refresh_cache(path, **fixed, **{forbidden: "secret-sentinel"})
            self.assertFalse(path.exists())

            signature = inspect.signature(refresh_cache)
            self.assertFalse(
                signature.parameters.keys()
                & {
                    "resource_name",
                    "resource_path",
                    "item",
                    "field",
                    "value",
                    "payload",
                    "content_hash",
                    "provider_reference",
                }
            )

    def test_non_owner_only_cache_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            _refresh(path)
            os.chmod(path, 0o644)

            inspected = inspect_cache(path)

            self.assertEqual(inspected.status, "permission_denied")
            self.assertTrue(path.exists())

    def test_permission_failure_is_reported_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            with (
                patch(
                    "luwu.provider_cache.filesystem.create_temporary_file",
                    side_effect=PermissionError,
                ),
                self.assertRaises(ProviderCacheError) as raised,
            ):
                _refresh(path)

            self.assertEqual(raised.exception.code, "cache_permission_denied")
            self.assertFalse(raised.exception.published)
            self.assertTrue(raised.exception.cleanup_confirmed)
            self.assertFalse(path.exists())

    def test_temporary_cleanup_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            with (
                patch(
                    "luwu.provider_cache.os.fsync",
                    side_effect=OSError("fsync"),
                ),
                patch(
                    "luwu.provider_cache.os.unlink",
                    side_effect=OSError("cleanup"),
                ),
                self.assertRaises(ProviderCacheError) as raised,
            ):
                _refresh(path)

            self.assertEqual(raised.exception.code, "cache_cleanup_failed")
            self.assertFalse(raised.exception.published)
            self.assertFalse(raised.exception.cleanup_confirmed)
            self.assertTrue(
                any(
                    directory_path.name.startswith(".provider-cache")
                    for directory_path in Path(directory).iterdir()
                )
            )

    def test_post_replace_sync_failure_reports_unknown_durability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            with (
                patch(
                    "luwu.provider_cache.filesystem.sync_directory",
                    side_effect=OSError("directory fsync"),
                ),
                self.assertRaises(ProviderCacheError) as raised,
            ):
                _refresh(path)

            self.assertEqual(raised.exception.code, "cache_durability_unknown")
            self.assertTrue(raised.exception.published)
            self.assertFalse(raised.exception.durability_confirmed)
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_filesystem_safety_primitive_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-cache.json"
            with patch.object(provider_cache.os, "name", "nt"):
                self.assertEqual(inspect_cache(path).status, "platform_unsupported")
                with self.assertRaises(ProviderCacheError) as raised:
                    _refresh(path)
            self.assertEqual(raised.exception.code, "platform_unsupported")


def _identity() -> ExecutableIdentity:
    return ExecutableIdentity(1, 2, 0o755, 3, 4)


def _refresh(
    path: Path,
    *,
    identity: ExecutableIdentity | None = None,
    provider_type: str = CACHE_PROVIDER_TYPE,
    capabilities: tuple[str, ...] = CACHE_CAPABILITIES,
    status: str = "ok",
    ttl_seconds: float = 60.0,
    now: float | None = 100.0,
):
    return refresh_cache(
        path,
        provider_type=provider_type,
        capabilities=capabilities,
        executable_identity=identity or _identity(),
        status=status,
        ttl_seconds=ttl_seconds,
        now=now,
    )


if __name__ == "__main__":
    unittest.main()
