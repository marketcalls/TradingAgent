"""Indicator registry and dispatcher for the OpenAlgo `ta` library."""

from .registry import (
    CATEGORIES,
    REGISTRY,
    IndicatorSpec,
    ParamSpec,
    get_spec,
    list_specs,
    validate_registry,
)

__all__ = [
    "CATEGORIES",
    "REGISTRY",
    "IndicatorSpec",
    "ParamSpec",
    "get_spec",
    "list_specs",
    "validate_registry",
]
