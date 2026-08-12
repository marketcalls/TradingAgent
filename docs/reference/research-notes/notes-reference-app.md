# Reference app: equity-research-agent

Source read in full: `d:\AI Bootcamp 2026\Day08\equity-research-agent\`
(excluding `.git`, `__pycache__`, `node_modules`). Everything below is verbatim from
that codebase — versions, event names and file names are exact.

---

## 1. Directory tree (exact, complete)

```
equity-research-agent/
  .env                     # real keys, gitignored (4 lines, same keys as .env.example)
  .env.example             # 4 keys, values blank
  .gitignore               # 31 lines
  LICENSE                  # MIT, "Copyright (c) 2026 Rajandran R"
  PLAN.md                  # 586 lines / 31,128 bytes
  README.md                # 132 lines
  backend/
    requirements.txt       # 7 lines, no pyproject.toml
    app/
      __init__.py          # EMPTY (0 bytes)
      config.py            # 82 lines  - settings + logging takeover
      tools.py             # 499 lines - three Toolkit subclasses, 10 tools
      agent.py             # 83 lines  - build_agent()
      main.py              # 241 lines - FastAPI, SSE, sessions
  data/
    equity.db              # SQLite, created lazily by Agno SqliteDb
  frontend/
    index.html             # 23 lines
    package.json           # 30 lines
    package-lock.json
    postcss.config.js      # 1 line
    tailwind.config.js     # 28 lines
    tsconfig.json          # 17 lines
    vite.config.ts         # 10 lines
    src/
      main.tsx             # 10 lines
      App.tsx              # 444 lines - THE ENTIRE UI
      index.css            # 108 lines - tokens, shimmer, markdown CSS
