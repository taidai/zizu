# Unified Alarm Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace separate legacy alarm-level/tag configuration with one versioned, batch-capable alarm-configuration workflow whose output is the existing unified `alarm_definition` runtime model.

**Architecture:** Add a deep `AlarmConfiguration` module that owns reusable rule-set revisions, entity-scope expansion, deterministic plans, validation, idempotent application, derived site-configuration installations, and legacy migration previews. FastAPI and React remain thin adapters; `AlarmRuntime` remains the only lifecycle writer.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, PostgreSQL/TimescaleDB, psycopg2, React 19, TypeScript, Vite, standard-library `unittest` plus the existing pytest runner.

## Global Constraints

- Seriousness is exactly `CRITICAL`, `MAJOR`, `WARNING`, or `INFO`; display labels/colors may vary but meanings may not.
- Runtime definitions reference confirmed entity instances, never raw tags, nodes, MQTT topics, or Neuron addresses.
- A batch contains at most 200 entities, 20 rules, and 2,000 expanded definitions.
- Every rule has a stable non-empty ID; rule-set changes create immutable revisions.
- Batch application is atomic, idempotent, audited, and part of the existing site-configuration/install lineage.
- Existing pending/active events keep their immutable definition version; only new events use the new current definition.
- Legacy `error1/error2/error3`, `t_alarm_levels`, and `t_entity_alarm_bindings` become read-only migration inputs.
- No new dependency may be added without maintainer approval.
- Do not include credentials, customer parameters, or real site topology in fixtures or documentation.
- Preserve unrelated Ticket #42 work by executing this plan in an isolated worktree based on the intended integration commit.

---

## File Structure

### New backend units

- `backend/app/services/alarm_configuration.py` — domain commands, immutable values, deterministic compiler, validation, repository protocol, and in-memory adapter.
- `backend/app/services/alarm_configuration_postgres.py` — PostgreSQL repository, current-site locking, derived installation transaction, and legacy candidate queries.
- `backend/app/api/alarm_configurations.py` — strict public HTTP models and capability-protected routes.
- `backend/tests/test_alarm_configuration.py` — pure/in-memory behavior tests.
- `backend/tests/test_alarm_configuration_public_api.py` — public HTTP authorization, error, idempotency, and zero-write tests.
- `backend/tests/test_alarm_configuration_postgres.py` — isolated PostgreSQL migration, concurrency, and restart tests.
- `init-db/migration_034_unified_alarm_configuration.sql` — rule sets, plans, origins, migration records, immutable constraints, and legacy write gates.

### Frontend units

- `frontend/src/pages/AlarmConfigurationPage.tsx` — unified configuration workspace and orchestration state.
- `frontend/src/components/alarm-configuration/EntityScopePicker.tsx` — confirmed entity batch selection.
- `frontend/src/components/alarm-configuration/RuleSetEditor.tsx` — multi-rule revision editor.
- `frontend/src/components/alarm-configuration/PlanPreview.tsx` — expanded add/update/preserve/delete/block preview.
- `frontend/src/components/alarm-configuration/LegacyMigrationPanel.tsx` — legacy candidates and blocking reasons.

### Existing files changed

- `backend/app/main.py` — register the new router and stop startup seeding of legacy alarm templates.
- Delete: `backend/app/core/standard_alarm_templates.py` — its only behavior writes retired legacy configuration at startup; migration mappings move into the new read-only migration compiler.
- `backend/app/api/alarm_levels.py` — retain compatibility reads and reject all legacy writes.
- `backend/app/api/tags.py` — reject legacy alarm-field writes while preserving unrelated tag edits.
- `backend/app/services/alarm_postgres.py` — install compiled site definitions with explicit origin metadata in the caller transaction.
- `backend/app/api/business_security.py` and `backend/tests/test_business_rest_authorization.py` — classify every new route.
- `frontend/src/api/client.ts` — typed unified-alarm client and removal of legacy mutation calls.
- `frontend/src/App.tsx` — one `alarm-config` navigation entry.
- `README.md`, `CONTEXT.md`, `CODEX_HANDOFF.md` — public contract, vocabulary, migration and verification evidence.
- Delete after functionality moves: `frontend/src/pages/AlarmLevelManagerPage.tsx`, `frontend/src/pages/AlarmConfigPage.tsx`. The reason is recorded in the handoff: both expose retired write models and retaining them would preserve an unsafe configuration path.

