# Agno reference note — building a production chat trading agent

Scope: everything needed to build a FastAPI/SSE chat agent with many tools and a
**user-approval gate on order placement**.

**Versions**
- Installed and verified: `agno==2.8.7` (`C:\Users\Admin1\AppData\Local\Programs\Python\Python314\Lib\site-packages\agno`)
- Docs dump (`d:\AI Bootcamp 2026\Day08\agno docs`) targets Agno **v2.x**, OpenAPI header says `2.7.2`;
  newest feature tooltip is `v2.7.3`. So docs ≈ 2.7.x, runtime = 2.8.7. Everything below was
  cross-checked against the 2.8.7 source; doc-only or deprecated items are flagged.
- Working reference implementation to copy patterns from:
  `d:\AI Bootcamp 2026\Day08\equity-research-agent\backend\app\{agent,tools,main}.py`

---

## 0. TL;DR for the trade-confirmation gate

```python
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.tools import tool

@tool(requires_confirmation=True)
def place_order(symbol: str, side: str, quantity: int, price: float) -> str:
    """Place a live order. Requires explicit user confirmation."""
    ...

agent = Agent(model=..., tools=[place_order], db=SqliteDb(db_file="trading.db"))

run = agent.run("Buy 10 RELIANCE at market")     # -> RunOutput, run.is_paused == True
for req in run.active_requirements:               # -> list[RunRequirement]
    if req.needs_confirmation:
        req.tool_execution.tool_name   # "place_order"
        req.tool_execution.tool_args   # {"symbol": "RELIANCE", ...}  <- show this in the UI
        req.confirm()                  # or req.reject("note shown to the model")

run = agent.continue_run(run_id=run.run_id, session_id=run.session_id,
                         requirements=run.requirements)
```

**A `db` is mandatory** to resume by `run_id`, and **`session_id` is required** alongside `run_id`
(2.8.7 raises `ValueError("Session ID is required to continue a run from a run_id.")`).

---

## 1. `Agent(...)` constructor

All params are **keyword-only** (`def __init__(self, *, ...)`).
Source: `agno/agent/agent.py:387`.

### 1.1 The set worth using for this project

```python
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai import OpenAIChat

agent = Agent(
    # --- identity ---
    id="trading-agent",                 # stable component id
    name="Trading Agent",
    # --- model ---
    model=OpenAIChat(id="gpt-4o"),      # Model instance or a plain str
    fallback_models=[...],              # optional: List[Model | str] on error
    # --- persistence ---
    db=SqliteDb(db_file="data/trading.db"),
    # --- tools ---
    tools=[MarketTools(), BrokerTools()],   # Toolkit | callable | Function | dict | CALLABLE FACTORY
    tool_call_limit=14,                     # hard cap on tool calls PER RUN (not per model request)
    tool_choice=None,                       # "auto" | "none" | {"type": "function", ...}
    tool_hooks=[my_hook],                   # wraps EVERY tool call
    # --- prompt / context ---
    description="You are an execution-aware trading assistant.",   # top of system message
    instructions=[...],                     # str | List[str] | Callable
    expected_output="A markdown brief ...", # appended as <expected_output>
    additional_context="...",               # free text appended to the system message
    markdown=True,                          # appends "Use markdown to format your answers."
    add_datetime_to_context=True,
    timezone_identifier="Asia/Kolkata",     # only meaningful with add_datetime_to_context
    datetime_format=None,                   # strftime override
    add_location_to_context=False,
    add_name_to_context=False,
    use_instruction_tags=False,             # wrap instructions in <instructions> tags
    # --- history ---
    add_history_to_context=True,
    num_history_runs=5,                     # DEFAULTS TO 3 if neither this nor num_history_messages set
    num_history_messages=None,              # mutually exclusive with num_history_runs
    max_tool_calls_from_history=6,          # trims old tool calls out of replayed history
    store_tool_messages=True,
    store_history_messages=False,
    # --- session state ---
    session_state={"watchlist": []},
    add_session_state_to_context=False,
    overwrite_db_session_state=False,
    enable_agentic_state=False,             # gives the model a set_session_state tool
    cache_session=False,
    # --- streaming / events ---
    stream=None,                            # per-run override is better
    stream_events=None,
    store_events=False,                     # persist events into RunOutput.events (bloats the DB)
    events_to_skip=None,                    # default [RunEvent.run_content]; ONLY affects storage
    # --- reliability ---
    retries=0,                              # whole-run retries on exception
    delay_between_retries=1,
    exponential_backoff=False,
    # --- structured output ---
    input_schema=None,                      # pydantic BaseModel for validating run input
    output_schema=None,                     # pydantic BaseModel -> RunOutput.content is that model
    parse_response=True,
    use_json_mode=False,
    parser_model=None,                      # second model that parses free text into output_schema
    # --- ops ---
    telemetry=True,                         # SET False to disable Agno's anonymous telemetry
    debug_mode=False,
    debug_level=1,                          # 1 or 2
)
```

### 1.2 Notes / gotchas verified in source

| Param | Behaviour |
|---|---|
| `num_history_runs` | If **both** `num_history_runs` and `num_history_messages` are None, Agno silently sets `num_history_runs = 3`. Setting both logs a warning and drops `num_history_messages`. |
| `add_history_to_context` | Warns and does nothing if `db` is None. |
| `tool_call_limit` | Enforced **across the whole run**, and holds even if the model requests many calls in one turn. Excess calls fail gracefully rather than raising. |
| `store_events` | When False, `RunOutput.events` is not persisted. `events_to_skip` filters **stored** events only — it never filters what the stream yields. |
| `telemetry` | Defaults **True**. Pass `telemetry=False`. |
| `timezone_identifier` | IANA name, e.g. `"Asia/Kolkata"`. Only used when `add_datetime_to_context=True`. |
| `retries` | Retries the entire run on exception; `delay_between_retries` seconds, optionally `exponential_backoff=True`. |
| `debug_mode` | Also settable per run: `agent.run(..., debug_mode=True)`. `debug_level=2` is very verbose. |
| `enable_agentic_state` | Lets the model mutate `session_state` itself via a tool. Leave off for trading. |
| `checkpoint` | `Literal["runs","tool-batch","tools"]` — mid-run checkpointing, useful if you want crash-resume. |

### 1.3 `run` / `arun` signature

```python
agent.run(
    input,                    # str | list | dict | Message | BaseModel | List[Message]
    *,
    stream=False, stream_events=None,
    user_id=None, session_id=None, session_state=None,
    run_id=None, run_context=None,
    audio=None, images=None, videos=None, files=None,
    knowledge_filters=None,
    add_history_to_context=None, add_dependencies_to_context=None,
    add_session_state_to_context=None,
    dependencies=None, metadata=None,
    output_schema=None,       # per-run structured output
    yield_run_output=False,   # stream mode: also yield the final RunOutput object
    debug_mode=None,
) -> RunOutput | Iterator[RunOutputEvent]
```

