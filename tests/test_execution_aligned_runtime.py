import json
import sys
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import afuture.directional_ohlc_cache as ohlc_cache_module
from afuture.directional import DirectionalConfig
from afuture.directional_activity import (
    ContractActivity,
    DirectionalActivitySnapshot,
    DirectionalActivityStore,
    DirectionalActivityTracker,
)
from afuture.directional_ohlc_cache import (
    DirectionalOHLCCacheIntegrityError,
    DirectionalOHLCCacheStore,
)
from afuture.execution_aligned_runtime import (
    FROZEN_PRODUCTS,
    ExecutionAlignedDirectionalPortfolioManager,
    ExecutionAlignedSignalHistory,
    SinaContinuousOHLCProvider,
)
from afuture.models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Offset,
    OrderType,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager

NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


class _Provider:
    def __init__(self):
        dates = pd.date_range(end="2026-08-21", periods=180, freq="B")
        close = pd.DataFrame({"A": range(100, 280)}, index=dates, dtype=float)
        open_prices = close.shift(1).fillna(close.iloc[0])
        self.history = ExecutionAlignedSignalHistory(open_prices, close)
        self.fail = False

    def load(self, products):
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.history


class _Policy:
    def __init__(self):
        self.calls = 0

    def target_weights(self, open_prices, close):
        self.calls += 1
        assert open_prices.index.equals(close.index)
        assert open_prices.index[-1] > self._last_observed(close)
        return {"A": 1.0}

    @staticmethod
    def _last_observed(close):
        return close.index[-2]


class _Broker:
    def is_ready(self):
        return True

    def get_account(self):
        return AccountSnapshot(
            balance=100000,
            equity=100000,
            available=100000,
            margin=0,
            realized_pnl=0,
            unrealized_pnl=0,
            trading_day="20260825",
        )

    def get_positions(self):
        return []

    def get_active_orders(self):
        return []


class _FlattenBroker(_Broker):
    def __init__(self):
        self.positions = [
            ContractPosition(
                "A2609",
                "DCE",
                long_today=3,
                short_today=3,
                long_price=100.0,
                short_price=100.0,
            )
        ]
        self.orders = []

    def get_positions(self):
        return self.positions

    def get_contract_catalog(self):
        return [
            ContractInfo(
                symbol="A2609",
                exchange="DCE",
                product="A",
                expiry="2026-12-15",
            )
        ]

    def subscribe(self, symbol, exchange):
        return None

    def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
        return {
            symbol: ContractSpec(
                symbol=symbol,
                exchange="DCE",
                multiplier=10.0,
                price_tick=1.0,
                margin_rate_long=0.1,
                margin_rate_short=0.1,
            )
            for symbol in symbols
        }

    def send_order(self, request):
        self.orders.append(request)
        return f"order-{len(self.orders)}"


def _manager(
    provider=None,
    policy=None,
    *,
    cache_path: Path | None = None,
    products=("A",),
    activity_tracker=None,
):
    cache = {"ohlc_cache_path": cache_path} if cache_path is not None else {}
    return ExecutionAlignedDirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            products=products,
            exchanges=("DCE",),
            signal_max_age_hours=120.0,
        ),
        _Broker(),
        RiskManager(RiskConfig()),
        signal_provider=provider or _Provider(),
        policy=policy or _Policy(),
        activity_tracker=activity_tracker,
        **cache,
    )


def _resign_cache(envelope: dict) -> None:
    content = envelope["content"]
    envelope["content_digest"] = sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    envelope["checksum"] = sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_execution_aligned_runtime_passes_open_and_close_history_to_policy():
    policy = _Policy()
    manager = _manager(policy=policy)
    history = manager._load_signal(NOW)
    weights = manager._next_target_weights(history)
    assert weights == {"A": 1.0}
    assert policy.calls == 1


def test_signal_freshness_uses_completed_trading_day_not_only_hour_age():
    manager = _manager()
    # Friday's completed bar is valid for the first post-weekend trading session.
    history = manager._load_signal(NOW, required_signal_day=date(2026, 8, 21))
    assert history.close.index[-1].date() == date(2026, 8, 21)

    # Missing a required normal completed trading day must fail even though 120h has not expired.
    with pytest.raises(RuntimeError, match="required signal trading day"):
        manager._load_signal(NOW, required_signal_day=date(2026, 8, 24))


