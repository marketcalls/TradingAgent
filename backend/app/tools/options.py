"""Options toolkit: chain, Greeks, symbol resolution, and the two options order paths.

Runtime traps this module is written against:

  - optionchain takes with_greeks only through **kwargs. The SDK has no named parameter
    for it, but options.py passes optionchain kwargs through RAW (no str() cast), so the
    server receives JSON true rather than the string "True". Greeks come back inside the
    same response with no extra broker calls and no 50-symbol cap, which is why this is
    the preferred Greeks source; multioptiongreeks is capped at 50 and needs a second
    round trip.

  - multioptiongreeks has no SDK wrapper. It goes through client.raw_post with no
    leading slash on the endpoint name, and it reports batch success even when
    individual symbols failed, so every item is inspected.

  - optionsymbol's strategy parameter defaults to None and is DEPRECATED - passing it
    raises a DeprecationWarning. It is the one call in this file that must NOT carry the
    strategy name. optionsmultiorder is the opposite: strategy is required with no
    default.

  - optionsmultiorder raises raw Python exceptions on a malformed leg (KeyError on a
    missing offset, ValueError on a non-numeric quantity) instead of returning an error
    dict. Legs are validated here so the model gets a fixable message.

  - price_type at the top level of optionsorder, but pricetype inside an
    optionsmultiorder leg dict. Same silent-default trap as the basket legs.

  - NSE_INDEX and BSE_INDEX are quote-only for ordinary orders, but they are the correct
    exchange for the options endpoints, where they identify the UNDERLYING. The actual
    order is routed to NFO or BFO. RiskGuard would refuse an index exchange outright, so
    the routed exchange is what gets checked.

  - The strike is resolved from the live underlying price at request time, so the exact
    instrument is unknown until the call runs. Both order tools resolve the symbol first
    through optionsymbol, so the risk check, the audit row and the confirmation card all
    name the real contract.

  - Three renderings of an expiry are in play: DDMMMYY in requests (25AUG26), DD-MMM-YY
    in the /expiry response (25-AUG-26), and DDMMMYY again inside a symbol. Anything
    coming back from get_expiry_dates is converted before it is sent.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.exceptions import RetryAgentRun

from ..config import Settings
from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient
from ..openalgo.normalize import ok, to_json
from ..safety.audit import AuditLog
from ..safety.risk import RiskGuard
from .orders import MutatingToolkit, num

log = logging.getLogger(__name__)

# The server accepts 1 to 50 symbols per multioptiongreeks call.
MAX_GREEKS_BATCH = 50
# optionsmultiorder accepts 1 to 20 legs.
MAX_OPTION_LEGS = 20

# Fields kept from each side of a chain row. The raw response carries roughly twice
# this, and a wide chain would blow the 12,000 character tool budget.
_CHAIN_FIELDS: tuple[str, ...] = (
    "symbol", "label", "ltp", "bid", "ask", "volume", "oi", "lotsize",
    "implied_volatility", "delta", "gamma", "theta", "vega",
)

# Underlying exchange -> exchange the resulting order actually trades on.
_ROUTES: dict[str, str] = {
    "NSE_INDEX": "NFO", "NSE": "NFO", "NFO": "NFO",
    "BSE_INDEX": "BFO", "BSE": "BFO", "BFO": "BFO",
}


def route_exchange(exchange: str) -> str:
    """Exchange an option order really lands on. NSE_INDEX routes to NFO, BSE_INDEX to
    BFO. Used for the risk check, which must never see a quote-only exchange."""
    ex = C.normalize_exchange(exchange)
    return _ROUTES.get(ex, ex)


def to_ddmmmyy(value: str) -> str:
    """25-AUG-26 (the /expiry response) becomes 25AUG26 (what a request wants)."""
    return (value or "").replace("-", "").replace(" ", "").upper()


class OptionsTools(MutatingToolkit):
    def __init__(self, client: OpenAlgoClient | None = None,
                 settings: Settings | None = None,
                 guard: RiskGuard | None = None,
                 audit: AuditLog | None = None,
                 session_id: str | None = None,
                 **kwargs: Any) -> None:
        super().__init__(
            name="options_tools",
            tools=[
                self.get_option_chain,
                self.resolve_option_symbol,
                self.get_option_greeks,
                self.get_synthetic_future,
                self.place_options_order,
                self.place_options_multi_order,
            ],
            requires_confirmation_tools=[
                "place_options_order",
                "place_options_multi_order",
            ],
            client=client, settings=settings, guard=guard, audit=audit,
            session_id=session_id, **kwargs,
        )

    # -- read-only tools ---------------------------------------------------

    def get_option_chain(self, underlying: str, exchange: str = "NSE_INDEX",
                         expiry_date: str = "", strike_count: int = 10,
                         with_greeks: bool = True, interest_rate: float = 0.0) -> str:
        """Get the option chain around the money, with implied volatility and Greeks.

        This is the cheapest way to see Greeks: they arrive inside the chain response,
        with no extra broker calls and no symbol cap. Each row is labelled ATM, ITMn or
        OTMn, which are the same offsets the order tools take.

        Args:
            underlying (str): Underlying symbol such as NIFTY, BANKNIFTY or RELIANCE.
            exchange (str): Exchange of the UNDERLYING: NSE_INDEX for index options,
                BSE_INDEX for SENSEX, NFO or BFO for stock options. Defaults to
                NSE_INDEX.
            expiry_date (str): Expiry as DDMMMYY, for example 25AUG26. Leave empty to
                use the nearest available expiry.
            strike_count (int): Number of strikes to return on each side of the money.
                Keep it small; a wide chain is truncated. Defaults to 10.
            with_greeks (bool): Include implied volatility, delta, gamma, theta and
                vega. Defaults to True.
            interest_rate (float): Risk-free rate as a percentage, used for the Greeks.
                Defaults to 0.

        Returns:
            str: JSON with the underlying price, the ATM strike, and one compacted row
                per strike carrying both the call and the put.
        """
        und = C.normalize_symbol(underlying)
        ex = C.normalize_exchange(exchange) or "NSE_INDEX"
        expiry = self._resolve_expiry(und, ex, expiry_date)

        res = self.client.call_enveloped(
            "optionchain", underlying=und, exchange=ex, expiry_date=expiry,
            strike_count=max(int(strike_count or 10), 1),
            with_greeks=bool(with_greeks),   # raw kwarg: reaches the wire as JSON true
            interest_rate=float(interest_rate or 0))
        if not res["ok"]:
            raise RetryAgentRun(
                f"Option chain failed for {und} {expiry} on {ex}: {res['error']}. "
                "Check the expiry with get_expiry_dates, and use NSE_INDEX for index "
                "options."
            )

        data = res.get("data") or {}
        chain = data.get("chain") or []
        rows = []
        for row in chain:
            if not isinstance(row, dict):
                continue
            rows.append({
                "strike": row.get("strike"),
                "ce": _compact_leg(row.get("ce")),
                "pe": _compact_leg(row.get("pe")),
            })
        return to_json(ok({
            "underlying": data.get("underlying", und),
            "underlying_ltp": data.get("underlying_ltp"),
            "underlying_prev_close": data.get("underlying_prev_close"),
            "exchange": ex,
            "expiry_date": data.get("expiry_date", expiry),
            "atm_strike": data.get("atm_strike"),
            "forward_price": data.get("forward_price"),
            "greeks_included": data.get("greeks_included", bool(with_greeks)),
            "strikes": len(rows),
            "chain": rows,
        }, "optionchain"))

    def resolve_option_symbol(self, underlying: str, offset: str, option_type: str,
                              exchange: str = "NSE_INDEX",
                              expiry_date: str = "") -> str:
        """Turn a strike offset into the exact tradable option symbol.

        Call this before an option order so the user sees the real contract, its lot
        size and the underlying price rather than an offset that resolves at execution
        time.

        Args:
            underlying (str): Underlying symbol such as NIFTY or BANKNIFTY.
            offset (str): ATM, or ITM1 to ITM50, or OTM1 to OTM50. No space and no zero
                padding. For a call, ITM is a lower strike; for a put it is a higher one.
            option_type (str): CE for a call or PE for a put.
            exchange (str): Exchange of the underlying, usually NSE_INDEX or BSE_INDEX.
                Defaults to NSE_INDEX.
            expiry_date (str): Expiry as DDMMMYY, for example 25AUG26. Leave empty for
                the nearest expiry.

        Returns:
            str: JSON with the resolved symbol, the exchange it trades on, lot size,
                tick size, freeze quantity and the underlying price.
        """
        und = C.normalize_symbol(underlying)
        ex = C.normalize_exchange(exchange) or "NSE_INDEX"
        off, opt = _validate_offset(offset, option_type)
        expiry = self._resolve_expiry(und, ex, expiry_date)

        res = self._resolve_symbol(und, ex, off, opt, expiry)
        if not res["ok"]:
            raise RetryAgentRun(
                f"Could not resolve {und} {expiry} {off} {opt} on {ex}: {res['error']}. "
                "Check the expiry with get_expiry_dates."
            )
        data = res.get("data") or {}
        return to_json(ok({**data, "underlying": und, "expiry_date": expiry,
                           "offset": off, "option_type": opt}, "optionsymbol"))

    def get_option_greeks(self, symbol: str = "", exchange: str = "",
                          symbols: list[dict] | None = None,
                          interest_rate: float = 0.0,
                          expiry_time: str = "") -> str:
        """Get implied volatility and Greeks for one option, or for a batch.

        Pass symbol and exchange for a single contract. Pass symbols for up to 50
        contracts in one round trip. For a whole chain, prefer get_option_chain, which
        returns the same Greeks without a symbol cap.

        Args:
            symbol (str): Option symbol such as NIFTY18AUG2624450CE. Used when symbols
                is empty.
            exchange (str): Exchange the option trades on: NFO, BFO, CDS or MCX.
            symbols (list[dict]): Optional batch, each entry a dict with 'symbol' and
                'exchange', for example
                [{"symbol": "NIFTY18AUG2624450CE", "exchange": "NFO"}]. Maximum 50.
            interest_rate (float): Risk-free rate as a percentage. Defaults to 0.
            expiry_time (str): Expiry time of day as HH:MM, when the broker default is
                wrong. Leave empty otherwise.

        Returns:
            str: JSON with implied volatility and delta, gamma, theta, vega and rho,
                per symbol.
        """
        batch = [s for s in (symbols or []) if isinstance(s, dict)]
        if batch:
            if len(batch) > MAX_GREEKS_BATCH:
                raise RetryAgentRun(
                    f"multioptiongreeks accepts at most {MAX_GREEKS_BATCH} symbols, got "
                    f"{len(batch)}. Split the request, or call get_option_chain instead - "
                    "it returns Greeks for the whole chain in one call."
                )
            cleaned = []
            for item in batch:
                sym = C.normalize_symbol(str(item.get("symbol", "")))
                ex = C.normalize_exchange(str(item.get("exchange", "")))
                if sym and ex:
                    cleaned.append({"symbol": sym, "exchange": ex})
            if not cleaned:
                raise RetryAgentRun(
                    "each entry in symbols needs both 'symbol' and 'exchange'."
                )
            payload: dict[str, Any] = {"symbols": cleaned}
            if interest_rate:
                payload["interest_rate"] = float(interest_rate)
            if expiry_time:
                payload["expiry_time"] = expiry_time
            res = self.client.raw_post("multioptiongreeks", payload)
            if not res["ok"]:
                raise RetryAgentRun(f"Batch Greeks failed: {res['error']}")
            items = res.get("data") or []
            # The batch reports success even when individual symbols failed.
            failed = [i.get("symbol") for i in items
                      if isinstance(i, dict) and str(i.get("status", "success")) != "success"]
            return to_json(ok({"requested": len(cleaned), "returned": len(items),
                               "failed": failed, "greeks": items},
                              "multioptiongreeks"))

        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        if not sym or not ex:
            raise RetryAgentRun(
                "get_option_greeks needs either symbol and exchange, or a symbols list."
            )
        kwargs: dict[str, Any] = {"symbol": sym, "exchange": ex}
        if interest_rate:
            kwargs["interest_rate"] = float(interest_rate)
        if expiry_time:
            kwargs["expiry_time"] = expiry_time
        res = self.client.call_enveloped("optiongreeks", **kwargs)
        if not res["ok"]:
            raise RetryAgentRun(
                f"Greeks failed for {sym} on {ex}: {res['error']}. Options trade on NFO "
                "or BFO, not on the index exchange."
            )
        return to_json(res)

    def get_synthetic_future(self, underlying: str, exchange: str = "NSE_INDEX",
                             expiry_date: str = "") -> str:
        """Get the put-call-parity forward price for an expiry. Read-only.

        The synthetic future is the price the options market implies for the underlying,
        and it is what the ATM strike is picked from. A wide gap against the spot price
        is worth mentioning.

        Args:
            underlying (str): Underlying symbol such as NIFTY.
            exchange (str): Exchange of the underlying, NSE_INDEX or BSE_INDEX. Defaults
                to NSE_INDEX.
            expiry_date (str): Expiry as DDMMMYY. Leave empty for the nearest expiry.

        Returns:
            str: JSON with the synthetic future price, the spot price and the ATM strike.
        """
        und = C.normalize_symbol(underlying)
        ex = C.normalize_exchange(exchange) or "NSE_INDEX"
        expiry = self._resolve_expiry(und, ex, expiry_date)
        res = self.client.call_enveloped("syntheticfuture", underlying=und,
                                         exchange=ex, expiry_date=expiry)
        if not res["ok"]:
            raise RetryAgentRun(
                f"Synthetic future failed for {und} {expiry} on {ex}: {res['error']}."
            )
        return to_json(res)

    # -- gated tools -------------------------------------------------------

    def place_options_order(self, underlying: str, offset: str, option_type: str,
                            action: str, quantity: int, exchange: str = "NSE_INDEX",
                            expiry_date: str = "", product: str = "MIS",
                            price_type: str = "MARKET", price: float = 0.0,
                            trigger_price: float = 0.0) -> str:
        """Place one option order by strike offset. Requires human confirmation.

        The strike is resolved from the live underlying price when the order runs, so
        the contract is confirmed here before anything is sent and the resolved symbol
        is reported back. Quantity must be a multiple of the lot size, which
        resolve_option_symbol reports.

        Args:
            underlying (str): Underlying symbol such as NIFTY or BANKNIFTY.
            offset (str): ATM, ITM1 to ITM50, or OTM1 to OTM50.
            option_type (str): CE for a call or PE for a put.
            action (str): BUY or SELL.
            quantity (int): Quantity in units, not lots. Must be a lot multiple.
            exchange (str): Exchange of the underlying: NSE_INDEX, BSE_INDEX, NFO or
                BFO. The order itself routes to NFO or BFO. Defaults to NSE_INDEX.
            expiry_date (str): Expiry as DDMMMYY. Leave empty for the nearest expiry.
            product (str): MIS for intraday or NRML for overnight. Defaults to MIS.
            price_type (str): MARKET, LIMIT, SL or SL-M. Defaults to MARKET.
            price (float): Limit price for LIMIT and SL.
            trigger_price (float): Trigger price for SL and SL-M.

        Returns:
            str: JSON with the resolved symbol, the order id and the analyzer mode, or
                the risk guard refusal.
        """
        und = C.normalize_symbol(underlying)
        ex = C.normalize_exchange(exchange) or "NSE_INDEX"
        off, opt = _validate_offset(offset, option_type)
        act = C.normalize_action(action)
        if act not in C.ACTIONS:
            raise RetryAgentRun(f"action must be BUY or SELL, got {action!r}.")
        prod = C.normalize_product(product) or "MIS"
        pt = C.normalize_price_type(price_type) or "MARKET"
        if pt not in C.PRICE_TYPES:
            raise RetryAgentRun(
                f"price_type must be one of {', '.join(C.PRICE_TYPES)}, got "
                f"{price_type!r}."
            )
        expiry = self._resolve_expiry(und, ex, expiry_date)
        routed = route_exchange(ex)

        resolved, lotsize = self._peek_symbol(und, ex, off, opt, expiry)
        args = {"underlying": und, "exchange": ex, "routed_exchange": routed,
                "offset": off, "option_type": opt, "action": act,
                "quantity": quantity, "expiry_date": expiry, "product": prod,
                "price_type": pt, "price": price, "trigger_price": trigger_price,
                "resolved_symbol": resolved, "lotsize": lotsize,
                "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("place_options_order", args, mode)

        # The risk check runs against the routed exchange, never the quote-only index.
        verdict = self.guard.check_order(
            symbol=resolved or f"{und}{expiry}{off}{opt}", exchange=routed,
            action=act, quantity=quantity, product=prod, price_type=pt, price=price,
            session_id=self.session_id, skip_market_checks=not resolved)
        if not verdict.allowed:
            return self._refused("place_options_order", args, verdict, mode)

        res = self.client.call_enveloped(
            "optionsorder", _order=True, strategy=self.strategy,
            underlying=und, exchange=ex, offset=off, option_type=opt, action=act,
            quantity=num(quantity), expiry_date=expiry or None,
            price_type=pt, product=prod,
            price=num(price), trigger_price=num(trigger_price))
        return self._completed("place_options_order", args, res, mode,
                               resolved_symbol=resolved, lotsize=lotsize)

    def place_options_multi_order(self, underlying: str, legs: list[dict],
                                  exchange: str = "NSE_INDEX",
                                  expiry_date: str = "") -> str:
        """Place a multi-leg option strategy. Requires human confirmation.

        Between 1 and 20 legs, all on one underlying. BUY legs execute before SELL legs
        for margin efficiency, and a failed leg does NOT roll back the ones already
        placed - partial execution leaves a real, unbalanced position. Report every leg
        result, not just the envelope status. Check the requirement with
        calculate_margin first; a hedged spread costs far less than the sum of its legs.

        Args:
            underlying (str): Underlying symbol such as NIFTY.
            legs (list[dict]): Between 1 and 20 legs. Each needs offset (ATM, ITMn or
                OTMn), option_type (CE or PE), action (BUY or SELL) and quantity, plus
                optional pricetype, product, price, trigger_price and a per-leg
                expiry_date for calendar spreads. Example:
                [{"offset": "ATM", "option_type": "CE", "action": "SELL", "quantity": 65},
                 {"offset": "OTM5", "option_type": "CE", "action": "BUY", "quantity": 65}]
            exchange (str): Exchange of the underlying, usually NSE_INDEX or BSE_INDEX.
                Defaults to NSE_INDEX.
            expiry_date (str): Common expiry as DDMMMYY. Leave empty for the nearest
                expiry. A leg may override it.

        Returns:
            str: JSON with one result per leg, each naming the resolved symbol and its
                order id, or the risk guard refusal.
        """
        und = C.normalize_symbol(underlying)
        ex = C.normalize_exchange(exchange) or "NSE_INDEX"
        expiry = self._resolve_expiry(und, ex, expiry_date)
        routed = route_exchange(ex)
        cleaned = self._clean_option_legs(legs, expiry)

        # Resolve each leg so the risk check, the audit row and the confirmation card
        # all name real contracts instead of offsets.
        risk_legs = []
        for leg in cleaned:
            resolved, lotsize = self._peek_symbol(
                und, ex, leg["offset"], leg["option_type"],
                leg.get("expiry_date") or expiry)
            leg["resolved_symbol"] = resolved
            leg["lotsize"] = lotsize
            risk_legs.append({
                "symbol": resolved or f"{und}{expiry}{leg['offset']}{leg['option_type']}",
                "exchange": routed,
                "action": leg["action"],
                "quantity": leg["quantity"],
                "product": leg.get("product", "MIS"),
                "pricetype": leg.get("pricetype", "MARKET"),
                "price": leg.get("price", 0),
            })

        args = {"underlying": und, "exchange": ex, "routed_exchange": routed,
                "expiry_date": expiry, "legs": cleaned, "leg_count": len(cleaned),
                "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("place_options_multi_order", args, mode)

        verdict = self.guard.check_basket(risk_legs, session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("place_options_multi_order", args, verdict, mode)

        # The SDK reads only the keys it knows about, but strip the annotations anyway.
        wire_legs = [{k: v for k, v in leg.items()
                      if k not in ("resolved_symbol", "lotsize")} for leg in cleaned]
        res = self.client.call_enveloped(
            "optionsmultiorder", _order=True,
            strategy=self.strategy,          # required here, unlike everywhere else
            underlying=und, exchange=ex, legs=wire_legs, expiry_date=expiry or None)
        return self._completed(
            "place_options_multi_order", args, res, mode,
            leg_count=len(cleaned),
            resolved_symbols=[leg.get("resolved_symbol") for leg in cleaned],
            note="a failed leg does not roll back the legs already placed")

    # -- internals ---------------------------------------------------------

    def _resolve_expiry(self, underlying: str, exchange: str, expiry_date: str) -> str:
        """Nearest expiry in DDMMMYY when the caller gave none."""
        given = to_ddmmmyy(expiry_date)
        if given:
            return given
        derivative_ex = route_exchange(exchange)
        res = self.client.call_enveloped("expiry", symbol=underlying,
                                         exchange=derivative_ex,
                                         instrumenttype="options")
        data = res.get("data") if res.get("ok") else None
        if not isinstance(data, list) or not data:
            raise RetryAgentRun(
                f"No expiry given and none could be looked up for {underlying} on "
                f"{derivative_ex}: {res.get('error') or 'empty list'}. Call "
                "get_expiry_dates and pass expiry_date as DDMMMYY, for example 25AUG26."
            )
        return to_ddmmmyy(str(data[0]))

    def _resolve_symbol(self, underlying: str, exchange: str, offset: str,
                        option_type: str, expiry: str) -> dict:
        """optionsymbol call. strategy is deprecated here and deliberately omitted."""
        return self.client.call_enveloped(
            "optionsymbol", underlying=underlying, exchange=exchange, offset=offset,
            option_type=option_type, expiry_date=expiry)

    def _peek_symbol(self, underlying: str, exchange: str, offset: str,
                     option_type: str, expiry: str) -> tuple[str, Any]:
        """Best-effort resolution. Returns ("", None) if the lookup fails.

        A failure here must not block the order: the server resolves the strike again
        anyway. It only means the risk check runs without a market price, which is
        recorded in the response.
        """
        try:
            res = self._resolve_symbol(underlying, exchange, offset, option_type, expiry)
        except Exception:  # noqa: BLE001
            log.warning("option symbol resolution failed", exc_info=True)
            return "", None
        if not res.get("ok"):
            log.warning("option symbol resolution failed: %s", res.get("error"))
            return "", None
        data = res.get("data") or {}
        return str(data.get("symbol") or ""), data.get("lotsize")

    def _clean_option_legs(self, legs: list[dict], default_expiry: str) -> list[dict]:
        """Validate legs before optionsmultiorder can raise KeyError or ValueError."""
        if not legs:
            raise RetryAgentRun(
                "place_options_multi_order needs at least one leg with offset, "
                "option_type, action and quantity."
            )
        if len(legs) > MAX_OPTION_LEGS:
            raise RetryAgentRun(
                f"at most {MAX_OPTION_LEGS} legs are accepted, got {len(legs)}."
            )
        cleaned: list[dict[str, Any]] = []
        for i, leg in enumerate(legs, start=1):
            if not isinstance(leg, dict):
                raise RetryAgentRun(f"leg {i} is not an object with named fields.")
            try:
                off, opt = _validate_offset(leg.get("offset", ""),
                                            leg.get("option_type", ""))
            except RetryAgentRun as exc:
                raise RetryAgentRun(f"leg {i}: {exc}") from None
            act = C.normalize_action(str(leg.get("action", "")))
            if act not in C.ACTIONS:
                raise RetryAgentRun(f"leg {i} action must be BUY or SELL.")
            try:
                qty = int(float(leg.get("quantity", 0)))
            except (TypeError, ValueError):
                raise RetryAgentRun(
                    f"leg {i} quantity must be a whole number of units, not lots."
                ) from None
            if qty <= 0:
                raise RetryAgentRun(f"leg {i} quantity must be positive.")

            entry: dict[str, Any] = {
                "offset": off, "option_type": opt, "action": act, "quantity": qty,
            }
            # pricetype with no underscore: this is a leg dict, not a top-level kwarg.
            raw_pt = leg.get("pricetype") or leg.get("price_type")
            if raw_pt:
                pt = C.normalize_price_type(str(raw_pt))
                if pt not in C.PRICE_TYPES:
                    raise RetryAgentRun(
                        f"leg {i} pricetype must be one of {', '.join(C.PRICE_TYPES)}."
                    )
                entry["pricetype"] = pt
            if leg.get("product"):
                entry["product"] = C.normalize_product(str(leg["product"]))
            if leg.get("price"):
                entry["price"] = float(leg["price"])
            if leg.get("trigger_price"):
                entry["trigger_price"] = float(leg["trigger_price"])
            if leg.get("disclosed_quantity"):
                entry["disclosed_quantity"] = int(leg["disclosed_quantity"])
            leg_expiry = to_ddmmmyy(str(leg.get("expiry_date", "")))
            if leg_expiry and leg_expiry != default_expiry:
                entry["expiry_date"] = leg_expiry
            cleaned.append(entry)
        return cleaned


def _compact_leg(leg: Any) -> dict | None:
    """Keep the fields that matter for a decision; drop the rest to fit the budget."""
    if not isinstance(leg, dict):
        return None
    return {k: leg.get(k) for k in _CHAIN_FIELDS if k in leg}


def _validate_offset(offset: str, option_type: str) -> tuple[str, str]:
    off = (offset or "").strip().upper()
    opt = (option_type or "").strip().upper()
    if not C.is_valid_offset(off):
        raise RetryAgentRun(
            f"offset must be ATM, ITM1 to ITM50, or OTM1 to OTM50, got {offset!r}. "
            "No space and no zero padding."
        )
    if opt not in C.OPTION_TYPES:
        raise RetryAgentRun(f"option_type must be CE or PE, got {option_type!r}.")
    return off, opt


__all__ = ["OptionsTools", "route_exchange", "to_ddmmmyy"]
