# Inline L0 Point Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an engineer select L0 points and publish L2 entities from the raw-point page without first managing a shared template, while preserving the existing immutable L1 and committed-frame runtime.

**Architecture:** Add node-private metadata to the existing immutable point-processing revision store, then expose one deep draft-planning interface that persists a node-private revision and reuses the existing preview/apply seam. Replace the separate L1 tab with an inline raw-point action; shared templates remain an explicit administrator reuse action.

**Tech Stack:** FastAPI, Python 3.12, PostgreSQL/TimescaleDB, React, TypeScript, Node test runner, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-inline-l0-point-processing-design.md`

## Global Constraints

- L0 values, quality, timestamps and sources remain immutable protocol facts.
- L1 remains the only processing engine; do not add scripts, a second expression engine, a second runtime path or an L1 time-series table.
- Cross-node formulas may consume only committed L2.
- Runtime changes only through the existing point-processing plan/apply configuration gate.
- Shared template JSON remains `zizu.point-processing/v1alpha1` and contains no node UUID or credentials.
- No new dependency, Redis, Kafka, microservice, device write, Caddy or TLS work.

---

### Task 1: Node-private immutable processing revisions

**Files:**
- Create: `init-db/migration_051_node_private_point_processing.sql`
- Modify: `backend/app/services/point_processing_templates.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Test: `backend/tests/test_node_private_point_processing_postgres.py`
- Modify: `scripts/test_build_release_images.py`

**Interfaces:**
- Produces: `RegisteredPointProcessingTemplate.reuse_scope: Literal["node", "shared"]` and `owner_node_id: UUID | None`.
- Produces: `PostgresPointProcessingTemplates.import_node_definition(raw, *, node_id, actor)`.
- Existing `import_template(raw, actor=...)` continues to create `shared` revisions.

- [ ] **Step 1: Write the failing PostgreSQL migration tests**

```python
def test_schema_051_backfills_shared_and_enforces_owner_pairing(self):
    self.apply_through_050()
    self.execute_migration_051()
    self.assert_existing_templates("shared", owner_node_id=None)
    self.assert_node_scope_requires_owner()

def test_node_definition_is_not_listed_as_shared_template(self):
    registered = self.templates.import_node_definition(
        RAW_TEMPLATE, node_id=self.node_id, actor="engineer"
    )
    assert registered.reuse_scope == "node"
    assert registered.owner_node_id == self.node_id
    assert self.templates.list_templates("PCS") == ()
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_node_private_point_processing_postgres.py -q`

Expected: FAIL because migration 051 and `import_node_definition` do not exist.

- [ ] **Step 3: Add replay-safe Schema 051**

```sql
ALTER TABLE public.t_point_processing_templates
  ADD COLUMN IF NOT EXISTS reuse_scope TEXT NOT NULL DEFAULT 'shared',
  ADD COLUMN IF NOT EXISTS owner_node_id UUID REFERENCES public.t_nodes(id);

ALTER TABLE public.t_point_processing_templates
  ADD CONSTRAINT chk_point_processing_template_reuse_scope
  CHECK (
    (reuse_scope='shared' AND owner_node_id IS NULL) OR
    (reuse_scope='node' AND owner_node_id IS NOT NULL)
  );
```

Also add an index on `(owner_node_id, status)` for node definitions and reject a node-private revision when the owner node is retired.

- [ ] **Step 4: Extend the catalog without changing shared JSON**

```python
@dataclass(frozen=True)
class RegisteredPointProcessingTemplate:
    revision_id: UUID
    template: PointProcessingTemplate
    reuse_scope: Literal["node", "shared"] = "shared"
    owner_node_id: UUID | None = None
```

`persist_point_processing_template` accepts `reuse_scope` and `owner_node_id`, validates their pairing, and includes the metadata in identity conflict checks. `list_templates(device_category)` filters `reuse_scope='shared'`.

- [ ] **Step 5: Run PostgreSQL and existing template tests**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_node_private_point_processing_postgres.py backend/tests/test_point_processing_templates.py backend/tests/test_point_processing_postgres.py -q`

Expected: PASS, including replay of Schema 051 and unchanged shared export content.

- [ ] **Step 6: Commit**

```bash
git add init-db/migration_051_node_private_point_processing.sql backend/app/services/point_processing_templates.py backend/app/services/point_processing_postgres.py backend/tests/test_node_private_point_processing_postgres.py scripts/test_build_release_images.py
git commit -m "feat: add node-private point processing revisions"
```

### Task 2: One draft-to-plan backend interface

**Files:**
- Modify: `backend/app/api/point_processings.py`
- Modify: `backend/app/services/point_processing.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Test: `backend/tests/test_point_processing_public_api.py`
- Test: `backend/tests/test_point_processing_postgres.py`

**Interfaces:**
- Consumes: `import_node_definition(raw, node_id, actor)` from Task 1.
- Produces: `POST /api/v1/nodes/{node_id}/point-processing-drafts/plan`.
- Response remains the existing `PointProcessingPlan.public_dict()` so existing apply/retry semantics are reused.

