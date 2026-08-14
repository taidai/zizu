"""Runtime process boundary for the immutable release image."""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


class ReleaseEntrypointTest(unittest.TestCase):
    def test_uses_loopback_when_the_host_network_release_requests_it(self) -> None:
        result = subprocess.run(
            [str(GIT_BASH), "-c", "./backend/docker-entrypoint.sh"],
            cwd=REPO_ROOT,
            env=os.environ | {
                "UVICORN_BIN": "echo",
                "APP_BIND_HOST": "127.0.0.1",
                "APP_PORT": "9000",
                "LOG_LEVEL": "warning",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            [
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "9000",
                "--log-level",
                "warning",
                "--no-proxy-headers",
            ],
        )


if __name__ == "__main__":
    unittest.main()
