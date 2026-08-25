# 文档索引

本索引只解决一个问题：每份 Markdown 属于当前事实、研究证据还是历史记录。当前权威文档描述现有代码和运行约束；研究证据保存固定输入下的结果，但不改变实盘行为；历史记录只用于追溯，不能当成当前说明；设计和计划记录开发决策，不是运行契约。

## 当前权威文档

- [`AGENTS.md`](../AGENTS.md) — 开发、验证和协作约定。
- [`README.md`](../README.md) — 项目定位、能力、使用方式和边界。
- [`docs/glossary.md`](glossary.md) — 术语、缩写和公式的统一定义。
- [`docs/architecture.md`](architecture.md) — 当前模块边界、依赖方向和数据流。
- [`docs/data-and-backtest.md`](data-and-backtest.md) — 当前数据、时间因果和模拟约束。
- [`docs/documentation-index.md`](documentation-index.md) — 本文档分类索引。
- [`docs/live-trading.md`](live-trading.md) — CTP、影子运行、停机和恢复手册。
- [`docs/production-checklist.md`](production-checklist.md) — 真实资金上线检查表。

## 工程审计与验证记录

- [`docs/refactoring/architecture-audit-20260825.md`](refactoring/architecture-audit-20260825.md) — 重构前的架构和正确性审计。
- [`docs/refactoring/industrial-refactoring-report-20260825.md`](refactoring/industrial-refactoring-report-20260825.md) — 工业重构、正确性修复和验证报告。
- [`docs/refactoring/solo-operations-hardening-report-20260825.md`](refactoring/solo-operations-hardening-report-20260825.md) — 单用户运维加固和验证报告。

## 当前研究证据

- [`docs/stress90-bounded-research-evidence.md`](stress90-bounded-research-evidence.md) — 有界研究过程、失败路线和防过拟合记录。
- [`docs/stress90-final-evidence.md`](stress90-final-evidence.md) — 当前离线压力研究候选的完整输入与结果；不代表已接入实盘。

## 历史或已替代记录

- [`docs/archive/evidence/directional-microstructure-cost-alpha-evidence.md`](archive/evidence/directional-microstructure-cost-alpha-evidence.md)
- [`docs/archive/evidence/directional-net-alpha-efficiency-evidence.md`](archive/evidence/directional-net-alpha-efficiency-evidence.md)
- [`docs/archive/evidence/directional-net-alpha-efficiency-final-report.md`](archive/evidence/directional-net-alpha-efficiency-final-report.md)
- [`docs/archive/evidence/directional-p0-p2-evidence.md`](archive/evidence/directional-p0-p2-evidence.md)
- [`docs/archive/evidence/directional-production-mechanics-evidence.md`](archive/evidence/directional-production-mechanics-evidence.md)
- [`docs/archive/evidence/opportunity-driven-directional-v2-evidence.md`](archive/evidence/opportunity-driven-directional-v2-evidence.md)
- [`docs/archive/evidence/production-mpv-integer-optimizer-final-evidence.md`](archive/evidence/production-mpv-integer-optimizer-final-evidence.md)
- [`docs/archive/evidence/research-cleanup-inventory.md`](archive/evidence/research-cleanup-inventory.md)
- [`docs/archive/evidence/research-final-evidence.md`](archive/evidence/research-final-evidence.md)
- [`docs/archive/evidence/return-target-100-evidence.md`](archive/evidence/return-target-100-evidence.md)
- [`docs/archive/evidence/stress68-oi-freeze-checkpoint.md`](archive/evidence/stress68-oi-freeze-checkpoint.md)
- [`docs/archive/evidence/stress80-final-evidence.md`](archive/evidence/stress80-final-evidence.md) — 当前压力研究候选的可复现前序证据。
- [`docs/archive/evidence/two_year_real_data_validation.md`](archive/evidence/two_year_real_data_validation.md)

这些文件保存当时有效的结果、来源和失败路线。除非当前权威文档明确采用，否则其中的参数和指标都不描述当前系统。

