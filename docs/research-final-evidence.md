# afuture 研究证据总览

本文件保留 2026-08-21~2026-08-23 的主要研究与生产机械结论。旧套利专项没有被删除或改写；Execution-Aligned Directional 是另一条账户互斥策略链。

## 1. 全局结论

当前必须同时成立的事实：

1. corrected M/OI、经济 pair、BU/FU、intraday、结构套利等市场中性路线没有接近 100% 年化；
2. 50 品种 directional 在明确允许历史选择偏差、gross≤2x 的 specific-contract / next-open float-notional 口径达到 **107.4623% 年化 / 27.4097% 最大回撤**；
3. 当前 production-mechanics Base 在相同两年区间达到 **109.0636% 年化 / 15.8529% 最大回撤 / actual gross peak 1.998253x / no permanent halt**；
4. 当前 production-mechanics Stress 在 15bp + 15% margin proxy 下达到 **28.9559% 年化 / 28.1152% 最大回撤 / actual gross peak 1.668769x / 474 active days / no permanent halt**；
5. Stress 已从旧版 `0.9249% + margin HALT` 修复为完整运行，但仍没有达到 80%；
6. 两年历史和所谓 OOS 都已经被研究流程观察，不是 pristine holdout，所有高收益数字都不是未来收益保证。

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
- Base score=`0.25 × annualized + 1.0 × Sharpe`；
- completed continuous intraday Base/Stress cost evidence causal；
- 15bp Stress 只做生存门，通过后按 Base score 排名；
- point-in-time concrete-contract selection；
- 20 天黑窗；
- target gross≤2x。

## 5. Float-notional L4

`2024-08-21 ~ 2026-08-20` 原官方 artifact：

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |

Final OOS 已被观察，因此 `pristine_final_oos=false`。

## 6. Production-mechanics 收口

没有通过增加 leverage 或放宽硬门解决 research/live gap。当前生产机械包括：

- D 日 completed activity 决定 D+1 concrete contract；
- required signal day = completed activity day；
- stale/missing evidence fail-closed；
- reductions 优先；
- Broker 是唯一 order/fill/position truth；
- integer lots / multiplier / max contract volume 35；
- **adaptive margin-aware target sizing**：当前 35% margin / 25% available / 5% daily-loss 配置下，无历史/平静 target margin share 上限为 30%，completed shock 高于 3% 时只进一步收缩；
- live 使用 Broker side-specific margin metadata；历史使用显式 12%/15% proxy；
- margin-fitted openings 仍由原 `RiskManager` 35%/25% hard gates 最终否决；
- 5% daily-loss 是同交易日 circuit，后续交易日满足完整安全条件才恢复；
- total DD / margin / available cash 等保持 hard/manual halt；
- completed-return governor：最近完成日收益≤-2% 或两日样本波动≥3% → 下一目标 25%，否则 100%；
- actual marked gross>2x 时运行时只减仓；
- 同一合约多空毛仓 flatten 按毛仓分别平；
- directional rebalance/fill/cycle execution quality 保持观测闭环。

## 7. 最终 Production L3

固定 run `32634296589`，artifact id `9491959916`：

| 指标 | Base | Stress |
|---|---:|---:|
| 年化 | **109.0636%** | **28.9559%** |
| 累计 | **312.2285%** | **62.9735%** |
| 最大回撤 | **15.8529%** | **28.1152%** |
| Sharpe | **2.0976** | **0.9604** |
| active days | **478 / 484** | **474 / 484** |
| daily circuit days | 3 | 2 |
| defensive days | 77 | 70 |
| margin reject days | 0 | **0** |
| actual gross peak | **1.998253x** | **1.668769x** |
| halted | **false** | **false** |

Base 直接通过 annualized≥100%、DD≤30%、actual gross≤2x、no permanent halt。

Stress 的重要变化不是“收益达到 80%”，而是它不再因 2x target 与 35% margin gate 的结构冲突很早退出；现在是一个完整运行的 15bp/高 margin 压力账户实验。

## 8. 本轮负证据

为了验证成本鲁棒性，而不是盲目保留每个想法，本轮测试并拒绝了：

- Base/Stress score 50/50：Base 年化约降至 **94.59%**；
- 只保留 rebalance `>=5` 的 57 templates：Base 年化约降至 **69.78%**。

这两者没有进入最终生产候选。最终保留全部 96 templates，只要求 Stress evidence 存活，排序仍由 Base score 决定。

## 9. 为什么现在停止历史优化

最终候选已经完成：

- Base ≥100%；
- Base DD <30%；
- Base/Stress 不永久 HALT；
- Stress margin reject = 0；
- gross ≤2x；
- 35% margin / 25% available / 5% daily loss / 30% DD 均未放宽。

Stress 年化提升到 28.9559%，仍未达到 80%。本轮已经把可解释的执行效率方向逐项实现并用固定 L3 晋级/否决；继续围绕同一已观察历史扩展 template、meta、governor 或门槛直到得到 80%，增加的是过拟合而不是新信息，因此不再追加同历史参数搜索。