```

**Important structural fact: there is no `backend/app/api/` package, no
`frontend/src/components/` directory and no `frontend/src/lib/` directory.**
PLAN.md Part 4 proposed a deeper layout (`agent/builder.py`, `agent/prompts.py`,
`agent/toolkits/*.py`, `agent/clients/*.py`, `api/chat.py`, `api/sessions.py`,
`errors.py`) but the app as built collapsed it to **four backend modules and one
React file**. The plan is aspirational; the shipped shape is flat. Mirror the
shipped shape unless the new app is meaningfully bigger.

Backend is 4 files / ~905 lines. Frontend is 3 source files / ~562 lines.
Whole app is under 1,500 lines of hand-written code.

---

## 2. Backend

### 2.1 requirements.txt (exact)

```
agno==2.8.7
litellm==1.79.1
fastapi>=0.115
uvicorn[standard]>=0.32
httpx>=0.28
yfinance>=1.4.1
python-dotenv>=1.0
```

Note: the two framework pins are **exact** (`==`), everything else is a floor.
No `pyproject.toml`, no lockfile, no `tavily-python` (Tavily is hit with raw httpx).

### 2.2 `config.py` — settings + logging

Module docstring explains *why* the logger is replaced:

```python
"""Settings and logging setup.

Agno ships a ColoredRichHandler that emits box-drawing glyphs. The project standard
is plain ASCII log output, so we replace the handlers before any Agent is built.
"""
```

Path constants computed from `__file__`, not cwd:

```python
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # repo root, above backend/
DATA_DIR     = PROJECT_ROOT / "data"
DB_PATH      = DATA_DIR / "equity.db"

load_dotenv(PROJECT_ROOT / ".env")                   # .env lives at REPO ROOT
```

`Settings` is a **plain class, not pydantic-settings** (PLAN.md said pydantic
`SecretStr`; the build used `os.getenv`). Fields:
`tavily_api_key`, `finedge_api_key`, `litellm_api_key`, `litellm_model`
(default `"openai/gpt-5.6-luna"`), and a hardcoded
`cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]`.

Defensive quirk carried into code: the source `.env` once had a duplicated key
prefix, so the model string is de-prefixed:

```python
model = os.getenv("LITELLM_MODEL", "openai/gpt-5.6-luna")
if model.startswith("LITELLM_MODEL="):
    model = model.split("=", 1)[1]
```

`Settings.missing() -> list[str]` returns names of unset keys (surfaced on
`/api/health`). `get_settings()` is `@lru_cache(maxsize=1)`.

`setup_logging(level=logging.INFO)`:
1. `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` and same for stderr
   — comment: *"The model emits currency and multiplication signs; cp1252 consoles
   raise on them."*
2. Formatter `"%(asctime)s %(levelname)-7s %(name)s %(message)s"` with `"%H:%M:%S"`.
3. Root logger: `handlers.clear()`, one `StreamHandler(sys.stdout)`.
4. For each of `("agno", "agno-team", "agno-workflow")`: clear handlers,
   `propagate = False`, attach own handler, `setLevel(logging.WARNING)`.
5. `httpx` and `LiteLLM` loggers pinned to WARNING.
6. `DATA_DIR.mkdir(parents=True, exist_ok=True)`.

### 2.3 `agent.py` — agent construction

Module docstring records the model quirks (temperature/top_p hardcoding, the
mandatory `reasoning_effort: "none"`), version-stamped: *"verified against the live
API with agno 2.8.7 and litellm 1.79.1"*.

`INSTRUCTIONS` is a module-level **list of 10 plain-English sentences** (not a
prompt file, not a callable): routing rules, symbol formats, evidence requirements,
sourcing/as-of dates, currency warnings, fiscal-year note, table formatting,
a no-investment-advice line, and "End every substantive answer with a '## Sources'
section".

`build_agent() -> Agent` (called once at import time in `main.py`):

```python
Agent(
    id="equity-research-analyst",
    name="Equity Research Analyst",
    model=LiteLLM(
        id=settings.litellm_model,
        api_key=settings.litellm_api_key,
        temperature=None,                              # Agno would send 0.7
        top_p=None,                                    # Agno would send 1.0
        request_params={"reasoning_effort": "none"},   # required whenever tools present
    ),
    db=SqliteDb(db_file=str(DB_PATH)),
    tools=[MarketTools(), ResearchTools(api_key=...), FinedgeTools(token=...)],
    description="You are a senior equity research analyst.",
    instructions=INSTRUCTIONS,
    expected_output="A focused markdown brief: a short thesis, a Key Metrics table, the main risks, then '## Sources'.",
    additional_context="If a data source is unavailable, state that explicitly rather than estimating.",
    markdown=True,
    add_datetime_to_context=True,
    timezone_identifier="Asia/Kolkata",
    add_location_to_context=False,      # would make 2 blocking HTTP calls per run
    add_history_to_context=True,
    num_history_runs=5,
    store_history_messages=False,       # default True -> quadratic transcript growth
    store_tool_messages=True,
    tool_call_limit=14,
    max_tool_calls_from_history=6,
    telemetry=False,
    store_events=False,
)
```

Never sets `system_message` (that would discard Agno's composed prompt), never sets
`output_schema` (would stream raw JSON deltas and disable markdown), no `reasoning=`,
no memory, no session summaries.

### 2.4 `main.py` — FastAPI

**Import order is load-bearing.** `setup_logging()` runs *between* imports, before
agno is imported, with `# noqa: E402` on every subsequent import:

```python
from .config import setup_logging
setup_logging()

from agno.db.base import SessionType          # noqa: E402
from fastapi import FastAPI, HTTPException, Request   # noqa: E402
...
```

Module-level singletons: `settings = get_settings()`, **`agent = build_agent()`** —
one agent instance reused by every request, constructed at import. `USER_ID = "local"`
is a module constant (single-user app, no auth).

CORS:

```python
app = FastAPI(title="Equity Research Analyst")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
```

(In dev the browser actually talks to Vite's `/api` proxy, so CORS is a safety net.)

Request model: `class ChatRequest(BaseModel): message: str; session_id: str | None = None`

#### SSE serializer (the one helper)

```python
def sse(event: str, data: dict[str, Any]) -> str:
    return f"data: {json.dumps({'type': event, **data}, default=str, ensure_ascii=False)}\n\n"
```

Notes: **no `event:` line** — the discriminator is a `type` field *inside* the JSON,
so the client uses `fetch`/`ReadableStream`, not `EventSource`. `default=str` keeps
non-serializable values from exploding. `ensure_ascii=False` preserves currency
symbols. Single-line JSON (no `indent`) is mandatory or `data:` framing breaks.

#### SSE event contract (exact, all six)

| `type` | Emitted from | Payload fields |
| --- | --- | --- |
| `start` | Agno `RunStarted` | `run_id`, `session_id` |
| `token` | Agno `RunContent` | `delta` (str; empty strings filtered out) |
| `tool_start` | Agno `ToolCallStarted` | `id`, `name`, `args` (dict) |
| `tool_end` | Agno `ToolCallCompleted` | `id`, `name`, `ok` (bool), `result` (str, capped 4000 chars + `"..."`), `duration` (float seconds or null) |
| `tool_end` | Agno `ToolCallError` (only if not already reported) | `id`, `name`, `ok: false`, `result` (error string) — **no `duration`** |
| `done` | `RunCompleted` / `RunCancelled` / fallthrough | `reason`: `"stop"` \| `"cancelled"` \| `"incomplete"`; on `"stop"` also `session_id`, `run_id` |
| `error` | Agno `RunError` **or** the `except` block | `message`, `kind` (error type name or null) |

Note the event names differ from PLAN.md's table (`tool_call_start`/`tool_result`);
the shipped names are **`tool_start`** and **`tool_end`**.

#### Stream loop specifics

- `stream = agent.arun(req.message, session_id=..., user_id=USER_ID, stream=True,
  stream_events=True)` — **not awaited**; it is an async generator directly.
  `stream_events=True` is mandatory or no tool events appear at all.
- Event discrimination is `name = getattr(ev, "event", None)` compared against the
  bare strings `"RunStarted"`, `"RunContent"`, `"ToolCallStarted"`,
  `"ToolCallCompleted"`, `"ToolCallError"`, `"RunCancelled"`, `"RunError"`,
  `"RunCompleted"` — no isinstance checks, no imported event classes.
- Disconnect check every iteration: `if await request.is_disconnected(): agent.cancel_run(run_id); return`.
- Double-report guard: `errored_tools: set[str]` — `ToolCallCompleted` with
  `tool_call_error=True` records the id; a following `ToolCallError` for the same id
  is `continue`d.
- `terminal: bool` flag; if the generator ends without a terminal event, it emits
  `done` with `reason: "incomplete"`.
- Whole loop wrapped in `try/except Exception` -> `log.exception("stream failed")` +
  an `error` frame. HTTP status is already 200 by then, so the `error` frame is the
  only failure signal the client gets.
- On `RunCompleted` for a brand-new session it calls `_name_session(sid, req.message)`
  because **Agno never titles sessions** — otherwise every sidebar row reads
  "New chat". Title = first message, whitespace-collapsed, truncated to 60 chars
  + `"..."`, written via `agent.db.rename_session(session_id=..., session_type=SessionType.AGENT, session_name=...)`.
- Response:

```python
StreamingResponse(gen(), media_type="text/event-stream", headers={
    "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
```

No heartbeat/ping is actually implemented (PLAN.md recommended one).

#### Endpoints (complete list, 6)

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/health` | `{ok, model, missing_keys}` |
| POST | `/api/chat/stream` | SSE stream (above) |
| POST | `/api/chat/{run_id}/cancel` | `{"cancelled": bool}` via `agent.cancel_run(run_id)` |
| GET | `/api/sessions` | `{items: [{session_id, title, updated_at}], total}` |
| GET | `/api/sessions/{session_id}` | `{session_id, messages: [{role, content}]}` |
| DELETE | `/api/sessions/{session_id}` | `{"deleted": true}` |

Session listing:

```python
rows, total = agent.db.get_sessions(session_type=SessionType.AGENT, user_id=USER_ID,
    limit=50, page=1, sort_by="updated_at", sort_order="desc", deserialize=False)
```

Transcript read uses `agent.get_session_messages(session_id=..., skip_roles=["system"])`,
keeps only `user`/`assistant` rows with non-empty content, and raises `HTTPException(404, ...)`
on failure. **Tool calls are not replayed on reload** — transcripts are text only.

`_title_of()` handles Agno's double-encoded JSON: loops `json.loads` **twice**, with a
docstring saying so, falling back to `"New chat"` on any failure.

---

## 3. The Toolkit pattern (`tools.py`)

### 3.1 Module-level docstring convention (copy this exactly in style)

The file opens with a one-line summary then a bulleted list of hard-won runtime
quirks, each phrased as a rule the module *depends on*:

```python
"""Agent toolkits: market data (yfinance), web research (Tavily), Indian fundamentals (Finedge).

Rules learned from live testing that this module depends on:
  - Agno never truncates tool results, so every return goes through _trim().
  - Agno calls str() on the return value and turns falsy returns into an EMPTY tool
    message, so every path returns a non-empty JSON string.
  - yfinance: .BO daily history is broken (returns ~2 rows), so .BO is rewritten to .NS.
  - yfinance: currency and financialCurrency can differ (INFY.NS is INR/USD), which makes
    naive ratios wrong by ~50x, so a mismatch is flagged explicitly.
  - yfinance: delisted or renamed symbols do not raise, they return a tiny dict.
  - Finedge: auth is a ?token= query param, symbols are case-sensitive uppercase,
    unknown symbols return 200 with {}, and throttling returns 503 (not 429).
  - Tavily: irrelevant results come back with low scores rather than an empty list,
    so results below SCORE_FLOOR are dropped.
"""
```

Same convention in `agent.py` ("Model notes verified against the live API with agno
2.8.7 and litellm 1.79.1:"), `main.py` ("Streaming notes verified against agno 2.8.7:")
and `config.py`. **Every module docstring states the observed behaviour and the code's
response to it.** No emoji, no icons, ASCII only.

### 3.2 Trimming and serialization

```python
MAX_TOOL_CHARS = 12_000
SCORE_FLOOR = 0.4

def _trim(payload: Any, limit: int = MAX_TOOL_CHARS) -> str:
    """Serialize and hard-cap size. Never returns an empty or falsy string."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, default=str, separators=(",", ":"))
    if not text:
        return '{"status":"no_data"}'
    if len(text) > limit:
        dropped = len(text) - limit
        return text[:limit] + f'... [TRUNCATED {dropped} chars]'
    return text
```

Every single tool return path goes through `_trim(...)`. Compact separators to save
tokens. Empty results become explicit sentinel objects, never `{}` / `[]` / `""`
(`{"status": "no_data"}`, `{"status": "no_news"}`, `{"status": "no_peers_found"}`,
`{"status": "no_relevant_results"}`, `{"status": "extraction_failed"}`).

### 3.3 Error handling

`from agno.exceptions import RetryAgentRun`. Raised for every *recoverable* failure,
and the message is always **an instruction to the model, not a stack trace**:

```python
raise RetryAgentRun(
    f"No quote for '{t}'. It may be delisted or the suffix is wrong. "
    "Indian tickers need market='IN'. Retry with a corrected symbol.")

raise RetryAgentRun(
    f"'{t}' returned almost no data, so it is likely delisted or renamed "
    "(for example ZOMATO.NS is now ETERNAL.NS). Try the current ticker.")

raise RetryAgentRun("statement must be one of: income, balance, cashflow.")
raise RetryAgentRun("Tavily rate limit hit. Wait a moment and retry once.")
raise RetryAgentRun("Finedge is rate limiting. Wait a moment and retry once.")
```

Bare `except Exception` blocks carry `# noqa: BLE001` and `raise ... from exc`.
Auth failure returns data rather than raising: `{"error": "finedge_auth_failed"}`.

### 3.4 Toolkit construction and caching

```python
class MarketTools(Toolkit):
    """Prices, fundamentals and news from yfinance. Both US and Indian markets."""
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(name="market_tools",
            tools=[self.get_quote, self.get_price_history, self.get_fundamentals,
                   self.get_financials, self.get_news],
            cache_results=True, cache_ttl=300, **kwargs)
```

- Three toolkits: `MarketTools` (`name="market_tools"`, cache 300s),
  `ResearchTools(api_key)` (`name="research_tools"`, **no caching** — searches must be
  fresh), `FinedgeTools(token)` (`name="india_tools"`, cache 900s).
- Tools are **bound methods** passed in `tools=[]`; `async def` methods go in the same
  list (there is no `async_tools` parameter).
- Instance attrs (`self.api_key`, `self.token`, `self._last_call`) are set **before**
  `super().__init__()`.
- Every subclass takes `**kwargs: Any` and forwards it.

### 3.5 Docstring style for tool schemas (Google style, load-bearing)

Summary line becomes the tool description; `Args:` entries become per-parameter
descriptions; type hints become JSON types; defaults are restated in prose.

```python
def get_quote(self, symbol: str, market: str = "US") -> str:
    """Get the latest price and day range for a stock, index or ETF.

    Args:
        symbol (str): Ticker or index alias. Examples: AAPL, RELIANCE, nifty, sensex.
        market (str): US or IN. Controls the exchange suffix. Defaults to US.

    Returns:
        str: JSON with price, currency, day range and 52 week range.
    """
```

Longer tools add a routing paragraph between summary and `Args:` — e.g.
`get_fundamentals`: *"Best for US names. Indian coverage is patchy: use
india_fundamentals for Indian companies..."* This is how tool selection is steered.

### 3.6 The 10 tools

`MarketTools`: `get_quote`, `get_price_history`, `get_fundamentals`, `get_financials`,
`get_news` (all sync, yfinance).
`ResearchTools`: `web_search`, `fetch_page` (both `async`, httpx -> `https://api.tavily.com`).
`FinedgeTools`: `india_fundamentals`, `india_financials`, `india_peers` (all `async`,
httpx -> `https://data.finedgeapi.com`).

Domain-shaping helpers worth mirroring: `INDEX_ALIASES` dict mapping human names
(`"nifty"`, `"sensex"`, `"sp500"`) to Yahoo symbols; `normalize_ticker(symbol, market)`
which rewrites `.BO` -> `.NS`, adds/strips `.NS` by market, and passes `^` indices
through. Downsampling before return (`step = max(1, len(closes) // 40)`, ~40 points).
Field allowlists rather than dumping blobs (`get_fundamentals` picks 24 named fields).
Statement frames sliced `df.iloc[:, :5]` and `df.head(30)`. Text fields truncated
inline (`[:900]`, `[:700]`, `[:300]`, `[:9000]`).

Rate pacing inside `FinedgeTools._get` — a monotonic-clock gate plus 503 retry:

```python
gap = time.monotonic() - self._last_call
if gap < 0.35:
    await asyncio.sleep(0.35 - gap)
self._last_call = time.monotonic()
...
for attempt in range(3):
    r = await client.get(...)
    if r.status_code == 503:
        await asyncio.sleep(0.6 * (attempt + 1)); continue
```

Each request opens its own `httpx.AsyncClient(timeout=45.0)` in an `async with` —
no shared client.

---

## 4. Frontend

### 4.1 Dependencies (package.json, exact; lockfile-resolved in parens)

```json
"dependencies": {
  "clsx": "^2.1.1",                 (2.1.1)
  "lucide-react": "^0.454.0",       (0.454.0)
  "react": "^18.3.1",               (18.3.1)
  "react-dom": "^18.3.1",           (18.3.1)
  "react-markdown": "^9.0.1",       (9.1.0)
  "remark-gfm": "^4.0.0",           (4.0.1)
  "tailwind-merge": "^2.5.4"        (2.6.1)
},
"devDependencies": {
  "@types/react": "^18.3.12",       (18.3.31)
  "@types/react-dom": "^18.3.1",    (18.3.7)
  "@vitejs/plugin-react": "^4.3.3", (4.7.0)
  "autoprefixer": "^10.4.20",       (10.5.4)
  "postcss": "^8.4.47",             (8.5.26)
  "tailwindcss": "^3.4.14",         (3.4.19)
  "typescript": "^5.6.3",           (5.9.3)
  "vite": "^5.4.10"                 (5.4.21)
}
```

`"name": "equity-research-agent-ui"`, `"private": true`, `"version": "0.1.0"`,
`"type": "module"`. Scripts: `dev: vite`, `build: tsc -b && vite build`,
`preview: vite preview`.

React 18 (not 19). **shadcn/ui is NOT installed** despite PLAN.md listing it — no
`components.json`, no Radix, no `@tanstack/react-query`, no
`react-syntax-highlighter`, no `sonner`. `clsx` and `tailwind-merge` are dependencies
but are **never imported** by any source file (leftovers). Tailwind 3 with a JS config
(not Tailwind 4 / CSS-first).

### 4.2 Component inventory (3 components, all in `src/App.tsx`)

There are no separate component files. Everything is in `App.tsx`, in this order:

| Symbol | Kind | Renders |
| --- | --- | --- |
| `useTheme()` | hook | reads `document.documentElement.classList.contains("dark")`; `toggle()` flips the `dark`/`light` classes on `<html>` and writes `localStorage["era-theme"]` |
| `ToolTimeline({tools, running})` | component | The "Behind the scenes" collapsible above each answer: toggle button with chevron, label (`"Working"` while pending, else `"Behind the scenes"`), failure count, step count; expanded rows show status dot/check/x, humanised tool label, duration, args as `key: value` mono text, and a truncated `<pre>` of the result (600 chars) |
| `Composer({onSend, onStop, running})` | component | Rounded-2xl bordered card with autogrow textarea, hint text "US and Indian equities", and a single button that is Send (`ArrowUp`) or Stop (`Square`) |
| `App()` | default export | Sidebar + header + scroll area + empty state + message list + `<Composer/>`; owns all state and the SSE reader |

Supporting module constants: `STARTERS` (4 suggested-prompt strings) and
`TOOL_LABELS` (a `Record<string,string>` mapping each of the 10 backend tool names to
a human phrase — `get_quote: "Fetching quote"`, `web_search: "Searching the web"`, etc.).

Types: `ToolCall {id, name, args?, ok?, result?, duration?}`,
`Message {role: "user"|"assistant", content, tools?, error?}`,
`SessionRow {session_id, title, updated_at?}`.

### 4.3 State management

**Plain `useState` in `App()`. No Redux, no Zustand, no react-query, no Context.**

```
messages: Message[]        sessions: SessionRow[]     sessionId: string | null
running: boolean           runIdRef, abortRef, scrollRef  (useRef)
```

The streaming update pattern is a local helper closed over `setMessages`:

```tsx
const patchLast = (fn: (m: Message) => Message) =>
  setMessages((prev) => { const next = [...prev]
    next[next.length - 1] = fn(next[next.length - 1]); return next })
```

`send()` pushes **two** messages up front (the user turn and an empty assistant turn)
so the assistant bubble exists before the first token. No `requestAnimationFrame`
coalescing was implemented (PLAN.md mentioned it); it sets state per event.

### 4.4 SSE consumption

`fetch` + `response.body.getReader()` + `TextDecoder`, with partial-line buffering:

```tsx
const res = await fetch("/api/chat/stream", { method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({ message: text, session_id: sessionId }), signal: ctrl.signal })
const reader = res.body!.getReader(); const decoder = new TextDecoder(); let buffer = ""
while (true) {
  const { done, value } = await reader.read(); if (done) break
  buffer += decoder.decode(value, { stream: true })
  const lines = buffer.split("\n")
  buffer = lines.pop() ?? ""            // keep the straddling partial line
  for (const line of lines) {
    if (!line.startsWith("data: ")) continue
    const ev = JSON.parse(line.slice(6))
    ...switch on ev.type...
  }
}
```

Dispatch: `start` -> stash `run_id`, adopt `session_id` if new; `token` -> append
`ev.delta`; `tool_start` -> push a `ToolCall`; `tool_end` -> map over tools and patch
the one whose `id` matches with `{ok, result, duration}`; `error` -> set `m.error`.
`done` is not handled explicitly (the reader just ends). `finally` clears `running`,
nulls the abort controller and refreshes the session list. `AbortError` is swallowed.

`stop()` POSTs `/api/chat/${runId}/cancel` **and** aborts the fetch.

### 4.5 Rendering rules

- **User messages get a bubble, assistant messages do not.** User: right-aligned,
  `rounded-xl bg-chat-user px-3 py-2`, `whitespace-pre-wrap`, tagged `data-user-msg`.
  Assistant: bare full-width text, `pr-10`, no avatar, no card.
- Markdown: `<ReactMarkdown remarkPlugins={[remarkGfm]}>` inside a `<div className="md text-base">`.
  All styling comes from the `.md` CSS in `index.css` — no `components={}` overrides,
  no syntax highlighter. Streams progressively (re-parsed each token).
- While `content` is empty and running: `<span className="shimmer text-base">Thinking</span>`.
- Errors render in a bordered box using `text-[color:var(--danger)]`.
- Autoscroll: an effect on `messages.length` finds the last `[data-user-msg]` node and
  `scrollTo({top: last.offsetTop - el.offsetTop - 16, behavior: "smooth"})` — pins the
  new question to the top so the answer streams into empty space below.
- Composer autogrow: `el.style.height = "0px"` **before** reading `scrollHeight`
  (comment explains why "auto" is wrong), capped by `max-h-[200px]`. Enter sends,
  Shift+Enter newlines. Clicking the card (outside a button) focuses the textarea.
- Empty state: `h1` "Equity Research Analyst", a subtitle, and the four `STARTERS` as
  pill buttons that send immediately.

### 4.6 Sidebar / sessions

Fixed `w-[264px]`, `border-r border-border bg-sidebar`. Contains a square brand mark
(`h-6 w-6 rounded-md bg-primary`) + title, a "New chat" button (`Plus` icon), a
"Threads" label, and the session rows. Each row: title button (truncate) plus a
`Trash2` delete button revealed on `group-hover` (`opacity-0 group-hover:opacity-60
hover:!opacity-100`). Active row gets `bg-muted`. Empty state "No threads yet".
`loadSessions()` swallows errors — *"sidebar is non-critical"*. It refetches after
every completed stream and after a delete. Opening a session replaces `messages` with
the fetched transcript and `tools: []` (tool cards do not survive a reload).

### 4.7 Styling

**`tailwind.config.js`** — `darkMode: ["class"]`, content `["./index.html",
"./src/**/*.{ts,tsx}"]`, no plugins. Colors are all `var(--...)` indirections:
`background, foreground, muted, muted-foreground, border, input, primary,
primary-foreground, sidebar, chat-user, danger, success`. Fonts:
sans `["Inter", "ui-sans-serif", "system-ui", "sans-serif"]`,
mono `["SFMono-Regular", "Menlo", "Consolas", "monospace"]`.
`maxWidth: { thread: "880px", composer: "768px" }` -> used as `max-w-thread` /
`max-w-composer`; the deliberate mismatch makes the composer read as a narrower
floating object.

