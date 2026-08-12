"""Shared OpenAlgo client.

Facts this module exists to encapsulate, all verified against openalgo 2.0.3:

  - The SDK is fully synchronous (httpx.Client plus OS threads for the websocket feed).
    Calling it from the FastAPI event loop stalls every other request for the length of
    a broker round trip, so acall() offloads to a worker thread.

  - Errors are RETURNED as dicts, never raised. A truthy response is not a success;
    always inspect status.

  - history() and instruments() return a DataFrame on success but a dict on error, so
    the type must be checked before touching .tail().

  - BaseAPI sets base_url = f"{host}/api/{version}/" - already slash-terminated - and
    _make_request does a bare string concat. An endpoint name with a leading slash
    produces "/api/v1//ping", which the server answers with a 308 that the SDK's
    httpx.Client does not follow (follow_redirects defaults to False). The call then
    returns {"status": "error", "message": "HTTP 308: <!doctype html>..."} instead of
    failing loudly. raw_post() strips leading slashes for exactly this reason.

  - The SDK default timeout is 120s, far too long for a chat UI.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from openalgo import api

from ..config import Settings, get_settings
from .normalize import envelope, err

log = logging.getLogger(__name__)

# Endpoints the SDK does not wrap, reachable only through raw_post.
UNWRAPPED_ENDPOINTS: tuple[str, ...] = (
    "placegttorder", "modifygttorder", "cancelgttorder", "gttorderbook",
    "multioptiongreeks", "ping", "pnl/symbols",
)


class RateLimiter:
    """Token bucket. OpenAlgo defaults are 50 req/s overall and 10 orders/s."""

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self._rate = float(rate_per_sec)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                time.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


class OpenAlgoClient:
    """One instance per process. httpx.Client pools connections and is thread-safe."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._api = api(
            api_key=self.settings.openalgo_api_key,
            host=self.settings.openalgo_host,
            version=self.settings.openalgo_api_version,
            timeout=self.settings.openalgo_timeout,
        )
        self._general = RateLimiter(rate_per_sec=40, burst=40)
        self._orders = RateLimiter(rate_per_sec=8, burst=8)

    @property
    def raw(self):
        """The underlying openalgo.api instance. Prefer call()/raw_post()."""
        return self._api

    def close(self) -> None:
        try:
            self._api.close()
        except Exception:  # noqa: BLE001
            log.debug("client close failed", exc_info=True)

    # -- core -------------------------------------------------------------

    def call(self, method: str, _order: bool = False, **kwargs: Any) -> Any:
        """Invoke an SDK method by name. Returns whatever the SDK returns."""
        fn = getattr(self._api, method, None)
        if fn is None:
            raise AttributeError(f"openalgo SDK has no method {method!r}")
        (self._orders if _order else self._general).acquire()
        return fn(**kwargs)

    def call_enveloped(self, method: str, source: str | None = None,
                       _order: bool = False, **kwargs: Any) -> dict:
        """Invoke and collapse into the standard {ok, source, data, error} shape."""
        src = source or method
        try:
            return envelope(self.call(method, _order=_order, **kwargs), src)
        except TypeError as exc:
            # A missing required kwarg is one of the few things the SDK raises.
            return err(f"bad arguments: {exc}", src, kind="TypeError")
        except Exception as exc:  # noqa: BLE001
            log.warning("call %s failed: %s", method, exc)
            return err(str(exc), src, kind=type(exc).__name__)

    def raw_post(self, endpoint: str, payload: dict | None = None,
                 _order: bool = False) -> dict:
        """Reach an endpoint the SDK does not wrap.

        The endpoint name must NOT carry a leading slash; see the module docstring.
        """
        name = (endpoint or "").strip().lstrip("/")
        if not name:
            return err("empty endpoint", "raw_post")
        body = {"apikey": self.settings.openalgo_api_key, **(payload or {})}
        (self._orders if _order else self._general).acquire()
        try:
            return envelope(self._api._make_request(name, body), name)
        except Exception as exc:  # noqa: BLE001
            log.warning("raw_post %s failed: %s", name, exc)
            return err(str(exc), name, kind=type(exc).__name__)

    # -- async wrappers ---------------------------------------------------

    async def acall(self, method: str, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.call, method, **kwargs)

    async def acall_enveloped(self, method: str, **kwargs: Any) -> dict:
        return await asyncio.to_thread(self.call_enveloped, method, **kwargs)

    async def araw_post(self, endpoint: str, payload: dict | None = None) -> dict:
        return await asyncio.to_thread(self.raw_post, endpoint, payload)

    # -- conveniences used across toolkits --------------------------------

    def ping(self) -> dict:
        return self.raw_post("ping")

    def analyzer_mode(self) -> str:
        """Returns 'analyze', 'live', or 'unknown'. Never raises."""
        res = self.call_enveloped("analyzerstatus")
        if not res.get("ok"):
            return "unknown"
        data = res.get("data") or {}
        mode = data.get("mode")
        if mode:
            return str(mode).lower()
        return "analyze" if data.get("analyze_mode") else "live"

    def ltp(self, symbol: str, exchange: str) -> float | None:
        """Last traded price, or None. Used by RiskGuard for notional checks."""
        res = self.call_enveloped("quotes", symbol=symbol, exchange=exchange)
        if not res.get("ok"):
            return None
        try:
            return float((res.get("data") or {}).get("ltp"))
        except (TypeError, ValueError):
            return None


_client: OpenAlgoClient | None = None
_client_lock = threading.Lock()


def get_client() -> OpenAlgoClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAlgoClient()
    return _client
