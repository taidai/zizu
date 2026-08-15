# Task 4 Report: Guided Acceptance UI and Final Verification

## Status

Completed. The alarm configuration workspace now observes the latest applied
configuration, guides the engineer through the existing alarm lifecycle, and
generates an immutable report only after the server says all required evidence
is complete.

## Implemented

- Added typed frontend progress, run, and report clients. Acceptance retries
  keep one in-memory idempotency key; no local storage, URL token, raw fetch,
  actor field, event-ID input, or JSON mapping was added.
- Added an observer-only latest-application progress read model. It reuses the
  report classifier but never saves a report or idempotency binding.
- Added four Chinese stages for each add/update definition: `待触发`,
  `待操作员在告警中心确认`, `待现场恢复`, and `通过`.
- Added an actual navigation action to the existing alarm center. The
  configuration page does not acknowledge or recover alarms.
- The report action is disabled until `ready_to_report` comes from the server.
  Generated reports are fetched again through the immutable GET API and show a
  readable report/event/audit reference, site version, full digest, overall
  result, transition timeline, event state, and acknowledgement audit result.
- Added explicit mobile single-column fallbacks and preserved the existing
  React, Tailwind, accent, radius, density, keyboard-focus, and Chinese-copy
  conventions.
- Updated README and CODEX_HANDOFF with observer-only semantics, exact required
  transition codes, preserve behavior, migration 036, PostgreSQL evidence, and
  the remaining deployment boundary.

## TDD Evidence

### RED

- Public progress test expected authenticated GET
  `/api/v1/alarm-configuration-applications/latest/acceptance-progress`; the
  unimplemented route returned `404 Not Found` instead of passing auth and
  returning progress.
- Frontend type contract failed with five missing exports: progress/report
  types plus `fetchAlarmConfigurationAcceptanceProgress`,
  `runAlarmConfigurationAcceptance`, and `fetchAlarmConfigurationReport`.
- First complete backend run exposed the new GET as unclassified in the
  OpenAPI/RBAC coverage gate. The focused test then exposed the expected total
  route count change from 156 to 157.

### GREEN

- Progress domain/public related run: 26 tests OK, 7 PostgreSQL skips.
- Required alarm configuration, public API, acceptance, and runtime run:
  80 tests, 72 passed, 8 PostgreSQL skips; compileall passed.
- Isolated PostgreSQL/protocol seam: 12/12 passed, including migration 036,
  progress zero-write, concurrency, rollback, append-only reports, and Task 3
  public protocol lifecycle plus restart replay. Container and database were
  both `zizu_alarm_task4_test`; exact cleanup completed.
- TypeScript `tsc -b`: passed.
- Frontend production build: passed, 8183 modules, 3m26s; only the existing
  large-chunk warning remains.
- Complete backend: 305 passed, 34 skipped, 199 subtests passed in 116.85s; only
  the existing Starlette/httpx deprecation and duplicate ZIP-member warnings.
- `git diff --check` and staged diff integrity check: passed before commit.

## Browser Smoke

The local production preview started successfully at `127.0.0.1:4173` and was
stopped after the attempt. The in-app browser runtime reported no available
browser instances, so no interactive browser result is claimed. Backend public
HTTP, real PostgreSQL, protocol, TypeScript, and production-build evidence are
reported separately and are not presented as a substitute for browser smoke.

## Scope and Concerns

- The minimal server read model was necessary because repeatedly POSTing failed
  acceptance reports would create immutable noise and client-side event joins
  would guess pass state.
- No dependency, telemetry creation, alarm creation, acknowledgement, recovery,
  deployment, Ticket 42, credential, customer parameter, or site topology was
  added or changed.
- Independent clean-environment timed acceptance and real deployment remain
  outside this task.
