# 压力研究过程与失败证据（历史代号 Stress-90）

> 阅读说明：本文记录有界研究过程、使用次数和被拒绝路线。`Stress-90` 的数字表示预先设定的压力情景年化收益目标，不表示压力参数。原始研究字段保留英文以便与输出文件核对；术语定义见 [`glossary.md`](glossary.md)。

## 基线

- Base commit: `b4207abb50aca1e39d5ebba3affc04765857251a`.
- Production checkpoint: Stress80 final candidate; live runtime wiring unchanged.
- Candidate weight SHA256: `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`.
- Exact pre-research Stress reproduction: annualized `80.067891%`, DD `-29.727688%`, turnover `337,934,465`, cost `506,901.6975`, net alpha `1,047,283.3025`, efficiency `30.990722 bps`, gross peak `1.683773x`, no HALT and zero margin rejects.

## 有界研究次数

- Major hypothesis families used: `2 / 3`.
- Production quick candidates used: `2 / 6`.
- Final full matrices used: `1 / 2`.

## 不再重复尝试的失败路线

PR #21/#23 and the inherited evidence lock candidate-owned MPV, generic/no-trade suppress-only, generic integer tracking/floor/ceil/lot feasibility, exact hard-feasible margin, turnover-first survivor allocation, all-DCE OI/freeze, OI-selective freeze, raw member flow, standalone Price×OI×Volume/session portfolios, naive curve/basis and Candidate A/B product selection. None was retuned.

## 最高优先级的收益来源归因

Behavior-neutral independent Stress account paths reconcile `initial equity + gross PnL - exact cost = final equity`:

| Window | Annualized | Max DD | Gross PnL | Cost | Net alpha | Turnover | Net/turn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| prior1 | -32.117204% | -30.807368% | -83,935.00 | 64,975.88 | -148,910.88 | 43,317,255 | -34.376805 bps |
| prior2 | -29.445651% | -30.144016% | -89,720.00 | 53,086.41 | -142,806.41 | 35,390,940 | -40.351121 bps |
| full_recent | 80.067891% | -29.727688% | 1,554,185.00 | 506,901.70 | 1,047,283.30 | 337,934,465 | 30.990722 bps |

The prior failure is gross expectancy, not mainly cost: entry plus increase gross is `-87,910` in prior1 and `-62,105` in prior2, versus `+1,416,250` recent. Recent increases alone contribute `+726,912.95` net, so generic turnover suppression would delete valid alpha. Recent AG and FU net alpha totals more than the portfolio because other products subtract value, identifying product leadership without authorizing product selection. Fixed holding cutoffs are not stable across windows. Capacity losses are material, but inherited generic integer/margin experiments already failed and were not reopened.

## 假设一：完整收益路径下的回撤预留

The inherited simulator kept only the last two completed returns, so its documented running-high-watermark 25% reserve triggered on zero days. Full causal history would have triggered on `162`, `170` and `32` decision days in prior1, prior2 and full_recent. The zero-degree-of-freedom correctness candidate kept `25% = 30% hard DD - 5% daily-loss reserve`, froze only entry/same-side increase, and left every hard gate unchanged.

| Window | Annualized | Max DD | Net alpha | Turnover | Net/turn |
| --- | ---: | ---: | ---: | ---: | ---: |
| Base full_recent | 128.778098% | -25.691466% | 1,950,608.52 | 460,472,955 | 42.360979 bps |
| Stress train | 10.177351% | -24.231874% | 48,772.07 | 118,355,285 | 4.120819 bps |
| Stress validation | 511.519900% | -16.988818% | 675,792.17 | 57,748,555 | 117.023217 bps |
| Stress OOS | 44.102450% | -24.165871% | 97,608.47 | 64,844,355 | 15.052732 bps |
| Stress full_recent | 0.829912% | -26.246962% | 8,000.23 | 137,739,850 | 0.580821 bps |

The full_recent account reaches the reserve and then permits reductions/exits while blocking recovery re-entry. The response self-extinguishes and fails both headline and efficiency gates. No threshold rescue was attempted. The winner below fixes the reserve definition without changing its measured path because every winner DD stays above the 25% boundary.

## 重要市场状态归因

