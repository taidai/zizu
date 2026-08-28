# AWS IoT SiteWise 对 ZiZu L0→L1→L2 的参考

日期：2026-08-28

## SiteWise 的做法

SiteWise 以资产（Asset）及资产层级表示真实设备和组织关系。资产中的工业数据统一称为属性
（Property），主要分成四种：静态属性 Attribute、原始测量 Measurement、即时转换 Transform、
时间窗口统计 Metric。

- Measurement 绑定设备数据流或唯一 alias，保存原始传感器数据。
- Transform 用变量和公式进行一对一转换；任一输入出现新点时，使用其他输入的最新值计算。
- Metric 用公式和滚动时间窗口汇总全部输入点，每个窗口输出一个数据点，并可跨资产层级汇总。
- 同一个 Property 既能查询当前 TQV（值、时间戳、质量），也能查询历史 TQV；实时和历史不是两种对象。
- Asset model interface 定义跨不同资产模型的标准属性和指标，已有不同命名可自动或手工映射到统一接口。

## 与 ZiZu 的对应

| SiteWise | ZiZu |
| --- | --- |
| Asset / hierarchy | 真实节点 / 节点树 |
| Data stream + Measurement | L0 原始点位 |
| Transform definition | L1 即时点位加工 |
| Metric definition | L1 统计加工 |
| Standard property / interface property | L2 稳定实体 |
| Current property TQV | 实时点位或实体视图 |
| Property value history | 历史点位或实体视图 |

## 建议吸收

1. L2 只保留一种“实体”，通过加工方式区分即时计算和统计计算；不要增加 L3，也不要建设独立统计实体体系。
2. 同一 L0 点位或 L2 实体提供“实时 / 历史”两个数据视图，不复制定义。
3. L1 明确分为即时加工和统计加工两类强类型规则；统计加工包含窗口、函数、时区和输出实体。
4. 品牌兼容继续由点位加工模板完成，作用类似 SiteWise 的标准接口映射，但不复制 Asset model、Component model、Interface 三套模型。
5. 页面按任务组织：原始点位、点位加工、实体数据；三栏血缘视图只用于定义和诊断加工链。

## 不应照搬

SiteWise Transform 在任一输入到达时立即使用其他输入的最新值，可能混用不同采样时刻。ZiZu 的光储 EMS
需要同拍数据一致性，应继续采用已确认的黑板、统一节拍、不可变帧和提交后可见语义，不复制该触发方式。
SiteWise 的云端模型、权限、门户和组件层级对单站 ARM 工控机过重，也不应照搬。

## 官方来源

- [Define data properties](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/asset-properties.html)
- [Transform data](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/transforms.html)
- [Aggregate data with metrics](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/metrics.html)
- [Query current values](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/current-values.html)
- [Query historical values](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/historical-values.html)
- [Manage data streams](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/manage-data-streams.html)
- [Asset model interfaces](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/interface-asset-model-relationship.html)
- [Asset model hierarchies](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/define-asset-hierarchies.html)
