"""Chart toolkit tests: registration, schemas, the event pattern, state, and live draws.

Five things here are worth more than the rest.

Schema generation, because agno builds the JSON schema from get_type_hints plus a parsed
docstring, so a missing Args: block silently yields a tool the model cannot call.

Confirmation flags, because a typo in requires_confirmation_tools only WARNS in agno
2.8.7. This page is read-only and must never pause for approval, so it is asserted.

The generator-tool contract, because the whole design rests on it: a tool yields a
ChartCommandEvent and then a plain string, the event reaches the UI before a word of
prose is written, and agno concatenates str() of every yielded event into the tool
result. That last part is why ChartCommandEvent.__str__ is empty, and it is asserted
rather than trusted.

State across tools, because the browser writes the chart context once per request. A
symbol switch followed by a draw in the same turn used to draw the OLD instrument on
the new chart, and a channel stored on RELIANCE was measured and narrated as SBIN one
turn later. The synthetic section drives both through a fake frame cache so they are
deterministic; the live section repeats the interval switch against OpenAlgo.

The "supertrend 3,10" reading, because both readings sit inside the declared bounds and
the tool has to state which one it used.

Run:  python backend/tests/test_tools_charts.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.config import setup_logging  # noqa: E402

setup_logging("WARNING")

from agno.exceptions import RetryAgentRun  # noqa: E402
from agno.run.agent import CustomEvent, RunOutputEvent  # noqa: E402
from agno.run.base import RunContext  # noqa: E402
from typing import get_args  # noqa: E402

from app.charts import geometry as G  # noqa: E402
from app.charts.contract import ChartContext  # noqa: E402
from app.openalgo.client import get_client  # noqa: E402
from app.openalgo.normalize import MAX_TOOL_CHARS  # noqa: E402
from app.tools.charts import (  # noqa: E402
    LIST_DETAIL_LIMIT,
    ChartCommandEvent,
    ChartTools,
    _restore_channel,
    assign_positional,
    load_catalogue,
    match_indicator,
    resolve_indicator_id,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []

EXPECTED = [
    "set_chart_symbol", "set_chart_interval", "set_chart_type",
    "list_chart_indicators", "add_chart_indicator", "remove_chart_indicator",
    "clear_annotations",
    "draw_envelope", "draw_trendline", "draw_levels", "draw_zone",
    "project_targets",
    "analyse_trend", "analyse_momentum", "describe_chart",
]

# What the browser sends every turn. Times are UTC seconds.
LIVE_CONTEXT = {
    "symbol": "RELIANCE", "exchange": "NSE", "interval": "D",
    "chartType": "candlestick", "barCount": 300,
    "firstTime": 1_700_000_000, "lastTime": 1_780_000_000,
    "visibleFrom": None, "visibleTo": None,
    "lastPrice": 1400.0,
    "indicators": [{"instanceId": "i1", "indicatorId": "supertrend",
                    "name": "Supertrend", "paneIndex": 0,
                    "settings": {"period": 10, "multiplier": 3}}],
    "theme": "dark",
}

#: How a trader actually names an indicator, spread across all four categories and all
#: 102 ids. Not one of these is a registry id spelled out in full; they are the
#: abbreviations, short forms, spoken phrases and length-with-the-name forms that reach
#: the tool in real use. Against the resolver this replaced, 100 of the 145 resolved.
COLLOQUIAL = [
    # Trend
    ("20 ema", "ema"), ("ema 20", "ema"), ("ema20", "ema"), ("50 sma", "sma"),
    ("simple moving average", "sma"), ("exponential moving average", "ema"),
    ("weighted moving average", "wma"), ("hull moving average", "hma"), ("hma", "hma"),
    ("super trend", "supertrend"), ("supertrend 3,10", "supertrend"),
    ("st", "supertrend"), ("psar", "parabolic-sar"), ("parabolic sar", "parabolic-sar"),
    ("ichimoku", "ichimoku"), ("ichimoku cloud", "ichimoku"), ("adx", "adx"),
    ("dmi", "adx"), ("alma", "alma"), ("dema", "dema"), ("tema", "tema"),
    ("kama", "kama"), ("kaufman", "kama"), ("t3", "t3"),
    ("chande kroll", "chande-kroll-stop"), ("cks", "chande-kroll-stop"),
    ("chandelier exit", "chandelier-exit"), ("aroon", "aroon"),
    ("aroon oscillator", "aroon-oscillator"), ("lsma", "lsma"),
    ("linear regression slope", "linreg-slope"), ("ma cross", "ma-cross"),
    ("mcginley", "mcginley-dynamic"), ("moving average ribbon", "ma-ribbon"),
    ("twap", "twap"), ("alligator", "alligator"), ("smma", "smma"),
    ("smoothed moving average", "smma"), ("vortex", "vortex"),
    ("volatility stop", "volatility-stop"), ("williams fractals", "williams-fractals"),
    ("fractals", "williams-fractals"), ("cpr", "cpr"), ("alphatrend", "alphatrend"),
    ("half trend", "halftrend"), ("hull suite", "hull-suite"),
    ("trend strength index", "trend-strength-index"), ("seasonality", "seasonality"),
    ("median", "median"), ("consolidation breakout", "consolidation-breakout"),
    # Volume
    ("vwap", "vwap"), ("obv", "obv"), ("on balance volume", "obv"),
    ("cmf", "chaikin-money-flow"), ("chaikin money flow", "chaikin-money-flow"),
    ("chaikin oscillator", "chaikin-oscillator"), ("eom", "ease-of-movement"),
    ("ease of movement", "ease-of-movement"), ("efi", "elder-force-index"),
    ("force index", "elder-force-index"), ("net volume", "net-volume"),
    ("klinger", "klinger-oscillator"), ("kvo", "klinger-oscillator"), ("vwma", "vwma"),
    ("nvi", "nvi"), ("pvi", "pvi"), ("pvt", "pvt"), ("pvo", "pvo"), ("adl", "adl"),
    ("accumulation distribution", "adl"), ("volume", "volume"),
    # Volatility
    ("bbands", "bollinger"), ("boll", "bollinger"), ("bollinger bands", "bollinger"),
    ("atr", "atr"), ("average true range", "atr"), ("vix fix", "williams-vix-fix"),
    ("wvf", "williams-vix-fix"), ("donchian channel", "donchian"),
    ("donchian", "donchian"), ("keltner", "keltner-channel"),
    ("keltner channel", "keltner-channel"),
    ("bollinger bandwidth", "bollinger-bandwidth"), ("bbw", "bollinger-bandwidth"),
    ("%b", "bollinger-percent-b"), ("choppiness", "choppiness-index"),
    ("ulcer", "ulcer-index"), ("historical volatility", "historical-volatility"),
    ("hv", "historical-volatility"), ("adr", "average-daily-range"),
    ("chop zone", "chop-zone"), ("standard deviation", "standard-deviation"),
    ("stdev", "standard-deviation"), ("mass index", "mass-index"),
    ("relative volatility index", "relative-volatility-index"),
    ("envelope", "envelope"), ("standard error bands", "standard-error-bands"),
    ("moving average channel", "ma-channel"), ("bbtrend", "bb-trend"),
    ("range analysis", "range-analysis"), ("chaikin volatility", "chaikin-volatility"),
    # Momentum
    ("rsi", "rsi"), ("macd", "macd"), ("stoch rsi", "stochastic-rsi"),
    ("stochrsi", "stochastic-rsi"), ("stochastic rsi", "stochastic-rsi"),
    ("stochastic", "stochastic"), ("stoch", "stochastic"),
    ("williams %r", "williams-percent-r"), ("willr", "williams-percent-r"),
    ("%r", "williams-percent-r"), ("kst", "know-sure-thing"),
    ("know sure thing", "know-sure-thing"), ("ao", "awesome-oscillator"),
    ("awesome oscillator", "awesome-oscillator"), ("cci", "cci"), ("mfi", "mfi"),
    ("money flow index", "mfi"), ("bop", "balance-of-power"),
    ("balance of power", "balance-of-power"), ("cmo", "chande-momentum"),
    ("chande momentum", "chande-momentum"), ("coppock", "coppock-curve"),
    ("dpo", "dpo"), ("detrended price oscillator", "dpo"),
    ("fisher transform", "fisher-transform"), ("connors rsi", "connors-rsi"),
    ("momentum", "momentum"), ("roc", "roc"), ("rate of change", "roc"),
    ("ppo", "ppo"), ("trix", "trix"), ("tsi", "tsi"),
    ("true strength index", "tsi"), ("smi", "smi"),
    ("ultimate oscillator", "ultimate-oscillator"), ("uo", "ultimate-oscillator"),
    ("relative vigor", "relative-vigor-index"), ("rvi", "relative-vigor-index"),
    ("woodies cci", "woodies-cci"), ("special k", "special-k"),
    ("rsi divergence", "rsi-divergence"), ("wavetrend", "wavetrend"),
    ("smi ergodic indicator", "smi-ergodic-indicator"),
    ("smi ergodic oscillator", "smi-ergodic-oscillator"),
]

#: Phrases the resolver used to answer with the WRONG indicator, each with the id it
#: must not return. Every one was confirmed by execution against the old resolver.
WRONG_ANSWERS = [
    ("volume weighted average price", "volume"),
    ("volume profile", "volume"),
    ("volume oscillator", "volume"),
    ("ema cross", "ema"),
    ("median price", "median"),
    ("volatility", "volatility-stop"),
    ("trend", "trend-strength-index"),
    ("range", "range-analysis"),
    ("linear regression", "linreg-slope"),
]

#: Bare generic words: never an answer, always a question with candidates.
GENERIC_WORDS = ["trend", "range", "volatility", "index", "oscillator", "channel",
                 "bands", "average", "stop", "price", "cross", "profile"]


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


def context_for(chart: dict | None) -> RunContext:
    """A RunContext carrying the session state the tools read and write."""
    state: dict = {}
    if chart is not None:
        state["chart"] = dict(chart)
    return RunContext(run_id="test-run", session_id="test-session", session_state=state)


async def drive(fn, run_context: RunContext, **kwargs):
    """Call one tool and split what it produced into events and text.

    Returns:
        A (commands, text, order) triple: every command dict yielded on
        ChartCommandEvents, the concatenated plain-string output the model would
        actually read, and the order events and text arrived in.
    """
    entrypoint = fn.entrypoint
    result = entrypoint(run_context=run_context, **kwargs)

    if inspect.isasyncgen(result):
        commands: list[dict] = []
        model_text = ""
        order: list[str] = []
        async for item in result:
            if isinstance(item, CustomEvent):
                order.append("event")
                commands.extend(getattr(item, "commands", None) or [])
                # Exactly what agno does with a yielded event, verbatim.
                model_text += str(item)
            else:
                order.append("text")
                model_text += str(item)
        return commands, model_text, order

    if inspect.isawaitable(result):
        return [], str(await result), ["text"]
    return [], str(result), ["text"]


# --- synthetic frames and a fake frame cache --------------------------------


def synthetic_frame(kind: str, base: float = 1400.0, n: int = 600, seed: int = 1,
                    start: str = "2024-01-01") -> pd.DataFrame:
    """A daily OHLCV frame of a known shape, priced around ``base``.

    range: a clean band, plus or minus six percent, the ordinary channel case.
    trend: a 0.5 point per day rally with small swings, for the slope units.
    triangle: converging swings with the apex inside the window, so the rails cross.
    onerail: swing highs but a monotonic low series, so only one rail fits.
    tiny: a band between about 2 and 20, so the downside target falls below zero.
    flat: a constant price, so nothing can be fitted.
    """
    rng = np.random.default_rng(seed)
    x = np.arange(n, dtype=float)
    spread = base * 0.003
    if kind == "range":
        close = base + 0.06 * base * np.sin(x / 6.0) + rng.normal(0, base * 0.002, n)
    elif kind == "trend":
        close = base + 0.5 * x + 3.0 * np.sin(x / 4.0)
    elif kind == "triangle":
        amp = np.maximum(0.0, 0.05 * base * (1.0 - x / (n * 0.8)))
        close = base + amp * np.sin(x / 4.0)
    elif kind == "onerail":
        # Rising close with bumpy highs, and a single dip in the lows three
        # quarters of the way along: one swing low, so the envelope's alternation
        # keeps the highest high on either side of it and the lower rail cannot
        # be fitted. A frame with NO swing low yields one high, not many.
        close = base + 0.3 * x
    elif kind == "tiny":
        close = 11.0 + 9.0 * np.sin(x / 4.0)
        spread = 0.05
    else:
        close = np.full(n, base)
    high = close + spread
    low = close - spread
    if kind == "onerail":
        high = close + 4.0 * spread * (1.0 + np.sin(x / 3.0))
        low = close - spread - 20.0 * spread * np.exp(-((x - n * 0.75) / 3.0) ** 2)
    idx = pd.date_range(start, periods=n, freq="D", name="timestamp")
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1000.0), "oi": np.zeros(n)}, index=idx)


class FakeFrames:
    """A frame cache that serves synthetic frames by symbol and honours lookback_bars."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[tuple[str, str, str, int]] = []

    def get_frame(self, symbol: str, exchange: str, interval: str,
                  start_date: str | None = None, end_date: str | None = None,
                  lookback_bars: int = 300, source: str = "api") -> dict[str, Any]:
        self.calls.append((symbol, exchange, interval, int(lookback_bars)))
        frame = self.frames.get(symbol.upper())
        if frame is None:
            return {"ok": False, "error": f"no candles for {symbol}"}
        return {"ok": True, "frame": frame.iloc[-int(lookback_bars):], "cached": False}


