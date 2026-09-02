"""Backend wiring for the charts page: session_state, tool scoping, and the polls.

Three things the chat and charts pages share one backend for, and that only fail
in combination:

  1. The chart reaches the tools through session_state, which agno deep-merges
     over the stored copy. A chat turn must therefore send chart=None, not omit
     the key, or a session that once carried a chart keeps it.
  2. The tool factory loads ChartTools only for a turn that has a chart, and never
     loads an order tool alongside them.
  3. /api/health and /api/mode are polled by the UI and call the synchronous SDK,
     so they must run off the event loop or every stream stalls behind them.

Nothing here needs a model or a broker: the merge is agno's own helper, the tool
factory is called directly, and the polls run against a stub client.

Run:  python backend/tests/test_chart_wiring.py
"""

from __future__ import annotations

import asyncio
import copy
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import setup_logging  # noqa: E402

setup_logging("WARNING")

from agno.run import RunContext  # noqa: E402
from agno.utils.merge_dict import merge_dictionaries  # noqa: E402

from app import main  # noqa: E402
from app.agent import build_tool_factory  # noqa: E402
from app.main import ChatRequest, _session_state_for  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []

CHART: dict[str, Any] = {
    "symbol": "RELIANCE", "exchange": "NSE", "interval": "5m",
    "visible_range": {"from": 1756700000, "to": 1756790000},
}


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion.

    Args:
        name: What was being checked.
        condition: The result.
        detail: Optional evidence to print alongside.
    """
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def kit_names(get_tools: Any, state: dict[str, Any]) -> set[str]:
    """The toolkit class names the factory resolves for a session_state.

    Args:
        get_tools: The callable build_tool_factory returned.
        state: The merged session_state a run would carry.

    Returns:
        The set of toolkit class names.
    """
    context = RunContext(run_id="run-1", session_id="session-1", session_state=state)
    return {type(kit).__name__ for kit in get_tools(context)}


def test_session_state() -> None:
    """The chart key is always present, and None clears a stored chart."""
    print("\n=== session_state per turn ===")
    chart_turn = _session_state_for(ChatRequest(message="mark the swing highs",
                                                chart_context=CHART))
    check("a chart turn carries the chart", chart_turn.get("chart") == CHART)

    chat_turn = _session_state_for(ChatRequest(message="what is my P&L"))
    check("a chat turn carries chart=None rather than no chart key",
          "chart" in chat_turn and chat_turn["chart"] is None, str(sorted(chat_turn)))
    empty = _session_state_for(ChatRequest(message="hello", chart_context={}))
    check("an empty chart context is None too", empty["chart"] is None)
    check("trading_enabled follows the request when it is given",
          _session_state_for(ChatRequest(message="x", trading_enabled=False))
          ["trading_enabled"] is False)
    check("trading_enabled follows the environment when it is not",
          _session_state_for(ChatRequest(message="x"))["trading_enabled"]
          is main.settings.trading_enabled)

    # agno's own merge: what the DB copy becomes after each kind of turn.
    stored = {"trading_enabled": True, "chart": CHART}
    omitted = copy.deepcopy(stored)
    merge_dictionaries(omitted, {"trading_enabled": True})
    check("(the defect) agno keeps a stored chart when the request omits the key",
          omitted.get("chart") == CHART)
    merged = copy.deepcopy(stored)
    merge_dictionaries(merged, chat_turn)
    check("an explicit None from a chat turn clears the stored chart",
          "chart" in merged and merged["chart"] is None)
    merged = copy.deepcopy(stored)
    merge_dictionaries(merged, _session_state_for(ChatRequest(
        message="x", chart_context={**CHART, "interval": "15m"})))
    check("a fresh chart replaces the stored one field by field",
          merged["chart"]["interval"] == "15m")


def test_tool_scoping() -> None:
    """ChartTools only with a chart, never beside an order tool, and gone after."""
    print("\n=== tool scoping ===")
    settings = copy.copy(main.settings)
    settings.trading_enabled = True
    settings.tool_profile = "full"
    get_tools = build_tool_factory(settings)

    chart_kits = kit_names(get_tools, {"trading_enabled": True, "chart": CHART})
    check("a chart turn loads ChartTools", "ChartTools" in chart_kits, str(sorted(chart_kits)))
    check("a chart turn loads no order tool even with trading enabled",
          not ({"OrderTools", "GttTools"} & chart_kits), str(sorted(chart_kits)))

    stored = {"trading_enabled": True, "chart": CHART}
    merge_dictionaries(stored, _session_state_for(ChatRequest(message="x",
                                                              trading_enabled=True)))
    chat_kits = kit_names(get_tools, stored)
    check("a chat turn after a chart turn in the same session sees no chart tools",
          "ChartTools" not in chat_kits, str(sorted(chat_kits)))
    check("and gets its order tools back", "OrderTools" in chat_kits, str(sorted(chat_kits)))

    stale = {"trading_enabled": True, "chart": CHART}
    merge_dictionaries(stale, {"trading_enabled": True})
    check("(control) a turn that omitted the key would have kept the chart tools",
          "ChartTools" in kit_names(get_tools, stale))

    plain = kit_names(get_tools, {"trading_enabled": False, "chart": None})
    check("a chat turn with trading off has neither chart nor order tools",
          not ({"ChartTools", "OrderTools", "GttTools"} & plain), str(sorted(plain)))


class StubClient:
    """Records which thread each broker call ran on, and how long it blocked."""

    def __init__(self, delay: float = 0.0) -> None:
        """Set up the recorder.

        Args:
            delay: Seconds each call sleeps, to show whether the loop is blocked.
        """
        self.delay = delay
        self.threads: list[int] = []

    def ping(self) -> dict[str, Any]:
        """A successful ping from a stub broker.

        Returns:
            The envelope /api/health reads.
        """
        self.threads.append(threading.get_ident())
        time.sleep(self.delay)
        return {"ok": True, "data": {"broker": "stub"}}

    def analyzer_mode(self) -> str:
        """Always analyze mode.

        Returns:
            "analyze".
        """
        self.threads.append(threading.get_ident())
        time.sleep(self.delay)
        return "analyze"


async def _polls_scenario(stub: StubClient) -> tuple[int, dict[str, Any], dict[str, Any], int]:
    """Call both polls while a ticker runs on the same loop.

    Args:
        stub: The stub client main.py has been pointed at.

    Returns:
        (loop thread id, health body, mode body, ticks counted during the polls).
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        health = await main.health()
        mode = await main.mode()
    finally:
        task.cancel()
    return threading.get_ident(), health, mode, ticks


