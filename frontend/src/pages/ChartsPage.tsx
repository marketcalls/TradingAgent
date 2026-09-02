/** The /charts page: a chart terminal with an AI analyst beside it.
 *
 * This file owns exactly one hard thing, the seam between a canvas engine that
 * runs its own animation loop and a React tree that must not know about it.
 * Every rule below was verified against openalgo-charts 1.9.2 and its React
 * integration notes, and each one has a failure mode that is silent:
 *
 *   - The terminal lives in a ref, never in state. It is a mutable object graph
 *     with a running rAF loop and a 60 fps tick stream; putting it in state gives
 *     React a value it will try to compare and schedules a render per swap.
 *   - Creation and destruction share ONE effect with a returned cleanup, and the
 *     ref is nulled in that cleanup. React 19 StrictMode mounts, unmounts and
 *     remounts every effect in development, and only a complete pair survives it.
 *     Splitting them across two effects leaves a live chart with no owner.
 *   - Callbacks read terminalRef.current, never the local `terminal`. The
 *     callback bag is built as a constructor argument, so at the moment those
 *     bodies are written the local is still undefined.
 *   - Every async path is guarded by a liveness flag that is re-checked after the
 *     await, because init() awaits the network and the page can unmount mid-flight.
 *   - The chart owns its container element outright: the engine writes inline
 *     styles, ARIA attributes and a live region onto it, and destroy() does not
 *     undo them. It therefore gets its own div, absolutely positioned so it has a
 *     resolved pixel height. A bare height:100% inside an auto-height flex parent
 *     measures zero and the chart draws nothing at all.
 *   - There is no window resize listener and no width/height prop. The engine
 *     installs its own ResizeObserver in the constructor.
 *   - The OHLC readout is a DOM node the terminal writes into directly, passed as
 *     legendEl. Routing the crosshair through setState would re-render this page
 *     on every pointer move.
 *   - The last price is not page state either. It ticks up to four times a
 *     second, and as state here every tick re-rendered the analyst column and
 *     re-parsed every markdown answer in it. The toolbar's readout subscribes
 *     through a callback and owns the only state that changes on a tick.
 *   - The interval and chart type in the toolbar are set from onViewChanged
 *     alone, never optimistically in the click handler. The terminal can refuse
 *     an interval the broker does not offer, and a toolbar that had already
 *     switched was then lying over a chart that had not.
 *
 * How an analyst command reaches the canvas: useAgentStream calls onChartCommand
 * when a chart_command frame lands, and each command is chained onto a single
 * promise queue whose head is the terminal's own init(). That serialises two
 * things at once. Commands can never interleave with each other, and a command
 * that arrives while the first history fetch is still in flight waits for the
 * chart instead of being dropped. The queue is rebuilt on every mount, so the
 * StrictMode double-mount cannot leave work chained behind a destroyed chart,
 * and a continuation checks it is still talking to the terminal it was queued
 * for before it touches one. Every link, init included, is held to a timeout:
 * a serial chain with no timeout is poisoned by one stalled fetch, and every
 * command in every later turn then waits behind it for ever.
 *
 * Markup is applied the moment its frame lands, before a word of prose. The two
 * are independent streams and the panel is always the slower of them.
 *
 * The page also keeps two things the analyst column cannot, because the column
 * unmounts when it is collapsed: the per-turn "Thought for" readings, and the
 * view generation. The generation counts symbol and view changes, and the chips
 * that act on "the structure you just drew" are withheld once the chart has
 * moved on from the one they were drawn on.
 *
 * Persistence uses the app's "oa-" prefix scoped to "oa-charts-". Not
 * "oa-trading-", which is OpenAlgo's own namespace for the same nine keys, and
 * sharing it would have the two applications clobber each other's layout.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { PanelRightOpen } from "lucide-react"
import ChartToolbar, {
  type PriceListener,
  type PriceSubscribe
} from "../components/charts/ChartToolbar"
import SymbolSearchDialog from "../components/charts/SymbolSearchDialog"
import DrawingRail from "../components/charts/DrawingRail"
import DrawingStyleBar from "../components/charts/DrawingStyleBar"
import IndicatorPickerDialog from "../components/charts/IndicatorPickerDialog"
import IndicatorSettingsDialog from "../components/charts/IndicatorSettingsDialog"
import AnalystPanel from "../components/charts/AnalystPanel"
import type { Route } from "../components/Sidebar"
import { ChartTerminal } from "../lib/charts/terminal"
import type {
  ChartTypeOption,
  DrawSelection,
  DrawState,
  IndicatorOption,
  IndicatorSettingsRequest,
  IntervalGroups,
  SymbolRow,
  TerminalApi,
  TerminalCallbacks,
  WsState
} from "../lib/charts/terminal-api"
import type { ChartCommand, ChartIndicator } from "../lib/charts/types"
import type { ChatMessage } from "../lib/sse"
import { useAgentStream } from "../lib/useAgentStream"
import { describeError } from "../lib/api"
import { cn } from "../lib/format"

/** The terminal's own localStorage namespace, kept clear of OpenAlgo's. */
const STORAGE_KEY = "oa-charts-"
const PANEL_KEY = "oa-charts-panel"
const VOLUME_KEY = "oa-charts-volume"
const SCALE_KEY = "oa-charts-price-scale"

