# 当前任务状态

> 本文件只保存最新有效状态。每次重要里程碑后原位替换过期字段，不追加相互矛盾的历史。原始日志和大型输出放入 `evidence/` 或任务已有证据目录。

## 当前任务目标

为 `ychenracing/afuture` 建立最小上下文项目状态系统，创建并安全维护：

- `PROJECT_BRIEF.md`：长期稳定事实；
- `TASK_STATE.md`：当前有效现场；
- `ACCEPTANCE.md`：当前任务稳定验收标准。

本任务只修改上下文与项目治理文档，不修改业务代码、策略逻辑、配置语义、依赖、运行时或生产行为。

## 当前验收范围

- 只读取当前任务所需的最小权威来源；证据不足或冲突时才扩大范围。
- 三个治理文件不存在时创建，存在时先读取并语义合并。
- 每条硬约束都有唯一主要归属；三文件不存在冲突、重复状态或过期现场。
- 无法现场核验的值明确写“待核验”，不得采用旧聊天、旧摘要或旧交接猜测。
- 具体 AND/OR、失败条件与 Definition of Done 见 `ACCEPTANCE.md`。

## Git 现场

核验时间：`2026-08-29T08:28:27Z`。

| 字段 | 当前值 |
| --- | --- |
| 仓库 | `ychenracing/afuture` |
| 本地工作树 | `/workspace/scratch/479eec6841a2/afuture`（本轮临时 authoring checkout；远端任务分支为交付权威） |
| 当前分支 | `codex/minimal-context-state-system` |
| HEAD | `待核验`（版本化文件无法静态自引用承载自身的提交 SHA；读取时运行 `git rev-parse HEAD` 或查询远端 ref） |
| `origin/main` | `8716e94f322d887c2a24e004d264c7cd9622625d` |
| 远端功能分支 | `codex/minimal-context-state-system`；本次状态刷新前已核验 head 为 `ded433401124550efce81cfd2180a190ca502ea0`，刷新提交后的实时值须重新查询 |
| merge-base(`HEAD`, `origin/main`) | `8716e94f322d887c2a24e004d264c7cd9622625d` |
| `git status --short` | 空（authoring checkout 在最终状态刷新前核验） |
| staged | 无 |
| unstaged | 无 |
| untracked | 无 |

说明：版本化文件无法包含“承载该文件的同一个提交 SHA”而不改变该 SHA。每次接管任务必须先执行本文件末尾的只读命令，用现场值替换上述快照；不得把本表当作无需复核的实时 API。

## 已完成并验证事项

- 已核验仓库默认分支为 `main`，任务基线与 `origin/main` 均为 `8716e94f322d887c2a24e004d264c7cd9622625d`。
- 已读取当前权威最小来源：`README.md`、`AGENTS.md`、`pyproject.toml`、`docs/architecture.md`、`docs/documentation-index.md`、`.github/workflows/ci.yml`、`constraints/core-dev.txt`、`constraints/live.txt`。
- 已确认仓库原先不存在 `PROJECT_BRIEF.md`、`TASK_STATE.md`、`ACCEPTANCE.md`，因此本轮为创建而非覆盖。
- 已确认 `docs/stress90-live-handoff-20260826.md` 与 `docs/stress90-live-continuation-prompt-20260826.md` 被 `docs/documentation-index.md` 归类为工程交接/审计记录，不是当前运行契约；当前治理任务没有另一个有效交接包。
- 当前任务 Prompt 已作为 `ACCEPTANCE.md` 的直接验收来源；没有把旧聊天中的 PR、分支或 SHA 作为现场事实。
- 已创建并逐项检查三个治理文件；语义覆盖表位于 `ACCEPTANCE.md`。
- 已核验任务 diff 只有 `ACCEPTANCE.md`、`PROJECT_BRIEF.md`、`TASK_STATE.md`，且 `git diff --check`、必需章节检查和冲突标记检查通过。
- 已在远端创建功能分支并写入三个文件；初次完整写入后的远端 head 为 `ded433401124550efce81cfd2180a190ca502ea0`。

## 剩余事项及执行顺序

当前治理文件的创建与远端保存已完成。后续不属于本轮文档治理的事项：

1. 由仓库维护者决定是否通过 PR 或其他受保护流程合入 `main`。
2. 下一项实际工程任务开始时，用新现场原位刷新本文件；不得追加旧快照。

## 当前阻塞、风险和 UNKNOWN

- 阻塞：无。
- UNKNOWN：本文件承载提交及远端 ref 的最终 SHA 无法在该提交内容中自引用，必须通过只读 Git 命令现场核验。
- 风险：本轮本地 HTTPS push 因 authoring 容器没有 Git 凭证而失败；随后使用已连接的 GitHub 权限创建远端分支和文件。authoring checkout 的本地提交与远端提交 SHA 不同，不得把本地 SHA 当成远端 head。
- 风险：版本化 `TASK_STATE.md` 是带核验时间的快照；接管者若不先做只读核验，可能误把它当成实时 API。
- 风险：真实 CTP、目标机 ABI、账户费率、保证金、Shadow 和测试柜台证据不属于本治理任务，也未在本轮验证；不得从文档治理结果推断生产可用性。

## 最近验证命令及准确结果

```text
git ls-remote https://github.com/ychenracing/afuture.git refs/heads/main
=> 8716e94f322d887c2a24e004d264c7cd9622625d  refs/heads/main

git status --short --branch
=> ## codex/minimal-context-state-system

git rev-parse HEAD
=> f70177878f4cf4960619ea9a747083cc31867797（本地 authoring 提交，不是远端 head）

git rev-parse origin/main
=> 8716e94f322d887c2a24e004d264c7cd9622625d

git rev-parse origin/codex/minimal-context-state-system
=> ded433401124550efce81cfd2180a190ca502ea0（本次状态刷新前）

git merge-base origin/codex/minimal-context-state-system origin/main
=> 8716e94f322d887c2a24e004d264c7cd9622625d

git diff --name-only origin/main...origin/codex/minimal-context-state-system
=> ACCEPTANCE.md
=> PROJECT_BRIEF.md
=> TASK_STATE.md

git diff --cached --check
=> exit 0，无输出
```

未运行应用测试、昂贵回测或研究 workflow：本轮尚未修改 Python、策略、配置、依赖或生产行为；最终必须运行文档和 Git 范围验证。

## 下一步具体动作

```bash
git status --short --branch
git rev-parse HEAD
git fetch origin main
git rev-parse origin/main
git merge-base HEAD origin/main
git diff --check
git diff --name-only origin/main...HEAD
```

下一任务开始时先用上述现场值原位替换本文件的过期 Git 快照，再根据任务影响面加载 `ACCEPTANCE.md` 和 `PROJECT_BRIEF.md`。若产生大型日志，把原文放入 `evidence/` 或现有任务证据目录，只在本文件记录结论、准确结果和路径。

## 最后更新时间

`2026-08-29T08:28:27Z`
