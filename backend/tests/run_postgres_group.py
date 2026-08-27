"""Run explicit PostgreSQL unittest modules without allowing silent skips."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


_REQUIRED_ENVIRONMENT = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)


def main(arguments: list[str]) -> int:
    if os.environ.get("ZIZU_POSTGRES_TEST") != "1":
        print("ZIZU_POSTGRES_TEST must be exactly 1", file=sys.stderr)
        return 2
    missing = [name for name in _REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing:
        print(
            "missing PostgreSQL test environment: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    if not os.environ["DB_NAME"].endswith("_test"):
        print("DB_NAME must end with _test", file=sys.stderr)
        return 2
    if not arguments:
        print("at least one unittest module is required", file=sys.stderr)
        return 2

    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in arguments)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(
            f"PostgreSQL test group refused {len(result.skipped)} skipped test(s)",
            file=sys.stderr,
        )
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