- [ ] **Step 1: Write failing service and API tests**

```python
def test_engineer_can_plan_node_draft_without_shared_template(client, auth):
    response = client.post(
        f"/api/v1/nodes/{NODE_ID}/point-processing-drafts/plan",
        headers=auth.engineer,
        json={"content": RAW_TEMPLATE, "input_selections": {"power": TAG_ID}},
    )
    assert response.status_code == 201
    assert response.json()["node_id"] == str(NODE_ID)
    assert response.json()["blocking_issues"] == []

def test_node_draft_cannot_be_planned_for_another_node(service):
    with pytest.raises(PointProcessingError, match="belongs to another node"):
        service.preview_node_definition(...)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_point_processing_public_api.py backend/tests/test_point_processing_postgres.py -q`

Expected: FAIL with a missing route/method.

- [ ] **Step 3: Implement a deep orchestration method**

```python
def preview_node_definition(
    self,
    *,
    node_id: UUID,
    content: Mapping[str, Any],
    input_selections: Mapping[str, UUID],
    actor: str,
) -> PointProcessingPlan:
    registered = self._templates.import_node_definition(
        content, node_id=node_id, actor=actor
    )
    return self.preview(PreviewPointProcessing(
        node_id=node_id,
        template_revision_id=registered.revision_id,
        input_selections=input_selections,
        actor=actor,
    ))
```

The method enforces the selected node category, exact L0 ownership, node-private ownership and existing formula/DAG checks. Persisting draft evidence may not change the active configuration revision; only existing apply may do so.

- [ ] **Step 4: Add the authenticated route**

Use a Pydantic body with `extra="forbid"`, `content: dict[str, Any]`, and at most 256 `input_selections`. Map template and processing errors to the existing structured HTTP format.

- [ ] **Step 5: Run public API, PostgreSQL and configuration-gate tests**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_point_processing_public_api.py backend/tests/test_point_processing_postgres.py backend/tests/test_configuration_runtime_gate.py -q`

Expected: PASS; a preview leaves active installation and configuration revision unchanged, apply remains idempotent.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/point_processings.py backend/app/services/point_processing.py backend/app/services/point_processing_postgres.py backend/tests/test_point_processing_public_api.py backend/tests/test_point_processing_postgres.py
git commit -m "feat: plan point processing from raw points"
```

### Task 3: Raw-point selection and inline editor

**Files:**
- Create: `frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx`
- Create: `frontend/src/components/data-trunk/inlinePointProcessingModel.ts`
- Create: `frontend/src/components/data-trunk/inlinePointProcessingModel.test.mjs`
- Modify: `frontend/src/components/NodeTagPanel.tsx`
- Modify: `frontend/src/api/client.ts`

**Interfaces:**
- Produces: `buildNodePointProcessingDraft(points, form): TemplateDocument`.
- Produces: `planNodePointProcessingDraft(nodeId, content, selections): Promise<PointProcessingPlan>`.
- `NodeTagPanel` receives `node`, `actorId`, `readOnly`, and `canManageTemplates`; selection is cleared on node change.

- [ ] **Step 1: Write failing pure model tests**

```javascript
test('one selected L0 becomes one direct L2 draft', async () => {
  const draft = model.buildNodePointProcessingDraft([POWER], {
    mode: 'passthrough', definitionKey: 'pcs.active_power', displayName: 'PCS有功功率'
  })
  assert.equal(draft.inputs.length, 1)
  assert.equal(draft.outputs[0].entityDefinition, 'pcs.active_power')
})

test('formula draft binds every selected L0 and never mutates points', async () => {
  const before = structuredClone([A, B])
  const draft = model.buildNodePointProcessingDraft([A, B], FORMULA_FORM)
  assert.deepEqual([A, B], before)
  assert.equal(draft.outputs[0].transform.kind, 'formula')
})
```

- [ ] **Step 2: Run the model test and confirm RED**

Run: `node --test frontend/src/components/data-trunk/inlinePointProcessingModel.test.mjs`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the model and client call**

Build canonical v1alpha1 content using existing `buildTransform` helpers. Generate a stable node-local asset key from node ID plus the user-supplied business key, but never place the node UUID in shared export content because promotion creates a new shared identity.

- [ ] **Step 4: Implement the inline panel**

The collapsed state shows only selection count and “加工为实体”. The expanded form defaults to direct use and exposes numeric conversion, status mapping and “高级加工” formula. “检查结果” calls the new plan endpoint; “发布” calls existing apply with the existing session retry record.

- [ ] **Step 5: Add selectable rows to `NodeTagPanel`**

Add a checkbox column without changing `RAW_POINT_COLUMNS`; keep selected IDs across paging/filtering, clear on node change, and pass selected full tag descriptors to the inline panel. L0 realtime/history reading remains unchanged.

- [ ] **Step 6: Run frontend tests and TypeScript**

Run: `node --test frontend/src/components/data-trunk/*.test.mjs`

