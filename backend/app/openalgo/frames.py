"""OHLCV frame cache.

Indicator work re-reads the same candles repeatedly - "show me RSI, MACD, ADX and
Supertrend on SBIN" is four indicators over one frame. This caches the CLEANED frame
so the NaN rule is enforced once, at the boundary.

Why cleaning matters: openalgo's _backend sma/rolling_sum are np.cumsum based, so a
single NaN anywhere in the input poisons every subsequent output. Measured on 300 bars
with one NaN injected at index 50, ta.sma(close, 14) returned 263/300 NaN. Broker
history routinely has gaps, so every frame is dropna().sort_index() before use.

This is deliberately NOT agno's Toolkit(cache_results=True): that one writes JSON to
disk and survives restarts, which is exactly wrong for market data.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import OrderedDict
from typing import Any

import pandas as pd

from .client import OpenAlgoClient, get_client

log = logging.getLogger(__name__)

CACHE_TTL_SEC = 60.0
CACHE_MAX_ENTRIES = 32

# Rough bars-per-day by interval, used to translate lookback_bars into a date range.
# Indian equity session is 6h15m = 375 minutes.
_BARS_PER_DAY = {
    "1m": 375, "3m": 125, "5m": 75, "10m": 37, "15m": 25,
    "30m": 13, "45m": 9, "60m": 7, "1h": 7, "D": 1, "W": 0.2, "M": 0.05,
}


class FrameCache:
    def __init__(self, client: OpenAlgoClient | None = None) -> None:
        self._client = client or get_client()
        self._store: OrderedDict[tuple, tuple[float, pd.DataFrame]] = OrderedDict()
        self._lock = threading.Lock()

    def _get(self, key: tuple) -> pd.DataFrame | None:
        import time
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            stamped, df = hit
            if time.monotonic() - stamped > CACHE_TTL_SEC:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return df

    def _put(self, key: tuple, df: pd.DataFrame) -> None:
        import time
        with self._lock:
            self._store[key] = (time.monotonic(), df)
            self._store.move_to_end(key)
            while len(self._store) > CACHE_MAX_ENTRIES:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def date_range_for(self, interval: str, lookback_bars: int) -> tuple[str, str]:
        """Translate a bar count into a start/end date, padded for weekends."""
        per_day = _BARS_PER_DAY.get(interval, 75)
        days = max(2, int(lookback_bars / max(per_day, 0.01)) + 1)
        # Roughly 5 trading days per 7 calendar days, plus slack for holidays.
        calendar_days = int(days * 7 / 5) + 5
        calendar_days = min(calendar_days, 2000)
        end = dt.date.today()
        start = end - dt.timedelta(days=calendar_days)
        return start.isoformat(), end.isoformat()

    def get_frame(self, symbol: str, exchange: str, interval: str,
                  start_date: str | None = None, end_date: str | None = None,
                  lookback_bars: int = 300, source: str = "api") -> dict[str, Any]:
        """Fetch candles, cleaned and sorted.

        Returns {"ok": True, "frame": DataFrame, "cached": bool} or
                {"ok": False, "error": str}.
        """
        if not start_date or not end_date:
            start_date, end_date = self.date_range_for(interval, lookback_bars)

        key = (symbol.upper(), exchange.upper(), interval, start_date, end_date, source)
        cached = self._get(key)
        if cached is not None:
            return {"ok": True, "frame": cached, "cached": True,
                    "start_date": start_date, "end_date": end_date}

        try:
            raw = self._client.call(
                "history", symbol=symbol.upper(), exchange=exchange.upper(),
                interval=interval, start_date=start_date, end_date=end_date,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # history returns a DataFrame on success and a dict on error.
        if not isinstance(raw, pd.DataFrame):
            message = "unknown error"
            if isinstance(raw, dict):
                message = str(raw.get("message") or raw.get("error") or raw)
            return {"ok": False, "error": message}

        if raw.empty:
            return {"ok": False,
                    "error": f"no candles for {symbol} on {exchange} at {interval} "
                             f"between {start_date} and {end_date}"}

        df = raw.copy()
        # Numeric coercion first, so a stray string becomes NaN and is then dropped
        # rather than exploding inside the Rust backend.
        for col in ("open", "high", "low", "close", "volume", "oi"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        essential = [c for c in ("open", "high", "low", "close") if c in df.columns]
        df = df.dropna(subset=essential) if essential else df.dropna()
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]

        if df.empty:
            return {"ok": False, "error": "all candles were incomplete after cleaning"}

        self._put(key, df)
        return {"ok": True, "frame": df, "cached": False,
                "start_date": start_date, "end_date": end_date}


_cache: FrameCache | None = None
_cache_lock = threading.Lock()


def get_frame_cache() -> FrameCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = FrameCache()
    return _cache