def test_exact_required_completed_day_survives_long_scheduled_closure():
    provider = _Provider()
    dates = pd.date_range(end="2026-02-13", periods=180, freq="B")
    close = pd.DataFrame({"A": range(100, 280)}, index=dates, dtype=float)
    provider.history = ExecutionAlignedSignalHistory(
        close.shift(1).fillna(close.iloc[0]),
        close,
    )
    manager = _manager(provider=provider)

    history = manager._load_signal(
        datetime(2026, 2, 24, 1, 1, tzinfo=timezone.utc),
        required_signal_day=date(2026, 2, 13),
    )

    assert history.close.index[-1].date() == date(2026, 2, 13)


def test_required_completed_day_still_rejects_future_history():
    provider = _Provider()
    dates = pd.date_range(end="2026-02-13", periods=180, freq="B")
    close = pd.DataFrame({"A": range(100, 280)}, index=dates, dtype=float)
    provider.history = ExecutionAlignedSignalHistory(
        close.shift(1).fillna(close.iloc[0]),
        close,
    )
    manager = _manager(provider=provider)

    with pytest.raises(RuntimeError, match="from the future"):
        manager._load_signal(
            datetime(2026, 2, 12, 1, 0, tzinfo=timezone.utc),
            required_signal_day=date(2026, 2, 13),
        )


def test_cached_signal_can_cover_transient_provider_failure_when_required_day_is_present():
    provider = _Provider()
    manager = _manager(provider=provider)
    manager._load_signal(NOW, required_signal_day=date(2026, 8, 21))
    provider.fail = True
    later = datetime(2026, 8, 25, 13, 1, tzinfo=timezone.utc)
    cached = manager._load_signal(later, required_signal_day=date(2026, 8, 21))
    assert cached.close.index[-1].date() == date(2026, 8, 21)


def test_verified_ohlc_cache_survives_restart_provider_outage(tmp_path: Path) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    first = _manager(provider=provider, cache_path=cache_path)

    original = first._load_signal(NOW, required_signal_day=date(2026, 8, 21))
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))

    assert envelope["schema_version"] == 1
    assert len(envelope["content_digest"]) == 64
    assert len(envelope["checksum"]) == 64

    provider.fail = True
    restarted = _manager(provider=provider, cache_path=cache_path)
    restored = restarted._load_signal(NOW, required_signal_day=date(2026, 8, 21))

    pd.testing.assert_frame_equal(restored.open, original.open)
    pd.testing.assert_frame_equal(restored.close, original.close)


def test_tampered_ohlc_cache_is_never_used_for_provider_outage(tmp_path: Path) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    envelope["content"]["close"][0][0] += 1.0
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")
    provider.fail = True

    with pytest.raises(RuntimeError, match="(checksum|digest) mismatch"):
        _manager(provider=provider, cache_path=cache_path)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )


def test_changed_overlapping_provider_history_falls_back_to_verified_cache(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    original = _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )
    revised_close = provider.history.close.copy()
    revised_close.iloc[20, 0] += 1.0
    provider.history = ExecutionAlignedSignalHistory(provider.history.open, revised_close)

    restarted = _manager(provider=provider, cache_path=cache_path)
    restored = restarted._load_signal(NOW, required_signal_day=date(2026, 8, 21))

    pd.testing.assert_frame_equal(restored.open, original.open)
    pd.testing.assert_frame_equal(restored.close, original.close)


def test_ohlc_cache_missing_or_expired_for_required_day_cannot_cover_provider_outage(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    provider.fail = True

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _manager(provider=provider, cache_path=cache_path)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )

    provider.fail = False
    _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )
    provider.fail = True
    with pytest.raises(RuntimeError, match="required signal trading day"):
        _manager(provider=provider, cache_path=cache_path)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 24),
        )


def test_ohlc_cache_rejects_schema_product_index_and_nonfinite_corruption(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )
    pristine = json.loads(cache_path.read_text(encoding="utf-8"))
    provider.fail = True

    schema = json.loads(json.dumps(pristine))
    schema["schema_version"] = 2
    _resign_cache(schema)
    cache_path.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(RuntimeError, match="schema version"):
        _manager(provider=provider, cache_path=cache_path)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )

    cache_path.write_text(json.dumps(pristine), encoding="utf-8")
    with pytest.raises(RuntimeError, match="product set"):
        _manager(
            provider=provider,
            cache_path=cache_path,
            products=("M",),
        )._load_signal(NOW, required_signal_day=date(2026, 8, 21))

    duplicate_index = json.loads(json.dumps(pristine))
    duplicate_index["content"]["dates"][0] = duplicate_index["content"]["dates"][1]
    _resign_cache(duplicate_index)
    cache_path.write_text(json.dumps(duplicate_index), encoding="utf-8")
    with pytest.raises(RuntimeError, match="duplicate daily date"):
        _manager(provider=provider, cache_path=cache_path)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )

    nonfinite = json.loads(json.dumps(pristine))
    nonfinite["content"]["open"][0][0] = float("nan")
    _resign_cache(nonfinite)
    cache_path.write_text(json.dumps(nonfinite), encoding="utf-8")
    with pytest.raises(RuntimeError, match="finite"):
        _manager(provider=provider, cache_path=cache_path)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )


