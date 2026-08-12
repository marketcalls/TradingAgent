"""Market data toolkit: quotes, depth, candles, intervals, calendar.

Agno schema rules this file depends on (verified against agno 2.8.7):
  - The JSON schema is built from get_type_hints plus a docstring-parser pass, so every
    tool needs real type hints AND a Google-style Args: block or the schema is unusable.
  - Instance attributes must be set BEFORE super().__init__, because the parent inspects
    the bound methods passed in tools=[...].
  - cache_results is an ON-DISK JSON cache that survives restarts. It is never enabled
    here: a cached quote is a wrong quote.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.exceptions import RetryAgentRun
from agno.tools import Toolkit

from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.frames import FrameCache, get_frame_cache
from ..openalgo.normalize import err, frame_summary, ok, to_json

log = logging.getLogger(__name__)


class MarketDataTools(Toolkit):
    def __init__(self, client: OpenAlgoClient | None = None,
                 frames: FrameCache | None = None, **kwargs: Any) -> None:
        self.client = client or get_client()
        self.frames = frames or get_frame_cache()
        super().__init__(
            name="market_data_tools",
            tools=[
                self.get_quote,
                self.get_quotes_bulk,
                self.get_market_depth,
                self.get_history,
                self.get_supported_intervals,
                self.get_market_calendar,
            ],
            **kwargs,
        )

    def get_quote(self, symbol: str, exchange: str) -> str:
        """Get the latest price snapshot for one instrument.

        Args:
            symbol (str): OpenAlgo symbol. Equity is the bare base symbol (RELIANCE).
                Futures are BASE+DDMMMYY+FUT. Options are BASE+DDMMMYY+STRIKE+CE/PE.
            exchange (str): Exchange code such as NSE, BSE, NFO, MCX, or NSE_INDEX for
                index quotes.

        Returns:
            str: JSON with ltp, open, high, low, prev_close, bid, ask and volume.
        """
        sym, ex = C.normalize_symbol(symbol), C.normalize_exchange(exchange)
        res = self.client.call_enveloped("quotes", symbol=sym, exchange=ex)
        if not res["ok"]:
            raise RetryAgentRun(
                f"Quote failed for {sym} on {ex}: {res['error']}. "
                "Check the exchange (equities are NSE or BSE, derivatives NFO or BFO, "
                "indices NSE_INDEX) or call search_symbols to find the exact symbol."
            )
        return to_json({**res, "symbol": sym, "exchange": ex})

    def get_quotes_bulk(self, symbols: list[dict]) -> str:
        """Get price snapshots for several instruments in one round trip.

        Prefer this over repeated get_quote calls when pricing a watchlist or a basket.

        Args:
            symbols (list[dict]): Instruments to quote, each a dict with 'symbol' and
                'exchange' keys, for example
                [{"symbol": "RELIANCE", "exchange": "NSE"}, {"symbol": "TCS", "exchange": "NSE"}].

        Returns:
            str: JSON with one result per requested instrument.
        """
        if not symbols:
            return to_json(err("no symbols supplied", "multiquotes"))
        cleaned = []
        for item in symbols:
            if not isinstance(item, dict):
                continue
            sym = C.normalize_symbol(str(item.get("symbol", "")))
            ex = C.normalize_exchange(str(item.get("exchange", "")))
            if sym and ex:
                cleaned.append({"symbol": sym, "exchange": ex})
        if not cleaned:
            raise RetryAgentRun(
                "No usable instruments. Each entry needs both 'symbol' and 'exchange', "
                'for example [{"symbol": "SBIN", "exchange": "NSE"}].'
            )
        res = self.client.call_enveloped("multiquotes", symbols=cleaned)
        return to_json(res)

    def get_market_depth(self, symbol: str, exchange: str) -> str:
        """Get the five-level order book for an instrument.

        Args:
            symbol (str): OpenAlgo symbol.
            exchange (str): Exchange code.

        Returns:
            str: JSON with bids, asks, total buy and sell quantity, ltp and volume.
        """
        sym, ex = C.normalize_symbol(symbol), C.normalize_exchange(exchange)
        res = self.client.call_enveloped("depth", symbol=sym, exchange=ex)
        if not res["ok"]:
            raise RetryAgentRun(f"Depth failed for {sym} on {ex}: {res['error']}")
        return to_json(res)

    def get_history(self, symbol: str, exchange: str, interval: str = "5m",
                    lookback_bars: int = 200, start_date: str = "",
                    end_date: str = "", last_n: int = 10) -> str:
        """Get historical candles, summarized.

        Returns statistics plus the most recent candles rather than the whole series,
        because a full frame is expensive and rarely what the question needs. To compute
        an indicator over the full series, use compute_indicator instead.

        Args:
            symbol (str): OpenAlgo symbol.
            exchange (str): Exchange code.
            interval (str): Candle interval such as 1m, 5m, 15m, 1h or D. Call
                get_supported_intervals first if unsure; the set is broker-specific.
            lookback_bars (int): Approximate number of bars to fetch when no explicit
                date range is given. Defaults to 200.
            start_date (str): Optional start date as YYYY-MM-DD. Overrides lookback_bars.
            end_date (str): Optional end date as YYYY-MM-DD.
            last_n (int): How many recent candles to include in the reply. Defaults to 10.

        Returns:
            str: JSON with bar count, period high and low, percent change, and the last
                n candles.
        """
        sym, ex = C.normalize_symbol(symbol), C.normalize_exchange(exchange)
        res = self.frames.get_frame(
            sym, ex, interval,
            start_date=start_date or None, end_date=end_date or None,
            lookback_bars=max(int(lookback_bars or 200), 10),
        )
        if not res["ok"]:
            raise RetryAgentRun(
                f"History failed for {sym} on {ex} at {interval}: {res['error']}. "
                "Call get_supported_intervals to see what this broker actually offers."
            )
        summary = frame_summary(res["frame"], last_n=max(int(last_n or 10), 1))
        return to_json(ok({**summary, "symbol": sym, "exchange": ex,
                           "interval": interval,
                           "start_date": res.get("start_date"),
                           "end_date": res.get("end_date")}, "history"))

    def get_supported_intervals(self) -> str:
        """List the candle intervals this broker actually supports.

        The set is broker-specific. Some brokers have no weekly or monthly candles, so
        check here before requesting one.

        Returns:
            str: JSON grouped into seconds, minutes, hours, days, weeks and months.
        """
        res = self.client.call_enveloped("intervals")
        return to_json(res)

    def get_market_calendar(self, year: int = 0, date: str = "") -> str:
        """Get exchange holidays for a year, or trading session times for a date.

        Args:
            year (int): Calendar year for the holiday list, for example 2026. Pass 0 to
                skip the holiday lookup.
            date (str): Date as YYYY-MM-DD for session timings. Leave empty to skip.

        Returns:
            str: JSON with a holidays list, a timings list, or both.
        """
        out: dict[str, Any] = {}
        if year:
            out["holidays"] = self.client.call_enveloped("holidays", year=int(year))
        if date:
            out["timings"] = self.client.call_enveloped("timings", date=date)
        if not out:
            out["holidays"] = self.client.call_enveloped("holidays")
        return to_json(ok(out, "market_calendar"))
