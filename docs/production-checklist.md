# 上线检查

检查须记录目标机、柜台性质、日期、实际命令/结果及对应源码和输入身份。未核验保持未勾选；离线、CI 和测试替身通过不能代替现场证据。具体操作见[CTP 运行](live-trading.md)和[Stress-90 手册](stress90-live-runbook.md)。

## 启动基础

- [ ] 最终 OS/CPU/Python 与受约束依赖完成原生 CTP import、真实登录和重连验证。
- [ ] 私有配置明确柜台、预期账户/币种、独占关系及风控；凭证不在代码、日志或备份中。
- [ ] runtime/registry/Shadow 隔离，单实例锁、磁盘、日志和通知可用。
- [ ] deployment identity 与源码、配置、constraints、原生模块、路径和 risk overlay 一致。
- [ ] `status` current/schema/sequence/checksum/identity 全部可信，没有 orphan/lineage/pending 事故。
- [ ] `doctor` fresh account/完整 positions/orders/trades/交易日、对账、metadata/quotes/费用/保证金通过，`orders_sent=0`。

## 方向和 Stress-90

- [ ] 上一完整日 activity、OHLC、OI 的日期/合约身份与 coverage 一致；未知不是零，缺 required day 不开仓。
- [ ] 固定输入/精确缺口与候选 parity 通过，seed 无历史账户收益；decision/intent exactly-once 已持久。
- [ ] 首窗和先减后开规则、全部 integer stages、gross/margin/available/日损/回撤/每合约上限合格。
- [ ] reserve schema 6 锁存、HHI freeze、risk overlay 和累计纯换月预算未被重置或绕过。
- [ ] 严格资金/session 证据已由真实受控来源认证；若选 operator-managed，明确它是独占账户零外部活动的 operator trust，未提升官方字段。
- [ ] 技术 permit 绑定当次证据；activation 本身仍 HALTED，不冒充现场授权。

## 现场生命周期

- [ ] 多日持久 Shadow 覆盖夜盘、跨日、正常停机、重启；vendor/CTP first/last/OI/量/dominant/flow 差异已解释。
- [ ] 专用测试柜台验证 entry/add/reduce/exit/reversal/roll、FAK partial、reject/cancel、重复/未知回报、Tick flood 和断线。
- [ ] 减仓未确认绝无开仓；成交/订单身份重启后不重记；今昨仓、方向和数量与柜台一致。
- [ ] prepared/首单/部分成交崩溃及恢复门通过，未知结果/迟到回报保持失败关闭。
- [ ] live 成交、手续费、保证金与计划差异以及结算单已核对，无法闭合资金变化不计为收益。
- [ ] HALTED 备份经语义验证，空目录恢复演练保持停机/失效 permit；未从 `.prev` 自动恢复。
- [ ] heartbeat、watchdog、通知失败和独立主机失联检测实测；真实一自然月证据来自现场时间与执行，服务存活/加速回放不能替代。

## 资金与继续运行

- [ ] 真实资金和风险规模已明确授权，commissioning 依据每手压力损失、实际成本/保证金和权益核定。
- [ ] `prepare-session` blockers、capacity report、状态和新技术许可均审查；错过首窗不追开 entry/add/reversal。
- [ ] 人工/外部订单、入出金或账户变化立即停机，使用带安全门的 lifecycle 操作，不静默重置。
- [ ] 显著恶化、身份/数据/持仓漂移或成本不可解释时停止新风险并处理，扩大风险须新证据和独立授权。