## 10. 当前最高信息价值证据

1. 新发生、此前未参与选择的未来数据；
2. 多日 CTP Shadow；
3. realized turnover/slippage/commission/tracking；
4. Broker 实际 margin 与 soft target headroom；
5. gross guard / daily circuit；
6. 测试柜台订单生命周期；
7. 极小真实仓位。

未来如需调整生产风险参数，应基于这些新证据。107.4623%、109.0636% 和 28.9559% 都只是已观察历史结果，不是未来年度收益承诺。

## 11. 2026-08-23 Execution-efficiency 最终晋级

相对进入本轮前的 `main` commit `b6b2cdca0f04193c10e14f8b3ad61902d6e36769`：

- Base 年化：108.8461% → **109.0636%**（+0.2175 个百分点）；最大回撤 17.8010% → **15.8529%**；
- Stress 年化：20.4057% → **28.9559%**（+8.5502 个百分点）；最大回撤 27.9925% → **28.1152%**；
- Stress active days：472 → **474 / 484**；margin rejects 仍为 **0**；no permanent HALT；
- hard gates 保持 2x gross / 35% margin / 25% available / 5% daily loss / 30% DD / 35 lots。

最终固定证据：workflow run `32634296589`，PR head `921bd8b4820a8c1efa9c804a8ea1a1b56c2d188f`，PR merge ref `1ec387433ee5011e44bc214e4e4c83a28e72c93c`，artifact id `9491959916`，artifact `stress-80-l3-1ec387433ee5011e44bc214e4e4c83a28e72c93c`，SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`。

Stress turnover attribution（notional）：entry/exit `189,920,240`、resize `51,722,155`、reversal `11,314,145`、roll `2,521,010`、daily circuit `1,440,740`，总计 `256,918,290`；所有 bucket 与总 turnover 精确闭合。

最终**保留**：turnover attribution、completed-day contract-roll hysteresis、same-sign `+1 lot` increase no-trade、completed-return shock adaptive margin contraction。最终**拒绝并回退**：product replacement persistence、cost-aware meta hysteresis、same-direction weight resize hysteresis。前两者固定 L3 分别把 Base 压至约 58.41% 与 58.32%；meta hysteresis 还令 Stress 为 -13.03%、DD 超 30% 并 HALT。

因此 28.9559% 仍不是 80%。本轮证据反而证明高换手中有相当部分是有效 Alpha 迁移，不能无限压低 turnover；在同一已反复观察历史上继续调整门槛直到得到 80% 会增加 selection bias，而不是提高实盘可信度。

## 12. PR #15 Net-alpha efficiency 最终研究结论

本轮没有生产收益“After”提升。原因不是停止过早，而是所有预声明、具有经济依据的新增方向均已有负证据：

1. entry/exit 诊断没有稳定的 causal low-quality cohort；
2. net-edge estimator 在 prior2 发生 -1.7128pp 的 qualified-vs-rejected 5-session separation；
3. slow trend、confirmed trend、cross-sectional momentum、breakout confirmation、carry、carry+trend、strength-ranked trend 均在 prior/OOS/15bp 下不稳定；
4. shock-derived margin candidate 固定 Stress 只有 4.7970% 年化并 permanent HALT。

按停止规则，不再增加参数自由度。PR #15 最终保留的生产变更仅是 behavior-neutral attribution；经济行为与 PR #14 完全等价，Base/Stress 仍为 109.0636% / 28.9559%。详细表格、产品贡献、entry/exit cohort、负实验和 lineage bridge 见 [`directional-net-alpha-efficiency-evidence.md`](directional-net-alpha-efficiency-evidence.md)。


## 13. PR #16 P0-P2 收口

在不建设 5–10 年分钟/OI/curve 大仓库的约束下，P0–P2 其余方向已逐项评估：integer risk-capital projection、template consensus capacity、regime scalar、daily price×OI×volume、5 组预声明 relative-value、8-root bounded universe expansion 均未通过独立稳健门，因此全部不进入生产。P0 integer projection 虽把 Stress turnover 从 256,918,290 降至 240,725,360，但固定 Production Stress 年化降至 17.9238%，直接拒绝。bounded universe 中只有 SI 的 full_recent continuous proxy 有正增量，但 prior2/OOS 反向且 DD >50%，不晋级。

最终只保留 P2 execution engineering：正常 opening FAK 在对手一档深度覆盖整笔手数时使用 best opposite quote，否则回退 legacy aggressive tick；reduction FAK 完全不变。固定 15bp historical Stress 不为该 live execution 优化虚增收益，PR #15 的 109.0636% Base / 28.9559% Stress 继续作为经济基线。完整证据见 [`directional-p0-p2-evidence.md`](directional-p0-p2-evidence.md)。
