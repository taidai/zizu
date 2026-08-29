"""Run ZiZu's existing verification commands and print one JSON result."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def summarize_status(checks: list[dict], *, site_requested: bool) -> str:
    if any(check["status"] == "FAILED" for check in checks):
        return "FAILED"
    if not site_requested:
        return "INCOMPLETE"
    return "PASSED"


def validate_liveness(payload: dict, expected_version: str) -> None:
    if payload.get("status") != "alive":
        raise ValueError("liveness status is not alive")
    if payload.get("version") != expected_version:
        raise ValueError("liveness version does not match repository VERSION")


def latest_schema(migrations_dir: Path) -> str:
    versions = [path.name.split("_", 2)[1] for path in migrations_dir.glob("migration_*.sql")]
    if not versions:
        raise ValueError("no database migrations found")
    return max(versions, key=int)


def run_check(name: str, command: list[str], cwd: Path) -> dict:
    print(f"\n[{name}] {' '.join(command)}", file=sys.stderr)
    try:
        result = subprocess.run(command, cwd=cwd, check=False)
        status = "PASSED" if result.returncode == 0 else "FAILED"
        return {"name": name, "status": status, "exit_code": result.returncode}
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return {"name": name, "status": "FAILED", "exit_code": 127}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the minimal ZiZu delivery checks.")
    parser.add_argument("--site-url", help="ZiZu site origin for anonymous read-only checks")
    args = parser.parse_args()

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    schema = latest_schema(ROOT / "init-db")
    commit_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = commit_result.stdout.strip() if commit_result.returncode == 0 else "UNKNOWN"

    python = sys.executable
    node = shutil.which("node") or "node"
    npm = shutil.which("npm") or "npm"
    frontend_tests = sorted(
        str(path.relative_to(ROOT / "frontend"))
        for path in (ROOT / "frontend" / "src").rglob("*.test.mjs")
    )
    checks = [
        run_check(
            "backend unit tests",
            [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            ROOT / "backend",
        ),
        run_check(
            "script tests",
            [python, "-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py"],
            ROOT,
        ),
        run_check(
            "frontend tests",
            [node, "--test", "--experimental-strip-types", *frontend_tests],
            ROOT / "frontend",
        ),
        run_check("frontend production build", [npm, "run", "build"], ROOT / "frontend"),
    ]

    if args.site_url:
        parsed = urlparse(args.site_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            checks.append({"name": "site read-only checks", "status": "FAILED", "exit_code": 2})
        else:
            origin = args.site_url.rstrip("/")
            request_headers = {"User-Agent": "ZiZu-Delivery-Verification/1"}
            try:
                with urlopen(Request(origin + "/", headers=request_headers), timeout=10) as response:
                    if response.status != 200:
                        raise ValueError("site root did not return HTTP 200")
                with urlopen(
                    Request(origin + "/api/v1/health/live", headers=request_headers),
                    timeout=10,
                ) as response:
                    if response.status != 200:
                        raise ValueError("liveness endpoint did not return HTTP 200")
                    validate_liveness(json.load(response), version)
                checks.append({"name": "site read-only checks", "status": "PASSED", "exit_code": 0})
            except Exception as exc:
                print(f"site read-only checks: {exc}", file=sys.stderr)
                checks.append({"name": "site read-only checks", "status": "FAILED", "exit_code": 1})

    status = summarize_status(checks, site_requested=bool(args.site_url))
    report = {
        "status": status,
        "commit": commit,
        "version": version,
        "schema": schema,
        "checks": checks,
        "missing": [] if args.site_url else ["site read-only checks"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"PASSED": 0, "INCOMPLETE": 1, "FAILED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