`arun` is identical plus `background: bool = False`.

**Critical async detail (verified):** `arun_dispatch` and `acontinue_run_dispatch` are plain `def`,
not `async def`. So:

```python
stream = agent.arun(msg, stream=True, stream_events=True)   # DO NOT await
async for ev in stream: ...

out = await agent.arun(msg)                                  # stream=False -> await
```

`agent.cancel_run(run_id)` / `await agent.acancel_run(run_id)` are `@staticmethod`s returning `bool`.

---

## 2. `Toolkit` subclassing

`from agno.tools import Toolkit` (`agno/tools/toolkit.py`).

### 2.1 Constructor

```python
Toolkit(
    name: str = "toolkit",
    tools: Sequence[Callable | Function] | None = None,
    async_tools: Sequence[tuple[Callable, str]] | None = None,   # [(self.aplace_order, "place_order")]
    instructions: str | None = None,
    add_instructions: bool = False,          # NOTE: False by default on Toolkit
    include_tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
    requires_confirmation_tools: list[str] | None = None,        # <-- HITL, by tool NAME
    external_execution_required_tools: list[str] | None = None,
    stop_after_tool_call_tools: list[str] | None = None,
    show_result_tools: list[str] | None = None,
    cache_results: bool = False,
    cache_ttl: int = 3600,
    cache_dir: str | None = None,
    timeout: int | None = None,
    auto_register: bool = True,
)
```

### 2.2 Registration rules

1. Subclass `Toolkit`, define methods, pass the **bound methods** in `tools=[...]` to
   `super().__init__()`. Registration happens in `__init__` when `auto_register=True`.
2. Set instance attributes **before** `super().__init__(...)` — the tools list references `self.x`.
3. Each entry becomes a `Function`; the name is `func.__name__` (or the alias in `async_tools`).
4. `include_tools` / `exclude_tools` are validated in the constructor and **raise `ValueError`** if a
   listed name is not in the toolkit. `requires_confirmation_tools` / `external_execution_required_tools`
   / `stop_after_tool_call_tools` / `show_result_tools` only **log a warning** on typos — so a typo
   in `requires_confirmation_tools` silently disables your safety gate. Guard it in a test.
5. Async is auto-detected via `inspect.iscoroutinefunction`. Async methods go to `async_functions`;
   `agent.arun()` prefers those, falling back to sync. You can name an async variant `aplace_order`
   and expose it to the model as `place_order` via `async_tools=[(self.aplace_order, "place_order")]`.
6. `@tool`-decorated methods inside a Toolkit work too: `_register_decorated_tool` rebinds `self` and
   **decorator settings win over toolkit settings** for `show_result`, `stop_after_tool_call`; for
   `requires_confirmation` / `external_execution` it is an **OR** (`function.X or name in toolkit_list`).
7. Duplicate tool names across toolkits: the second one is **skipped with a warning**
   (`Duplicate tool name '<x>' already registered on agent`). Namespace your tool names.

```python
from typing import Any
from agno.tools import Toolkit

class BrokerTools(Toolkit):
    """Order placement and portfolio state."""

    def __init__(self, client, **kwargs: Any) -> None:
        self.client = client                          # BEFORE super().__init__
        super().__init__(
            name="broker_tools",
            tools=[self.get_positions, self.get_orders, self.place_order, self.cancel_order],
            requires_confirmation_tools=["place_order", "cancel_order"],   # the HITL gate
            instructions=(
                "Always call get_positions before proposing a new order. "
                "Quantities are in shares, never in currency."
            ),
            add_instructions=True,                     # must be True or `instructions` is ignored
            cache_results=False,                       # NEVER cache order/position calls
            **kwargs,
        )

    def get_positions(self, account: str = "default") -> str:
        """Get current open positions for an account.

        Args:
            account (str): Broker account id. Defaults to "default".

        Returns:
            str: JSON list of positions with symbol, qty, avg_price and P&L.
        """
        ...
```

### 2.3 Schema generation — docstring & type-hint rules

`Function.process_entrypoint()` (`agno/tools/function.py:408`) builds the JSON schema:

- **Types** come from `typing.get_type_hints(entrypoint)`. An unannotated parameter degrades the
  schema. Annotate everything.
- **Descriptions** come from the docstring, parsed by `docstring_parser` (`parse()`), so Google,
  NumPy and Sphinx styles all work. Per-arg text comes from the `Args:` section. If the docstring
  declares a type (`symbol (str): ...`), it is prefixed into the description as `"(str) ..."`.
- **Tool description** = short + long description of the docstring
  (`get_entrypoint_docstring`), unless you pass `description=` to `@tool`.
- **Required** = every parameter with no default (unless `strict=True`, which marks all required).
- **Stripped from the schema and injected at call time**: `self`, `return`, `run_context`, `agent`,
  `team`, `images`, `videos`, `audios`, `files` — *and* any parameter whose annotation is
  `Agent`, `Team`, `RunContext`, `Image`, `Video`, `Audio`, `File` regardless of its name.

```python
from agno.run import RunContext

def add_to_watchlist(run_context: RunContext, symbol: str) -> str:
    """Add a symbol to the session watchlist."""
    st = run_context.session_state or {}
    st.setdefault("watchlist", []).append(symbol.upper())
    run_context.session_state = st
    return f"watchlist: {st['watchlist']}"
```

### 2.4 Caching — `cache_results` / `cache_ttl` / `cache_dir`

**The Toolkit docstring says "in-memory" but the implementation is on-disk JSON.** Verified in
`Function._get_cache_file_path`: `<cache_dir or tempdir()>/agno_cache/functions/<tool_name>/<key>.json`.

- Key = hash of the entrypoint args, so different args cache separately.
- `cache_ttl` default `3600` s; expiry checked on read.
- Generator / async-generator tools are **never** cached.
- Precedence in a Toolkit: a `@tool(cache_results=True)` decorator value wins; `cache_ttl` from the
  decorator wins only when it differs from the 3600 default.
- **Implication for trading:** cache is on disk and survives a process restart. Use it for reference
  data (instrument master, historical bars) with a modest TTL; **never** for quotes, positions,
  order book or anything you place orders against.

### 2.5 Toolkit / tool instructions

`add_instructions=True` + `instructions="..."` appends the text to the agent's system message
(collected into `agent._tool_instructions`, see `agno/agent/_tools.py:403-432`). Works at three levels:
per-`Function` (`@tool(instructions=..., add_instructions=True)` — `add_instructions` defaults **True**
on `@tool`), per-`Toolkit` (defaults **False**, must be turned on), and the agent's own `instructions`.

### 2.6 How many tools before degradation?

