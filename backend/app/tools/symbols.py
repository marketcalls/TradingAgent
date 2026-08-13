"""Symbol services: search, symbol info, expiries, instrument master.

list_instruments is deliberately hostile to unfiltered calls. The NSE master is
thousands of rows; returning it whole would blow the tool budget and tell the model
nothing. A filter is required and the output is capped.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from agno.exceptions import RetryAgentRun
from agno.tools import Toolkit

from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.normalize import err, ok, to_json

log = logging.getLogger(__name__)

MAX_INSTRUMENT_ROWS = 60
# Search results are for identifying an instrument, so only the fields that help do that
# are returned. The raw rows carry token, brsymbol, brexchange, freeze_qty and tick_size
# as well, which pushed a 60-row reply past the 12,000-character tool budget and got the
# whole thing truncated.
SEARCH_ROWS = 25
SEARCH_FIELDS = ("symbol", "name", "exchange", "instrumenttype", "expiry", "strike",
                 "lotsize")


def _slim(row: dict) -> dict:
    return {k: row[k] for k in SEARCH_FIELDS if k in row and row[k] not in ("", None)}


class SymbolTools(Toolkit):
    def __init__(self, client: OpenAlgoClient | None = None, **kwargs: Any) -> None:
        self.client = client or get_client()
        super().__init__(
            name="symbol_tools",
            tools=[
                self.search_symbols,
                self.get_symbol_info,
                self.get_expiry_dates,
                self.list_instruments,
            ],
            **kwargs,
        )

    def search_symbols(self, query: str, exchange: str = "") -> str:
        """Find tradable symbols by free-text query.

        Use this whenever you are unsure of the exact OpenAlgo symbol instead of
        constructing one by hand. It resolves company names, partial symbols, and
        option descriptions such as "NIFTY 26000 DEC CE".

        Args:
            query (str): Free text, for example "reliance", "SBIN", or "NIFTY 26000 DEC CE".
            exchange (str): Optional exchange filter such as NSE, NFO or MCX. Leave empty
                to search everywhere.

        Returns:
            str: JSON list of matches with symbol, name, exchange, expiry, strike,
                lotsize, tick_size and instrumenttype.
        """
        q = (query or "").strip()
        if not q:
            raise RetryAgentRun("search_symbols needs a non-empty query.")

        # Searching the spoken name does not find the instrument: "NIFTY 50" returns
        # NIFTY500 and NIFTYNXT50, never plain NIFTY. Resolve the alias first.
        alias = C.resolve_index_alias(q)
        note = None
        if alias:
            note = f"{q!r} is not an OpenAlgo symbol; searched {alias} instead"
            q = alias

        kwargs: dict[str, Any] = {"query": q}
        ex = C.normalize_exchange(exchange)
        if ex:
            kwargs["exchange"] = ex
        res = self.client.call_enveloped("search", **kwargs)
        if not res["ok"]:
            raise RetryAgentRun(f"Symbol search failed for {q!r}: {res['error']}")
        data = res.get("data")
        if isinstance(data, list) and data:
            # Rank before truncating, or an exact match can be cut off entirely: the
            # raw order for "NIFTY 50" leads with NIFTY500 and NIFTYNXT50.
            from .market import _rank_matches
            ranked = _rank_matches(q, data)
            res["data"] = [_slim(r) for r in ranked[:SEARCH_ROWS]]
            if len(ranked) > SEARCH_ROWS:
                res["truncated"] = (f"showing the {SEARCH_ROWS} closest of "
                                    f"{len(ranked)} matches, best first")
        if note:
            res["corrected"] = note
        return to_json(res)

    def get_symbol_info(self, symbol: str, exchange: str) -> str:
        """Get contract details for an exact symbol.

        Call this before sizing a derivatives order: lot size and tick size decide the
        valid quantity and price increments.

        Args:
            symbol (str): Exact OpenAlgo symbol, for example NIFTY25AUG26FUT.
            exchange (str): Exchange code, for example NFO.

        Returns:
            str: JSON with lotsize, tick_size, freeze_qty, expiry, strike,
                instrumenttype and the broker-side symbol.
        """
        sym, ex = C.normalize_symbol(symbol), C.normalize_exchange(exchange)
        res = self.client.call_enveloped("symbol", symbol=sym, exchange=ex)
        if not res["ok"]:
            raise RetryAgentRun(
                f"No contract found for {sym} on {ex}: {res['error']}. "
                "Call search_symbols to find the exact symbol."
            )
        return to_json(res)

    def get_expiry_dates(self, symbol: str, exchange: str,
                         instrumenttype: str = "options") -> str:
        """List available expiry dates for a derivatives underlying.

        Args:
            symbol (str): Underlying base symbol, for example NIFTY or RELIANCE.
            exchange (str): Derivatives exchange, for example NFO, BFO, MCX or CDS.
            instrumenttype (str): Either "options" or "futures". Defaults to "options".

        Returns:
            str: JSON list of expiry dates.
        """
        it = (instrumenttype or "options").strip().lower()
        if it not in C.EXPIRY_INSTRUMENT_TYPES:
            raise RetryAgentRun(
                f"instrumenttype must be 'options' or 'futures', got {instrumenttype!r}."
            )
        res = self.client.call_enveloped(
            "expiry", symbol=C.normalize_symbol(symbol),
            exchange=C.normalize_exchange(exchange), instrumenttype=it,
        )
        if not res["ok"]:
            raise RetryAgentRun(f"Expiry lookup failed: {res['error']}")
        return to_json(res)

    def list_instruments(self, exchange: str, name_contains: str = "",
                         instrumenttype: str = "", limit: int = 40) -> str:
        """List contracts from the instrument master, filtered.

        The raw master runs to thousands of rows, so a filter is required and the
        output is capped. For a one-off lookup prefer search_symbols.

        Args:
            exchange (str): Exchange code. Required, for example NSE or NFO.
            name_contains (str): Case-insensitive substring matched against the symbol
                and the company name. Required unless instrumenttype is given.
            instrumenttype (str): Optional filter such as EQ, FUT, CE or PE.
            limit (int): Maximum rows to return, capped at 60. Defaults to 40.

        Returns:
            str: JSON list of matching contracts plus the total match count.
        """
        ex = C.normalize_exchange(exchange)
        if not ex:
            raise RetryAgentRun("list_instruments requires an exchange.")
        needle = (name_contains or "").strip().upper()
        itype = (instrumenttype or "").strip().upper()
        if not needle and not itype:
            raise RetryAgentRun(
                "list_instruments needs name_contains or instrumenttype; the unfiltered "
                "master is thousands of rows. Use search_symbols for a single lookup."
            )

        try:
            raw = self.client.call("instruments", exchange=ex)
        except Exception as exc:  # noqa: BLE001
            return to_json(err(f"{type(exc).__name__}: {exc}", "instruments"))

        if not isinstance(raw, pd.DataFrame):
            message = raw.get("message") if isinstance(raw, dict) else str(raw)
            raise RetryAgentRun(f"Instrument master unavailable for {ex}: {message}")

        df = raw
        if itype:
            if "instrumenttype" in df.columns:
                df = df[df["instrumenttype"].astype(str).str.upper() == itype]
        if needle:
            cols = [c for c in ("symbol", "name") if c in df.columns]
            if cols:
                mask = False
                for c in cols:
                    hit = df[c].astype(str).str.upper().str.contains(needle, regex=False,
                                                                     na=False)
                    mask = hit if mask is False else (mask | hit)
                df = df[mask]

        total = int(len(df))
        capped = min(max(int(limit or 40), 1), MAX_INSTRUMENT_ROWS)
        keep = [c for c in ("symbol", "name", "exchange", "instrumenttype", "expiry",
                            "strike", "lotsize", "tick_size") if c in df.columns]
        rows = df.head(capped)[keep].to_dict(orient="records") if keep else []

        return to_json(ok({"exchange": ex, "total_matches": total,
                           "returned": len(rows), "instruments": rows},
                          "instruments"))
