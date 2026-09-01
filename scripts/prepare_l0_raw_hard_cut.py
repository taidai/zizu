"""Inspect and execute the one-shot L0 raw BIT hard cut."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys
from typing import Sequence

from app.services.l0_raw_cutover import (
    CutoverError,
    apply_cutover,
    clear_runtime_test_data,
    inspect_cutover,
)


def _connect():
    import psycopg2

    from scripts.provision_database_roles import optional, required

    return psycopg2.connect(
        host=optional("DB_OWNER_HOST", required("DB_HOST")),
        port=optional("DB_OWNER_PORT", required("DB_PORT")),
        dbname=required("DB_NAME"),
        user=required("DB_OWNER_USER"),
        password=required("DB_OWNER_PASSWORD"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--clear-runtime", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--actor")
    parser.add_argument("--expected-config-revision", type=int)
    return parser


def _emit(payload: dict, *, stream=None) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stdout if stream is None else stream,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    connection = _connect()
    try:
        if args.inspect:
            report = inspect_cutover(connection)
            payload = {
                "status": "blocked" if report.blockers else "ready",
                "deterministic_output_ids": [
                    str(item) for item in report.deterministic_output_ids
                ],
                "blockers": [
                    {
                        **asdict(item),
                        "node_id": str(item.node_id),
                        "processing_revision_id": str(item.processing_revision_id),
                        "output_id": str(item.output_id),
                    }
                    for item in report.blockers
                ],
                "digest": report.digest,
            }
            _emit(payload)
            return 2 if report.blockers else 0
        if args.apply:
            if not args.expected_digest or not args.actor:
                raise CutoverError("CUTOVER_ARGUMENT_INVALID")
            revisions = apply_cutover(
                connection,
                expected_digest=args.expected_digest,
                actor=args.actor,
            )
            _emit(
                {
                    "status": "applied",
                    "processing_revision_ids": [str(item) for item in revisions],
                }
            )
            return 0
        if args.expected_config_revision is None:
            raise CutoverError("CUTOVER_ARGUMENT_INVALID")
        deleted = clear_runtime_test_data(
            connection,
            expected_configuration_revision=args.expected_config_revision,
        )
        _emit({"status": "cleared", "deleted": deleted})
        return 0
    except CutoverError as exc:
        _emit({"status": "rejected", "code": exc.code}, stream=sys.stderr)
        return 2
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