def test_polls_off_loop() -> None:
    """The UI's polls must not stall the event loop behind a broker round trip."""
    print("\n=== health and mode polls ===")
    stub = StubClient(delay=0.4)
    saved = main.client
    main.client = stub  # type: ignore[assignment]
    try:
        loop_thread, health, mode, ticks = asyncio.run(_polls_scenario(stub))
    finally:
        main.client = saved
    check("/api/health pings the broker off the event loop",
          len(stub.threads) >= 1 and stub.threads[0] != loop_thread)
    check("/api/health reports the broker the stub named",
          health.get("openalgo_connected") is True and health.get("broker") == "stub",
          str(health))
    check("/api/mode reads the analyzer mode off the event loop",
          len(stub.threads) == 2 and stub.threads[1] != loop_thread)
    check("/api/mode reports the stub's mode",
          mode.get("mode") == "analyze" and mode.get("orders_are_real") is False,
          str({k: mode.get(k) for k in ("mode", "orders_are_real")}))
    check("the event loop kept running during 0.8s of broker calls",
          ticks >= 6, f"{ticks} ticks")


def main_() -> int:
    """Run the suite.

    Returns:
        1 when anything failed, 0 otherwise.
    """
    print("Chart wiring tests")
    test_session_state()
    test_tool_scoping()
    test_polls_off_loop()

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed, 0 skipped")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main_())
