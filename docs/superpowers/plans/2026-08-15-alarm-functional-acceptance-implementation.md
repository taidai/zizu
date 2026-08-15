# Alarm Functional Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce an immutable, restart-safe machine report that proves every new or updated unified alarm configuration traverses trigger, acknowledgement, and field-driven recovery through the normal public product path.

**Architecture:** Add an observer-only `AlarmConfigurationAcceptance` module tied to an applied alarm-configuration plan. It reads immutable definitions, alarm events, transitions, and audit IDs; it never publishes telemetry, acknowledges events, or changes runtime state. Public tests drive telemetry through the existing protocol simulator and acknowledgement through the operator API before requesting the report.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL/TimescaleDB, existing `AlarmRuntime`, existing protocol-simulator test seam, standard-library `unittest`, existing React/Vite workspace.

## Global Constraints

- This plan starts only after the unified alarm-configuration implementation plan is green.
- Acceptance observes normal domain actions; it does not create telemetry, alarms, acknowledgements, or recoveries.
- Every `add` or `update` definition must pass; a `preserve` definition may reference a prior passed report for the exact same immutable definition ID.
- Required transition codes are exactly `ALARM_ACTIVATED`, `ALARM_ACKNOWLEDGED`, and `ALARM_RECOVERED`.
- A passed item requires a recovered event and a non-null acknowledgement audit event ID.
- Bad quality or a freshness gap must not satisfy trigger/recovery continuity.
- Reports are immutable, actor-bound, site-version-bound, digest-addressed, and idempotent.
- Tests use public HTTP and the protocol simulator, not direct telemetry/event SQL insertion.
- No new dependency may be added without maintainer approval.

---

## File Structure

- Create: `backend/app/services/alarm_configuration_acceptance.py` — report domain, observer, canonical digest, and in-memory repository.
- Create: `backend/app/services/alarm_configuration_acceptance_postgres.py` — immutable PostgreSQL report repository.
- Modify: `backend/app/api/alarm_configurations.py` — acceptance start/read routes.
- Modify: `backend/app/services/alarm_configuration.py` — expose applied-plan definition IDs and actions.
- Modify: `backend/app/services/alarm_configuration_postgres.py` — load applied-plan evidence.
- Create: `init-db/migration_036_alarm_configuration_acceptance.sql` — immutable report/idempotency tables.
- Create: `backend/tests/test_alarm_configuration_acceptance.py` — pure observer tests.
- Create: `backend/tests/test_alarm_configuration_acceptance_public_api.py` — public HTTP report tests.
- Create: `backend/tests/test_alarm_configuration_acceptance_postgres.py` — protocol-to-report restart seam.
- Modify: `backend/tests/postgres_delivery_app.py` — reuse test-only protocol simulator wiring without adding a production simulator route.
- Modify: `backend/tests/test_business_rest_authorization.py` — classify acceptance routes.
- Modify: `frontend/src/pages/AlarmConfigurationPage.tsx` and `frontend/src/api/client.ts` — guided evidence progress and report display.
- Modify: `README.md`, `CODEX_HANDOFF.md` — acceptance contract and exact verification evidence.

---

### Task 1: Observer-Only Acceptance Domain

**Files:**
- Create: `backend/app/services/alarm_configuration_acceptance.py`
- Create: `backend/tests/test_alarm_configuration_acceptance.py`
- Modify: `backend/app/services/alarm_configuration.py`

**Interfaces:**
- Consumes: `AppliedAlarmConfiguration`, applied plan items, `AlarmRuntime.list()`, `AlarmRuntime.timeline()`.
- Produces: `RunAlarmConfigurationAcceptance`, `AlarmConfigurationAcceptanceItem`, `AlarmConfigurationAcceptanceReport`, `AlarmConfigurationAcceptanceRepository`, `InMemoryAlarmConfigurationAcceptanceRepository`, `AlarmConfigurationAcceptance.run()` and `.get()`.

- [ ] **Step 1: Write the failing complete-lifecycle test**

Use literal definition/event IDs and a real in-memory `AlarmRuntime`. Create two applied plan items: one `add`, one `update`. Drive each runtime event through pending → active, call real `acknowledge`, then submit recovery observations. Run the acceptance observer and assert:

