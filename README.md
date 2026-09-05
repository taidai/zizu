# ZiZu

用于开发和交付 EMS 的配置型工业 IoT 平台。

实施工程师不修改平台源码、不直接编写 SQL，只需建立真实节点、接入设备点位、把原始数据加工成稳定
实体，再配置告警、调度策略、控制和固定 EMS 工作台，即可交付单站工业控制系统。光储充 EMS 是首个参考
交付场景。

**当前版本：`v0.8.5`** · [English](README_EN.md) · [完整中英文架构说明](docs/ZIZU-TECHNICAL-ARCHITECTURE.md)

> 当前状态：核心数据主干和调度策略基础闭环已经落地，告警正在现场打磨；统一控制和固定 EMS 工作台仍需完成真实
> 光储充站点的端到端验收。ZiZu 尚不能宣称完整 EMS 已经交付就绪。

## 核心结构

```text
真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体
                                           ↓
                         告警 / 调度策略 / 控制 / 固定 EMS 工作台
```

| 部分 | 作用 | 用户看到什么 |
|---|---|---|
| 真实节点树 | 表示场站、系统和真实设备 | 光伏、储能、充电、并网、负荷及其设备 |
| L0 原始点位 | 保存设备实际上传的值、质量、时间和来源 | 原始数据的实时值、历史和链路状态 |
| L1 点位加工 | 完成映射、换算、状态解析、组合和强类型公式 | 从原始点位定义实体的数据来源与计算 |
| L2 全局实体 | 为所有上层功能提供稳定业务含义 | 实体的实时值、历史、质量和来源证据 |
| 上层应用 | 使用 L2 完成运行功能 | 告警、调度策略、控制和 EMS 工作台 |

L0、L1、L2 是所选真实节点的三个数据视角，不是三类物理子节点。点位、公式和实体不得伪装成节点树
层级。普通实施人员主要使用“原始数据”和“标准实体”两个页面：从 L0 选择点位、定义加工、预览结果并
发布 L2。点位加工模板只在同类设备需要复用时使用，不是首台设备接入的前置条件。
可写数值点只有在单一 Neuron `RW` 点位“直接使用”且显式填写安全边界后，才能发布为调度策略可选的可控 L2。

### 数据怎样运行

```text
设备 → Neuron → NanoMQ → 实时黑板 → 统一数据帧 → L1 计算 → committed L2
                                                                  ↓
                                              告警 / 调度策略 / 控制 / 页面
```

- 单站只有一个活动采集写者；实时黑板按默认 1 秒节拍冻结不可变数据帧。
- 只有数据或质量变化才产生新帧；重复、倒退和迟到数据直接放弃。
- 数据库提交前不推送页面、不触发告警、不执行 JDM、不下发控制。
- 质量分为 `GOOD`、`UNCERTAIN`、`BAD`、`STALE`；非 `GOOD` 数据禁止进入自动控制。
- 每个 L2 都能追溯到实际 L0 观测、L1 修订、配置修订、质量和时间依据。
- 上层应用只能消费已提交 L2，不能直接依赖品牌点位或原始 MQTT。

## 功能模块

| 模块 | 主要能力 |
|---|---|
| 节点与数据 | 真实节点 CRUD、Neuron 点位导入、L0 实时/历史、数据链诊断、点位加工、L2 实时/历史与来源 |
| 告警中心 | L2 告警规则、等级、触发与恢复、多码故障映射、确认、历史、HTTP 通知及投递记录 |
| 调度策略 | 绑定 L2、配置 2充2放、试算、发布、启停，并查看决策、控制意图和回读 |
| 控制 | 人工和调度策略共用安全入口，经唯一 L0 写点下发，并以新 L2 回读确认结果 |
| EMS 工作台 | 按节点类型和标准 L2 展示能流、功率、SOC、趋势和告警 |
| 系统工具 | MQTT、HTTP 通知、系统状态及管理员配置 |

平台不做多租户、解决方案包、设备实例中间层、第二套规则引擎、任意脚本、Redis、Kafka、新微服务或
自由页面设计器。统计计算属于 L1，结果仍是普通 L2，不建立新的“统计实体层”。

## 使用方式

### 1. 建立现场

在“节点与数据”中按真实关系建立场站、子系统和设备。节点树只描述现场拓扑，不放点位或公式。

### 2. 接入原始点位

在设备节点上配置 Neuron 接入并导入点位。进入“原始数据”检查：

- 点位是否出现；
- 当前值、数据时间和接收时间是否更新；
- 质量是否为 `GOOD`；
- `Neuron → MQTT → 数据接收 → 数据帧 → L0` 链路是否连通；
- 实时与历史是否能查询到同一个点位的数据。

L0 必须保留协议原值；例如设备上传 `0/1`，L0 仍显示 `0/1`。布尔含义、故障码和单位换算在 L1 定义。

### 3. 加工为全局实体

在原始数据中选择一个或多个 L0 点位，填写实体名称、业务标识、结果类型和单位，再选择加工方式：

- 直接使用；
- 比例与偏移换算；
- 枚举或状态解析；
- 多点组合；
- 强类型公式；
- 跨节点计算。跨节点输入只能选择其他节点的 L2。

先“检查结果”，确认输入绑定、类型、单位、质量传播和试算值，再发布。发布后在“标准实体”查看 L2 的
实时值、历史和来源证据。需要接入第二台同型号设备时，再把已验证加工保存为模板复用。

