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
 *
 * How an analyst command reaches the canvas: useAgentStream calls onChartCommand
 * when a chart_command frame lands, and each command is chained onto a single
 * promise queue whose head is the terminal's own init(). That serialises two
 * things at once. Commands can never interleave with each other, and a command
 * that arrives while the first history fetch is still in flight waits for the
 * chart instead of being dropped. The queue is rebuilt on every mount, so the
 * StrictMode double-mount cannot leave work chained behind a destroyed chart.
 *
 * Markup is applied the moment its frame lands, before a word of prose. The two
 * are independent streams and the panel is always the slower of them.
 *
 * Persistence uses the app's "oa-" prefix scoped to "oa-charts-". Not
 * "oa-trading-", which is OpenAlgo's own namespace for the same nine keys, and
 * sharing it would have the two applications clobber each other's layout.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { PanelRightOpen } from "lucide-react"
import ChartToolbar from "../components/charts/ChartToolbar"
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
import { useAgentStream } from "../lib/useAgentStream"
import { describeError } from "../lib/api"
import { cn } from "../lib/format"

/** The terminal's own localStorage namespace, kept clear of OpenAlgo's. */
const STORAGE_KEY = "oa-charts-"
const PANEL_KEY = "oa-charts-panel"
const VOLUME_KEY = "oa-charts-volume"
const SCALE_KEY = "oa-charts-price-scale"

type PriceScaleMode = Parameters<TerminalApi["setPriceScaleMode"]>[0]
type DrawToolGroups = ReturnType<TerminalApi["drawTools"]>

/** An open label editor. `placed` marks a drawing the user just created and has
 *  not named yet, which is the only case where cancelling removes it. */
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

/** The stream hands commands over as unknown[], because lib/sse.ts describes the
 *  frame and not this page's vocabulary. Shape is checked here and the op is not:
 *  the terminal ignores an op it does not know, so enumerating them at this
 *  boundary would only add a second place to forget one. */
function isChartCommand(value: unknown): value is ChartCommand {
  if (!value || typeof value !== "object") return false
  return typeof (value as { op?: unknown }).op === "string"
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
}

export default function ChartsPage({ onNavigate }: ChartsPageProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const legendRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<TerminalApi | null>(null)
  const aliveRef = useRef(true)
  /** One chain for every analyst command, rooted at init(). See the header. */
  const queueRef = useRef<Promise<void>>(Promise.resolve())

  const [symbol, setSymbol] = useState("")
  const [exchange, setExchange] = useState("")
  const [name, setName] = useState("")
  const [interval, setBarInterval] = useState("")
  const [intervals, setIntervals] = useState<IntervalGroups>(EMPTY_INTERVALS)
  const [chartType, setChartType] = useState("")
  const [chartTypes, setChartTypes] = useState<ChartTypeOption[]>([])
  const [wsState, setWsState] = useState<WsState>("connecting")
  const [lastPrice, setLastPrice] = useState<number | null>(null)

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

    // Annotated rather than inferred from the constructor, so this bag keeps its
    // types while terminal.ts is still being written beside it.
    const callbacks: TerminalCallbacks = {
      onReady(info) {
        if (!alive) return
        setIntervals(info.intervals)
        setBarInterval(info.interval)
        setChartType(info.chartType)
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
      },
      onViewChanged(view) {
        // Fires when the analyst changes the view rather than the toolbar. Without
        // it the interval control kept reading whatever it said at startup while
        // the chart underneath had already reloaded at a different timeframe.
        if (!alive) return
        setBarInterval(view.interval)
        setChartType(view.chartType)
      },
      onWsState(state) {
        if (alive) setWsState(state)
      },
      onLastPrice(price) {
        if (alive) setLastPrice(price)
      },
      onDrawState(state) {
        if (alive) setDrawState(state)
      },
      onDrawSelection(selection) {
        if (alive) setDrawSelection(selection)
      },
      onDrawTextEdit(request) {
        // A text tool that was just placed and is still empty. Cancelling it
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
    // history fetch waits for the chart instead of being dropped.
    queueRef.current = terminal.init().catch((error: unknown) => {
      if (alive) setToast({ message: describeError(error), tone: "error" })
    })

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
    const batch = commands.filter(isChartCommand)
    if (batch.length === 0) return
    queueRef.current = queueRef.current.then(async () => {
      for (const command of batch) {
        const api = terminalRef.current
        if (!api) return
        try {
          await api.apply(command)
        } catch (error) {
          // One bad op must not stall the rest of the batch or poison the chain.
          if (aliveRef.current) setToast({ message: describeError(error), tone: "error" })
        }
      }
    })
  }, [])

  // Called on every send. This is how the analyst learns which chart is open:
  // the reference interaction names no symbol, exchange, interval or date.
  const context = useCallback(() => terminalRef.current?.context() ?? null, [])

  const stream = useAgentStream({ onChartCommand, context })

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

  const handleInterval = useCallback((next: string) => {
    const api = terminalRef.current
    if (!api) return
    setBarInterval(next)
    void api.setInterval(next).catch((error: unknown) => {
      if (aliveRef.current) setToast({ message: describeError(error), tone: "error" })
    })
  }, [])

  const handleChartType = useCallback((next: string) => {
    const api = terminalRef.current
    if (!api) return
    setChartType(next)
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
  // the field opens empty and saving replaces whatever was there.
  const handleEditText = useCallback(() => {
    if (!drawSelection) return
    setTextEdit({ id: drawSelection.id, text: "", placed: false })
  }, [drawSelection])

  const commitText = useCallback((id: string, text: string) => {
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
        lastPrice={lastPrice}
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
                  if (event.key === "Enter") commitText(textEdit.id, textEdit.text)
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
                  onClick={() => commitText(textEdit.id, textEdit.text)}
                  className="rounded-lg bg-primary px-2.5 py-1 text-xs text-primary-foreground"
                >
                  Save
                </button>
              </div>
            </div>
          ) : null}

          {toast ? (
            <div
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
            symbol={symbol}
            interval={interval}
            onSend={(text) => void stream.send(text)}
            onStop={stream.stop}
            onReset={stream.reset}
            onCollapse={() => setPanelOpen(false)}
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
