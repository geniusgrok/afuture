# 当前离线压力研究证据（历史代号 Stress-90）

> 阅读说明：`Stress-90` 的“90”表示本轮预先设定的压力情景年化收益目标不低于 90%，不是 90 个基点成本、90% 保证金或实盘风险等级。标准情景使用单边 5 个基点成本和固定 12% 保证金比例假设；压力情景使用单边 15 个基点和 15% 假设。本文保留文件名、输出字段和研究代号，便于与程序结果核对；术语定义见 [`glossary.md`](glossary.md)。该历史 checkpoint 的状态为 `production_wiring=false`，候选当时没有接入实盘；后续 productionization 不倒写这一事实。历史 evaluator 保留的 `Production` role 名称只是兼容标签，输出固定声明 `evidence_scope="historical_research_only"`、`live_authorized=false`、`risk_increase_authorized=false`、`prospective_evidence=false`。

## 2026-08-27 productionization 验证记录

本节记录后续实盘安全接线的代码证据，不重跑、替代或倒写下文的固定历史研究矩阵。
最终代码候选位于 PR #29 的远端提交
`48a16d181c0bf7820fd156714d0c45177aa8ad28`，对应源码 tree
`87da8c84fa341640e32aeb908badc9a5d818554c`；base `main` 仍为
`da8de59304963c7b1d6737a63e8dadd6eaecd860`。最终独立 adversarial review 为
Critical 0、Important 0，定向测试 `312 passed`。GitHub Actions run
`33044030711` 的 quality、Python 3.10 和 Python 3.13 均成功。

稳定代码候选只运行了一次最终 L4，结果为：

```text
pytest                         1554 passed, 1 skipped
ruff check                     passed
ruff format --check            247 files already formatted
mypy afuture                   102 source files clean
compileall                     passed
pip check                      no broken requirements
wheel + sdist build            passed
all five example configs       passed
Shadow zero-write smoke        4 passed
fixed-pair and Auto replay     4 trades, flat positions, zero margin
current CLI help               passed
```

这些结果证明代码候选及其 fail-closed 门禁通过离线工程验收，不授权真实资金。目标 ABI
仍无法提供权威的 prior-day final funding/settlement witness，也没有 official immutable
nonadjacent session ledger；相应 settlement rollover 和周末/节假日 continuity 继续在
Broker 构造或状态推进前失败关闭。目标机 CTP ABI、multi-day Shadow、测试柜台、真实费率/
保证金、FAK/reconnect、极小真钱和 risk-scale 审批证据仍未完成。

五份固定历史输入在本次 workspace 中仍不存在，因此本次 productionization **没有复现**
候选 SHA、Stress-90 `112.100053%` 或 Stress-80 `80.067891%`。下文数值仅是既有固定输入
历史证据，不是本次运行结果，也不是未来收益承诺。

## 结论

本轮将“基于历史集中度冻结新增风险”确定为当前离线研究候选。它超过预设的压力情景 90% 年化目标，使两个更早历史窗口转为正收益，并保持实盘运行链不变。

这是一份固定输入下的研究结论，不是实盘收益承诺，也不代表离线策略已经获得真实资金权限。文中的门禁 `passed` 只表示冻结历史研究契约通过，不授权实盘、扩大风险、前瞻验证或投产。

## 固定候选

候选继承前一版的目标构造：

1. 9 个品种的 60 分钟价格与持仓量方向确认；
2. D 日信号最早在 D+1 执行；
3. 开仓、同方向加仓和反转都需要确认；
4. 固定使用此前 20 个完整交易日判断成本后是否值得交易；
5. 固定使用 3 个交易日作为收益观察范围；
6. 固定要求覆盖单边 15 个基点成本；
7. 存活模板优先降低目标偏差，其次降低换手。

本轮只增加一个没有拟合参数的因果状态：

- 以当前目标权重绝对值计算标准 HHI 集中度；
- 与所有严格早于当前时点的有限 HHI 中位数比较；
- 当前 HHI 不高于历史扩展中位数时，只冻结新开品种和同方向加仓；
- 减仓、退出、反转和同品种换月仍可执行；
- 初始历史不足时不冻结；
- 无论候选是否成交，每个因果目标日都会推进集中度历史，避免账户结果反向污染信号状态。

