# 最小上下文项目状态系统验收标准

> 本文件定义当前治理任务的稳定验收标准。任何“计划执行”均不等于“已经通过”；实际进度和验证结果只记录在 `TASK_STATE.md`。

## 必须满足的功能条件

1. 仓库根目录存在且仅由本任务创建或安全合并以下治理文件：`PROJECT_BRIEF.md`、`TASK_STATE.md`、`ACCEPTANCE.md`。
2. `PROJECT_BRIEF.md` 只保存长期稳定内容：项目目标、系统定位、架构边界、不可变业务约束、权威来源和状态 owner、标准命令、目录索引及禁止事项。
3. `PROJECT_BRIEF.md` 不保存当前分支、临时 SHA、当前测试结果、本轮进度、大段历史过程或完整日志。
4. `TASK_STATE.md` 只保存最新有效任务现场：目标、验收范围、Git 快照、已验证事项、剩余顺序、阻塞/风险/UNKNOWN、最近命令和准确结果、下一步及更新时间。
5. `TASK_STATE.md` 原位替换过期值；不得并列保留相互矛盾的新旧 SHA、测试结果或进度。
6. `ACCEPTANCE.md` 只保存当前任务的稳定功能条件、组合逻辑、非回归、安全和数据完整性要求、必要验证、可选检查、失败条件与 Definition of Done。
7. 无法核验的字段必须标记为“待核验”并给出验证方法；禁止依据旧聊天、旧摘要、旧交接或记忆猜测现场。
8. 原始日志和大型输出必须进入 `evidence/` 或项目已有证据目录，不复制进 `TASK_STATE.md`。
9. 旧指令到三个文件的语义覆盖表必须存在，每条硬约束有明确归属。
10. 三个文件之间不得存在冲突、重复状态或已经过期的现场描述。

## 组合逻辑

本任务通过当且仅当：

```text
PASS = files_present
   AND project_brief_scope_valid
   AND task_state_scope_valid
   AND acceptance_scope_valid
   AND verified_facts_not_guessed
   AND semantic_coverage_complete
   AND cross_file_consistent
   AND governance_only_diff
   AND required_validation_passed
```

任一组件为 false 或 UNKNOWN 时，整体不得标记为通过。

## 非回归要求

- 不修改 `afuture/`、`config/`、`constraints/`、`tests/`、`tools/`、生产/研究 workflow、部署模板或既有业务文档的语义。
- 不修改策略、风控、账户、订单、成交、状态机、Broker、数据时序、配置默认值、依赖版本或生产操作顺序。
- 不降低 `AGENTS.md` 的渐进式验证要求，不把昂贵 L4 研究矩阵变成文档改动的必要内循环。
- 不改变 README、架构文档和文档索引定义的权威性层级。

## 安全和数据完整性要求

- 精确保留否定、禁止、例外、数字、阈值、日期、版本、分支、SHA、路径、命令、结果、AND/OR、风险和 UNKNOWN。
- 当前现场的只读核验结果高于旧聊天、旧摘要和历史交接；冲突必须显式说明，不得静默改写历史。
- 版本化 `TASK_STATE.md` 不得声称能够静态自引用其承载提交 SHA；接管时必须重新运行只读 Git 核验。
- 不得在治理文件中记录 CTP 用户名、密码、Broker、账户、投资者或其他凭证值。
- 不得执行或建议通过 `reset`、`clean`、`rebase`、force push、丢弃工作、删除工作树/分支或改写历史来完成本任务。
- 文档治理完成不构成 Shadow、测试柜台、真实报单或风险扩大的许可。

## 必须运行的验证

1. 文件与范围：确认三个文件均存在，且任务 diff 只包含这三个文件。
2. Git 现场：核验当前分支、HEAD、`origin/main`、远端功能分支、merge-base 和完整 `git status --short`。
3. 文档质量：运行 `git diff --check`，检查 Markdown 无尾随空格、冲突标记和格式错误。
4. 字段完整性：逐项检查用户 Prompt 要求的全部字段与禁止内容。
5. 语义覆盖：逐行检查下方覆盖表，每项主要归属明确且未弱化。
6. 交叉一致性：确认 `PROJECT_BRIEF.md` 无临时现场，`TASK_STATE.md` 无过期并列值，`ACCEPTANCE.md` 未把计划写成通过。
7. 非回归范围：确认未修改业务代码、策略、配置、依赖、测试、workflow 或生产行为。

## 可以跳过的可选检查

- `python -m pytest -q`、完整 CI、CTP import、doctor、Shadow、测试柜台和真实资金检查：仅文档治理 diff 时可跳过。
- `research-*.yml` 的昂贵历史回测、压力矩阵、benchmark 和 L4 研究验收：本任务必须跳过，除非后续 diff 实际触及策略、数据时间规则、成交/成本假设、仓位构造或共享会计基础设施。
- Markdown 链接爬取和外部站点可用性检查：可选，不替代上述必需验证。

## 明确失败条件

