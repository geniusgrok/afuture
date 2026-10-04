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

仅采集开始时的左截断边界可明确排除；之后缺条、缺完整交易日或已有持仓
退出条无成交量，该具体合约失败。不能按未来覆盖情况选择过去交易日。
其他独立合约仍可诊断，但不能称全池完成。条结束时间及下一条开盘时间是
行情模型约定，不能代替逐笔或柜台证明。各合约是独立单手过程，合计不是
账户收益，不能把日线账户引擎的日风控时钟改成5分钟时钟。

供需意外须有真实发布记录、原始版本和发布前已存在的预期；上月预测变化
不能充当市场意外。产业链先检验真实单位、整数手及全部腿预算，输入不足
时不假设物理系数或成交。独立、跨段成立的成本后贡献才触发统一账户组合；
原核心四格通过后才触发50LOO和固定双剔除。
