from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest


def _frames(products: tuple[str, ...], periods: int = 140):
    index = pd.date_range("2026-01-01", periods=periods, freq="D")
    values = [[100.0 + row + column for column in range(len(products))] for row in range(periods)]
    close = pd.DataFrame(values, index=index, columns=products)
    return close - 1.0, close


def test_stress90_order_path_loads_only_verified_cache_and_requires_completed_day(tmp_path):
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_ohlc_refresh import load_stress90_completed_ohlc

    products = ("A", "M")
    store = DirectionalOHLCCacheStore(tmp_path / "ohlc.json")
    open_prices, close = _frames(products)
    store.save(products, open_prices, close)

    entry = load_stress90_completed_ohlc(
        store,
        products=products,
        current_ctp_trading_day="20260525",
        authoritative_ctp_trading_day="20260525",
        required_completed_day=close.index[-1].strftime("%Y%m%d"),
    )
    assert entry.content_digest

    with pytest.raises(RuntimeError, match="required completed day"):
        load_stress90_completed_ohlc(
            store,
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
            required_completed_day="20260524",
        )


def test_explicit_refresh_is_append_only_and_rejects_current_day_or_revision(tmp_path):
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_ohlc_refresh import refresh_directional_ohlc_cache

    products = ("A", "M")
    store = DirectionalOHLCCacheStore(tmp_path / "ohlc.json")
    open_prices, close = _frames(products)
    store.save(products, open_prices.iloc[:-1], close.iloc[:-1])

    refreshed = refresh_directional_ohlc_cache(
        store,
        provider=SimpleNamespace(
            load=lambda _products: SimpleNamespace(open=open_prices, close=close)
        ),
        products=products,
        current_ctp_trading_day="20260525",
    )
    assert refreshed.row_count == 140

    revised = open_prices.copy()
    revised.iloc[0, 0] += 1.0
    with pytest.raises(RuntimeError, match="revised values"):
        refresh_directional_ohlc_cache(
            store,
            provider=SimpleNamespace(
                load=lambda _products: SimpleNamespace(open=revised, close=close)
            ),
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
        )

    with pytest.raises(RuntimeError, match="Broker-derived"):
        refresh_directional_ohlc_cache(
            store,
            provider=SimpleNamespace(
                load=lambda _products: SimpleNamespace(open=open_prices, close=close)
            ),
            products=products,
            current_ctp_trading_day="20260526",
            authoritative_ctp_trading_day="20260525",
        )

    future_open = pd.concat(
        [
            open_prices,
            pd.DataFrame([[999.0, 999.0]], index=[pd.Timestamp("2026-05-25")], columns=products),
        ]
    )
    future_close = pd.concat(
        [
            close,
            pd.DataFrame([[999.0, 999.0]], index=[pd.Timestamp("2026-05-25")], columns=products),
        ]
    )
    with pytest.raises(RuntimeError, match="current/future"):
        refresh_directional_ohlc_cache(
            store,
            provider=SimpleNamespace(
                load=lambda _products: SimpleNamespace(open=future_open, close=future_close)
            ),
            products=products,
            current_ctp_trading_day="20260525",
        )


def test_cache_refresh_cli_is_explicit_and_never_requires_ctp_credentials(
    tmp_path,
    monkeypatch,
    capsys,
):
    import afuture.cli as cli

    parsed = cli.build_parser().parse_args(
        [
            "directional-ohlc-refresh",
            "--config",
            "stress90.toml",
            "--current-trading-day",
            "20260825",
        ]
    )
    assert parsed.current_trading_day == "20260825"

    config = SimpleNamespace(
        state_path=str(tmp_path / "runtime" / "directional_state.json"),
        directional=SimpleNamespace(policy="stress90", products=("A", "M")),
    )
    credential_flags = []

    def fake_load_config(_path, *, require_ctp_credentials):
        credential_flags.append(require_ctp_credentials)
        return config

    calls = []

    def fake_refresh(
        store,
        *,
        provider,
        products,
        current_ctp_trading_day,
        authoritative_ctp_trading_day,
    ):
        calls.append(
            (
                store.path,
                provider,
                products,
                current_ctp_trading_day,
                authoritative_ctp_trading_day,
            )
        )
        return SimpleNamespace(
            latest_date=pd.Timestamp("2026-08-24").date(),
            row_count=170,
            content_digest="a" * 64,
        )

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(
        "afuture.directional_ohlc_refresh.refresh_directional_ohlc_cache",
        fake_refresh,
    )
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    TradingDayEvidenceStore(tmp_path / "runtime" / "ctp_trading_day_evidence.json").save(
        trading_day="20260825",
        account_identity_digest="a" * 64,
    )

    assert (
        cli.main(
            [
                "directional-ohlc-refresh",
                "--config",
                "stress90.toml",
                "--current-trading-day",
                "20260825",
            ]
        )
        == 0
    )
    assert credential_flags == [False]
    assert calls[0][0] == tmp_path / "runtime" / "directional_ohlc_cache.json"
    assert calls[0][2:] == (("A", "M"), "20260825", "20260825")
    assert '"latest_completed_day": "20260824"' in capsys.readouterr().out