Agno publishes **no numeric limit**; there is no `max_tools`. The docs' guidance
(`tools/overview.md`, "Design Tools for Reliable Calls") is qualitative: *"Expose only the tools
needed for the task — reduces ambiguous choices and limits access."* The practical ceiling is the
model's, not Agno's. Field guidance for this build:

- Keep the exposed set to roughly **15–25 well-named tools**. The equity-research reference agent runs
  10 tools across 3 toolkits comfortably with `tool_call_limit=14`.
- Beyond that, prune with `include_tools` / `exclude_tools`, or scope dynamically (below), or split
  into a Team.

### 2.7 Dynamic tool scoping — YES, via callable factories

`tools` accepts `Callable[..., List]` as well as a list. The factory is resolved **at the start of
each run** and can read `run_context`:

```python
from agno.run import RunContext

def get_tools(run_context: RunContext):
    state = run_context.session_state or {}
    base = [MarketTools(), ResearchTools()]
    if state.get("trading_enabled") and state.get("role") == "trader":
        base.append(BrokerTools(client))      # order tools only appear when allowed
    return base

agent = Agent(model=..., tools=get_tools, cache_callables=False)
agent.print_response("Buy 10 RELIANCE", session_state={"role": "trader", "trading_enabled": True})
```

Related constructor params: `cache_callables: bool = True`,
`callable_tools_cache_key: Callable[..., Optional[str]]`, `callable_knowledge_cache_key`.
Set `cache_callables=False` when the toolset must change per session/user, otherwise the first
resolution is reused.

Post-init mutation also exists: `agent.add_tool(fn)` and `agent.set_tools([...])` (replaces all).

### 2.8 Tool exceptions

`from agno.exceptions import AgentRunException, RetryAgentRun, StopAgentRun, RunCancelledException`

- `RetryAgentRun("message")` — feeds the message back to the model and lets it retry. This is the
  right way to say "bad symbol, try `market='IN'`".
- `StopAgentRun("message")` — ends the run.
- `RunCancelledException` — re-raised untouched by the `@tool` wrapper.

Other useful ones: `ModelProviderError`, `ModelRateLimitError`, `ContextWindowExceededError`,
`InputCheckError` / `OutputCheckError` (guardrails), `RunNotFoundError`, `RunNotContinuableError`.

**Behaviour the reference impl learned the hard way:** Agno does not truncate tool results, calls
`str()` on the return value, and turns a falsy return into an *empty* tool message. Always return a
non-empty string and cap its size yourself.

---

## 3. HUMAN-IN-THE-LOOP — the trade-confirmation gate (CRITICAL)

Docs: `hitl/overview.md`, `hitl/user-confirmation.md`, `hitl/user-input.md`,
`hitl/external-execution.md`, `hitl/approval.md`.
Source: `agno/run/requirement.py`, `agno/agent/_run.py`, `agno/agent/_tools.py`.

### 3.1 The four HITL flavours

| Flag | What happens | Use for |
|---|---|---|
| `@tool(requires_confirmation=True)` | Pauses **before** the call; you approve/reject; Agno then executes it. | **Order placement / cancel / modify.** |
| `@tool(requires_user_input=True, user_input_fields=[...])` | Pauses to collect missing arg values, then Agno executes. | Ask for the limit price the model shouldn't guess. |
| `@tool(external_execution=True)` | Pauses; **Agno never calls the function**; you execute it and hand back the result. | Route the real order through your own risk-checked path. |
| `@approval` (`from agno.approval import approval`) | Persists a pending approval row; an admin resolves it in the DB; then `continue_run`. | Async/out-of-band desk approval. |

`requires_confirmation`, `requires_user_input` and `external_execution` are **mutually exclusive** on
one tool — the `@tool` decorator raises `ValueError` if you set more than one.

### 3.2 `@tool` decorator — full parameter list (`agno/tools/decorator.py`)

```python
@tool(
    name=None, description=None, strict=None,
    instructions=None, add_instructions=True,
    show_result=None, stop_after_tool_call=None,
    requires_confirmation=None,
    requires_user_input=None, user_input_fields=None,
    external_execution=None, external_execution_silent=None,
    pre_hook=None, post_hook=None, tool_hooks=None,
    cache_results=False, cache_dir=None, cache_ttl=3600,
)
```
Any other kwarg raises `ValueError: Invalid tool configuration arguments: {...}`.

### 3.3 Detecting the pause

```python
run: RunOutput = agent.run("Buy 10 RELIANCE at 1450 limit")

run.is_paused                        # bool  -> run.status == RunStatus.paused
run.status                           # RunStatus.paused  ("PAUSED")
run.run_id, run.session_id
run.requirements                     # list[RunRequirement] — ALL, including resolved
run.active_requirements              # list[RunRequirement] — only unresolved  <- iterate this
run.tools                            # list[ToolExecution]
run.tools_requiring_confirmation     # list[ToolExecution] (legacy-style accessor, still present)
run.tools_requiring_user_input
run.tools_awaiting_external_execution
run.content                          # "I have tools to execute, but I need confirmation."
```

`RunStatus` (`agno/run/base.py`): `pending | running | completed | paused | cancelled | error | regenerated`
→ values `"PENDING" "RUNNING" "COMPLETED" "PAUSED" "CANCELLED" "ERROR" "REGENERATED"`.

### 3.4 `RunRequirement` — the modern API (prefer this)

`from agno.run.requirement import RunRequirement` (also re-exported via the run module).

Fields: `id`, `created_at`, `tool_execution: ToolExecution`, `confirmation`, `confirmation_note`,
`user_input_schema`, `user_feedback_schema`, `external_execution_result`,
`member_agent_id` / `member_agent_name` / `member_run_id` (team origin).

Properties: `needs_confirmation`, `needs_user_input`, `needs_user_feedback`,
`needs_external_execution`, `external_execution_silent`, `pause_type`
(`"confirmation" | "user_input" | "user_feedback" | "external_execution"`).

Methods:
```python
req.confirm()                                  # raises ValueError if not a confirmation requirement
req.reject(note: str | None = None)            # note is fed back to the model
req.provide_user_input({"to_address": "x@y"})  # dict[field_name -> value]
req.provide_user_feedback({"Which broker?": ["Zerodha"]})
req.set_external_execution_result("filled 10 @ 1450.25")
req.is_resolved() -> bool
req.to_dict() / RunRequirement.from_dict(d)    # JSON-safe, for shipping to a browser and back
```

`req.to_dict()` / `from_dict()` is exactly what you want for a web UI: serialize the requirement into
the SSE payload, let the user click Approve/Reject, POST it back, rebuild with `from_dict`, then
`continue_run(requirements=[...])`.

### 3.5 `ToolExecution` — what to render in the confirmation card

`from agno.models.response import ToolExecution`

