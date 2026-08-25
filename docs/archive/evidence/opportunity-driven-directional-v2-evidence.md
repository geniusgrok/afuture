# Opportunity-Driven Directional V2 研究证据

> **Historical record.** Preserved for bounded research lineage, not current runtime behavior; see [`documentation-index.md`](../../documentation-index.md).

日期：2026-08-24

## 1. 结论

本阶段重新审视了 `afuture` 的产品选标层，而不是继续把 15bp Stress 年化作为单一优化目标。两条因果、低自由度的产品选择候选均实际实现并经过固定证据门，但都**没有晋级 Production**。

最终 Production 保持 PR #17 / `main@e89ff6c03b9909904ddcc958891cbedad0d30918` 的正式行为：

- `ExecutionAlignedDirectionalPortfolioManager`；
- `ExecutionAlignedAggressivePolicy`；
- 冻结 50 品种、96 templates、Meta lookback 11 / rebalance 3 / active 3；
- gross <=2x；margin <=35%；available >=25%；daily loss 5%；total DD 30%；max 35 lots；
- completed-return governor、completed-activity contract selection、integer/margin-aware lots、reduction-first、depth-aware opening 与 Broker/RiskManager 语义均不变。

固定 Production full_recent 基线仍为：Base **109.0636%** 年化 / **15.8529%** DD / Sharpe **2.0976**；Stress **28.9559%** 年化 / **28.1152%** DD / Sharpe **0.9604**，0 margin rejects、无 full_recent permanent HALT。

没有为了让失败候选过门而调 `core_share`、top fraction、lookback、min observations 或其它阈值。

## 2. Candidate A — generic Opportunity Score

### 2.1 设计

Candidate A 将选标与方向信号拆开，在冻结 96-template core 之后，使用交易日前已经完成的日线信息构造产品 Opportunity Score。固定组件包括价格趋势效率、波动调整强度、breakout location、volume participation 与 open-interest participation。候选只允许缩小已有 raw core exposure：评分靠前的 active products 保留 100%，靠后的 active products 缩至 75%；未评分产品 fail-open；不允许新增产品、翻转方向、扩大单品种风险或把删掉的 gross 重新分配。

该设计的目的不是直接提高 Stress，而是检验“独立的产品机会质量层”是否能删除低质量 exposure，同时保留主要 gross Alpha。

### 2.2 固定 Production L3

固定输入 artifact：`9473260618`，SHA-256：
`ab4321a9cf8b9a61a0f9d435a6fb6bba7dd63986043b429c12c0ff47fd15389c`。

Candidate A Production L3：run `32666984798`，artifact `9500429342`，artifact digest：
`sha256:005112bb5527ad03a8295d0e60148a19d62ffd099dd365d6a1743c0a18a3d0bb`。

| 指标 | PR #17 基线 | Candidate A | 结论 |
|---|---:|---:|---|
| Base 年化 | 109.0636% | **86.5200%** | fail |
| Stress 年化 | 28.9559% | **16.7075%** | fail |
| Stress Sharpe | 0.9604 | **0.6580** | fail |
| Stress DD | 28.1152% | **24.7197%** | 改善但不足以补偿 Alpha 损失 |
| Stress turnover | 256,918,290 | **227,088,785** | -11.6% |
| Stress cost | 385,377.435 | **340,633.1775** | 节省约 44.7k |
| Stress gross signal PnL | 700,245 | **513,365** | 删除约 186.9k gross Alpha |

Candidate A 确实降低换手、成本和回撤，但删除的 gross Alpha 远大于节省的成本，因此拒绝。最大损失集中在原本高贡献产品（尤其 AG，其后 LU/AL），说明简单的通用 market-shape opportunity rank 不能替代策略自身真正的产品 edge。

## 3. Candidate B — completed Product Alpha / Turnover Efficiency

### 3.1 设计

Candidate B 不再用通用市场形态，而直接从冻结 core 自身已经实现的产品级收益历史学习：

```text
raw_weight[t] = PR #17 frozen core target
intraday[t] = close[t] / open[t] - 1
product_gross_alpha[t] = raw_weight[t] * intraday[t]
turnover[t] = abs(raw_weight[t] - raw_weight[t-1])
score[t] = cumulative gross alpha through t-1 / cumulative turnover through t-1
```

`score[t]` 严格只读取 `t-1` 及以前完成结果。使用 expanding history，不增加 lookback 参数。固定 flat-bps 成本对每个产品的 Alpha/turnover 比率只减相同常数，因此 Base/Stress 的产品排序相同。

Overlay 与 Candidate A 使用同一个预声明风险收缩语义：有证据的 active products 中，top half 保留 100%，lower half 保留 75%；无证据 fail-open；绝不新增/翻转/扩大风险，也不重新分配被删除的 gross。

### 3.2 Cheap Float screen — 通过

