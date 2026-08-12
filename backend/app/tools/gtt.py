"""GTT toolkit: broker-side triggers that fire a real order later.

Runtime traps this module is written against:

  - The SDK does not wrap GTT at all. All four endpoints - placegttorder,
    modifygttorder, cancelgttorder, gttorderbook - go through client.raw_post, which
    reaches them via the SDK's own transport. The endpoint name must NOT carry a
    leading slash: BaseAPI.base_url already ends in one, so "/placegttorder" builds
    "/api/v1//placegttorder", the server answers 308, and the SDK's httpx.Client does
    not follow redirects. The failure surfaces as "HTTP 308: <!doctype html>" rather
    than anything obviously wrong.

  - GTT can return HTTP 501 while analyzer mode is on ("Sandbox GTT support not yet
    implemented"), and 501 again when the broker ships no gtt_api module. Some OpenAlgo
    builds do sandbox GTT and some do not, so this module detects the 501 and answers
    with a plain-language explanation instead of leaking the raw status line. When it
    happens there is no rehearsal path: the first GTT the user places is necessarily
    live.

  - GTT is narrower than a normal order: product is CNC or NRML only (MIS is rejected
    because it is intraday-only), price type is LIMIT or MARKET only (no SL or SL-M),
    and the exchange must be a real venue. All of that is checked here so the model
    gets a fixable message instead of a broker rejection.

  - SINGLE needs exactly one of triggerprice_sl and triggerprice_tg. OCO needs all four
    of triggerprice_sl, stoploss, triggerprice_tg and target, with
    triggerprice_sl < triggerprice_tg. last_price is fetched server-side - never send it.

  - Modify is a full replacement, not a patch. Omitted fields are lost, and SINGLE
    cannot become OCO.

  - The payload spelling is pricetype, with no underscore, because these are raw wire
    dicts rather than SDK keyword arguments. The tools expose price_type for
    consistency with place_order and translate at the boundary.

  - Agno gates a whole function, not one argument, so manage_gtt_order is confirmation
    gated even for the read-only list operation. The alternative would be an ungated
    path to cancel, which is not a trade worth making. RiskGuard only runs for the
    modify and cancel branches.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.exceptions import RetryAgentRun

from ..config import Settings
from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient
from ..openalgo.normalize import err, to_json
from ..safety.audit import AuditLog
from ..safety.risk import RiskGuard
from .orders import MutatingToolkit, num

log = logging.getLogger(__name__)

GTT_OPERATIONS: tuple[str, ...] = ("modify", "cancel", "list")

_UNAVAILABLE_HINT = (
    "GTT cannot be rehearsed in analyze mode. OpenAlgo answers the GTT endpoints with "
    "HTTP 501 while the analyzer is on, and also when the connected broker ships no GTT "
    "module, so there is no simulated path for this call. Either switch to live mode "
    "deliberately - the first GTT order placed is a real broker-side trigger - or use a "
    "regular SL or SL-M order, which does work in analyze mode."
)


def _is_gtt_unavailable(res: dict) -> bool:
    """Recognize the 501 that analyzer mode and unsupported brokers both produce."""
    if res.get("ok"):
        return False
    message = str(res.get("error") or "").lower()
    return (
        "http 501" in message
        or "sandbox gtt" in message
        or "not yet implemented" in message
        or "not supported for broker" in message
    )


class GttTools(MutatingToolkit):
    def __init__(self, client: OpenAlgoClient | None = None,
                 settings: Settings | None = None,
                 guard: RiskGuard | None = None,
                 audit: AuditLog | None = None,
                 session_id: str | None = None,
                 **kwargs: Any) -> None:
        super().__init__(
            name="gtt_tools",
            tools=[
                self.place_gtt_order,
                self.manage_gtt_order,
            ],
            requires_confirmation_tools=[
                "place_gtt_order",
                "manage_gtt_order",
            ],
            client=client, settings=settings, guard=guard, audit=audit,
            session_id=session_id, **kwargs,
        )

    # -- tools -------------------------------------------------------------

    def place_gtt_order(self, symbol: str, exchange: str, action: str, quantity: int,
                        product: str = "CNC", trigger_type: str = "SINGLE",
                        price_type: str = "LIMIT", price: float = 0.0,
                        triggerprice_sl: float = 0.0, triggerprice_tg: float = 0.0,
                        stoploss: float = 0.0, target: float = 0.0) -> str:
        """Create a Good Till Triggered order. Requires human confirmation.

        A GTT rests at the broker until its trigger price is hit, then fires a real
        order - possibly weeks later, without the agent involved. Say that plainly to
        the user before calling this.

        SINGLE fires one order: set exactly one of triggerprice_sl (trigger below the
        last price, the usual stop-loss) or triggerprice_tg (trigger above it, the usual
        target), and leave the other at 0. OCO rests both legs and cancels one when the
        other fires: set all four of triggerprice_sl, stoploss, triggerprice_tg and
        target, keeping triggerprice_sl below triggerprice_tg.

        Args:
            symbol (str): OpenAlgo symbol, for example SBIN.
            exchange (str): NSE, BSE, NFO, BFO, CDS, BCD or MCX.
            action (str): BUY or SELL.
            quantity (int): Quantity for the order that will fire.
            product (str): CNC for delivery or NRML for F&O overnight. MIS is rejected
                by GTT because it is intraday-only. Defaults to CNC.
            trigger_type (str): SINGLE for one trigger, OCO for a paired
                stop-loss and target. Defaults to SINGLE.
            price_type (str): LIMIT or MARKET only. Defaults to LIMIT.
            price (float): Limit price of the child order for SINGLE. Send 0 for MARKET.
                Ignored for OCO, where each leg carries its own price.
            triggerprice_sl (float): Trigger price below the last traded price.
            triggerprice_tg (float): Trigger price above the last traded price.
            stoploss (float): Limit price of the stop-loss leg. OCO only.
            target (float): Limit price of the target leg. OCO only.

        Returns:
            str: JSON with the trigger id, the risk guard refusal, or an explanation
                that GTT is unavailable in this mode.
        """
        payload = self._build_gtt(
            symbol=symbol, exchange=exchange, action=action, quantity=quantity,
            product=product, trigger_type=trigger_type, price_type=price_type,
            price=price, triggerprice_sl=triggerprice_sl,
            triggerprice_tg=triggerprice_tg, stoploss=stoploss, target=target)

        args = dict(payload)
        mode = self.analyzer_mode()
        self._attempt("place_gtt_order", args, mode)

        verdict = self._check_gtt(payload)
        if not verdict.allowed:
            return self._refused("place_gtt_order", args, verdict, mode)

        res = self.client.raw_post("placegttorder", payload, _order=True)
        if _is_gtt_unavailable(res):
            return self._unavailable("place_gtt_order", args, res, mode)
        return self._completed("place_gtt_order", args, res, mode)

    def manage_gtt_order(self, operation: str, trigger_id: str = "", symbol: str = "",
                         exchange: str = "", action: str = "", quantity: int = 0,
                         product: str = "CNC", trigger_type: str = "SINGLE",
                         price_type: str = "LIMIT", price: float = 0.0,
                         triggerprice_sl: float = 0.0, triggerprice_tg: float = 0.0,
                         stoploss: float = 0.0, target: float = 0.0) -> str:
        """List, modify or cancel resting GTT orders. Requires human confirmation.

        Only active triggers are ever listed or modifiable. Modify is a FULL
        REPLACEMENT: resend the complete specification, because anything left out is
        lost, and a SINGLE trigger cannot be converted into an OCO one. Cancelling an
        OCO removes both legs at once, which usually means removing a stop-loss from a
        live position.

        Args:
            operation (str): "list" to show active triggers, "modify" to replace one, or
                "cancel" to remove one.
            trigger_id (str): The trigger id, required for modify and cancel. Call this
                tool with operation="list" first to find it.
            symbol (str): OpenAlgo symbol. Required for modify.
            exchange (str): Exchange code. Required for modify.
            action (str): BUY or SELL. Required for modify.
            quantity (int): Quantity. Required for modify.
            product (str): CNC or NRML. Defaults to CNC.
            trigger_type (str): SINGLE or OCO, matching the existing trigger.
            price_type (str): LIMIT or MARKET only. Defaults to LIMIT.
            price (float): Limit price of the child order for SINGLE.
            triggerprice_sl (float): Trigger price below the last traded price.
            triggerprice_tg (float): Trigger price above the last traded price.
            stoploss (float): Limit price of the stop-loss leg. OCO only.
            target (float): Limit price of the target leg. OCO only.

        Returns:
            str: JSON with the active trigger list, the resulting trigger id, the risk
                guard refusal, or an explanation that GTT is unavailable in this mode.
        """
        op = (operation or "").strip().lower()
        if op not in GTT_OPERATIONS:
            raise RetryAgentRun(
                f"operation must be one of {', '.join(GTT_OPERATIONS)}, got "
                f"{operation!r}."
            )

        if op == "list":
            # Read-only branch. The tool is gated as a whole because agno gates a
            # function, not an argument, but there is nothing here to risk-check.
            res = self.client.raw_post("gttorderbook")
            if _is_gtt_unavailable(res):
                return to_json(err(_UNAVAILABLE_HINT, "gttorderbook",
                                   kind="gtt_unavailable", operation="list"))
            data = res.get("data")
            return to_json({**res, "operation": "list",
                            "active_triggers": len(data) if isinstance(data, list) else None})

        tid = str(trigger_id or "").strip()
        if not tid:
            raise RetryAgentRun(
                f"operation={op!r} needs a trigger_id. Call manage_gtt_order with "
                'operation="list" to see the active triggers and their ids.'
            )

        if op == "cancel":
            args = {"operation": "cancel", "trigger_id": tid,
                    "strategy": self.strategy}
            mode = self.analyzer_mode()
            self._attempt("manage_gtt_order", args, mode)

            verdict = self.guard.check_destructive("cancel_gtt_order",
                                                   session_id=self.session_id)
            if not verdict.allowed:
                return self._refused("manage_gtt_order", args, verdict, mode)

            res = self.client.raw_post(
                "cancelgttorder",
                {"strategy": self.strategy, "trigger_id": tid}, _order=True)
            if _is_gtt_unavailable(res):
                return self._unavailable("manage_gtt_order", args, res, mode)
            return self._completed("manage_gtt_order", args, res, mode,
                                   operation="cancel")

        payload = self._build_gtt(
            symbol=symbol, exchange=exchange, action=action, quantity=quantity,
            product=product, trigger_type=trigger_type, price_type=price_type,
            price=price, triggerprice_sl=triggerprice_sl,
            triggerprice_tg=triggerprice_tg, stoploss=stoploss, target=target)
        payload["trigger_id"] = tid

        args = {**payload, "operation": "modify"}
        mode = self.analyzer_mode()
        self._attempt("manage_gtt_order", args, mode)

        verdict = self._check_gtt(payload)
        if not verdict.allowed:
            return self._refused("manage_gtt_order", args, verdict, mode)

        res = self.client.raw_post("modifygttorder", payload, _order=True)
        if _is_gtt_unavailable(res):
            return self._unavailable("manage_gtt_order", args, res, mode)
        return self._completed("manage_gtt_order", args, res, mode, operation="modify")

    # -- internals ---------------------------------------------------------

    def _build_gtt(self, *, symbol: str, exchange: str, action: str, quantity: Any,
                   product: str, trigger_type: str, price_type: str, price: Any,
                   triggerprice_sl: Any, triggerprice_tg: Any,
                   stoploss: Any, target: Any) -> dict[str, Any]:
        """Validate the GTT rules client-side and build the wire payload.

        Everything the model can fix is raised as RetryAgentRun with the rule spelled
        out, rather than sent off to collect a broker rejection.
        """
        sym = C.normalize_symbol(symbol)
        ex = C.normalize_exchange(exchange)
        act = C.normalize_action(action)
        prod = C.normalize_product(product) or "CNC"
        tt = (trigger_type or "SINGLE").strip().upper()
        pt = C.normalize_price_type(price_type) or "LIMIT"

        if not sym or not ex:
            raise RetryAgentRun("a GTT order needs both symbol and exchange.")
        if act not in C.ACTIONS:
            raise RetryAgentRun(f"action must be BUY or SELL, got {action!r}.")
        if tt not in C.GTT_TRIGGER_TYPES:
            raise RetryAgentRun(
                f"trigger_type must be SINGLE or OCO, got {trigger_type!r}."
            )
        if prod not in C.GTT_PRODUCTS:
            raise RetryAgentRun(
                f"GTT supports only {' or '.join(C.GTT_PRODUCTS)}; {prod} was requested. "
                "MIS is intraday-only and a GTT can rest for months."
            )
        if pt not in C.GTT_PRICE_TYPES:
            raise RetryAgentRun(
                f"GTT price_type must be LIMIT or MARKET, got {price_type!r}. SL and "
                "SL-M are not accepted - the trigger itself is the stop."
            )

        sl_trigger = _f(triggerprice_sl)
        tg_trigger = _f(triggerprice_tg)
        sl_price = _f(stoploss)
        tg_price = _f(target)

        if tt == "SINGLE":
            if (sl_trigger > 0) == (tg_trigger > 0):
                raise RetryAgentRun(
                    "a SINGLE GTT needs exactly one trigger price: triggerprice_sl for a "
                    "trigger below the last traded price, or triggerprice_tg for one "
                    "above it. Leave the other at 0."
                )
            if pt == "LIMIT" and _f(price) <= 0:
                raise RetryAgentRun(
                    "a LIMIT GTT needs a positive price for the order that will fire. "
                    "Use price_type=MARKET if the fill price does not matter."
                )
        else:
            if not (sl_trigger > 0 and tg_trigger > 0 and sl_price > 0 and tg_price > 0):
                raise RetryAgentRun(
                    "an OCO GTT needs all four of triggerprice_sl, stoploss, "
                    "triggerprice_tg and target to be positive."
                )
            if sl_trigger >= tg_trigger:
                raise RetryAgentRun(
                    f"an OCO GTT needs triggerprice_sl ({sl_trigger}) below "
                    f"triggerprice_tg ({tg_trigger})."
                )

        # Wire spelling is pricetype with no underscore; last_price is server-side.
        return {
            "strategy": self.strategy,
            "trigger_type": tt,
            "exchange": ex,
            "symbol": sym,
            "action": act,
            "product": prod,
            "quantity": num(quantity),
            "pricetype": pt,
            "price": num(price),
            "triggerprice_sl": num(sl_trigger),
            "triggerprice_tg": num(tg_trigger),
            "stoploss": num(sl_price),
            "target": num(tg_price),
        }

    def _check_gtt(self, payload: dict):
        """Run the single-leg gate on a GTT specification.

        price_type is deliberately reported to the guard as MARKET: a GTT exists to rest
        at a price away from the last trade, so the fat-finger deviation check would
        refuse every legitimate stop-loss. The notional cap still applies, priced at the
        last traded price.
        """
        return self.guard.check_order(
            symbol=payload["symbol"], exchange=payload["exchange"],
            action=payload["action"], quantity=payload["quantity"],
            product=payload["product"], price_type="MARKET", price=0,
            session_id=self.session_id)

    def _unavailable(self, tool: str, args: dict, res: dict, mode: str) -> str:
        """Answer a 501 in plain language rather than leaking the status line."""
        self._record("result", tool, args, analyzer_mode=mode, risk_verdict="allowed",
                     ok=False, response=res)
        log.warning("GTT unavailable in mode=%s: %s", mode, res.get("error"))
        return to_json(err(_UNAVAILABLE_HINT, res.get("source", tool),
                           kind="gtt_unavailable", mode=mode, order_placed=False,
                           server_message=res.get("error")))


def _f(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f  # NaN guard


__all__ = ["GttTools"]