def test_provider_open_close_index_misalignment_is_not_silently_intersected() -> None:
    provider = _Provider()
    provider.history = ExecutionAlignedSignalHistory(
        provider.history.open.iloc[:-1],
        provider.history.close,
    )

    with pytest.raises(RuntimeError, match="open/close indexes must match"):
        _manager(provider=provider)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )


def test_provider_rejects_aware_open_index_even_when_utc_dates_match_naive_close() -> None:
    provider = _Provider()
    aware_open = provider.history.open.copy()
    aware_open.index = aware_open.index.tz_localize("UTC")
    provider.history = ExecutionAlignedSignalHistory(aware_open, provider.history.close)

    with pytest.raises(RuntimeError, match="naive calendar-day midnight"):
        _manager(provider=provider)._load_signal(NOW)


def test_provider_rejects_asia_shanghai_midnight_before_timezone_conversion() -> None:
    provider = _Provider()
    open_prices = provider.history.open.copy()
    close = provider.history.close.copy()
    open_prices.index = open_prices.index.tz_localize("Asia/Shanghai")
    close.index = close.index.tz_localize("Asia/Shanghai")
    provider.history = ExecutionAlignedSignalHistory(open_prices, close)

    with pytest.raises(RuntimeError, match="naive calendar-day midnight"):
        _manager(provider=provider)._load_signal(NOW)


def test_provider_rejects_non_midnight_daily_index() -> None:
    provider = _Provider()
    open_prices = provider.history.open.copy()
    close = provider.history.close.copy()
    open_prices.index = open_prices.index + pd.Timedelta(hours=12)
    close.index = close.index + pd.Timedelta(hours=12)
    provider.history = ExecutionAlignedSignalHistory(open_prices, close)

    with pytest.raises(RuntimeError, match="naive calendar-day midnight"):
        _manager(provider=provider)._load_signal(NOW)


def test_float32_then_integer_equivalent_overlap_can_append_required_day(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    provider.history = ExecutionAlignedSignalHistory(
        provider.history.open.astype("float32"),
        provider.history.close.astype("float32"),
    )
    _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )

    appended_close = provider.history.close.astype("int64")
    appended_open = provider.history.open.astype("int64")
    appended_close.loc[pd.Timestamp("2026-08-24"), "A"] = 280
    appended_open.loc[pd.Timestamp("2026-08-24"), "A"] = 279
    provider.history = ExecutionAlignedSignalHistory(appended_open, appended_close)

    result = _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 24),
    )

    assert result.close.index[-1] == pd.Timestamp("2026-08-24")
    assert result.close.dtypes.to_dict() == {"A": pd.Float64Dtype().numpy_dtype}


def test_unchanged_float64_overlap_can_append_without_revision_fallback(tmp_path: Path) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )
    provider.history.close.loc[pd.Timestamp("2026-08-24"), "A"] = 280.0
    provider.history.open.loc[pd.Timestamp("2026-08-24"), "A"] = 279.0

    result = _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 24),
    )

    assert result.close.index[-1] == pd.Timestamp("2026-08-24")


def test_first_save_and_restart_use_identical_decoded_float64_values(tmp_path: Path) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    provider.history = ExecutionAlignedSignalHistory(
        (provider.history.open * 1.001).astype("float32"),
        (provider.history.close * 1.001).astype("float32"),
    )

    first = _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )
    provider.fail = True
    restarted = _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )

    pd.testing.assert_frame_equal(first.open, restarted.open)
    pd.testing.assert_frame_equal(first.close, restarted.close)
    assert first.close.dtypes.to_dict() == {"A": pd.Float64Dtype().numpy_dtype}