Cheap screen：run `32667950088`，artifact `9500585934`，digest：
`sha256:ac4f43a864b490a9cb4863c608692832158ecc5034d76e42321a784e50678fd4`。

Float / next-open 层看起来非常强：

| 指标 | Float 基线 | Candidate B |
|---|---:|---:|
| Base full_recent | 187.2603% | **193.9672%** |
| Stress full_recent | 109.3145% | **120.0932%** |
| Base OOS | 167.4162% | **180.6322%** |
| Stress OOS | 100.3473% | **114.4929%** |
| weight turnover | 608.4x | **555.8722x** |

prior1 / prior2 / train 的 Stress 也均改善。按照预声明规则，Candidate B 因此获得**唯一一次** fixed Production-mechanics L3 资格；没有先看 Production 再调整参数。

### 3.3 Fixed Production L3 — 拒绝

Production L3：run `32677278182`，artifact `9503314166`，artifact digest：
`sha256:3994db3ad70b7193bdfaa39914db8bfbc782bd2d7fc7ea4dec737c2691493ddc`。

输入仍为同一 artifact `9473260618`，SHA 校验通过。

| full_recent | PR #17 基线 | Candidate B | Delta |
|---|---:|---:|---:|
| Base 年化 | 109.0636% | **89.6849%** | **-19.3787 pp** |
| Base DD | 15.8529% | **15.6577%** | +0.1952 pp |
| Base Sharpe | 2.0976 | **1.9062** | -0.1914 |
| Stress 年化 | 28.9559% | **21.5225%** | **-7.4334 pp** |
| Stress DD | 28.1152% | **24.4621%** | +3.6531 pp |
| Stress Sharpe | 0.9604 | **0.7752** | -0.1852 |

Production OOS 暴露出 Float screen 没捕捉到的路径依赖：

| OOS | 基线 | Candidate B |
|---|---:|---:|
| Base 年化 | 69.2576% | **130.1675%** |
| Stress 年化 | 74.5376% | **50.5664%** |

其它关键窗口：

- prior1 Stress：-29.7096% → **-30.8721%**（-1.1624 pp，超过预声明 -1pp 容忍）；
- prior2 Stress：-29.7319% → **-29.4757%**；
- train Stress：-8.5133% → **-3.2805%**；
- validation Stress：88.4423% → **23.1627%**（-65.2795 pp）。

Stress 经济效率也变差：

| 指标 | 基线 | Candidate B |
|---|---:|---:|
| gross signal PnL | 700,245 | **585,980** |
| turnover | 256,918,290 | **239,286,290** |
| 15bp cost | 385,377.435 | **358,929.435** |
| net alpha PnL | 314,867.565 | **227,050.565** |
| Net Alpha / Turnover | 12.2556 bps | **9.4887 bps** |

候选没有新增 full_recent HALT / margin reject，gross 仍在 2x 内；失败原因是**经济质量**而不是风险实现错误。

## 4. 为什么 Float 选标会在 Production 中失效

Candidate B 是本阶段最重要的负证据：即使一个产品 selector 在 roll-safe Float 层同时提高 Base、Stress、OOS 并降低换手，也不能推断它会提高真实 Production path。

Production target 还会经过：

```text
product weights
→ concrete-contract availability/roll
→ integer lots
→ max 35 lots
→ margin-aware fitting
→ completed-return governor
→ one-lot stabilization
→ account equity path / circuit / hard gates
→ realized turnover and cost
```

产品权重的小幅缩放可能跨过整数手数、margin fitting 或 governor 的离散边界，改变后续 equity、持仓和可用容量，因此 Float 层的边际排序不是 Production 层的边际价值排序。

这次结果明确否定了“先在 Float 上找到高 Alpha/turnover 产品，再直接缩低分产品”的简单方案。后续若继续研究选标，必须使用**Production-aligned capacity-aware evidence**，或者引入真正新的 point-in-time 信息，而不是继续围绕 75%、top-half、lookback 等已观察历史调参。

## 5. 最终 Production 决策

Candidate A、Candidate B 均拒绝；没有 Candidate C 参数救援。最终分支不保留任何失败候选的 live runtime 接线，`runtime_factory` 恢复 PR #17 正式 manager。

因此本阶段对 Production 经济行为的最终增量为 **0**：

- Base：**109.0636%** 年化 / **15.8529%** DD / Sharpe **2.0976**；
- Stress：**28.9559%** 年化 / **28.1152%** DD / Sharpe **0.9604**；
- Stress turnover：**256,918,290**；
- Stress Net Alpha / Turnover：**12.2556 bps**；
- hard risk 和账户/订单/成交语义全部保持 PR #17。

本阶段的有效产出是：把“选标能力不足”从概念问题变成了两个可审计的反例，并建立了更严格的晋级原则——**以后任何选标层必须证明 Production-aligned marginal value，而不能只凭 Float headline 提升晋级。**
