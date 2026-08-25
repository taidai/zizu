from __future__ import annotations

import unittest


class ConfigurationRevisionTest(unittest.TestCase):
    def test_rejects_invalid_publish_metadata_before_database_access(self) -> None:
        from app.services.configuration_revision import (
            ConfigurationRevisionError,
            validate_configuration_publish,
        )

        invalid = (
            {"base_revision": -1},
            {"actor": " "},
            {"resource_kind": ""},
            {"resource_id": ""},
            {"after_digest": "not-a-digest"},
        )
        valid = {
            "base_revision": 0,
            "actor": "user:engineer",
            "action": "point_processing.publish",
            "resource_kind": "node",
            "resource_id": "node-1",
            "before_digest": None,
            "after_digest": "a" * 64,
            "details": {},
        }
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(ConfigurationRevisionError):
                    validate_configuration_publish(**(valid | override))


if __name__ == "__main__":
    unittest.main()
