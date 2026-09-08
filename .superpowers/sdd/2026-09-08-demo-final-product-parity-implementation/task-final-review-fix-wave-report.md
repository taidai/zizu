# v1.0.5 final whole-branch review — single fix wave

Status: **DONE_WITH_CONCERNS** — all three requested fixes and their focused RED/GREEN assertions are complete; the final broader Browser gate is **40/41**, with one unresolved existing fault-map focus scenario. This is not a full acceptance PASS.

## Scope and ownership

- Baseline: `a44a169d66e92eb95b6748121a77b2c924c967e5`.
- Worktree: `C:\Users\chent\Documents\zizu-node-e2e\.superpowers\worktrees\demo-final-parity-v105`.
- Binding brief: `task-final-review-fix-wave-brief.md` in this directory. This is the only combined final-review fix wave and addresses all three findings.
- Applied systematic-debugging, test-driven-development, the complete writing-good-tests reference, and verification-before-completion. No subagent was spawned; independent acceptance remains with the controller. CODEX_HANDOFF is controller-owned for this wave.
- Changed only the slot service, its focused tests, the three modal components, workbench CSS, their three existing Browser specs, and this report. No repository adapter, migration, API route/client/payload, permission, local harness, dependency, version, release, deployment or 1号机 change.

## Finding 1 — unknown slot commit result

### Root cause and contract

`EmsWorkbenchSlots._bind_locked()` cancelled the configuration fence for every repository exception. A PostgreSQL commit acknowledgement can be lost after the transaction is durable; cancellation then resumed RUNNING with the old blackboard. The next same-key lookup returned the persisted receipt directly from RUNNING without runtime reconciliation.

The existing PostgreSQL transaction already persists the manual binding (or clear), configuration revision/audit, and idempotency receipt atomically. `PostgresConfigurationRevisions.publish()` assigns `base_revision + 1`; `find_replay()` checks the actor/key and full request digest. No new persistence contract or schema is needed.

### Minimal change

- Known `EmsWorkbenchSlotError` / `ConfigurationRevisionError` domain failures still cancel the fence and retain their existing public semantics. These originate before commit in the current repository.
- Other repository exceptions are conservatively treated as result unknown. They keep the real gate QUIESCED and retain the original slot, nullable target, base revision, actor and idempotency key as operation ownership. They return the existing `CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED` category (503) with truthful unknown-result text.
- Same-key recovery validates the receipt's slot, target and next revision and checks the persisted manual binding/clear. Only the matching owned operation can invoke the existing authoritative runtime restore/revision reset. A successful recovery clears ownership; another same-key replay does not publish or restore again.
- No persisted receipt, a mismatched receipt/owner or binding, or unsuccessful runtime recovery cannot produce success or start a second publication. Capture stays fail closed. Normal RUNNING replay and cross-publisher busy behavior remain intact.
- Generic infrastructure failures are not guessed to be rollbacks merely because no receipt is currently visible.

### RED → GREEN

Before product changes, two focused tests produced three real assertion failures: committed bind + lost ACK, committed clear + lost ACK, and unknown/not-persisted all observed RUNNING where QUIESCED was required.

The final tests use the real `ConfigurationRuntimeGate` and `RealtimeBlackboard`; only the external repository boundary is controlled. They prove:

1. Persisted transaction + lost ACK leaves capture blocked and the original runtime revision in place.
2. Same-key retry returns the authoritative revision-8 receipt, permits a real blackboard tick at revision 8, and restores exactly once.
3. Further replay yields the same receipt with one publication total.
4. Unknown/no durable receipt keeps the original revision, returns a truthful reconciliation-required error, performs no restore, and never publishes again.

Focused service/API suite: **29/29 pass**, including existing old-slot/new-slot and old-slot/pending-node interleaving guards, role restrictions, stale-revision 409/cancel, and compact single-entity response contract.

## Finding 2 — active-modal feedback

### Root cause and change

The operations already retained their draft/confirmation but rendered feedback outside the active overlay. Feedback now uses alert/status regions inside the active dialog. Page-level summaries are shown again after closing the dialog; they are not discarded or announced twice behind an active overlay.