```python
self.assertEqual("passed", report.status)
self.assertEqual(2, len(report.items))
self.assertEqual(
    {"ALARM_ACTIVATED", "ALARM_ACKNOWLEDGED", "ALARM_RECOVERED"},
    set(report.items[0].transition_codes),
)
self.assertEqual("recovered", report.items[0].event_state)
self.assertIsNotNone(report.items[0].acknowledgement_audit_event_id)
```

The test must not call a private acceptance helper or prebuild a report.

- [ ] **Step 2: Verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_acceptance.AlarmConfigurationAcceptanceTest.test_complete_lifecycle_passes -v
```

Expected: import failure for the new module.

- [ ] **Step 3: Implement exact immutable report values**

Define:

```python
@dataclass(frozen=True)
class RunAlarmConfigurationAcceptance:
    application_id: UUID
    actor: str
    idempotency_key: str

@dataclass(frozen=True)
class AlarmConfigurationAcceptanceItem:
    definition_id: UUID
    definition_key: str
    action: str
    status: str
    code: str
    event_id: UUID | None
    event_state: str | None
    transition_codes: tuple[str, ...]
    acknowledgement_audit_event_id: UUID | None
    evidence: dict[str, Any]

@dataclass(frozen=True)
class AlarmConfigurationAcceptanceReport:
    id: UUID
    application_id: UUID
    installation_id: UUID
    site_configuration_version: int
    actor: str
    status: str
    items: tuple[AlarmConfigurationAcceptanceItem, ...]
    started_at: datetime
    finished_at: datetime
    digest: str
```

Canonicalize report content before SHA-256. The service may read runtime state and save the report; it must have no dependency capable of publishing observations or acknowledging alarms.

- [ ] **Step 4: Add failing negative and preserve tests**

Test literal failures:

- no event → `ALARM_ACCEPTANCE_EVENT_MISSING`;
- active but unacknowledged → `ALARM_ACCEPTANCE_ACKNOWLEDGEMENT_MISSING`;
- acknowledged but not recovered → `ALARM_ACCEPTANCE_RECOVERY_MISSING`;
- recovered event missing one required transition → `ALARM_ACCEPTANCE_TIMELINE_INCOMPLETE`;
- preserve with prior report for a different definition ID → fail;
- preserve with prior passed item for the exact same immutable definition ID → pass and reference prior report ID.

- [ ] **Step 5: Implement negative classification and run all domain tests**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_acceptance -v
```

Expected: complete, incomplete, and preserve cases pass with literal codes.

- [ ] **Step 6: Commit Task 1**

```powershell
git add backend/app/services/alarm_configuration_acceptance.py backend/app/services/alarm_configuration.py backend/tests/test_alarm_configuration_acceptance.py
git commit -m "feat: verify configured alarm lifecycles"
```

---

### Task 2: Immutable PostgreSQL Reports and Public API

**Files:**
- Create: `init-db/migration_036_alarm_configuration_acceptance.sql`
- Create: `backend/app/services/alarm_configuration_acceptance_postgres.py`
- Modify: `backend/app/api/alarm_configurations.py`
- Modify: `backend/app/services/alarm_configuration_postgres.py`
- Modify: `backend/tests/test_alarm_configuration_acceptance_public_api.py`
- Modify: `backend/tests/test_business_rest_authorization.py`

**Interfaces:**
- Consumes: Task 1 acceptance service and Plan A application repository.
- Produces: `POST /api/v1/alarm-configuration-applications/{application_id}/acceptance` and `GET /api/v1/alarm-configuration-reports/{report_id}`.

- [ ] **Step 1: Write failing authorization and idempotency HTTP tests**

Assert:

- anonymous POST/GET → 401;
- operator may read reports but cannot run acceptance → 403;
- engineer/admin may run/read;
- missing `Idempotency-Key` → 422 stable request error;
- same key/same application returns the same report;
- same key/different application → 409 `IDEMPOTENCY_KEY_REUSED`.

