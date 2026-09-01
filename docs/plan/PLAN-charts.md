# Plan: the /charts page and the chart analyst

A second surface in the TradingAgent app: a real charting terminal with an AI
technical analyst beside it. The analyst can see the chart, analyse it, interpret
what it sees, and act on it, the way a person would.

Everything here was measured against the running system on 2026-09-01, not read
off a docs page. Where docs and source disagree, source wins and the
disagreement is recorded, per PLAN.md Part 1.

---

## Part 0. What was verified before planning

Eight research agents ran over openalgo-charts, OpenAlgo, the Agno SDK, this
repo and the two skill sets. The live system was probed directly.

### 0.1 The model can do the job

`LITELLM_MODEL=openai/gpt-5.6-luna`, probed through litellm:

| Property | Value |
| --- | --- |
| `supports_vision` | true, confirmed with a real image round trip in 3.1s |
| `supports_function_calling` | true |
| `supports_reasoning` | true |
| `max_input_tokens` | 922,000 |
| `max_output_tokens` | 128,000 |
| Cost per token | 2e-07 in, 1.2e-06 out |

Given five chart tool schemas and the prompt `draw the channel for the price
move from 4446 to 3235`, it emitted `find_swing_points {"from_price":4446,
"to_price":3235}` in 5.3s using 44 reasoning tokens.

### 0.2 The data is there

OpenAlgo answered on `http://127.0.0.1:5000`. Analyzer mode on.

- Live intervals: `1m 3m 5m 10m 15m 30m 1h D`. No seconds, weeks or months.
  `utils/constants.py` accepts 29 values but the broker advertises 8. Call
  `intervals()` at runtime, never hard-code.
- RELIANCE daily returned 272 bars over 400 days; 5m returned 504 over 10 days.
  NIFTY on `NSE_INDEX` works, volume 0 as expected for an index.

**The trap that will bite.** The SDK localises intraday to `Asia/Kolkata` but
leaves `D`, `W`, `M` naive:

    if interval not in ['D', 'W', 'M']:
        df["timestamp"] = df["timestamp"].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')

The chart engine's one internal time model is UTC seconds. Mixing the two puts
annotations on the wrong bars the moment the user switches timeframe.
Normalisation happens in exactly one function and nowhere else.

Second trap: `OpenAlgoDataFeed.getBars` throws without `from` and `to`, and
converts them to **IST** `YYYY-MM-DD` strings because that is the server's
convention. `chart.setTimezone()` does not change it. Widen the range by a day
rather than assuming they agree.

### 0.3 The chart library

`openalgo-charts` 1.9.2, zero runtime dependencies, six lazy tiers, ESM only.

Introspected from the built bundle rather than the README, which is wrong in
both directions:

Counts differ between the checked-in `dist/` in the sibling repo and the
published package. Measured, not read:

| Registry | Sibling repo `dist/` | Published 1.9.2, installed here |
| --- | --- | --- |
| Drawing tools | 43 | **51** |
| Indicators | 91 | **102** |
| Chart types | 15 | **13** base, 15 with the transform tier |

The sibling `dist/` was built 27 Aug against `src/` last touched 1 Sep, which is
the drift. This project installs `openalgo-charts@1.9.2` from npm and reads every
count from `registeredIndicators()`, `registeredDrawingTools()` and
`registeredChartTypes()` at runtime, never from prose. OpenAlgo's own frontend
has the same problem pointing the other way: it pins 1.9.2 and has 1.8.3 in
`node_modules`.

Every indicator carries a machine-readable input schema. `supertrend` is:

    period      number  "ATR Period"  default 10   min 1    max 200
    multiplier  number  "Multiplier"  default 3    min 0.1  max 20

That schema becomes the tool schema, generated from `registeredIndicators()`,
never hand-maintained.

Also recorded: `DEFAULT_THEME` is `lightTheme`, despite the `ChartOptions.theme`
JSDoc claiming dark is the default. Pass the theme explicitly.

### 0.4 The drawing API

`DrawingController` from `openalgo-charts/draw`. Anchors are
`{ time: UTC seconds, price }`, never pixels or bar indices, which is what keeps
a shape welded to its bars when history pages in.

    draw.add({ tool, points, style, paneIndex }) -> Drawing

Style merge order is `controller.defaultStyle`, then `tool.defaultStyle`, then
yours. Tool defaults beat the controller's, so styling is passed per drawing.

