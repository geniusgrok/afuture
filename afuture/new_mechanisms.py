"""Causal calculations for offline event, curve, session and bought-option research.

These functions neither submit orders nor own runtime positions or risk. An edge
is a hypothesis; it requires qualified source versions and a separate fill replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite, log


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware evidence clock required")
    return value


@dataclass(frozen=True)
class Observation:
    value: float
    measured_at: datetime
    available_at: datetime
    source_sha256: str

    def before(self, decision: datetime) -> float:
        aware(decision)
        if aware(self.measured_at) > aware(self.available_at):
            raise ValueError("observation available before it was measured")
        if self.available_at >= decision:
            raise ValueError("information must be available strictly before decision")
        if not isfinite(self.value):
            raise ValueError("non-finite observation")
        if len(self.source_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in self.source_sha256
        ):
            raise ValueError("source byte identity required")
        return self.value


def event_residual(
    shock: Observation,
    response: Observation,
    *,
    response_started_at: datetime,
    decision: datetime,
    response_per_shock: float,
    calibration_matured_at: datetime,
    calibration_events: int,
) -> float:
    """Estimate remaining adjustment after a genuinely observable event response.

    ``shock`` can be a revision against an earlier observed version. It must not
    be called a consensus surprise unless actual prior consensus is supplied.
    Calibration contains only completed earlier event outcomes.
    """
    change, actual = shock.before(decision), response.before(decision)
    if aware(response_started_at) < shock.available_at:
        raise ValueError("response window precedes this event byte version")
    if response.measured_at <= response_started_at:
        raise ValueError("post-event response interval is empty")
    if aware(calibration_matured_at) >= decision or calibration_events < 12:
        raise ValueError("requires twelve strictly matured earlier events")
    if not isfinite(response_per_shock):
        raise ValueError("invalid causal response calibration")
    return change * response_per_shock - actual


def butterfly_lots(months: tuple[int, int, int]) -> tuple[int, int, int]:
    """Equal maturity spacing permits a whole-lot, duration-balanced butterfly."""
    near, middle, far = months
    if near >= middle or middle >= far or middle - near != far - middle:
        raise ValueError("three distinct equally spaced maturity months required")
    return 1, -2, 1


def anchored_curve_residual(
    anchor: Observation, prices: tuple[Observation, Observation, Observation], decision: datetime
) -> float:
    """Fair RMB price curvature minus actually observed three-leg curvature.

    The anchor must come from a separately evidenced inventory/spot/carry model;
    a rolling price mean is not a fundamental anchor.
    """
    values = tuple(price.before(decision) for price in prices)
    if min(values) <= 0:
        raise ValueError("positive concrete-contract prices required")
    return anchor.before(decision) - (values[0] - 2 * values[1] + values[2])


def cross_session_residual(
    foreign: tuple[Observation, Observation],
    fx: tuple[Observation, Observation],
    domestic: tuple[Observation, Observation],
    *,
    decision: datetime,
    closure_started_at: datetime,
    domestic_opened_at: datetime,
    beta: float,
    calibration_matured_at: datetime,
) -> float:
    """Subtract the already observed domestic reopening response from foreign news.

    Callers must qualify concrete foreign-contract identity, clocks, mapping,
    and the exchange closure calendar before using this calculation.
    """
    start, end = (point.before(decision) for point in foreign)
    fx_start, fx_end = (point.before(decision) for point in fx)
    cn_start, cn_open = (point.before(decision) for point in domestic)
    if min(start, end, fx_start, fx_end, cn_start, cn_open) <= 0:
        raise ValueError("positive executable price references required")
    closed, reopened = aware(closure_started_at), aware(domestic_opened_at)
    if not closed <= foreign[0].measured_at < foreign[1].measured_at <= reopened:
        raise ValueError("foreign interval must lie inside the domestic closure")
    if (
        domestic[0].measured_at > closed
        or domestic[1].measured_at < reopened
        or domestic[1].measured_at >= decision
        or fx[0].measured_at > foreign[0].measured_at
        or fx[1].measured_at > reopened
        or fx[1].measured_at < foreign[1].measured_at
    ):
        raise ValueError("foreign/FX/domestic session clocks do not align")
    if aware(calibration_matured_at) >= decision or not isfinite(beta):
        raise ValueError("mapping calibration has not matured")
    foreign_local = log(end * fx_end / (start * fx_start))
    return beta * foreign_local - log(cn_open / cn_start)


def bought_option_edge(
    *,
    ask: Observation,
    expected_resale_bid: float,
    calibration_matured_at: datetime,
    decision: datetime,
    planned_exit: datetime,
    exercise_cutoff: datetime,
    multiplier: float,
    roundtrip_fees: float,
) -> float:
    """Expected resale value versus actual ask, avoiding unsupported exercise.

    The forecast needs mature real option bid outcomes. Intrinsic value, a
    theoretical mid or an underlying return is not an executable resale bid.
    """
    premium = ask.before(decision)
    if not decision < aware(planned_exit) < aware(exercise_cutoff):
        raise ValueError("resale must precede the verified exercise cutoff")
    if aware(calibration_matured_at) >= decision:
        raise ValueError("option calibration contains unmatured outcomes")
    if (
        not all(
            isfinite(value) for value in (premium, expected_resale_bid, multiplier, roundtrip_fees)
        )
        or min(premium, multiplier) <= 0
        or min(expected_resale_bid, roundtrip_fees) < 0
    ):
        raise ValueError("invalid option price/unit/fees")
    return (expected_resale_bid - premium) * multiplier - roundtrip_fees


def bought_option_net(
    entry_ask: float, exit_bid: float, multiplier: float, lots: int, fees: float
) -> float:
    """Whole-lot premium cash identity; no short leg or expiry settlement assumed."""
    if isinstance(lots, bool) or not isinstance(lots, int) or lots <= 0:
        raise ValueError("positive integer bought-option lots required")
    if not all(isfinite(x) for x in (entry_ask, exit_bid, multiplier, fees)):
        raise ValueError("finite option cash inputs required")
    if min(entry_ask, multiplier) <= 0 or min(exit_bid, fees) < 0:
        raise ValueError("invalid option fill inputs")
    return (exit_bid - entry_ask) * multiplier * lots - fees