```
tool_call_id, tool_name, tool_args (dict), result, tool_call_error, metrics, child_run_id,
stop_after_tool_call, created_at,
requires_confirmation, confirmed, confirmation_note,
requires_user_input, user_input_schema, user_feedback_schema, answered,
external_execution_required, external_execution_silent,
approval_type, approval_id,
.is_paused -> bool property
```

Show `tool_name` + `tool_args` in the chat card. For an order that is
`{"symbol": "RELIANCE", "side": "BUY", "quantity": 10, "price": 1450.0}`.

### 3.6 Resuming — `continue_run` / `acontinue_run`

```python
agent.continue_run(
    run_response: RunOutput | None = None,   # positional; pass this OR run_id
    *,
    run_id: str | None = None,
    requirements: list[RunRequirement] | None = None,   # PREFERRED
    updated_tools: list[ToolExecution] | None = None,   # DEPRECATED in 2.8.7 (DeprecationWarning)
    input: str | None = None,                # append a new user message before resuming
    continue_from: int | "end" | "last_user" = "end",
    fork: bool = False, regenerate: bool = False, replace_original: None,
    additional_instructions: str | None = None,
    stream: bool | None = None, stream_events: bool | None = False,
    user_id=None, session_id=None, run_context=None,
    knowledge_filters=None, dependencies=None, metadata=None,
    debug_mode=None, yield_run_output=False,
) -> RunOutput | Iterator[RunOutputEvent]
```
`acontinue_run(...)` mirrors it and adds `background: bool = False`.

Three ways to call it:

```python
# (a) same process, you still hold the RunOutput — simplest
run = agent.continue_run(run)                      # or continue_run(run_response=run)

# (b) across an HTTP request boundary — what a chat backend does
run = agent.continue_run(run_id=rid, session_id=sid, requirements=reqs)

# (c) admin/out-of-band approval already resolved in the DB (@approval flow)
run = agent.continue_run(run_id=rid, session_id=sid)   # no requirements arg
```

**Hard rules verified in `continue_run_dispatch` (`agno/agent/_run.py:3251`):**

1. `ValueError("Either run_response or run_id must be provided.")`
2. `ValueError("Session ID is required to continue a run from a run_id.")` unless `agent.session_id`
   is set. **Always pass `session_id` with `run_id`.**
3. `Exception("continue_run() is not supported with an async DB. Please use acontinue_run() instead.")`
4. If the run still has unresolved requirements and you pass neither `requirements` nor
   `updated_tools`, Agno looks for a resolved admin approval; if there is none it raises
   `ValueError("Run has unresolved HITL requirements. Provide the `requirements` parameter ...")`.
5. `RunNotFoundError` if the run_id is not in the session; `RunNotContinuableError` if the run was
   cancelled.
6. Continuing a **COMPLETED** run auto-forks into a new `run_id` (a sibling in the same session).
   A **PAUSED** run continues in place, keeping its `run_id`. This is what you want.

### 3.7 Approve vs reject — what the model sees

From `handle_tool_call_updates` (`agno/agent/_tools.py:856`):

- **Approved** (`confirmed=True` **and** `result is None`): the tool runs now. The
  `result is None` guard means a confirmed tool is never executed twice on a re-continue.
- **Rejected** (`confirmed=False`, or left `None`): `reject_tool_call` writes a tool message with
  `confirmation_note or "Function call was rejected by the user"`, sets `tool_call_error=True`, and
  the model continues the conversation knowing it was refused. Give a useful note:

```python
req.reject("Rejected: quantity exceeds the per-order risk limit. Propose a smaller size.")
```

- Either way `requires_confirmation` is flipped to `False` so the tool won't re-pause in the same run.
- Non-HITL tools requested in the same model turn execute normally; only the gated ones pause the run.

### 3.8 Streaming behaviour (this is the part that bites)

```python
async for ev in agent.arun(msg, stream=True, stream_events=True):
    if ev.event == "RunPaused":            # RunPausedEvent
        ...
```

Verified facts:

1. **`RunPausedEvent` is yielded unconditionally in streaming mode, even when
   `stream_events=False`.** In `handle_agent_run_paused_stream` the event is `yield`ed directly and
   is not behind an `if stream_events:` guard, unlike `RunStarted` / `RunContinued` / `RunCompleted`.
   So a UI that only wants content tokens still receives the pause. Handle it or you will hang.
2. The paused event carries everything you need:
   `ev.tools: list[ToolExecution]`, `ev.requirements: list[RunRequirement]`,
   `ev.active_requirements` (property), `ev.is_paused` (always `True`), `ev.run_id`, `ev.session_id`,
   `ev.content` (the "I need confirmation" string).
3. **The stream ends at the pause.** `handle_agent_run_paused_stream` yields the event and `return`s —
   no `RunCompleted` follows. Your SSE generator must treat `RunPaused` as terminal-for-now and close
   the response, otherwise the client waits forever.
4. `RunContinuedEvent` is emitted at the top of a streamed `continue_run`, **only when
   `stream_events=True`**. Note `continue_run`'s `stream_events` defaults to **`False`**, not `None` —
   pass it explicitly or you lose all tool events on the resume leg.
5. On resume the confirmed tool emits normal `ToolCallStarted` / `ToolCallCompleted` events, then
   content, then `RunCompleted`.
6. Run state is persisted at the pause (`cleanup_and_store`), so the resume can legitimately happen
   in a different HTTP request / different worker, as long as the same `db` is used.

Sketch of the SSE backend shape (extends the pattern in the reference `main.py`):

```python
async def gen():
    async for ev in agent.arun(msg, session_id=sid, user_id=uid, stream=True, stream_events=True):
        name = ev.event
        if name == "RunStarted":
            yield sse("start", {"run_id": ev.run_id, "session_id": ev.session_id})
        elif name == "RunContent":
            if isinstance(ev.content, str) and ev.content:
                yield sse("token", {"delta": ev.content})
        elif name == "ToolCallStarted":
            yield sse("tool_start", {"id": ev.tool.tool_call_id, "name": ev.tool.tool_name,
                                     "args": ev.tool.tool_args})
        elif name == "ToolCallCompleted":
            yield sse("tool_end", {"id": ev.tool.tool_call_id, "name": ev.tool.tool_name,
                                   "ok": not ev.tool.tool_call_error, "result": ev.tool.result})
        elif name == "RunPaused":
            yield sse("confirm", {
                "run_id": ev.run_id, "session_id": ev.session_id,
                "requirements": [r.to_dict() for r in ev.active_requirements],
            })
            return                              # terminal for this request
        elif name == "RunCancelled":
            yield sse("done", {"reason": "cancelled"}); return
        elif name == "RunError":
            yield sse("error", {"message": str(ev.content), "kind": ev.error_type}); return
        elif name == "RunCompleted":
            yield sse("done", {"reason": "stop", "run_id": ev.run_id, "session_id": ev.session_id})


# second endpoint: POST /api/chat/confirm  {run_id, session_id, decisions: {req_id: bool}}
async def confirm_gen(run_id, session_id, decisions, notes):
    run = agent.get_run_output(run_id=run_id, session_id=session_id)
    for req in run.active_requirements:
        if req.needs_confirmation:
            if decisions.get(req.id):
                req.confirm()
            else:
                req.reject(notes.get(req.id))
    async for ev in agent.acontinue_run(
        run_id=run_id, session_id=session_id,
        requirements=run.requirements,
        stream=True, stream_events=True,          # MUST pass; defaults to False here
    ):
        ...  # same event switch as above
```

