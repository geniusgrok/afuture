# Stress90 Bounded Research Evidence

## Authority and baseline

- Base commit: `b4207abb50aca1e39d5ebba3affc04765857251a`.
- Production checkpoint: Stress80 final candidate; live runtime wiring unchanged.
- Candidate weight SHA256: `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`.
- Local fixed-input reproduction before research: Base evidence inherited; Stress full_recent `80.067891%`, DD `-29.727688%`, gross peak `1.683773x`, turnover `337,934,465`, transaction cost `506,901.6975`, net alpha `1,047,283.3025`, net alpha / turnover `30.990722 bps`, no full_recent HALT, zero margin rejects.

## Bounded counters

- Major hypothesis families used: `0 / 3`.
- Production quick candidates used: `0 / 6`.
- Final full matrices used: `0 / 2`.

## Locked negative evidence

The failed paths documented by PR #21/#23 and the inherited evidence files are not eligible for result-driven rescue. This includes candidate-owned MPV, generic/no-trade suppress-only, generic integer tracking/floor/ceil/lot feasibility, exact hard-feasible margin, turnover-first survivor allocation, all-DCE OI/freeze, OI-selective freeze, raw member flow, standalone Price×OI×Volume/session portfolios, naive curve/basis and Candidate A/B product selection.

## P0 Production attribution

Pending behavior-neutral implementation and one fixed Stress full_recent replay.

## Hypothesis ledger

No family is selected before P0 identifies the top three structural opportunities. Every future entry in this section must be committed before its Production result is observed and must include the exact rule and rejection condition.

## Final disposition

Pending. Main must remain unchanged unless a clean candidate satisfies every promotion gate.