**`index.css`** — three `@tailwind` directives, then oklch tokens on `:root` and
`.dark`. The organising idea (stated in a comment): *almost every non-text colour is
an alpha overlay off a single base*, which is why light and dark stay structurally
identical.

| Token | Light | Dark |
| --- | --- | --- |
| `--background` | `oklch(0.994 0 89.876)` | `oklch(0.205 0 0)` |
| `--foreground` | `oklch(0.097 0 0)` | `oklch(0.985 0 89.876)` |
| `--muted-foreground` | `oklch(0.097 0 0 / 0.5)` | `oklch(0.985 0 89.876 / 0.5)` |
| `--muted` | `oklch(0.097 0 0 / 0.04)` | `oklch(0.994 0 89.876 / 0.04)` |
| `--border` | `oklch(0.097 0 0 / 0.06)` | `oklch(0.994 0 89.876 / 0.06)` |
| `--input` | `oklch(0.097 0 0 / 0.12)` | `oklch(0.994 0 89.876 / 0.12)` |
| `--primary` | `oklch(0.546 0.215 262.881)` | same |
| `--primary-foreground` | `oklch(0.994 0 89.876)` | same |
| `--sidebar` | `oklch(0.097 0 0 / 0.02)` | `oklch(0.994 0 89.876 / 0.02)` |
| `--chat-user` | `oklch(0.097 0 0 / 0.08)` | `oklch(0.994 0 89.876 / 0.08)` |
| `--danger` | `oklch(0.505 0.19 27.518)` | `oklch(0.808 0.103 19.571)` |
| `--success` | `oklch(0.448 0.108 151.328)` | `oklch(0.871 0.136 154.449)` |
| `--shadow-l` | `0 4px 4px -2px oklch(0 0 0 / 0.05), 0 4px 8px -2px oklch(0 0 0 / 0.04)` | same at 0.1 / 0.08 |

