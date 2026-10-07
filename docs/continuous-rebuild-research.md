# 连续失败后的离线分流执行

本轮研究未盈利持仓退出、固定信号族组合、条件环境诊断、发布前供需意外、
产业链整数容量和真实分钟过程。各分支失败后继续其他具备输入的分支，
原经济门仍适用；历史开发资料不能重新命名为未见样本。

`tools/continuous_rebuild_research.py` 复用上轮原件和原账户引擎。E3 在 E1
盈利保护尚未启动时，若先前完成日价格相对实际入场开盘不利移动达到同一
固定入场波幅，下一真实开盘退出。同侧原目标未归零或反转前禁止重新进入。

M1 使用原96模板，六个信号族等权、族内模板等权，取消短期冠军轮换。
原 OI、成本、幸存预算与自身因果 HHI 保留，相互抵消后的敞口不补到2倍。
exAG 在模板选择之前重建池。M1Q 是明确的数据资格修订：必要的上一完成
日 OI 缺失时删除目标；输入仍保存为缺失，不填零或替代代理。原 M1 数据
阻塞保留，不能称其已完成经济验收。

```bash
python tools/continuous_rebuild_research.py --previous "$AFUTURE_PREVIOUS_RUN" \
  --output "$AFUTURE_NEW_RUN/strategy" build --baseline "$AFUTURE_BASELINE" \
  --fixed "$AFUTURE_FIXED_INPUTS" --extension "$AFUTURE_EXTENSION" --qualified
python tools/continuous_rebuild_research.py --previous "$AFUTURE_PREVIOUS_RUN" \
  --output "$AFUTURE_NEW_RUN/E3_historical" replay \
  --strategy "$AFUTURE_NEW_RUN/strategy" --market "$AFUTURE_SPECIFIC" \
  --units "$AFUTURE_UNITS" --variant E3 --window historical
```

替换 `E3` 为 `M1Q` 可评估固定组合，替换 `historical` 为 `recent` 可评估
近期窗口。每次输出为新目录。工具先复现原 B0 上游状态，再生成候选；
账户成交、同合约损益、费用和每日权益逐项核对。

环境诊断使用严格更早的20日市场波动与扩展中位数，观察各族在两个固定
历史段中的条件压力成本代理。代理不包含真实完整持仓损益，不能作为账户
收益。不同族呈现相反、跨段稳定且压力成本为正的偏好才触发 M2，未满足
时不建立可任意调参的分类器。

`tools/minute_session_research.py` 消费真实具体合约5分钟数据，固定日盘
首15分钟区间突破，完成信号后等待一个完整额外条，再用后续条的真实开盘
报价观察一手过程；区间重入或预定日盘结束前退出，两端均计费。

```bash
python tools/minute_session_research.py --inputs "$AFUTURE_MINUTE_INPUTS" \
  --units "$AFUTURE_UNITS" --calendar "$AFUTURE_EXCHANGE_CALENDAR" \
  --output "$AFUTURE_NEW_RUN/T1"
```

仅采集开始时的左截断边界可明确排除；之后缺条、缺完整交易日，或入场及
持仓退出条无成交量，该具体合约失败。不能按未来覆盖或已完成入场条的
成交量选择过去交易日或跳过不利交易。
其他独立合约仍可诊断，但不能称全池完成。条结束时间及下一条开盘时间是
行情模型约定，不能代替逐笔或柜台证明。各合约是独立单手过程，合计不是
账户收益，不能把日线账户引擎的日风控时钟改成5分钟时钟。

供需意外须有真实发布记录、原始版本和发布前已存在的预期；上月预测变化
不能充当市场意外。产业链先检验真实单位、整数手及全部腿预算，输入不足
时不假设物理系数或成交。组件可凭已验证的压力成本净贡献或对既有账户亏损
日的保护作用进入统一账户组合实验；这是开发筛选，不是独立经济认证。
最终组合仍须通过原核心四格，然后才触发50LOO和固定双剔除。

