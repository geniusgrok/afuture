# afuture 研究证据总览

本文件保留 2026-08-21~2026-08-23 的主要研究与生产机械结论。旧套利专项没有被删除或改写；Execution-Aligned Directional 是另一条账户互斥策略链。

## 1. 全局结论

当前必须同时成立的事实：

1. corrected M/OI、经济 pair、BU/FU、intraday、结构套利等市场中性路线没有接近 100% 年化；
2. 50 品种 directional 在明确允许历史选择偏差、gross≤2x 的 specific-contract / next-open float-notional 口径达到 **107.4623% 年化 / 27.4097% 最大回撤**；
3. 当前 production-mechanics Base 在相同两年区间达到 **108.8461% 年化 / 17.8010% 最大回撤 / actual gross peak 1.998253x / no permanent halt**；
4. Stress production-mechanics 只有 **0.9249% 年化**并因 margin hard gate HALT，因此“Base 通过”不能改写成“生产鲁棒性已证明”；
5. 两年历史和所谓 OOS 都已经被研究流程观察，不是 pristine holdout，所有高收益数字都不是未来收益保证。

最新正式证据：

- [`return-target-100-evidence.md`](return-target-100-evidence.md)
- [`directional-production-mechanics-evidence.md`](directional-production-mechanics-evidence.md)

## 2. corrected M/OI calendar

修正中国期货 trading day、同步采样、front-3、20 天黑窗、historical listing 和 point-in-time volume 后：

```text
prior-forward             4 trades, -1.958R
final OOS                  2 trades, +0.296R
recent two years           5 trades, +1.028R
neighbor stability         0 / 16
2% risk proxy annualized   ≈ 1.07%
```

旧同品种策略没有通过高收益经济门。

## 3. 其它套利研究

研究过 cross-sectional / market-neutral momentum、reversal、skewness；rolling residual / beta / OU half-life / regime；P/Y、PP/V、AL/ZN、BU/FU、CU/AL、J/JM 等经济关系；60 分钟 intraday；soybean crush、steel/coke、polymer/base-metals 等结构关系。

BU/FU specific-contract 信号证明并非单纯 continuous roll 假象，但收益仍远低于目标；失败 intraday/structural 实验不进入生产维护面。

## 4. Directional 收益优先阶段

研究 family：breakout、time-series momentum、momentum、moving average、reversal、acceleration / slow-fast。

连续合约研究先发现高收益候选，随后 specific-contract next-open 暴露理论目标与实际执行错位。最终策略把模板筛选/meta evidence 对齐到 execution-aware 历史，并明确承认 selection bias。

当前正式 signal/meta：

- 50 品种；
- 96-template pool；
- meta lookback=11；
- meta rebalance=3；
- meta count=3；
- score=`0.25 × annualized + 1.0 × Sharpe`；
- completed continuous intraday proxy causal ranking；
- point-in-time concrete-contract selection；
- 20 天黑窗；
- target gross≤2x。

## 5. Float-notional L4

固定原始数据：

```text
products                  = 50
candidate contract calls  = 3000
usable concrete contracts = 2540
specific daily rows       ≈ 495086
missing next returns      = 0 on final products
```

`2024-08-21 ~ 2026-08-20` 原官方 artifact：

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |

Final OOS 已被观察，因此 `pristine_final_oos=false`。

## 6. Production-mechanics 收口

本轮没有通过增加 Alpha/template/leverage 解决 research/live gap，而是修正生产机械：

- D 日 completed activity 决定 D+1 concrete contract；
- required signal day = completed activity day；
- stale/missing evidence fail-closed；
- reductions 优先；
- Broker 是唯一 order/fill/position truth；
- integer lots / multiplier / max contract volume 35；
- 5% daily-loss 改为同交易日 circuit，后续交易日满足完整安全条件才恢复；
- total DD / margin / available cash 等保持 hard/manual halt；
- completed-return governor：最近完成日收益≤-2% 或两日样本波动≥3% → 下一目标 25%，否则 100%；
- normal target 不预先 haircut；actual marked gross>2x 时运行时只减仓；
- 同一合约多空毛仓 flatten 按毛仓分别平，避免 net=0 漏风险；
- directional rebalance/fill/cycle execution quality 保持观测闭环。

最终生产 signal policy 仍只有 `ExecutionAlignedAggressivePolicy`。

## 7. 最终 Production L3

固定 run `32617588179`：

| 指标 | Base | Stress |
|---|---:|---:|
| 年化 | **108.8461%** | **0.9249%** |
| 累计 | **311.4052%** | **1.7840%** |
| 最大回撤 | **17.8010%** | **5.8553%** |
| Sharpe | **2.0812** | 0.2246 |
| active days | **478 / 484** | **14 / 484** |
| daily circuit days | 4 | 0 |
| defensive days | 78 | 2 |
| margin reject days | 0 | 6 |
| actual gross peak | **1.998253x** | 1.856519x |
| halted | **false** | **true** |

Base 直接通过四个核心历史门：annualized≥100%、DD≤30%、actual gross≤2x、no permanent halt。

Stress 的早期 margin halt 说明成本/保证金鲁棒性仍弱。Base 的 prior1/prior2 独立窗口也分别约 -28.94% / -28.80%，并触及 30% 总回撤门，说明 regime dependence 仍明显。

## 8. 为什么现在停止历史优化

Base acceptance 已满足后继续围绕同一历史扩展参数空间，增加的是过拟合而不是新信息。当前明确停止：

- 扩大 template pool；
- 提高 gross >2x；
- 放宽 5% daily loss / 30% DD；
- 放宽 margin/cash gate；
- 围绕同一两年继续扫 governor / cap / meta。

下一步的决策变量应来自真实账户，而不是历史目标函数。

## 9. 当前最高信息价值证据

1. 新发生、此前未参与选择的未来数据；
2. 多日 CTP Shadow；
3. realized turnover/slippage/commission/tracking；
4. 真实 Broker margin / gross guard / daily circuit；
5. 测试柜台订单生命周期；
6. 极小真实仓位。

未来如需调整生产风险参数，应基于这些新证据。107.4623% 和 108.8461% 都只是已观察历史结果，Stress 0.9249% + halt 也必须同时纳入判断。