回撤预留固定为 25%，即 30% 总回撤硬限制减去 5% 单日亏损预留。它使用全部已经完成的因果账户收益，而不是只看最近两日。最终候选各窗口均未触及 25% 软边界，因此这项正确性修复没有改变本矩阵结果。

候选权重 SHA256：`8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`。

## 固定输入

| 文件 | SHA256 |
| --- | --- |
| `broad_daily_universe.csv` | `c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f` |
| `return_target_specific_contracts.csv` | `f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64` |
| `execution_aligned_weights.csv` | `250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1` |
| `prior_two_year_broad_60m.csv` | `3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1` |
| `two_year_broad_60m.csv` | `5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b` |

研究没有伪造现货、库存、保证金、持仓或成交记录。

## 如何复核

上述五个输入文件不提交到 Git。仓库中的下载工具可以重新抓取部分日线数据，但外部数据源会修订，且两个 60 分钟文件没有完整的公开重建链。因此：

- 拥有与表中摘要一致的五个归档文件时，可以精确复核固定候选和账户矩阵；
- 只有当前 Git 仓库时，不能声称已经完整复现这份历史结果；
- 重新下载得到摘要不同的数据属于一次新的研究输入，必须另行记录，不能覆盖本证据。

将五个文件放入 `runtime/` 后，先核对摘要，再分别运行七个独立账户窗口：

```bash
sha256sum \
  runtime/broad_daily_universe.csv \
  runtime/return_target_specific_contracts.csv \
  runtime/execution_aligned_weights.csv \
  runtime/prior_two_year_broad_60m.csv \
  runtime/two_year_broad_60m.csv

python tools/evaluate_directional_stress90_final.py --scenario base   --window full_recent --output runtime/stress90/base_full_recent.json
python tools/evaluate_directional_stress90_final.py --scenario stress --window prior1      --output runtime/stress90/stress_prior1.json
python tools/evaluate_directional_stress90_final.py --scenario stress --window prior2      --output runtime/stress90/stress_prior2.json
python tools/evaluate_directional_stress90_final.py --scenario stress --window train       --output runtime/stress90/stress_train.json
python tools/evaluate_directional_stress90_final.py --scenario stress --window validation  --output runtime/stress90/stress_validation.json
python tools/evaluate_directional_stress90_final.py --scenario stress --window oos         --output runtime/stress90/stress_oos.json
python tools/evaluate_directional_stress90_final.py --scenario stress --window full_recent --output runtime/stress90/stress_full_recent.json
```

每个输出都会校验候选摘要、固定风险约束，并携带上述四个研究授权边界字段。组装器要求每个窗口的字段值和精确类型都一致，否则失败关闭。可用以下命令组装最终门：

```bash
python - <<'PY'
import json
from pathlib import Path
from tools.evaluate_directional_stress90_final import assemble_matrix_payload

paths = sorted(Path("runtime/stress90").glob("*.json"))
matrix = assemble_matrix_payload(
    json.loads(path.read_text(encoding="utf-8")) for path in paths
)
print(json.dumps(matrix["gate"], ensure_ascii=False, indent=2))
PY
```

预期历史研究输出为 `passed: true` 且 `reasons` 为空；这不改变四个否定授权字段。执行环境、代码版本、输入摘要或依赖版本变化时，应把结果视为新的证据，而不是强行解释为数值误差。

## 完整账户模拟矩阵

七行均为独立账户模拟。标准情景使用单边 5 个基点和固定标准保证金比例；压力情景使用单边 15 个基点和固定压力保证金比例。

| 情景与窗口 | 年化收益 | 最大回撤 | 毛收益 | 成本 | 净收益 | 换手金额 | 每换手净收益 | 最高总敞口 | 硬停机 | 保证金拒绝 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 压力前序一 | 12.141524% | -23.228982% | 122,085.00 | 66,958.75 | 55,126.25 | 44,639,165 | 12.349302 bps | 1.677099 倍 | 否 | 0 |
| 压力前序二 | 8.578529% | -18.354608% | 111,235.00 | 69,935.80 | 41,299.20 | 46,623,865 | 8.857953 bps | 1.648349 倍 | 否 | 0 |
| 标准汇总窗口 | 156.881655% | -15.708467% | 2,693,765.00 | 132,381.86 | 2,561,383.14 | 264,763,725 | 96.742223 bps | 1.983123 倍 | 否 | 0 |
| 压力训练 | 28.891985% | -13.657897% | 229,025.00 | 91,023.25 | 138,001.75 | 60,682,165 | 22.741732 bps | 1.648285 倍 | 否 | 0 |
| 压力验证 | 512.267292% | -11.783634% | 726,940.00 | 50,469.45 | 676,470.55 | 33,646,300 | 201.053474 bps | 1.626864 倍 | 否 | 0 |
| 压力样本外 | 102.808956% | -17.632605% | 268,380.00 | 62,293.73 | 206,086.28 | 41,529,150 | 49.624487 bps | 1.649642 倍 | 否 | 0 |
| 压力汇总窗口 | 112.100053% | -14.567214% | 1,929,280.00 | 310,257.23 | 1,619,022.78 | 206,838,150 | 78.274862 bps | 1.670510 倍 | 否 | 0 |