---

### Task 1: Deterministic Rule-Set and Batch Plan Compiler

**Files:**
- Create: `backend/app/services/alarm_configuration.py`
- Create: `backend/tests/test_alarm_configuration.py`

**Interfaces:**
- Produces: `AlarmRule`, `AlarmRuleSetRevision`, `EntitySelection`, `ResolvedAlarmEntity`, `PlanAlarmConfiguration`, `AlarmConfigurationPlanItem`, `AlarmConfigurationPlan`, `AlarmConfigurationError`, `AlarmConfigurationRepository`, `InMemoryAlarmConfigurationRepository`, and `AlarmConfiguration.plan()`.
- Consumes: UUIDs, current site context, and confirmed entity metadata supplied by the repository; it does not import FastAPI or psycopg2.

- [ ] **Step 1: Write the failing deterministic expansion test**

```python
def test_four_entities_and_three_rules_expand_to_twelve_stable_definitions(self) -> None:
    service, repository = configured_service(entity_count=4)
    revision = repository.save_rule_set_revision(
        key="pcs-power-limits",
        name="PCS 功率分级",
        rules=(
            rule("warning", "WARNING", "gt", 450, "lte", 430),
            rule("major", "MAJOR", "gt", 500, "lte", 470),
            rule("critical", "CRITICAL", "gt", 550, "lte", 500),
        ),
        actor="user:engineer",
    )

    plan = service.plan(
        PlanAlarmConfiguration(
            installation_id=repository.current_installation_id,
            selection=EntitySelection(
                entity_instance_ids=repository.entity_ids,
                device_instance_ids=(),
                entity_definition_ids=(),
            ),
            rule_set_id=revision.rule_set_id,
            rule_set_revision=revision.revision,
        )
    )

    self.assertEqual(12, len(plan.items))
    self.assertEqual(12, len({item.definition_key for item in plan.items}))
    self.assertEqual("ready", plan.status)
    self.assertEqual([], list(plan.blockers))
    self.assertTrue(all(item.action == "add" for item in plan.items))
```

- [ ] **Step 2: Run the test and verify RED**

Run from `backend`:

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration.AlarmConfigurationPlanTest.test_four_entities_and_three_rules_expand_to_twelve_stable_definitions -v
```

Expected: import failure for `app.services.alarm_configuration`; this proves the public domain seam does not exist yet.

- [ ] **Step 3: Implement immutable values and deterministic expansion**

Define these exact public shapes in `alarm_configuration.py`:

```python
Severity = Literal["CRITICAL", "MAJOR", "WARNING", "INFO"]

@dataclass(frozen=True)
class AlarmRule:
    id: str
    name: str
    severity: Severity
    trigger: dict[str, Any]
    trigger_duration_seconds: float
    recovery: dict[str, Any]
    recovery_duration_seconds: float
    notification_throttle_seconds: float
    unit: str | None = None
    fault_map_id: UUID | None = None

@dataclass(frozen=True)
class AlarmRuleSetRevision:
    rule_set_id: UUID
    key: str
    name: str
    revision: int
    rules: tuple[AlarmRule, ...]
    digest: str

@dataclass(frozen=True)
class ResolvedAlarmEntity:
    id: UUID
    device_instance_id: UUID
    definition_id: str
    display_name: str
    data_type: str
    unit: str | None
    confirmation_id: UUID | None

@dataclass(frozen=True)
class EntitySelection:
    entity_instance_ids: tuple[UUID, ...] = ()
    device_instance_ids: tuple[UUID, ...] = ()
    entity_definition_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class PlanAlarmConfiguration:
    installation_id: UUID
    selection: EntitySelection
    rule_set_id: UUID
    rule_set_revision: int

@dataclass(frozen=True)
class AlarmConfigurationPlanItem:
    definition_key: str
    entity_instance_id: UUID
    rule_id: str
    action: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    blockers: tuple[dict[str, Any], ...]

