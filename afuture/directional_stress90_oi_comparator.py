"""Order-incapable Shadow comparison of CTP raw evidence with a vendor reference."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from .directional_stress90_oi_runtime import CompletedOiEvidence
from .directional_stress90_policy import STRESS90_POLICY, canonical_stress90_digest


@dataclass(frozen=True)
class VendorOiProductEvidence:
    dominant_symbol: str
    first_open: float
    last_close: float
    first_hold: float
    last_hold: float
    total_volume: float
    flow: int


@dataclass(frozen=True)
class VendorOiReference:
    source: str
    trading_day: str
    products: Mapping[str, VendorOiProductEvidence]


@dataclass(frozen=True)
class OiEvidenceComparison:
    trading_day: str
    live_evidence_digest: str
    vendor_source: str
    product_results: Mapping[str, Mapping[str, object]]
    unexplained_flow_differences: tuple[str, ...]
    matched: bool
    comparison_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "trading_day": self.trading_day,
            "live_evidence_digest": self.live_evidence_digest,
            "vendor_source": self.vendor_source,
            "product_results": {
                product: dict(result) for product, result in self.product_results.items()
            },
            "unexplained_flow_differences": list(self.unexplained_flow_differences),
            "matched": self.matched,
            "comparison_digest": self.comparison_digest,
        }


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate vendor OI JSON key: {key}")
        result[key] = value
    return result


def load_vendor_oi_reference(
    path: str | Path,
) -> tuple[VendorOiReference, str]:
    """Load an operator-supplied immutable vendor snapshot; never fetch a network URL."""

    payload = Path(path).read_bytes()
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ValueError("vendor OI reference is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("vendor OI reference is not valid JSON") from exc
    fields = {"schema_version", "source", "trading_day", "products"}
    if not isinstance(raw, Mapping) or set(raw) != fields or raw["schema_version"] != 1:
        raise ValueError("vendor OI reference envelope is invalid")
    products_raw = raw["products"]
    if not isinstance(products_raw, Mapping):
        raise ValueError("vendor OI reference products are invalid")
    product_fields = {
        "dominant_symbol",
        "first_open",
        "last_close",
        "first_hold",
        "last_hold",
        "total_volume",
        "flow",
    }
    products: dict[str, VendorOiProductEvidence] = {}
    for product, row in products_raw.items():
        if (
            not isinstance(product, str)
            or not isinstance(row, Mapping)
            or set(row) != product_fields
        ):
            raise ValueError("vendor OI product record is invalid")
        products[product.upper()] = VendorOiProductEvidence(
            dominant_symbol=row["dominant_symbol"],
            first_open=row["first_open"],
            last_close=row["last_close"],
            first_hold=row["first_hold"],
            last_hold=row["last_hold"],
            total_volume=row["total_volume"],
            flow=row["flow"],
        )
    reference = _validate_vendor(
        VendorOiReference(
            source=raw["source"],
            trading_day=raw["trading_day"],
            products=products,
        )
    )
    return reference, sha256(payload).hexdigest()


def _validate_vendor(reference: VendorOiReference) -> VendorOiReference:
    if not isinstance(reference.source, str) or not reference.source.strip():
        raise ValueError("vendor OI reference source is required")
    if not isinstance(reference.trading_day, str) or len(reference.trading_day) != 8:
        raise ValueError("vendor OI reference trading day must be YYYYMMDD")
    if set(reference.products) != set(STRESS90_POLICY.oi_products):
        raise ValueError("vendor OI reference product manifest mismatch")
    normalized: dict[str, VendorOiProductEvidence] = {}
    for product in STRESS90_POLICY.oi_products:
        row = reference.products[product]
        if not isinstance(row, VendorOiProductEvidence) or not row.dominant_symbol:
            raise ValueError(f"vendor OI product identity is invalid: {product}")
        values = (
            row.first_open,
            row.last_close,
            row.first_hold,
            row.last_hold,
            row.total_volume,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or float(value) < 0.0
            for value in values
        ):
            raise ValueError(f"vendor OI product values are invalid: {product}")
        if row.first_open <= 0.0 or row.last_close <= 0.0:
            raise ValueError(f"vendor OI product prices are invalid: {product}")
        if isinstance(row.flow, bool) or row.flow not in (-1, 0, 1):
            raise ValueError(f"vendor OI product flow is invalid: {product}")
        normalized[product] = row
    return VendorOiReference(
        source=reference.source.strip(),
        trading_day=reference.trading_day,
        products=MappingProxyType(normalized),
    )


def compare_completed_oi_evidence(
    live: CompletedOiEvidence,
    vendor: VendorOiReference,
    *,
    absolute_tolerance: float = 1e-9,
) -> OiEvidenceComparison:
    """Compare all migration fields; any flow mismatch is an activation blocker."""

    reference = _validate_vendor(vendor)
    if not live.complete or any(
        live.flows[product] is None for product in STRESS90_POLICY.oi_products
    ):
        raise ValueError("CTP OI evidence must be complete before vendor comparison")
    if live.trading_day != reference.trading_day:
        raise ValueError("CTP/vendor OI trading-day mismatch")
    if not isfinite(absolute_tolerance) or absolute_tolerance < 0.0:
        raise ValueError("OI comparison tolerance must be finite and non-negative")

    results: dict[str, Mapping[str, object]] = {}
    flow_differences: list[str] = []
    matched = True
    fields = ("first_open", "last_close", "first_hold", "last_hold", "total_volume")
    for product in STRESS90_POLICY.oi_products:
        live_symbol = live.dominant_symbols[product]
        if live_symbol is None or live_symbol not in live.contracts:
            raise ValueError(f"CTP dominant OI evidence is missing: {product}")
        live_row = live.contracts[live_symbol]
        vendor_row = reference.products[product]
        numeric = {
            field: {
                "ctp": float(getattr(live_row, field)),
                "vendor": float(getattr(vendor_row, field)),
                "delta": float(getattr(live_row, field) - getattr(vendor_row, field)),
                "matched": abs(float(getattr(live_row, field) - getattr(vendor_row, field)))
                <= absolute_tolerance,
            }
            for field in fields
        }
        dominant_match = live_symbol.upper() == vendor_row.dominant_symbol.upper()
        live_flow = live.flows[product]
        if live_flow is None:  # pragma: no cover - complete evidence guard above
            raise ValueError(f"CTP OI flow is missing: {product}")
        flow_match = live_flow == vendor_row.flow
        product_match = (
            dominant_match
            and flow_match
            and all(bool(item["matched"]) for item in numeric.values())
        )
        if not flow_match:
            flow_differences.append(product)
        matched = matched and product_match
        results[product] = MappingProxyType(
            {
                "ctp_dominant_symbol": live_symbol,
                "vendor_dominant_symbol": vendor_row.dominant_symbol,
                "dominant_symbol_matched": dominant_match,
                "ctp_flow": live_flow,
                "vendor_flow": vendor_row.flow,
                "flow_matched": flow_match,
                "numeric": numeric,
                "matched": product_match,
            }
        )
    payload = {
        "trading_day": live.trading_day,
        "live_evidence_digest": live.evidence_digest,
        "vendor_source": reference.source,
        "vendor_products": {
            product: asdict(reference.products[product]) for product in STRESS90_POLICY.oi_products
        },
        "product_results": {product: dict(results[product]) for product in results},
        "unexplained_flow_differences": flow_differences,
        "matched": matched,
    }
    return OiEvidenceComparison(
        trading_day=live.trading_day,
        live_evidence_digest=live.evidence_digest,
        vendor_source=reference.source,
        product_results=MappingProxyType(results),
        unexplained_flow_differences=tuple(flow_differences),
        matched=matched,
        comparison_digest=canonical_stress90_digest(payload),
    )