`agent.get_run_output(run_id, session_id=None, user_id=None) -> RunOutput | None` (and
`aget_run_output`) is the supported way to rehydrate a paused run in a later request;
`agent.get_last_run_output(session_id=None)` also exists.

### 3.9 The `@approval` variant (persisted, admin-resolved)

```python
from agno.approval import approval
from agno.tools import tool

@approval                                  # or @approval(type="audit") for non-blocking audit trail
@tool(requires_confirmation=True)
def place_order(...): ...

db = SqliteDb(db_file="trading.db", approvals_table="approvals")
```

At the pause Agno writes a pending row and stamps `ToolExecution.approval_id`. DB methods:
`db.get_approval(approval_id)`, `db.get_approvals(...)`, `db.update_approval(...)`,
`db.delete_approval(...)`, `db.update_approval_run_status(run_id, run_status)` (+ async variants).

```python
db.update_approval(approval_id, expected_status="pending", status="approved",
                   resolved_by="risk_desk", resolved_at=int(time.time()))
run = agent.continue_run(run_id=run.run_id, session_id=run.session_id)   # no requirements needed
```

`expected_status="pending"` gives you optimistic concurrency. `@approval(type="audit")` is
non-blocking: it still needs one HITL flag on `@tool`, logs the decision, and does not gate on an admin.

### 3.10 Teams

Identical API on `Team`. `requirement.member_agent_name` tells you which member triggered it, and
tools attached to the team leader itself also pause.

---

## 4. Streaming

### 4.1 Turning it on

```python
stream = agent.arun(msg, stream=True, stream_events=True)   # async generator, DO NOT await
for ev in agent.run(msg, stream=True, stream_events=True):  # sync iterator
```

- `stream=True` alone yields **only `RunContent`** events (plus terminal events and `RunPaused`).
- `stream_events=True` is required for `RunStarted`, `ToolCall*`, `Reasoning*`, `Memory*`, hooks, etc.
- The v1 name `stream_intermediate_steps` is **gone**; it is silently swallowed by `**kwargs`.
- `yield_run_output=True` makes the stream also yield the final `RunOutput` object as its last item.

### 4.2 Complete event list — `RunEvent` (`agno/run/agent.py:143`)

Enum member → wire string (`ev.event` is the string).

**Core**
| Enum | `ev.event` | Payload highlights |
|---|---|---|
| `run_started` | `"RunStarted"` | `model`, `model_provider` |
| `run_content` | `"RunContent"` | `content` (delta), `content_type`, `reasoning_content`, `citations`, `references`, `response_audio`, `image` |
| `run_content_completed` | `"RunContentCompleted"` | — |
| `run_intermediate_content` | `"RunIntermediateContent"` | `content` (only with `output_model`) |
| `run_completed` | `"RunCompleted"` | `content`, `metrics`, `session_state`, `metadata`, media |
| `run_error` | `"RunError"` | `content` (message), `error_type`, `error_id` |
| `run_cancelled` | `"RunCancelled"` | — |

**Control flow (HITL)**
| `run_paused` | `"RunPaused"` | `tools`, `requirements`, `.is_paused`, `.active_requirements` |
| `run_continued` | `"RunContinued"` | — |

**Tools**
| `tool_call_started` | `"ToolCallStarted"` | `tool: ToolExecution` |
| `tool_call_completed` | `"ToolCallCompleted"` | `tool`, `content`, `images/videos/audio/files` |
| `tool_call_error` | `"ToolCallError"` | `tool`, `error` |

**Hooks** `PreHookStarted`, `PreHookCompleted`, `PostHookStarted`, `PostHookCompleted`
**Reasoning** `ReasoningStarted`, `ReasoningStep`, `ReasoningContentDelta`, `ReasoningCompleted`
**Memory** `MemoryUpdateStarted`, `MemoryUpdateCompleted`
**Session summary** `SessionSummaryStarted`, `SessionSummaryCompleted`
**Parser model** `ParserModelResponseStarted`, `ParserModelResponseCompleted`
**Output model** `OutputModelResponseStarted`, `OutputModelResponseCompleted`
**Model request** `ModelRequestStarted`, `ModelRequestCompleted`
**Compression** `CompressionStarted`, `CompressionCompleted`
**Followups** `FollowupsStarted`, `FollowupsCompleted`
**Custom** `CustomEvent` (`"CustomEvent"`)

Every event inherits `BaseAgentRunEvent`: `created_at`, `event`, `agent_id`, `agent_name`, `run_id`,
`parent_run_id`, `session_id`, `workflow_id`, `workflow_run_id`, `step_id`, `step_name`, `step_index`,
`nested_depth`, `tools`, `content`, plus the properties `tools_requiring_confirmation`,
`tools_requiring_user_input`, `tools_awaiting_external_execution`.

Compare either way: `ev.event == "ToolCallStarted"` or `ev.event == RunEvent.tool_call_started`
(`RunEvent` is a `str, Enum` so both work).

### 4.3 Streaming gotchas learned in production (from the reference impl + source)

1. Errors **do not raise** in streaming mode. They arrive as `RunErrorEvent` and the stream ends.
2. `ToolCallCompleted` can fire with `tool.tool_call_error=True` and then be **followed by a separate
   `ToolCallError`** for the same `tool_call_id`. De-dupe on `tool_call_id` or you double-count.
3. A cancelled run may still emit a trailing `RunCompleted` — treat `RunCancelled` as terminal.
4. `RunPaused` terminates the stream; there is no `RunCompleted` after it.
5. `continue_run` / `acontinue_run` default `stream_events=False`. Pass `stream_events=True`.
6. Client disconnect: check `await request.is_disconnected()` in the loop and call
   `agent.cancel_run(run_id)`.
7. Custom events: subclass `agno.run.agent.CustomEvent` as a `@dataclass` and `yield` it from an
   async tool — good for emitting live order-status updates into the same SSE channel.

```python
from dataclasses import dataclass
from agno.run.agent import CustomEvent

@dataclass
class OrderStatusEvent(CustomEvent):
    order_id: str | None = None
    state: str | None = None

@tool()
async def place_order_streaming(symbol: str, qty: int):
    """Place an order and stream its lifecycle."""
    yield OrderStatusEvent(order_id="O123", state="submitted")
```