@dataclass(frozen=True)
class AlarmConfigurationPlan:
    id: UUID
    installation_id: UUID
    base_site_configuration_version: int
    rule_set_revision: AlarmRuleSetRevision
    status: str
    items: tuple[AlarmConfigurationPlanItem, ...]
    blockers: tuple[dict[str, Any], ...]
    digest: str

class AlarmConfigurationRepository(Protocol):
    def save_rule_set_revision(
        self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str
    ) -> AlarmRuleSetRevision: ...
    def get_rule_set_revision(
        self, rule_set_id: UUID, revision: int
    ) -> AlarmRuleSetRevision | None: ...
    def resolve_entities(
        self, installation_id: UUID, selection: EntitySelection
    ) -> tuple[ResolvedAlarmEntity, ...]: ...
    def current_site_version(self) -> int: ...
    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan: ...

class AlarmConfiguration:
    def __init__(self, repository: AlarmConfigurationRepository) -> None: ...
    def create_rule_set(
        self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str
    ) -> AlarmRuleSetRevision: ...
    def create_rule_set_revision(
        self, *, rule_set_id: UUID, rules: tuple[AlarmRule, ...], actor: str
    ) -> AlarmRuleSetRevision: ...
    def plan(self, command: PlanAlarmConfiguration) -> AlarmConfigurationPlan: ...
```

Normalize and sort resolved entity UUIDs and rule IDs before expansion. Derive the stable definition key as
`site.alarm.<rule-set-key>.<entity-instance-uuid>.<rule-id>`. Compute the plan digest from canonical JSON containing the current site version, installation ID, rule-set revision digest, selection, and every expanded item.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command. Expected: `OK`, one test.

- [ ] **Step 5: Add a mutation-oriented stability test**

Add a test that reverses both input entity order and rule order, then asserts the same plan digest and the same ordered definition keys. It must fail if sorting is removed.

- [ ] **Step 6: Run the domain file**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration -v
```

Expected: all Task 1 tests pass.

- [ ] **Step 7: Commit Task 1**

```powershell
git add backend/app/services/alarm_configuration.py backend/tests/test_alarm_configuration.py
git commit -m "feat: compile batch alarm configuration plans"
```

---

### Task 2: Validation, Preview Actions, and In-Memory Atomic Apply

**Files:**
- Modify: `backend/app/services/alarm_configuration.py`
- Modify: `backend/tests/test_alarm_configuration.py`

**Interfaces:**
- Consumes: Task 1 types.
- Produces: `AlarmConfiguration.apply(command: ApplyAlarmConfigurationPlan)`, `AppliedAlarmConfiguration`, and stable validation codes.

- [ ] **Step 1: Write failing validation tests using literal expectations**

Create table-driven subtests covering these exact breaks:

```python
cases = (
    (unconfirmed_entity(), "ALARM_ENTITY_UNRESOLVED"),
    (entity(data_type="STRING"), "ALARM_DATA_TYPE_UNSUPPORTED"),
    (entity(unit="kW"), rule("x", "MAJOR", "gt", 10, "lte", 9, unit="degC"), "ALARM_UNIT_MISMATCH"),
    (unsafe_hysteresis_rule(), "ALARM_THRESHOLD_INVALID"),
)
```

For each case, assert `plan.status == "blocked"`, the literal blocker code, and `repository.applied_count == 0`.