Run: `npm --prefix frontend run build`

Expected: all tests PASS and `tsc -b && vite build` exits 0.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx frontend/src/components/data-trunk/inlinePointProcessingModel.ts frontend/src/components/data-trunk/inlinePointProcessingModel.test.mjs frontend/src/components/NodeTagPanel.tsx frontend/src/api/client.ts
git commit -m "feat: process raw points inline"
```

### Task 4: Remove the separate L1 task page and add explicit promotion

**Files:**
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.ts`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`
- Modify: `frontend/src/pages/NodeTreePage.tsx`
- Modify: `frontend/src/components/data-trunk/EntityDataPanel.tsx`
- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/components/data-trunk/PointProcessingTemplateManager.tsx`
- Modify: `backend/app/api/point_processings.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Test: `backend/tests/test_point_processing_public_api.py`

**Interfaces:**
- Node tabs become exactly `raw-points` and `entities`.
- Produces administrator-only `POST /api/v1/nodes/{node_id}/point-processing-templates/promote`.

- [ ] **Step 1: Change tests first**

Assert node tabs contain only `原始点位` and `实体数据`; entity empty-state copy points to “在原始点位中加工为实体”; promotion copies content to a new shared identity and leaves the active node revision unchanged.

- [ ] **Step 2: Run tests and confirm RED**

Run: `node --test frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_point_processing_public_api.py -q`

- [ ] **Step 3: Hard-cut the separate tab**

Remove `point-processing` from `nodeDataTabs`, remove the processing branch from `NodeTreePage`, and change ordinary copy to two user concepts: raw points and entities. Keep engineering provenance showing L1 revision in entity technical details.

- [ ] **Step 4: Implement explicit administrator promotion**

Load the node's current node-private revision, replace its asset identity/display/brand/model with administrator input, reset revision to 1, parse again, and persist through existing shared `import_template`. Require `SYSTEM_MANAGE`; do not switch any installation.

- [ ] **Step 5: Remove obsolete template-first ordinary UI**

Keep shared template browse/import/export for administrators, but remove it from the normal node flow. Delete only code proven unreachable after TypeScript and tests; do not delete historical evidence or shared template endpoints.

- [ ] **Step 6: Run frontend and backend regression suites**

Run: `node --test frontend/src/components/data-trunk/*.test.mjs`

Run: `npm --prefix frontend run build`

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_point_processing*.py backend/tests/test_data_trunk*.py -q`

- [ ] **Step 7: Commit**

```bash
git add frontend/src backend/app/api/point_processings.py backend/app/services/point_processing_postgres.py backend/tests/test_point_processing_public_api.py
git commit -m "feat: hide l1 behind raw point processing"
```

### Task 5: Goal audit, release and machine-1 acceptance

**Files:**
- Modify: `VERSION`
- Modify: `backend/app/VERSION`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-27-zizu-platform-core-architecture-design.md`
- Create: `docs/deploy-1号机-v0.4.87-http.md`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Produces: version `0.4.87`, Schema 051, immutable linux/arm64 release digest and machine-verifiable acceptance evidence.

- [ ] **Step 1: Run the completion audit**

Map every requirement in the inline spec to a test or live check. Confirm no separate point-processing tab, no L0 mutation, node-private isolation, shared promotion, committed L2 output and unchanged upper-layer L2 boundary.

- [ ] **Step 2: Run full local verification**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests -q`

Run: `node --test frontend/src/**/*.test.mjs`

Run: `npm --prefix frontend run build`

Run: `backend/.venv/Scripts/python.exe -m compileall -q backend/app`

Run: `git diff --check`

Expected: zero failures and zero build/diff errors.

- [ ] **Step 3: Version and document the release**

Set both version files to `0.4.87`, update release schema assertions to `051`, update current README language, and record that ordinary users process L0 inline while L1 remains internal.

- [ ] **Step 4: Build and publish immutable ARM64 image**

Push the source commit, tag the release, wait for the immutable image workflow to succeed, and resolve the linux/arm64 digest. Do not deploy a mutable tag.

- [ ] **Step 5: Back up and deploy machine 1**

Create and verify a PostgreSQL dump before Schema 051. Preserve `network_mode: host`, tmpfs `/dev/mqueue`, restart `unless-stopped`, current volumes and secrets. Do not start Caddy/TLS, run automatic strategies or write devices.

- [ ] **Step 6: Perform real vertical acceptance**

On a disposable node or reversible configuration: select one current PCS L0, create a direct L2, verify L0 before/after equality, committed L2 realtime/history/source, node-private invisibility in shared templates, administrator promotion, and unchanged active revision after promotion. Retire disposable acceptance configuration and prove no active residue.

- [ ] **Step 7: Record evidence and commit docs**

```bash
git add VERSION backend/app/VERSION README.md docs/superpowers/specs/2026-08-27-zizu-platform-core-architecture-design.md docs/deploy-1号机-v0.4.87-http.md CODEX_HANDOFF.md
git commit -m "docs: record 0.4.87 inline processing release"
```