def context_from_frame(symbol: str, df: pd.DataFrame, visible: tuple[int, int] | None = None,
                       bar_count: int | None = None, **extra: Any) -> dict:
    """A browser-style chart context whose times come from the frame itself."""
    times = G.to_utc_seconds(df.index)
    ctx = {
        "symbol": symbol, "exchange": "NSE", "interval": "D",
        "chartType": "candlestick", "barCount": bar_count or len(df),
        "firstTime": float(times[0]), "lastTime": float(times[-1]),
        "visibleFrom": None, "visibleTo": None,
        "lastPrice": float(df["close"].iloc[-1]),
        "indicators": [], "theme": "dark",
    }
    if visible is not None:
        ctx["visibleFrom"] = float(times[visible[0]])
        ctx["visibleTo"] = float(times[visible[1]])
    ctx.update(extra)
    return ctx


def last_close_fallback(frame: pd.DataFrame) -> float:
    return float(frame["close"].iloc[-1])


def rail_anchors(cmds: list[dict]) -> list[dict]:
    """The from/to anchors of every trendline in the first draw command."""
    if not cmds:
        return []
    return [s[k] for s in cmds[0].get("shapes", []) for k in ("from", "to") if k in s]


# --- registration and schemas -----------------------------------------------


def test_registration() -> ChartTools:
    print("\n=== toolkit registration ===")
    kit = ChartTools()
    merged = kit.get_async_functions()
    check("chart_tools registers 15 tools",
          sorted(merged) == sorted(EXPECTED),
          f"{sorted(merged)}" if sorted(merged) != sorted(EXPECTED) else "15")

    # Documented split, asserted so a future refactor cannot flip it in silence:
    # async generators answer False to iscoroutinefunction and land in .functions,
    # plain coroutines land in .async_functions. Only get_async_functions() has both.
    check("the 11 drawing tools and the catalogue lookup are the sync-dict half",
          len(kit.functions) == 12 and "draw_envelope" in kit.functions
          and "list_chart_indicators" in kit.functions,
          f"{len(kit.functions)} in functions")
    check("the 3 analysis tools are the async-dict half",
          sorted(kit.async_functions) == ["analyse_momentum", "analyse_trend",
                                          "describe_chart"],
          str(sorted(kit.async_functions)))
    return kit


def test_schemas(kit: ChartTools) -> None:
    print("\n=== schema generation ===")
    # Function.description and Function.parameters are populated LAZILY by
    # process_entrypoint(), which the Agent calls when it assembles its tool list.
    functions = kit.get_async_functions()
    for fn in functions.values():
        fn.process_entrypoint()

    problems: list[str] = []
    described = 0
    for fn in functions.values():
        params = fn.parameters or {}
        props = params.get("properties", {})
        if fn.description:
            described += 1
        else:
            problems.append(f"{fn.name}: no description")
        sig = inspect.signature(fn.entrypoint)
        for pname in sig.parameters:
            if pname in ("self", "agent", "team", "run_context"):
                continue
            if pname not in props:
                problems.append(f"{fn.name}: arg {pname} missing from schema")
            elif "type" not in props[pname] and "anyOf" not in props[pname]:
                problems.append(f"{fn.name}: arg {pname} has no type")

    check("every tool has a description", described == 15, f"{described}/15")
    check("every argument reached the schema with a type", not problems,
          "; ".join(problems[:4]) if problems else "clean")

    check("run_context never reaches the model's schema",
          all("run_context" not in (fn.parameters or {}).get("properties", {})
              for fn in functions.values()))

    fn = functions["add_chart_indicator"]
    props = (fn.parameters or {}).get("properties", {})
    check("add_chart_indicator schema has indicator_id and settings",
          "indicator_id" in props and "settings" in props, str(sorted(props)))
    check("arg descriptions survived the docstring parse",
          bool(props.get("indicator_id", {}).get("description")),
          str(props.get("indicator_id", {}).get("description", ""))[:60])

    fn = functions["draw_envelope"]
    props = (fn.parameters or {}).get("properties", {})
    check("draw_envelope prices typed as numbers",
          props.get("from_price", {}).get("type") == "number"
          and props.get("to_price", {}).get("type") == "number",
          str({k: v.get("type") for k, v in props.items()}))


def test_confirmation_flags(kit: ChartTools) -> None:
    print("\n=== confirmation flags ===")
    gated = [f.name for f in kit.get_async_functions().values()
             if getattr(f, "requires_confirmation", False)]
    # The page is read-only at the TOOL layer, not just in the prompt. A chart
    # turn used to inherit the environment's trading_enabled and register every
    # order tool; a model that called one paused the run for an approval the
    # charts panel cannot give. This is the test that keeps that door shut.
    from types import SimpleNamespace as _NS
    from app.agent import build_tool_factory as _btf
    from app.config import get_settings as _gs
    _factory = _btf(_gs())
    def _names(kits):
        out = set()
        for k in kits:
            out |= set(getattr(k, "functions", {}) or {}) | set(getattr(k, "async_functions", {}) or {})
        return out
    _chart_turn = _names(_factory(_NS(session_state={"chart": {"symbol": "RELIANCE"}})))
    _chat_turn = _names(_factory(_NS(session_state={})))
    _order_names = {n for n in _chat_turn if n.startswith(("place_", "modify_", "cancel_", "close_all", "manage_gtt"))}
    check("a chart turn registers no order tool at all",
          len(_order_names & _chart_turn) == 0 and len(_order_names) > 0,
          f"{len(_order_names & _chart_turn)} of {len(_order_names)} order tools reachable")
    check("a chart turn still has the chart tools",
          "draw_envelope" in _chart_turn and "list_chart_indicators" in _chart_turn, str(len(_chart_turn)))
    check("a chat turn does not see the chart tools",
          "draw_envelope" not in _chat_turn, str(len(_chat_turn)))
    check("no chart tool is confirmation gated", gated == [], str(gated))

    external = [f.name for f in kit.get_async_functions().values()
                if getattr(f, "external_execution", False)]
    check("no chart tool needs external execution", external == [], str(external))

    cached = [f.name for f in kit.get_async_functions().values()
              if getattr(f, "cache_results", False)]
    check("no chart tool caches to disk", cached == [], str(cached))


# --- the event contract -----------------------------------------------------


def test_event_contract() -> None:
    print("\n=== ChartCommandEvent contract ===")
    event = ChartCommandEvent(commands=[{"op": "clear"}])

    check("ChartCommandEvent is an agno CustomEvent", isinstance(event, CustomEvent))
    check("agno's dispatch test accepts it",
          isinstance(event, tuple(get_args(RunOutputEvent))),
          "isinstance(item, tuple(get_args(RunOutputEvent)))")
    check("event name is CustomEvent", event.event == "CustomEvent", event.event)

    # agno does `function_call_output += str(item)` for every yielded CustomEvent
    # (models/base.py:2202, 2749, 2887). Anything non-empty here is geometry leaking
    # into the model's context and eating the 12,000 char budget.
    check("str() contributes nothing to the tool result", str(event) == "",
          repr(str(event))[:60])
    check("repr() still shows the commands, for logs",
          "commands=" in repr(event), repr(event)[-60:])

    # to_dict() is asdict-based, so only real dataclass FIELDS reach the browser.
    payload = event.to_dict()
    check("commands survive to_dict for the SSE wire",
          payload.get("commands") == [{"op": "clear"}], str(payload.get("commands")))


