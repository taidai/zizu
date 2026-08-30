# Node Management Headless Acceptance Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Spec:** `docs/superpowers/specs/2026-08-30-node-management-headless-acceptance-design.md`

**Goal:** Add a guarded, deterministic Playwright journey that proves every existing Node Management function works on server 1 without modifying real device nodes.

**Global constraints:** Credentials stay in environment variables; all writes are namespaced under `E2E验证`; no control command or rule execution; one scenario ≤60 seconds and the suite ≤300 seconds; production changes require a failing test first.

---

## Task 1: Add and test the acceptance safety contract

**Files:**
- Create: `frontend/e2e/support/acceptanceEnvironment.mjs`
- Create: `frontend/e2e/support/acceptanceEnvironment.test.mjs`

- [ ] Write tests that reject a missing base URL, missing credentials, missing live-write acknowledgement, a write root other than `E2E验证`, and a temporary name without a run ID.
- [ ] Run `node --test e2e/support/acceptanceEnvironment.test.mjs` and observe the missing-module failure.
- [ ] Implement the smallest environment parser and name guard; never return passwords from printable summaries.
- [ ] Re-run the test and confirm it passes.

## Task 2: Install the Playwright harness and failure report

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/support/nodeManagementReporter.mjs`
- Create: `frontend/e2e/support/nodeManagementReporter.test.mjs`

- [ ] Write reporter tests for pass/fail counts, total duration, failure artifact paths, and secret redaction; run them red.
- [ ] Install `@playwright/test` as a test-only dependency and Chromium.
- [ ] Configure headless Chromium, one worker, 60-second test timeout, 300-second global timeout, no retries, and trace/screenshot only on failure.
- [ ] Add `test:e2e:node` and `test:e2e:node:list` scripts.
- [ ] Implement the compact machine-readable and human-readable report; re-run reporter tests green.
- [ ] Run `npm run test:e2e:node:list` and confirm Playwright discovers the suite.

## Task 3: Build the isolated Neuron/MQTT fixture

**Files:**
- Create: `backend/scripts/node_management_e2e_fixture.py`
- Create: `backend/tests/test_node_management_e2e_fixture.py`
- Create: `frontend/e2e/support/e2eFixture.ts`

- [ ] Write Python tests for the deterministic Neuron payload, E2E-only resource naming, current millisecond timestamp, and refusal to publish a non-E2E source; run them red.
- [ ] Implement a fixture command that creates/deletes the namespaced Neuron node/group/tag through ZiZu APIs and publishes telemetry through the existing Paho MQTT dependency.
- [ ] Add a Playwright helper that invokes the fixture without printing credentials and records cleanup failures.
- [ ] Run the Python fixture unit tests green and perform a read-only fixture preflight against 1 号机.

## Task 4: Prove node CRUD, import, and L0

**Files:**
- Create: `frontend/e2e/node-management.spec.ts`

- [ ] Add the login and environment preflight journey.
- [ ] Add platform node create, edit, search, refresh, select, and later-retire assertions using user-visible roles and labels.
- [ ] Add Neuron import preview, confirm, and result assertions for the isolated simulated source.
- [ ] Publish deterministic telemetry and assert L0 realtime value, quality, timestamp, and source.
- [ ] Assert L0 history, search, type filter, and pagination controls.
- [ ] Run the targeted test against 1 号机 and capture the first real failure before changing product code.
- [ ] Fix only demonstrated product defects with focused tests, then re-run green.

## Task 5: Prove L1 templates and L2 evidence

**Files:**
- Modify: `frontend/e2e/node-management.spec.ts`
- Modify only if a demonstrated defect requires it: `frontend/src/pages/NodeTreePage.tsx`
- Modify only if a demonstrated defect requires it: `frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx`
- Modify only if a demonstrated defect requires it: `frontend/src/components/data-trunk/PointProcessingTemplateManager.tsx`
- Modify only if a demonstrated defect requires it: `frontend/src/components/data-trunk/EntityDataPanel.tsx`

- [ ] Add L0 input selection, processing check, preview, and entity publication assertions.
- [ ] Add shared template create, check, publish, select, and install assertions.
- [ ] Assert L2 realtime value, history, quality, source evidence, and technical evidence.
- [ ] Run each new assertion red against the current deployment before any product fix.
- [ ] Apply minimal tested fixes and re-run green.

## Task 6: Prove rule binding and cleanup

**Files:**
- Modify: `frontend/e2e/node-management.spec.ts`

- [ ] Assign one existing rule to the temporary E2E node, verify the binding, remove it, and verify removal without activation or execution.
- [ ] Retire the temporary platform node through the UI and verify it disappears from the active tree.
- [ ] Delete only the namespaced simulated Neuron resources and report any cleanup failure.
- [ ] Assert no false success: failed network/API responses must surface a visible error and fail the scenario.

## Task 7: Verify, document, and deploy any proven fixes

**Files:**
- Modify: `docs/acceptance-checklist.md`
- Modify: `README.md` only if a new command must be documented there
- Modify product/version/deployment files only if product defects required a release

- [ ] Run `node --test e2e/support/*.test.mjs`.
- [ ] Run `python -m unittest backend/tests/test_node_management_e2e_fixture.py` from the repository root.
- [ ] Run `npm run build` in `frontend`.
- [ ] Run the full `npm run test:e2e:node` suite against 1 号机 and confirm total time is below five minutes.
- [ ] Update the acceptance checklist to make headless testing the default and visible Browser the milestone/failure tool.
- [ ] If product code changed, bump the patch version, build immutable ARM images, deploy the pinned digest to 1 号机, and repeat the full suite against the deployed version.
- [ ] Review the diff for credentials, unrelated changes, and unsafe real-node selectors.