No `--radius` token in the built CSS (PLAN.md listed one); radii are Tailwind classes.

`.shimmer` replaces every spinner — two stacked linear-gradients, `background-size:
300% 100%`, `background-clip: text` + `-webkit-text-fill-color: transparent`,
`animation: shimmer 3s linear infinite` sweeping `background-position` 100% -> 0%.

`.scroll-thin` — 6px webkit scrollbar, thumb `var(--input)`, transparent track.

`.md` markdown block: `p` margin-bottom `1.125rem` / line-height 1.5;
**h1..h4 all render at `1rem`/600** so answers stay flat rather than shouting;
lists indented `1.25rem`; links `var(--primary)` underlined with 2px offset; inline
`code` in `--muted` with a border; `pre` rounded 8px; tables are `display: block;
overflow-x: auto` with a 14px radius, header in `--muted-foreground`, and
`tbody tr:nth-child(even)` striped with `var(--sidebar)`.

**`index.html`** — Inter loaded from Google Fonts with two `preconnect` links
(weights 400;500;600;700), and a pre-paint inline script that reads
`localStorage["era-theme"]`, falls back to `prefers-color-scheme`, and adds the
`dark` or `light` class to `<html>` before first paint.

**`vite.config.ts`** — `port: 5173` and `proxy: { "/api": { target:
"http://127.0.0.1:8077", changeOrigin: true } }`. This is why every fetch in App.tsx
uses a bare relative `/api/...` path.