出现任一情况即失败：

- 三文件缺失，或同名文件存在时被未读即覆盖；
- 把当前分支、临时 SHA、测试结果或本轮进度写入 `PROJECT_BRIEF.md`；
- `TASK_STATE.md` 同时保留相互矛盾的新旧 SHA、结果或进度；
- 把未运行、未完成或 UNKNOWN 的检查写成已通过；
- 从旧聊天、历史交接、记忆或推测填充无法现场核验的 Git/测试事实；
- 删除或模糊任何禁止项、数字、阈值、路径、命令、AND 条件或风险；
- 原始日志或大型测试输出被复制进 `TASK_STATE.md`；
- 三文件互相冲突，或语义覆盖表存在未归属硬约束；
- diff 触及业务代码、策略、配置语义、依赖、workflow、部署或生产行为；
- 用降低验证、降低推理质量或扩大任务范围换取上下文缩短；
- 执行破坏性 Git 操作或覆盖用户现有工作。

## Definition of Done

只有在以下条件全部满足后才完成：

- 三个文件已创建或安全合并并保存到独立治理分支；
- 必需验证全部以新鲜证据通过；
- 功能分支已推送且远端 SHA、`origin/main`、merge-base、工作树状态已重新核验；
- diff 仅含三个 Markdown 文件；
- 无未归属硬约束、语义遗漏、冲突、重复状态或未解释 UNKNOWN；
- 最终报告仅包含创建/更新文件、关键归类、未核验信息、语义遗漏结论和 `TASK_STATE.md` 维护方法。

## 旧指令到新文件的语义覆盖表

| 原硬约束或信息类别 | 主要归属 | 覆盖方式 |
| --- | --- | --- |
| 项目名称、最终目标、系统定位 | `PROJECT_BRIEF.md` | “项目名称与最终目标”。 |
| 架构、模块边界、权威数据源、状态 owner | `PROJECT_BRIEF.md` | “关键架构与模块边界”“权威数据源与状态 Owner”。 |
| 不可改变业务约束、数字阈值、禁止事项 | `PROJECT_BRIEF.md` | 独立章节精确保留；验收侧只检查不得弱化。 |
| 标准 build/test/lint/run 命令 | `PROJECT_BRIEF.md` | 从 README、CI、`pyproject.toml` 与 constraints 归一。 |
| 重要目录与权威文档索引 | `PROJECT_BRIEF.md` | “重要目录与文件索引”。 |
| 禁止在长期简报中放分支、SHA、当前测试和进度 | `ACCEPTANCE.md` | 功能条件 2–3 与失败条件；临时值只进 `TASK_STATE.md`。 |
| 当前任务目标与验收范围 | `TASK_STATE.md` | 仅保存本轮最新有效目标和范围。 |
| 当前分支、HEAD、`origin/main`、远端功能分支、merge-base、git status | `TASK_STATE.md` | “Git 现场”；无法静态自引用或尚未推送的值标待核验并提供命令。 |
| 已完成事项、剩余顺序、阻塞、风险、UNKNOWN、下一步、更新时间 | `TASK_STATE.md` | 各独立章节；更新时替换旧值。 |
| 最近测试/验证命令及准确结果 | `TASK_STATE.md` | 只记录实际运行结果；未运行的检查不写通过。 |
| 功能条件与 AND/OR 逻辑 | `ACCEPTANCE.md` | “必须满足的功能条件”“组合逻辑”。 |
| 非回归、安全、数据完整性、必须/可选验证、失败条件、DoD | `ACCEPTANCE.md` | 各独立章节。 |
| 同名文件先读后合并 | `ACCEPTANCE.md` | 功能条件与失败条件；本轮已核验三文件原先不存在。 |
| 原始日志进入 `evidence/`，不复制到状态文件 | `TASK_STATE.md` + `ACCEPTANCE.md` | 状态文件顶部给出存放规则，验收负责阻断复制大型输出。 |
| 不删除禁止项、数字、阈值、SHA、路径、验收和风险 | `ACCEPTANCE.md` | 安全完整性要求与失败条件。 |
| 三文件冲突、重复、过期检查 | `ACCEPTANCE.md` | 必须验证 6；状态文件只保留最新值。 |
| 只做上下文治理，不改变生产经济行为 | 三文件 | `PROJECT_BRIEF.md` 保存长期禁止项，`TASK_STATE.md` 声明当前范围，`ACCEPTANCE.md` 设非回归阻断门。 |
| 最小读取、现场高于旧聊天、信息不足才扩大 | `TASK_STATE.md` + `ACCEPTANCE.md` | 记录实际来源；安全要求规定权威顺序和冲突处理。 |
| 旧 Stress-90 交接/续办文件的当前地位 | `TASK_STATE.md` | 按 `docs/documentation-index.md` 归为工程审计记录，非当前运行契约。 |
| 最终只报告五类信息 | `ACCEPTANCE.md` | Definition of Done 明确最终报告范围。 |