- [ ] **Step 2: Verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_acceptance_public_api -v
```

Expected: 404 for acceptance routes.

- [ ] **Step 3: Add migration 036**

Create:

- `t_alarm_configuration_reports` with immutable report JSON, status, actor, application/installation/site-version FKs, digest and timestamps;
- `t_alarm_configuration_acceptance_idempotency` keyed by actor/key with request digest and report FK;
- append-only trigger rejecting UPDATE/DELETE/TRUNCATE of reports;
- indexes by application and site version;
- checks for SHA-256 digests and `passed/failed` status.

- [ ] **Step 4: Implement one-transaction report persistence**

Load the applied plan and exact definition IDs, inspect runtime events/transitions, save report and idempotency row on one connection. Concurrent same-key requests must return one report; different request digests must receive the stable conflict without writing a second report.

- [ ] **Step 5: Implement strict public routes and capability metadata**

Use `configuration.write` for run and `runtime.read` for report GET. Responses contain stable definition/event/report IDs and evidence, but no raw tag, topic, Neuron address, token, or Secret. Add both method/path pairs to the route-coverage test.

- [ ] **Step 6: Run Task 2 tests**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_acceptance_public_api tests.test_business_rest_authorization -v
```

Expected: all authorization, report behavior, redaction, and idempotency tests pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add init-db/migration_036_alarm_configuration_acceptance.sql backend/app/services/alarm_configuration_acceptance_postgres.py backend/app/api/alarm_configurations.py backend/app/services/alarm_configuration_postgres.py backend/tests/test_alarm_configuration_acceptance_public_api.py backend/tests/test_business_rest_authorization.py
git commit -m "feat: persist alarm configuration reports"
```

---

### Task 3: Public Protocol-to-Alarm-to-Report PostgreSQL Seam

**Files:**
- Create: `backend/tests/test_alarm_configuration_acceptance_postgres.py`
- Modify: `backend/tests/postgres_delivery_app.py`

**Interfaces:**
- Consumes: public auth, unified configuration API, existing test-only protocol simulator, alarm event API, and acceptance report API.
- Produces: restart-safe evidence for two entities × three severities without direct telemetry/event SQL.

- [ ] **Step 1: Write the full RED scenario**

The test must execute only these product/public operations after database fixture provisioning:

1. login as engineer and operator;
2. create a three-rule set (`WARNING > 450`, `MAJOR > 500`, `CRITICAL > 550`) with safe lower recovery thresholds;
3. select two confirmed `activePower` entity instances;
4. create and inspect a six-item plan;
5. apply with an idempotency key;
6. publish normal, trigger, bad-quality/gap, sustained trigger, and sustained recovery values through `/protocol-simulator/neuron`;
7. query `/alarm-events`, confirm six distinct events and exact severity counts;
8. acknowledge every active event as operator through `/alarm-events/{id}/acknowledgements`;
9. publish recovery values and verify six recovered events;
10. run acceptance and assert a passed six-item report;
11. restart Uvicorn without rebuilding fixtures;
12. read the same report, events, and timelines and assert identical IDs/digest.

Expected initial RED: configuration/acceptance routes or report persistence missing.

- [ ] **Step 2: Wire the existing protocol simulator, not a production bypass**

`postgres_delivery_app.py` may expose the existing test-only Neuron payload route and pipeline flush. It must still call the production parser/normalizer, `DataPipeline`, confirmed binding resolver, `TagAlarmAdapter`, and `AlarmRuntime`. Do not add a production route, direct `t_telemetry_latest` insert, direct event insert, or fake report.

- [ ] **Step 3: Make quality/gap assertions independent**

Before the successful six-event path, use one entity to prove:

- a bad-quality value does not activate;
- a sample after the freshness window restarts pending rather than activating from the old timestamp;
- the event list contains no extra active event or notification.

- [ ] **Step 4: Run the disposable PostgreSQL seam**

Use a disposable TimescaleDB container/database whose name ends in `_test`, set `NO_PROXY=127.0.0.1,localhost`, and run:

```powershell
$env:ZIZU_POSTGRES_TEST='1'
$env:NO_PROXY='127.0.0.1,localhost'
$env:no_proxy='127.0.0.1,localhost'
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_acceptance_postgres -v
```

Expected: one public seam passes, including process restart. Stop/remove only the exact disposable container created for this test.

- [ ] **Step 5: Add database invariants to the seam**

After public assertions, use read-only SQL only for structural evidence: six immutable definitions/origins, one derived installation/site version, one report/idempotency row, no new `t_alarms` rows, and no mutation of legacy configuration rows. These are persistence invariants, not substitutes for public behavior.

- [ ] **Step 6: Commit Task 3**

```powershell
git add backend/tests/test_alarm_configuration_acceptance_postgres.py backend/tests/postgres_delivery_app.py
git commit -m "test: prove alarm configuration lifecycle publicly"
```

---

### Task 4: Guided Acceptance UI and Final Verification

**Files:**
- Modify: `frontend/src/pages/AlarmConfigurationPage.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `README.md`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Consumes: Task 2 report APIs and Task 3 proven flow.
- Produces: product-visible evidence progress and immutable report display without JSON/UUID entry.