- HTTP editor: blank name/new URL validation, 403, 503, failed network request, and save success stay in `HTTP 通知编辑器`. Failed saves preserve the draft, and successful saves keep the editor open. Existing error-code translation and secret-safe fallback remain unchanged.
- Permanent truncate: failed server/network requests remain in `确认永久清空数据`, preserving `t_audit_log` and `yes`, with the explicit retry button available. Closing restores the page-level error. The blank confirmation remains disabled; request body remains `{table, confirm}`.
- Control: expired-confirmation validation, 409 rejection, 503 unknown result, and lost network receipt remain in `确认控制命令`. Unknown-result retries retain the same confirmation, command body and idempotency key; they never automatically retry. Happy-path close, readback semantics, server checks and API calls are unchanged.

### RED evidence and test-contract correction

- Four control-dialog RED cases: expired confirmation, network result unknown, 409 dispatch rejection and 503 result unknown all found no alert inside the current dialog.
- Six HTTP-editor RED cases: blank name, blank URL, 403, 503, network failure and successful save all found no corresponding in-dialog alert/status.
- Two truncate-dialog RED cases: 503 and network failure found no in-dialog alert.
- Initial broad RED collection was stopped after the four control failures before a touch-target selector with omitted arrow text was used. The corrected exact visible navigation names then produced the actual dimension failures below; no product behavior was changed to accommodate a selector.
- First full Browser GREEN attempt: **39 pass / 2 fail**. Both failures were new HTTP tests incorrectly expecting arbitrary upstream text (`HTTP_SAVE_403/503`) to bypass the pre-existing safe Chinese error mapping. Inspection confirmed the in-modal alert was present and readable. Only those fixtures/expectations were corrected: real `HTTP_NOTIFICATION_PERSISTENCE_UNAVAILABLE` for 503 and the existing safe fallback for 403. The product mapping/client did not change.
- Final HTTP tests also check the displayed alert is hit-testable above overlays, the draft survives, and closing restores the page summary. Truncate tests similarly check retained scope/input and the summary after closing.

## Finding 3 — workbench touch targets

The three current workbench navigation actions are `查看设备 →`, `查看告警 →` and `前往工程配置 →`. Browser fixtures use the engineer role so all three are actual rendered actions, not absent operator-only elements.

| Viewport | RED actual heights (device / alarm / engineering) | Required |
| --- | --- | --- |
| 1024 × 768 | 32 / 36 / 36 px | each ≥44 px |
| 1280 × 800 | 32 / 39 / 39 px | each ≥44 px |

Only the three existing minimum-height declarations were changed to 44px, including the 1024 media path. Labels, navigation behavior, layout structure and permissions are unchanged. Existing overview layout assertions remain, and the new Browser tests measure actual bounding boxes at both required viewports.

## Commands and retained local evidence

All commands ran in this worktree; no remote/field target was contacted. Browser API requests use controlled routes. WebSocket fixtures are the existing test behavior. No fixed sleeps, retries, skip, product hooks or test-harness modifications were added.

```powershell
# backend directory, real Zen path supplied by controller
$env:PYTHONPATH = 'C:\Users\chent\AppData\Local\Temp\zizu-v087-local-514b7ea4b65748a1b7673a8577fe3ef8\python-packages;C:\Users\chent\Documents\zizu-node-e2e\.superpowers\worktrees\demo-final-parity-v105\backend;C:\Users\chent\Documents\zizu-node-e2e\.superpowers\worktrees\demo-final-parity-v105'
C:\veighna_studio\python.exe -B -m unittest tests.test_ems_workbench -v
C:\veighna_studio\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v

# frontend directory
$fixWaveJsTests = @(rg --files src e2e/support -g '*.test.mjs' | Sort-Object)
node --experimental-strip-types --test --test-isolation=none --test-reporter=spec @fixWaveJsTests
npm.cmd run build
node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 4196 --strictPort
$env:ZIZU_E2E_BASE_URL = 'http://127.0.0.1:4196'
node node_modules/@playwright/test/cli.js test e2e/tablet-tools.spec.ts e2e/tablet-control.spec.ts e2e/tablet-runtime.spec.ts --workers=1 --retries=0
git diff --check
```

Local untracked/ignored evidence under `frontend/test-results/`:

- `final-fix-wave-red.log`: four control-dialog failures before the interrupted broader collection.
- `final-fix-wave-red-tools-touch.log`: **10/10 RED**, six HTTP + two truncate + two viewport cases; six actual dimension failures across the two viewport cases.
- `final-fix-wave-backend-focused.log`: 29/29 using the final real-blackboard test implementation.
- `final-fix-wave-backend.log`: first full discovery, 773 total / 492 pass / 281 conditional skips; real Zen loaded; no test failure.
- `final-fix-wave-model-support.log`: 201/201, zero skips/cancellations.
- `final-fix-wave-build.log`: TypeScript + production build, 8,210 modules, exit 0; only existing chunk-size warnings.
- `final-fix-wave-green.log`: first full Browser run, 39/41, exposing the two fixture/formatter expectation errors described above.
- `final-fix-wave-green-final.log`: final Browser verification log.
- `final-fix-wave-backend-final.log`: full discovery repeated after strengthening the focused tests to the real blackboard implementation.
- `playwright-artifacts/`: fresh double-viewport screenshots plus HTTP/truncate/control feedback screenshots produced by these actual components.

## Self-review and acceptance boundary

- Authorization, mutation payloads, HTTP semantics, schema and immutable receipts are unchanged. Slot infrastructure ambiguity now reuses the existing reconciliation-required public category instead of pretending rollback.
- Existing cross-publisher ownership tests remain protected; unknown-result recovery never treats an arbitrary QUIESCED gate as permission to resume.
- The only template content change is placement/accessible presentation of existing feedback (plus the in-dialog expired notice); no automatic retry or destructive/control safety bypass was added.
- The repository contract did not change, so this fix wave did not provision/run a new PostgreSQL database or alter the local harness. The 281 conditional backend skips are explicitly retained, not described as passing PostgreSQL coverage.
- Full node → L0 → L1 → L2 → alarm acceptance, independent whole-branch re-review, final screenshot matrix and final acceptance bookkeeping remain with the controller. This report does not claim v1.0.5 release/deployment/field acceptance.

## Final verification and unresolved concern

| Gate | Final result |
| --- | --- |
| Slot service/API focused tests, real gate + blackboard | 29 pass / 0 fail / 0 skip |
| Backend full discovery, real Zen, final test implementation | 773 total = 492 pass + 281 conditional skip; exit 0; 141.454 s |
| Frontend model/support | 201 pass / 0 fail / 0 skip; exit 0 |
| TypeScript + production build | PASS; 8,210 modules; exit 0 |
| Browser affected suites, final run | 40 pass / 1 fail / 0 skip / 0 retry; exit 1; 179.5 s |
| Requested modal/touch Browser assertions | All 14 relevant cases pass, including six actual ≥44px dimension assertions |
| Diff whitespace | `git diff --check` PASS; Git emits only existing Windows LF→CRLF warnings |

The unresolved Browser case is `tablet-tools.spec.ts:455`, `故障映射保存刷新后恢复到重新渲染的编辑按钮`. After save and the explicitly released refresh, the new edit button is visible but not focused; `toBeFocused()` stays inactive until the existing 30-second test deadline. The same unchanged case passed in the first full 41-case run. Neither `FaultMapManager` nor `useModalFocus` nor this test body was changed by this wave. Its cause is **not established**; it is not classified as a proven environment-only problem or waived as a flaky pass. No blind retry, timeout increase, skip, weakened focus assertion, harness change or out-of-scope fix was used to conceal it.

Exact evidence: `frontend/test-results/final-fix-wave-green-final.log`, and `frontend/test-results/playwright-artifacts/tablet-tools-故障映射保存刷新后恢复到重新渲染的编辑按钮-chromium/{test-failed-1.png,error-context.md,trace.zip}`. This remains a controller-owned blocker for full acceptance and must be considered alongside the earlier whole-branch focus work.

Visual self-check opened the actual 1024/1280 touch screenshots and current HTTP 503, truncate 503 and control unknown-result screenshots. The relevant messages are inside their active overlays and drafts/confirmation scope remain visible. Workbench content keeps its existing scroll layout; no redesign was attempted.

Cleanup: the worktree-owned loopback preview PID 49964 / port 4196 was verified and stopped via its owned process session. The tools spec's existing preview lifecycle cleaned port 4397. A final `Get-NetTCPConnection` check found neither port listening. No file or database was deleted; no field process or external service was touched.

Commit scope is exactly the nine listed implementation/test paths plus this report, starting at `a44a169`. The terminal response supplies the resulting commit range. No push/tag/deploy was performed.