/** How long one link of the command chain may take before the chain moves on.
 *  Generous, because a symbol change is a history fetch; not unbounded, because
 *  the chain is serial and one stalled link holds every later one. */
const COMMAND_TIMEOUT_MS = 20_000

type PriceScaleMode = Parameters<TerminalApi["setPriceScaleMode"]>[0]
type DrawToolGroups = ReturnType<TerminalApi["drawTools"]>

/** An open label editor. `placed` marks a drawing the user just created and has
 *  not named yet, which is the only case where an empty commit removes it. */
interface TextEdit {
  id: string
  text: string
  placed: boolean
}

const EMPTY_INTERVALS: IntervalGroups = {
  seconds: [],
  minutes: [],
  hours: [],
  days: [],
  weeks: [],
  months: []
}

const EMPTY_DRAW_STATE: DrawState = {
  activeTool: null,
  canUndo: false,
  canRedo: false,
  magnet: false,
  count: 0
}

/** Settles as `work` does, or rejects once `ms` has passed. A late settlement
 *  of `work` is then observed and discarded, never surfaced twice. */
function withTimeout<T>(work: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(
      () => reject(new Error(`${what} did not finish within ${Math.round(ms / 1000)}s`)),
      ms
    )
    work.then(
      (value) => {
        window.clearTimeout(timer)
        resolve(value)
      },
      (error: unknown) => {
        window.clearTimeout(timer)
        reject(error)
      }
    )
  })
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value)
}

/** The stream hands commands over as unknown[], because lib/sse.ts describes the
 *  frame and not this page's vocabulary. Each op's required fields are checked
 *  here, because a frame with a known op and a missing field reached the engine
 *  and the raw TypeError was toasted at the user. A frame that fails is dropped
 *  with a console.warn, and an op this build does not know is treated the same:
 *  the terminal would ignore it, and the warning says why nothing happened. */
function isChartCommand(value: unknown): value is ChartCommand {
  if (!value || typeof value !== "object") return false
  const candidate = value as Record<string, unknown>
  switch (candidate.op) {
    case "draw":
      return typeof candidate.group === "string" && Array.isArray(candidate.shapes)
    case "clear":
      return candidate.group === undefined || typeof candidate.group === "string"
    case "set_symbol":
      return typeof candidate.symbol === "string" && typeof candidate.exchange === "string"
    case "set_interval":
      return typeof candidate.interval === "string"
    case "set_chart_type":
      return typeof candidate.chartType === "string"
    case "add_indicator":
      return typeof candidate.indicatorId === "string"
    case "remove_indicator":
      return typeof candidate.instanceId === "string" || typeof candidate.indicatorId === "string"
    case "focus":
      return isFiniteNumber(candidate.from) && isFiniteNumber(candidate.to)
    default:
      return false
  }
}

/** Found by scanning back rather than taking length - 1, so the timing survives
 *  a hook that appends the user turn without an empty assistant turn beside it. */
function lastAssistantIndex(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "assistant") return index
  }
  return -1
}

/** The theme is a class on <html>, exactly as App.tsx sets it. */
function readTheme(): "dark" | "light" {
  return document.documentElement.classList.contains("dark") ? "dark" : "light"
}