- [ ] **Step 1: Add typed report client functions**

Add `runAlarmConfigurationAcceptance(applicationId, idempotencyKey)` and `fetchAlarmConfigurationReport(reportId)`. Keep idempotency keys in component state for retry; do not use raw `fetch`, localStorage, or query-string tokens.

- [ ] **Step 2: Add guided evidence progress**

For the latest applied plan, show each new/update definition with statuses: waiting for trigger, active waiting for acknowledgement, acknowledged waiting for field recovery, passed. Link to the existing alarm center for acknowledgement; do not add an acknowledgement bypass to the configuration page.

- [ ] **Step 3: Add report generation/display**

Enable “生成验收报告” only when the server says every required definition has complete evidence. Display report ID, site version, digest, overall status, and each definition's event/timeline/audit result. Do not ask the user to paste event IDs or JSON mappings.

- [ ] **Step 4: Build and browser-smoke the flow**

```powershell
Set-Location frontend
npm run build
```

Expected: build passes. In the local production build, verify an engineer can see progress, an operator acknowledges only in the alarm center, and the engineer returns to generate/read the immutable report.

- [ ] **Step 5: Run related and complete backend suites**

Run:

```powershell
Set-Location backend
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration tests.test_alarm_configuration_public_api tests.test_alarm_configuration_acceptance tests.test_alarm_configuration_acceptance_public_api tests.test_alarm_runtime -v
```

Then run the complete backend suite and the explicit PostgreSQL seam from Task 3:

```powershell
$env:PYTHONPATH=(Resolve-Path '.\.venv\Lib\site-packages').Path
& 'C:\veighna_studio\python.exe' -m pytest tests -q -p no:cacheprovider
```

Report exact passed/skipped/failed counts; do not convert timeouts into success.

- [ ] **Step 6: Update README and handoff**

Document the guided product flow, observer-only acceptance semantics, required transition codes, preserve-report rule, immutable report response, and PostgreSQL/protocol evidence. Record all command results and remaining deployment boundary in `CODEX_HANDOFF.md`.

- [ ] **Step 7: Run final integrity checks**

```powershell
git diff --check
git diff --stat
git diff --no-ext-diff
```

Expected: clean diff. Review the complete diff and reject any credential, customer parameter, or real site topology before commit.

- [ ] **Step 8: Commit Task 4**

```powershell
git add frontend/src/pages/AlarmConfigurationPage.tsx frontend/src/api/client.ts README.md CODEX_HANDOFF.md
git commit -m "feat: guide alarm configuration acceptance"
```

---

## Final Completion Audit

Before claiming the user goal complete, map every design requirement to current evidence:

- one alarm-configuration menu and no legacy write UI;
- fixed four-level semantics with optional display customization;
- batch entity selection and multi-rule revision authoring;
- deterministic preview and atomic, idempotent derived installation;
- legacy read-only migration and database write gate;
- public authorization and stable zero-write failures;
- protocol-driven trigger/pending/active/acknowledged/recovered behavior;
- bad-quality/freshness continuity behavior;
- exact severity counts and no duplicate events/notifications;
- immutable report containing definition, event, timeline, acknowledgement audit, site version, and digest;
- PostgreSQL process restart persistence;
- complete backend suite and frontend production build;
- no unverified deployment claim.

Only when every line is proven by a test, runtime artifact, or inspected persisted record may the active goal be marked complete.
