# Research cleanup inventory

> **Historical governance record.** This inventory describes an earlier cleanup boundary. Current document authority is defined in [`documentation-index.md`](../../documentation-index.md).

Final review removes only experiments that failed their economic promotion gate and were never wired into production behavior.

Removed experimental families:

- broad cross-sectional momentum / slow-fast / reversal / skewness research: no stable family survived the preregistered prior + Train/Validation gates;
- 60-minute BU/FU + PP/V intraday research: `0/24` pre-OOS profiles passed;
- structural multi-leg research: soybean crush, steel/coke margin and related structures did not provide a stable return level sufficient to justify three-leg production complexity.

Their evidence and rejection rationale are preserved in `docs/archive/evidence/research-final-evidence.md`. Production code, risk permissions, account/order/fill semantics and the surviving corrected M/OI / broad economic-pair L3 / specific-contract daily L4 research chain are not deleted by this cleanup.
