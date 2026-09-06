# Objective

<!-- 本 PR 最终解决什么问题？不要只描述修改了哪些文件。模板只记录状态，不授予修改、合并或实盘权限。 -->

# Acceptance Criteria

<!--
保留明确的 AND/OR 逻辑、数值阈值和失败条件。
未完成项保持未勾选，不得把计划写成已经通过。
-->

- [ ]

# Scope

## Included

-

## Excluded

-

# Non-Negotiable Constraints

<!-- 本 PR 不得违反的业务、架构、安全、运行时或数据完整性约束。 -->

-

# Changes

<!-- 按模块说明实际完成的改动。 -->

-

# Current Verified State

<!--
只保留当前有效状态。更新时替换过期值，不无限追加历史。
未知字段写 Not verified；确实不适用时写 Not applicable 并说明原因，不得混用或猜测。
远端 SHA 必须回读核验。无本地 worktree 时不编造本地状态。
仅当本轮合同明确要求时补充 bootstrap 信息；本模板不要求空提交、临时 bootstrap 文件或新 Issue。
-->

- Base branch:
- Verified base SHA:
- Head branch:
- Verified remote head SHA:
- Latest recoverable commit:
- Local worktree state:
- Recovery evidence locations (when needed):
- Related issue (if any):
- Last verified (include timezone):

# Completed and Verified

-

# Remaining Work

1.

# Verification

<!--
逐项区分工程检查、经济验证和目标环境运行验证，只将实际执行的检查记为 Passed。
未运行写 Not run；受阻写 Blocked 并说明原因；不适用写 Not applicable 并说明依据。
记录证据对应的 SHA、输入和环境；复用证据说明等价依据，旧结果不能冒充当前 HEAD 的 CI。
-->

| Check / Type | Command or Workflow | Result | Evidence / Verified SHA |
|---|---|---|---|
| | | Not run | |

# Risks and Unknowns

<!-- 核验后才能写 None known；不要把未评估当成没有风险。 -->

## Verified Risks

- Not assessed

## Unknowns

- Not assessed

# Runtime and Data Impact

- [ ] Production behavior impact has been assessed and documented
- [ ] Configuration semantic impact has been assessed and documented
- [ ] Dependency impact has been assessed and documented
- [ ] Runtime, persisted data, or schema impact has been assessed and documented

# Next Action

-

# Merge Readiness

- [ ] Merge is explicitly authorized for this task
- [ ] Acceptance criteria are satisfied
- [ ] Applicable required tests have passed
- [ ] Required CI checks have passed for the current HEAD
- [ ] Required reviews are satisfied and no blocking review remains
- [ ] Branch protection and conflict checks permit merging
- [ ] No material correctness, security, data-integrity, or economic issue remains
- [ ] PR body reflects the latest verified state
- [ ] Safe to merge