## 开发设计与计划
- [`docs/archive/development/plans/2026-08-21-auto-arbitrage-hardening.md`](archive/development/plans/2026-08-21-auto-arbitrage-hardening.md)
- [`docs/archive/development/plans/2026-08-22-directional-production-reality.md`](archive/development/plans/2026-08-22-directional-production-reality.md)
- [`docs/archive/development/plans/2026-08-22-return-target-100.md`](archive/development/plans/2026-08-22-return-target-100.md)
- [`docs/archive/development/plans/2026-08-23-directional-execution-efficiency.md`](archive/development/plans/2026-08-23-directional-execution-efficiency.md)
- [`docs/archive/development/plans/2026-08-23-directional-net-alpha-efficiency.md`](archive/development/plans/2026-08-23-directional-net-alpha-efficiency.md)
- [`docs/archive/development/plans/2026-08-23-directional-stress-robustness.md`](archive/development/plans/2026-08-23-directional-stress-robustness.md)
- [`docs/archive/development/plans/2026-08-23-p0-p2-alpha-capital.md`](archive/development/plans/2026-08-23-p0-p2-alpha-capital.md)
- [`docs/archive/development/plans/2026-08-23-production-return-100.md`](archive/development/plans/2026-08-23-production-return-100.md)
- [`docs/archive/development/plans/2026-08-24-directional-microstructure-cost-alpha.md`](archive/development/plans/2026-08-24-directional-microstructure-cost-alpha.md)
- [`docs/archive/development/plans/2026-08-24-opportunity-driven-directional-v2.md`](archive/development/plans/2026-08-24-opportunity-driven-directional-v2.md)
- [`docs/archive/development/plans/2026-08-25-industrial-refactoring.md`](archive/development/plans/2026-08-25-industrial-refactoring.md)
- [`docs/archive/development/plans/2026-08-25-solo-operations-hardening.md`](archive/development/plans/2026-08-25-solo-operations-hardening.md)
- [`docs/archive/development/specs/2026-08-21-auto-arbitrage-hardening-design.md`](archive/development/specs/2026-08-21-auto-arbitrage-hardening-design.md)
- [`docs/archive/development/specs/2026-08-22-directional-production-reality-design.md`](archive/development/specs/2026-08-22-directional-production-reality-design.md)
- [`docs/archive/development/specs/2026-08-22-return-target-100-design.md`](archive/development/specs/2026-08-22-return-target-100-design.md)
- [`docs/archive/development/specs/2026-08-23-directional-execution-efficiency-design.md`](archive/development/specs/2026-08-23-directional-execution-efficiency-design.md)
- [`docs/archive/development/specs/2026-08-23-directional-net-alpha-efficiency-design.md`](archive/development/specs/2026-08-23-directional-net-alpha-efficiency-design.md)
- [`docs/archive/development/specs/2026-08-23-directional-stress-robustness-design.md`](archive/development/specs/2026-08-23-directional-stress-robustness-design.md)
- [`docs/archive/development/specs/2026-08-23-p0-p2-alpha-capital-design.md`](archive/development/specs/2026-08-23-p0-p2-alpha-capital-design.md)
- [`docs/archive/development/specs/2026-08-24-directional-microstructure-cost-alpha-design.md`](archive/development/specs/2026-08-24-directional-microstructure-cost-alpha-design.md)
- [`docs/archive/development/specs/2026-08-24-opportunity-driven-directional-v2-design.md`](archive/development/specs/2026-08-24-opportunity-driven-directional-v2-design.md)
- [`docs/archive/development/specs/2026-08-24-product-alpha-efficiency-selector-design.md`](archive/development/specs/2026-08-24-product-alpha-efficiency-selector-design.md)
- [`docs/archive/development/specs/2026-08-25-industrial-refactoring-design.md`](archive/development/specs/2026-08-25-industrial-refactoring-design.md)
- [`docs/archive/development/specs/2026-08-25-solo-operations-hardening-design.md`](archive/development/specs/2026-08-25-solo-operations-hardening-design.md)

开发记录可能包含未完成任务、草案代码或已被替代的假设。发生冲突时，以当前权威文档和当前研究证据为准。
