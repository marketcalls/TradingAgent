"""RiskGuard: deterministic order checks the model cannot talk its way past.

This is layer 4 of the four safety layers in the plan, and the only one that holds
against both a confused model and a careless click. It runs INSIDE every mutating tool,
AFTER human confirmation, BEFORE the broker is touched.

Everything here is plain Python and config. There is no prompt involved, so no amount of
clever phrasing in the chat can change the outcome.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..openalgo import constants as C
from ..openalgo.client import OpenAlgoClient, get_client

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> str:
        if self.allowed:
            return "risk check passed"
        return f"Order refused by the risk guard ({self.code}): {self.reason}"


ALLOW = Verdict(True)


class RiskGuard:
    def __init__(self, settings: Settings | None = None,
                 client: OpenAlgoClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client or get_client()
        self._lock = threading.Lock()
        # fingerprint -> monotonic timestamp of the last accepted identical order
        self._recent: dict[str, float] = {}
        self._session_counts: dict[str, int] = {}

    # -- individual checks -------------------------------------------------

    def _check_kill_switch(self) -> Verdict:
        if self.settings.kill_switch_engaged():
            return Verdict(False, code="kill_switch",
                           reason=f"the kill switch file {self.settings.kill_switch_file} "
                                  "exists; remove it to resume trading")
        return ALLOW

    def _check_trading_enabled(self) -> Verdict:
        if not self.settings.trading_enabled:
            return Verdict(False, code="trading_disabled",
                           reason="TRADING_ENABLED is false in the environment")
        return ALLOW

    def _check_analyzer(self) -> Verdict:
        if not self.settings.require_analyzer_mode:
            return ALLOW
        mode = self._client.analyzer_mode()
        if mode != "analyze":
            return Verdict(False, code="live_mode_blocked",
                           reason=f"REQUIRE_ANALYZER_MODE is true and OpenAlgo is in "
                                  f"{mode} mode; orders are refused until analyzer mode "
                                  "is on or the setting is changed",
                           details={"mode": mode})
        return ALLOW

    def _check_exchange(self, exchange: str) -> Verdict:
        ex = C.normalize_exchange(exchange)
        if not ex:
            return Verdict(False, code="missing_exchange", reason="exchange is required")
        if C.is_index_exchange(ex):
            return Verdict(False, code="index_not_tradable",
                           reason=f"{ex} is a quote-only feed; indices cannot be traded. "
                                  "Trade the future or option instead")
        if ex not in self.settings.allowed_exchanges:
            return Verdict(False, code="exchange_not_allowed",
                           reason=f"{ex} is not in ALLOWED_EXCHANGES "
                                  f"({', '.join(self.settings.allowed_exchanges)})")
        return ALLOW

    def _check_product(self, product: str) -> Verdict:
        p = C.normalize_product(product)
        if p and p not in self.settings.allowed_products:
            return Verdict(False, code="product_not_allowed",
                           reason=f"{p} is not in ALLOWED_PRODUCTS "
                                  f"({', '.join(self.settings.allowed_products)})")
        return ALLOW

    def _check_symbol(self, symbol: str) -> Verdict:
        s = C.normalize_symbol(symbol)
        if not s:
            return Verdict(False, code="missing_symbol", reason="symbol is required")
        if s in self.settings.symbol_denylist:
            return Verdict(False, code="symbol_denied",
                           reason=f"{s} is in SYMBOL_DENYLIST")
        return ALLOW

    def _check_quantity(self, quantity: Any) -> Verdict:
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            return Verdict(False, code="bad_quantity",
                           reason=f"quantity {quantity!r} is not a number")
        if qty <= 0:
            return Verdict(False, code="bad_quantity",
                           reason=f"quantity must be positive, got {qty}")
        if qty > self.settings.max_order_quantity:
            return Verdict(False, code="quantity_cap",
                           reason=f"quantity {qty:g} exceeds MAX_ORDER_QUANTITY "
                                  f"({self.settings.max_order_quantity})")
        return ALLOW

    def _check_notional_and_price(self, symbol: str, exchange: str, quantity: Any,
                                  price_type: str, price: Any) -> Verdict:
        """Notional cap, and the fat-finger check that catches most real losses."""
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            return ALLOW  # already reported by _check_quantity

        ltp = self._client.ltp(C.normalize_symbol(symbol), C.normalize_exchange(exchange))
        pt = C.normalize_price_type(price_type)

        limit_price = None
        if pt in ("LIMIT", "SL"):
            try:
                limit_price = float(price)
            except (TypeError, ValueError):
                limit_price = None
            if limit_price is not None and limit_price <= 0:
                return Verdict(False, code="bad_price",
                               reason=f"{pt} order needs a positive price, got {limit_price}")

        reference = limit_price if limit_price else ltp
        if reference:
            notional = qty * reference
            if notional > self.settings.max_order_value:
                return Verdict(False, code="notional_cap",
                               reason=f"order notional {notional:,.2f} exceeds "
                                      f"MAX_ORDER_VALUE ({self.settings.max_order_value:,.2f}) "
                                      f"at a reference price of {reference:,.2f}",
                               details={"notional": notional, "reference": reference})

        if limit_price and ltp and ltp > 0:
            deviation = abs(limit_price - ltp) / ltp * 100.0
            if deviation > self.settings.max_price_deviation_pct:
                return Verdict(False, code="price_deviation",
                               reason=f"limit price {limit_price:,.2f} is {deviation:.1f} "
                                      f"percent away from the last traded price {ltp:,.2f}, "
                                      f"beyond the {self.settings.max_price_deviation_pct:g} "
                                      "percent guard. Check for a fat-finger error",
                               details={"ltp": ltp, "deviation_pct": round(deviation, 2)})
        return ALLOW

    def _check_duplicate(self, fingerprint: str) -> Verdict:
        window = self.settings.duplicate_order_window_sec
        if window <= 0:
            return ALLOW
        now = time.monotonic()
        with self._lock:
            for key, stamped in list(self._recent.items()):
                if now - stamped > window:
                    self._recent.pop(key, None)
            last = self._recent.get(fingerprint)
            if last is not None and now - last <= window:
                return Verdict(False, code="duplicate",
                               reason=f"an identical order was accepted "
                                      f"{now - last:.1f}s ago; the guard suppresses repeats "
                                      f"within {window}s. Wait or change the order")
        return ALLOW

    def _check_session_cap(self, session_id: str | None) -> Verdict:
        if not session_id:
            return ALLOW
        with self._lock:
            used = self._session_counts.get(session_id, 0)
        if used >= self.settings.max_orders_per_session:
            return Verdict(False, code="session_cap",
                           reason=f"this session has already placed {used} orders, at the "
                                  f"MAX_ORDERS_PER_SESSION limit "
                                  f"({self.settings.max_orders_per_session})")
        return ALLOW

    # -- public API --------------------------------------------------------

    def check_order(self, *, symbol: str, exchange: str, action: str = "",
                    quantity: Any = 0, product: str = "", price_type: str = "MARKET",
                    price: Any = 0, session_id: str | None = None,
                    skip_market_checks: bool = False) -> Verdict:
        """Full gate for a single-leg order."""
        fingerprint = _fingerprint(symbol, exchange, action, quantity, product, price_type, price)

        for verdict in (
            self._check_kill_switch(),
            self._check_trading_enabled(),
            self._check_analyzer(),
            self._check_symbol(symbol),
            self._check_exchange(exchange),
            self._check_product(product),
            self._check_quantity(quantity),
            self._check_session_cap(session_id),
            self._check_duplicate(fingerprint),
        ):
            if not verdict.allowed:
                log.warning("risk refused: %s - %s", verdict.code, verdict.reason)
                return verdict

        if not skip_market_checks:
            verdict = self._check_notional_and_price(symbol, exchange, quantity,
                                                     price_type, price)
            if not verdict.allowed:
                log.warning("risk refused: %s - %s", verdict.code, verdict.reason)
                return verdict

        action_norm = C.normalize_action(action)
        if action_norm and action_norm not in C.ACTIONS:
            return Verdict(False, code="bad_action",
                           reason=f"action must be BUY or SELL, got {action!r}")

        self._remember(fingerprint, session_id)
        return ALLOW

    def check_basket(self, legs: list[dict], session_id: str | None = None) -> Verdict:
        """Every leg must pass, and the combined notional must fit the cap."""
        if not legs:
            return Verdict(False, code="empty_basket", reason="no legs supplied")
        if len(legs) > 50:
            return Verdict(False, code="basket_too_large",
                           reason=f"{len(legs)} legs exceeds the 50-leg guard")

        total = 0.0
        for i, leg in enumerate(legs, start=1):
            # pricetype is the spelling inside basket/margin/leg dicts, price_type at top level
            pt = leg.get("pricetype") or leg.get("price_type") or "MARKET"
            verdict = self.check_order(
                symbol=leg.get("symbol", ""), exchange=leg.get("exchange", ""),
                action=leg.get("action", ""), quantity=leg.get("quantity", 0),
                product=leg.get("product", ""), price_type=pt, price=leg.get("price", 0),
                session_id=session_id, skip_market_checks=True,
            )
            if not verdict.allowed:
                return Verdict(False, code=verdict.code,
                               reason=f"leg {i} ({leg.get('symbol')}): {verdict.reason}",
                               details=verdict.details)
            ltp = self._client.ltp(C.normalize_symbol(leg.get("symbol", "")),
                                   C.normalize_exchange(leg.get("exchange", "")))
            if ltp:
                try:
                    total += float(leg.get("quantity", 0)) * ltp
                except (TypeError, ValueError):
                    pass

        if total > self.settings.max_order_value:
            return Verdict(False, code="notional_cap",
                           reason=f"combined basket notional {total:,.2f} exceeds "
                                  f"MAX_ORDER_VALUE ({self.settings.max_order_value:,.2f})",
                           details={"notional": total})
        return ALLOW

    def check_destructive(self, operation: str, session_id: str | None = None) -> Verdict:
        """Gate for cancel_all_orders / close_all_positions / GTT cancel.

        No symbol or quantity to check, but the kill switch, the master switch and
        analyzer mode all still apply.
        """
        for verdict in (self._check_kill_switch(),
                        self._check_trading_enabled(),
                        self._check_analyzer()):
            if not verdict.allowed:
                log.warning("risk refused %s: %s", operation, verdict.code)
                return verdict
        return ALLOW

    def _remember(self, fingerprint: str, session_id: str | None) -> None:
        with self._lock:
            self._recent[fingerprint] = time.monotonic()
            if session_id:
                self._session_counts[session_id] = self._session_counts.get(session_id, 0) + 1

    def session_order_count(self, session_id: str) -> int:
        with self._lock:
            return self._session_counts.get(session_id, 0)

    def reset(self) -> None:
        """Test helper. Clears duplicate and session state."""
        with self._lock:
            self._recent.clear()
            self._session_counts.clear()


def _fingerprint(symbol: str, exchange: str, action: str, quantity: Any,
                 product: str, price_type: str, price: Any) -> str:
    return "|".join([
        C.normalize_symbol(symbol), C.normalize_exchange(exchange),
        C.normalize_action(action), str(quantity),
        C.normalize_product(product), C.normalize_price_type(price_type), str(price),
    ])


_guard: RiskGuard | None = None
_guard_lock = threading.Lock()


def get_risk_guard() -> RiskGuard:
    global _guard
    if _guard is None:
        with _guard_lock:
            if _guard is None:
                _guard = RiskGuard()
    return _guard
