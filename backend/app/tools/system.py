"""System toolkit: analyzer mode, connectivity, sandbox P&L, alerts.

set_analyzer_mode is the one tool here that mutates anything, and it is the most
consequential switch in the whole application: turning analyzer mode off converts every
subsequent simulated order into a real one. It is confirmation-gated, and the UI
additionally requires the user to type GO LIVE before the approval is accepted.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.exceptions import RetryAgentRun
from agno.tools import Toolkit

from ..config import Settings, get_settings
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.normalize import ok, to_json

log = logging.getLogger(__name__)


class SystemTools(Toolkit):
    def __init__(self, client: OpenAlgoClient | None = None,
                 settings: Settings | None = None, **kwargs: Any) -> None:
        self.client = client or get_client()
        self.settings = settings or get_settings()
        super().__init__(
            name="system_tools",
            tools=[
                self.get_analyzer_status,
                self.set_analyzer_mode,
                self.get_sandbox_pnl,
                self.check_connection,
                self.send_alert,
            ],
            requires_confirmation_tools=["set_analyzer_mode"],
            **kwargs,
        )

    def get_analyzer_status(self) -> str:
        """Check whether orders are being simulated or sent to the real broker.

        Analyzer mode simulates order responses. Always report which mode is active when
        discussing an order.

        Returns:
            str: JSON with mode ("analyze" or "live") and the simulated log count.
        """
        res = self.client.call_enveloped("analyzerstatus")
        if res["ok"]:
            data = res.get("data") or {}
            res["mode"] = data.get("mode") or (
                "analyze" if data.get("analyze_mode") else "live")
            res["orders_are_real"] = res["mode"] == "live"
        return to_json(res)

    def set_analyzer_mode(self, analyze: bool) -> str:
        """Switch between simulated (analyze) and real (live) order execution.

        Turning analyze off means every subsequent order goes to the real broker with
        real money. Never call this unless the user has asked for it in plain terms.

        Args:
            analyze (bool): True for simulated orders, False for real orders.

        Returns:
            str: JSON with the resulting mode.
        """
        want_analyze = bool(analyze)
        if not want_analyze and self.settings.require_analyzer_mode:
            return to_json({
                "ok": False, "source": "analyzertoggle",
                "error": "REQUIRE_ANALYZER_MODE is true in the environment, so live mode "
                         "is blocked. The operator must change that setting on the server "
                         "before real orders can be placed.",
            })
        res = self.client.call_enveloped("analyzertoggle", mode=want_analyze)
        if res["ok"]:
            data = res.get("data") or {}
            res["mode"] = data.get("mode") or ("analyze" if want_analyze else "live")
            log.warning("analyzer mode set to %s", res["mode"])
        return to_json(res)

    def get_sandbox_pnl(self) -> str:
        """Get simulated profit and loss by symbol, for orders placed in analyze mode.

        Returns:
            str: JSON with per-symbol simulated P&L.
        """
        return to_json(self.client.raw_post("pnl/symbols"))

    def check_connection(self) -> str:
        """Verify the OpenAlgo server is reachable and the broker session is alive.

        Call this first if any other tool reports a connection problem.

        Returns:
            str: JSON with the connected broker name and the current analyzer mode.
        """
        res = self.client.ping()
        if not res["ok"]:
            return to_json({
                "ok": False, "source": "ping", "error": res.get("error"),
                "hint": "The OpenAlgo server may not be running, or the API key is wrong.",
            })
        return to_json(ok({"broker": (res.get("data") or {}).get("broker"),
                           "mode": self.client.analyzer_mode(),
                           "host": self.settings.openalgo_host}, "ping"))

    def send_alert(self, message: str, channel: str = "telegram",
                   to: str = "") -> str:
        """Send a notification to Telegram or WhatsApp through OpenAlgo.

        Args:
            message (str): The text to send.
            channel (str): Either "telegram" or "whatsapp". Defaults to "telegram".
            to (str): Optional recipient. For Telegram this is the OpenAlgo login id; for
                WhatsApp a phone number with country code. Falls back to the configured
                default.

        Returns:
            str: JSON delivery acknowledgement.
        """
        text = (message or "").strip()
        if not text:
            raise RetryAgentRun("send_alert needs a non-empty message.")

        ch = (channel or "telegram").strip().lower()
        if ch == "telegram":
            username = to or self.settings.telegram_username
            if not username:
                return to_json({
                    "ok": False, "source": "telegram",
                    "error": "No Telegram recipient. Set OPENALGO_TELEGRAM_USERNAME in "
                             "the environment or pass 'to'.",
                })
            return to_json(self.client.call_enveloped(
                "telegram", username=username, message=text))

        if ch == "whatsapp":
            recipient = to or self.settings.whatsapp_default_to
            kwargs: dict[str, Any] = {"message": text}
            if recipient:
                kwargs["to"] = recipient
            return to_json(self.client.call_enveloped("whatsapp", **kwargs))

        raise RetryAgentRun(
            f"channel must be 'telegram' or 'whatsapp', got {channel!r}."
        )