def test_context_contract() -> None:
    print("\n=== ChartContext contract ===")
    ctx = ChartContext.model_validate({**LIVE_CONTEXT, "analystGroups": ["envelope", "levels"]})
    check("analystGroups arrives from the browser's camelCase key",
          ctx.analyst_groups == ["envelope", "levels"], str(ctx.analyst_groups))
    check("the snake_case name validates too",
          ChartContext.model_validate({"symbol": "X", "analyst_groups": ["zone"]}).analyst_groups
          == ["zone"])
    check("a frontend that does not send it yet gets None, not an empty list",
          ChartContext.model_validate(LIVE_CONTEXT).analyst_groups is None)
    check("an empty list is preserved as an empty list, which means cleared",
          ChartContext.model_validate({**LIVE_CONTEXT, "analystGroups": []}).analyst_groups == [])
    dumped = ChartContext.model_validate({**LIVE_CONTEXT, "analystGroups": ["zone"]}).model_dump(by_alias=True)
    check("the field round-trips by alias, so a rewritten context keeps it",
          dumped.get("analystGroups") == ["zone"] and "visibleFrom" in dumped, str(sorted(dumped))[:80])


async def test_ordering_and_shapes(kit: ChartTools) -> None:
    print("\n=== command emission ===")
    functions = kit.get_async_functions()
    rc = context_for(LIVE_CONTEXT)

    cmds, text, order = await drive(functions["set_chart_type"], rc,
                                    chart_type="hollow-candle")
    check("set_chart_type emits the event BEFORE the prose",
          order[:2] == ["event", "text"], str(order))
    check("set_chart_type command shape",
          cmds == [{"op": "set_chart_type", "chartType": "hollow-candle"}], str(cmds))
    check("set_chart_type text is short and clean",
          0 < len(text) < 200 and text.startswith("Chart type set"), text)
    chart = rc.session_state["chart"]
    check("set_chart_type rewrites the chart type in session state and keeps the range",
          chart.get("chartType") == "hollow-candle" and chart.get("lastTime") == 1_780_000_000
          and chart.get("lastPrice") == 1400.0, str({k: chart.get(k) for k in ("chartType", "lastTime")}))

    cmds, text, _ = await drive(functions["set_chart_symbol"], rc,
                                symbol="nifty 50", exchange="NSE")
    check("set_chart_symbol corrects a spoken index name",
          cmds == [{"op": "set_symbol", "symbol": "NIFTY", "exchange": "NSE_INDEX"}],
          str(cmds))
    chart = rc.session_state["chart"]
    check("set_chart_symbol rewrites session_state['chart'] in place for the rest of the turn",
          chart.get("symbol") == "NIFTY" and chart.get("exchange") == "NSE_INDEX"
          and chart.get("interval") == "D",
          str({k: chart.get(k) for k in ("symbol", "exchange", "interval")}))
    check("and clears the loaded range, the viewport and the last price",
          all(chart.get(k) is None for k in ("firstTime", "lastTime", "visibleFrom",
                                               "visibleTo", "lastPrice")),
          str({k: chart.get(k) for k in ("firstTime", "lastTime", "lastPrice")}))

    cmds, text, _ = await drive(functions["clear_annotations"], rc, group="envelope")
    check("clear_annotations targets one group",
          cmds == [{"op": "clear", "group": "envelope"}], str(cmds))
    cmds, text, _ = await drive(functions["clear_annotations"], rc)
    check("clear_annotations with no group clears every analyst layer",
          cmds == [{"op": "clear"}], str(cmds))

    try:
        await drive(functions["set_chart_type"], rc, chart_type="renko")
        check("renko is refused as a bar transform, not a chart type", False, "no raise")
    except RetryAgentRun as exc:
        check("renko is refused as a bar transform, not a chart type",
              "transform" in str(exc), str(exc)[:70])

    try:
        await drive(functions["set_chart_type"], rc, chart_type="spaghetti")
        check("an unknown chart type raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("an unknown chart type raises RetryAgentRun",
              "Available types" in str(exc), str(exc)[:60])

    # A fresh context for the zone: the symbol switch above cleared the range.
    rc = context_for(LIVE_CONTEXT)
    cmds, text, _ = await drive(functions["draw_zone"], rc,
                                from_price=1465, to_price=1420, label="supply")
    shape = (cmds[0]["shapes"][0] if cmds else {})
    check("draw_zone emits a zone shape on the zone group",
          cmds and cmds[0]["op"] == "draw" and cmds[0]["group"] == "zone"
          and shape.get("kind") == "zone", str(cmds)[:120])
    check("zone uses the contract's 'from' alias, not 'from_'",
          "from" in shape and "from_" not in shape, str(sorted(shape)))
    check("zone anchors carry UTC seconds and prices",
          shape.get("from", {}).get("price") == 1465.0
          and shape.get("to", {}).get("price") == 1420.0, str(shape))
    check("a zone above price is toned bearish", shape.get("tone") == "bearish",
          str(shape.get("tone")))

    rc_state = rc.session_state or {}
    check("the zone outlived the turn in session state",
          "zone" in (rc_state.get("chart_patterns") or {}),
          str(sorted(rc_state.get("chart_patterns") or {})))


# --- the indicator catalogue ------------------------------------------------


def test_catalogue() -> None:
    print("\n=== indicator catalogue ===")
    catalogue = load_catalogue()
    indicators = catalogue.get("indicators", {})
    check("the generated catalogue holds the chart's 102 indicators",
          len(indicators) == 102, f"{len(indicators)} from openalgo-charts "
                                  f"{catalogue.get('chart_engine_version')}")
    check("catalogue carries the chart types too",
          "candlestick" in catalogue.get("chart_types", []),
          str(catalogue.get("chart_types", []))[:80])

    spec = indicators.get("supertrend", {})
    by_key = {i["key"]: i for i in spec.get("inputs", [])}
    check("supertrend declares ATR Period and Multiplier with bounds",
          by_key.get("period", {}).get("label") == "ATR Period"
          and by_key["period"]["default"] == 10 and by_key["period"]["max"] == 200
          and by_key["multiplier"]["default"] == 3 and by_key["multiplier"]["min"] == 0.1,
          str({k: (v.get("default"), v.get("min"), v.get("max")) for k, v in by_key.items()
               if v.get("type") == "number"}))

    check("spoken names resolve to registry ids",
          resolve_indicator_id("Super Trend") == "supertrend"
          and resolve_indicator_id("bollinger bands") == "bollinger"
          and resolve_indicator_id("psar") == "parabolic-sar"
          and resolve_indicator_id("RSI") == "rsi")
    check("an unknown name resolves to nothing",
          resolve_indicator_id("moon phase") is None)

    # Both readings of 3,10 sit inside the declared bounds, so the tie is broken by
    # closeness to the indicator's own defaults.
    settings, reading, spare = assign_positional(indicators["supertrend"], [3, 10])
    check("supertrend 3,10 resolves to period 10 and multiplier 3",
          settings == {"period": 10, "multiplier": 3}, str(settings))
    check("the reading is stated in the indicator's own labels",
          reading == "ATR Period 10, Multiplier 3", reading)
    check("nothing is left over", spare == [], str(spare))
    check("supertrend 10,3 reads the same way",
          assign_positional(indicators["supertrend"], [10, 3])[0] == settings)

    check("macd 12,26,9 stays in declaration order",
          list(assign_positional(indicators["macd"], [12, 26, 9])[0].values())
          == [12, 26, 9], str(assign_positional(indicators["macd"], [12, 26, 9])[0]))
    check("rsi 21 lands on Length, not on the oversold band",
          assign_positional(indicators["rsi"], [21])[0] == {"length": 21},
          str(assign_positional(indicators["rsi"], [21])[0]))

    # Every numeric setting is a slot. RSI declares length, overbought, oversold; the
    # first-N rule read "rsi 30 70" as a length of 30 and never looked at oversold.
    settings, reading, spare = assign_positional(indicators["rsi"], [30, 70])
    check("rsi 30 70 lands on the oversold and overbought bands, not on the length",
          settings == {"overbought": 70, "oversold": 30}, str(settings))
    check("rsi 25 75 does too", assign_positional(indicators["rsi"], [25, 75])[0]
          == {"overbought": 75, "oversold": 25},
          str(assign_positional(indicators["rsi"], [25, 75])[0]))
    check("rsi 14 30 70 fills all three in the natural order",
          assign_positional(indicators["rsi"], [14, 30, 70])[0]
          == {"length": 14, "overbought": 70, "oversold": 30})
    check("bollinger 2 is the standard deviation, an exact default hit",
          assign_positional(indicators["bollinger"], [2])[0] == {"stdDev": 2},
          str(assign_positional(indicators["bollinger"], [2])[0]))
    settings, reading, spare = assign_positional(indicators["ema"], [200, 50])
    check("ema 200 50 places the first number and hands the second back",
          settings == {"length": 200} and spare == [50.0], f"{settings} spare={spare}")


