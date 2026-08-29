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

核验时间：`2026-08-29T08:23:03Z`。

| 字段 | 当前值 |
| --- | --- |
| 仓库 | `ychenracing/afuture` |
| 本地工作树 | `/workspace/scratch/479eec6841a2/afuture` |
| 当前分支 | `codex/minimal-context-state-system` |
| HEAD | `8716e94f322d887c2a24e004d264c7cd9622625d`（本状态文件首次写入前的已核验基线；包含本文件的提交 SHA 无法在该提交内容中自引用，读取时必须重新运行 `git rev-parse HEAD`） |
| `origin/main` | `8716e94f322d887c2a24e004d264c7cd9622625d` |
| 远端功能分支 | `待核验`；验证命令：`git ls-remote --heads origin codex/minimal-context-state-system` |
| merge-base(`HEAD`, `origin/main`) | `8716e94f322d887c2a24e004d264c7cd9622625d` |
| `git status --short` | 空（在三个文件首次写入前核验） |
| staged | 无 |
| unstaged | 无 |
| untracked | 无（在三个文件首次写入前核验） |

说明：版本化文件无法包含“承载该文件的同一个提交 SHA”而不改变该 SHA。每次接管任务必须先执行本文件末尾的只读命令，用现场值替换上述快照；不得把本表当作无需复核的实时 API。

## 已完成并验证事项

- 已核验仓库默认分支为 `main`，任务基线与当时的 `origin/main` 均为 `8716e94f322d887c2a24e004d264c7cd9622625d`。
- 已读取当前权威最小来源：`README.md`、`AGENTS.md`、`pyproject.toml`、`docs/architecture.md`、`docs/documentation-index.md`、`.github/workflows/ci.yml`、`constraints/core-dev.txt`、`constraints/live.txt`。
- 已确认仓库原先不存在 `PROJECT_BRIEF.md`、`TASK_STATE.md`、`ACCEPTANCE.md`，因此本轮为创建而非覆盖。
- 已确认 `docs/stress90-live-handoff-20260826.md` 与 `docs/stress90-live-continuation-prompt-20260826.md` 被 `docs/documentation-index.md` 归类为工程交接/审计记录，不是当前运行契约；当前治理任务没有另一个有效交接包。
- 当前任务 Prompt 已作为 `ACCEPTANCE.md` 的直接验收来源；没有把旧聊天中的 PR、分支或 SHA 作为现场事实。

## 剩余事项及执行顺序

1. 完成三个文件的内容核对与语义覆盖检查。
2. 运行文档结构、字段完整性、禁止项、Git diff 和范围验证。
3. 提交三个治理文件并推送功能分支。
4. 推送后重新核验远端分支、`origin/main`、merge-base 和工作树状态；把无法自引用的提交 SHA 作为外部交付证据报告。

## 当前阻塞、风险和 UNKNOWN

- 阻塞：无。
- UNKNOWN：远端功能分支在本文件首次写入时尚未建立，推送后必须核验。
- 风险：版本化 `TASK_STATE.md` 的嵌入式 HEAD 天然是提交前快照；接管者若不先做只读核验，可能误把它当成实时状态。
- 风险：真实 CTP、目标机 ABI、账户费率、保证金、Shadow 和测试柜台证据不属于本治理任务，也未在本轮验证；不得从文档治理结果推断生产可用性。

## 最近验证命令及准确结果

```text
git ls-remote https://github.com/ychenracing/afuture.git refs/heads/main
=> 8716e94f322d887c2a24e004d264c7cd9622625d  refs/heads/main

git status --short --branch
=> ## codex/minimal-context-state-system

git rev-parse HEAD
=> 8716e94f322d887c2a24e004d264c7cd9622625d

git rev-parse origin/main
=> 8716e94f322d887c2a24e004d264c7cd9622625d

git merge-base HEAD origin/main
=> 8716e94f322d887c2a24e004d264c7cd9622625d
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

随后检查 `PROJECT_BRIEF.md`、`TASK_STATE.md`、`ACCEPTANCE.md` 是否完整覆盖当前 Prompt，且 diff 只包含这三个文件。

## 最后更新时间

`2026-08-29T08:23:03Z`
