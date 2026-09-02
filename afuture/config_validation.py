"""Small, shared validation primitives for executable configuration."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite


def require_keys(raw: Mapping[str, object], allowed: set[str], section: str) -> None:
    """Reject misspelled or unsupported fields instead of silently ignoring them."""
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"{section} has unknown field(s): {', '.join(unknown)}")


def require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} keys must be strings")
    return value


def require_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def require_integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


def require_finite_number(value: object, field: str) -> float:
    if type(value) is int:
        return float(value)
    if type(value) is float and isfinite(value):
        return value
    raise ValueError(f"{field} must be a finite number")


def require_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a string")
    return value


def require_string_sequence(value: object, field: str) -> tuple[str, ...]:
    if type(value) is list and all(type(item) is str for item in value):
        return tuple(value)
    if type(value) is tuple and all(type(item) is str for item in value):
        return value
    if type(value) not in {list, tuple}:
        raise ValueError(f"{field} must be a list of strings")
    raise ValueError(f"{field} must be a list of strings")