def _tight(text: str) -> str:
    """The resolver's own normalisation: lowercase, letters and digits only."""
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def test_resolver() -> None:
    """Every one of the 102 is reachable, and no fuzzy step ever guesses.

    Three full sweeps, then a corpus of what people actually type, then the collisions
    that must NOT resolve. The last group is the point of the exercise: a wrong
    indicator drawn confidently costs more than one clarifying question.
    """
    print("\n=== indicator name resolution ===")
    catalogue = load_catalogue().get("indicators", {})
    total = len(catalogue)

    # The resolver's lookup maps are plain dicts, so a normalisation collision between
    # two ids or two display names would silently drop one of them.
    ids = {_tight(k) for k in catalogue}
    names = {_tight(e.get("name", "")) for e in catalogue.values()}
    check("no two ids and no two display names collide once normalised",
          len(ids) == total and len(names) == total,
          f"{len(ids)} ids, {len(names)} names, {total} indicators")

    missed = [k for k in catalogue if resolve_indicator_id(k) != k]
    check(f"all {total} resolve by registry id", not missed,
          f"{total - len(missed)}/{total}, missed {missed[:5]}")

    missed = [k for k, e in catalogue.items() if resolve_indicator_id(e["name"]) != k]
    check(f"all {total} resolve by display name", not missed,
          f"{total - len(missed)}/{total}, missed {missed[:5]}")

    missed = [k for k, e in catalogue.items()
              if resolve_indicator_id(_tight(k)) != k
              or resolve_indicator_id(_tight(e["name"])) != k]
    check(f"all {total} resolve by the normalised form of id and name", not missed,
          f"{total - len(missed)}/{total}, missed {missed[:5]}")

    # The corpus. Every entry is a phrasing a trader types rather than a registry id.
    unresolved = [(phrase, want, resolve_indicator_id(phrase))
                  for phrase, want in COLLOQUIAL
                  if resolve_indicator_id(phrase) != want]
    hits = len(COLLOQUIAL) - len(unresolved)
    print(f"        colloquial corpus: {hits}/{len(COLLOQUIAL)} "
          f"({hits / len(COLLOQUIAL) * 100:.1f}%)")
    for phrase, want, got in unresolved:
        print(f"        UNRESOLVED {phrase!r}: wanted {want}, got {got}")
    check(f"all {len(COLLOQUIAL)} colloquial phrasings resolve to the right indicator",
          not unresolved, f"{hits}/{len(COLLOQUIAL)}")

    # Every one of these has a longer id that starts with the same letters. Landing on
    # the variant instead of the base indicator is the failure mode that matters.
    check("'rsi' stays RSI, never stochastic-rsi, connors-rsi or rsi-divergence",
          resolve_indicator_id("rsi") == "rsi", str(resolve_indicator_id("rsi")))
    check("'sma' stays SMA, never smma",
          resolve_indicator_id("sma") == "sma", str(resolve_indicator_id("sma")))
    check("'stochastic' stays Stochastic, never stochastic-rsi",
          resolve_indicator_id("stochastic") == "stochastic",
          str(resolve_indicator_id("stochastic")))
    check("'bollinger' stays Bollinger Bands, never the bandwidth or %b variant",
          resolve_indicator_id("bollinger") == "bollinger",
          str(resolve_indicator_id("bollinger")))
    check("'smi' stays Stochastic Momentum Index, never an ergodic variant",
          resolve_indicator_id("smi") == "smi", str(resolve_indicator_id("smi")))

    # "ma" is genuinely ambiguous, so it must ask rather than pick.
    ambiguous = match_indicator("ma")
    check("'ma' resolves to nothing, because it could be any of several",
          ambiguous.indicator_id is None, str(ambiguous.indicator_id))
    check("'ma' hands back the candidates so the caller can ask",
          len(ambiguous.candidates) > 1 and "ma-cross" in ambiguous.candidates
          and "ma-ribbon" in ambiguous.candidates, str(ambiguous.candidates))

    for phrase in ("chaikin", "smi ergodic", "hull", "chop"):
        match = match_indicator(phrase)
        check(f"{phrase!r} is reported as ambiguous, not guessed at",
              match.indicator_id is None and len(match.candidates) > 1,
              f"{match.indicator_id} {match.candidates}")

    nothing = match_indicator("moon phase")
    check("a name that matches nothing resolves to nothing, with no candidates",
          nothing.indicator_id is None and not nothing.candidates
          and not nothing.values, str(nothing))

    # A number said with the name is the indicator's length, either way round.
    for phrase in ("20 ema", "ema 20", "ema20"):
        match = match_indicator(phrase)
        check(f"{phrase!r} is an EMA carrying the 20",
              match.indicator_id == "ema" and list(match.values) == [20.0], str(match))
    settings, _, _ = assign_positional(catalogue["ema"],
                                       list(match_indicator("20 ema").values))
    check("the spoken 20 lands on Length through assign_positional, not a second path",
          settings == {"length": 20}, str(settings))

    check("a digit that is part of an id is never torn off it",
          match_indicator("t3").indicator_id == "t3"
          and not match_indicator("t3").values, str(match_indicator("t3")))
    match = match_indicator("supertrend 3,10")
    check("'supertrend 3,10' keeps both numbers in the order they were said",
          match.indicator_id == "supertrend" and list(match.values) == [3.0, 10.0],
          str(match))

    # THE WRONG ANSWERS. A generic word inside the phrase that is itself an id used to
    # win the reverse-containment step: "volume weighted average price" drew Volume.
    wrong = [(phrase, bad, resolve_indicator_id(phrase)) for phrase, bad in WRONG_ANSWERS
             if resolve_indicator_id(phrase) == bad]
    for phrase, bad, got in wrong:
        print(f"        WRONG {phrase!r} still resolves to {got}")
    check(f"none of the {len(WRONG_ANSWERS)} wrong-answer phrases resolves to the wrong id",
          not wrong, f"{len(wrong)} still wrong")
    check("'volume weighted average price' is VWAP, by the alias that consumes every word",
          resolve_indicator_id("volume weighted average price") == "vwap",
          str(resolve_indicator_id("volume weighted average price")))
    for phrase in ("volume profile", "ema cross", "median price", "linear regression"):
        match = match_indicator(phrase)
        check(f"{phrase!r} refuses rather than guessing",
              match.indicator_id is None, f"{match.indicator_id} {match.candidates[:4]}")
    generic = [(w, match_indicator(w)) for w in GENERIC_WORDS]
    bad_generic = [w for w, m in generic if m.indicator_id is not None]
    haystack = [_tight(k) + " " + _tight(e.get("name", "")) for k, e in catalogue.items()]
    contained = {w for w in GENERIC_WORDS if any(w in h for h in haystack)}
    no_candidates = [w for w, m in generic
                     if m.indicator_id is None and not m.candidates and w in contained]
    check("a bare generic word never resolves through a fuzzy step", not bad_generic,
          str(bad_generic))
    check("a bare generic word comes back with the candidates that contain it",
          not no_candidates, str(no_candidates))
    check("a generic word no indicator contains comes back empty-handed, which is honest",
          not match_indicator("profile").candidates)
    check("'trend' names supertrend and the other trend indicators as candidates",
          "supertrend" in match_indicator("trend").candidates
          and "trend-strength-index" in match_indicator("trend").candidates,
          str(match_indicator("trend").candidates))
    # Generic words that ARE registry ids still resolve exactly, as they must.
    check("exact ids that happen to be generic words still resolve",
          resolve_indicator_id("volume") == "volume"
          and resolve_indicator_id("momentum") == "momentum"
          and resolve_indicator_id("median") == "median")
    check("exact display names made of generic words still resolve",
          resolve_indicator_id("volatility stop") == "volatility-stop"
          and resolve_indicator_id("range analysis") == "range-analysis"
          and resolve_indicator_id("moving average channel") == "ma-channel")
    # The standard spoken forms the reviewer found missing.
    for phrase, want in (("average directional index", "adx"),
                         ("moving average convergence divergence", "macd"),
                         ("true range", "atr"), ("envelopes", "envelope"),
                         ("moving average", "sma")):
        check(f"{phrase!r} resolves to {want}", resolve_indicator_id(phrase) == want,
              str(resolve_indicator_id(phrase)))
    # Stopwords are dropped before deciding whether a run covers the phrase.
    for phrase in ("the rsi", "add rsi line", "show me bollinger bands on chart",
                   "donchian channels"):
        check(f"{phrase!r} still resolves through its covered words",
              resolve_indicator_id(phrase) in ("rsi", "bollinger", "donchian"),
              str(resolve_indicator_id(phrase)))
    anchored = match_indicator("anchored vwap")
    check("'anchored vwap' is VWAP or a refusal that names vwap, never something else",
          anchored.indicator_id == "vwap"
          or (anchored.indicator_id is None and "vwap" in anchored.candidates),
          f"{anchored.indicator_id} {anchored.candidates}")


def test_list_chart_indicators(kit: ChartTools) -> None:
    print("\n=== list_chart_indicators ===")
    listing = kit.get_async_functions()["list_chart_indicators"].entrypoint

    body = listing()
    payload = json.loads(body)
    data = payload["data"]
    check("an empty query still answers, with the whole catalogue",
          payload["ok"] and data["total"] == 102, str(data.get("total")))
    # 102 rows with their settings is 14,726 characters, so the whole catalogue can
    # only be returned as ids. Same trade-off, same flag, as list_indicators.
    check("the full catalogue drops detail rather than being truncated",
          data.get("detail_omitted") is True
          and sum(len(v) for v in data["by_category"].values()) == 102,
          str(sorted(data.get("by_category", {}))))
    check("the full listing stays under the 12,000 char cap",
          len(body) < MAX_TOOL_CHARS, f"{len(body)} chars of {MAX_TOOL_CHARS}")

    oversize = []
    for category in ("Trend", "Momentum", "Volatility", "Volume"):
        body = listing(category=category)
        rows = json.loads(body)["data"]["indicators"]
        if len(body) > MAX_TOOL_CHARS:
            oversize.append(f"{category}={len(body)}")
        if category == "Momentum":
            check("a category filter returns exactly that category, with settings",
                  len(rows) == 29 and all(r["category"] == "Momentum" for r in rows),
                  f"{len(rows)} rows")
            check("each row carries the settings add_chart_indicator needs",
                  any(r["id"] == "macd"
                      and "fastPeriod=12 [1..500]" in r["settings"] for r in rows),
                  str(next((r for r in rows if r["id"] == "macd"), {}))[:110])
    check("no category listing exceeds the cap", not oversize, str(oversize))
    check("the category filter is case-insensitive",
          json.loads(listing(category="trend"))["data"]["total"] == 36,
          str(json.loads(listing(category="trend"))["data"]["total"]))

    rows = json.loads(listing(query="bollinger"))["data"]["indicators"]
    check("a keyword query narrows to that family",
          sorted(r["id"] for r in rows)
          == ["bollinger", "bollinger-bandwidth", "bollinger-percent-b"],
          str([r["id"] for r in rows]))

    # A keyword that is a substring of nothing is usually an abbreviation, so the
    # search falls back to the same matcher add_chart_indicator uses.
    rows = json.loads(listing(query="willr"))["data"]["indicators"]
    check("an abbreviation query still finds its indicator, first",
          rows and rows[0]["id"] == "williams-percent-r",
          str([r["id"] for r in rows]))

    check("the detail threshold is the same one list_indicators uses",
          LIST_DETAIL_LIMIT == 45, str(LIST_DETAIL_LIMIT))

    try:
        listing(category="nonsense")
        check("an unknown category raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("an unknown category raises RetryAgentRun naming the valid ones",
              "Valid categories" in str(exc), str(exc)[:80])

    try:
        listing(query="moon phase")
        check("a query that matches nothing raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("a query that matches nothing raises RetryAgentRun",
              "No chart indicator matches" in str(exc), str(exc)[:80])