---

## 5. Sessions & storage

### 5.1 Db classes

```python
from agno.db.sqlite import SqliteDb, AsyncSqliteDb
from agno.db.postgres import PostgresDb, AsyncPostgresDb
```

```python
SqliteDb(
    db_file=None, db_engine=None, db_url=None,
    session_table=None, memory_table=None, metrics_table=None, eval_table=None,
    knowledge_table=None, culture_table=None, learnings_table=None,
    approvals_table=None, schedules_table=None, schedule_runs_table=None,
    traces_table=None, spans_table=None, versions_table=None,
    components_table=None, component_configs_table=None, component_links_table=None,
    auth_tokens_table=None, service_accounts_table=None, mcp_oauth_*_table=None,
    id=None,
)
```
Connection resolution: `db_engine` -> `db_url` -> `db_file` -> `./agno.db`. Parent dirs auto-created.

`PostgresDb(...)` takes the same table params but **no `db_file`**, plus `db_schema: str = "ai"` and
`create_schema: bool = True`. Raises `ValueError("One of db_url or db_engine must be provided")`.

```python
PostgresDb(db_url="postgresql+psycopg://user:pass@host:5432/db", db_schema="ai")
```

Default table names (`agno/db/base.py`): `agno_sessions`, `agno_memories`, `agno_metrics`,
`agno_eval_runs`, `agno_knowledge`, `agno_culture`, `agno_traces`, `agno_spans`,
`agno_schema_versions`, `agno_components`, `agno_learnings`, `agno_schedules`, `agno_approvals`.

For a trading app: SQLite is fine for a single-process dev backend; move to `PostgresDb` for
production, because a paused run must be readable by whichever worker handles the confirm request.

**Async DB caveat:** `continue_run()` raises if the agent's db is an `Async*Db`. Use `acontinue_run()`.

### 5.2 Session CRUD on the db

```python
from agno.db.base import SessionType   # SessionType.AGENT/.TEAM/.WORKFLOW -> "agent"/"team"/"workflow"

db.get_sessions(session_type=None, user_id=None, component_id=None, session_name=None,
                start_timestamp=None, end_timestamp=None,
                limit=None, page=None, sort_by=None, sort_order=None,
                deserialize=True)
    # deserialize=False -> returns Tuple[List[dict], int]  (rows, total)

db.get_session(session_id, session_type=None, user_id=None, deserialize=True)
db.delete_session(session_id, user_id=None) -> bool
db.delete_sessions(session_ids: List[str], user_id=None) -> None
db.rename_session(session_id, session_type, session_name, user_id=None, deserialize=True)
db.upsert_session(session, deserialize=True)
db.upsert_sessions(sessions, deserialize=True, preserve_updated_at=False)
```

**`get_recent_sessions` does not exist in 2.8.7.** Use
`db.get_sessions(session_type=SessionType.AGENT, user_id=uid, limit=50, page=1,
sort_by="updated_at", sort_order="desc", deserialize=False)` — exactly what the reference
`main.py` does for the sidebar.

Note from the reference impl: in Agno's SQLite schema, `session_data` comes back **double-encoded
JSON**, so `json.loads` twice before reading `session_name`.

### 5.3 Agent-level session helpers

Every one has an `a`-prefixed async twin.

```python
agent.get_session(session_id=None, user_id=None) -> AgentSession | None
agent.save_session(session) -> None
agent.set_session_name(session_id=None, autogenerate=False, session_name=None) -> AgentSession
agent.get_session_name(session_id=None) -> str
agent.rename(name, session_id=None) -> None
agent.get_session_state(session_id=None) -> dict          # raises if no session_id anywhere
agent.update_session_state(session_state_updates: dict, session_id=None) -> str
agent.get_session_metrics(session_id=None)
agent.get_session_summary(session_id=None)
agent.delete_session(session_id, user_id=None) -> None
agent.get_session_messages(session_id=None, last_n_runs=None, limit=None,
                           skip_roles=None, skip_statuses=None,
                           skip_history_messages=True) -> List[Message]
agent.get_chat_history(session_id=None, last_n_runs=None) -> List[Message]
agent.get_run_output(run_id, session_id=None, user_id=None) -> RunOutput | None
agent.get_last_run_output(session_id=None) -> RunOutput | None
agent.fork_session(...)
```

Naming a session (Agno does not auto-name them):
```python
agent.set_session_name(session_id=sid, session_name="RELIANCE swing idea")
# or, from the db directly:
agent.db.rename_session(session_id=sid, session_type=SessionType.AGENT, session_name=title)
```

Rebuilding a chat transcript for the UI:
```python
msgs = agent.get_session_messages(session_id=sid, skip_roles=["system"])
[{"role": m.role, "content": m.content} for m in msgs if m.role in ("user", "assistant") and m.content]
```

### 5.4 `session_state`

Constructor: `session_state`, `add_session_state_to_context=False`,
`overwrite_db_session_state=False` (default = **merge**), `enable_agentic_state=False`,
`resolve_in_context=True`, `cache_session=False`.
Per-run: `agent.run(..., session_state={...}, add_session_state_to_context=True)`.

**Tools do NOT receive a `session_state` kwarg in 2.8.7.** Verified in
`FunctionCall._build_entrypoint_args` — the injected names are `agent`, `team`, `run_context`, `fc`,
`_agno_agent`, `_agno_team`, `_agno_run_context`, `images`, `videos`, `audios`, `files`. Use
`run_context: RunContext` and mutate `run_context.session_state`:

```python
from agno.run import RunContext

def set_risk_limit(run_context: RunContext, max_notional: float) -> str:
    """Set the per-order notional limit for this session."""
    st = run_context.session_state or {}
    st["max_notional"] = max_notional
    run_context.session_state = st
    return f"max_notional set to {max_notional}"
```

(`session_state` *is* injected by name into callable tool/knowledge factories, dynamic-instruction
callables, and pre/post hooks — just not into tools.)

`RunContext` fields (`agno/run/base.py`): `run_id`, `session_id`, `user_id`, `workflow_id`,
`workflow_name`, `dependencies`, `knowledge_filters`, `metadata`, `session_state`, `output_schema`,
`messages`, `tools`, `knowledge`, `members`, `client_tools`.

Agno also injects `current_user_id` and `current_session_id` into `session_state` automatically.
Session state **survives the HITL pause/continue round-trip** (see
`examples/agents/human-in-the-loop/confirmation-with-session-state.md`, which is literally a
watchlist example).

Instructions can interpolate state: `instructions="Watchlist: {watchlist}"` with
`resolve_in_context=True` (default).

---

## 6. Memory & knowledge (optional)

### 6.1 User memories