**`tsconfig.json`** — single file (no `tsconfig.node.json`), `target: ES2020`,
`moduleResolution: "bundler"`, `jsx: "react-jsx"`, `strict: true`, `noEmit: true`,
`include: ["src"]`. No path aliases (`@/`).

**`postcss.config.js`** — one line: `export default { plugins: { tailwindcss: {}, autoprefixer: {} } }`.

---

## 5. PLAN.md shape (the doc style the new plan must match)

**Size: 586 lines, 31,128 bytes, roughly 4,500 words.** Substantial but not padded —
it reads as a validation report that happens to conclude in a build plan.

Front matter is four unnumbered lines then a blockquote revision note:

```
# Equity Research Analyst Agent — Validation Report & Build Plan

Date: 2026-08-07
Framework: **Agno**
Stack: FastAPI + React (Vite) + SQLite + shadcn/ui
Model: `openai/gpt-5.6-luna` via Agno's LiteLLM model class (...)

> Revision 2 — rebuilt around the Agno framework. ...
```

Exact heading outline (H1 + 9 H2 "Part" sections + 17 H3 subsections; `---` rules
between Parts):

```
# Equity Research Analyst Agent — Validation Report & Build Plan
## Part 0 — Decide this first: Agno version skew
## Part 1 — Agno validation results (live, against the real API)
   ### 1.1 The model path works — with two mandatory suppressions
   ### 1.2 Streaming — works, with four sharp edges
   ### 1.3 Agno solves two problems we had budgeted for
   ### 1.4 Event taxonomy for the UI
   ### 1.5 Cancellation
   ### 1.6 Persistence — `SqliteDb` replaces our hand-rolled schema
   ### 1.7 Architecture decision: our own FastAPI app, not AgentOS
## Part 2 — Data source validation (Revision 1, unchanged and still valid)
   ### 2.1 Finedge — WORKS (personal tier)
   ### 2.2 Tavily — WORKS
   ### 2.3 yfinance 1.4.1 — WORKS, but cannot own Indian fundamentals
   ### 2.4 Division of labour
## Part 3 — Tools: all three custom, and why
   ### 3.1 Two framework behaviours that force custom wrappers
   ### 3.2 Per-source verdict
   ### 3.3 Toolkit authoring rules for 2.2.10
   ### 3.4 The tool set (10)
## Part 4 — Architecture
   ### 4.1 The Agent
   ### 4.2 SSE endpoint
   ### 4.3 Sessions
## Part 5 — Frontend (unchanged from Revision 1)
## Part 6 — Build order
## Part 7 — Conventions
## Part 8 — Fixes needed before building
## Part 9 — Open assumptions
```

