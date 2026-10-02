# 供需状态研究输入

本入口只采集和核验上期所公开报告，不生成策略权重或订单。冻结经济合同仍使用同引擎 B0 对照、完整池/ex-AG 两成本连续账户及核心合格后的剔除和集中度验收。新供需信号须在数据资格成立后另行事前冻结；采集成功不代表收益来源成立。

本轮按数据口径而非历史利润选择既有池中的 AL、CU、NI、PB、SN、ZN。库存周报和同一报告日的仓单日报按周频取得；不声称包含所有日报或整个50品种池。官方数据日历先按完整 ISO 周选最后一个交易日，再应用日期范围，覆盖节前提前结束的交易周，不把截至周中的一天当周报日。日期范围和日历原件哈希写入私有 `DATA_SCOPE.json`，复作前核验。

六个有色品种的周报库存小计取 `SPOTWGHTS`，仓单取 `WRTWGHTS`，`WHSTOCKS` 为可用库容量。只接受仓库总计，排除保税/完税小计的重复累计；不将库存、仓单或库容混为同一个因子。单位、代码/中文品种名、报告日期、数量有限且非负、重复总计都需通过校验。旧官方成功码 `0` 和旧日报缺 VARID/WHTYPE 的明确格式有单独识别；其他未知或不一致输入失败关闭。

每次调用保存原始字节、SHA-256、HTTP元数据、UTC获取时间和追加式收据。`update_date`、`Last-Modified` 和报告日期只作来源元数据，不用于将当前观察的字节提前至历史。按决策时间导出时，只选该时间之前实际观察到的版本；重复观察保留首次获取时间，修订保留旧版。`available_at` 是选中收据的观察时刻，`first_observed_at` 是相同字节版本的首次观察时刻。原件损坏或冲突版本拒绝使用。

```bash
python tools/shfe_supply_evidence.py collect PRIVATE_RUN_DIR --workers 4
python tools/shfe_supply_evidence.py revalidate PRIVATE_RUN_DIR
python tools/shfe_supply_evidence.py qualify PRIVATE_RUN_DIR
python tools/shfe_supply_evidence.py export PRIVATE_RUN_DIR \
  --decision-at 2026-10-02T12:00:00+08:00 --output PRIVATE_RUN_DIR/asof.csv
```

`PRIVATE_RUN_DIR` 须包含已冻结的 `DATA_SCOPE.json` 和哈希匹配的 `trade-data.js`。通常 `collect` 复用已验证的完成报告，只下载未完成项；`--refresh` 追加新的真实观察与修订记录，不覆盖旧原件。`revalidate` 只用已保存字节修正解析，保留原获取时间及旧失败，不重新请求网络。未来时间只是导出参数，不是声称届时已经运行；没有对应观察的报告不会被创建或补齐。

导出的都是观测输入，历史最终档仅能用于其首次可靠观察之后的研究。没有同期版本链时，不运行使用这些数值的原980日正式经济候选，也不把零信号/空账户当作通过。即使窗口末数据完整，实际回放仍必须提供每个决策时点、合理的数据新鲜度规则、预热以及真实合约/整数手/成本条件，并沿用 D→D+1。该工具不会提供新的信号规则、自动调参、风控权限或真实柜台认证。

源码与定向检查在本分支；全量原件、范围、成败收据、版本资格和传输回读保存在 `geniusgrok/afuture-evidence-vault` 的 `runs/afuture-supply-state-20261002/`。已失败 D/G、C/F 和原 B0/C4B 结果继续保留，不将本数据入口解释为它们已晋级。