```python
from agno.memory import MemoryManager
from agno.db.schemas.memory import UserMemory

agent = Agent(db=SqliteDb(db_file="trading.db"), update_memory_on_run=True)
agent.run("I only trade NSE large caps", user_id="rajandran", session_id="s1")
agent.get_user_memories(user_id="rajandran")   # -> List[UserMemory]
```

- `update_memory_on_run: bool` is the **current** name. `enable_user_memories` still works but is
  soft-deprecated (2.8.7 maps it onto `update_memory_on_run`).
- `enable_agentic_memory=True` gives the model tools to manage memories itself.
- `add_memories_to_context`, `memory_manager=MemoryManager(model=..., db=..., add_memories=True,
  update_memories=True, delete_memories=False, clear_memories=False)`.
- **No extra deps** — just your model provider and `sqlalchemy`. Stored in `agno_memories`.
- Costs an extra model call per run. For a trading agent, use it only for durable preferences
  (risk tolerance, preferred exchange), never for positions.

### 6.2 Knowledge / vector search

```python
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.lancedb import LanceDb, SearchType

kb = Knowledge(vector_db=LanceDb(uri="/tmp/lancedb", table_name="research"),
               contents_db=db, max_results=10)
kb.insert(path="./research_pdfs")        # NOT add_content() — that name is gone in 2.8.7
agent = Agent(knowledge=kb, search_knowledge=True, add_knowledge_to_context=False)
```

- Content API: `insert` / `ainsert` / `insert_many` (kwargs `path`, `url`, `text_content`, `topics`,
  `metadata`, `include`, `exclude`, `upsert=True`, `skip_if_exists=False`, `reader`, `auth`),
  plus `search`/`asearch`, `get_content`, `remove_content_by_id`, `remove_all_content`.
- Agent params: `knowledge`, `knowledge_filters`, `add_knowledge_to_context`,
  `search_knowledge=True` (adds a `search_knowledge_base` tool),
  `add_search_knowledge_instructions=True`, `knowledge_retriever`, `references_format`.
- **Extra deps required:** `pip install "agno[lancedb]"` (lancedb>=0.26.0) or
  `"agno[pgvector]"` + `"agno[psycopg]"`; `"agno[vectordbs]"` bundles them. The embedder needs its
  own SDK (`openai`, `google-genai`, ...).
- Probably skip for v1 of the trading agent — live market tools cover the need.

---

## 7. Model providers

| Import | Class | Default `id` |
|---|---|---|
| `from agno.models.openai import OpenAIChat` | `OpenAIChat` | `gpt-5.4-mini` |
| `from agno.models.openai import OpenAIResponses` | `OpenAIResponses` | `gpt-5.4-mini` |
| `from agno.models.anthropic import Claude` | `Claude` | `claude-sonnet-4-5-20250929` |
| `from agno.models.litellm import LiteLLM` | `LiteLLM` | `gpt-4o` |
| `from agno.models.litellm import LiteLLMOpenAI` | `LiteLLMOpenAI` (OpenAI-compatible proxy) | `gpt-4o` |
| `from agno.models.ollama import Ollama` | `Ollama` | `llama3.1` |
| `from agno.models.google import Gemini` | `Gemini` | `gemini-3.5-flash` |
| `from agno.models.groq import Groq` | `Groq` | `llama-3.3-70b-versatile` |

Key params per provider (names differ — this is the usual source of bugs):

- **OpenAIChat** — `api_key`, **`base_url`**, `temperature`, `max_tokens`, `max_completion_tokens`,
  `top_p`, `seed`, `stop`, `reasoning_effort`, `verbosity`, `strict_output=True`,
  `extra_headers/query/body`, `request_params`, `timeout`, `max_retries`, `http_client`,
  `client_params`. Env `OPENAI_API_KEY`.
- **OpenAIResponses** — **`max_output_tokens`** (not `max_tokens`), `reasoning`, `reasoning_effort`
  (`minimal|low|medium|high`), `reasoning_summary`, `parallel_tool_calls`, `max_tool_calls`,
  `truncation`, `background`, `store`.
- **Claude** — **`max_tokens` defaults to 8192**, `temperature`, `top_p`, `top_k`, `stop_sequences`,
  `thinking`, `cache_system_prompt`, `cache_tools`, `betas`, `citations=True`, `request_params`.
  Env `ANTHROPIC_API_KEY`.
- **LiteLLM** — `api_key`, **`api_base`** (not `base_url`), `temperature: float = 0.7`,
  `top_p: float = 1.0`, `max_tokens`, `request_params`, `extra_headers/query/body`.
  `supports_native_structured_outputs = False`.
- **LiteLLMOpenAI** — env `LITELLM_API_KEY`, `base_url` defaults to `http://0.0.0.0:4000`.
- **Ollama** — **`host`** (not `base_url`), `format`, `options`, `keep_alive`, `timeout`,
  `api_key` (env `OLLAMA_API_KEY`; if set with no host, host becomes `https://ollama.com`).
  `supports_native_structured_outputs = True`.
- **Gemini** — **`max_output_tokens`**, `temperature`, `top_p`, `top_k`, `stop_sequences`, `seed`,
  `safety_settings`, `generation_config`, `search`, `grounding`, `url_context`, `vertexai`,
  `project_id`, `location`.
- **Groq** — `api_key` (env `GROQ_API_KEY`), `base_url`, `temperature`, `max_tokens`, `top_p`,
  `seed`, `request_params`.

Extras: `pip install "agno[openai]" | [anthropic] | [google] | [groq] | [ollama] | [litellm]`
(litellm is pinned `<=1.82.6`).

**LiteLLM landmine from the reference impl** (`agent.py` header comment): Agno's `LiteLLM` class
hardcodes `temperature=0.7` / `top_p=1.0` and sends them unconditionally. Models that reject those
need them passed as `None` (Agno filters `None` out of the payload). `request_params` is merged last,
so it reaches every sync/async/streaming call:

```python
LiteLLM(id=model_id, api_key=key, temperature=None, top_p=None,
        request_params={"reasoning_effort": "none"})
```

### 7.1 Structured output

- **`response_model` is gone in 2.8.7.** Use `output_schema`.
- `output_schema: Type[BaseModel] | Dict[str, Any]` on the constructor **or** per run
  (`agent.run(..., output_schema=Order)`).
- `RunOutput.content` holds the parsed model instance; `RunOutput.content_type` is the class name.
- Fallbacks when the provider can't do native structured outputs: `use_json_mode=True`, or
  `parser_model=OpenAIChat(...)` (+ `parser_model_prompt`), or `output_model` / `output_model_prompt`.
- `input_schema: Type[BaseModel]` validates the run input.

```python
from pydantic import BaseModel

class OrderIntent(BaseModel):
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float | None = None

agent = Agent(model=OpenAIChat(id="gpt-4o"), output_schema=OrderIntent)
intent: OrderIntent = agent.run("buy 10 reliance at 1450 limit").content
```