Style rules observable throughout:

- **Every claim is evidenced.** Version numbers, file:line citations
  (`litellm/chat.py:143-147`, `agent.py:1627`, `models/base.py:1507`), measured
  numbers (`3 of 40 calls failed at ~3.7 req/s`, `ROE missing on 62%`, `0.855 vs 0.926`).
- Heavy use of **verdict tables**: capability matrices, per-source verdicts, the event
  taxonomy table, the SSE event table, the design-token table, the build-order table.
- Code blocks are short and annotated with trailing `# REQUIRED` / `# Agno would send 0.7`
  comments explaining each load-bearing line.
- Bold lead-ins to paragraphs (`**Why each line is mandatory:**`, `**Two decoys to
  avoid:**`, `**Recommendation:** ...`, `**Fallback:** ...`).
- Rejected alternatives are named and argued down, not omitted (AgentOS, built-in
  toolkits, structured output, `reasoning=True`, `OpenAIResponses`).
- Part 6 is a 10-row `| Step | Deliverable | Proves |` table, step 0 through 10, ending
  with a one-line summary of where the split falls.
- Part 9 is a numbered list of assumptions stated plainly ("Single user, no auth.").
- Em dashes throughout, British spelling in places ("Finalise", "labour"), no emoji,
  no icons, no bullet-point fluff.