**The primary markup shape is `polyline`, not `parallel-channel`.** Confirmed
against the reference recording zoomed in: the boundaries step and bend, tracking
the actual swing points. They are not two straight parallel rails.

`polyline` takes a variable number of anchors and **closes and fills the path
when `style.fill === true`**. So one drawing expresses the whole shaded envelope:
walk the swing highs left to right, then the swing lows right to left.

    points: [...highs, ...lows.reverse()]
    style:  { fill: true, fillColor, fillOpacity: 0.10, color, lineWidth }

This is easier and more honest than a channel fit. No regression, no offset
algebra, no log-scale correction, and every vertex is a real pivot the user can
point at on the chart. It also degrades well: three pivots or thirty, same code.

The straight diagonal from the swing high in the reference is a separate object,
a `trend-line` with `extendRight`, drawn alongside the envelope.

`parallel-channel` stays available for when a user explicitly asks for a
straight channel, and its semantics are worth recording because they are not
obvious. `points[0]` and `points[1]` are the base line. `points[2]` contributes
**only its price**; its time is ignored for geometry and is just a drag handle.
The offset is measured from the midpoint of the base segment in screen space,
which on a linear scale reduces to:

    p2.price = (p0.price + p1.price) / 2 + offset

To put the second rail through a known price P at time `p0.time`:

    p2.price = (p0.price + p1.price) / 2 + (P - p0.price)

On a logarithmic scale the offset is a rigid pixel distance, so compute
`points[2].price` through `coordinateToPrice(priceToCoordinate(mid) + px)`.

Six facts that will otherwise cost a day each:

1. A drawing renders only once it has `max(1, tool.points)` anchors. A
   `parallel-channel` with two points is invisible and un-hittable, silently.
2. `chart.destroy()` does **not** destroy the controller. Call
   `draw.destroy()` yourself, first, or placement mode can be stranded on.
3. `add()` honours a supplied `id` verbatim. The hit-test parser splits on `#`,
   so an id must not contain one.
4. `update(id, {points})` replaces; `update(id, {style})` merges.
5. `fromJSON` bypasses `_insert`: no id generation, no `defaultStyle` merge, and
   it wipes undo, redo and the selection. Entries must carry complete ids and
   styles.
6. The fib tools ship `fill: true, fillOpacity: 0.06` by default. Pass
   `fill: false` for bare levels.

### 0.5 Agno 2.8.7

Two findings that change the design.

**`output_schema` disables token streaming.** `agent/_response.py:1067`:

    should_parse_structured_output = output_schema is not None and agent.parse_response and agent.parser_model is None
    stream_model_response = True
    if should_parse_structured_output:
        stream_model_response = False

So the obvious design, an agent returning a typed annotation list, silently
costs the streaming prose. Rejected.

**`CustomEvent` yielded from a generator tool is the transport.** A tool can push
an event to the UI and then return a string to the model:

    yield ChartDrawingEvent(shapes=[...], group_id="ch_1")   # to the canvas
    yield "Drew a descending channel from 4446.8 to 3235."   # to the model

This gives the ordering seen in the reference recording: markup appears while
the status still reads "Fetching candles...", before a word of prose. The shape
data never passes through the model's output, so the model cannot corrupt it.
Tool inputs stay typed regardless: Agno inlines nested Pydantic schemas into
tool parameters, with no streaming penalty.

Also recorded: the parameter is `stream_events`, not `stream_intermediate_steps`
(the v1 name, absent in 2.8.7). "Thought for 27s" is a client-side timer, not a
field on any event; Agno's own TUI computes it the same way.

### 0.6 Where /charts lives

Decided with the user: **inside the TradingAgent React app**, not OpenAlgo.

OpenAlgo's `/trading` is `terminal.ts` at 3,089 lines plus roughly 8,000 lines of
panels, and the repo has no LLM dependency anywhere. Rebuilding the analysis half
here is smaller than it looks, because the order, position, bracket and
trade-feed code is the bulk of it and is out of scope. In exchange the AI panel
is native: one origin, one SSE contract, the existing session and thread UI, no
CORS and no auth bridge.

Porting Agno into OpenAlgo was considered and rejected. OpenAlgo runs a single
gunicorn worker under eventlet and its own architecture notes record a
deliberate ban on asyncio. Agno's streaming path is async throughout.

---

## Part 1. Scope

### 1.1 In scope for v1

Chart: symbol search and switching, interval switcher driven by a runtime
`intervals()` call, chart types, indicator picker with generated settings
dialogs, drawing tools with a properties panel, crosshair and OHLC legend, panes
and price scales, themes, layout persistence, live ticks.