### 4. 配置上层应用

- 在告警中心选择 L2，设置等级、触发值、恢复值和持续时间；需要外部通知时绑定 HTTP 通知配置。
- 在“调度策略”中新建 2充2放，绑定 SOC 输入 L2 和功率控制 L2，填写四个时段、功率目标、SOC 范围及其他时段安全目标。
- 先试算并检查快照证据、命中行和拟执行意图；试算不下发。保存后发布不可变版本，再单独启用；运行结果在事件与控制回读中查看，可随时停用。
- GoRules JDM 是调度策略唯一的内部执行语义；简表和“完整规则图”编辑同一份 JDM，不存在第二套规则或动作模型。
- 2充2放的 SOC 输入只接受业务标识 `bms.soc` 或 `storage.soc`、数值类型、单位 `%` 的 L2；值必须为 0～100 的有限数字。品牌原值若为 0～1 比例，先在 L1 换算成百分比，不能把温度、功率或其他百分比实体当 SOC。功率目标使用有安全边界的可写数值 L2，单位为 `kW`。这些约束只适用于内置的 `soc` / `power-target` 绑定，不限制其他 JDM 绑定。
- 修改名称或保存策略必须保留原 JDM、触发方式、时区、全部绑定及其新鲜度契约。完整规则图不能无损显示为内置简表时，继续在完整图中编辑，不自动改写成默认时段；发布、启用和运行时仍会重新检查绑定，错误配置不会自动替换实体。
- 对可控 L2 配置唯一写点、限值、联锁、权限、超时和回读条件。
- EMS 工作台按稳定 L2 语义显示数据，不直接绑定品牌地址。

### 5. 验证并交付

沿固定主干逐层验收：

```text
节点 → L0 实时/历史 → L1 检查与发布 → L2 实时/历史/来源
     → 告警 → 调度策略 → 控制回读 → EMS 工作台
```

交付时锁定平台版本、镜像摘要、数据库 Schema、模板摘要和配置修订，并完成备份恢复、断线、STALE、
进程重启和配置并发验证。详细检查见 [验收清单](docs/acceptance-checklist.md)。

## 快速启动

### 前置条件

- Docker 与 Docker Compose
- Python 3.12+
- 已运行并可访问的 Neuron（需要接入现场协议时）

### 本机启动

```bash
git clone https://github.com/taidai/zizu.git
cd zizu
python scripts/bootstrap_runtime_secrets.py
docker compose up -d --build
docker compose ps
```

首次启动后创建唯一平台管理员，密码通过无回显交互输入：

```bash
docker compose exec backend python -m scripts.bootstrap_admin --username admin
```

默认配置按生产安全要求运行并强制 HTTPS。只有完全隔离的本机开发环境才可在 `.env` 中同时设置：

```env
DEPLOYMENT_MODE=development
ALLOW_INSECURE_DEV_SECRETS=true
AUTH_REQUIRE_HTTPS=false
```

随后访问 `http://127.0.0.1:9000`。生产环境必须使用 TLS 入口、非公开凭据和固定镜像摘要；不要使用
`latest`。常规不可变部署使用 `deploy/docker-compose.release.yml`，裁剪内核 e606 使用
`deploy/docker-compose.release.e606.yml`。

常用运维命令：

```bash
docker compose ps
docker compose logs -f backend
docker compose restart backend
docker compose down
```

`docker compose down` 不删除数据卷；不要使用 `down -v`，除非明确要永久删除数据库和运行数据。

## 本地开发与验证

后端：

```bash
cd backend
python -m pip install -e .
python -m unittest discover -s tests -p 'test_*.py'
```

前端：

```bash
cd frontend
npm ci
npm run dev
npm run build
```

无头浏览器验收需要先配置目标地址和测试身份，再运行：

```bash
cd frontend
npm run test:e2e:node
npm run test:e2e:alarm-http
npm run test:e2e:dispatch-strategy
```

提交或发布前必须再次按[验收清单](docs/acceptance-checklist.md)走通
“节点 → L0 → L1 → L2 → 告警”主干。页面能够打开不等于工业系统已经可交付。

## 技术栈

| 层 | 技术 |
|---|---|
| 协议接入 | Neuron |
| 消息总线 | NanoMQ / MQTT |
| 后端 | Python 3.12、FastAPI |
| 数据库 | PostgreSQL、TimescaleDB |
| 决策模型 | GoRules ZEN / JDM |
| 前端 | React 18、TypeScript、Vite、ECharts |
| 部署 | Docker Compose、不可变镜像摘要 |

## 文档

- [中英文技术架构说明](docs/ZIZU-TECHNICAL-ARCHITECTURE.md)
- [核心架构总纲](docs/superpowers/specs/2026-08-27-zizu-platform-core-architecture-design.md)
- [领域术语](CONTEXT.md)
- [架构决策记录](docs/adr/)
- [验收清单](docs/acceptance-checklist.md)
- [v0.8.4 现场部署记录](docs/deploy-1号机-v0.8.4-http.md)

文档冲突时，解释顺序为：核心架构总纲 → 最新 accepted ADR → 当前专项规格 → 历史记录。

## 许可证

ZiZu 按仓库中的[《十善业协议（Daśa-kuśala License）1.0》](LICENSE)授权。使用、修改或分发前请阅读
完整条款；该许可证包含用途限制和分发义务。
