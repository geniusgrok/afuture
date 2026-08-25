from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_CHINA = ZoneInfo("Asia/Shanghai")


def _local(hour: int, minute: int, *, day: int = 24) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=_CHINA)


def test_session_manifest_is_immutable_and_covers_the_frozen_fifty_products_once():
    from afuture.directional_sessions import (
        PRODUCT_SESSION_MANIFEST,
        SESSION_MANIFEST_EFFECTIVE_DATE,
        SESSION_MANIFEST_SOURCE_URLS,
        SESSION_MANIFEST_VERSION,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    assert tuple(PRODUCT_SESSION_MANIFEST) == FROZEN_PRODUCTS
    assert len(PRODUCT_SESSION_MANIFEST) == 50
    assert {row.product for row in PRODUCT_SESSION_MANIFEST.values()} == set(FROZEN_PRODUCTS)
    assert PRODUCT_SESSION_MANIFEST["A"].exchange == "DCE"
    assert PRODUCT_SESSION_MANIFEST["TA"].exchange == "CZCE"
    assert PRODUCT_SESSION_MANIFEST["AG"].exchange == "SHFE"
    assert PRODUCT_SESSION_MANIFEST["BC"].exchange == "INE"
    assert STRESS90_POLICY.session_manifest_digest
    assert SESSION_MANIFEST_VERSION == "stress90-cn-futures-sessions-v1"
    assert SESSION_MANIFEST_EFFECTIVE_DATE == "2026-08-25"
    assert set(SESSION_MANIFEST_SOURCE_URLS) == {"CZCE", "DCE", "INE", "SHFE"}
    with pytest.raises(TypeError):
        PRODUCT_SESSION_MANIFEST["A"] = PRODUCT_SESSION_MANIFEST["M"]  # type: ignore[index]


def test_session_manifest_has_explicit_audited_night_close_groups():
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST

    groups: dict[str, set[str]] = {}
    day_only: set[str] = set()
    for product, row in PRODUCT_SESSION_MANIFEST.items():
        if row.has_night_session:
            groups.setdefault(row.sessions[0], set()).add(product)
        else:
            day_only.add(product)

    assert groups["21:00-02:30"] == {"AG", "AU"}
    assert groups["21:00-01:00"] == {
        "AL",
        "BC",
        "BU",
        "CU",
        "HC",
        "NI",
        "PB",
        "RB",
        "SN",
        "SS",
        "ZN",
    }
    assert day_only == {"AP", "CJ", "LH", "PK", "SF", "SM", "UR"}


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_local(20, 54), "before"),
        (_local(20, 55), "open"),
        (_local(21, 10), "open"),
        (_local(21, 11), "missed"),
        (_local(0, 30, day=25), "missed"),
        (_local(14, 0, day=25), "missed"),
        (_local(16, 0, day=25), "before"),
    ],
)
def test_night_product_has_one_auditable_first_entry_window(now: datetime, expected: str):
    from afuture.directional_sessions import opening_window_status

    assert opening_window_status("A", now, action_category="entry").value == expected
    assert opening_window_status("A", now, action_category="same_sign_add").value == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_local(8, 54, day=25), "before"),
        (_local(8, 55, day=25), "open"),
        (_local(9, 10, day=25), "open"),
        (_local(9, 11, day=25), "missed"),
        (_local(21, 0), "before"),
    ],
)
def test_day_only_product_waits_for_the_first_day_window(now: datetime, expected: str):
    from afuture.directional_sessions import opening_window_status

    assert opening_window_status("LH", now, action_category="entry").value == expected


@pytest.mark.parametrize("category", ["reduction", "exit", "same_product_roll"])
def test_risk_reducing_and_roll_actions_bypass_entry_deadline(category: str):
    from afuture.directional_sessions import opening_window_status

    assert (
        opening_window_status("A", _local(14, 0, day=25), action_category=category).value
        == "bypass"
    )


def test_session_bucket_uses_ctp_trading_day_across_natural_midnight():
    from afuture.directional_sessions import session_bucket_for_tick

    before_midnight = session_bucket_for_tick("AG", _local(23, 45), "20260825")
    after_midnight = session_bucket_for_tick("AG", _local(0, 15, day=25), "20260825")

    assert before_midnight is not None and after_midnight is not None
    assert before_midnight.trading_day == after_midnight.trading_day == "20260825"
    assert before_midnight.session == after_midnight.session == "21:00-02:30"
    assert before_midnight.bucket_start.isoformat().endswith("23:00:00+08:00")
    assert after_midnight.bucket_start.isoformat().endswith("00:00:00+08:00")


def test_session_lookup_does_not_guess_weekends_or_holidays():
    from afuture.directional_sessions import opening_window_status, session_bucket_for_tick

    friday = datetime(2026, 8, 28, 21, 0, tzinfo=_CHINA)
    saturday = datetime(2026, 8, 29, 21, 0, tzinfo=_CHINA)

    assert opening_window_status("A", friday, action_category="entry") == opening_window_status(
        "A", saturday, action_category="entry"
    )
    assert session_bucket_for_tick("A", saturday, "20260901").trading_day == "20260901"


def test_ticks_outside_fixed_product_sessions_have_no_bucket():
    from afuture.directional_sessions import session_bucket_for_tick

    assert session_bucket_for_tick("A", _local(16, 0), "20260825") is None
    with pytest.raises(ValueError, match="unknown frozen product"):
        session_bucket_for_tick("UNKNOWN", _local(9, 0), "20260825")