- [ ] **Step 2: Verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration.AlarmConfigurationValidationTest -v
```

Expected: failures because Task 1 expands invalid inputs as ready.

- [ ] **Step 3: Implement focused validation**

Implement explicit validators for:

- finite numeric thresholds and durations;
- supported operators `eq/ne/gt/gte/lt/lte`;
- numeric entity types for ordered comparisons;
- unit equality after trimming, with no implicit conversion in v1;
- trigger/recovery separation (`gt/gte` trigger requires lower `lt/lte` recovery; inverse for low alarms);
- unique stable rule IDs;
- confirmed active binding;
- batch limits 200/20/2,000.

Return plan items with `action` in `add/update/preserve/delete_candidate/block`, literal `before/after`, and blockers. Do not throw for a normal plan blocker; reserve `AlarmConfigurationError` for malformed commands, stale apply, and repository failures.

- [ ] **Step 4: Add failing apply/idempotency tests**

```python
def test_apply_is_atomic_and_same_key_returns_same_derived_installation(self) -> None:
    plan = self.ready_plan()
    first = self.service.apply(ApplyAlarmConfigurationPlan(
        plan_id=plan.id, plan_digest=plan.digest,
        idempotency_key="alarm-plan-1", actor="user:engineer"))
    replay = self.service.apply(ApplyAlarmConfigurationPlan(
        plan_id=plan.id, plan_digest=plan.digest,
        idempotency_key="alarm-plan-1", actor="user:engineer"))
    self.assertEqual(first, replay)
    self.assertEqual(1, self.repository.applied_count)
    self.assertEqual(plan.base_site_configuration_version + 1, first.site_configuration_version)
```

Also test:

- same key with a different request → `IDEMPOTENCY_KEY_REUSED`;
- stale base version → `ALARM_PLAN_STALE` and zero writes;
- wrong digest → `ALARM_PLAN_DIGEST_MISMATCH` and zero writes;
- injected audit failure rolls back definitions, current pointers, derived installation, and site version.

- [ ] **Step 5: Verify RED, then implement repository transaction apply**

Add protocol methods `get_plan`, `current_site_context`, `find_idempotency`, and `apply_plan`. The in-memory adapter must stage all mutations in local copies and publish them only after definition/current/site-version/audit writes succeed.

Use these exact command/result shapes:

```python
@dataclass(frozen=True)
class ApplyAlarmConfigurationPlan:
    plan_id: UUID
    plan_digest: str
    idempotency_key: str
    actor: str

@dataclass(frozen=True)
class AppliedAlarmConfiguration:
    id: UUID
    plan_id: UUID
    installation_id: UUID
    site_configuration_version: int
    definition_ids: tuple[UUID, ...]
    audit_event_id: UUID
    applied_at: datetime
```

- [ ] **Step 6: Run Task 2 tests**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration -v
```

