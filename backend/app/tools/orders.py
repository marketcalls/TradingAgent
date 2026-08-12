"""Order execution toolkit. Every mutating tool here is confirmation-gated.

Runtime traps this module is written against, all verified against agno 2.8.7 and
openalgo 2.0.3:

  - requires_confirmation_tools only WARNS on a typo, while include_tools and
    exclude_tools raise. A misspelled entry therefore removes the safety gate in
    silence. backend/tests/test_tools_orders.py asserts fn.requires_confirmation is
    True on every gated tool; renaming a method here without updating both the list
    below and that test is how an unconfirmed live order happens.

  - price_type is the SDK spelling at the TOP level (placeorder, placesmartorder,
    splitorder, modifyorder), but pricetype is the spelling INSIDE basket, margin and
    leg dicts. A leg carrying price_type is forwarded verbatim, ignored by the server,
    and the order silently drops to the MARKET default. This is the single most
    expensive typo in this API.

  - The SDK returns errors as dicts and never raises, so a truthy response is not a
    success. Everything goes through call_enveloped, which collapses the four different
    response shapes OpenAlgo uses for writes.

  - RiskGuard runs INSIDE the tool body: after the human confirmation Agno enforces,
    before the broker is touched. A refusal returns the verdict as JSON and no order is
    sent. The model cannot argue with it because there is no prompt involved.

  - Two audit rows per mutating call - "attempt" before the broker, "result" after - so
    a crash mid-flight still leaves evidence. An audit failure is logged and swallowed:
    losing the log is better than losing the ability to square off.

  - orders.py str()-casts every kwarg and drops None values, so numeric extras are
    formatted here rather than handed over as floats (0.0 would reach the wire as
    "0.0").

  - The request kwarg is order_id; the response field is orderid. Both spellings appear
    below on purpose.

  - basketorder, splitorder and cancelallorder report success when only some children
    worked. The per-item results are surfaced rather than trusted in aggregate.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from agno.exceptions import RetryAgentRun
from agno.tools import Toolkit

from ..config import Settings, get_settings
from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.normalize import err, to_json
from ..safety.audit import AuditLog, get_audit_log
from ..safety.risk import RiskGuard, Verdict, get_risk_guard

log = logging.getLogger(__name__)

# A split order may not fan out past this many children (server limit).
MAX_SPLIT_CHILDREN = 100


def num(value: Any, default: str = "0") -> str:
    """Format a number for the wire.

    The order endpoints str()-cast every kwarg, so an unformatted 0.0 arrives as the
    string "0.0". Integers are emitted without a decimal point.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return str(int(f)) if f == int(f) else str(f)


class MutatingToolkit(Toolkit):
    """Shared plumbing for every confirmation-gated toolkit.

    GttTools and OptionsTools import this so all three order paths behave identically.
    The order of operations is fixed and must not be rearranged:

      1. Agno pauses the run for human confirmation (requires_confirmation_tools).
      2. The tool body writes an audit "attempt" row carrying the analyzer mode.
      3. RiskGuard runs. On refusal the tool returns here and the broker is untouched.
      4. The broker call runs.
      5. The tool body writes an audit "result" row.

    Instance attributes are assigned before super().__init__ because Toolkit inspects
    the bound methods passed in tools=[...].
    """

    def __init__(self, *, name: str, tools: list,
                 requires_confirmation_tools: list[str] | None = None,
                 client: OpenAlgoClient | None = None,
                 settings: Settings | None = None,
                 guard: RiskGuard | None = None,
                 audit: AuditLog | None = None,
                 session_id: str | None = None,
                 **kwargs: Any) -> None:
        self.client = client or get_client()
        self.settings = settings or get_settings()
        self.guard = guard or get_risk_guard()
        self.audit = audit or get_audit_log()
        self.session_id = session_id
        super().__init__(
            name=name,
            tools=tools,
            requires_confirmation_tools=list(requires_confirmation_tools or []),
            **kwargs,
        )

    # -- shared helpers ----------------------------------------------------

    @property
    def strategy(self) -> str:
        """Strategy name stamped on every order so it is attributable in OpenAlgo."""
        return self.settings.default_strategy_name

    def analyzer_mode(self) -> str:
        """Current mode, never raising. Analyzer mode is application-wide and another
        tab can flip it, so it is read per call rather than cached."""
        try:
            return self.client.analyzer_mode()
        except Exception:  # noqa: BLE001
            log.warning("analyzer mode lookup failed", exc_info=True)
            return "unknown"

    def _record(self, phase: str, tool: str, args: dict, **fields: Any) -> None:
        """Audit, and never let auditing take a trade down."""
        try:
            self.audit.record(phase, tool, args, session_id=self.session_id, **fields)
        except Exception:  # noqa: BLE001
            log.warning("audit %s failed for %s", phase, tool, exc_info=True)

    def _attempt(self, tool: str, args: dict, mode: str) -> None:
        self._record("attempt", tool, args, analyzer_mode=mode, risk_verdict="pending")

    def _refused(self, tool: str, args: dict, verdict: Verdict, mode: str) -> str:
        """Return the refusal to the model. No broker call has happened."""
        self._record("result", tool, args, analyzer_mode=mode, risk_verdict="refused",
                     risk_reason=verdict.reason, ok=False,
                     response={"refused": verdict.code})
        return to_json(err(verdict.as_message(), tool, kind=verdict.code, mode=mode,
                           order_placed=False,
                           risk={"code": verdict.code, "reason": verdict.reason,
                                 "details": verdict.details}))

    def _completed(self, tool: str, args: dict, res: dict, mode: str,
                   **extra: Any) -> str:
        self._record("result", tool, args, analyzer_mode=mode, risk_verdict="allowed",
                     ok=bool(res.get("ok")), response=res)
        out = {**res, **extra}
        out.setdefault("mode", mode)
        out["analyzer_mode_at_send"] = mode
        return to_json(out)


