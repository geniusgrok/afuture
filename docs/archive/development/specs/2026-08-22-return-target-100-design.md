# afuture 年化 100% 收益目标重构设计

## 1. 目标与边界

目标优先级调整为：在不依赖过高杠杆的前提下，尽最大可能把真实历史成本后年化收益提高到 100% 以上，同时保留可执行性和回撤约束。

本轮允许比既往更高的模型/参数选择自由度，即允许有限程度的历史过拟合，但禁止以下做法：

- 用未来价格或同 bar 收盘后的信息决定同 bar 收益；
- 把连续合约换月跳空直接当成可交易收益；
- 通过无限增大 gross leverage 把低收益 Alpha 机械放大到目标；
- 绕过现有 RiskManager、PairExecutor、账户/订单/成交状态机；
- 把研究结果直接描述成真实实盘收益承诺。

研究 gross leverage 初始硬上限 2.0；只有在明确证明收益提升主要来自 Alpha 而非杠杆后，才允许最高 2.5 的候选敏感性检查，生产默认仍不自动提高。

## 2. 核心判断

现有同品种 M/OI 和 BU/FU 残差均值回归收益过低，继续在同一家族中微调参数无法跨越一个数量级以上的收益差距。因此必须扩展“收益来源”，而不是继续精修单一 pair。

优先研究四类可复用 Alpha：

1. **Cross-sectional relative momentum**：同交易所内做多相对最强、做空相对最弱商品，保持双腿结构；
2. **Slow momentum / fast reversal**：慢趋势与短期反转组合，减少追在局部极值；
3. **Breakout / acceleration**：使用历史滚动区间和收益加速度捕捉强趋势阶段；
4. **Economic residual mean reversion**：保留现有经济 pair 残差家族作为低相关收益源。

研究组合层按滚动历史质量在这些 Alpha 之间动态轮动，而不是固定等权。

## 3. 数据与时序

### L3 搜索数据

使用 AKShare/Sina 中国商品期货连续日线构建约 50 品种 broad universe。连续序列只用于快速发现收益家族，不作为最终生产执行证据。

固定窗口：

- prior1: 2022-08-22 ~ 2023-08-20
- prior2: 2023-08-21 ~ 2024-08-20
- train: 2024-08-21 ~ 2025-08-20
- validation: 2025-08-21 ~ 2026-02-20
- final OOS: 2026-02-21 ~ 2026-08-20
- full recent: 2024-08-21 ~ 2026-08-20

所有信号必须至少 lag 1 个交易日；t 日收盘前可见统计决定 t+1 收益。

### L4 验证数据

任何准备生产化的 pair/产品组合必须进一步使用 specific-contract 日线重建 roll-safe tradable index：t 日按当日已知 OI/volume/交割黑窗选合约，t→t+1 收益必须来自同一具体合约。

## 4. 收益目标组合器

新增 `tools/evaluate_return_target_portfolio.py`，只负责研究，不获得生产下单权限。

### 4.1 候选信号

预定义但更宽的有限搜索空间：

- momentum lookback: 5/10/20/40/60/120
- fast reversal: 1/3/5/10
- slow-fast: slow 20/40/60/120，fast 3/5/10
- breakout: 20/40/60
- volatility normalization: 10/20/40
- rebalance: 1/2/5/10
- active pairs: 1/2/3/4
- gross leverage: 1.0/1.25/1.5/1.75/2.0

候选不是穷举所有组合，而是按策略家族生成有明确经济含义的小型模板集合，然后进行阶段式筛选。

### 4.2 同交易所双腿构造

为了复用现有双腿执行边界，每个候选交易只允许同交易所产品配对。对每个决策日：

- 计算每个产品的 lagged signal；
- 在交易所内将最强产品与最弱产品配成 long/short pair；
- 不允许同一产品同时出现在多个 active pair；
- 根据 pair 历史波动进行风险归一；
- 总 gross exposure 不超过配置上限。

### 4.3 Alpha 轮动

每个策略模板都形成一个独立日收益序列。组合层只使用过去窗口（例如 60/120 日）的收益质量：

- rolling return / Sharpe；
- drawdown；
- hit ratio；
- turnover/cost；
- 当前横截面 dispersion。

根据这些历史指标选择 1~N 个当前最强 Alpha，允许弱 Alpha 权重归零。不能使用未来 OOS 结果决定当日权重。

### 4.4 成本

至少三档单边成本：

- base: 5bp
- stress: 15bp
- extreme: 30bp

调仓 turnover 按 gross weight 变化计费。达到 100% 的判断至少要求 base 成本成立；stress 需要保持正收益且回撤不失控。

## 5. 选择与有限过拟合政策

用户允许一定过拟合，因此本轮与旧治理不同：

- 可以使用更宽参数模板；
- 可以按 train+validation 的目标函数直接优化收益；
- 可以对 full recent 做描述性目标达成搜索；
- 但必须同时保存 `selection_result` 与 `evaluation_result`，明确哪些数字参与过选择；
- final OOS 已不是 pristine，报告必须继续标记 `pristine_final_oos=false`；
- 不能把 full-history 最优参数冒充独立 OOS 证明。

优化目标不是单纯 annual return：

`score = annualized_return - drawdown_penalty - turnover_penalty - instability_penalty`

收益权重显著高于过去版本，但仍对极端回撤和单窗口崩溃施加惩罚。

## 6. 晋级门

### 研究目标门

优先目标：

- full recent base-cost annualized return >= 100%；
- max drawdown > -30%；
- gross leverage <= 2.0；
- 至少 50 个 active trading days；
- stress-cost annualized return > 0；
- 至少两个不同 Alpha family 对收益有实质贡献，或单一 family 在多个独立窗口均显著为正。

如果 2.0x 以下无法达到 100%，允许输出“最大已验证收益”并继续下一轮新 Alpha 研究，但不得通过更高杠杆伪造达标。

### 生产晋级门

达到研究目标后还必须：

- specific-contract roll-safe 验证；
- RiskManager/PairExecutor 兼容性测试；
- 同交易所双腿 exchange/contract metadata 正确；
- 真实 L1 Shadow 与 test cabinet 仍是实盘前置门。

## 7. 生产架构

新增轻量 `AggressivePortfolioPolicy`，职责仅为：

- 根据已经注册的可交易 pair 及其历史评分，给出允许开仓的 pair ID 集合和风险预算乘数；
- 不生成 OrderRequest；
- 不修改账户状态；
- 不绕过 RiskManager；
- 不处理成交或恢复。

第一阶段只实现研究版并验证收益。只有具体 Alpha 通过 L4 后，才把对应 pair 生成规则和 policy 接到 AutoPairManager。

## 8. 验证策略

遵循仓库 AGENTS.md 的渐进验证：

- L1：新研究函数的因果性、配对、gross cap、turnover 成本、无未来信息单测；
- L2：小型合成数据 smoke，确认高收益信号能被识别、差收益信号被淘汰；
- L3：GitHub Actions broad real-data 搜索；
- L4：只对 L3 存活候选抓 specific contracts 做最终 roll-safe 验证；
- 最终：一次完整主 CI + code review。

## 9. 停止条件

达到以下任一条件停止继续扩搜索空间：

1. 低杠杆成本后达到 >=100% 且通过稳定性/回撤门，转入生产接线；
2. 已覆盖预注册 Alpha 家族且所有候选离目标仍有数量级差距，此时必须说明目标与当前数据不相容，而不是继续无限扩大参数自由度；
3. 新搜索提升只来自 future leakage、roll artifact 或杠杆，立即拒绝。