Expected: validation, action preview, stale, digest, idempotency, and rollback tests all pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add backend/app/services/alarm_configuration.py backend/tests/test_alarm_configuration.py
git commit -m "feat: validate and apply alarm configuration plans"
```

---

### Task 3: PostgreSQL Schema and Transaction Adapter

**Files:**
- Create: `init-db/migration_034_unified_alarm_configuration.sql`
- Create: `backend/app/services/alarm_configuration_postgres.py`
- Modify: `backend/app/services/alarm_postgres.py`
- Create: `backend/tests/test_alarm_configuration_postgres.py`

**Interfaces:**
- Consumes: Task 2 `AlarmConfigurationRepository` protocol and domain values.
- Produces: `PostgresAlarmConfigurationRepository` and a real transaction that creates a derived installation/site version and definition versions together.

- [ ] **Step 1: Write a failing fresh/upgrade migration test**

Use the existing isolated `*_test` database guard. Apply migrations through 032, insert one installed package/site configuration, then apply migration 034 twice. Assert these tables exist and the second application succeeds:

```text
t_alarm_rule_sets
t_alarm_rule_set_revisions
t_alarm_configuration_plans
t_alarm_definition_origins
t_legacy_alarm_migrations
t_alarm_configuration_idempotency
```

Expected RED: migration file/table missing.

- [ ] **Step 2: Add migration 034 with named constraints**

The migration must:

- create immutable rule-set revision rows with `(rule_set_id, revision)` unique and SHA-256 digest checks;
- store complete canonical plan JSON, base site version, digest, status, actor, and applied result;
- store definition origin (`package`, `rule_set`, `site_override`, `legacy_migration`);
- store legacy source kind/key, target definition IDs, state, actor, and time without altering legacy rows;
- store idempotency by `(actor, idempotency_key)` with request digest and applied installation;
- allow multiple immutable definition versions for the same installation/asset/entity by replacing the old unique constraint with `(installation_id, asset_id, entity_instance_id, content_digest)`;
- keep definition rows immutable and current pointers mutable only in the repository transaction;
- add append-only triggers for rule-set revisions, applied plans, origins, and migration evidence.

Do not relax `t_site_configuration_versions` lineage. Alarm apply creates a new `t_solution_install_plans`, `t_solution_installations`, and site version referencing the same immutable package record/digest while copying the current parameters, Secret references, metadata, configuration digest, and entity identity installation.

- [ ] **Step 3: Verify migration GREEN**

```powershell
$env:ZIZU_POSTGRES_TEST='1'
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_postgres.AlarmConfigurationMigrationTest -v
```

Expected: fresh, upgrade, and replay tests pass.

- [ ] **Step 4: Write the failing real transaction test**

Through `PostgresAlarmConfigurationRepository`, save one rule set, plan two entities × two rules, apply once, and assert:

- one derived `t_solution_installations` row;
- site version increments once;
- four new `t_alarm_definitions` rows;
- four current pointers;
- four origin rows;
- one configuration audit and one idempotency row;
- copied package/parameter/Secret-reference/entity-identity state equals the previous site version.

Inject an origin/audit constraint failure and assert all counts remain unchanged.

- [ ] **Step 5: Implement `PostgresAlarmConfigurationRepository`**

Use one psycopg2 connection and `SELECT ... FOR UPDATE` on `t_site_configuration_state`. Do not call another repository method that opens a second connection inside apply. Pass the same transaction into `PostgresAlarmDefinitionCatalog.install_definitions()` and write origin rows before commit.

- [ ] **Step 6: Add the concurrent stale-plan test**

Run two different ready plans based on the same site version in parallel. Assert exactly one succeeds, one receives `ALARM_PLAN_STALE`, the site-version chain is contiguous, and no orphan definition/origin/installation exists.

- [ ] **Step 7: Run PostgreSQL tests**

```powershell
$env:ZIZU_POSTGRES_TEST='1'
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_postgres -v
```

Expected: migration, transaction, rollback, replay, and concurrency tests pass against the disposable database.

- [ ] **Step 8: Commit Task 3**

```powershell
git add init-db/migration_034_unified_alarm_configuration.sql backend/app/services/alarm_configuration_postgres.py backend/app/services/alarm_postgres.py backend/tests/test_alarm_configuration_postgres.py
git commit -m "feat: persist alarm configuration revisions"
```

---

### Task 4: Public API, Authorization, and Stable Failures

**Files:**
- Create: `backend/app/api/alarm_configurations.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/business_security.py`
- Modify: `backend/tests/test_business_rest_authorization.py`
- Create: `backend/tests/test_alarm_configuration_public_api.py`

**Interfaces:**
- Consumes: `AlarmConfiguration` plus the in-memory/PostgreSQL repository adapters.
- Produces: the nine configuration/migration method routes from the design; acceptance routes are implemented in the companion acceptance plan.

- [ ] **Step 1: Write the anonymous and role-matrix RED tests**

Build a real FastAPI test app with auth and the new router. Assert:

- anonymous `GET /api/v1/alarm-configurations` → 401 `AUTHENTICATION_REQUIRED`;
- operator GET/POST → 403 `PERMISSION_DENIED`;
- engineer/admin GET and plan/apply → allowed;
- rejection creates authorization audit and never calls the configuration repository.

- [ ] **Step 2: Verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_public_api.AlarmConfigurationAuthorizationTest -v
```

Expected: 404 because the router does not exist.

- [ ] **Step 3: Implement strict request/response models and dependency injection**

Create `get_alarm_configuration()` returning the PostgreSQL service in production and override it in tests. Use `ConfigDict(extra="forbid")` for all bodies. Define:

```python
class EntitySelectionRequest(BaseModel):
    entity_instance_ids: list[UUID] = Field(default_factory=list, max_length=200)
    device_instance_ids: list[UUID] = Field(default_factory=list, max_length=200)
    entity_definition_ids: list[str] = Field(default_factory=list, max_length=200)

class CreatePlanRequest(BaseModel):
    installation_id: UUID
    selection: EntitySelectionRequest
    rule_set_id: UUID
    rule_set_revision: int = Field(ge=1)

class ApplyPlanRequest(BaseModel):
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")
```

