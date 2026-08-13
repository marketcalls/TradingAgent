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

  - Unlike the equity-research sibling, temperature and top_p do NOT need to be None
    here; Baseten accepts both, so real values are sent.

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
from .model_providers import prepare_provider
from .tools.account import AccountTools
from .tools.indicators import IndicatorTools
from .tools.market import MarketDataTools
from .tools.symbols import SymbolTools
from .tools.system import SystemTools

log = logging.getLogger(__name__)

DESCRIPTION = (
    "You are a trading assistant operating a live OpenAlgo brokerage connection for "
    "Indian markets."
)

INSTRUCTIONS = [
    # symbology
    "Symbols follow OpenAlgo's format. Equity is the bare base symbol (RELIANCE, SBIN). "
    "Futures are BASE+DDMMMYY+FUT (NIFTY25AUG26FUT). Options are BASE+DDMMMYY+STRIKE+CE "
    "or PE (NIFTY25AUG2625950CE). When you are not certain of a symbol, call "
    "search_symbols rather than constructing one.",
    "Indices live on NSE_INDEX, BSE_INDEX, MCX_INDEX and GLOBAL_INDEX and are quote-only. "
    "They cannot be traded. To trade an index view, use its future or option.",

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

    # boundary
    "You carry out instructions and provide analysis. You do not give personalised "
    "investment advice and you do not tell the user what they should buy or sell.",
]


def _read_only_toolkits(client=None, frames=None) -> list:
    return [
        MarketDataTools(client=client, frames=frames),
        SymbolTools(client=client),
        AccountTools(client=client),
        IndicatorTools(client=client, frames=frames),
        SystemTools(client=client),
    ]


def _order_toolkits(client=None) -> list:
    """Imported lazily so a partial checkout still runs the read-only agent."""
    kits: list = []
    try:
        from .tools.orders import OrderTools
        kits.append(OrderTools(client=client))
    except ImportError:
        log.warning("order tools unavailable")
    try:
        from .tools.gtt import GttTools
        kits.append(GttTools(client=client))
    except ImportError:
        log.warning("gtt tools unavailable")
    try:
        from .tools.options import OptionsTools
        kits.append(OptionsTools(client=client))
    except ImportError:
        log.warning("options tools unavailable")
    return kits


def build_tool_factory(settings: Settings):
    """Return a callable agno resolves at the start of every run.

    Order tools are added only when the session has trading enabled AND the environment
    allows it. This keeps the default schema at 26 tools and means an unprivileged
    session cannot reach an order tool at all.
    """

    def get_tools(run_context: RunContext) -> list:
        kits = _read_only_toolkits()
        state = getattr(run_context, "session_state", None) or {}
        session_allows = bool(state.get("trading_enabled", settings.trading_enabled))
        if settings.trading_enabled and session_allows:
            kits.extend(_order_toolkits())
        return kits

    return get_tools


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

    kwargs: dict[str, Any] = {
        "id": settings.litellm_model,
        "temperature": settings.litellm_temperature,
        "top_p": settings.litellm_top_p,
        # For a reasoning model a small cap returns EMPTY content, not short content.
        "max_tokens": settings.litellm_max_tokens,
        "request_params": request_params or None,
    }

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


def build_agent(settings: Settings | None = None) -> Agent:
    settings = settings or get_settings()

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
        instructions=INSTRUCTIONS,
        expected_output=(
            "A direct answer. Tables for anything with more than one row. State the "
            "analyzer mode whenever an order is involved."
        ),
        markdown=True,
        add_datetime_to_context=True,
        timezone_identifier=settings.timezone,
        add_location_to_context=False,
        add_history_to_context=True,
        num_history_runs=5,
        store_history_messages=False,
        store_tool_messages=True,
        # Higher than the equity sibling's 14: a trading question legitimately chains
        # search, expiry, resolve, quote, margin and place.
        tool_call_limit=20,
        max_tool_calls_from_history=6,
        telemetry=False,
        store_events=False,
    )