class OrderTools(MutatingToolkit):
    def __init__(self, client: OpenAlgoClient | None = None,
                 settings: Settings | None = None,
                 guard: RiskGuard | None = None,
                 audit: AuditLog | None = None,
                 session_id: str | None = None,
                 **kwargs: Any) -> None:
        super().__init__(
            name="order_tools",
            tools=[
                self.place_order,
                self.place_smart_order,
                self.place_basket_order,
                self.place_split_order,
                self.modify_order,
                self.cancel_order,
                self.cancel_all_orders,
                self.close_all_positions,
                self.get_order_status,
            ],
            # Eight mutating tools. get_order_status is read-only and stays open.
            requires_confirmation_tools=[
                "place_order",
                "place_smart_order",
                "place_basket_order",
                "place_split_order",
                "modify_order",
                "cancel_order",
                "cancel_all_orders",
                "close_all_positions",
            ],
            client=client, settings=settings, guard=guard, audit=audit,
            session_id=session_id, **kwargs,
        )

    # -- validation --------------------------------------------------------

    def _validate_leg(self, action: str, price_type: str, price: Any,
                      trigger_price: Any) -> tuple[str, str]:
        """Normalize and reject anything the model can fix itself."""
        act = C.normalize_action(action)
        if act not in C.ACTIONS:
            raise RetryAgentRun(
                f"action must be BUY or SELL, got {action!r}."
            )
        pt = C.normalize_price_type(price_type)
        if pt not in C.PRICE_TYPES:
            raise RetryAgentRun(
                f"price_type must be one of {', '.join(C.PRICE_TYPES)}, got "
                f"{price_type!r}. Note the hyphen in SL-M."
            )
        if pt in ("LIMIT", "SL") and not _positive(price):
            raise RetryAgentRun(
                f"a {pt} order needs a positive price. Call get_quote first and pass the "
                "price you actually want."
            )
        if pt in ("SL", "SL-M") and not _positive(trigger_price):
            raise RetryAgentRun(f"a {pt} order needs a positive trigger_price.")
        return act, pt

    # -- tools -------------------------------------------------------------

    def place_order(self, symbol: str, action: str, exchange: str, quantity: int,
                    product: str = "MIS", price_type: str = "MARKET",
                    price: float = 0.0, trigger_price: float = 0.0,
                    disclosed_quantity: int = 0) -> str:
        """Place a single order. Requires human confirmation before it runs.

        State the symbol, exchange, action, quantity, product, price type and the
        current last traded price to the user before calling this. Indices such as
        NIFTY on NSE_INDEX are quote-only and can never be ordered; trade the future or
        option instead.

        Args:
            symbol (str): OpenAlgo symbol, for example SBIN, NIFTY25AUG26FUT or
                NIFTY18AUG2624450CE.
            action (str): BUY or SELL.
            exchange (str): Tradable exchange such as NSE, BSE, NFO, BFO, MCX or CDS.
            quantity (int): Number of shares or contracts. Must be a lot multiple for
                derivatives.
            product (str): MIS for intraday, CNC for equity delivery, NRML for F&O
                overnight. Defaults to MIS.
            price_type (str): MARKET, LIMIT, SL or SL-M. Defaults to MARKET.
            price (float): Limit price. Required for LIMIT and SL.
            trigger_price (float): Trigger price. Required for SL and SL-M.
            disclosed_quantity (int): Quantity shown on the exchange book. Defaults to 0.

        Returns:
            str: JSON with the broker order id and the analyzer mode the order was sent
                under, or the risk guard refusal if the order was blocked.
        """
        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        prod = C.normalize_product(product) or "MIS"
        act, pt = self._validate_leg(action, price_type, price, trigger_price)

        args = {"symbol": sym, "action": act, "exchange": ex, "quantity": quantity,
                "product": prod, "price_type": pt, "price": price,
                "trigger_price": trigger_price,
                "disclosed_quantity": disclosed_quantity,
                "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("place_order", args, mode)

        verdict = self.guard.check_order(
            symbol=sym, exchange=ex, action=act, quantity=quantity, product=prod,
            price_type=pt, price=price, session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("place_order", args, verdict, mode)

        res = self.client.call_enveloped(
            "placeorder", _order=True, strategy=self.strategy,
            symbol=sym, action=act, exchange=ex,
            price_type=pt,               # top level: price_type, not pricetype
            product=prod, quantity=num(quantity),
            price=num(price), trigger_price=num(trigger_price),
            disclosed_quantity=num(disclosed_quantity))
        return self._completed("place_order", args, res, mode)

    def place_smart_order(self, symbol: str, action: str, exchange: str, quantity: int,
                          position_size: int, product: str = "MIS",
                          price_type: str = "MARKET", price: float = 0.0,
                          trigger_price: float = 0.0) -> str:
        """Reconcile a position to a target size. Requires human confirmation.

        This tool can trade DOUBLE the quantity you pass. position_size is the absolute
        target position, not a delta: BUY 100 with position_size 100 against an existing
        short of 100 sends a BUY for 200 because it crosses through flat. Read the
        current position with get_positionbook first and tell the user the computed
        delta, not the quantity field. If the position already equals the target,
        OpenAlgo places nothing and answers "No action needed".

        Args:
            symbol (str): OpenAlgo symbol.
            action (str): BUY or SELL.
            exchange (str): Tradable exchange code.
            quantity (int): Order quantity used when a new position must be opened.
            position_size (int): Absolute target position. Positive is long, negative is
                short, 0 means flat.
            product (str): MIS, CNC or NRML. Defaults to MIS.
            price_type (str): MARKET, LIMIT, SL or SL-M. Defaults to MARKET.
            price (float): Limit price for LIMIT and SL.
            trigger_price (float): Trigger price for SL and SL-M.

        Returns:
            str: JSON with the broker order id, or the risk guard refusal.
        """
        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        prod = C.normalize_product(product) or "MIS"
        act, pt = self._validate_leg(action, price_type, price, trigger_price)
        try:
            target = int(position_size)
        except (TypeError, ValueError):
            raise RetryAgentRun(
                f"position_size must be a whole number, got {position_size!r}. It is the "
                "absolute target position: positive long, negative short, 0 flat."
            ) from None

        # The worst case is a cross through flat, which trades quantity + |target|.
        worst_case = max(abs(int(quantity or 0)), abs(int(quantity or 0)) + abs(target))
        args = {"symbol": sym, "action": act, "exchange": ex, "quantity": quantity,
                "position_size": target, "product": prod, "price_type": pt,
                "price": price, "trigger_price": trigger_price,
                "worst_case_quantity": worst_case, "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("place_smart_order", args, mode)

        # Size the risk check on the worst case, not on the quantity field.
        verdict = self.guard.check_order(
            symbol=sym, exchange=ex, action=act, quantity=worst_case, product=prod,
            price_type=pt, price=price, session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("place_smart_order", args, verdict, mode)

        res = self.client.call_enveloped(
            "placesmartorder", _order=True, strategy=self.strategy,
            symbol=sym, action=act, exchange=ex, price_type=pt, product=prod,
            quantity=num(quantity), position_size=num(target),
            price=num(price), trigger_price=num(trigger_price))
        return self._completed("place_smart_order", args, res, mode,
                               worst_case_quantity=worst_case)

    def place_basket_order(self, orders: list[dict]) -> str:
        """Place several independent orders in one call. Requires human confirmation.

        Partial success is normal: the envelope reports success when at least one leg
        worked, so read the per-leg results before telling the user the basket is done.
        BUY legs are processed before SELL legs.

        Args:
            orders (list[dict]): Legs to place. Each needs symbol, exchange, action
                (BUY or SELL) and quantity, plus optional pricetype (MARKET, LIMIT, SL,
                SL-M), product (MIS, CNC, NRML), price and trigger_price. Example:
                [{"symbol": "SBIN", "exchange": "NSE", "action": "BUY", "quantity": 1,
                  "pricetype": "MARKET", "product": "MIS"}]

        Returns:
            str: JSON with one result per leg, each carrying its own order id and
                status, or the risk guard refusal.
        """
        legs = self._clean_legs(orders)
        args = {"orders": legs, "legs": len(legs), "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("place_basket_order", args, mode)

        verdict = self.guard.check_basket(legs, session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("place_basket_order", args, verdict, mode)

        res = self.client.call_enveloped("basketorder", _order=True,
                                         strategy=self.strategy, orders=legs)
        return self._completed("place_basket_order", args, res, mode,
                               leg_count=len(legs),
                               note="check every leg result; partial fills are normal")

    def place_split_order(self, symbol: str, action: str, exchange: str, quantity: int,
                          splitsize: int, product: str = "MIS",
                          price_type: str = "MARKET", price: float = 0.0,
                          trigger_price: float = 0.0) -> str:
        """Break one large order into child orders. Requires human confirmation.

        Use this when a quantity exceeds the exchange freeze limit. The last child
        carries the remainder. The broker sees many orders, so the risk guard prices the
        whole split, not one slice.

        Args:
            symbol (str): OpenAlgo symbol.
            action (str): BUY or SELL.
            exchange (str): Tradable exchange code.
            quantity (int): Total quantity across every child order.
            splitsize (int): Quantity per child order. Must be positive, and
                quantity/splitsize must not exceed 100 children.
            product (str): MIS, CNC or NRML. Defaults to MIS.
            price_type (str): MARKET, LIMIT, SL or SL-M. Defaults to MARKET.
            price (float): Limit price for LIMIT and SL.
            trigger_price (float): Trigger price for SL and SL-M.

        Returns:
            str: JSON with the child count and one result per child, or the risk guard
                refusal.
        """
        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        prod = C.normalize_product(product) or "MIS"
        act, pt = self._validate_leg(action, price_type, price, trigger_price)

        try:
            total, chunk = int(quantity), int(splitsize)
        except (TypeError, ValueError):
            raise RetryAgentRun(
                "quantity and splitsize must both be whole numbers."
            ) from None
        if chunk <= 0:
            raise RetryAgentRun("splitsize must be a positive whole number.")
        children = math.ceil(total / chunk) if total > 0 else 0
        if children > MAX_SPLIT_CHILDREN:
            raise RetryAgentRun(
                f"quantity {total} at splitsize {chunk} would create {children} child "
                f"orders, over the {MAX_SPLIT_CHILDREN} limit. Raise splitsize."
            )

        args = {"symbol": sym, "action": act, "exchange": ex, "quantity": total,
                "splitsize": chunk, "children": children, "product": prod,
                "price_type": pt, "price": price, "trigger_price": trigger_price,
                "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("place_split_order", args, mode)

        # check_basket rather than check_order: a split is a multi-order operation, and
        # the cap that matters is the notional of the whole thing, not of one slice.
        verdict = self.guard.check_basket(
            [{"symbol": sym, "exchange": ex, "action": act, "quantity": total,
              "product": prod, "pricetype": pt, "price": price}],
            session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("place_split_order", args, verdict, mode)

        res = self.client.call_enveloped(
            "splitorder", _order=True, strategy=self.strategy,
            symbol=sym, action=act, exchange=ex, quantity=num(total),
            splitsize=num(chunk), price_type=pt, product=prod,
            price=num(price), trigger_price=num(trigger_price))
        return self._completed("place_split_order", args, res, mode,
                               expected_children=children)

    def modify_order(self, order_id: str, symbol: str, action: str, exchange: str,
                     quantity: int, price: float, product: str,
                     price_type: str = "LIMIT", trigger_price: float = 0.0,
                     disclosed_quantity: int = 0) -> str:
        """Replace an open order. Requires human confirmation.

        This is a full replacement, not a patch: every field is mandatory and anything
        omitted is lost. Read the current order with get_order_status or get_orderbook
        first and resend the complete specification with only the intended field
        changed. A modified LIMIT can fill instantly. Symbol, action and product cannot
        actually be changed - the broker rejects that - so send the original values.

        Args:
            order_id (str): The broker order id to modify.
            symbol (str): OpenAlgo symbol of the existing order.
            action (str): BUY or SELL, matching the existing order.
            exchange (str): Exchange of the existing order.
            quantity (int): New total quantity.
            price (float): New limit price. Send 0 for MARKET.
            product (str): Product of the existing order: MIS, CNC or NRML.
            price_type (str): MARKET, LIMIT, SL or SL-M. Defaults to LIMIT.
            trigger_price (float): New trigger price, or 0 when unused.
            disclosed_quantity (int): New disclosed quantity, or 0.

        Returns:
            str: JSON with the order id, or the risk guard refusal.
        """
        oid = str(order_id or "").strip()
        if not oid:
            raise RetryAgentRun("modify_order needs the order_id to modify.")
        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        prod = C.normalize_product(product) or "MIS"
        act, pt = self._validate_leg(action, price_type, price, trigger_price)

        args = {"order_id": oid, "symbol": sym, "action": act, "exchange": ex,
                "quantity": quantity, "price": price, "product": prod,
                "price_type": pt, "trigger_price": trigger_price,
                "disclosed_quantity": disclosed_quantity, "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("modify_order", args, mode)

        verdict = self.guard.check_order(
            symbol=sym, exchange=ex, action=act, quantity=quantity, product=prod,
            price_type=pt, price=price, session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("modify_order", args, verdict, mode)

        res = self.client.call_enveloped(
            "modifyorder", _order=True, order_id=oid, strategy=self.strategy,
            symbol=sym, action=act, exchange=ex, price_type=pt, product=prod,
            quantity=num(quantity), price=num(price),
            trigger_price=num(trigger_price),
            disclosed_quantity=num(disclosed_quantity))
        return self._completed("modify_order", args, res, mode)

    def cancel_order(self, order_id: str) -> str:
        """Cancel one open or trigger-pending order. Requires human confirmation.

        Cancelling a stop-loss removes protection from an open position. Say so before
        calling this.

        Args:
            order_id (str): The broker order id to cancel.

        Returns:
            str: JSON acknowledgement with the order id, or the risk guard refusal.
        """
        oid = str(order_id or "").strip()
        if not oid:
            raise RetryAgentRun(
                "cancel_order needs an order_id. Call get_orderbook to find it."
            )
        args = {"order_id": oid, "strategy": self.strategy}
        mode = self.analyzer_mode()
        self._attempt("cancel_order", args, mode)

        # No symbol or quantity is involved, so the destructive gate is the right one:
        # kill switch, master switch and analyzer mode still apply.
        verdict = self.guard.check_destructive("cancel_order", session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("cancel_order", args, verdict, mode)

        res = self.client.call_enveloped("cancelorder", _order=True, order_id=oid,
                                         strategy=self.strategy)
        return self._completed("cancel_order", args, res, mode)

    def cancel_all_orders(self) -> str:
        """Cancel every open and trigger-pending order. Requires human confirmation.

        Bulk and irreversible: this kills resting stop-losses and targets as well as
        entry orders. It reports success even when individual cancels failed, so the
        failed list is surfaced in the reply and must be read.

        Returns:
            str: JSON with the cancelled and failed order ids, or the risk guard
                refusal.
        """
        args = {"strategy": self.strategy, "scope": "all open and pending orders"}
        mode = self.analyzer_mode()
        self._attempt("cancel_all_orders", args, mode)

        verdict = self.guard.check_destructive("cancel_all_orders",
                                               session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("cancel_all_orders", args, verdict, mode)

        res = self.client.call_enveloped("cancelallorder", _order=True,
                                         strategy=self.strategy)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        return self._completed(
            "cancel_all_orders", args, res, mode,
            cancelled=len(data.get("canceled_orders", []) or []),
            failed=len(data.get("failed_cancellations", []) or []))

    def close_all_positions(self) -> str:
        """Square off every open position at market. Requires human confirmation.

        The most destructive call in the API. It walks the position book across every
        exchange and sends MARKET orders in the opposite direction, with no broker-side
        prompt and no way to undo a fill. Never call this to "clean up" - only when the
        user has asked for it in plain terms.

        Returns:
            str: JSON acknowledgement, or the risk guard refusal.
        """
        args = {"strategy": self.strategy, "scope": "all open positions"}
        mode = self.analyzer_mode()
        self._attempt("close_all_positions", args, mode)

        verdict = self.guard.check_destructive("close_all_positions",
                                               session_id=self.session_id)
        if not verdict.allowed:
            return self._refused("close_all_positions", args, verdict, mode)

        res = self.client.call_enveloped("closeposition", _order=True,
                                         strategy=self.strategy)
        return self._completed("close_all_positions", args, res, mode)

    def get_order_status(self, order_id: str) -> str:
        """Look up one order by id. Read-only, so it runs without confirmation.

        Args:
            order_id (str): The broker order id, as returned by place_order or listed in
                get_orderbook.

        Returns:
            str: JSON with order_status, filled quantity, average price, price type and
                product.
        """
        oid = str(order_id or "").strip()
        if not oid:
            raise RetryAgentRun(
                "get_order_status needs an order_id. Call get_orderbook to list them."
            )
        res = self.client.call_enveloped("orderstatus", order_id=oid,
                                         strategy=self.strategy)
        if not res["ok"]:
            raise RetryAgentRun(
                f"Order status lookup failed for {oid}: {res['error']}. Check the id "
                "against get_orderbook."
            )
        return to_json(res)

    # -- internals ---------------------------------------------------------

    def _clean_legs(self, orders: list[dict]) -> list[dict]:
        """Normalize basket legs into the exact shape basketorder wants.

        The key here is pricetype with no underscore. price_type inside a leg is
        forwarded verbatim and ignored, which quietly turns a LIMIT into a MARKET.
        """
        if not orders:
            raise RetryAgentRun(
                "place_basket_order needs at least one leg, each with symbol, exchange, "
                "action and quantity."
            )
        cleaned: list[dict[str, Any]] = []
        for i, leg in enumerate(orders, start=1):
            if not isinstance(leg, dict):
                raise RetryAgentRun(f"leg {i} is not an object with named fields.")
            sym = C.normalize_symbol(str(leg.get("symbol", "")))
            ex = C.normalize_exchange(str(leg.get("exchange", "")))
            act = C.normalize_action(str(leg.get("action", "")))
            if not sym or not ex:
                raise RetryAgentRun(f"leg {i} needs both symbol and exchange.")
            if act not in C.ACTIONS:
                raise RetryAgentRun(f"leg {i} action must be BUY or SELL, got {act!r}.")
            raw_pt = leg.get("pricetype") or leg.get("price_type") or "MARKET"
            pt = C.normalize_price_type(str(raw_pt))
            if pt not in C.PRICE_TYPES:
                raise RetryAgentRun(
                    f"leg {i} pricetype must be one of {', '.join(C.PRICE_TYPES)}."
                )
            entry = {
                "symbol": sym,
                "exchange": ex,
                "action": act,
                "quantity": num(leg.get("quantity", 0)),
                "pricetype": pt,          # no underscore inside a leg dict
                "product": C.normalize_product(str(leg.get("product", "MIS"))) or "MIS",
                "price": num(leg.get("price", 0)),
                "trigger_price": num(leg.get("trigger_price", 0)),
            }
            if leg.get("disclosed_quantity"):
                entry["disclosed_quantity"] = num(leg["disclosed_quantity"])
            cleaned.append(entry)
        return cleaned


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


__all__ = ["OrderTools", "MutatingToolkit", "num"]