Analyst: see, analyse, interpret, act.

### 1.2 Out of scope for v1

Deferred: market replay, compare and overlay symbols, watchlist panel, option
chain panel, market depth panel, screenshot export, linked chart grids.

Permanently out of scope on this page: everything that mutates broker state. No
order placement, no order lines, no drag-to-modify, no bracket lines, no
position lines, no buy/sell buttons, no trade tier. The chat page keeps the order
tools it already has behind RiskGuard; /charts does not get them.

Worth stating plainly: nothing the analyst does on /charts can reach a broker.

### 1.3 Single layout

One chart, no grid, no linked panes.

### 1.4 What v1 actually contains

Taken from an exhaustive inventory of `/trading`, split by whether the chart
engine provides it (a tier import and a call, hours) or OpenAlgo hand-built the
UI around it (the engine is canvas-only and ships no DOM, so every picker,
dialog and rail is app code, days).

| Area | From the library | Hand-built UI needed |
| --- | --- | --- |
| Symbol | metadata, tick, price format | search dialog, segment chips |
| Interval | `CandleBuilder` from seconds | fetch, grouping, lookback table, dropdown |
| Chart types | 15 registered types, transforms | catalogue, icons, dropdown, volume re-bucketing |
| Indicators | catalogue, add, remove, defaults, style keys | picker, settings dialog, plot-style rows |
| Drawings | controller, tools, undo, magnet, lock, persistence | rail, tool catalogue, style bar, text dialog |
| Panes and scales | overlay volume, autoscale, viewport | grid toggles, reset-scale wrapper |
| Crosshair | hovered-bar events, on-canvas legends | the DOM OHLC readout |
| Live | candle builder, bar cache | reconcile loop, REST fallback, state pill |
| Theme | light and dark themes | oklch to rgb rasterization, rebuild on swap |

Three things `/trading` never built, all of which are complete engine APIs
sitting unused in the bundle. They are the cheapest way for `/charts` to be
better than the page it is modelled on, and each is roughly a menu over an API
that already works:

- **Log, percentage and indexed-to-100 price scales.** `PRICE_SCALE_MODES` and
  `chart.priceAxisState` are exported; nothing in OpenAlgo's frontend references
  either.
- **Compare and overlay symbols.** A whole comparison tier, zero references.
- **Indicator alerts.** `IndicatorAlertSpec` and friends, zero references. This
  one matters here because the reference recording's second chip is "Set alert
  at resistance", so the analyst wants it.

Also worth knowing: chart types deliberately exclude Kagi and Point and Figure,
and intervals `Q` and `Y` exist in the server's constant list but are never
grouped by the intervals service, so they can never reach a dropdown.

---

## Part 2. The analyst

| | What it means | How it works |
| --- | --- | --- |
| See | "Analyse this screen" | Canvas to PNG, sent as an image part |
| Analyse | trend, momentum, structure | Python over the full frame |
| Interpret | "descending channel, lower highs" | The model narrates returned geometry |
| Act | draw, retimeframe, add indicator | Tools that emit chart commands |

### 2.1 Vision proposes, geometry disposes

Vision is what makes this feel like a person looking at a chart, and it is the
only way to answer "what do you make of this screen". It is also where a model
will confidently misread a price axis.

So the rule is: the model may look and say there is a channel here, but it must
call the geometry tool to find the actual swing points before anything is drawn.
The model chooses what to draw and writes the caption; Python computes where.
Same principle the README already states about order previews: the numbers are
fetched by the backend, not written by the AI, so they are worth trusting even
if the reply above them is wrong.

An LLM eyeballing 200 bars of OHLCV and inventing "4446.8 down to 3628" is the
one part of this that would demo beautifully and be wrong in production.

### 2.2 Two indicator systems, both needed

| | Count | Runs | Job |
| --- | --- | --- | --- |
| Chart registry | 91 | Browser | What you see: overlay, pane, legend, settings dialog |
| OpenAlgo `ta` | 127 | Backend | What it reasons over: full series, detection, scans |

"Add supertrend 3,10" is the first. "Is momentum weakening?" is the second. They
must agree on parameters, or the chart shows one thing while the reply argues
another.

### 2.3 Ambiguity is stated, not guessed

`supertrend 3,10` has two readings and both sit inside the declared bounds, so
validation cannot disambiguate it. Convention says ATR period 10, multiplier 3.
The agent applies the convention and says which reading it used. Silently
picking one is how a feature feels magical once and wrong afterwards.