def test_provider_rejects_integer_that_cannot_round_trip_through_float64(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    close = provider.history.close.astype(object)
    close.iloc[0, 0] = 2**53 + 1
    provider.history = ExecutionAlignedSignalHistory(provider.history.open, close)

    with pytest.raises(RuntimeError, match="losslessly represented as float64"):
        _manager(
            provider=provider,
            cache_path=tmp_path / "directional_ohlc_cache.json",
        )._load_signal(NOW, required_signal_day=date(2026, 8, 21))


@pytest.mark.parametrize(
    ("future_value", "reason"),
    [(999.0, "beyond authoritative planning day"), (float("nan"), "finite"), (-1.0, "positive")],
)
def test_provider_rejects_every_row_beyond_local_planning_day(
    future_value: float,
    reason: str,
) -> None:
    provider = _Provider()
    provider.history.close.loc[pd.Timestamp("2026-08-25"), "A"] = future_value
    provider.history.open.loc[pd.Timestamp("2026-08-25"), "A"] = future_value

    with pytest.raises(RuntimeError, match=reason):
        _manager(provider=provider)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )


def test_provider_rejects_invalid_unfinished_current_trading_day_row() -> None:
    provider = _Provider()
    provider.history.close.loc[pd.Timestamp("2026-08-24"), "A"] = float("nan")
    provider.history.open.loc[pd.Timestamp("2026-08-24"), "A"] = 279.0

    with pytest.raises(RuntimeError, match="must be finite"):
        _manager(provider=provider)._load_signal(
            NOW,
            required_signal_day=date(2026, 8, 21),
        )