function readFlag(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key)
    return raw === null ? fallback : raw === "1"
  } catch {
    // Private browsing can refuse storage; the default still applies for this tab.
    return fallback
  }
}

function readScaleMode(): PriceScaleMode {
  try {
    const raw = localStorage.getItem(SCALE_KEY)
    if (raw === "logarithmic" || raw === "percentage" || raw === "indexed-to-100") return raw
  } catch {
    // Same as above: an unreadable store is not an error, it is a default.
  }
  return "linear"
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Nothing here is worth failing a render over.
  }
}

interface ChartsPageProps {
  /** Given by the host when it can route away from /charts. Without it, chips
   *  that would navigate are not offered rather than being offered and dead.
   *  App.tsx's own `navigate` is exactly this shape and can be passed verbatim. */
  onNavigate?: (target: Route) => void
  /** False while the host keeps this page mounted but hidden. Keyboard
   *  shortcuts and the price readout stand down until it is shown again. */
  active?: boolean
}

export default function ChartsPage({ onNavigate, active = true }: ChartsPageProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const legendRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<TerminalApi | null>(null)
  const aliveRef = useRef(true)
  /** One chain for every analyst command, rooted at init(). See the header. */
  const queueRef = useRef<Promise<void>>(Promise.resolve())

  const [chartReady, setChartReady] = useState(false)
  const [symbol, setSymbol] = useState("")
  const [exchange, setExchange] = useState("")
  const [name, setName] = useState("")
  const [interval, setBarInterval] = useState("")
  const [intervals, setIntervals] = useState<IntervalGroups>(EMPTY_INTERVALS)
  const [chartType, setChartType] = useState("")
  const [chartTypes, setChartTypes] = useState<ChartTypeOption[]>([])
  const [wsState, setWsState] = useState<WsState>("connecting")

  // Bumped on every symbol load and view change. The chips born from a turn
  // remember the generation they were born at; see the header.
  const [viewGeneration, setViewGeneration] = useState(0)
  const generationRef = useRef(0)

  const [volumeVisible, setVolumeVisible] = useState(() => readFlag(VOLUME_KEY, true))
  const [priceScaleMode, setPriceScaleMode] = useState<PriceScaleMode>(readScaleMode)

  const [drawGroups, setDrawGroups] = useState<DrawToolGroups>([])
  const [drawState, setDrawState] = useState<DrawState>(EMPTY_DRAW_STATE)
  const [drawSelection, setDrawSelection] = useState<DrawSelection | null>(null)
  const [textEdit, setTextEdit] = useState<TextEdit | null>(null)

  const [indicators, setIndicators] = useState<ChartIndicator[]>([])
  const [catalogue, setCatalogue] = useState<IndicatorOption[]>([])
  const [pickerOpen, setPickerOpen] = useState(false)
  const [settingsRequest, setSettingsRequest] = useState<IndicatorSettingsRequest | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)

  const [panelOpen, setPanelOpen] = useState(() => readFlag(PANEL_KEY, true))
  const [toast, setToast] = useState<{ message: string; tone: "info" | "error" } | null>(null)

  // The price never enters this component's state. The terminal writes it to
  // a ref and forwards it to whichever readout is currently subscribed.
  const lastPriceRef = useRef<number | null>(null)
  const priceListenerRef = useRef<PriceListener | null>(null)
  const subscribePrice = useCallback<PriceSubscribe>((listener) => {
    priceListenerRef.current = listener
    listener(lastPriceRef.current)
    return () => {
      if (priceListenerRef.current === listener) priceListenerRef.current = null
    }
  }, [])

  // The ready payload carries neither the volume flag nor the price scale mode, so
  // the page asserts its own remembered values once the chart exists rather than
  // letting the toolbar display a guess the chart disagrees with. Refs, so the
  // lifecycle effect below stays free of data dependencies.
  const volumeRef = useRef(volumeVisible)
  const scaleRef = useRef(priceScaleMode)
  useEffect(() => {
    volumeRef.current = volumeVisible
    write(VOLUME_KEY, volumeVisible ? "1" : "0")
  }, [volumeVisible])
  useEffect(() => {
    scaleRef.current = priceScaleMode
    write(SCALE_KEY, priceScaleMode)
  }, [priceScaleMode])
  useEffect(() => {
    write(PANEL_KEY, panelOpen ? "1" : "0")
  }, [panelOpen])

  // Re-assigned on mount, not only at module init: StrictMode remounts this page
  // and the second mount must not inherit the first one's teardown.
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  // The chart: created and destroyed in one effect, with the instance in a ref.
  useEffect(() => {
    const container = containerRef.current
    const legendEl = legendRef.current
    if (!container || !legendEl) return
    let alive = true

    const bumpGeneration = () => {
      generationRef.current += 1
      setViewGeneration(generationRef.current)
    }

    // Annotated rather than inferred from the constructor, so this bag keeps its
    // types while terminal.ts is still being written beside it.
    const callbacks: TerminalCallbacks = {
      onReady(info) {
        if (!alive) return
        setIntervals(info.intervals)
        setBarInterval(info.interval)
        setChartType(info.chartType)
        setChartReady(true)
        // Read through the ref: the local is still undefined in this body.
        const api = terminalRef.current
        if (!api) return
        setChartTypes(api.chartTypes())
        setDrawGroups(api.drawTools())
        api.setVolumeVisible(volumeRef.current)
        api.setPriceScaleMode(scaleRef.current)
      },
      onSymbolLoaded(sym) {
        if (!alive) return
        setSymbol(sym.symbol)
        setExchange(sym.exchange)
        setName(sym.name ?? "")
        bumpGeneration()
      },
      onViewChanged(view) {
        // Fires on every interval or chart type change, whether the toolbar or
        // the analyst asked for it, and only once the terminal has accepted it.
        // It is the only place the toolbar's interval and type are set.
        if (!alive) return
        setBarInterval(view.interval)
        setChartType(view.chartType)
        bumpGeneration()
      },
      onWsState(state) {
        if (alive) setWsState(state)
      },
      onLastPrice(price) {
        if (!alive) return
        lastPriceRef.current = price
        priceListenerRef.current?.(price)
      },
      onDrawState(state) {
        if (alive) setDrawState(state)
      },
      onDrawSelection(selection) {
        if (alive) setDrawSelection(selection)
      },
      onDrawTextEdit(request) {
        // A text tool that was just placed and is still empty. Leaving it empty
        // removes the drawing, because an empty label is invisible clutter the
        // user cannot then select to delete.
        if (alive) setTextEdit({ id: request.id, text: request.text, placed: true })
      },
      onIndicators(list) {
        if (alive) setIndicators(list)
      },
      onIndicatorSettings(request) {
        if (alive) setSettingsRequest(request)
      },
      onToast(message, tone) {
        if (alive) setToast({ message, tone })
      }
    }

    const terminal = new ChartTerminal({
      container,
      legendEl,
      storageKey: STORAGE_KEY,
      getTheme: readTheme,
      callbacks
    })

    terminalRef.current = terminal
    // The queue is rooted at init so a command that arrives during the first
    // history fetch waits for the chart instead of being dropped. Held to the
    // same timeout as a command: a stalled config or intervals fetch otherwise
    // held every command of every turn behind it.
    queueRef.current = withTimeout(terminal.init(), COMMAND_TIMEOUT_MS, "loading the chart").catch(
      (error: unknown) => {
        if (alive) setToast({ message: describeError(error), tone: "error" })
      }
    )

    return () => {
      alive = false
      terminal.destroy()
      terminalRef.current = null
      queueRef.current = Promise.resolve()
    }
  }, [])

  // The theme is a class on <html> owned by the shell, so the chart is told to
  // re-tint by watching that attribute. Canvas colours are literal strings: a
  // swap that is not reported leaves the chart on the previous palette.
  useEffect(() => {
    const observer = new MutationObserver(() => terminalRef.current?.applyTheme())
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 4000)
    return () => window.clearTimeout(timer)
  }, [toast])

  const onChartCommand = useCallback((commands: unknown[]) => {
    const batch: ChartCommand[] = []
    for (const command of commands) {
      if (isChartCommand(command)) batch.push(command)
      else console.warn("chart_command dropped: malformed frame", command)
    }
    if (batch.length === 0) return
    // The continuation runs later, possibly after a remount has replaced the
    // terminal. It is bound to the instance it was queued for, and steps aside
    // if the ref now names another one.
    const owner = terminalRef.current
    if (!owner) return
    queueRef.current = queueRef.current.then(async () => {
      for (const command of batch) {
        if (terminalRef.current !== owner) return
        try {
          await withTimeout(owner.apply(command), COMMAND_TIMEOUT_MS, `the ${command.op} command`)
        } catch (error) {
          // One bad or stalled op must not stall the rest of the batch or poison
          // the chain: a timeout is caught here exactly like a rejection.
          if (aliveRef.current && terminalRef.current === owner) {
            setToast({ message: describeError(error), tone: "error" })
          }
        }
      }
    })
  }, [])

  // Called on every send. This is how the analyst learns which chart is open:
  // the reference interaction names no symbol, exchange, interval or date.
  const context = useCallback(() => terminalRef.current?.context() ?? null, [])

  const stream = useAgentStream({ onChartCommand, context })

  // Read through a ref so the callback the panel receives never changes and the
  // panel's memo holds. The hook's send is itself stable, but this does not
  // depend on that staying true.
  const sendRef = useRef(stream.send)
  sendRef.current = stream.send
  const handleSend = useCallback((text: string) => {
    void sendRef.current(text)
  }, [])

  const handleCollapse = useCallback(() => setPanelOpen(false), [])

  // The "Thought for" clock. Started on the first sign of a run, stopped when
  // the answer begins or the run ends without one, kept per turn index so a
  // finished turn goes on showing its own reading once the next one starts.
  // It lives here and not in the panel because the panel unmounts on collapse.
  const lastAssistant = useMemo(() => lastAssistantIndex(stream.messages), [stream.messages])
  const answered = lastAssistant >= 0 && Boolean(stream.messages[lastAssistant].content)
  const startedRef = useRef<number | null>(null)
  const [seconds, setSeconds] = useState<Record<number, number>>({})

  useEffect(() => {
    if (stream.running && startedRef.current === null) startedRef.current = Date.now()
  }, [stream.running])

  useEffect(() => {
    const started = startedRef.current
    if (started === null) return
    if (stream.running && !answered) return
    startedRef.current = null
    if (lastAssistant < 0) return
    const elapsed = Math.max(0, Math.round((Date.now() - started) / 1000))
    setSeconds((current) =>
      current[lastAssistant] === undefined ? { ...current, [lastAssistant]: elapsed } : current
    )
  }, [stream.running, answered, lastAssistant])

  // A new thread drops every measurement with the transcript it belonged to.
  useEffect(() => {
    if (stream.messages.length === 0) {
      startedRef.current = null
      setSeconds({})
    }
  }, [stream.messages.length])

  // The chips born from a finished turn belong to the view as it was when the
  // run ended. Once the chart moves on, "project a target from the structure
  // you just drew" is about a chart the user is no longer looking at.
  const [chipGeneration, setChipGeneration] = useState(0)
  useEffect(() => {
    if (!stream.running) setChipGeneration(generationRef.current)
  }, [stream.running])
  const chipsStale = chipGeneration !== viewGeneration

  const handleSearch = useCallback(
    (query: string, withinExchange?: string) =>
      terminalRef.current?.searchSymbols(query, withinExchange) ?? Promise.resolve([]),
    []
  )

  const handlePickSymbol = useCallback((row: SymbolRow) => {
    setSearchOpen(false)
    const api = terminalRef.current
    if (!api) return
    void api.setSymbol(row.symbol, row.exchange).catch((error: unknown) => {
      if (aliveRef.current) setToast({ message: describeError(error), tone: "error" })
    })
  }, [])

  // Neither handler touches the toolbar's state: onViewChanged does, once the
  // terminal has accepted the change. See the header.
  const handleInterval = useCallback((next: string) => {
    const api = terminalRef.current
    if (!api) return
    void api.setInterval(next).catch((error: unknown) => {
      if (aliveRef.current) setToast({ message: describeError(error), tone: "error" })
    })
  }, [])

  const handleChartType = useCallback((next: string) => {
    const api = terminalRef.current
    if (!api) return
    void api.setChartType(next).catch((error: unknown) => {
      if (aliveRef.current) setToast({ message: describeError(error), tone: "error" })
    })
  }, [])

  const handleToggleVolume = useCallback(() => {
    setVolumeVisible((current) => {
      const next = !current
      terminalRef.current?.setVolumeVisible(next)
      return next
    })
  }, [])

  const handlePriceScaleMode = useCallback((mode: PriceScaleMode) => {
    setPriceScaleMode(mode)
    terminalRef.current?.setPriceScaleMode(mode)
  }, [])

  const handleResetScale = useCallback(() => {
    terminalRef.current?.resetScale()
  }, [])

  const handleOpenIndicators = useCallback(() => {
    setPickerOpen(true)
    const api = terminalRef.current
    if (!api) return
    // Re-read on every open: a lazily imported tier can grow the registry, and
    // the count is never taken from prose.
    void api
      .indicatorCatalogue()
      .then((list) => {
        if (aliveRef.current) setCatalogue(list)
      })
      .catch((error: unknown) => {
        if (aliveRef.current) setToast({ message: describeError(error), tone: "error" })
      })
  }, [])

  const handleAddIndicator = useCallback((id: string) => {
    const api = terminalRef.current
    if (!api) return
    void api.addIndicator(id).catch((error: unknown) => {
      if (aliveRef.current) setToast({ message: describeError(error), tone: "error" })
    })
  }, [])

  const handleRemoveIndicator = useCallback((instanceId: string) => {
    terminalRef.current?.removeIndicator(instanceId)
  }, [])

  const handleApplySettings = useCallback(
    (patch: Record<string, unknown>) => {
      if (!settingsRequest) return
      terminalRef.current?.applyIndicatorSettings(settingsRequest.instanceId, patch)
    },
    [settingsRequest]
  )

  const handleResetSettings = useCallback(() => {
    if (!settingsRequest) return
    void terminalRef.current?.resetIndicatorSettings(settingsRequest.instanceId)
  }, [settingsRequest])

  const handlePickTool = useCallback((toolId: string | null) => {
    void terminalRef.current?.setDrawTool(toolId)
  }, [])

  const handleToggleMagnet = useCallback(() => {
    terminalRef.current?.setMagnet(!drawState.magnet)
  }, [drawState.magnet])

  const handleUpdateDrawing = useCallback(
    (patch: Record<string, unknown>) => {
      if (!drawSelection) return
      terminalRef.current?.updateDrawing(drawSelection.id, patch)
    },
    [drawSelection]
  )

  const handleRemoveDrawing = useCallback(() => {
    if (!drawSelection) return
    terminalRef.current?.removeDrawing(drawSelection.id)
  }, [drawSelection])

  // The style bar's edit path has no way to read the label back off the chart, so
  // the field opens empty. Saving text replaces whatever was there; saving
  // nothing keeps it, since an existing label has text worth keeping.
  const handleEditText = useCallback(() => {
    if (!drawSelection) return
    setTextEdit({ id: drawSelection.id, text: "", placed: false })
  }, [drawSelection])

  const commitText = useCallback((id: string, text: string, placed: boolean) => {
    if (text.trim().length === 0) {
      // An empty label is invisible and cannot be hit to delete. A drawing
      // that was just placed goes; an existing one keeps the text it had.
      if (placed) terminalRef.current?.removeDrawing(id)
      setTextEdit(null)
      return
    }
    terminalRef.current?.updateDrawing(id, { text })
    setTextEdit(null)
  }, [])

  const cancelText = useCallback((id: string, placed: boolean) => {
    if (placed) terminalRef.current?.removeDrawing(id)
    setTextEdit(null)
  }, [])

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <ChartToolbar
        symbol={symbol}
        exchange={exchange}
        name={name}
        interval={interval}
        intervals={intervals}
        chartType={chartType}
        chartTypes={chartTypes}
        wsState={wsState}
        subscribePrice={subscribePrice}
        active={active}
        volumeVisible={volumeVisible}
        priceScaleMode={priceScaleMode}
        onOpenSymbolSearch={() => setSearchOpen(true)}
        onInterval={handleInterval}
        onChartType={handleChartType}
        onOpenIndicators={handleOpenIndicators}
        onToggleVolume={handleToggleVolume}
        onResetScale={handleResetScale}
        onPriceScaleMode={handlePriceScaleMode}
      />

      <div className="flex min-h-0 flex-1">
        <DrawingRail
          groups={drawGroups}
          state={drawState}
          active={active}
          onPickTool={handlePickTool}
          onUndo={() => terminalRef.current?.undo()}
          onRedo={() => terminalRef.current?.redo()}
          onToggleMagnet={handleToggleMagnet}
          onClearAll={() => terminalRef.current?.clearDrawings()}
        />

        <div className="relative min-w-0 flex-1">
          {/* The engine owns this element outright: it writes inline styles, ARIA
              attributes and a live region onto it, and destroy() leaves them. */}
          <div ref={containerRef} className="absolute inset-0" />

          {/* The terminal writes the OHLC readout straight into this node. */}
          <div
            ref={legendRef}
            className="pointer-events-none absolute left-3 top-2 z-10 max-w-[55%] overflow-hidden whitespace-nowrap font-mono text-[11px] text-muted-foreground"
          />

          {drawSelection ? (
            <div className="absolute left-1/2 top-2 z-20 -translate-x-1/2">
              <DrawingStyleBar
                selection={drawSelection}
                onUpdate={handleUpdateDrawing}
                onRemove={handleRemoveDrawing}
                onEditText={handleEditText}
              />
            </div>
          ) : null}

          {textEdit ? (
            <div className="absolute left-1/2 top-16 z-30 w-[280px] -translate-x-1/2 rounded-xl border border-border bg-background p-2 shadow-l">
              <label className="block text-xs text-muted-foreground" htmlFor="oa-charts-text">
                Label for this drawing
              </label>
              <input
                id="oa-charts-text"
                type="text"
                autoFocus
                value={textEdit.text}
                onChange={(event) => {
                  const next = event.target.value
                  setTextEdit((current) => (current ? { ...current, text: next } : current))
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") commitText(textEdit.id, textEdit.text, textEdit.placed)
                  if (event.key === "Escape") cancelText(textEdit.id, textEdit.placed)
                }}
                className="mt-1 w-full rounded-lg border border-input bg-background px-2 py-1 text-sm outline-none focus:border-primary"
              />
              <div className="mt-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => cancelText(textEdit.id, textEdit.placed)}
                  className="rounded-lg border border-input px-2.5 py-1 text-xs"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => commitText(textEdit.id, textEdit.text, textEdit.placed)}
                  className="rounded-lg bg-primary px-2.5 py-1 text-xs text-primary-foreground"
                >
                  Save
                </button>
              </div>
            </div>
          ) : null}

          {toast ? (
            <div
              role={toast.tone === "error" ? "alert" : "status"}
              aria-live={toast.tone === "error" ? "assertive" : "polite"}
              className={cn(
                "absolute bottom-10 left-1/2 z-30 -translate-x-1/2 rounded-lg border bg-background px-3 py-1.5 text-xs shadow-l",
                toast.tone === "error" ? "border-danger-border text-danger" : "border-border text-muted-foreground"
              )}
            >
              {toast.message}
            </div>
          ) : null}
        </div>

        {panelOpen ? (
          <AnalystPanel
            messages={stream.messages}
            running={stream.running}
            ready={chartReady}
            symbol={symbol}
            interval={interval}
            lastAssistant={lastAssistant}
            seconds={seconds}
            chipsStale={chipsStale}
            onSend={handleSend}
            onStop={stream.stop}
            onReset={stream.reset}
            onCollapse={handleCollapse}
            onNavigate={onNavigate}
          />
        ) : (
          <div className="flex w-9 shrink-0 flex-col items-center border-l border-border bg-sidebar py-2">
            <button
              type="button"
              onClick={() => setPanelOpen(true)}
              className="flex h-7 w-7 items-center justify-center rounded-lg border border-border hover:bg-muted"
              aria-label="Open the analyst panel"
              title="Analyst"
            >
              <PanelRightOpen className="h-3.5 w-3.5 shrink-0" />
            </button>
          </div>
        )}
      </div>

      <SymbolSearchDialog
        open={searchOpen}
        onSearch={handleSearch}
        onPick={handlePickSymbol}
        onClose={() => setSearchOpen(false)}
      />

      <IndicatorPickerDialog
        open={pickerOpen}
        catalogue={catalogue}
        active={indicators}
        onPick={handleAddIndicator}
        onRemove={handleRemoveIndicator}
        onClose={() => setPickerOpen(false)}
      />

      <IndicatorSettingsDialog
        request={settingsRequest}
        onApply={handleApplySettings}
        onReset={handleResetSettings}
        onClose={() => setSettingsRequest(null)}
      />
    </div>
  )
}