Note: structured output and tool-calling can conflict on some providers. For a chat agent, prefer a
free-text agent with a separate structured extraction call rather than `output_schema` on the main agent.

---

## 8. Teams vs one agent with many tools

```python
from agno.team import Team, TeamMode
team = Team(members=[research_agent, execution_agent], mode=TeamMode.route, model=..., db=...)
```
`TeamMode`: `coordinate` (default), `route`, `broadcast`, `tasks`.

**Recommendation for this build: one Agent with many tools, not a Team.**

Agno's own guidance (`teams/overview.md`): "The leader and members make separate model calls. This
adds latency, token usage, and coordination state." Use a single agent when the task fits one domain
of expertise, when minimizing token cost matters, and when the extra coordination does not improve
the result. Trading chat is one domain (market data + portfolio + order entry).

Concrete reasons to stay single-agent here:
- **Latency.** Every delegation is an extra model round-trip. Order entry is latency-sensitive.
- **HITL clarity.** HITL works on Teams and `requirement.member_agent_name` tells you the origin, but
  a single agent gives a flat, unambiguous "this run is paused on this tool call" model in the UI.
- **Tool count is not a reason to split.** 15–25 tools on one agent is fine; prune with
  `include_tools` / `exclude_tools` or a callable factory first.

Revisit a Team only if you add a genuinely separate discipline (e.g. a backtesting/quant researcher
with its own toolset and its own model) whose outputs and metrics you want tracked separately.

---

## 9. Version and breaking-change notes

**Installed: `agno==2.8.7`. Docs dump targets v2.x (OpenAPI header `2.7.2`, newest feature tooltip
`v2.7.3`).** Doc examples use `gpt-5.4-mini`, `gemini-3.5-flash`, `update_memory_on_run`,
`Knowledge.insert`, `run_context.session_state` and `TeamMode` — all consistent with the 2.8 line.

### 9.1 v1 -> v2 renames (from `other/v2-migration.md` / `other/v2-changelog.md`, verified in source)

| v1 | v2 / 2.8.7 |
|---|---|
| `storage=` (`agno.storage.*`) | `db=` (`agno.db.sqlite`, `agno.db.postgres`, ...) |
| `response_model` | `output_schema` |
| `stream_intermediate_steps` | `stream_events` |
| `Memory` class | `MemoryManager` (`agno.memory`) |
| `add_history_to_messages` | `add_history_to_context` |
| `num_history_responses` | `num_history_runs` |
| `context` / `add_context` | `dependencies` / `add_dependencies_to_context` |
| `RunResponse` / `TeamRunResponse` | `RunOutput` / `TeamRunOutput` (`agno.run.agent`, `agno.run.team`) |
| `RunResponseStartedEvent` etc. | `RunStartedEvent`, `RunContentEvent`, `RunCompletedEvent`, `RunErrorEvent`, `RunCancelledEvent`, `RunPausedEvent`, `RunContinuedEvent` |
| `message=` / `messages=` | `input=` |
| `agent_id` / `team_id` | `id` |
| `add_datetime_to_instructions` | `add_datetime_to_context` (also `add_location_to_context`, `add_name_to_context`) |
| `extra_data` | `metadata` |
| `add_messages` | `additional_input` |
| `add_state_in_messages` | removed; `resolve_in_context=True` handles `{placeholders}` |
| `show_tool_calls` | removed (always on) |
| `Playground`, `FastAPIApp`, `AGUIApp` | `AgentOS` + interfaces |
| `arun(stream=True)` returned a coroutine | now returns an `AsyncIterator` directly — **do not await** |

A DB migration script exists for v1 tables:
`libs/agno/migrations/v1_to_v2/migrate_to_v2.py` (Postgres, MySQL, SQLite, MongoDB; idempotent).

### 9.2 Changes *inside* 2.x that bite (newer than the v2.0 changelog)

- `enable_user_memories` -> **`update_memory_on_run`** (old name still mapped, soft-deprecated).
- `search_session_history` -> **`search_past_sessions`**; `num_history_sessions` ->
  **`num_past_sessions_to_search`** (old names still mapped in `__init__`).
- `Knowledge.add_content` -> **`Knowledge.insert` / `insert_many`**.
- **`continue_run(updated_tools=...)` is deprecated in 2.8.7** and emits a `DeprecationWarning`.
  Use `requirements=[RunRequirement, ...]`. Many doc pages still show `updated_tools` and
  `run_response.tools_requiring_confirmation` with `tool.confirmed = True` — that path still works
  but is the legacy one. **Write new code against `active_requirements` / `req.confirm()` /
  `req.reject()` / `requirements=`.**
- `TeamMode` enum introduced; prefer `mode=` over `respond_directly` / `delegate_to_all_members`.
- `RunContext` is now the tool-injection vehicle; there is no `session_state` tool kwarg.
- `AuthMiddleware` was `JWTMiddleware` before v2.7 (alias kept).
- `LiteLLMOpenAI` is importable from `agno.models.litellm` but is not in that module's `__all__`.

### 9.3 Suggested requirements pin

```
agno==2.8.7
fastapi>=0.115
uvicorn[standard]>=0.32
httpx>=0.28
sqlalchemy>=2.0
python-dotenv>=1.0
# + one of: openai / anthropic / litellm / google-genai / ollama
```

---

## 10. Quick checklist for the trade-confirmation feature

1. `db=SqliteDb(...)` (Postgres in prod) — **required**, or the run cannot be resumed.
2. Mark order tools `@tool(requires_confirmation=True)`, or list them in the toolkit's
   `requires_confirmation_tools=[...]`. Add a unit test that asserts the tool's
   `requires_confirmation` is actually `True` (typos there only warn).
3. Never set `cache_results=True` on order/position/quote tools.
4. Stream with `agent.arun(msg, stream=True, stream_events=True)`; **do not await**.
5. Treat `RunPaused` as terminal for that HTTP request; ship
   `[r.to_dict() for r in ev.active_requirements]` to the browser and close the SSE stream.
6. On the confirm endpoint: `agent.get_run_output(run_id, session_id=...)`, resolve each
   `active_requirement` with `.confirm()` / `.reject(note)`, then
   `agent.acontinue_run(run_id=..., session_id=..., requirements=run.requirements,
   stream=True, stream_events=True)` — **`stream_events` defaults to `False` here, pass it**.
7. Always pass `session_id` alongside `run_id`.
8. De-dupe `ToolCallCompleted(tool_call_error=True)` against the following `ToolCallError`.
9. Set `telemetry=False`, `store_events=False`, and a sane `tool_call_limit`.
10. Handle `RunError` (errors do not raise in streaming mode) and client disconnect
    (`agent.cancel_run(run_id)`).