Read `Idempotency-Key` as a required header on apply. Map `AlarmConfigurationError.code` to 404 for missing resources, 409 for stale/digest/idempotency conflicts, 422 for invalid commands, and 503 for persistence/audit unavailability.

- [ ] **Step 4: Add public HTTP happy-path and zero-write tests**

Only through HTTP: create a rule set, create revision 2, create a two-entity plan, inspect literal four-item preview, apply, replay same key, query current configuration. Add literal tests for every stable machine code in Section 8 of the design and assert repository write counts remain zero for blockers.

- [ ] **Step 5: Register routes and route-coverage policy**

Register `alarm_configurations.router` in `create_app()`. Add every exact method/path to `test_business_rest_authorization.py`; reads use `configuration.read`, mutations use `configuration.write`. The OpenAPI coverage test must fail if a future alarm-configuration route lacks a capability.

- [ ] **Step 6: Run Task 4 tests**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_public_api tests.test_business_rest_authorization -v
```

Expected: all public behavior and route coverage pass.

- [ ] **Step 7: Commit Task 4**

```powershell
git add backend/app/api/alarm_configurations.py backend/app/main.py backend/app/api/business_security.py backend/tests/test_alarm_configuration_public_api.py backend/tests/test_business_rest_authorization.py
git commit -m "feat: expose unified alarm configuration api"
```

---

### Task 5: Legacy Migration Preview and Contract Gate

**Files:**
- Modify: `backend/app/services/alarm_configuration.py`
- Modify: `backend/app/services/alarm_configuration_postgres.py`
- Modify: `backend/app/api/alarm_configurations.py`
- Modify: `backend/app/api/alarm_levels.py`
- Modify: `backend/app/api/tags.py`
- Modify: `backend/app/main.py`
- Modify: `init-db/migration_034_unified_alarm_configuration.sql`
- Modify: `backend/tests/test_alarm_configuration_public_api.py`
- Modify: `backend/tests/test_alarm_configuration_postgres.py`

**Interfaces:**
- Consumes: Task 4 API and repositories.
- Produces: `LegacyAlarmMigrationCandidate`, `LegacyAlarmMigrationPlan`, read-only legacy APIs, and database-level prevention of new legacy configuration.

- [ ] **Step 1: Write failing migration-classification tests**

Seed controlled legacy rows and assert literal classifications:

- one tag with one confirmed entity binding → `ready` and `error1 → CRITICAL`;
- one tag with no binding → `ALARM_ENTITY_UNRESOLVED`;
- one old entity definition with two active instances and no explicit selection → `ALARM_MIGRATION_AMBIGUOUS`;
- custom old level uses its stored `severity`;
- missing fault-map reference → blocker;
- already migrated source returns the same target and is not duplicated.

- [ ] **Step 2: Verify RED**

Run the two alarm-configuration test files. Expected: migration routes or repository methods missing.

- [ ] **Step 3: Implement read-only candidate queries and migration plans**

Resolve tags only through active `t_entity_instance_bindings`; do not use name, priority, creation order, or raw entity definitions as runtime identity. Persist mapping evidence in `t_legacy_alarm_migrations`; do not update or delete the legacy source row.

- [ ] **Step 4: Write failing compatibility-write tests**

Assert:

- legacy GET routes still return old rows and include `deprecated: true` plus `replacement: /api/v1/alarm-configurations`;
- create/update/delete/bind under `/alarm-levels` returns 409 `ALARM_CONFIGURATION_MIGRATION_REQUIRED`;
- tag create/update/batch-update containing any legacy alarm field returns the same 409;
- an unrelated tag display-name update still succeeds;
- startup no longer inserts or updates `t_alarm_levels`.

- [ ] **Step 5: Implement the application and database contract gate**

Remove the `seed_standard_alarm_templates()` startup call and delete `standard_alarm_templates.py`. Define the literal `error1/error2/error3` severity mapping in the new migration compiler; do not carry forward the old startup entity bindings. In migration 034 add triggers that reject:

- all INSERT/UPDATE/DELETE/TRUNCATE on `t_alarm_levels` and `t_entity_alarm_bindings`;
- INSERT with non-null legacy alarm fields on `t_tags`;
- UPDATE OF `alarm_level`, `alarm_type`, `alarm_threshold`, or `fault_map_id` on `t_tags`.

The migration stores mappings separately, so no owner exception is required. Verify ordinary non-alarm tag updates remain possible.

- [ ] **Step 6: Run compatibility and PostgreSQL gates**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_public_api -v
$env:ZIZU_POSTGRES_TEST='1'
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration_postgres -v
```