async def test_spoken_indicators(kit: ChartTools) -> None:
    print("\n=== spoken indicator names through the tool ===")
    functions = kit.get_async_functions()
    rc = context_for(LIVE_CONTEXT)

    for phrase in ("20 ema", "ema 20"):
        cmds, text, _ = await drive(functions["add_chart_indicator"], rc,
                                    indicator_id=phrase)
        check(f"{phrase!r} reaches the chart as an EMA of length 20",
              cmds == [{"op": "add_indicator", "indicatorId": "ema",
                        "settings": {"length": 20}}], str(cmds))
        print(f"        reply: {text}")

    for phrase, want in (("bbands", "bollinger"), ("stoch rsi", "stochastic-rsi"),
                         ("willr", "williams-percent-r"), ("kst", "know-sure-thing"),
                         ("keltner", "keltner-channel"), ("vix fix",
                                                          "williams-vix-fix")):
        cmds, _, _ = await drive(functions["add_chart_indicator"], rc,
                                 indicator_id=phrase)
        check(f"{phrase!r} draws {want}",
              cmds and cmds[0]["indicatorId"] == want, str(cmds))

    try:
        await drive(functions["add_chart_indicator"], rc, indicator_id="ma")
        check("an ambiguous name asks instead of drawing one at random", False,
              "no raise")
    except RetryAgentRun as exc:
        check("an ambiguous name asks instead of drawing one at random, and lists them",
              "ma-cross" in str(exc) and "matches 5" in str(exc), str(exc)[:110])

    try:
        await drive(functions["add_chart_indicator"], rc, indicator_id="bolinger")
        check("a misspelling names the closest ids it did find", False, "no raise")
    except RetryAgentRun as exc:
        check("a misspelling names the closest ids it did find",
              "Closest ids: bollinger" in str(exc), str(exc)[:110])

    # A generic word through the tool is a question with candidates, never a draw.
    try:
        await drive(functions["add_chart_indicator"], rc, indicator_id="trend")
        check("'trend' through the tool asks rather than drawing", False, "no raise")
    except RetryAgentRun as exc:
        check("'trend' through the tool asks rather than drawing, naming candidates",
              "supertrend" in str(exc) and "Ask the user" in str(exc), str(exc)[:110])
    try:
        await drive(functions["add_chart_indicator"], rc,
                    indicator_id="volume weighted average price")
        cmds, text, _ = [], "", []
    except RetryAgentRun as exc:
        cmds, text = [], str(exc)
    cmds, text, _ = await drive(functions["add_chart_indicator"], rc,
                                indicator_id="volume weighted average price")
    check("'volume weighted average price' through the tool draws VWAP, not Volume",
          cmds and cmds[0]["indicatorId"] == "vwap", str(cmds))


async def test_indicator_tool(kit: ChartTools) -> None:
    print("\n=== add_chart_indicator ===")
    functions = kit.get_async_functions()
    rc = context_for(LIVE_CONTEXT)

    cmds, text, order = await drive(functions["add_chart_indicator"], rc,
                                    indicator_id="supertrend",
                                    settings={"values": [3, 10]})
    check("add_chart_indicator emits the event first", order[:2] == ["event", "text"],
          str(order))
    check("supertrend 3,10 reaches the chart as period 10, multiplier 3",
          cmds == [{"op": "add_indicator", "indicatorId": "supertrend",
                    "settings": {"period": 10, "multiplier": 3}}], str(cmds))
    check("the reply NAMES the reading it used, so the user can correct it",
          "ATR Period 10" in text and "Multiplier 3" in text
          and "Read 3,10" in text, text)
    print(f"        reply: {text}")

    cmds, _, _ = await drive(functions["add_chart_indicator"], rc,
                             indicator_id="supertrend", settings={"values": "3,10"})
    check("the same numbers as a bare string work too",
          cmds and cmds[0]["settings"] == {"period": 10, "multiplier": 3}, str(cmds))

    cmds, text, _ = await drive(functions["add_chart_indicator"], rc,
                                indicator_id="rsi")
    check("no settings means the indicator's own defaults",
          cmds == [{"op": "add_indicator", "indicatorId": "rsi", "settings": {}}],
          str(cmds))

    # Beyond the first N slots: the bands, and a number with nowhere to go.
    cmds, text, _ = await drive(functions["add_chart_indicator"], rc,
                                indicator_id="rsi 30 70")
    check("'rsi 30 70' reaches the chart as the oversold and overbought bands",
          cmds and cmds[0]["settings"] == {"overbought": 70, "oversold": 30}, str(cmds))
    check("and the reply states that reading",
          "Overbought 70" in text and "Oversold 30" in text, text[:120])
    cmds, text, _ = await drive(functions["add_chart_indicator"], rc,
                                indicator_id="ema 200 50")
    check("'ema 200 50' adds the 200 EMA",
          cmds and cmds[0]["indicatorId"] == "ema" and cmds[0]["settings"] == {"length": 200},
          str(cmds))
    check("and says the 50 was not placed instead of dropping it",
          "50 was not placed" in text and "Length" in text, text)
    print(f"        reply: {text}")

    try:
        await drive(functions["add_chart_indicator"], rc, indicator_id="supertrend",
                    settings={"period": 900})
        check("an out-of-bounds setting raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("an out-of-bounds setting raises RetryAgentRun",
              "between 1 and 200" in str(exc), str(exc)[:80])

    try:
        await drive(functions["add_chart_indicator"], rc, indicator_id="supertrend",
                    settings={"lookback": 10})
        check("an unknown setting name raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("an unknown setting name raises RetryAgentRun",
              "no setting called" in str(exc), str(exc)[:70])

    try:
        await drive(functions["add_chart_indicator"], rc, indicator_id="moon phase")
        check("an unknown indicator raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("an unknown indicator raises RetryAgentRun",
              "no indicator called" in str(exc), str(exc)[:70])

    cmds, text, _ = await drive(functions["remove_chart_indicator"], rc,
                                indicator_id="Super Trend")
    check("remove_chart_indicator finds the live instance id",
          cmds == [{"op": "remove_indicator", "indicatorId": "supertrend",
                    "instanceId": "i1"}], str(cmds))

    cmds, text, _ = await drive(functions["remove_chart_indicator"], rc,
                                indicator_id="macd")
    check("removing something not on the chart says so instead of emitting",
          cmds == [] and "not on the chart" in text, text[:90])


# --- the empty chart --------------------------------------------------------


async def test_indicator_colours(kit: ChartTools) -> None:
    """Colours a user speaks must reach the chart as colours the engine can parse.

    The prompt that started this: "bollinger 20,2 fill light yellow" was refused
    with "shading is not supported by this chart indicator". Two things were wrong.
    The library's own bollinger declares no fill, which the frontend now overrides,
    and the tool had no word for shading and passed colour names through unresolved.
    A name reaching the engine paints nothing at all, silently, which is worse than
    refusing it.
    """
    print("\n=== indicator colours ===")
    rc = context_for(LIVE_CONTEXT)
    functions = kit.get_async_functions()

    cmds, text, _ = await drive(
        functions["add_chart_indicator"], rc,
        indicator_id="bollinger",
        settings={"length": 20, "stdDev": 2, "fill": "light yellow"},
    )
    applied = cmds[0]["settings"] if cmds else {}
    check("the refused prompt now reaches the chart",
          bool(cmds) and cmds[0]["op"] == "add_indicator", str(cmds)[:70])
    check("'fill' is aliased onto the indicator's own fill colour key",
          applied.get("fillColor") == "#ffff73", str(applied.get("fillColor")))
    # A plot's opacity runs 0 to 100 and defaults to 100. Pushing the "light" hint
    # into it made the band lines invisible, so the hue carries "light" on its own.
    check("light is carried by the hue, and plot opacity is left alone",
          "upper:opacity" not in applied and applied.get("fillColor") == "#ffff73",
          str({k: v for k, v in applied.items() if "opacity" in k or k == "fillColor"}))
    check("the numbers still land",
          applied.get("length") == 20 and applied.get("stdDev") == 2, str(applied)[:60])
    print(f"        reply: {text[:120]}")

    # Every colour input, on any indicator, not just the fill.
    for iid, key, spoken, want in (
        ("rsi", "color", "orange", "#ffa500"),
        ("supertrend", "upColor", "green", "#008000"),
        ("bollinger", "fillColor", "#ffff00", "#ffff00"),
    ):
        cmds, _, _ = await drive(functions["add_chart_indicator"], rc,
                                 indicator_id=iid, settings={key: spoken})
        got = (cmds[0]["settings"] if cmds else {}).get(key)
        check(f"{iid}.{key} resolves {spoken!r} to a hex the engine can parse",
              got == want, f"{spoken} -> {got}")

    # A name the engine cannot parse must never reach it.
    try:
        await drive(functions["add_chart_indicator"], rc,
                    indicator_id="bollinger", settings={"fill": "bananafish"})
        check("an unknown colour is refused, not forwarded", False, "no raise")
    except RetryAgentRun as exc:
        check("an unknown colour is refused, not forwarded",
              "bananafish" in str(exc) and "Known names" in str(exc), str(exc)[:70])


async def test_empty_context(kit: ChartTools) -> None:
    print("\n=== empty chart context ===")
    functions = kit.get_async_functions()
    for state in ({}, {"chart": {}}, {"chart": {"symbol": ""}}):
        rc = RunContext(run_id="r", session_id="s", session_state=dict(state))
        for name in ("draw_envelope", "draw_trendline", "draw_levels", "draw_zone",
                     "project_targets", "analyse_trend", "analyse_momentum",
                     "describe_chart"):
            try:
                cmds, text, _ = await drive(
                    functions[name], rc,
                    **({"from_price": 100.0, "to_price": 90.0}
                       if name in ("draw_zone",) else {}))
            except Exception as exc:  # noqa: BLE001
                check(f"{name} on an empty chart", False,
                      f"raised {type(exc).__name__}: {exc}")
                continue
            clean = (cmds == [] and text.startswith("No chart is open"))
            check(f"{name} on an empty chart says no chart is open, quietly",
                  clean, text[:60] if not clean else "")
        break  # one representative state is enough per tool; the rest are the same shape

    # And the other two states must not raise either.
    problems = []
    for state in ({"chart": {}}, {"chart": {"symbol": ""}}, {"chart": "nonsense"}):
        rc = RunContext(run_id="r", session_id="s", session_state=dict(state))
        try:
            _, text, _ = await drive(functions["describe_chart"], rc)
            if not text.startswith("No chart is open"):
                problems.append(f"{state} -> {text[:40]}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{state} raised {type(exc).__name__}")
    check("a malformed chart context is handled, not raised", not problems,
          "; ".join(problems))


