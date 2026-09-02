from datetime import datetime, timedelta, timezone

import pytest

from afuture.portfolio_risk import PortfolioRiskAnalyzer


def test_timestamped_correlation_uses_common_time_buckets_only():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=4, bucket_seconds=60)
    base = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
    for index, value in enumerate([1, 2, 4, 3, 6, 5]):
        analyzer.update("left", value, base + timedelta(minutes=index))
        # Same shape but ten minutes later: index alignment would be a false +1 correlation.
        analyzer.update("right", value * 2, base + timedelta(minutes=index + 10))
    assert analyzer.correlation("left", "right") is None


def test_timestamped_correlation_detects_aligned_common_buckets():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=4, bucket_seconds=60)
    base = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
    for index, value in enumerate([1, 2, 4, 3, 6, 5]):
        timestamp = base + timedelta(minutes=index)
        analyzer.update("left", value, timestamp)
        analyzer.update("right", value * 2, timestamp + timedelta(seconds=15))
    assert analyzer.correlation("left", "right") > 0.99


def test_insufficient_common_timestamp_buckets_reject_additional_risk():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=4, bucket_seconds=60)
    base = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
    for index, value in enumerate([1, 2, 4]):
        timestamp = base + timedelta(minutes=index)
        analyzer.update("incumbent", value, timestamp)
        analyzer.update("candidate", value * 2, timestamp)

    assert analyzer.correlation("candidate", "incumbent") is None
    decision = analyzer.allow_open(
        "candidate",
        risk_group="metals",
        open_pairs={"incumbent": "energy"},
    )
    assert not decision.allowed
    assert "incumbent" in decision.reason
    assert "correlation evidence" in decision.reason


def test_one_timestamped_side_never_falls_back_to_ordinal_alignment():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=4, bucket_seconds=60)
    base = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
    for index, value in enumerate([1, 2, 4, 3, 6, 5]):
        analyzer.update("timed", value, base + timedelta(minutes=index))
        analyzer.update("untimestamped", value * 2)

    assert analyzer.correlation("timed", "untimestamped") is None
    decision = analyzer.allow_open(
        "timed",
        risk_group="metals",
        open_pairs={"untimestamped": "energy"},
    )
    assert not decision.allowed
    assert "untimestamped" in decision.reason
    assert "unknown correlation evidence" in decision.reason


def test_insufficient_untimestamped_samples_are_unknown_and_reject_additional_risk():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=4)
    for value in [1, 2, 4]:
        analyzer.update("incumbent", value)
        analyzer.update("candidate", value * 2)

    assert analyzer.correlation("candidate", "incumbent") is None
    assert not analyzer.allow_open(
        "candidate",
        risk_group="metals",
        open_pairs={"incumbent": "energy"},
    ).allowed


def test_insufficient_first_difference_observations_are_unknown():
    evidence = PortfolioRiskAnalyzer._correlation_evidence_from_values([1.0, 2.0], [2.0, 4.0])

    assert not evidence.known
    assert evidence.correlation is None
    assert "first-difference" in evidence.reason


@pytest.mark.parametrize(
    ("side", "values"),
    [
        ("left", [10.0, 10.0, 10.0, 10.0]),
        ("right", [10.0, 10.0, 10.0, 10.0]),
        ("left", [1.0, 1.0 + 1e-8, 1.0, 1.0 + 1e-8]),
        ("right", [1.0, 1.0 + 1e-8, 1.0, 1.0 + 1e-8]),
    ],
    ids=[
        "left-zero-variance",
        "right-zero-variance",
        "left-near-zero-variance",
        "right-near-zero-variance",
    ],
)
def test_one_sided_zero_or_near_zero_change_variance_is_unknown(side, values):
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=4)
    varying = [0.0, 1.0, 3.0, 2.0]
    for index, value in enumerate(values):
        left, right = (value, varying[index]) if side == "left" else (varying[index], value)
        analyzer.update("incumbent", left)
        analyzer.update("candidate", right)

    assert analyzer.correlation("candidate", "incumbent") is None
    assert not analyzer.allow_open(
        "candidate",
        risk_group="metals",
        open_pairs={"incumbent": "energy"},
    ).allowed


