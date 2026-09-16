from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from luwu import platform_support
from luwu.errors import PlatformError
from luwu.platform_support import (
    ensure_supported,
    is_supported_platform,
    probe_platform,
)


class PlatformTests(unittest.TestCase):
    def test_current_platform_probe_is_metadata_only_and_supported(self) -> None:
        status = probe_platform()

        self.assertTrue(status.supported)
        self.assertEqual(status.system, "Linux")
        self.assertEqual(status.machine, "x86_64")
        self.assertEqual(status.missing, ())
        self.assertTrue(is_supported_platform())
        self.assertEqual(status.to_dict()["missing"], [])

    def test_other_posix_platform_is_explicitly_unsupported(self) -> None:
        status = probe_platform(system="Darwin", machine="x86_64")

        self.assertFalse(status.supported)
        self.assertIn("linux", status.missing)
        with self.assertRaises(PlatformError) as context:
            ensure_supported(system="Darwin", machine="x86_64")
        self.assertEqual(context.exception.code, "platform_unsupported")

    def test_missing_no_follow_or_lock_primitive_fails_closed(self) -> None:
        with patch.object(platform_support.os, "O_NOFOLLOW", None):
            status = probe_platform()
        self.assertFalse(status.supported)
        self.assertIn("os.O_NOFOLLOW", status.missing)

        missing_lock = types.SimpleNamespace()
        status = probe_platform(fcntl_module=missing_lock)
        self.assertFalse(status.supported)
        self.assertIn("directory-lock", status.missing)

    def test_unsupported_python_version_is_not_authorization(self) -> None:
        status = probe_platform(python_version=(3, 15, 0))

        self.assertFalse(status.supported)
        self.assertIn("python-3.12-3.14", status.missing)
        with self.assertRaises(PlatformError):
            ensure_supported(python_version=(3, 15, 0))


if __name__ == "__main__":
    unittest.main()