def test_provider_permits_current_ctp_day_but_caches_only_required_completed_day(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    provider.history.close.loc[pd.Timestamp("2026-08-25"), "A"] = 280.0
    provider.history.open.loc[pd.Timestamp("2026-08-25"), "A"] = 279.0
    tracker = SimpleNamespace(current_trading_day="20260825")

    result = _manager(
        provider=provider,
        cache_path=cache_path,
        activity_tracker=tracker,
    )._load_signal(NOW, required_signal_day=date(2026, 8, 21))
    entry = DirectionalOHLCCacheStore(cache_path).load(("A",))

    assert result.close.index[-1] == pd.Timestamp("2026-08-21")
    assert entry is not None
    assert entry.latest_date == date(2026, 8, 21)


def _completed_only_activity_tracker(tmp_path: Path) -> DirectionalActivityTracker:
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    store.save(
        DirectionalActivitySnapshot(
            "20260821",
            {
                "A2609": ContractActivity(
                    "A2609",
                    "DCE",
                    "A",
                    "20260821",
                    8_000,
                    30_000,
                    datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
                )
            },
        )
    )
    tracker = DirectionalActivityTracker(store)
    assert tracker.current_trading_day == ""
    return tracker


def test_completed_only_tracker_rejects_provider_row_after_required_day(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    provider.history.close.loc[pd.Timestamp("2026-08-24"), "A"] = 280.0
    provider.history.open.loc[pd.Timestamp("2026-08-24"), "A"] = 279.0

    with pytest.raises(RuntimeError, match="beyond authoritative planning day 2026-08-21"):
        _manager(
            provider=provider,
            activity_tracker=_completed_only_activity_tracker(tmp_path),
        )._load_signal(NOW, required_signal_day=date(2026, 8, 21))


def test_completed_only_tracker_falls_back_without_accepting_later_provider_row(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    cached_provider = _Provider()
    cached_provider.history = ExecutionAlignedSignalHistory(
        cached_provider.history.open.iloc[-140:],
        cached_provider.history.close.iloc[-140:],
    )
    _manager(provider=cached_provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )

    expanded_provider = _Provider()
    expanded_provider.history.close.loc[pd.Timestamp("2026-08-24"), "A"] = 280.0
    expanded_provider.history.open.loc[pd.Timestamp("2026-08-24"), "A"] = 279.0
    result = _manager(
        provider=expanded_provider,
        cache_path=cache_path,
        activity_tracker=_completed_only_activity_tracker(tmp_path),
    )._load_signal(NOW, required_signal_day=date(2026, 8, 21))
    entry = DirectionalOHLCCacheStore(cache_path).load(("A",))

    assert len(result.close) == 140
    assert entry is not None
    assert entry.row_count == 140


def test_provider_without_required_completed_day_does_not_write_disk_evidence(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"

    result = _manager(cache_path=cache_path)._load_signal(NOW)

    assert result.close.index[-1] == pd.Timestamp("2026-08-21")
    assert not cache_path.exists()


@pytest.mark.parametrize(
    ("field", "malformed"),
    [("date", "0001-01-01"), ("value", 10**1000)],
)
def test_cache_codec_wraps_datetime_and_numeric_range_failures(
    tmp_path: Path,
    field: str,
    malformed: object,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    provider = _Provider()
    _manager(provider=provider, cache_path=cache_path)._load_signal(
        NOW,
        required_signal_day=date(2026, 8, 21),
    )
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    if field == "date":
        envelope["content"]["dates"][0] = malformed
    else:
        envelope["content"]["open"][0][0] = malformed
    _resign_cache(envelope)
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(DirectionalOHLCCacheIntegrityError, match="directional OHLC cache"):
        DirectionalOHLCCacheStore(cache_path).load(("A",))


@pytest.mark.parametrize("failure_boundary", ["fsync", "replace"])
def test_atomic_cache_save_failure_preserves_previous_verified_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    cache_path = tmp_path / "directional_ohlc_cache.json"
    store = DirectionalOHLCCacheStore(cache_path)
    dates = pd.date_range(end="2026-08-21", periods=140, freq="B")
    original_close = pd.DataFrame({"A": range(100, 240)}, index=dates, dtype=float)
    original_open = original_close.shift(1).fillna(original_close.iloc[0])
    store.save(("A",), original_open, original_close)
    original_bytes = cache_path.read_bytes()

    if failure_boundary == "fsync":
        monkeypatch.setattr(
            ohlc_cache_module.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("injected fsync failure")),
        )
    else:
        monkeypatch.setattr(
            Path,
            "replace",
            lambda _source, _target: (_ for _ in ()).throw(OSError("injected replace failure")),
        )

    with pytest.raises(OSError, match="injected"):
        store.save(("A",), original_open + 1.0, original_close + 1.0)

    assert cache_path.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [cache_path]


def test_default_execution_aligned_runtime_requires_the_frozen_50_product_universe():
    assert len(FROZEN_PRODUCTS) == 50
    with pytest.raises(ValueError, match="frozen 50-product universe"):
        ExecutionAlignedDirectionalPortfolioManager(
            DirectionalConfig(
                enabled=True,
                products=("A", "M"),
                exchanges=("DCE",),
            ),
            _Broker(),
            RiskManager(RiskConfig()),
            signal_provider=_Provider(),
        )


def test_execution_aligned_flatten_closes_both_sides_when_same_contract_is_hedged():
    broker = _FlattenBroker()
    manager = ExecutionAlignedDirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            products=("A",),
            exchanges=("DCE",),
            signal_max_age_hours=120.0,
        ),
        broker,
        RiskManager(RiskConfig(max_contract_volume=10)),
        signal_provider=_Provider(),
        policy=_Policy(),
    )
    manager.bootstrap(NOW)
    manager.observe(
        Tick(
            symbol="A2609",
            exchange="DCE",
            timestamp=NOW,
            bid_price=99.0,
            ask_price=101.0,
            last_price=100.0,
            bid_volume=100.0,
            ask_volume=100.0,
            trading_day="20260825",
            volume=5000.0,
            open_interest=30000.0,
        )
    )

    result = manager.flatten(NOW)

    assert result.action == "reduce"
    assert len(broker.orders) == 2
    assert {order.side.value for order in broker.orders} == {"BUY", "SELL"}
    assert {order.volume for order in broker.orders} == {3}
    assert all(order.offset is not Offset.OPEN for order in broker.orders)
    assert all(order.order_type is OrderType.FAK for order in broker.orders)
    assert all(order.reference == "directional:flatten" for order in broker.orders)


def test_sina_provider_rejects_duplicate_daily_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = pd.DataFrame(
        {
            "date": ["2026-08-21", "2026-08-21"],
            "open": [100.0, 200.0],
            "close": [101.0, 201.0],
        }
    )
    fake_akshare = SimpleNamespace(futures_zh_daily_sina=lambda symbol: source)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    with pytest.raises(ValueError, match="duplicate daily date"):
        SinaContinuousOHLCProvider._load_one("A")


def test_execution_history_rejects_duplicate_daily_index() -> None:
    manager = _manager()
    dates = list(pd.date_range("2026-01-01", periods=140, freq="B"))
    dates.append(dates[-1])
    frame = pd.DataFrame({"A": range(len(dates))}, index=dates, dtype=float)
    history = ExecutionAlignedSignalHistory(frame, frame)

    with pytest.raises(RuntimeError, match="duplicate daily date"):
        manager._canonicalize_history(
            history,
            latest_allowed_date=date(2026, 8, 21),
        )
