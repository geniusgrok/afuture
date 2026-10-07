# 持续改造的离线研究路线

本轮按不同原因推进预算对照、持仓退出、供需版本资格和双腿相对价值研究。
每个候选在经济输出前冻结机制和输入；失败结果仍保留。原经济目标及生产
默认不由这些工具修改，历史开发结果不能作为未见样本或真钱许可。

## 已有研究的复用

具体合约完整持仓收益评分已经在 C2 做过并被经济否决，不重新包装成新方向。
C4B 保留原成本批准幅度、剩余预算留空，已有原历史结果，本轮仅做近期固定
规格对照。D/Dlot 趋势和单腿 carry G 的失败也继续有效。

## 研究工具

`tools/adaptive_alpha_research.py` 使用保存的 B0 分层权重和完成日 OI 推进
后续信号；ex-AG 在排序前重建品种池，核对历史前缀。B0 与 C4B 分别使用
自身严格更早的目标 HHI 预热，初始账户均为空仓 50 万元。工具复用原账户
模拟器，逐笔核对成交、整数手、同合约隔夜/日内损益、成本和每日权益。

```bash
python tools/adaptive_alpha_research.py \
  --baseline "$AFUTURE_BASELINE_RUN" --prior "$AFUTURE_PRIOR_PAIRED" \
  --output "$AFUTURE_NEW_RUN/recent"
```

`tools/pair_episode_research.py` 消费预先冻结、由前一完成日生成的具体合约
配对。当前假设使用相同两个合约的 63 个完整收盘观察、一次标准差及压力
往返成本门槛，下一真实开盘一手一腿；在先前收盘跨过固定入场均值、失去
流动性/期限资格或完成 20 个持有日后，在下一开盘退出。每腿均计费，保证金
不抵消，未平仓过程只估值，不补造期末成交。合并名义超过 10 万元则不入场。

```bash
python tools/pair_episode_research.py \
  --market "$AFUTURE_SPECIFIC_MARKET" --selections "$AFUTURE_PRIOR_PAIRS" \
  --units "$AFUTURE_UNIT_AUDIT" --mechanism calendar \
  --output "$AFUTURE_NEW_RUN/calendar"
```

各品种是独立的单手观察过程，不能相加后称账户收益。已有持仓缺少真实价格
时明确失败；其他品种可以独立诊断，但缺口仍阻止宣称完整品种池验证。
同品种双腿不能交给当前单合约产品净权重接口；这些结果不证明生产多腿执行。
RB/HC 同交割月一手对一手用于吨数相等的钢材相对价值假设，仍有品种基差风险。

`tools/holding_exit_research.py` 在原账户模拟器的最终目标上施加只减仓退出。
它从实际已有持仓读取先前完成日行情，以 20 个同合约观察的固定平均真实波幅
为尺度；完成日价格先向有利方向移动至少一个波幅后，若从有利峰值回撤一个
波幅，则在下一真实开盘退出。同侧原目标未归零或反转前禁止再入。其余原账户
约束保持，未使用历史最高价假定成交。

```bash
python tools/holding_exit_research.py \
  --recent "$AFUTURE_NEW_RUN/recent" --market "$AFUTURE_SPECIFIC_MARKET" \
  --units "$AFUTURE_UNIT_AUDIT" --output "$AFUTURE_NEW_RUN/E1_recent"
# 原980日同时重跑相同真实合约单位的B0对照：
python tools/holding_exit_research.py \
  --recent "$AFUTURE_NEW_RUN/recent" --market "$AFUTURE_SPECIFIC_MARKET" \
  --units "$AFUTURE_UNIT_AUDIT" --output "$AFUTURE_NEW_RUN/E1_historical" --historical
# 固定C4B预算和同一退出机制在单个账户中的组合：
python tools/holding_exit_research.py \
  --recent "$AFUTURE_NEW_RUN/recent" --market "$AFUTURE_SPECIFIC_MARKET" \
  --units "$AFUTURE_UNIT_AUDIT" --output "$AFUTURE_NEW_RUN/E2_historical" \
  --historical --parent C4B
```

组合版本 E2 只更换已冻结的目标父配置，退出尺度与规则保持 E1。原980日
同时重跑 C4B 对照，始终由同一个原账户模拟器重算资金与持仓，不能拼接
C4B 与 E1 的独立账户收益，也不能按成本或是否含白银切换机制。

## 后续分流

- 数据不足：保留失败输入与缺口，继续其他独立方向。
- 毛收益不足：更换有不同经济依据的机制，不先改费用假设。
- 毛收益为正但净收益不足：有明确周转证据才修改成交或持有机制。
- 局部净贡献成立：先做对应机制消融及统一账户容量验证，再按原四格验收。
- 原四格通过：继续原定逐品种剔除、固定双剔除、集中度与广度检查。

供需来源使用实际捕获时间和同口径单位核验；当前抓到的旧统计日期不能倒填为
过去已知。自然时间中新发布的有效版本和未来收益必须实际发生。开发历史上的
单手就绪检查只决定是否值得进一步研究，不能代替统计认证或原经济合同。
