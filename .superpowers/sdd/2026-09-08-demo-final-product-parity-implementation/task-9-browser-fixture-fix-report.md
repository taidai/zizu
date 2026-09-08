# Task 9b — Browser fixture contract fixes report

## Scope

Only E2E contracts changed. No product component, API, dependency, timer, or version file changed.

## RED evidence

1. `tablet-applications.spec.ts` had two existing RED cases at 1280×800 and 1024×768: each looked for the removed exact button label `新建 2充2放` while the merged native-JDM page exposes `新建通用策略`.
2. Before fixture completion, the focused tools navigation run emitted the expected root failure:
   `Cannot read properties of undefined (reading 'filter')` from `EMSWorkbenchPage`, plus a Playwright `pageerror`. The old `/dispatch-strategies` fallthrough returned `{}`, so `strategies` was undefined. The test now records `pageerror` for both the admin and engineer navigation paths, so that regression is observable.

## GREEN evidence

- `ZIZU_E2E_BASE_URL=http://127.0.0.1:4195 npx playwright test e2e/tablet-applications.spec.ts --grep '告警、调度和系统工具在 (1280x800|1024x768) 保持触控可达且只读打开' --reporter=line`
  - `2 passed (19.2s)`; both viewports only checked the current generic creation target and made no create request.
- `npx playwright test e2e/tablet-tools.spec.ts --reporter=line`
  - `11 passed (48.5s)` with the local built preview on port 4397. The intended mocked 403/409/503 paths log HTTP console errors in their respective negative tests; no unexpected `pageerror` occurred. The admin and engineer navigation tests explicitly assert an empty `pageErrors` array.
- `git diff --check`
  - exit 0.

## Fixture alignment

- `/dispatch-strategies` now returns `{ strategies: [] }`.
- `/alarm-events` now returns the formal empty list envelope: `items`, `total`, `page`, `page_size`, `total_pages: 1`, and `summary`. `total_pages: 1` matches the live `backend/app/api/alarm_events.py` pagination contract and the existing complete E2E fixtures.

## Self-review

- The applications test uses the exact merged generic native-JDM label and never clicks it.
- The tools fixture models successful API responses locally rather than weakening product parsing or adding fallbacks.
- Only the two specified E2E spec files and this evidence report are included in the change.