# --- state and geometry through the tools, on synthetic frames --------------


async def test_synthetic_tools() -> None:
    """Everything that must be exact runs here, on frames whose shape is known."""
    print("\n=== state and geometry through the tools (synthetic frames) ===")
    frames = {
        "RELIANCE": synthetic_frame("range", 1400.0, seed=1),
        "SBIN": synthetic_frame("range", 800.0, seed=2),
        "TRI": synthetic_frame("triangle", 1400.0),
        "ONERAIL": synthetic_frame("onerail", 1400.0),
        "TINY": synthetic_frame("tiny"),
        "TREND": synthetic_frame("trend", 100.0),
        "FLAT": synthetic_frame("flat", 1400.0),
    }
    fake = FakeFrames(frames)
    kit = ChartTools(frames=fake)
    functions = kit.get_async_functions()
    reliance = frames["RELIANCE"].iloc[-300:]
    sbin = frames["SBIN"].iloc[-300:]

    # --- G2B: switch symbol and draw in the SAME turn --------------------------
    rc = context_for(context_from_frame("RELIANCE", reliance, bar_count=300))
    cmds, text, _ = await drive(functions["set_chart_symbol"], rc, symbol="SBIN", exchange="NSE")
    chart = rc.session_state["chart"]
    check("set_chart_symbol leaves SBIN in session state for the rest of the turn",
          chart.get("symbol") == "SBIN" and chart.get("lastTime") is None, str(chart)[:80])
    fake.calls.clear()
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    anchors = rail_anchors(cmds)
    check("a draw in the same turn fetches SBIN, not the symbol the browser last sent",
          fake.calls and fake.calls[-1][0] == "SBIN", str(fake.calls[-1:]))
    check("and the rails it draws sit on SBIN's prices, nowhere near RELIANCE's",
          len(anchors) == 4 and all(700 < a["price"] < 900 for a in anchors),
          str([round(a["price"], 1) for a in anchors]))
    check("the reply names SBIN", "SBIN" in text and "RELIANCE" not in text, text[:80])
    stored = rc.session_state["chart_patterns"]["envelope"]
    check("the stored record is keyed to SBIN", stored["symbol"] == "SBIN")

    # --- G2A: draw on RELIANCE, browser switches to SBIN, project_targets ------
    rc = context_for(context_from_frame("RELIANCE", reliance, bar_count=300))
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    check("the RELIANCE channel draws two rails", len(rail_anchors(cmds)) == 4, text[:80])
    state = rc.session_state
    state["chart"] = context_from_frame("SBIN", sbin, bar_count=300)
    later = RunContext(run_id="turn-2", session_id="test-session", session_state=state)
    cmds, text, _ = await drive(functions["project_targets"], later)
    check("project_targets refuses to measure a RELIANCE channel on an SBIN chart",
          cmds == [] and "does not apply" in text and "RELIANCE" in text and "SBIN" in text,
          text[:140])
    _, text, _ = await drive(functions["describe_chart"], later)
    check("describe_chart on SBIN does not list the RELIANCE envelope",
          "envelope" not in json.loads(text)["data"]["analyst_markup"],
          str(json.loads(text)["data"]["analyst_markup"]))
    state["chart"] = context_from_frame("RELIANCE", reliance, bar_count=300, interval="5m")
    cmds, text, _ = await drive(functions["project_targets"], later)
    check("an interval change is refused the same way",
          cmds == [] and "does not apply" in text and "5m" in text, text[:140])

    # --- G15: the browser says what is on the chart ------------------------------
    rc = context_for(context_from_frame("RELIANCE", reliance, bar_count=300))
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    draw_anchors = rail_anchors(cmds)
    state = rc.session_state
    state["chart"] = context_from_frame("RELIANCE", reliance, bar_count=300, analystGroups=[])
    cleared = RunContext(run_id="turn-3", session_id="test-session", session_state=state)
    cmds, text, _ = await drive(functions["project_targets"], cleared)
    check("after the user cleared the chart, project_targets says the channel is gone",
          cmds == [] and "no longer on the chart" in text, text[:120])
    _, text, _ = await drive(functions["describe_chart"], cleared)
    check("and describe_chart reports no analyst markup",
          json.loads(text)["data"]["analyst_markup"] == [],
          str(json.loads(text)["data"]["analyst_markup"]))
    state["chart"] = context_from_frame("RELIANCE", reliance, bar_count=300,
                                        analystGroups=["levels"])
    cmds, text, _ = await drive(functions["project_targets"], cleared)
    check("a group list that names other layers but not the envelope still refuses",
          cmds == [] and "no longer on the chart" in text)
    state["chart"] = context_from_frame("RELIANCE", reliance, bar_count=300,
                                        analystGroups=["envelope"])
    cmds, text, _ = await drive(functions["project_targets"], cleared)
    check("with the envelope reported on screen, project_targets works",
          cmds and cmds[0]["group"] == "targets", text[:100])

    # --- G5: the breakout narrated is the rail drawn -------------------------------
    record = state["chart_patterns"]["envelope"]
    measured = G.measured_move(_restore_channel(record))
    upper_edge = draw_anchors[1]
    lower_edge = draw_anchors[3]
    check("project_targets' breakout equals the upper rail's right-edge price to 1e-6",
          abs(measured["breakout"] - upper_edge["price"]) < 1e-6,
          f"{measured['breakout']:.6f} vs {upper_edge['price']:.6f}")
    check("and the breakdown equals the lower rail's",
          abs(measured["breakdown"] - lower_edge["price"]) < 1e-6)
    levels = [s for s in cmds[0]["shapes"] if s.get("kind") == "level"]
    upside = next((s for s in levels if s["label"].startswith("upside")), None)
    check("the target rays start at the right edge of the drawn rails",
          upside is not None and abs(upside["time"] - upper_edge["time"]) < 1e-6
          and upside.get("ray") is True,
          str({k: upside.get(k) for k in ("time", "ray")} if upside else None))
    width = upper_edge["price"] - lower_edge["price"]
    check("the upside target is one channel height above the drawn upper rail",
          upside is not None and abs(upside["price"] - (upper_edge["price"] + width)) < 1e-6)
    check("the record carries the rails themselves, not just the pivots",
          isinstance(record.get("upper"), dict) and "slope" in record["upper"]
          and record["upper"]["to"]["price"] == upper_edge["price"])
    check("the record is JSON-safe", isinstance(json.dumps(state["chart_patterns"]), str))

    # --- G4: rails that cross -------------------------------------------------------
    tri = frames["TRI"].iloc[-300:]
    rc = context_for(context_from_frame("TRI", tri, bar_count=300))
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    anchors = rail_anchors(cmds)
    print(f"        triangle reply: {text}")
    check("a resolved triangle still draws its two rails", len(anchors) == 4, str(len(anchors)))
    check("its upper rail ends at or below its lower rail",
          len(anchors) == 4 and anchors[1]["price"] <= anchors[3]["price"],
          f"{anchors[1]['price']:.2f} vs {anchors[3]['price']:.2f}" if len(anchors) == 4 else "")
    check("the reply says plainly that the rails cross and this is not a channel",
          "rails cross inside the window" in text and "not a channel" in text
          and not text.startswith("Drew a "), text[:120])
    cmds, text, _ = await drive(functions["project_targets"], rc)
    check("project_targets refuses to measure crossed rails",
          cmds == [] and "already resolved" in text, text[:120])

    # --- G17: one rail ---------------------------------------------------------------
    one = frames["ONERAIL"].iloc[-300:]
    rc = context_for(context_from_frame("ONERAIL", one, bar_count=300))
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    check("a frame with swings on one side only draws one trendline",
          len(rail_anchors(cmds)) == 2, str(len(rail_anchors(cmds))))
    check("and says so instead of calling it a channel",
          text.startswith("Drew one trendline") and "too few swings" in text, text[:100])

    # --- G18: a downside target below zero -------------------------------------------
    tiny = frames["TINY"].iloc[-300:]
    rc = context_for(context_from_frame("TINY", tiny, bar_count=300))
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    check("the tiny band draws a channel", len(rail_anchors(cmds)) == 4, text[:80])
    cmds, text, _ = await drive(functions["project_targets"], rc)
    labels = [s.get("label", "") for s in (cmds[0]["shapes"] if cmds else [])]
    check("a downside target at or below zero is not drawn",
          cmds and not any(lbl.startswith("downside") for lbl in labels), str(labels))
    check("and the reply says it was not drawn, rather than dropping it in silence",
          "downside target" in text and "not drawn" in text, text[:160])

    # --- G6: slope units ------------------------------------------------------------
    trend = frames["TREND"].iloc[-300:]
    rc = context_for(context_from_frame("TREND", trend, bar_count=300))
    _, text, _ = await drive(functions["analyse_trend"], rc)
    data = json.loads(text)["data"]
    check("analyse_trend reports the bar spacing", data.get("bar_seconds") == 86400.0,
          str(data.get("bar_seconds")))
    check("a 0.5 per day rally reads as a slope of 0.5 per bar, not 0.0",
          abs((data.get("slope_per_bar") or 0.0) - 0.5) < 0.1
          and abs((data.get("slope_high_per_bar") or 0.0) - 0.5) < 0.1,
          f"slope_per_bar={data.get('slope_per_bar')} high={data.get('slope_high_per_bar')} "
          f"low={data.get('slope_low_per_bar')}")
    check("the direction is still up", data.get("direction") == "up", str(data.get("direction")))

    # --- G13: the message when nothing fits ------------------------------------------
    flat = frames["FLAT"].iloc[-300:]
    rc = context_for(context_from_frame("FLAT", flat, bar_count=300))
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    check("a flat frame draws nothing and says to zoom out or omit the prices",
          cmds == [] and "Zoom out to include more bars" in text and "lookback" not in text,
          text[:120])
    cmds, text, _ = await drive(functions["draw_trendline"], rc, lookback=300)
    check("draw_trendline says the same",
          cmds == [] and "Zoom out to include more bars" in text and "lookback" not in text,
          text[:120])

    # --- viewport clipping through the tool layer -------------------------------------
    times = G.to_utc_seconds(reliance.index)
    rc = context_for(context_from_frame("RELIANCE", reliance, visible=(100, 220), bar_count=300))
    cmds, text, _ = await drive(functions["draw_envelope"], rc, lookback=300)
    record = rc.session_state["chart_patterns"]["envelope"]
    pivot_times = [p["time"] for p in record["highs"] + record["lows"]]
    check("with a real viewport, every pivot the envelope used is inside it",
          pivot_times and all(times[100] <= pt <= times[220] for pt in pivot_times),
          f"{len(pivot_times)} pivots")
    anchors = rail_anchors(cmds)
    check("both rails start inside the viewport and end at its right edge",
          len(anchors) == 4 and all(times[100] <= a["time"] <= times[220] for a in anchors)
          and anchors[1]["time"] == times[220] and anchors[3]["time"] == times[220])
    _, text, _ = await drive(functions["analyse_trend"], rc)
    data = json.loads(text)["data"]
    full_data = json.loads((await drive(functions["analyse_trend"],
                                        context_for(context_from_frame("RELIANCE", reliance,
                                                                       bar_count=300))))[1])["data"]
    check("analyse_trend on the viewport sees fewer swings than on the whole frame",
          len(data["swing_highs"]) < len(full_data["swing_highs"]),
          f"{len(data['swing_highs'])} vs {len(full_data['swing_highs'])}")

    # --- G7 and G9: named prices, the missing price, and lookback insensitivity ------
    # A descending line needs swing highs BETWEEN the two named prices, so the
    # low named is the last one in the window, not the trough right after the peak.
    hs, ls = G.swing_pivots(reliance, 2, 2, 0.0, float(times[100]), float(times[220]))
    high = max(hs, key=lambda p: p.price)
    low_after = ls[-1] if ls and ls[-1].time > high.time and sum(
        1 for p in hs if high.time <= p.time <= ls[-1].time) >= 2 else None
    check("the synthetic band has a swing high followed by more highs and a later low to name",
          low_after is not None)
    if low_after is not None:
        rc = context_for(context_from_frame("RELIANCE", reliance, visible=(100, 220), bar_count=300))
        cmds, text, _ = await drive(functions["draw_trendline"], rc,
                                    from_price=round(high.price), to_price=round(low_after.price))
        shape = cmds[0]["shapes"][0] if cmds else {}
        print(f"        descending trendline reply: {text}")
        check("a descending pair snaps its start to the swing high and its end to the swing low",
              cmds and "snapped to the swing high" in text and "snapped to the swing low" in text,
              text[:160])
        # The docstring's promise: fitted across the swing highs BETWEEN the named
        # prices. The best line by touches need not pass through the named high
        # itself, but it may not leave the span the user named.
        check("the line runs through the swing highs inside the span the user named",
              shape and "through the swing highs" in text
              and high.time - 1e-6 <= shape["from"]["time"] <= low_after.time + 1e-6
              and shape["to"]["time"] <= low_after.time + 1e-6
              and shape["from"]["price"] >= shape["to"]["price"] - 0.01 * high.price,
              str(shape.get("from")))
        cmds600, _, _ = await drive(functions["draw_trendline"], rc,
                                    from_price=round(high.price), to_price=round(low_after.price),
                                    lookback=600)
        check("the same named prices at lookback 600 draw the same line as at 300",
              json.dumps(cmds, sort_keys=True) == json.dumps(cmds600, sort_keys=True))
        check("the 600 bar fetch really was larger",
              fake.calls[-1][3] == 600 and fake.calls[-2][3] == 300, str(fake.calls[-2:]))
        env_a, _, _ = await drive(functions["draw_envelope"], rc,
                                  from_price=round(high.price), to_price=round(low_after.price))
        env_b, _, _ = await drive(functions["draw_envelope"], rc,
                                  from_price=round(high.price), to_price=round(low_after.price),
                                  lookback=600)
        check("draw_envelope with named prices is lookback-insensitive too",
              json.dumps(env_a, sort_keys=True) == json.dumps(env_b, sort_keys=True))
    try:
        await drive(functions["draw_trendline"], rc, from_price=1.0, to_price=2.0)
        check("draw_trendline with an impossible price raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("draw_trendline with an impossible price raises RetryAgentRun naming the "
              "visible range and how to fix it",
              "visible" in str(exc) and "zoom out" in str(exc).lower() and "ranged" in str(exc),
              str(exc)[:140])

    # --- G12: levels read against one reference price -------------------------------
    rc = context_for(context_from_frame("RELIANCE", reliance, bar_count=300))
    cmds, text, _ = await drive(functions["draw_levels"], rc, count=4)
    shapes = cmds[0]["shapes"] if cmds else []
    reference = float(reliance["close"].iloc[-1])
    consistent = all(
        (s["label"].startswith("resistance") and s["tone"] == "bearish" and s["price"] >= reference)
        or (s["label"].startswith("support") and s["tone"] == "bullish" and s["price"] < reference)
        for s in shapes)
    check("every level's role, tone and side of price agree", shapes and consistent,
          str([(s["label"], s["tone"]) for s in shapes]))
    check("the reply says which swings each level came from", "from swing" in text, text[:120])


