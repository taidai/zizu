# Task 9m — 系统工具弹窗键盘焦点闭环报告

## 状态

`DONE`。实现提交：`04f4ae38196927b87a5b3ece9587b8b4f9285159`（`fix(tools): close modal focus loops`）。未部署、未 push、未升版、未操作 1 号机。

## RED

先在 `frontend/e2e/tablet-tools.spec.ts` 写入真实 production-preview 键盘回归，产品代码尚未修改时执行：

```powershell
npx playwright test e2e/tablet-tools.spec.ts -g "顶层工具弹窗进入焦点" --reporter=line
```

结果：`0 passed / 1 failed`。失败位于打开“HTTP 通知”顶层弹窗后的首个焦点断言：关闭按钮应获得焦点，实际为 `Received: inactive`。失败原因正是当前 HEAD 缺少初始 modal focus，不是编译、fixture、定位器或环境错误。

## GREEN 与验证

```powershell
npx playwright test e2e/tablet-tools.spec.ts --reporter=line
```

结果：`13 passed / 0 failed / 0 skipped`，Playwright 配置 `retries: 0`，最终用时 `2.0m`。其中新增串行回归覆盖：

- 顶层 HTTP 工具弹窗打开后聚焦关闭按钮，首尾 Tab / Shift+Tab 循环，Escape 后恢复“打开HTTP 通知”；
- HTTP 嵌套编辑器初始焦点、首尾循环、Escape 只关闭当前层、鼠标关闭恢复“编辑”；
- 故障映射嵌套编辑器初始焦点、首尾循环、Escape 只关闭当前层、取消恢复“新建映射表”；
- 永久清空确认框初始焦点、首尾循环、Escape/取消只关闭确认层并恢复“准备清空表”；
- 受控 health route 响应完成后，SQL 输入框仍保持焦点，无固定等待。

```powershell
$jsTests = @(rg --files src e2e/support -g '*.test.mjs' | Sort-Object)
node --experimental-strip-types --test --test-isolation=none --test-reporter=spec @jsTests
```

结果：`201 passed / 0 failed / 0 skipped`（model + support）。

```powershell
npm run build
```

结果：exit `0`，Vite production build 完成 `8210 modules transformed`；仅保留仓库既有 chunk-size warning。

```powershell
git diff --check HEAD^ HEAD
```

结果：exit `0`，无 whitespace error。

测试编写中的一次修正：首轮 GREEN 联跑中，异步焦点用例的 `getByLabel('SQL 查询')` 同时命中 region 与 textarea，形成测试代码 strict-mode error；将定位收窄为真实 `textbox` 后单跑 `1 passed`，最终完整 `tablet-tools.spec.ts` 为上述 `13/13`。没有用 retry、skip 或产品测试 hook 掩盖该错误。

## 文件

- `frontend/src/components/useModalFocus.ts`
- `frontend/src/components/AdminPanel.tsx`
- `frontend/src/components/admin/AlarmHttpNotificationPanel.tsx`
- `frontend/src/components/FaultMapManager.tsx`
- `frontend/e2e/tablet-tools.spec.ts`

## 实现与自查

- 小型前端 hook 只负责打开时聚焦首个可操作控件、当前 dialog 的 Tab 圈闭、Escape 关闭，以及下一帧恢复触发按钮。
- 事件仅由 `event.target.closest('[role="dialog"]')` 对应的当前层处理；嵌套层停止传播，父层不会同时消费同一个 Tab/Escape。
- 顶层全局 Escape 旁路已移除；顶层、HTTP 编辑器、故障映射编辑器、危险确认框的 Escape/关闭/取消/overlay 路径复用各自关闭函数。
- focus effect 只依赖 modal 的 open 状态；health 响应和其他普通 rerender 不会重新聚焦。
- 未改 API、后端、数据模型、权限、文案、清空语义、样式、依赖、版本或发布文件。

## Concerns

- 必需门禁无未解决 concern。
- 一次额外的非 brief 联合旁查（`alarm-http-template-cursor.spec.ts` 与 tools 同进程联跑）为 `20 passed / 1 failed`：首个 cursor case 在 `beforeEach` 15 秒超时，后续 7 个 cursor cases 与 13 个 tools cases 均通过。该旁查不属于 Task 9m 必需门禁，且失败发生在测试准备阶段、没有产品断言；本任务未用 retry 重跑或扩范围处理。最终 required `tablet-tools`、201 model/support、production build 与 diff-check 均独立 exit 0。