`tools/execute_research_continuation.py` 把选方向、运行与验账接成实际循环。
P1 检验固定族目标的整数手实现：在原目标总预算内，按减少金额跟踪误差的
幅度确定性地从向下取整改为相邻向上取整。原保证金、现金保留、HHI、退出
与成交账本继续负责最终仓位。P1 未通过后自动执行 C1：六族方向共识仅控制
原成本门之后额外恢复的预算。如果已验证组件满足上述研究准入条件，自动
检验它与 E1 的固定等权目标在同一账户内的结果，不能拼接权益曲线。
上述已验证预算分支未通过后，执行 R1 行业内相对多空：继承事前四行业组，
按63个已完成日收益于每周首个实际交易日选各组最强和最弱，各配0.25，
两端有并列或历史不足则整组空置。保留原OI与成本门，不把被过滤预算补满。
旧全池横截面动量已失败；该实验检验行业内配对的结构修订。实际成交后的
行业净敞口另从整数手事件重建，原始目标中性不代表最终持仓中性。

```bash
python tools/execute_research_continuation.py \
  --previous "$AFUTURE_PREVIOUS_RUN" --continuous "$AFUTURE_CONTINUOUS_RUN" \
  --baseline "$AFUTURE_BASELINE" --output "$AFUTURE_NEW_RUN"
```

输出根目录必须预先保存 `FROZEN_PROTOCOL.json` 和 `R1_PROTOCOL.json`，
其中 `inputs` 逐项记录
真实输入的绝对 `path` 和 `sha256`。运行前冻结源码与输入身份；E1 对照日
权益还须匹配旧原件清单。每项实验先保存具体规则，再执行历史及近期、全池
及独立去白银池、基础及压力成本的八格账户，并重读每日账本和事件核验。
缺行情或软件错误记录为证据无效；真实风险触发和经济失败分别留存，均不能
被“程序运行成功”替代。失败自动调用下一方向选择器，不需要再次启动命令。

`tools/research_continuation.py` 提供可替换的 `select_next(history)` 回调。
它把已完成实验的诊断传入选择器，拒绝重复执行同一已完成规则。每次完整
结果都有原件清单和完成凭据；恢复时逐字节核验，完整但尚未生成凭据的账户
先补验复用，部分执行保留后另建目录重试。源或输入变化必须使用新的
`--campaign`，不能覆盖旧实验。真实数据和代码始终与各自结果绑定。

已登记方向用尽时状态为 `needs_new_hypothesis`，不代表经济目标完成。
外层研究工作应继续诊断、登记有依据的新机制或处理具体数据缺口，不能把
固定候选数量用完当作停止理由。该命令本身不会凭空生成任意新算法，也不
创建定时任务；原核心门通过仍须完成既有扩展验收，没有实盘启用权限。

`tools/usda_revision_research.py` 提供后续供需方向所需的官方预测版本面板。
它分别保存每份报告的当月预测和该份报告引用的前月值；修订信号只比较两份
真实相邻月原件的当月列，按同一作物年对齐，缺月不跨月拼接，缺值保持缺值。
新作物年前月尚未发布的情况不会被填成零。可检验信号使用最新共同作物年，
世界大豆、豆粕、豆油分别映射 B、M、Y，库存消费比下降对应看多。

这项研究的输入是官方自身预测修订，不能称为市场共识意外。HTML 发布时间、
TXT 修改时间和本次实际捕获时间分别留存。历史开发假设取两份报告各自
HTML/TXT 时钟的最大值，再等待最早可能夜盘开盘也晚于该时点的完整交易日。
归档时钟只支持带有该假设的回放，不证明历史版本不可变或已取得独立PIT认证。

`tools/execute_usda_revision_research.py` 将这些输入接入同一账户与八格核验。
每份新报告先清除旧信号；配对原件满足可用时点后，只激活仍属于最新报告的
修订。晚到旧报告不能覆盖已知新报告。三个品种平分不超过2倍的原始预算，
仍经过原OI资格、成本、HHI和E1退出。U1未过核心门而存在已核验的净贡献或
保护贡献时，自动检验与各池E1目标固定等权的U1MIX，保留归档时钟假设。

```bash
python tools/execute_usda_revision_research.py --usda "$AFUTURE_USDA_INPUTS" \
  --previous "$AFUTURE_PREVIOUS_RUN" --continuous "$AFUTURE_CONTINUOUS_RUN" \
  --baseline "$AFUTURE_BASELINE" --output "$AFUTURE_NEW_RUN"
```

该入口要求输出根目录预先保存 `U1_ACCOUNT_PROTOCOL.json`、
`U1_ACCOUNT_PROTOCOL_ADDENDUM.json`、`U1_COMBINATION_PROTOCOL.json`，
并校验其中真实输入的身份。原件、可用时间、行情、单位和对照路径都绑定到
执行上下文；即使核心数值通过，也仍需发布时间资格和原扩展验收。