# --- live -------------------------------------------------------------------


async def test_live(kit: ChartTools) -> None:
    print("\n=== live chart work on real candles ===")
    client = get_client()
    if not client.settings.openalgo_api_key or not client.ping().get("ok"):
        skip("live chart tools", "OpenAlgo unreachable")
        return

    import app.tools.charts as charts_module
    if charts_module.G is None:
        skip("live chart tools", "app/charts/geometry.py is not on disk yet")
        return

    functions = kit.get_async_functions()
    rc = context_for(LIVE_CONTEXT)

    _, text, _ = await drive(functions["describe_chart"], rc)
    payload = json.loads(text)
    check("describe_chart reports the live context",
          payload["ok"] and payload["data"]["symbol"] == "RELIANCE",
          payload["data"]["summary"][:80])

    _, text, _ = await drive(functions["set_chart_interval"], rc, interval="D")
    check("set_chart_interval accepts an interval the broker really has",
          "interval set to" in text, text[:60])
    try:
        await drive(functions["set_chart_interval"], rc, interval="7m")
        check("an unsupported interval raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("an unsupported interval raises RetryAgentRun",
              "does not offer" in str(exc), str(exc)[:80])
    # The interval call above cleared the range; the browser would resend it.
    rc = context_for(LIVE_CONTEXT)

    # The headline case: a real envelope on real RELIANCE daily candles.
    cmds, text, order = await drive(functions["draw_envelope"], rc,
                                    lookback=300, label="daily swing")
    check("draw_envelope yields the event before the prose",
          order[:2] == ["event", "text"], str(order))
    ok_shape = bool(cmds) and cmds[0]["op"] == "draw" and cmds[0]["group"] == "envelope"
    shapes = cmds[0]["shapes"] if ok_shape else []
    # A channel is TWO STRAIGHT LINES and nothing else. An earlier build emitted a
    # polyline through every pivot, which kinked back on itself and read as a blob
    # rather than a channel. This assertion is what stops that returning.
    check("a channel is exactly two straight trendlines",
          len(shapes) == 2 and all(s.get("kind") == "trendline" for s in shapes),
          f"{len(shapes)} shapes: {[s.get('kind') for s in shapes]}")
    check("neither rail is a polyline or an envelope",
          not any(s.get("kind") in ("envelope", "polyline") for s in shapes),
          str([s.get("kind") for s in shapes]))
    anchors = [s[k] for s in shapes for k in ("from", "to") if k in s]
    # Rails END together at the right edge so they read as one structure, but
    # each STARTS at its own first anchor pivot. Forcing a common start was what
    # extrapolated a support line backwards to fifty rupees above the candles.
    check("both rails end at the same right edge",
          len(anchors) == 4 and anchors[1]["time"] == anchors[3]["time"],
          str([a["time"] for a in anchors]))
    check("the upper rail sits above the lower rail at the right edge",
          len(anchors) == 4 and anchors[1]["price"] > anchors[3]["price"],
          f"{anchors[1]['price']:.2f} over {anchors[3]['price']:.2f}" if len(anchors) == 4 else "")
    # Containment is what a channel means. Over each rail's own span, no closed
    # bar may sit beyond it by more than the crossing slack.
    from app.openalgo.frames import get_frame_cache as _gfc
    _frame = _gfc().get_frame("RELIANCE", "NSE", "D", lookback_bars=300)["frame"]
    _t = G.to_utc_seconds(_frame.index)
    _hi = _frame["high"].to_numpy(dtype=float)
    _lo = _frame["low"].to_numpy(dtype=float)
    def _crossings(rail_from, rail_to, side):
        slope = (rail_to["price"] - rail_from["price"]) / (rail_to["time"] - rail_from["time"])
        line = rail_from["price"] + slope * (_t - rail_from["time"])
        span = (_t >= rail_from["time"]) & (_t <= rail_to["time"])
        span[-1] = False
        slack = np.abs(line) * G.CROSSING_TOLERANCE_PCT / 100.0
        bad = span & ((_hi > line + slack) if side == "high" else (_lo < line - slack))
        return int(bad.sum())
    if len(anchors) == 4:
        up_x = _crossings(anchors[0], anchors[1], "high")
        lo_x = _crossings(anchors[2], anchors[3], "low")
        check("no closed bar pokes above the upper rail over its span",
              up_x == 0, f"{up_x} bars above")
        check("no closed bar pokes below the lower rail over its span",
              lo_x == 0, f"{lo_x} bars below")
    # Determinism: the same chart must draw the same channel twice.
    cmds2, _, _ = await drive(functions["draw_envelope"], rc, lookback=300, label="daily swing")
    check("the same chart draws the same channel twice",
          json.dumps(cmds, sort_keys=True) == json.dumps(cmds2, sort_keys=True),
          "identical" if cmds == cmds2 else "DIFFERENT")
    check("every anchor is UTC seconds, not milliseconds and not a bar index",
          all(1_000_000_000 < a["time"] < 4_000_000_000 for a in anchors),
          str([a["time"] for a in anchors[:2]]))
    # A rail fitted to pivots bunched in part of the window used to be projected
    # across the whole of it, which put one rail at 1198 on a chart whose lowest
    # bar was 1267 and crossed the two lines. Neither rail may leave the data's own
    # range by more than the height of that range.
    from app.openalgo.frames import get_frame_cache
    res = get_frame_cache().get_frame("RELIANCE", "NSE", "D", lookback_bars=300)
    frame = res["frame"]
    lo, hi = float(frame["low"].min()), float(frame["high"].max())
    reach = hi - lo
    check("neither rail runs away from the data it describes",
          all(lo - reach <= a["price"] <= hi + reach for a in anchors),
          f"data {lo:.2f}-{hi:.2f}, rails "
          f"{min(a['price'] for a in anchors):.2f}-{max(a['price'] for a in anchors):.2f}"
          if anchors else "")
    # Nearness is judged where the rails end: the browser's window closes in June
    # 2026 here, so the reference is the last closed close before that edge, and
    # the bound is one window range capped at 20 percent of that close.
    if len(anchors) == 4:
        edge = anchors[1]["time"]
        ftimes = G.to_utc_seconds(frame.index)
        inside = frame[ftimes <= edge]
        edge_close = float(inside["close"].iloc[-2]) if len(inside) > 1 else last_close_fallback(frame)
        edge_range = float(inside["high"].iloc[:-1].max()) - float(inside["low"].iloc[:-1].min())
        bound = min(edge_range, 0.2 * edge_close)
        check("each rail ends near price: within one window range and 20 percent of the "
              "close at the window's edge",
              abs(anchors[1]["price"] - edge_close) <= bound
              and abs(anchors[3]["price"] - edge_close) <= bound,
              f"upper {anchors[1]['price']:.2f}, lower {anchors[3]['price']:.2f}, "
              f"close at edge {edge_close:.2f}, bound {bound:.2f}")
    shape = shapes[0] if shapes else {}
    check("the shape carries a tone, never a colour",
          shape.get("tone") in ("bullish", "bearish", "neutral")
          and not any("color" in k.lower() for k in shape),
          str(shape.get("tone")))
    check("the tool result the model reads stays far under the 12,000 char cap",
          len(text) < 400, f"{len(text)} chars of {MAX_TOOL_CHARS}")
    print(f"        reply: {text}")
    print("\n        emitted command JSON:")
    print("        " + json.dumps(cmds, indent=2).replace("\n", "\n        "))

    stored = (rc.session_state or {}).get("chart_patterns", {})
    check("the envelope geometry outlived the turn",
          "envelope" in stored and len(stored["envelope"]["highs"]) >= 2,
          str(sorted(stored)))
    check("what is stored is JSON-safe, so session state can be persisted",
          isinstance(json.dumps(stored), str))

    # project_targets works off the STORED geometry, in what is effectively a later
    # turn: a fresh RunContext carrying the same session state dict.
    later = RunContext(run_id="turn-2", session_id="test-session",
                       session_state=rc.session_state)
    cmds_t, text, _ = await drive(functions["project_targets"], later)
    check("project_targets measures the envelope drawn in an earlier turn",
          (cmds_t and cmds_t[0]["group"] == "targets") or "no target price" in text,
          text[:110])
    if cmds_t:
        print(f"        targets: {text}")
        measured = G.measured_move(_restore_channel(stored["envelope"]))
        check("the live breakout is the drawn upper rail's right-edge price to 1e-6",
              len(anchors) == 4 and abs(measured["breakout"] - anchors[1]["price"]) < 1e-6,
              f"{measured['breakout']:.6f} vs {anchors[1]['price']:.6f}" if len(anchors) == 4 else "")

    # A real interval switch drops the channel that was drawn on the daily chart.
    _, text, _ = await drive(functions["set_chart_interval"], later, interval="5m")
    check("switching interval drops the daily channel and says so",
          "envelope" not in (later.session_state.get("chart_patterns") or {})
          and "dropped" in text and later.session_state["chart"].get("interval") == "5m",
          text[:140])
    rc = context_for(LIVE_CONTEXT)

    # Named prices on real data. The window is set to bars that both a 300 and a
    # 600 bar fetch cover, so the snap pool is the same either way.
    times = G.to_utc_seconds(frame.index)
    window = dict(LIVE_CONTEXT, visibleFrom=float(times[-200]), visibleTo=float(times[-1]),
                  firstTime=float(times[0]), lastTime=float(times[-1]))
    hs, ls = G.swing_pivots(frame, 2, 2, 0.0, float(times[-200]), float(times[-1]))
    high = max(hs, key=lambda p: p.price) if hs else None
    # The far end of each pair is the LAST low after the high (descending) or the
    # FIRST low before it (ascending), so the fitted side has pivots in between.
    low_after = next((p for p in reversed(ls) if high is not None and p.time > high.time
                      and sum(1 for q in hs if high.time <= q.time <= p.time) >= 2), None)
    low_before = next((p for p in ls if high is not None and p.time < high.time
                       and sum(1 for q in ls if p.time <= q.time <= high.time) >= 2), None)
    if high is not None and low_after is not None:
        rcw = context_for(window)
        cmds, text, _ = await drive(functions["draw_trendline"], rcw,
                                    from_price=round(high.price), to_price=round(low_after.price))
        print(f"        live descending trendline: {text}")
        shape = cmds[0]["shapes"][0] if cmds else {}
        check("live: a descending pair from the window high to a later low draws a line "
              "through the highs",
              cmds and "through the swing highs" in text and "snapped to the swing high" in text
              and "snapped to the swing low" in text, text[:160])
        check("live: the line stays inside the span the user named and falls",
              shape and high.time - 1e-6 <= shape["from"]["time"]
              and shape["to"]["time"] <= low_after.time + 1e-6
              and shape["from"]["price"] > shape["to"]["price"],
              f"{shape.get('from')} to {shape.get('to')}")
        cmds600, _, _ = await drive(functions["draw_trendline"], rcw,
                                    from_price=round(high.price), to_price=round(low_after.price),
                                    lookback=600)
        check("live: the same named prices at lookback 600 draw the same line",
              json.dumps(cmds, sort_keys=True) == json.dumps(cmds600, sort_keys=True))
        record = rcw.session_state["chart_patterns"]["trendline"]
        check("live: both anchors of the stored trendline are inside the viewport",
              float(times[-200]) <= record["from"]["time"] <= float(times[-1])
              and float(times[-200]) <= record["to"]["time"] <= float(times[-1]))
    else:
        skip("live descending trendline", "no high followed by a low in the window")
    if high is not None and low_before is not None:
        rcw = context_for(window)
        cmds, text, _ = await drive(functions["draw_trendline"], rcw,
                                    from_price=round(low_before.price), to_price=round(high.price))
        print(f"        live ascending trendline: {text}")
        check("live: an ascending pair draws a line through the lows",
              cmds and "through the swing lows" in text, text[:120])
    try:
        await drive(functions["draw_trendline"], context_for(window), from_price=1.0, to_price=2.0)
        check("live: draw_trendline with an impossible price raises RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("live: draw_trendline with an impossible price raises RetryAgentRun",
              "visible" in str(exc) and "ranged" in str(exc), str(exc)[:120])
    # The viewport through the tool layer, on live data.
    rcw = context_for(window)
    cmds, text, _ = await drive(functions["draw_envelope"], rcw, lookback=300)
    record = rcw.session_state["chart_patterns"].get("envelope", {})
    pivot_times = [p["time"] for p in record.get("highs", []) + record.get("lows", [])]
    check("live: every pivot of a viewport-clipped envelope is inside the viewport",
          pivot_times and all(float(times[-200]) <= pt <= float(times[-1]) for pt in pivot_times),
          f"{len(pivot_times)} pivots")

    cmds, text, _ = await drive(functions["draw_levels"], rc, count=4)
    check("draw_levels emits horizontal levels",
          (cmds and all(s["kind"] == "level" for s in cmds[0]["shapes"]))
          or "nothing worth drawing" in text, text[:110])
    if cmds:
        print(f"        levels: {text}")
        shapes = cmds[0]["shapes"]
        reference = LIVE_CONTEXT["lastPrice"]
        check("live: each level's role and tone are read against the live last price",
              all((s["label"].startswith("resistance") and s["tone"] == "bearish"
                   and s["price"] >= reference)
                  or (s["label"].startswith("support") and s["tone"] == "bullish"
                      and s["price"] < reference) for s in shapes),
              str([(s["label"], s["tone"]) for s in shapes]))

    _, text, _ = await drive(functions["analyse_trend"], rc)
    payload = json.loads(text)
    check("analyse_trend answers with numbers, not adjectives",
          payload["ok"] and payload["data"]["swing_highs"]
          and payload["data"]["structure"],
          f"{payload['data']['direction']}, {payload['data']['structure']}")
    check("analyse_trend's slopes are per bar and not rounded to nothing",
          payload["data"]["bar_seconds"] == 86400.0
          and any(abs(payload["data"][k] or 0.0) > 0.01
                  for k in ("slope_high_per_bar", "slope_low_per_bar")),
          f"high {payload['data']['slope_high_per_bar']} low {payload['data']['slope_low_per_bar']}")

    _, text, _ = await drive(functions["analyse_momentum"], rc)
    payload = json.loads(text)
    check("analyse_momentum reads RSI, MACD and ADX through the dispatcher",
          payload["ok"] and set(payload["data"]["readings"]) == {"rsi", "macd", "adx"},
          payload["data"]["verdict"][:90])

    # A falsy tool return becomes an EMPTY tool message, which derails the model, and
    # an oversized one is truncated into invalid JSON. Every result is checked on real
    # data, which is the only place either could actually happen.
    empty: list[str] = []
    oversize: list[str] = []
    for name in ("describe_chart", "analyse_trend", "analyse_momentum",
                 "draw_envelope", "draw_trendline", "draw_levels"):
        _, body, _ = await drive(functions[name], rc)
        if not body:
            empty.append(name)
        if len(body) > MAX_TOOL_CHARS:
            oversize.append(f"{name}={len(body)}")
    check("no chart tool returned an empty payload", not empty, str(empty))
    check("no chart tool result exceeded the 12,000 char cap", not oversize,
          str(oversize))

    try:
        await drive(functions["draw_envelope"], rc, from_price=1.0, to_price=2.0)
        check("an impossible price raises an actionable RetryAgentRun", False, "no raise")
    except RetryAgentRun as exc:
        check("an impossible price raises an actionable RetryAgentRun",
              "ranged" in str(exc) and "zoom out" in str(exc).lower(), str(exc)[:100])


async def main_async() -> int:
    print("Chart toolkit tests")
    kit = test_registration()
    test_schemas(kit)
    test_confirmation_flags(kit)
    test_event_contract()
    test_context_contract()
    test_catalogue()
    test_resolver()
    test_list_chart_indicators(kit)
    await test_ordering_and_shapes(kit)
    await test_spoken_indicators(kit)
    await test_indicator_tool(kit)
    await test_indicator_colours(kit)
    await test_empty_context(kit)
    await test_synthetic_tools()
    await test_live(kit)

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    n_skip = sum(1 for _, s in results if s == SKIP)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    return 1 if n_fail else 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
