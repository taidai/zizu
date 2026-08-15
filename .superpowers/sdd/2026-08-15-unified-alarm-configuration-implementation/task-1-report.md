# Task 1 实施报告

## RED 证据

先运行 brief 指定命令时，工作树没有 `.venv`，因此命令本身无法启动（PowerShell 找不到
`.\.venv\Scripts\python.exe`）。使用可用的 Python 运行同一 unittest 目标，得到预期的
`ModuleNotFoundError: No module named 'app.services.alarm_configuration'`，证明测试先于领域实现失败。

## 实现摘要

- 新增纯领域 `AlarmRule`、规则集修订、实体选择/解析、计划值对象及异常与仓储协议。
- 新增 `InMemoryAlarmConfigurationRepository`，支持规则集修订、实体解析、站点版本与计划保存。
- `AlarmConfiguration.plan()` 对实体 UUID 和规则 ID 做确定性排序，展开稳定定义键，并以规范 JSON 计算摘要。
- 未引入 FastAPI、数据库驱动或新依赖；未修改 Task 1 范围外文件。

## 测试命令与输出摘要

```text
python -m unittest tests.test_alarm_configuration -v
Ran 2 tests in 0.002s
OK

python -m compileall -q app/services/alarm_configuration.py tests/test_alarm_configuration.py
git diff --check
```

两个测试覆盖四实体×三规则的 12 项展开，以及实体/规则输入顺序变化后的摘要与定义键稳定性。

## 自审

- 值对象使用 `frozen=True`；计划项仅产生 `add`，无持久化/API/UI 提前实现。
- 规则修订摘要和计划摘要均使用排序键、紧凑规范 JSON 与 SHA-256。
- 由于仓库未提供 `.venv`，brief 的精确解释器命令无法执行；已用系统 Python 3.13 完成等价测试与静态检查。

## 文件与 commit

- `backend/app/services/alarm_configuration.py`
- `backend/tests/test_alarm_configuration.py`
- Commit: `dca3baf`

## Fix round 1/5（审查反馈）

### RED 证据

补充五类回归测试后先运行 `python -m unittest tests.test_alarm_configuration -v`，9 个测试中 9 个失败/未通过：
超出实体/规则/展开上限未拒绝，空/重复/非法 severity 未拒绝，未确认实体仍为 ready，乱序仓储未排序，规则嵌套字典 mutation 改变历史值。失败原因均对应缺失实现，而非测试收集错误。

### 修复摘要

- 增加实体 200、规则 20、展开 2000 的稳定上限校验。
- 创建/修订规则集校验 rule id 非空、唯一及四个允许 severity。
- 计划只展开已确认实体；存在未确认实体时返回 blocked 及 `UNCONFIRMED_ENTITY` blocker。
- `plan()` 自身按实体 UUID 排序，不依赖仓储排序。
- `AlarmRule` 构造及内存仓储入库均深复制 trigger/recovery，避免输入 mutation 改写历史 revision。

### 验证

```text
python -m unittest tests.test_alarm_configuration -v
Ran 9 tests in 0.034s
OK

python -m compileall -q app/services/alarm_configuration.py tests/test_alarm_configuration.py
git diff --check
```

### 范围与提交

本轮仅修改 Task 1 领域实现、Task 1 测试及本报告；未新增依赖、未修改 API/UI/数据库。`CODEX_HANDOFF.md` 未在本轮变更。
修复提交：`ca30bd4`。初始提交为 `e77fd38`（包含报告与此前交接记录）。
