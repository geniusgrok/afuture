# Stress90 有界稳健性研究设计

## 状态与目标

本研究精确继承 `main@b4207abb50aca1e39d5ebba3affc04765857251a` 的 Stress80 Production research checkpoint。当前固定基线为 Base `128.119261%`、Stress `80.067891%`、Stress DD `29.727688%`、turnover `337,934,465`、net alpha / turnover `30.990722 bps`。任何未满足晋级门的研究结果都不得改变 `main`。

晋级最低门为 Stress full_recent `>=85%`，理想目标为 `>=90%`，同时 Base `>=100%`、Stress train/validation/OOS 全部为正、所有指定窗口 DD `<=30%`、full_recent 不 HALT、margin rejects 为零、realized gross `<=2x`、max lots `<=35`。风险硬门、reduction-first、RiskManager 权限和 Broker/CTP 真值均不可放宽。

## 架构边界

研究分为四层，只有上一层给出通过证据才进入下一层：

1. **P0 behavior-neutral attribution**：一次重放当前 winner，导出原始 daily/events，不改变 target、lot、account 或 risk path；按产品、动作、持仓时长、风险触发、OI 支持、turnover bucket、capacity loss 分解已实现 gross PnL、精确成本、net alpha、turnover 与 realized drawdown-window contribution。
2. **Hypothesis ledger**：依据 P0 top 3 structural opportunity，在看到候选结果前记录 economic rationale、causal inputs、degrees of freedom、exact rule、expected mechanism、promotion gate、rejection condition。每个 family 只有一个 canonical implementation 和最多一个 correctness fix。
3. **Production quick gate**：每个通过 focused tests 的候选只运行 Base full_recent、Stress train/validation/OOS/full_recent 五个独立账户窗口。总 quick candidates `<=6`，family `<=3`，每 family `<=2`。
4. **Promotion**：仅 Stress `>=85%` 且其余硬门全部通过的候选可进入最多两次 full matrix。最终从最新未移动的 main 创建 clean promotion branch，仅复制 winner dependency closure，删除临时 workflow，完成 review 和永久树 Python 3.10/3.13 full CI 后 squash merge。

## P0 attribution 定义

P0 不做反事实重放，避免把 path-dependent capacity、equity、integer lots 与 risk state 伪装成可加收益。

- 产品：PnL event 与 trade event 的 `product` 为精确 ownership；net alpha = realized gross PnL - exact product transaction cost。
- 动作：`entry`、`exit`、`reversal`、`roll` 沿用 simulator audit；`resize` 依据同产品同方向 `abs(lots_after)` 相对 `abs(lots_before)` 进一步拆为 `increase` 或 `reduction`。
- PnL 动作归属：gap PnL 归属上一完成 session 的最后持仓动作；intraday PnL 归属当日 rebalance 后最后动作。该标签是持仓生命周期归因，不声称删除动作后的反事实收益。
- 持仓时长：按产品与方向的连续完成 session 计数，使用固定自然 bucket `1`、`2-3`、`4-5`、`6-10`、`>10`。
- 风险日：soft reserve freeze、daily circuit、gross guard、hard halt 分开；非触发日独立汇总。soft reserve 使用原 hard-limit-derived 25% 规则重新从已完成账户收益识别，仅作标签。
- OI 支持：固定 9 个 supported products 与 unsupported products；不按收益选品。
- turnover bucket：仅作 ex-post 描述，正 turnover 日按样本四分位数标记 Q1-Q4，零 turnover 单列；bucket 不得作为后续因果规则的直接阈值。
- capacity：直接汇总 simulator 已记录的 integer rounding、35-lot clipping、unavailable contract、margin fitting、lot stabilization notional loss。
- DD contribution：先从 realized equity path 找出 peak-to-trough 区间，再在该区间汇总产品/动作的 realized gross PnL 与 exact cost；报告 additive dollars，不伪造独立 counterfactual DD 百分点。

所有维度必须通过 reconciliation：gross PnL、turnover、cost、net alpha 与 simulator 总账一致；结果必须复现候选 digest 和 Stress80 headline。

## 允许的 hypothesis family

P0 完成前不选择具体规则。P0 后最多从以下路线中选择三个 family，并在 ledger 中冻结 canonical rule：

1. 低自由度 causal regime response：只允许 completed market trend、breadth、realized volatility、OI participation breadth、correlation/sector concentration 的 sign、median、zero crossing 或 expanding percentile 自然状态。
2. 生命周期/turnover：只允许 completed stateful continuity、same-direction carry-forward，或由 exact saved cost 与 lost gross alpha break-even 直接推导的 deadband；不得使用任意 hysteresis grid。
3. 真正新 PIT 信息覆盖：仅按 exchange、contract lifecycle、availability class 扩展，不按历史 product PnL 挑品。

禁止重启已存在负证据的 MPV、generic no-trade suppress-only、turnover-first、generic integer、hard-feasible margin、all-DCE OI/freezing、candidate-owned feedback 等路径。

## 验证与停止

- 小修改只运行 focused unit tests。
- P0 milestone 运行 attribution reconciliation tests 与一次 Stress full_recent replay。
- 候选通过 cheap/focused gate 后才消耗一次 Production quick run。
- 六个 quick candidates 均未达到 Stress `85%` 时立即停止收益优化，只保留负证据与 next-data recommendation。
- 只有最终候选才运行 full matrix；永久树全仓 CI 在稳定候选上运行一次。
- 研究代码不得被 live runtime、`runtime_factory.py`、`directional_runtime.py` 或 `execution_aligned_runtime.py` 导入。