组装后的晋级门结果为通过，拒绝原因列表为空。

前一版候选在相同干净代码树上仍得到：压力情景年化 80.067891%、最大回撤 -29.727688%、换手 337,934,465、每换手净收益 30.990722 个基点、无硬停机且无保证金拒绝。因此新规则是在继承前一版信号和硬限制的基础上增加，而不是回退已有结果。

## 相比前一候选的变化

| 指标 | 前一候选 | 当前候选 | 变化 |
| --- | ---: | ---: | ---: |
| 压力汇总窗口年化收益 | 80.067891% | 112.100053% | +32.032162 个百分点 |
| 压力汇总窗口最大回撤 | -29.727688% | -14.567214% | 改善 15.160474 个百分点 |
| 压力净收益 | 1,047,283.30 | 1,619,022.78 | +571,739.47 |
| 压力换手金额 | 337,934,465 | 206,838,150 | -131,096,315 |
| 每换手净收益 | 30.990722 bps | 78.274862 bps | +47.284141 bps |
| 前序一 | -32.117204% / 硬停机 | 12.141524% | 转正且未停机 |
| 前序二 | -29.445651% / 硬停机 | 8.578529% | 转正且未停机 |

收益改善不是简单压低换手：近期较集中的开仓和加仓仍是主要收益来源。新规则只抑制了在前序一、前序二和汇总窗口中净贡献都为负的分散领导状态新增风险。

## 时间因果和防过拟合约束

- 当前目标权重在决策时已经可见，比较只使用严格早于当前时点的 HHI；
- 先比较，再把当前 HHI 加入历史；
- 验证和样本外账户只接收窗口开始前的目标状态历史；
- 追加未来数据不能改变过去的目标状态标签；
- 状态不读取候选持仓、已实现盈亏或结果标签；
- 没有搜索阈值、分位数、回看窗口、品种、杠杆、保证金、持仓量设置、成本门、收益观察范围或回撤预留；
- 允许的三类假设只使用了两类，六个快速候选只使用了两个；达到预设目标后停止；
- 完整收益路径的回撤预留只测试一次，作为单独策略时失败，没有继续调参挽救。

## 风控与实盘边界

以下硬限制没有变化：

- 目标和实际总敞口不超过权益 2 倍；
- 保证金不超过权益 35%，可用资金不低于权益 25%；
- 单日亏损硬限制 5%；
- 总回撤硬限制 30%；
- 单合约不超过 35 手；
- 减仓先于开仓；
- Broker/CTP 是持仓、资金和成交的唯一真相；
- `RiskManager`、单日熔断和 `HALTED` 的权限没有变化。

当前候选只存在于离线验收组件，没有修改实盘策略，也没有把研究 CSV 当成实盘数据源。未来若接入实盘，仍需验证启动预热、重启恢复、60 分钟数据缺失或陈旧、交易时段、交易日历、换月以及线上线下一致性。

## 失败路线

[`stress90-bounded-research-evidence.md`](stress90-bounded-research-evidence.md) 保存研究次数、最高优先级归因、因果市场状态、单独回撤预留失败结果及继承的失败路线。没有通过调整参数挽救失败候选。

## 验证记录

- 固定输入矩阵：通过，使用 1 次完整矩阵额度；
- 相关机制和单元测试：通过；
- 审查发现并修复一项矩阵载荷未能失败关闭的问题，没有遗留行为问题；
- Python 3.10 和 Python 3.13 的完整持续集成均通过；
- 最终研究合并提交为 `482455dc57bc6a134f45232e290b4a49c3f7073d`，该提交没有修改实盘接线。