def test_non_finite_correlation_result_is_unknown_and_rejects_additional_risk():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=4)
    for value in [1e308, -1e308, 1e308, -1e308]:
        analyzer.update("incumbent", value)
        analyzer.update("candidate", value)

    assert analyzer.correlation("candidate", "incumbent") is None
    assert not analyzer.allow_open(
        "candidate",
        risk_group="metals",
        open_pairs={"incumbent": "energy"},
    ).allowed


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_update_rejects_non_finite_value_before_any_series_write(value):
    analyzer = PortfolioRiskAnalyzer()
    timestamp = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="finite"):
        analyzer.update("bad", value, timestamp)

    assert "bad" not in analyzer._series
    assert "bad" not in analyzer._timed_series


def test_update_rejects_invalid_timestamp_before_any_series_write():
    analyzer = PortfolioRiskAnalyzer()

    with pytest.raises(ValueError, match="timezone-aware"):
        analyzer.update("bad", 1.0, datetime(2026, 8, 21, 1, 0))

    assert "bad" not in analyzer._series
    assert "bad" not in analyzer._timed_series


def test_first_pair_is_allowed_without_correlation_evidence():
    analyzer = PortfolioRiskAnalyzer()

    assert analyzer.allow_open("candidate", risk_group="metals", open_pairs={}).allowed


def test_group_limit_remains_prioritized_over_unknown_correlation_evidence():
    analyzer = PortfolioRiskAnalyzer()

    decision = analyzer.allow_open(
        "candidate",
        risk_group="metals",
        open_pairs={"incumbent": "metals"},
    )

    assert not decision.allowed
    assert decision.reason == "risk-group concentration limit reached"


def test_correlation_at_exact_limit_rejects_additional_risk():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=5, max_correlation=0.8)
    incumbent = [0.0, 1.0, 0.0, 1.0, 0.0]
    candidate = [0.0, 1.4, 1.2, 1.4, 0.0]
    for left, right in zip(incumbent, candidate, strict=True):
        analyzer.update("incumbent", left)
        analyzer.update("candidate", right)

    correlation = analyzer.correlation("candidate", "incumbent")
    assert correlation == analyzer.max_correlation
    decision = analyzer.allow_open(
        "candidate",
        risk_group="metals",
        open_pairs={"incumbent": "energy"},
    )

    assert not decision.allowed
    assert decision.reason == "pair correlation limit reached versus incumbent"


def test_known_high_correlation_rejects_and_known_low_correlation_allows():
    analyzer = PortfolioRiskAnalyzer(window=10, min_samples=6, max_correlation=0.8)
    high = [1, 2, 4, 3, 6, 5]
    low_left = [0, 1, 0, 1, 0, 1]
    low_right = [0, 1, 2, 1, 0, 1]
    for index, value in enumerate(high):
        analyzer.update("high_incumbent", value)
        analyzer.update("high_candidate", value * 2)
        analyzer.update("low_incumbent", low_left[index])
        analyzer.update("low_candidate", low_right[index])

    high = analyzer.allow_open(
        "high_candidate",
        risk_group="metals",
        open_pairs={"high_incumbent": "energy"},
    )
    low = analyzer.allow_open(
        "low_candidate",
        risk_group="metals",
        open_pairs={"low_incumbent": "energy"},
    )

    assert not high.allowed
    assert high.reason == "pair correlation limit reached versus high_incumbent"
    low_correlation = analyzer.correlation("low_candidate", "low_incumbent")
    assert low_correlation is not None
    assert abs(low_correlation) < analyzer.max_correlation
    assert low.allowed