---

## Part 3. Architecture

### 3.1 Data paths

Two consumers of one source, deliberately separate.

    Browser  ->  FastAPI /api/oa/*  ->  OpenAlgo :5000     chart candles
    Backend  ->  frames.get_frame   ->  OpenAlgo :5000     analysis frames

Candles are proxied through FastAPI so the OpenAlgo API key never reaches the
browser. OpenAlgo's own frontend does hand the key to the browser
(`GET /api/websocket/apikey` decrypts and returns it), but that app is
same-origin with the broker API and this one is not.

Live ticks are the one open question. Three options, in preference order:

1. Proxy the WebSocket through FastAPI. Key stays server-side, matches the REST
   decision. Most work.
2. Poll `quotes` through the REST proxy on a timer. Simplest; OpenAlgo's own
   terminal already uses this as its reconnect fallback. Not tick-accurate.
3. Hand the key to the browser and use `OpenAlgoLiveDataFeed` directly, exactly
   as OpenAlgo does. Least work, most capability, and it puts a broker
   credential in the page.

Recommend 1, with 2 as the fallback if the socket work slips. Do not do 3 without
deciding deliberately to accept it.

Whatever is chosen, `subscribeBars` needs `seedFrom`. History normally ends
inside the forming bucket, so an unseeded builder opens a second bar in the same
bucket: wrong open, volume from zero, a red candle under a live green one.

**The trap in dropping the trading code.** OpenAlgo's terminal subscribes to
**Depth, not LTP**, for tradeable instruments, and the chart's live price comes
off `depth.ltp`. Depth looks like order-book machinery and is the obvious thing
to delete along with the order tools. Deleting it freezes the chart. Their own
comment records why the dual subscription was removed:

    // Depth is the terminal's ONLY subscription for tradeable instruments,
    // so the chart ticks off depth.ltp ... This replaced the old dual
    // LTP+Depth subscribe, which broke on brokers whose adapters track one
    // mode per symbol (Depth overwrote LTP and the chart froze while depth
    // kept flowing -- issue #1664).

So: keep the Depth subscription and `depth.ltp`, drop only the bid/ask feed into
the buy/sell buttons. Indices have no order book and stay on LTP, selected by a
`quoteOnly` flag on the exchange.

That same stream then gives a read-only depth ladder for free in v2, which is
worth noting because OpenAlgo's `MarketDepthPanel` is not a standalone panel at
all: it exists inside their order dialog. A read-only ladder would be new here,
but the row renderer is presentational and liftable.

Two more things that must not be deleted with the trading code: `long-position`
and `short-position` are **drawing** tools, risk-reward boxes that place nothing;
and `setHistoryLoader` must always report back, or scroll-back paging silently
stops for the rest of the session.

### 3.2 Annotation transport

    tool -> ChartDrawingEvent (CustomEvent) -> _pump -> SSE "chart_command"
         -> handleEvent case -> DrawingController

Adding an SSE event type needs three edits in lockstep, and the frontend
silently drops frames whose `type` it does not recognise:

- the `sse()` call in `backend/app/main.py:_pump`
- the `StreamEvent` union in `frontend/src/lib/sse.ts`
- the `switch (event.type)` that consumes it

TypeScript 7 will point at every site once the union member exists, because the
switch is exhaustive.

### 3.3 Annotation ownership

Every AI-placed drawing gets a supplied id namespaced `ai:<run>:<n>`. Auto ids
are `d1`, `d2`, ... from a module-level counter shared by every controller in the
module instance, which is not addressable across sessions. The id must not
contain `#`.

This is what makes "clear what you drew" safe: it never touches a drawing the
user placed by hand.

    const kept = draw.toJSON().filter(d => !d.id.startsWith("ai:"))

One logical annotation is several primitives (two rails, a fill, an anchor
marker, a price label) added and removed as a group.

Two ways to apply a set, and the choice is a real tradeoff:

| | Per-shape `add()` | One `fromJSON()` |
| --- | --- | --- |
| Undo | One step per shape | Wipes undo, redo and selection |
| Repaints | One per shape | One |
| Requires complete ids and styles | No | Yes |

A 25-shape markup at the default `historyLimit: 50` evicts half the user's undo
history. Use `fromJSON` for a wholesale replace, or `add()` with
`historyLimit: 200`. There is no batch-add API.

Detected patterns outlive the turn and stay addressable by id, because the
follow-up chips need them: projecting a target needs the channel's geometry to
still exist server-side.

`locked: true` keeps AI markup rendered but un-draggable, which is probably the
right default. `visible: false` is the hide toggle.

### 3.4 Why the drawing tier and not the `draws()` hook

Three mechanisms exist. The choice matters.

| | `draws()` on an indicator | `IndicatorDrawings` standalone | `DrawingController` |
| --- | --- | --- | --- |
| Bundle | base | base | `/draw` tier |
| Shapes | 4 kinds | 4 kinds | 43 tools incl. parallel-channel |
| User can drag a rail | No | No | Yes |
| Survives symbol change | Regenerated | Manual | Yes |
| Persisted in chart state | No | No | Yes |
| Undo, clipboard | No | No | Yes |

An AI-detected channel is a proposal the trader should own, so
`DrawingController` is right: `parallel-channel` is already a registered tool, so
the agent's channel is indistinguishable from a hand-drawn one and rides the
same persistence path.

`IndicatorDrawings` standalone stays available if a later feature wants
non-interactive decoration: one `setItems()` replaces the whole set atomically,
no undo pollution, no tier import.

### 3.5 Geometry

Pure Python over numpy and pandas, both already in `requirements.txt`.

**Swing pivot detection is the whole foundation.** The primary output is not a
fitted line, it is an ordered list of real pivots, which the frontend connects.
Everything else is built on it.

- swing pivots over a lookback, with a significance filter so a noisy series
  does not return sixty vertices
- the envelope: highs left to right, lows right to left, one closed polyline
- trendline through selected pivots, with touch validation
- horizontal support and resistance by pivot price clustering
- converging trendlines for triangles and wedges
- measured-move projection from envelope width

Each returns exact anchors. The model never computes a coordinate.

**"Visible" is part of the request.** The reference prompt says "the price move
from 4446 to 3235", and the drawn envelope spans exactly the pivots on screen.
So the visible logical range is part of the ambient context in 4.1, and pivot
selection is clipped to it. A drawing is not re-derived on pan: once placed it
is an object the user owns, and silently redrawing it under them would be worse
than leaving it where they saw it.

**Density control matters more than fit quality.** A pivot detector with too
short a lookback produces a sawtooth that reads as noise, not structure. The
tunable is the significance filter, and it wants a default that produces roughly
the five to nine vertices per boundary visible in the reference, not a vertex
per swing.

**The pivot off-by-one to avoid.** `pivotHigh` returns the pivot price on the
*confirming* bar, carrying the value from `right` bars earlier. OpenAlgo's own
`zones_with_draws.js` anchors at the confirming bar, so every vertex in it sits
`right` bars too far right. On an envelope that shears the whole shape sideways.
Anchor at `bars[i - right].time`.

---

## Part 4. UX contract

Read off the reference recording frame by frame. Each line is something visible.

### 4.1 Ambient context

The reference prompt supplies no symbol, exchange, interval or date. The agent
resolved all four from the chart the user was looking at.

Chart state is injected every turn through `session_state`, never asked for. If
the model has to ask which symbol, the magic is gone.

The two numbers in the prompt were approximate and came back snapped to real
swing points, 4446.8 and 3235. Loose-number snapping is a requirement.

### 4.2 The status line

One line, replaced in place, not a growing log.

| Phase | Shown |
| --- | --- |
| Submit to first tool call | `Thinking...` |
| While the history tool runs | `Fetching candles...` |
| Once the answer starts | `Thought for 27s` |

Each tool gets a human phrase, not its function name. `ToolTimeline.tsx` already
carries a 43-entry `TOOL_LABELS` map doing exactly this, including
`get_history: "Loading price history"`. Extend it, do not rebuild it.

### 4.3 Ordering

Markup lands on the chart while the status still reads "Fetching candles...",
before any prose. Drawing and prose are two independent streams. Do not wait for
the text.

### 4.4 The answer

Short bold heading, then labelled bullets naming the actual anchor prices, then
an offer of the next step. It names the instrument and timeframe back at the
user, which is how a wrong-symbol answer gets caught. It is a caption for the
drawing, not an essay.

### 4.5 Follow-up chips

Two, not five. Both are concrete actions on the object just drawn. Chips declare
whether they re-prompt the agent or navigate elsewhere.

---

## Part 5. Conventions

Inherited from PLAN.md Part 10 and the existing frontend, not negotiable:

- No icons or emoji in code, comments, logs or agent instructions.
- Frontend: double quotes, no semicolons, 2-space indent, `cn()` for classes,
  the `.shimmer` text sweep instead of spinners, zero `dark:` utilities because
  light and dark are token swaps. Every file opens with a `/** why */` block.
- Backend: type hints and Google-style docstrings on every tool method, because
  Agno builds the JSON schema from `get_type_hints` plus a parsed docstring.
- Module docstrings record hard-won runtime facts. The timezone split in 0.2, the
  channel anchor semantics in 0.4 and the pivot offset in 3.5 each belong in one.
- Tool results pass the 12,000 char cap. Geometry travels on the custom event,
  not the tool result, so the cap does not apply to it.

Chart-specific:

- The Tailwind palette is alpha overlays on one base colour, which a canvas
  cannot consume. Add opaque `--chart-up`, `--chart-down`, `--chart-grid` to both
  theme blocks and map them in `@theme inline`.
- `DrawingStyle` stores literal colour strings, not theme references. On a theme
  swap, AI drawings must be re-`update()`d or they keep the old palette.
- The chart lives in a plain class behind a ref, never in React state. A 60 fps
  tick stream must not enter React. Only low-frequency UI-shaped events reach
  `setState`.
- Create and destroy the chart and the controller in **one** effect, guarded by a
  liveness flag, so StrictMode's double mount is survivable.
- A chart rebuild is a full teardown, and it happens on every interval, chart
  type and theme change. Anything attached must be re-attached in the rebuild
  tail or it silently vanishes.
- localStorage keys use the app's existing `oa-` prefix, scoped `oa-charts-`.
  Not `oa-trading-`, which is OpenAlgo's namespace for the same nine keys
  (symbol, interval, chart type, drawings, indicators, magnet, grid, volume,
  settings). Sharing it would have the two apps clobber each other.

On persistence there is a choice worth making deliberately. OpenAlgo keeps all
of this in localStorage, so a layout dies with a cache clear and never follows
the user to a second device. Their own watchlist code argues against that: "a
list built up over months is real work, and in the browser it dies with a cache
clear". This app already has SQLite and a session concept, so chart layout can
be server-side from the start for roughly the cost of the localStorage version.

---

## Part 6. Build order

Each step ends somewhere demonstrable.

| # | Step | Done when |
| --- | --- | --- |
| 1 | Route shell: extract chat body to a page, add route state, sidebar nav | /charts renders, chat still works |
| 2 | REST proxy and credentials endpoint | Candles reach the browser, key stays server-side |
| 3 | Chart mount: create, dispose, resize, StrictMode, theme tokens | Candles on screen, theme toggle clean |
| 4 | Symbol search, interval switcher, chart types | Chart usable by hand |
| 5 | Indicators: picker, generated settings dialogs, panes | supertrend 3,10 addable by mouse |
| 6 | Drawing tier: toolbar, properties panel, persistence | Channel drawable by hand |
| 7 | Live ticks | Last bar forms in real time |
| 8 | Agent skeleton: chart session state, `chart_command` end to end | "switch to 15 minute" works by prompt |
| 9 | Geometry library and its tests | Pivots and channel fits verified against known frames |
| 10 | Markup tools, group ids, clear | "draw the channel from 4446 to 3235" works |
| 11 | Analysis tools: trend, momentum, structure | "what's the trend" answers with numbers |
| 12 | Vision: canvas capture, image part | "analyse this screen" works |
| 13 | Status phrases, chips, reasoning timer | Matches the reference frames |

Steps 1 to 7 are a chart with no AI. Steps 8 to 13 are the analyst. The split is
deliberate: the chart has to be good on its own before an agent drives it.

---

## Part 7. Open questions

1. Live tick transport, Part 3.1. Needs deciding before step 7.
2. Whether the chat page and /charts share one session or hold separate
   conversations. Separate is simpler and probably right, but it means
   extracting the send and event-handling block into a reusable hook.
3. Whether v1 ships pattern detection beyond the pivot envelope, trendlines and
   support and resistance. The named patterns (head and shoulders, double tops,
   flags) are more code and less reliable, and all of them sit on the same pivot
   list, so they are additive rather than a rewrite.
4. The pivot significance default. It decides whether the envelope reads as
   structure or as noise, and it is the one number a user will want to tune.

## Part 8. Unrelated defect found while surveying

`getHealth` in `frontend/src/lib/api.ts` reads `payload.openalgo`, but the
backend returns `openalgo_connected`. `openalgoReachable` is therefore always
null and the "OpenAlgo server is not reachable" banner can never fire. Not part
of this work; recorded so it is not lost.
