"""Agent construction.

Model notes verified against the live Baseten endpoint with agno 2.8.7 and
litellm 1.79.1:

  - litellm 1.79.1 has a first-class baseten provider. A model id that is not an
    8-character deployment code routes to https://inference.baseten.co/v1 and reads
    LITELLM_API_KEY. tools and tool_choice are both supported on that path.

  - DeepSeek V4 Flash is a REASONING model. It spends completion tokens on hidden
    reasoning before emitting any content. At max_tokens=16 the whole budget went to
    reasoning and content came back None with finish_reason="length" - an empty reply,
    not a truncated one. max_tokens is therefore load-bearing, not a cost dial.

  - Sampling parameters are NOT universal, so they are resolved per model rather than
    sent blindly. Baseten accepts temperature and top_p, so real values go through.
    OpenAI's gpt-5.6 family rejects top_p outright ("openai does not support
    parameters: ['top_p']") and accepts temperature only as 1 ("gpt-5 models don't
    support temperature=0.2"). Both are handled: unsupported parameters come from
    get_supported_openai_params, and value constraints from sampling_overrides, because
    no metadata call expresses "this parameter, but only this value".

  - gpt-5.6 also refuses to combine function tools with any reasoning effort except
    "none" on /v1/chat/completions. Since this agent always sends tools, effort is
    pinned to "none" for that family and a conflicting .env value is overridden.

Tool scoping is a callable factory rather than a fixed list. That is a safety layer as
well as a context saving: a session that has not enabled trading has no order tools in
its schema at all, so no amount of prompting can reach one.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.litellm import LiteLLM
from agno.run import RunContext

from .config import Settings, get_settings
from .model_providers import prepare_provider, sampling_overrides
from .tools.account import AccountTools
from .tools.indicators import IndicatorTools
from .tools.market import MarketDataTools
from .tools.symbols import SymbolTools
from .tools.system import SystemTools

log = logging.getLogger(__name__)

# The order rule lives in the DESCRIPTION as well as the instructions because position
# matters. Agno puts the description at the very top of the system message, and a rule
# buried tenth in a list of twenty is followed only most of the time: gpt-5.6-luna was
# measured pausing on 2 of 3 order requests and writing an order table in prose on the
# third. Prose instead of a tool call means the user never sees an approval card, which
# looks exactly like a broken confirmation gate.
DESCRIPTION = (
    "You are a trading assistant operating a live OpenAlgo brokerage connection for "
    "Indian markets. When the user tells you to place, modify or cancel an order, you "
    "CALL the matching tool. You never write out an order as a table or a proposal and "
    "stop there: the interface, not your reply, is what asks the user to approve it, and "
    "nothing reaches the broker until they do. Describing an order instead of calling "
    "the tool means the user is never asked at all."
)

# The chart rules, shared by the full and lean prompts. The chart toolkit is
# scoped per run, so whichever prompt is active has to carry these.
CHART_INSTRUCTIONS = [
    "When a chart is open you are a technical analyst working on it. Its symbol, "
    "exchange, interval and visible range are given to you: never ask the user which "
    "instrument or timeframe they mean, and never make them repeat it.",
    "You do not decide where a line goes. The drawing tools compute the geometry from "
    "the bars and hand back exact anchors; you choose what to draw and describe what "
    "came back. Never write a swing high, a level or a target you did not get from a "
    "tool.",
    "Name the instrument and timeframe back in your answer, and quote the anchor prices "
    "the tool returned. That is how the user catches an answer about the wrong chart.",
    "Keep a markup reply to a short heading and two or three labelled lines. It is a "
    "caption for something the user can already see, not an essay.",
    "A setting written as two numbers can be ambiguous. Apply the usual convention, "
    "then say which reading you used so a wrong guess costs one short correction.",
]

INSTRUCTIONS = [
    # symbology
    "Symbols follow OpenAlgo's format. Equity is the bare base symbol (RELIANCE, SBIN). "
    "Futures are BASE+DDMMMYY+FUT (NIFTY25AUG26FUT). Options are BASE+DDMMMYY+STRIKE+CE "
    "or PE (NIFTY25AUG2625950CE). When you are not certain of a symbol, call "
    "search_symbols rather than constructing one.",
    "Indices live on NSE_INDEX, BSE_INDEX, MCX_INDEX and GLOBAL_INDEX and are quote-only. "
    "They cannot be traded. To trade an index view, use its future or option.",
    "Index symbols carry NO spaces and no '50' suffix. The ones you will need: on "
    "NSE_INDEX use NIFTY (not 'NIFTY 50'), BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 "
    "and INDIAVIX; on BSE_INDEX use SENSEX and BANKEX. Sector indices follow the same "
    "pattern, for example NIFTYIT and NIFTYPHARMA.",
    "If a quote or lookup fails, read the error: it lists the closest real symbols. Use "
    "one of them rather than guessing another spelling, and never give up on a question "
    "because one symbol lookup failed.",
    "For an OPTION contract never use search_symbols and never build the symbol by hand. "
    "Call resolve_option_symbol with the underlying, expiry and an offset such as ATM, "
    "ITM2 or OTM3, which returns the exact symbol with its lot size. For a straddle "
    "resolve the ATM call and the ATM put. To see many strikes at once call "
    "get_option_chain, which also returns Greeks.",

    # constants
    "Valid exchanges: NSE, BSE, NFO, BFO, CDS, BCD, MCX, NCDEX, NCO. Products: MIS "
    "(intraday), CNC (delivery equity), NRML (derivatives). Price types: MARKET, LIMIT, "
    "SL, SL-M. Actions: BUY, SELL. Never invent a value outside these lists.",

    # data discipline
    "Quote before you size. Get the current price with get_quote before proposing any "
    "order, and state that price in your proposal.",
    "Candle intervals are broker-specific. Call get_supported_intervals before assuming "
    "an interval exists; this broker has no weekly or monthly candles.",
    "Indicator names come from list_indicators, never from memory. Call "
    "describe_indicator when you are unsure of an indicator's parameters or outputs. "
    "Use compute_indicators_batch when the user asks for several indicators on one "
    "symbol, so the candles are fetched once.",

    # order discipline
    "Before proposing an order, state symbol, exchange, action, quantity, product, "
    "price type and the current last traded price. For derivatives also state the lot "
    "size from get_symbol_info, because quantity must be a multiple of it.",
    "When the user gives a complete order instruction, CALL the order tool. Do not ask "
    "for confirmation in your reply and do not describe the order instead of placing it. "
    "The interface shows the user an approval card and nothing reaches the broker until "
    "they approve it, so calling the tool is how you present an order for approval.",
    "Only ask a question first when a required detail is genuinely missing or ambiguous, "
    "such as an unknown symbol, or a quantity that is not a multiple of the lot size.",
    "Never place an order the user did not ask for, and never place a second order "
    "without a fresh instruction.",
    "If an order is rejected by the user or refused by the risk guard, do not re-propose "
    "the identical order. Address the stated reason, or ask what the user wants changed.",
    "close_all_positions squares off every open position at market. Treat it as the most "
    "dangerous action available and confirm the user means all positions, not one.",

    # mode awareness
    "Analyzer mode simulates orders; live mode sends real money to the broker. Always "
    "say which mode an order was placed in, or would be placed in. Never present a "
    "simulated fill as a real one.",

    # presentation
    "Answer in markdown. Use a table whenever there is more than one row of data - "
    "positions, orders, option chains and indicator comparisons are all tables.",
    "Quote money with its currency and use Indian digit grouping. Report P&L with its "
    "sign. Times are Asia/Kolkata.",
    "If a tool returns no data, say so plainly. Never invent a price, a position, or an "
    "order id.",

    *CHART_INSTRUCTIONS,

    # boundary
    "You carry out instructions and provide analysis. You do not give personalised "
    "investment advice and you do not tell the user what they should buy or sell.",
]


# The lean profile. Small local models cannot choose reliably among 26 tools, so this
# keeps only the ones that answer the great majority of questions. Names are checked by
# agno's include_tools, which RAISES on a typo, so a rename here fails loudly.
LEAN_READ_ONLY: dict[str, list[str]] = {
    "market": ["get_quote", "get_history", "get_market_depth"],
    "symbols": ["search_symbols"],
    "account": ["get_funds", "get_positionbook", "get_holdings", "get_orderbook"],
    "indicators": ["compute_indicator", "list_indicators"],
    "system": ["get_analyzer_status"],
}
LEAN_ORDERS: list[str] = ["place_order", "modify_order", "cancel_order", "get_order_status"]

# OptionsTools mixes read-only analytics with two order tools. The analytics half must be
# available WITHOUT trading enabled: without resolve_option_symbol the agent cannot name
# an option contract at all, and was reduced to guessing from search results.
OPTIONS_READ_ONLY: list[str] = ["resolve_option_symbol", "get_option_chain",
                                "get_option_greeks", "get_synthetic_future"]
OPTIONS_ORDERS: list[str] = ["place_options_order", "place_options_multi_order"]


def _options_read_toolkit(client=None):
    """The analytics half of OptionsTools, safe without trading enabled."""
    try:
        from .tools.options import OptionsTools
        return OptionsTools(client=client, include_tools=OPTIONS_READ_ONLY)
    except ImportError:
        log.warning("options tools unavailable")
        return None


def _chart_toolkit(client=None, frames=None):
    """The chart analyst's tools, loaded only for a request that has a chart.

    Scoped the same way order tools are, and for the same reason: a tool the model
    cannot use is a tool it can still pick wrongly. The chat page has no chart to
    act on, so it never sees these fifteen.
    """
    try:
        from .tools.charts import ChartTools
        return ChartTools(client=client, frames=frames)
    except ImportError:
        log.warning("chart tools unavailable")
        return None


def _read_only_toolkits(client=None, frames=None, lean: bool = False) -> list:
    if not lean:
        kits = [
            MarketDataTools(client=client, frames=frames),
            SymbolTools(client=client),
            AccountTools(client=client),
            IndicatorTools(client=client, frames=frames),
            SystemTools(client=client),
        ]
        options = _options_read_toolkit(client)
        if options is not None:
            kits.append(options)
        return kits
    return [
        MarketDataTools(client=client, frames=frames,
                        include_tools=LEAN_READ_ONLY["market"]),
        SymbolTools(client=client, include_tools=LEAN_READ_ONLY["symbols"]),
        AccountTools(client=client, include_tools=LEAN_READ_ONLY["account"]),
        IndicatorTools(client=client, frames=frames,
                       include_tools=LEAN_READ_ONLY["indicators"]),
        SystemTools(client=client, include_tools=LEAN_READ_ONLY["system"]),
    ]


def _order_toolkits(client=None, lean: bool = False) -> list:
    """Imported lazily so a partial checkout still runs the read-only agent.

    In lean mode only the four core order tools are exposed. The gated set is
    unchanged - every one of them still requires confirmation.
    """
    kits: list = []
    try:
        from .tools.orders import OrderTools
        kits.append(OrderTools(client=client,
                               **({"include_tools": LEAN_ORDERS} if lean else {})))
    except ImportError:
        log.warning("order tools unavailable")
    if lean:
        return kits
    try:
        from .tools.gtt import GttTools
        kits.append(GttTools(client=client))
    except ImportError:
        log.warning("gtt tools unavailable")
    try:
        from .tools.options import OptionsTools
        # Only the order half here; the analytics half is already in the read-only set.
        kits.append(OptionsTools(client=client, include_tools=OPTIONS_ORDERS))
    except ImportError:
        log.warning("options tools unavailable")
    return kits


def build_tool_factory(settings: Settings):
    """Return a callable agno resolves at the start of every run.

    Order tools are added only when the session has trading enabled AND the environment
    allows it. This keeps the default schema at 26 tools and means an unprivileged
    session cannot reach an order tool at all.
    """

    lean = settings.effective_tool_profile == "lean"

    def get_tools(run_context: RunContext) -> list:
        kits = _read_only_toolkits(lean=lean)
        state = getattr(run_context, "session_state", None) or {}
        # The charts page sends its chart with every turn. Its absence is what
        # tells us this is the chat page, where there is nothing to draw on.
        if state.get("chart"):
            chart = _chart_toolkit()
            if chart is not None:
                kits.append(chart)
            # The charts page is read-only, and that has to be true at THIS layer,
            # not just in the prompt. Order tools were being registered here
            # because the page never sends trading_enabled and so inherited the
            # environment default. A model that then called place_order would
            # pause the run for an approval the charts panel cannot give, and
            # the run sat paused forever. With no order tool in the schema there
            # is nothing to pause on.
            return kits
        session_allows = bool(state.get("trading_enabled", settings.trading_enabled))
        if settings.trading_enabled and session_allows:
            kits.extend(_order_toolkits(lean=lean))
        return kits

    return get_tools


def _supported_params(model_id: str) -> set[str] | None:
    """What OpenAI-style parameters this model accepts, or None if unknown."""
    try:
        import litellm
        params = litellm.get_supported_openai_params(model=model_id)
        return set(params) if params else None
    except Exception:  # noqa: BLE001
        return None


def build_model(settings: Settings) -> LiteLLM:
    """Build the model from whatever provider LITELLM_MODEL names.

    LiteLLM is here for portability: the same agent runs against Baseten, OpenAI,
    Anthropic, Gemini, Groq, OpenRouter or a local Ollama model, decided entirely by
    the prefix on LITELLM_MODEL. Two details make that work:

      - A local provider gets api_key=None. Passing an empty string instead makes some
        LiteLLM paths send an Authorization header, which local servers do not expect.
      - api_base is only passed when set, so LiteLLM resolves each provider's own
        default endpoint (including http://localhost:11434 for Ollama).

    Tool calling is not optional for this agent, so a local model must be one that
    supports tools. `ollama list` shows the capability; gemma4:e4b and llama3.1 do.
    """
    # LiteLLM's static table does not know most Ollama tags and would fall back to a
    # broken JSON-emulation path. Ask Ollama what the model can actually do first.
    prepare_provider(settings.litellm_model, settings.resolve_model_api_base())

    request_params: dict[str, Any] = {}
    # stream_options is an OpenAI-style extra. Ollama rejects unknown fields, so it is
    # only sent to hosted providers.
    if settings.litellm_include_usage and not settings.is_local_model:
        request_params["stream_options"] = {"include_usage": True}

    effort = settings.resolve_reasoning_effort()
    # Only send it if the provider actually accepts it. Baseten, for one, rejects
    # reasoning_effort outright with
    #   UnsupportedParamsError: baseten does not support parameters: ['reasoning_effort']
    # which fails EVERY request rather than tuning anything.
    if effort and not settings.reasoning_capability().get("controllable"):
        log.warning(
            "LITELLM_REASONING_EFFORT=%s is set, but %s does not accept reasoning_effort. "
            "Ignoring it; sending it would fail every request.",
            effort, settings.litellm_model)
        effort = None
    if effort:
        # request_params is merged last by agno, so this reaches every sync, async and
        # streaming call.
        request_params["reasoning_effort"] = effort
        if settings.is_local_model:
            # LiteLLM turns this into Ollama's boolean `think` flag.
            log.info("reasoning_effort=%s -> thinking %s", effort,
                     "OFF" if effort == "none" else "ON")
        else:
            log.info("reasoning_effort=%s", effort)
            # Measured on DeepSeek V4 Flash: effort=high spent 1498 reasoning tokens and
            # returned EMPTY content at max_tokens=1500. Reasoning is billed out of the
            # same budget as the answer.
            if effort in ("medium", "high") and settings.litellm_max_tokens < 4096:
                log.warning(
                    "LITELLM_REASONING_EFFORT=%s with LITELLM_MAX_TOKENS=%d is risky: on a "
                    "reasoning model the hidden reasoning is billed from the same budget "
                    "as the reply, and can consume all of it, leaving an empty answer. "
                    "Raise LITELLM_MAX_TOKENS to 4096 or more.",
                    effort, settings.litellm_max_tokens)

    kwargs: dict[str, Any] = {
        "id": settings.litellm_model,
        # For a reasoning model a small cap returns EMPTY content, not short content.
        "max_tokens": settings.litellm_max_tokens,
        "request_params": request_params or None,
    }

    # Sampling parameters are not universal. OpenAI's gpt-5.6 family rejects top_p with
    #   UnsupportedParamsError: openai does not support parameters: ['top_p']
    # which fails every request. Ask LiteLLM what this model accepts and send only that;
    # agno omits a None from the payload, so None is how a parameter is withheld.
    supported = _supported_params(settings.litellm_model)
    # Some constraints are about the VALUE, not the parameter, and no metadata call
    # exposes them - gpt-5 accepts temperature but only the value 1.
    forced = sampling_overrides(settings.litellm_model)
    for name, value in (("temperature", settings.litellm_temperature),
                        ("top_p", settings.litellm_top_p)):
        if name in forced:
            kwargs[name] = forced[name]
            log.info("%s constrains %s; using %r", settings.litellm_model, name, forced[name])
        elif supported is None or name in supported:
            kwargs[name] = value
        else:
            kwargs[name] = None
            log.info("%s does not accept %s; withholding it", settings.litellm_model, name)

    api_key = settings.resolve_model_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    elif settings.is_local_model:
        # A local server needs no credential, but agno's __post_init__ falls back to
        # getenv("LITELLM_API_KEY") when api_key is None and logs a misleading ERROR if
        # that is unset. A placeholder stops the noise; local providers ignore it.
        kwargs["api_key"] = "local"
    api_base = settings.resolve_model_api_base()
    if api_base:
        kwargs["api_base"] = api_base

    log.info("model %s provider=%s local=%s",
             settings.litellm_model, settings.model_provider or "inferred",
             settings.is_local_model)
    return LiteLLM(**kwargs)


# A short instruction set for small models. The full list is 18 blocks, which crowds an
# 8B model's context alongside the tool schemas and measurably degrades tool selection.
LEAN_INSTRUCTIONS = [
    "Answer using your tools. Call a tool once, read the result, then answer from it.",
    "Do not call the same tool repeatedly with the same arguments. If a tool has already "
    "returned what you need, answer instead of calling it again.",
    "Only call tools for the instrument the user actually named. Never substitute a "
    "different symbol.",
    "Symbols are OpenAlgo format: equity is the bare symbol (SBIN, RELIANCE). If a symbol "
    "is not found, call search_symbols once.",
    "Products are MIS, CNC or NRML. Price types are MARKET, LIMIT, SL, SL-M.",
    "When the user gives a complete order instruction, call the order tool. The interface "
    "asks the user to approve it, so do not ask for confirmation in your reply.",
    "Say whether an order is simulated (analyze mode) or real (live mode).",
    "Answer in markdown, using a table when there is more than one row of data.",
    "Never invent a price, a position, or an order id.",
]


# When trading is off the order tools are not registered at all, so the model discovers
# it has no way to place an order and has to guess why. Telling it the actual reason
# turns an unhelpful "I don't seem to have that tool" into an answer the user can act on.
TRADING_DISABLED_NOTE = (
    "Order placement is switched off in this deployment: TRADING_ENABLED is false in the "
    "server environment, so no order tool is loaded and nothing you do can place, modify "
    "or cancel an order. This is a deliberate safety setting, not a fault and not "
    "something you can work around. You can still quote, analyse, compute indicators and "
    "size a trade. If the user asks you to place an order, prepare the full order "
    "details, then say plainly that order placement is disabled and that an operator must "
    "set TRADING_ENABLED=true and restart the backend to enable it."
)


def build_agent(settings: Settings | None = None) -> Agent:
    settings = settings or get_settings()
    lean = settings.effective_tool_profile == "lean"
    if lean:
        log.info("lean tool profile active for %s", settings.litellm_model)

    instructions = list(LEAN_INSTRUCTIONS if lean else INSTRUCTIONS)
    if lean:
        # The chart toolkit is scoped per run, so the lean prompt has to carry
        # the chart rules as well or a chart turn on a small model is told to act
        # only on a named instrument and refuses to draw.
        instructions.extend(CHART_INSTRUCTIONS)
    if not settings.trading_enabled:
        instructions.append(TRADING_DISABLED_NOTE)

    return Agent(
        id="openalgo-trading-agent",
        name="Trading Agent",
        model=build_model(settings),
        # db is REQUIRED for the confirmation gate: without it a paused run cannot be
        # resumed in a later HTTP request.
        db=SqliteDb(db_file=str(settings.db_path)),
        tools=build_tool_factory(settings),
        cache_callables=False,
        description=DESCRIPTION,
        instructions=instructions,
        expected_output=(
            "A direct answer. Tables for anything with more than one row. State the "
            "analyzer mode whenever an order is involved."
        ),
        markdown=True,
        add_datetime_to_context=True,
        timezone_identifier=settings.timezone,
        add_location_to_context=False,
        add_history_to_context=True,
        num_history_runs=2 if lean else 5,
        store_history_messages=False,
        store_tool_messages=True,
        # Full: higher than the equity sibling's 14, because a trading question
        # legitimately chains search, expiry, resolve, quote, margin and place.
        # Lean: a hard stop on the runaway loops small models fall into - gemma4:e4b
        # was observed making 16 calls on a single quote request.
        tool_call_limit=6 if lean else 20,
        max_tool_calls_from_history=3 if lean else 6,
        telemetry=False,
        store_events=False,
    )
