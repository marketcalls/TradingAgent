"""Chart toolkit tests: registration, schemas, the event pattern, and a live draw.

Four things here are worth more than the rest.

Schema generation, because agno builds the JSON schema from get_type_hints plus a parsed
docstring, so a missing Args: block silently yields a tool the model cannot call.

Confirmation flags, because a typo in requires_confirmation_tools only WARNS in agno
2.8.7. This page is read-only and must never pause for approval, so it is asserted.

The generator-tool contract, because the whole design rests on it: a tool yields a
ChartCommandEvent and then a plain string, the event reaches the UI before a word of
prose is written, and agno concatenates str() of every yielded event into the tool
result. That last part is why ChartCommandEvent.__str__ is empty, and it is asserted
rather than trusted.

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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import setup_logging  # noqa: E402

setup_logging("WARNING")

from agno.exceptions import RetryAgentRun  # noqa: E402
from agno.run.agent import CustomEvent, RunOutputEvent  # noqa: E402
from agno.run.base import RunContext  # noqa: E402
from typing import get_args  # noqa: E402

from app.openalgo.client import get_client  # noqa: E402
from app.openalgo.normalize import MAX_TOOL_CHARS  # noqa: E402
from app.tools.charts import (  # noqa: E402
    LIST_DETAIL_LIMIT,
    ChartCommandEvent,
    ChartTools,
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
        state["chart"] = chart
    return RunContext(run_id="test-run", session_id="test-session", session_state=state)


async def drive(fn, run_context: RunContext, **kwargs):
    """Call one tool and split what it produced into events and text.

    Returns:
        A (commands, text) pair: every command dict yielded on ChartCommandEvents, and
        the concatenated plain-string output the model would actually read.
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

    cmds, text, _ = await drive(functions["set_chart_symbol"], rc,
                                symbol="nifty 50", exchange="NSE")
    check("set_chart_symbol corrects a spoken index name",
          cmds == [{"op": "set_symbol", "symbol": "NIFTY", "exchange": "NSE_INDEX"}],
          str(cmds))

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
    settings, reading = assign_positional(indicators["supertrend"], [3, 10])
    check("supertrend 3,10 resolves to period 10 and multiplier 3",
          settings == {"period": 10, "multiplier": 3}, str(settings))
    check("the reading is stated in the indicator's own labels",
          reading == "ATR Period 10, Multiplier 3", reading)
    check("supertrend 10,3 reads the same way",
          assign_positional(indicators["supertrend"], [10, 3])[0] == settings)

    check("macd 12,26,9 stays in declaration order",
          list(assign_positional(indicators["macd"], [12, 26, 9])[0].values())
          == [12, 26, 9], str(assign_positional(indicators["macd"], [12, 26, 9])[0]))
    check("rsi 21 lands on Length, not on the oversold band",
          assign_positional(indicators["rsi"], [21])[0] == {"length": 21},
          str(assign_positional(indicators["rsi"], [21])[0]))


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
    settings, _ = assign_positional(catalogue["ema"],
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
    check("both rails span the same two times, so they read as one channel",
          len(anchors) == 4 and anchors[0]["time"] == anchors[2]["time"]
          and anchors[1]["time"] == anchors[3]["time"],
          str([a["time"] for a in anchors]))
    check("the upper rail sits above the lower rail at both ends",
          len(anchors) == 4 and anchors[0]["price"] > anchors[2]["price"]
          and anchors[1]["price"] > anchors[3]["price"],
          f"{anchors[0]['price']:.2f}/{anchors[2]['price']:.2f} then "
          f"{anchors[1]['price']:.2f}/{anchors[3]['price']:.2f}" if len(anchors) == 4 else "")
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
    cmds, text, _ = await drive(functions["project_targets"], later)
    check("project_targets measures the envelope drawn in an earlier turn",
          (cmds and cmds[0]["group"] == "targets") or "no target price" in text,
          text[:110])
    if cmds:
        print(f"        targets: {text}")

    cmds, text, _ = await drive(functions["draw_trendline"], rc, lookback=300)
    check("draw_trendline emits a trendline or explains why it cannot",
          (cmds and cmds[0]["shapes"][0]["kind"] == "trendline")
          or "nothing to draw a line through" in text, text[:110])
    if cmds:
        shape = cmds[0]["shapes"][0]
        check("trendline uses the 'from' alias and extendRight",
              "from" in shape and "extendRight" in shape, str(sorted(shape)))

    cmds, text, _ = await drive(functions["draw_levels"], rc, count=4)
    check("draw_levels emits horizontal levels",
          (cmds and all(s["kind"] == "level" for s in cmds[0]["shapes"]))
          or "nothing worth drawing" in text, text[:110])
    if cmds:
        print(f"        levels: {text}")

    _, text, _ = await drive(functions["analyse_trend"], rc)
    payload = json.loads(text)
    check("analyse_trend answers with numbers, not adjectives",
          payload["ok"] and payload["data"]["swing_highs"]
          and payload["data"]["structure"],
          f"{payload['data']['direction']}, {payload['data']['structure']}")

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
              "ranged" in str(exc), str(exc)[:100])


async def main_async() -> int:
    print("Chart toolkit tests")
    kit = test_registration()
    test_schemas(kit)
    test_confirmation_flags(kit)
    test_event_contract()
    test_catalogue()
    test_resolver()
    test_list_chart_indicators(kit)
    await test_ordering_and_shapes(kit)
    await test_spoken_indicators(kit)
    await test_indicator_tool(kit)
    await test_empty_context(kit)
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