Causal labels used only current targets, prices completed before the decision day, the already-fixed 20-session horizon, zero/sign/unanimity boundaries, or a strictly prior expanding median. Appended future data and current-day close shocks cannot alter an earlier label. Market-trend sign, signal/trend alignment, exact directional unanimity and realized-volatility state did not isolate a stable positive prior regime.

Target-weight HHI relative to its strictly prior expanding median produced one stable opportunity for entry plus same-side increase:

| Window | Concentration state | Gross PnL | Cost | Net alpha | Turnover |
| --- | --- | ---: | ---: | ---: | ---: |
| prior1 | above prior median | -35,680.00 | 8,878.99 | -44,558.99 | 5,919,325 |
| prior1 | not above prior median | -60,030.00 | 20,420.01 | -80,450.01 | 13,613,340 |
| prior2 | above prior median | -25,005.00 | 5,805.51 | -30,810.51 | 3,870,340 |
| prior2 | not above prior median | -36,900.00 | 18,455.52 | -55,355.52 | 12,303,680 |
| full_recent | above prior median | 1,447,945.00 | 120,484.58 | 1,327,460.42 | 80,323,055 |
| full_recent | not above prior median | -50,825.00 | 123,958.46 | -174,783.46 | 82,638,970 |

## 假设二：基于历史中位数的新增风险冻结

- **Economic rationale:** diffuse leadership loses after 15bp in all diagnostic windows, while concentrated recent leadership preserves the main alpha engine.
- **Causal inputs:** absolute current causal target weights and earlier target HHI only. No realized candidate PnL, holdings outcome or future data.
- **Degrees of freedom:** zero fitted thresholds and no new lookback. Standard HHI is compared to its strictly prior expanding median; equality is weak leadership; insufficient initial history passes.
- **Exact rule:** if current HHI is no greater than the median of earlier finite HHI, freeze new product entries and same-sign increases. Reductions, exits, reversals, same-product rolls and all hard-gate actions pass. State updates on every causal target day, independently of account actions.
- **Rejection rule:** any declared gate failure rejects the candidate. No percentile, lookback, equality, product, transform or weight rescue was allowed.

Quick evidence:

| Window | Annualized | Max DD | Gross PnL | Cost | Net alpha | Turnover | Net/turn | Gross peak | HALT | Rejects |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| Stress prior1 | 12.141524% | -23.228982% | 122,085.00 | 66,958.75 | 55,126.25 | 44,639,165 | 12.349302 bps | 1.677099x | false | 0 |
| Stress prior2 | 8.578529% | -18.354608% | 111,235.00 | 69,935.80 | 41,299.20 | 46,623,865 | 8.857953 bps | 1.648349x | false | 0 |
| Base full_recent | 156.881655% | -15.708467% | 2,693,765.00 | 132,381.86 | 2,561,383.14 | 264,763,725 | 96.742223 bps | 1.983123x | false | 0 |
| Stress train | 28.891985% | -13.657897% | 229,025.00 | 91,023.25 | 138,001.75 | 60,682,165 | 22.741732 bps | 1.648285x | false | 0 |
| Stress validation | 512.267292% | -11.783634% | 726,940.00 | 50,469.45 | 676,470.55 | 33,646,300 | 201.053474 bps | 1.626864x | false | 0 |
| Stress OOS | 102.808956% | -17.632605% | 268,380.00 | 62,293.73 | 206,086.28 | 41,529,150 | 49.624487 bps | 1.649642x | false | 0 |
| Stress full_recent | 112.100053% | -14.567214% | 1,929,280.00 | 310,257.23 | 1,619,022.78 | 206,838,150 | 78.274862 bps | 1.670510x | false | 0 |

The frozen quick gate passes with no reasons. Stress full_recent improves by `32.032162` percentage points, DD by `15.160474` points, turnover by `131,096,315`, net alpha by `571,739.47`, and efficiency by `47.284141 bps`. Both prior windows become positive and stop HALTing. The ideal `>=90%` target is met, so no third hypothesis family or extra quick candidate was run.

## 结论

The clean seven-window matrix reproduces the quick values exactly and the assembled frozen gate passes with no reasons. The remaining work is permanent-tree review/CI and merge identity verification; no second matrix or further hypothesis is authorized.
