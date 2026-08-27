from pathlib import Path


def insert_after(path: str, anchor: str, addition: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if addition.strip() in text:
        return
    if anchor not in text:
        raise SystemExit(f"documentation anchor missing: {path}: {anchor[:100]!r}")
    p.write_text(text.replace(anchor, anchor + addition, 1), encoding="utf-8")


def insert_before(path: str, anchor: str, addition: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if addition.strip() in text:
        return
    if anchor not in text:
        raise SystemExit(f"documentation anchor missing: {path}: {anchor[:100]!r}")
    p.write_text(text.replace(anchor, addition + anchor, 1), encoding="utf-8")


insert_after(
    "README.md",
    "固定 Stress-90 候选现在可以通过 `directional.policy = \"stress90\"` 显式接入 Shadow、测试柜台和 CTP runtime；普通 `execution_aligned` 仍保留原有行为。代码 wiring、历史候选验证、Shadow、测试柜台、极小真钱和扩大风险是六个不同阶段，前一阶段不能自动授权后一阶段。\n",
    "\nStress-90 账户连续性默认使用 `account_continuity_mode = \"strict\"`，保持现有官方结算/交易时段证据的 fail-closed 门。个人专用、账户独占且运行期间无人工交易和出入金时，可显式选择 `operator_managed`，通过 `stress90-operator-roll-forward` 记录带 checksum 的操作者信任连续性凭证；它不是交易所或 Broker 的官方结算/session 见证，成功后仍保持 `HALTED`、kill switch 开启，并要求重新运行 `status`、`doctor` 和签发新的技术 permit。任何外部账户活动都必须先走 `stress90-account-rebase`。\n",
)
insert_after(
    "README.md",
    "afuture stress90-settlement-roll-forward --help\n",
    "afuture stress90-operator-roll-forward --help\n",
)

insert_after(
    "docs/configuration.md",
    "| `AFUTURE_STRESS90_ORDER_EPOCH_ACK` | Stress-90 CTP order journal 容量 epoch 封存必需 | 必须为 `I_CONFIRM_STRESS90_CTP_ORDER_JOURNAL_EPOCH_ROLLOVER`，并同时传入 `--confirm-rollover`。 |\n",
    "| `AFUTURE_OPERATOR_CONTINUITY_ACK` | Stress-90 `operator_managed` 跨日必需 | 必须为 `I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY`，并同时传入 `--confirm-operator-continuity`。 |\n",
)
insert_after(
    "docs/configuration.md",
    "| `directional.account_exclusive` | `true` | 必须保持 `true`；同一账户不能混入手工或其他程序持仓。 |\n",
    "| `directional.account_continuity_mode` | `strict` | `strict` 保持官方结算/session 证据门；`operator_managed` 仅允许 `system.mode=live`、Stress-90、directional enabled 且账户独占，用操作者信任凭证推进跨日，但不伪造 Broker 验证字段。 |\n",
)
insert_after(
    "docs/configuration.md",
    "- `account_exclusive = true`；运行期间禁止手工交易、其他策略、充值和出金；\n",
    "- `account_continuity_mode` 默认 `strict`；`operator_managed` 只适用于个人专用、单账户、账户独占运行。其 receipt 明确标记为 `operator_trust`，不能替代官方结算见证或官方 session ledger；\n- `operator_managed` 期间一旦发生人工交易、外部委托、入金或出金，必须保持停机并先执行现有 `stress90-account-rebase`；跨日成功后仍需新的 Doctor 技术 permit；\n",
)

live_section = '''\n### 11.1 Stress-90 账户连续性模式\n\nStress-90 默认 `directional.account_continuity_mode = "strict"`。未配置该字段时行为与原有严格模式完全一致：缺少 prior-day final funding/settlement witness 或权威 nonadjacent session ledger 时继续 fail closed，现有 `stress90-settlement-roll-forward` 的含义不变。\n\n`operator_managed` 只面向个人专用、单账户、`account_exclusive=true` 的 live Stress-90 runtime。它记录本地 `stress90_operator_continuity.json` receipt，把当前 CTP 交易日、账户/epoch/runtime、registry/TDE、完整 session ownership、order journal、Broker/local 持仓对账、Deposit/Withdraw、OHLC/activity/OI/policy 对齐和操作者的“无人工交易、无外部委托、无入金、无出金”声明绑定在同一 checksum 链中。该 receipt 的 authority 是 `operator_trust`，**不是**交易所/Broker 官方结算见证或官方 session ledger；`external_activation_gates_completed` 始终保持 `false`。\n\n跨日必须显式执行 `afuture stress90-operator-roll-forward`，并同时提供 `--confirm-live`、`--confirm-operator-continuity`、唯一 64-hex `--operation-id`、`--operator-reason` 和 `AFUTURE_OPERATOR_CONTINUITY_ACK=I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY`。命令只在 `HALTED`、kill switch 开启、fresh account/positions/session ownership 完整、活动委托为零、journal 无未终态/unknown、Broker/local 持仓一致且当前 Deposit/Withdraw 都为零时推进；它不报单也不撤单。周五到周一等自然日间隔可以由 operator receipt 明确确认，但不能跳过已经存在的中间 OHLC/OI session 数据，也不使用本机日期、`BDay` 或静态节假日日历猜测交易日。\n\n成功后 generic/policy state 仍通过原有 recoverable lifecycle transaction/CAS 原子推进，runtime 仍为 `HALTED`、kill switch 仍开启、`metadata_verified=false`。旧 activation permit 被失效，必须再次执行 `status`、`doctor` 并签发新的 technical permit。任何人工交易、外部委托、入金、出金或无法解释的资金变化都不能算策略收益；必须先用现有 `stress90-account-rebase` 建立新的账户 epoch，然后才能重新建立 operator continuity。\n\n'''
insert_before("docs/live-trading.md", "## 12. 启动对账与执行质量\n", live_section)

insert_after(
    "docs/production-checklist.md",
    "- [ ] 生产配置显式包含 `directional.policy = \"stress90\"` 和 `directional.account_exclusive = true`。\n",
    "- [ ] `directional.account_continuity_mode` 已明确选择；默认和通用生产建议为 `strict`。选择 `operator_managed` 时已确认这是个人专用、单账户、账户独占的操作者信任模型，而非官方结算/session 证据。\n",
)
insert_after(
    "docs/production-checklist.md",
    "- [ ] 同一账户没有手工或其他程序交易，保持账户独占假设。\n",
    "- [ ] 若使用 `operator_managed`，每次跨日 receipt 的 account/epoch/runtime、registry/TDE、完整 session ownership、journal、持仓对账、Deposit/Withdraw 和四项 no-external-activity assertion 均有效；`orders_sent=0`、`cancels_sent=0`。\n- [ ] 若账户发生人工交易、外部委托、入金、出金或无法解释的资金变化，已先执行 `stress90-account-rebase`，没有把外部资金变化计入策略收益。\n- [ ] operator roll-forward 后 runtime 仍为 `HALTED` 且 kill switch 开启，已重新运行 `status`/`doctor` 并签发新 technical permit；外部 activation blockers 仍单独保留。\n",
)

insert_after(
    "docs/stress90-live-runbook.md",
    "account_exclusive = true\n",
    "account_continuity_mode = \"strict\"  # 默认；个人独占账户才考虑 operator_managed\n",
)
runbook_section = '''\n### 6.1 可选：个人独占账户的 operator-managed 跨日\n\n默认 `strict` 不变，仍要求原有 authoritative settlement/session 证据。只有该账户完全由本进程独占、运行期间不进行人工交易/外部委托/入金/出金时，才可在私有生产配置中显式设置：\n\n```toml\n[directional]\naccount_continuity_mode = "operator_managed"\n```\n\n跨日时先保持 `HALTED` 和 kill switch，不启动 live 交易循环。确认 CTP fresh account、完整持仓、活动委托、当前 session order/trade、journal、registry、TradingDayEvidence、OHLC/activity/OI/policy 均一致后：\n\n```bash\nexport AFUTURE_OPERATOR_CONTINUITY_ACK=I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY\nOPERATION_ID="$(openssl rand -hex 32)"\n\nafuture stress90-operator-roll-forward \\\n  --config /secure/path/afuture.directional-stress90.toml \\\n  --confirm-live \\\n  --confirm-operator-continuity \\\n  --operation-id "$OPERATION_ID" \\\n  --operator-reason 'exclusive account continuity; no manual trade/order/deposit/withdrawal'\n\nunset AFUTURE_OPERATOR_CONTINUITY_ACK OPERATION_ID\n```\n\n该命令不报单、不撤单；Deposit/Withdraw 非零、活动委托、unknown order/trade、journal 未终态、持仓漂移、source/data day 不齐或 prepared transaction 不匹配都会失败关闭。自然日间隔大于 1（例如周五到周一）只记录为 operator trust；不把本机日期、`BDay`、静态节假日日历或 OHLC 端点本身当作官方 session ledger。\n\n成功后仍必须看到 `HALTED`、`kill_switch=true`、`metadata_verified=false`，并依次重新运行 `status`、无报单 `doctor`、再签发新的 technical permit。receipt 不能把 `external_activation_gates_completed` 变成 true。若发生任何外部账户活动，先执行 `stress90-account-rebase`；同账户 rebase 会推进 account epoch，后续 operator receipt 只可在 registry/TDE 已证明该新 epoch 的前提下重新锚定，旧 receipt 不能跨 epoch 授权。\n\n'''
insert_before("docs/stress90-live-runbook.md", "## 7. 独立持久 Shadow\n", runbook_section)

trouble_section = '''\n### 4.1 Stress-90 operator-managed 连续性故障\n\n`status`/`doctor` 会把严格外部门与 operator trust receipt 分开显示。`operator_managed` 下若报告 receipt 缺失、checksum/`.prev`/lineage 损坏、account/epoch/runtime 不匹配、registry/TDE 不再绑定、需要 roll-forward、需要 rebase 或需要新 permit，保持 `HALTED`，不要删除 artifact、复制 `.prev`、手改 checksum 或修改本机日期。\n\n若 CTP 当前 Deposit/Withdraw 非零、出现人工/外部订单或成交、Broker/local 持仓漂移、unknown order/trade 或无法解释的权益变化，operator continuity 不再成立。先用柜台事实查明原因；发生合法外部资金/账户活动时走 `stress90-account-rebase`，然后重新建立 continuity。`stress90-operator-roll-forward` 本身绝不发送订单或撤单；成功后也不会解除 kill switch，必须重新运行 `status`、`doctor` 并签发新 technical permit。\n\n'''
insert_before("docs/troubleshooting.md", "## 5. 日志和证据\n", trouble_section)