Expected: migration classifications, compatibility reads, write rejection, startup zero-write, and DB triggers pass.

- [ ] **Step 7: Commit Task 5**

```powershell
git add backend/app/services/alarm_configuration.py backend/app/services/alarm_configuration_postgres.py backend/app/api/alarm_configurations.py backend/app/api/alarm_levels.py backend/app/api/tags.py backend/app/main.py init-db/migration_034_unified_alarm_configuration.sql backend/tests/test_alarm_configuration_public_api.py backend/tests/test_alarm_configuration_postgres.py
git add -u backend/app/core/standard_alarm_templates.py
git commit -m "feat: migrate legacy alarm configuration safely"
```

---

### Task 6: Unified React Alarm Configuration Workspace

**Files:**
- Create: `frontend/src/pages/AlarmConfigurationPage.tsx`
- Create: `frontend/src/components/alarm-configuration/EntityScopePicker.tsx`
- Create: `frontend/src/components/alarm-configuration/RuleSetEditor.tsx`
- Create: `frontend/src/components/alarm-configuration/PlanPreview.tsx`
- Create: `frontend/src/components/alarm-configuration/LegacyMigrationPanel.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Delete: `frontend/src/pages/AlarmLevelManagerPage.tsx`
- Delete: `frontend/src/pages/AlarmConfigPage.tsx`

**Interfaces:**
- Consumes: Task 4/5 public APIs only.
- Produces: one authenticated `alarm-config` workspace; no raw UUID/JSON/SQL/tag-address inputs.

- [ ] **Step 1: Add typed API contracts before UI behavior**

Define `AlarmRule`, `AlarmRuleSetRevision`, `AlarmConfigurationPlan`, `AlarmConfigurationPlanItem`, `LegacyAlarmMigrationCandidate`, and client functions matching every public route. Reuse `apiFetch`; do not add a dependency or direct `fetch` call.

- [ ] **Step 2: Replace navigation with one lazy page**

In `App.tsx`, remove the `alarm-levels` page key, navigation item, lazy import, and render branch. Point the existing `alarm-config` item to `AlarmConfigurationPage`.

- [ ] **Step 3: Implement the list and entity scope picker**

The page loads current configuration, rule sets, and migration count. `EntityScopePicker` offers confirmed entity instances grouped by device instance and entity definition. Display names are primary; UUIDs may appear only in a diagnostic tooltip.

- [ ] **Step 4: Implement multi-rule editing with safe defaults**

Each row edits stable ID, name, fixed severity select, trigger/recovery operators and values, durations, and notification throttle. Reject blank/duplicate IDs locally for usability while treating server validation as authoritative. Defaults are presentation only: no value is saved until the user creates a plan.

- [ ] **Step 5: Implement preview-first apply**

Render counts and every action `add/update/preserve/delete_candidate/block`. Disable apply while blockers exist. Require a fresh user click after a stale-plan response; never auto-regenerate and auto-apply. Generate one UUID idempotency key per deliberate apply click and retain it for retries of that same request.

- [ ] **Step 6: Implement legacy migration panel**

Show source, proposed severity/rule, resolved entity, and blocker reason. Ambiguous candidates require explicit entity-instance selection before plan creation. Do not provide legacy edit controls.

- [ ] **Step 7: Remove retired pages and scan for old writes**

Delete the two retired page files after their useful display logic has moved. Run:

```powershell
rg -n "AlarmLevelManagerPage|fetchAlarmLevels|createAlarmLevel|updateAlarmLevel|deleteAlarmLevel|batchUpdateTags.*alarm" frontend/src
```

Expected: no legacy alarm-level/configuration client type or function remains. All compatibility display data comes from the new migration endpoint.

- [ ] **Step 8: Build and manually smoke the production UI**

```powershell
Set-Location frontend
npm run build
```

Expected: TypeScript and Vite build pass, with only the existing large-chunk warning. Start the existing local app against the in-memory/public test backend and verify: one menu, batch entity selection, three rule rows, 12-item preview, blocker disables apply, successful apply refreshes current version.

- [ ] **Step 9: Commit Task 6**

```powershell
git add frontend/src/api/client.ts frontend/src/App.tsx frontend/src/pages/AlarmConfigurationPage.tsx frontend/src/components/alarm-configuration
git add -u frontend/src/pages/AlarmLevelManagerPage.tsx frontend/src/pages/AlarmConfigPage.tsx
git commit -m "feat: unify alarm configuration workspace"
```

---

### Task 7: Documentation and Configuration Regression Gate

**Files:**
- Modify: `README.md`
- Modify: `CONTEXT.md`
- Modify: `CODEX_HANDOFF.md`
- Modify: `backend/tests/test_alarm_configuration_public_api.py`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: public contract, migration instructions, vocabulary, and a regression gate that no new legacy write endpoint is introduced.

- [ ] **Step 1: Add the structural public-surface test**

Enumerate OpenAPI operations and assert:

- all unified configuration routes carry Bearer security and the intended capability;
- every `/alarm-levels` mutation is absent or returns the migration code;
- tag schemas no longer advertise writable legacy alarm fields;
- only `/health/live` remains anonymous under the existing global contract.

- [ ] **Step 2: Update public documentation**

Document fixed severity semantics, rule-set schema, batch limits, request/response examples, preview actions, error codes, legacy mapping, idempotency, derived installation/site version, and the one-page UI workflow. Update `CONTEXT.md` with `告警规则组`, `告警配置计划`, and `告警配置验收报告` terms.

- [ ] **Step 3: Run the related backend regression**

Set the standard non-production test secrets, then run:

```powershell
$env:DB_PASSWORD='database-secret-value'
$env:NEURON_PASSWORD='neuron-secret-value'
$env:NANOMQ_API_PASSWORD='nanomq-secret-value'
$env:JWT_SECRET='jwt-secret-value-that-is-at-least-32-chars'
& .\.venv\Scripts\python.exe -m unittest tests.test_alarm_configuration tests.test_alarm_configuration_public_api tests.test_business_rest_authorization tests.test_alarm_runtime -v
```

Expected: all related tests pass.

- [ ] **Step 4: Run full backend and frontend gates**

```powershell
Set-Location backend
$env:PYTHONPATH=(Resolve-Path '.\.venv\Lib\site-packages').Path
& 'C:\veighna_studio\python.exe' -m pytest tests -q -p no:cacheprovider
Set-Location ..\frontend
npm run build
```

Expected: the full backend suite and frontend production build pass; any failure blocks completion and must be reported by exact test name.

- [ ] **Step 5: Verify diff and sensitive-data boundaries**

```powershell
git diff --check
git diff --stat
git diff --no-ext-diff
```

Expected: diff check clean. Review the complete diff and reject any credential, customer parameter, or real site topology before commit.

- [ ] **Step 6: Update handoff with exact evidence and commit**

Record commands, counts, skipped tests, PostgreSQL container identity, frontend build result, removed legacy pages, and remaining acceptance-plan work. Then:

```powershell
git add README.md CONTEXT.md CODEX_HANDOFF.md backend/tests/test_alarm_configuration_public_api.py
git commit -m "docs: define unified alarm configuration contract"
```

---

## Plan A Completion Gate

Plan A is complete only when the one-page configuration workflow, batch planning, atomic application, derived site-version lineage, legacy read-only migration, PostgreSQL concurrency/restart evidence, public API authorization, frontend build, and full regression all pass. Alarm lifecycle acceptance and immutable reports are completed by the companion plan; do not close the user goal after Plan A alone.