README.md (132 lines) is a distinct shorter doc: title, "Running it" (two terminal
blocks), "What it does" (source-ownership table + rationale), "Architecture"
(annotated file tree), **"Things that will bite you if you change this code"** (the
biggest section — ten bolded quirks with explanations), "Configuration",
"Not built yet", "License".

---

## 6. Config keys and run commands

`.env` and `.env.example` live at the **repo root**, not in `backend/`. Four keys,
names only in the example:

```
TAVILY_API_KEY=
FINEDGE_API_KEY=
LITELLM_API_KEY=
LITELLM_MODEL=
```

Convention: `<SERVICE>_API_KEY` uppercase, plus `LITELLM_MODEL` for the model id
(`openai/gpt-5.6-luna`). Loaded with `python-dotenv` from `PROJECT_ROOT / ".env"`.
No prefix, no nesting, no pydantic-settings. Keys never reach the browser.

Run commands (from README, two terminals; there are no scripts, Makefile, Dockerfile
or CI config anywhere in the repo):

```bash
# backend  (http://127.0.0.1:8077)
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8077

# frontend (http://localhost:5173)
cd frontend
npm install
npm run dev
```

Ports: backend **8077**, frontend **5173**, and Vite proxies `/api` to the backend.
**Single uvicorn worker only** — Agno's run-cancellation registry is per-process, so
a cancel landing on another worker is a silent no-op.

