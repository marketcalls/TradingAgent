"""Account toolkit: funds, books, holdings, margin.

All read-only, so none of these are confirmation-gated. None are cached either -
a stale position is worse than a slow one.

Note the spelling split the SDK imposes: order calls take price_type at the top level,
but the dicts inside margin() and basketorder() use pricetype. calculate_margin below
normalizes whatever the model sends into the form margin() actually wants.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.exceptions import RetryAgentRun
from agno.tools import Toolkit

from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.normalize import ok, to_json

log = logging.getLogger(__name__)


class AccountTools(Toolkit):
    def __init__(self, client: OpenAlgoClient | None = None, **kwargs: Any) -> None:
        self.client = client or get_client()
        super().__init__(
            name="account_tools",
            tools=[
                self.get_funds,
                self.get_orderbook,
                self.get_tradebook,
                self.get_positionbook,
                self.get_holdings,
                self.calculate_margin,
            ],
            **kwargs,
        )

    def get_funds(self) -> str:
        """Get available cash, collateral, utilised margin and realised P&L.

        Returns:
            str: JSON with availablecash, collateral, utiliseddebits, m2mrealized and
                m2munrealized.
        """
        return to_json(self.client.call_enveloped("funds"))

    def get_orderbook(self) -> str:
        """Get today's orders and their statuses.

        Returns:
            str: JSON with the order list and counts of buy, sell, completed, open and
                rejected orders.
        """
        return to_json(self.client.call_enveloped("orderbook"))

    def get_tradebook(self) -> str:
        """Get today's executed trades with fill prices.

        Returns:
            str: JSON list of trades with action, symbol, quantity, average_price and
                trade_value.
        """
        return to_json(self.client.call_enveloped("tradebook"))

    def get_positionbook(self, symbol: str = "", exchange: str = "",
                         product: str = "") -> str:
        """Get open positions and their P&L, optionally filtered to one instrument.

        Args:
            symbol (str): Optional symbol filter, for example SBIN. Leave empty for all.
            exchange (str): Optional exchange filter, used together with symbol.
            product (str): Optional product filter such as MIS, CNC or NRML.

        Returns:
            str: JSON list of positions with quantity, average_price, ltp and pnl,
                plus aggregate P&L.
        """
        res = self.client.call_enveloped("positionbook")
        if not res["ok"]:
            return to_json(res)

        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        prod = C.normalize_product(product)
        data = res.get("data")
        if isinstance(data, list) and (sym or ex or prod):
            filtered = [
                p for p in data
                if (not sym or C.normalize_symbol(str(p.get("symbol", ""))) == sym)
                and (not ex or C.normalize_exchange(str(p.get("exchange", ""))) == ex)
                and (not prod or C.normalize_product(str(p.get("product", ""))) == prod)
            ]
            res["data"] = filtered
            res["filter"] = {"symbol": sym or None, "exchange": ex or None,
                             "product": prod or None}
            res["matched"] = len(filtered)
        return to_json(res)

    def get_holdings(self) -> str:
        """Get long-term holdings and their unrealised P&L.

        Returns:
            str: JSON with the holdings list and totals for value, investment and P&L.
        """
        return to_json(self.client.call_enveloped("holdings"))

    def calculate_margin(self, positions: list[dict]) -> str:
        """Estimate the margin a set of positions would require, before placing them.

        Use this to check affordability for multi-leg option strategies, where the
        hedged requirement is far lower than the sum of the legs.

        Args:
            positions (list[dict]): Legs to price, each with symbol, exchange, action
                (BUY or SELL), product, pricetype and quantity. Example:
                [{"symbol": "NIFTY25AUG2625000CE", "exchange": "NFO", "action": "BUY",
                  "product": "NRML", "pricetype": "MARKET", "quantity": "75"}]

        Returns:
            str: JSON with total_margin_required, span_margin and exposure_margin.
        """
        if not positions:
            raise RetryAgentRun("calculate_margin needs at least one position.")

        cleaned: list[dict[str, Any]] = []
        for leg in positions:
            if not isinstance(leg, dict):
                continue
            # The API wants 'pricetype' inside this list, not 'price_type'.
            pt = leg.get("pricetype") or leg.get("price_type") or "MARKET"
            cleaned.append({
                "symbol": C.normalize_symbol(str(leg.get("symbol", ""))),
                "exchange": C.normalize_exchange(str(leg.get("exchange", ""))),
                "action": C.normalize_action(str(leg.get("action", ""))),
                "product": C.normalize_product(str(leg.get("product", "NRML"))),
                "pricetype": C.normalize_price_type(str(pt)),
                "quantity": str(leg.get("quantity", 0)),
            })
        if not cleaned:
            raise RetryAgentRun(
                "No usable positions. Each needs symbol, exchange, action, product, "
                "pricetype and quantity."
            )

        res = self.client.call_enveloped("margin", positions=cleaned)
        if not res["ok"]:
            raise RetryAgentRun(f"Margin calculation failed: {res['error']}")
        return to_json(ok({**(res.get("data") or {}), "legs": len(cleaned)}, "margin"))
