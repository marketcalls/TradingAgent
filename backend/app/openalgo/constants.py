"""OpenAlgo enumerations and symbology helpers.

Values come from docs/prompt/order-constants.md and the shared validation constants in
the OpenAlgo server. Broker capability metadata decides which subset is actually usable,
so these are the outer bound, not a guarantee.
"""

from __future__ import annotations

import re

# Tradable venues.
TRADABLE_EXCHANGES: tuple[str, ...] = (
    "NSE", "NFO", "CDS", "BSE", "BFO", "BCD", "MCX", "NCDEX", "NCO", "CRYPTO",
)

# Quote-only venues. Orders against these must be refused before they reach the broker.
INDEX_EXCHANGES: tuple[str, ...] = (
    "NSE_INDEX", "BSE_INDEX", "MCX_INDEX", "GLOBAL_INDEX",
)

ALL_EXCHANGES: tuple[str, ...] = TRADABLE_EXCHANGES + INDEX_EXCHANGES

PRODUCTS: tuple[str, ...] = ("MIS", "CNC", "NRML")
# GTT accepts a narrower set than regular orders.
GTT_PRODUCTS: tuple[str, ...] = ("CNC", "NRML")

PRICE_TYPES: tuple[str, ...] = ("MARKET", "LIMIT", "SL", "SL-M")
GTT_PRICE_TYPES: tuple[str, ...] = ("LIMIT", "MARKET")

ACTIONS: tuple[str, ...] = ("BUY", "SELL")

GTT_TRIGGER_TYPES: tuple[str, ...] = ("SINGLE", "OCO")

OPTION_TYPES: tuple[str, ...] = ("CE", "PE")

# /expiry takes these; instrument records report EQ/FUT/CE/PE/PERPFUT instead.
EXPIRY_INSTRUMENT_TYPES: tuple[str, ...] = ("options", "futures")

# The canonical set. The real list is broker-specific - always confirm with /intervals.
KNOWN_INTERVALS: tuple[str, ...] = (
    "1m", "3m", "5m", "10m", "15m", "30m", "45m", "60m", "1h", "D", "W", "M",
)

_OFFSET_RE = re.compile(r"^(ATM|ITM([1-9]|[1-4][0-9]|50)|OTM([1-9]|[1-4][0-9]|50))$")


def is_valid_offset(offset: str) -> bool:
    """ATM, ITM1..ITM50, OTM1..OTM50."""
    return bool(_OFFSET_RE.match((offset or "").strip().upper()))


def is_index_exchange(exchange: str) -> bool:
    return (exchange or "").strip().upper() in INDEX_EXCHANGES


def normalize_exchange(exchange: str) -> str:
    return (exchange or "").strip().upper()


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def normalize_action(action: str) -> str:
    return (action or "").strip().upper()


def normalize_product(product: str) -> str:
    return (product or "").strip().upper()


def normalize_price_type(price_type: str) -> str:
    """Accepts SLM / SL_M / sl-m and returns the canonical SL-M."""
    raw = (price_type or "").strip().upper().replace("_", "-")
    if raw in ("SLM", "SL M"):
        return "SL-M"
    return raw


def describe_exchange(exchange: str) -> str:
    return {
        "NSE": "NSE equity",
        "BSE": "BSE equity",
        "NFO": "NSE futures and options",
        "BFO": "BSE futures and options",
        "CDS": "NSE currency derivatives",
        "BCD": "BSE currency derivatives",
        "MCX": "MCX commodities",
        "NCDEX": "NCDEX commodities",
        "NCO": "NSE commodities",
        "CRYPTO": "Crypto derivatives",
        "NSE_INDEX": "NSE indices (quote only)",
        "BSE_INDEX": "BSE indices (quote only)",
        "MCX_INDEX": "MCX sectoral indices (quote only)",
        "GLOBAL_INDEX": "Global indices (quote only)",
    }.get(normalize_exchange(exchange), "unknown exchange")