`.gitignore` covers: `.env`, `.env.*` (with `!.env.example`), `data/`, `*.db`,
`*.db-wal`, `*.db-shm`, `__pycache__/`, `*.py[cod]`, `.venv/`, `venv/`, `*.egg-info/`,
`.pytest_cache/`, `.ruff_cache/`, `node_modules/`, `dist/`, `.vite/`, `*.tsbuildinfo`,
`.vscode/`, `.idea/`, `.DS_Store`, `Thumbs.db`. LICENSE is MIT, "Copyright (c) 2026
Rajandran R".

---

## 7. Conventions to carry over

1. **No emoji or icon glyphs in source or log output.** Verified: zero emoji in any
   backend or frontend file. Lucide SVG icons are used in JSX only (that is UI, not
   log output). Agno's `ColoredRichHandler` is torn out in `setup_logging()` before
   any Agent is built; `print_response` / `aprint_response` / `cli_app` are never
   called (they emit Rich borders, a bullet and a spinner).
2. **ASCII-only, UTF-8-safe logging.** `sys.stdout.reconfigure(encoding="utf-8",
   errors="replace")` because the model emits currency and multiplication signs and a
   cp1252 Windows console raises `UnicodeEncodeError`. Log messages are lowercase
   plain sentences with `%s` interpolation: `log.warning("could not name session %s",
   session_id)`, `log.exception("stream failed")`.
3. **Plain-language comments that explain the *why*, never the *what*.** Every comment
   in the codebase records an observed behaviour and the response to it, e.g.
   *"BSE daily history is broken in yfinance; NSE is the reliable feed."*,
   *"Reset to 0 (not 'auto') so scrollHeight reflects content, not the old box."*,
   *"Tavily returns irrelevant hits with low scores rather than an empty list."*,
   *"sidebar is non-critical"*. No banner comments, no section dividers, no TODOs.
4. **Module docstrings list the runtime quirks the module depends on**, version-stamped
   where relevant ("verified against agno 2.8.7 and litellm 1.79.1").
5. `from __future__ import annotations` at the top of every backend module; modern
   `str | None` unions; `dict[str, Any]` lowercase generics.
6. `# noqa: BLE001` on deliberate broad excepts, `# noqa: E402` on imports that must
   follow `setup_logging()`.
7. `telemetry=False` on the Agent (it otherwise posts run metadata to
   `os-api.agno.com` after every run).
8. Two-space-ish compact code: 4-space Python indent, ~95 char lines; TypeScript with
   **no semicolons** and double quotes.
9. Single-user app: `USER_ID = "local"` constant, no auth, no tests.
