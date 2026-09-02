/** The chart engine, as a plain class React never looks inside.
 *
 * There is no React import in this file and there must never be one. The engine
 * is a mutable object graph with a running animation loop, and a quote stream
 * pushes a new price several times a second; routing either through setState
 * would re-render the page at tick rate and ask React to diff an object it
 * cannot compare. Only the low-frequency, UI-shaped events in TerminalCallbacks
 * cross back over, and the OHLC readout does not even do that: it is written
 * straight into a DOM node this class owns.
 *
 * Facts about openalgo-charts 1.9.2 that decide the structure below. Every one
 * is a silent failure, not an exception, which is why each is named where it
 * applies rather than left to a reader to rediscover:
 *
 *   - chart.destroy() does NOT destroy a DrawingController. It has to be
 *     destroyed by hand, FIRST, or a leaked one can strand placement mode on and
 *     leave the next chart in that container un-pannable.
 *   - A chart rebuild is a full teardown. Drawings, indicators, the volume
 *     preference and the price-scale mode all belong to the chart that just went
 *     away, so they are snapshotted before and re-applied in the rebuild tail,
 *     indicators first: a drawing on an indicator pane names that pane by index
 *     and addPrimitive creates the pane it names, so restoring drawings first
 *     conjured an empty pane and pushed the re-added indicator below it.
 *   - Only a chart-type change rebuilds. setTheme is a live swap and an interval
 *     change is a data reload, and all three keep the user's zoom: the visible
 *     span and the gap past the last bar are captured before the data moves and
 *     put back after it lands. Only the first load and a symbol change frame
 *     the viewport afresh.
 *   - setHistoryLoader latches after one call and re-arms only on
 *     historyLoadComplete, so every exit from the pager reports back or
 *     scroll-back paging stops for the rest of the session, silently.
 *   - The draw tier is a lazy chunk. The await is a real suspension point: this
 *     terminal can be destroyed while the chunk is in flight, so the destroyed
 *     flag is re-checked after it.
 *   - DEFAULT_THEME is lightTheme despite the ChartOptions JSDoc saying dark, so
 *     the theme is passed explicitly on creation and again on every swap.
 *   - Drawings never expand the price scale (their autoscaleInfo returns null),
 *     so markup drawn off-screen stays off-screen. "focus" moves the time window
 *     and re-arms autoscale, which is the whole remedy the engine offers. The
 *     engine drops setVisibleLogicalRange at width 0, so a focus that lands
 *     while the page is hidden is held and applied on the next real resize.
 *   - withBarCache serves CLOSED bars only: it strips the forming bar on store,
 *     and a hit inside the TTL is short by that bar. The primary series is
 *     therefore always fetched fresh; the cache fronts scroll-back paging alone,
 *     where every bar is immutable.
 *   - The tick stream is not the whole truth. A Quote frame carries the day's
 *     cumulative volume, which the builder diffs per bar, but a missed tick, a
 *     hidden tab or a relay reconnect leaves closed bars that were never built.
 *     A history reconcile every 25 to 35 seconds (jittered so a fleet of tabs
 *     does not poll in step), and at once after every stream restart, snaps the
 *     closed bars to the broker's OHLC and volume and re-seeds the builder for
 *     the forming bar. The builder folds every later tick into its own copy of
 *     that bar, so a correction written to rawBars alone is gone on the next tick.
 *   - DrawingController pushes one undo snapshot per add, update and remove,
 *     and 1.9.2 has no batch API. Analyst maintenance therefore costs the user
 *     undo entries; the history limit is raised to absorb it.
 *   - DrawingController keys its layers by pane index and its constructor
 *     restores the chart's own drawings slot. Removing an indicator's pane
 *     renumbers the panes underneath both, so the controller is rebuilt with
 *     the indices moved down, and the slot is written first or the constructor
 *     conjures an empty pane from the stale index.
 *   - DrawingController.add does no duplicate-id check: a second drawing under
 *     an id already on the chart is drawn beside the first. Appending to an
 *     analyst group (the "notes" group grows one label per call) therefore
 *     numbers its new ids after the ones already there, never from zero.
 *   - The built-in bollinger declares no fills where every other band indicator
 *     does, so "shade the bands" was refused. indicatorOverrides registers a
 *     fill-enabled copy under the same id, and it runs the moment the
 *     indicators tier loads and before any catalogue read, so the picker, the
 *     persisted layouts and the analyst all see one bollinger.
 */

import {
  INDICATOR_SOURCES,
  compactVolume,
  createChart,
  getIndicator,
  hasIndicator,
  indicatorDefaults,
  indicatorStyleInputs,
  isKnownInterval,
  precisionForStep,
  registeredIndicators,
  withBarCache,
  type Bar,
  type Chart,
  type IndicatorInput,
  type PriceScaleMode,
  type SeriesApi,
  type SeriesStyle,
  type SeriesType,
  type UnsubscribeFn
} from "openalgo-charts"
import type { Drawing, DrawingController, DrawingStyle } from "openalgo-charts/draw"
import type { ISeriesTransform } from "openalgo-charts/transform"
import { formatNumber } from "../format"
import { annotationSpan, buildAnnotations, inGroup, nextGroupIndex } from "./annotations"
import {
  ProxyDataFeed,
  OaSocket,
  getIntervals,
  getOaConfig,
  getQuote,
  getSymbolInfo,
  nowSec,
  searchSymbols,
  socketUrl,
  type SymbolInfo
} from "./feed"
import { applyIndicatorOverrides } from "./indicatorOverrides"
import {
  DEFAULT_INTERVAL,
  emptyIntervals,
  flattenIntervals,
  historyRange,
  intervalSeconds,
  lookbackDays,
  pickInterval
} from "./intervals"
import {
  legendColors,
  readChartTheme,
  tonePalette,
  volumeColor,
  type LegendColors
} from "./theme"
import type {
  ChartTypeOption,
  DrawSelection,
  IndicatorOption,
  IntervalGroups,
  SettingsField,
  SymbolRow,
  TerminalApi,
  TerminalCallbacks,
  TerminalOptions,
  WsState
} from "./terminal-api"
import {
  isAiDrawing,
  type AnnotationShape,
  type ChartCommand,
  type ChartContext,
  type ChartIndicator
} from "./types"

type DrawTier = typeof import("openalgo-charts/draw")
type TransformTier = typeof import("openalgo-charts/transform")

type TransformKind =
  | "heikin-ashi"
  | "renko"
  | "range"
  | "line-break"
  | "point-figure"
  | "kagi"

interface ChartTypeDef {
  value: string
  label: string
  series: SeriesType
  /** Movement-driven types re-bucket the bars before the renderer sees them. */
  transform?: TransformKind
  /** baseline needs a baseValue, which is data and so cannot be a constant. */
  baseline?: boolean
  /** histogram is the one series colour ChartTheme cannot reach. */
  themedColor?: boolean
}

/** The switcher's catalogue. Thirteen types are in the base registry; the six
 *  derived ones need openalgo-charts/transform, which also registers the
 *  point-figure and kagi renderers as a side effect of being imported. */
const CHART_TYPES: ChartTypeDef[] = [
  { value: "candlestick", label: "Candles", series: "candlestick" },
  { value: "hollow-candle", label: "Hollow Candles", series: "hollow-candle" },
  { value: "volume-candle", label: "Volume Candles", series: "volume-candle" },
  { value: "bar", label: "Bars (OHLC)", series: "bar" },
  { value: "high-low", label: "High-Low", series: "high-low" },
  { value: "line", label: "Line", series: "line" },
  { value: "line-markers", label: "Line with Markers", series: "line-markers" },
  { value: "step", label: "Step", series: "step" },
  { value: "area", label: "Area", series: "area" },
  { value: "hlc-area", label: "HLC Area", series: "hlc-area" },
  { value: "baseline", label: "Baseline", series: "baseline", baseline: true },
  { value: "column", label: "Column", series: "column", themedColor: true },
  { value: "histogram", label: "Histogram", series: "histogram", themedColor: true },
  { value: "heikin-ashi", label: "Heikin Ashi", series: "candlestick", transform: "heikin-ashi" },
  { value: "renko", label: "Renko", series: "candlestick", transform: "renko" },
  { value: "range", label: "Range Bars", series: "candlestick", transform: "range" },
  { value: "line-break", label: "Line Break", series: "candlestick", transform: "line-break" },
  {
    value: "point-figure",
    label: "Point and Figure",
    series: "point-figure",
    transform: "point-figure"
  },
  { value: "kagi", label: "Kagi", series: "kagi", transform: "kagi" }
]

/** Toolbar grouping for the 51 registered drawing tools. Any id the registry
 *  reports that is missing here still reaches the rail, under "Other", so a tool
 *  added by a later release is never silently unavailable. */
const DRAW_GROUPS: [string, string[]][] = [
  ["Lines", [
    "trend-line", "ray", "extended-line", "arrow",
    "horizontal-line", "horizontal-ray", "vertical-line", "cross-line"
  ]],
  ["Shapes", [
    "rectangle", "rotated-rectangle", "ellipse", "circle", "triangle",
    "polyline", "path", "arc", "curve", "double-curve"
  ]],
  ["Channels", ["parallel-channel", "fib-channel"]],
  ["Fibonacci", ["fib-retracement", "fib-extension", "fib-time-zone", "fib-fan"]],
  ["Gann", ["gann-fan", "gann-box"]],
  ["Cycles", ["cyclic-lines", "time-cycles", "sine-line"]],
  ["Projection", ["long-position", "short-position", "forecast"]],
  ["Measure", ["measure", "price-range", "date-range"]],
  ["Brushes", ["brush", "highlighter"]],
  ["Annotations", [
    "text", "callout", "price-label", "price-note", "note",
    "balloon", "comment", "signpost", "flag-mark", "table"
  ]],
  ["Marks", ["arrow-up", "arrow-down", "arrow-left", "arrow-right"]]
]

/** Tools whose content is typed, so placing one empty is useless. */
const TEXT_TOOLS = new Set([
  "text", "callout", "note", "balloon", "comment", "signpost", "price-note", "table"
])

/** Bars in view on load. A fixed count rather than a time span, so the price
 *  range and the cursor-to-price mapping are the same on every screen width. */
const VISIBLE_BARS = 180

/** Live price is pushed to React at most this often. The chart itself updates on
 *  every tick; only the crossing into the render cycle is rate limited. */
const PRICE_PUSH_MS = 250

/** The history reconcile cadence. 25 to 35 seconds, the same window OpenAlgo's
 *  own terminal uses, jittered so every open tab does not poll in step. */
const RECONCILE_BASE_MS = 25000
const RECONCILE_JITTER_MS = 10000

/** Days of history a reconcile re-asks for. Enough to cover a hidden tab's
 *  afternoon at any intraday interval; a daily series gets two or three bars. */
const RECONCILE_MAX_DAYS = 3

/** Undo depth. The engine's default is 50 and every analyst add, update and
 *  remove costs one entry: a thirty-shape analysis replaced by a thirty-shape
 *  one is sixty entries, and a theme swap over it is up to thirty more. There
 *  is no batch API in 1.9.2, so the depth absorbs the cost instead. */
const UNDO_DEPTH = 500

const DEFAULT_SYMBOL = "RELIANCE"
const DEFAULT_EXCHANGE = "NSE"

function describe(error: unknown): string {
  if (error instanceof Error && error.message !== "") return error.message
  if (typeof error === "string" && error !== "") return error
  return "the chart request failed"
}

function chartTypeDef(value: string): ChartTypeDef {
  return CHART_TYPES.find((entry) => entry.value === value) ?? CHART_TYPES[0]
}

function sameBar(a: Bar, b: Bar): boolean {
  return (
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    (a.volume ?? 0) === (b.volume ?? 0)
  )
}

/** One descriptor input as the settings form wants it, with the live value. */
function toField(input: IndicatorInput, values: Record<string, unknown>): SettingsField {
  const current = values[input.key]
  const base: SettingsField = {
    key: input.key,
    type: input.type,
    label: input.label,
    value: current === undefined || current === null ? input.default : current,
    default: input.default
  }
  if (input.type === "number") {
    return { ...base, min: input.min, max: input.max, step: input.step }
  }
  if (input.type === "select") {
    return { ...base, options: input.options.map((o) => ({ label: o.label, value: o.value })) }
  }
  if (input.type === "source") {
    return { ...base, options: INDICATOR_SOURCES.map((o) => ({ label: o.label, value: o.value })) }
  }
  return base
}

interface LegendNodes {
  title: HTMLSpanElement
  ohlc: HTMLSpanElement
  volume: HTMLSpanElement
  change: HTMLSpanElement
}

/** What of the viewport survives a data change. Logical indices belong to one
 *  series and cannot cross a reload; the zoom (bars in view) and the gap past
 *  the last bar can. */
interface ViewSnapshot {
  span: number
  rightGap: number
}

export class ChartTerminal implements TerminalApi {
  private readonly container: HTMLElement
  private readonly legendEl: HTMLElement
  private readonly storageKey: string
  private readonly getTheme: () => "dark" | "light"
  private readonly cb: TerminalCallbacks

  private chart: Chart | null = null
  private price: SeriesApi | null = null
  private volume: SeriesApi | null = null

  private socket: OaSocket | null = null
  private live: ProxyDataFeed | null = null
  private feed: ReturnType<typeof withBarCache> | null = null
  private offBars: UnsubscribeFn | null = null

  private draw: DrawingController | null = null
  private drawTier: DrawTier | null = null
  private transformTier: TransformTier | null = null
  private indicatorTier = false
  private drawJson: Drawing[] = []
  private drawTool: string | null = null
  private magnet = false
  private mutingDraw = false

  private rawBars: Bar[] = []
  private shownBars: Bar[] = []
  private symbolInfo: SymbolInfo = {
    symbol: DEFAULT_SYMBOL,
    exchange: DEFAULT_EXCHANGE,
    quoteOnly: false
  }
  /** True once symbolInfo came from the proxy rather than from storage. */
  private symbolLoaded = false
  /** The instrument the user or the analyst last asked for. Set the moment it is
   *  asked for; symbolInfo follows once the metadata has loaded. */
  private target = { symbol: DEFAULT_SYMBOL, exchange: DEFAULT_EXCHANGE }
  private interval = DEFAULT_INTERVAL
  private chartTypeId = "candlestick"
  private intervals: IntervalGroups = emptyIntervals()

  private tracked: { indicatorId: string; settings: Record<string, unknown> }[] = []
  private applyingIndicators = false

  private readonly aiGroups = new Map<string, AnnotationShape[]>()

  private lastPrice: number | null = null
  private prevClose: number | null = null
  private pushedPriceAt = 0
  private wsState: WsState = "closed"

  private volumeVisible = true
  private scaleMode: PriceScaleMode = "linear"

  /** Every view change runs on this chain, in order. */
  private chain: Promise<void> = Promise.resolve()
  /** Latest-wins token for the data-loading view changes. */
  private viewGen = 0
  /** Which rebuild a restoreAfterBuild belongs to. */
  private buildGen = 0
  private loadToken = 0
  private loadingOlder = false
  private noMoreHistory = false
  private destroyed = false

  /** The transform box size for the series as loaded. Fixed per load: derived
   *  per tick from the last close, renko re-bricked the whole history on every
   *  tick and the view jumped with it. */
  private box: number | null = null

  private reconcileTimer: ReturnType<typeof setTimeout> | null = null

  /** A focus that arrived at width 0 and waits for a real resize. */
  private pendingFocus: { from: number; to: number } | null = null

  private legend: LegendNodes | null = null
  /** Seeded with the library's light values so a read before the first
   *  styleLegend is a plausible colour rather than an empty string. */
  private legendPalette: LegendColors = {
    up: "#089981",
    down: "#e0473e",
    muted: "#5b6472",
    text: "#0f172a"
  }
  /** Unsubscribes belonging to the socket, which outlives every chart. */
  private readonly sessionOff: (() => void)[] = []
  /** Unsubscribes belonging to the current chart. Cleared on every rebuild. */
  private readonly chartOff: (() => void)[] = []
  /** Unsubscribes belonging to the current drawing controller, which can be
   *  re-attached to the same chart (a pane removal rebuilds it). Kept apart from
   *  chartOff so a re-attach does not stack a second set of listeners. */
  private readonly drawOff: (() => void)[] = []

  constructor(options: TerminalOptions) {
    this.container = options.container
    this.legendEl = options.legendEl
    this.storageKey = options.storageKey
    this.getTheme = options.getTheme
    this.cb = options.callbacks
  }

  /* storage */

  private store(key: string, value: string): void {
    try {
      window.localStorage.setItem(`${this.storageKey}${key}`, value)
    } catch {
      // Private browsing, or a full quota. A preference that cannot be saved is
      // not a reason to refuse to chart.
    }
  }

  private read(key: string): string | null {
    try {
      return window.localStorage.getItem(`${this.storageKey}${key}`)
    } catch {
      return null
    }
  }

  private restorePreferences(): void {
    const symbol = this.read("symbol")
    const exchange = this.read("exchange")
    if (symbol !== null && exchange !== null) {
      this.symbolInfo = { symbol, exchange, quoteOnly: false }
    }
    this.target = { symbol: this.symbolInfo.symbol, exchange: this.symbolInfo.exchange }
    const interval = this.read("interval")
    if (interval !== null) this.interval = interval
    const chartType = this.read("charttype")
    if (chartType !== null && CHART_TYPES.some((entry) => entry.value === chartType)) {
      this.chartTypeId = chartType
    }
    // Absent means shown: only an explicit "0" hides volume, so a first visit
    // and an existing pane both keep it.
    this.volumeVisible = this.read("volume") !== "0"
    this.magnet = this.read("magnet") === "1"
    const mode = this.read("scale")
    if (mode === "logarithmic" || mode === "percentage" || mode === "indexed-to-100") {
      this.scaleMode = mode
    }
    try {
      const raw = this.read("indicators")
      const parsed: unknown = raw === null ? null : JSON.parse(raw)
      if (Array.isArray(parsed)) this.tracked = parsed as typeof this.tracked
    } catch {
      // A layout written by an older build. Better empty than broken.
    }
    try {
      const raw = this.read("draw")
      const parsed: unknown = raw === null ? null : JSON.parse(raw)
      if (Array.isArray(parsed)) this.drawJson = parsed as Drawing[]
    } catch {
      this.drawJson = []
    }
    // The semantic shapes behind the analyst's drawings. Without them a reload
    // restores the drawings but not what they mean, and a theme swap can no
    // longer re-tint them.
    try {
      const raw = this.read("ai-groups")
      const parsed: unknown = raw === null ? null : JSON.parse(raw)
      if (Array.isArray(parsed)) {
        for (const entry of parsed) {
          if (!Array.isArray(entry) || entry.length !== 2) continue
          const [group, shapes] = entry as [unknown, unknown]
          if (typeof group !== "string" || !Array.isArray(shapes)) continue
          this.aiGroups.set(group, shapes as AnnotationShape[])
        }
      }
    } catch {
      this.aiGroups.clear()
    }
  }

  private storeAiGroups(): void {
    this.store("ai-groups", JSON.stringify([...this.aiGroups.entries()]))
  }

  /* lifecycle */

  async init(): Promise<void> {
    this.restorePreferences()

    let wsPath = "/api/oa/ws"
    try {
      const config = await getOaConfig()
      wsPath = config.wsPath
      if (!config.hostReachable) {
        this.cb.onToast("the OpenAlgo server is not reachable, so the chart has no data", "error")
      }
    } catch (error) {
      this.cb.onToast(describe(error), "error")
    }
    if (this.destroyed) return

    let intervalsError: string | null = null
    try {
      this.intervals = await getIntervals()
    } catch (error) {
      intervalsError = describe(error)
    }
    if (this.destroyed) return
    this.interval = pickInterval(this.intervals, this.interval)
    if (flattenIntervals(this.intervals).length === 0) {
      // An empty list is an unavailable list. The saved interval is kept and
      // the picker validates nothing until the list arrives on a later visit.
      const why = intervalsError === null ? "the broker offered no intervals" : intervalsError
      this.cb.onToast(`${why}; keeping ${this.interval}`, "error")
    }

    const socket = new OaSocket(socketUrl(wsPath))
    this.socket = socket
    this.sessionOff.push(socket.onState((state) => this.reportWs(state)))
    // Transient only: a fatal code arrives as the "auth failed" state instead.
    this.sessionOff.push(socket.onError((_code, message) => this.cb.onToast(message, "error")))
    this.live = new ProxyDataFeed(socket)
    // The cache fronts scroll-back paging ONLY. It never stores the forming
    // bar, so a hit is short by exactly the bar the chart is watching: a reload
    // served from it dropped the forming bar, rebuilt it from ticks with the
    // wrong open and no volume, and at 18:00 a D to 5m to D flick lost the day's
    // candle outright. reload() goes to the wire; pageHistory, whose bars are
    // immutable, is what warm loading is for.
    this.feed = withBarCache(this.live, { ttlMs: 60000, max: 16 })
    socket.connect()

    // The rail needs the tool catalogue the moment the page renders, and
    // drawTools() is synchronous, so the tier is awaited here rather than on
    // first use. It stays a dynamic import so it is still its own chunk.
    await this.ensureDrawTier()
    if (this.destroyed) return
    if (chartTypeDef(this.chartTypeId).transform !== undefined) {
      await this.ensureTransformTier()
      if (this.destroyed) return
    }

    this.build(false)
    // On the same chain as every later view change, so a symbol picked while
    // the first history is in flight lands after it rather than beside it.
    await this.enqueue((gen) => this.loadView(gen, true), true)
    if (this.destroyed) return
    this.cb.onReady({
      intervals: this.intervals,
      interval: this.interval,
      chartType: this.chartTypeId
    })
  }

  destroy(): void {
    if (this.destroyed) return
    this.destroyed = true
    this.stopReconcile()
    // First, and by hand. chart.destroy() leaves a DrawingController running,
    // and a leaked one can strand placement mode on.
    this.detachDrawing()
    this.teardownLive()
    for (const off of this.sessionOff) off()
    this.sessionOff.length = 0
    this.socket?.close()
    this.socket = null
    this.live = null
    this.feed = null
    for (const off of this.chartOff) off()
    this.chartOff.length = 0
    this.chart?.destroy()
    this.chart = null
    this.price = null
    this.volume = null
    this.legend = null
    this.legendEl.textContent = ""
  }

  /* the view chain */

  /** Run one view change after every earlier one.
   *
   *  A data-loading change (symbol, interval) takes a fresh generation and is
   *  skipped, or abandoned after its next await, once a later one exists: the
   *  later change reads the same target fields and does everything the earlier
   *  one would have. A chart-type change is serialised but never superseded,
   *  because a symbol change that follows it does not rebuild the chart. Two
   *  quick symbol picks used to race their metadata fetches and could end on
   *  the first, with the toolbar and the saved symbol both wrong.
   */
  private enqueue(task: (gen: number) => Promise<void>, bump: boolean): Promise<void> {
    const gen = bump ? ++this.viewGen : this.viewGen
    const run = this.chain.then(async () => {
      if (this.destroyed) return
      if (bump && gen !== this.viewGen) return
      await task(gen)
    })
    this.chain = run.catch(() => undefined)
    return run
  }

  private stale(gen: number): boolean {
    return this.destroyed || gen !== this.viewGen
  }

  /** Bring the chart to the target symbol and the current interval. */
  private async loadView(gen: number, frame: boolean): Promise<void> {
    const target = this.target
    const changed =
      target.symbol !== this.symbolInfo.symbol || target.exchange !== this.symbolInfo.exchange
    if (changed || !this.symbolLoaded) {
      this.teardownLive()
      this.stopReconcile()
      if (changed) {
        this.prevClose = null
        this.lastPrice = null
      }
      await this.loadSymbolInfo(target.symbol, target.exchange, gen)
      if (this.stale(gen)) return
    }
    await this.reload(gen, frame || changed)
  }

  /* tiers */

  private async ensureDrawTier(): Promise<DrawTier | null> {
    if (this.drawTier !== null) return this.drawTier
    let tier: DrawTier
    try {
      tier = await import("openalgo-charts/draw")
    } catch (error) {
      this.cb.onToast(describe(error), "error")
      return null
    }
    // The await above is a real suspension point.
    if (this.destroyed) return null
    this.drawTier = tier
    return tier
  }

  private async ensureTransformTier(): Promise<TransformTier | null> {
    if (this.transformTier !== null) return this.transformTier
    let tier: TransformTier
    try {
      tier = await import("openalgo-charts/transform")
    } catch (error) {
      this.cb.onToast(describe(error), "error")
      return null
    }
    if (this.destroyed) return null
    // Idempotent, and called explicitly because a bundler that shakes out a bare
    // side-effect import would otherwise leave point-figure and kagi unknown.
    tier.registerTransformChartTypes()
    this.transformTier = tier
    return tier
  }

  private async ensureIndicatorTier(): Promise<boolean> {
    if (this.indicatorTier) return true
    try {
      await import("openalgo-charts/indicators")
      // Every time the tier lands, before the destroyed check and before any
      // read of the registry: registration is idempotent, and a terminal torn
      // down mid-import must not leave the registry half overridden for the
      // next one, which would skip the import and read the built-in bollinger.
      applyIndicatorOverrides()
    } catch (error) {
      this.cb.onToast(describe(error), "error")
      return false
    }
    if (this.destroyed) return false
    this.indicatorTier = true
    return true
  }

  /* chart construction */

  private tickSize(): number {
    const tick = this.symbolInfo.tickSize
    return tick !== undefined && tick > 0 ? tick : 0.05
  }

  private decimals(): number {
    return precisionForStep(this.tickSize())
  }

  private priceStyle(def: ChartTypeDef, mode: "dark" | "light"): SeriesStyle {
    if (def.themedColor === true) return { color: readChartTheme(mode).lineColor }
    return {}
  }

  /** Create the chart. `keepView` carries the zoom across a rebuild; false
   *  frames the series afresh, which only the first build wants. */
  private build(keepView: boolean): void {
    const view = keepView ? this.captureView() : null
    // Snapshot before the chart the drawings live on goes away.
    this.detachDrawing()
    for (const off of this.chartOff) off()
    this.chartOff.length = 0
    this.chart?.destroy()
    this.container.textContent = ""

    const mode = this.getTheme()
    const chart = createChart(this.container, {
      theme: readChartTheme(mode),
      priceAxisWidth: 72,
      // This terminal draws its own OHLC readout over the pane's top-left
      // corner, so indicator legend rows have to start below it or their gear
      // and close buttons land underneath and cannot be clicked.
      legendOffset: { top: 34, left: 10 },
      // Alt+H and Alt+V arm the horizontal-line and vertical-line drawing
      // tools, and the engine's default keymap binds the same two combos to
      // the grid toggles, so every line tool armed by keyboard also flipped a
      // grid. The grid keeps no keyboard binding; the tools keep theirs.
      shortcuts: { disabledCommands: ["toggleGridHorz", "toggleGridVert"] }
    })
    this.chart = chart

    const def = chartTypeDef(this.chartTypeId)
    this.price = chart.addSeries(def.series, {
      style: this.priceStyle(def, mode),
      priceFormat: { type: "price", minMove: this.tickSize() }
    })
    // Volume rides the hidden overlay scale inside the price pane: it
    // autoscales on its own but draws no axis, so the right-hand column stays
    // one clean price ladder. The top margin pins it to the bottom fifth.
    this.volume = chart.addSeries("histogram", {
      paneIndex: 0,
      priceScaleId: "",
      style: { color: volumeColor(mode) },
      priceFormat: { type: "volume" }
    })
    this.volume.priceScale().setOptions({ marginTop: 0.82, marginBottom: 0 })
    if (!this.volumeVisible) this.volume.applyOptions({ visible: false })
    if (this.scaleMode !== "linear") {
      chart.setPriceScaleOptions({ mode: this.scaleMode }, "primary")
    }

    this.setPriceData()
    if (keepView) this.restoreView(view)
    else this.frameViewport()

    chart.subscribeCrosshairMove((event) => this.writeLegend(event.bar))
    this.chartOff.push(
      chart.on("indicatorSettings", (payload) => {
        const id = (payload as { instanceId?: unknown }).instanceId
        if (typeof id === "string") void this.openIndicatorSettings(id)
      })
    )
    // The legend's own close button removes an indicator without going through
    // this class. Without this the tracked list keeps it, and the next rebuild
    // brings the deleted indicator back.
    this.chartOff.push(chart.on("indicatorRemoved", () => this.syncIndicators()))
    // Removing the last indicator on a pane removes the pane, from either path.
    this.chartOff.push(
      chart.on("paneRemoved", (payload) => {
        const pane = (payload as { paneIndex?: unknown }).paneIndex
        if (typeof pane === "number") this.rehomeDrawings(pane)
      })
    )
    this.chartOff.push(chart.on("resize", (payload) => this.onResize(payload)))
    chart.setHistoryLoader(() => void this.pageHistory())

    this.buildLegend()
    this.writeLegend(null)
    void this.restoreAfterBuild(++this.buildGen)
  }

  /** Everything a rebuild threw away, put back in the order it is wanted.
   *
   *  Indicators before drawings: a drawing on an indicator pane names the pane
   *  by index and addPrimitive creates the pane it names, so the other order
   *  conjured an empty pane and displaced the indicator being re-added. The
   *  generation drops the restoration of a rebuild that was overlapped by a
   *  newer one, which used to re-add every indicator twice. */
  private async restoreAfterBuild(gen: number): Promise<void> {
    await this.reapplyIndicators(gen)
    if (this.destroyed || gen !== this.buildGen) return
    await this.ensureDrawing()
  }

  /* data */

  private boxSizeFor(lastClose: number): number {
    const tick = this.tickSize()
    const stepped = Math.round((lastClose * 0.0015) / tick) * tick
    return Math.max(tick, Number(stepped.toFixed(this.decimals())))
  }

  private makeTransform(kind: TransformKind, tier: TransformTier): ISeriesTransform {
    // Fixed at load. Re-deriving it from each tick's close re-bricked renko on
    // every tick; a chart type switched on mid-session gets the loaded value.
    const box =
      this.box ??
      this.boxSizeFor(this.rawBars.length > 0 ? this.rawBars[this.rawBars.length - 1].close : 100)
    switch (kind) {
      case "heikin-ashi":
        return new tier.HeikinAshiTransform()
      case "renko":
        return new tier.RenkoTransform({ boxSize: box })
      case "range":
        return new tier.RangeBarsTransform({ range: box })
      case "line-break":
        return new tier.LineBreakTransform({ lines: 3 })
      case "point-figure":
        return new tier.PointFigureTransform({ boxSize: box, reversal: 3, method: "hl" })
      case "kagi":
        return new tier.KagiTransform({ reversal: box })
    }
  }

  /** Volume re-bucketed onto a transform's own element times.
   *
   *  Transform output timestamps are synthetic (colliding times are bumped by a
   *  second so each element gets its own logical index), so a volume series
   *  joined to them by raw time would land on the wrong elements.
   */
  private volumeBars(shown: Bar[], transformed: boolean): Bar[] {
    if (!transformed) {
      return this.rawBars.map((bar) => ({
        time: bar.time,
        open: 0,
        high: bar.volume ?? 0,
        low: 0,
        close: bar.volume ?? 0
      }))
    }
    const out: Bar[] = []
    let index = 0
    for (const element of shown) {
      let total = 0
      while (index < this.rawBars.length && this.rawBars[index].time <= element.time) {
        total += this.rawBars[index].volume ?? 0
        index += 1
      }
      out.push({ time: element.time, open: 0, high: total, low: 0, close: total })
    }
    let tail = 0
    while (index < this.rawBars.length) {
      tail += this.rawBars[index].volume ?? 0
      index += 1
    }
    if (out.length > 0 && tail > 0) {
      const last = out[out.length - 1]
      last.high += tail
      last.close += tail
    }
    return out
  }

  private setPriceData(): void {
    const price = this.price
    const volume = this.volume
    if (price === null || volume === null) return
    const def = chartTypeDef(this.chartTypeId)
    const tier = this.transformTier
    let shown = this.rawBars
    let transformed = false
    if (def.transform !== undefined && tier !== null && this.rawBars.length > 0) {
      try {
        shown = tier.runTransform(this.makeTransform(def.transform, tier), this.rawBars)
        transformed = true
      } catch (error) {
        this.cb.onToast(describe(error), "error")
        shown = this.rawBars
      }
    }
    this.shownBars = shown
    if (def.baseline === true && shown.length > 0) {
      const mean = shown.reduce((sum, bar) => sum + bar.close, 0) / shown.length
      price.applyOptions({ baseValue: mean })
    }
    price.setData(shown)
    volume.setData(this.volumeBars(shown, transformed))
  }

  private frameViewport(): void {
    const chart = this.chart
    if (chart === null) return
    const count = this.shownBars.length
    if (count === 0) return
    const right = count - 1 + 4
    const span = Math.min(VISIBLE_BARS, count)
    chart.setVisibleLogicalRange({ from: right - span, to: right })
  }

  private captureView(): ViewSnapshot | null {
    const chart = this.chart
    if (chart === null || this.shownBars.length === 0) return null
    const range = chart.getVisibleLogicalRange()
    if (!Number.isFinite(range.from) || !Number.isFinite(range.to)) return null
    const span = range.to - range.from
    if (!(span > 0)) return null
    return { span, rightGap: range.to - (this.shownBars.length - 1) }
  }

  /** Put a captured zoom back over whatever series is loaded now. With nothing
   *  captured (an empty chart before, or a hidden page) the series is framed. */
  private restoreView(view: ViewSnapshot | null): void {
    if (view === null) {
      this.frameViewport()
      return
    }
    const chart = this.chart
    const count = this.shownBars.length
    if (chart === null || count === 0) return
    const to = count - 1 + view.rightGap
    chart.setVisibleLogicalRange({ from: to - view.span, to })
  }

  private teardownLive(): void {
    if (this.offBars !== null) {
      this.offBars()
      this.offBars = null
    }
  }

  private reportWs(state: WsState): void {
    if (this.wsState === state) return
    this.wsState = state
    this.cb.onWsState(state)
  }

  private async loadSymbolInfo(symbol: string, exchange: string, gen: number): Promise<void> {
    const info = await getSymbolInfo(symbol, exchange)
    if (this.stale(gen)) return
    this.symbolInfo = info
    this.symbolLoaded = true
    this.store("symbol", info.symbol)
    this.store("exchange", info.exchange)
    // Both halves of the price format travel with the symbol. The series was
    // created with the previous instrument's tick, and pushing precision alone
    // left a 0.05 ladder under a 0.25 instrument.
    this.price?.applyOptions({ precision: this.decimals() })
    this.chart?.setPriceScaleOptions({ minMove: this.tickSize() }, "primary")
    this.buildLegend()
    this.cb.onSymbolLoaded({ symbol: info.symbol, exchange: info.exchange, name: info.name })
  }

  /** Reload history for the current symbol and interval, then resubscribe.
   *
   *  Straight to the wire, never through the cache: a cached series is closed
   *  bars only. The load token guards the pager and the reconcile, which are
   *  not on the view chain; the generation guards against a newer view change.
   */
  private async reload(gen: number, frame: boolean): Promise<void> {
    const live = this.live
    if (live === null) return
    const token = ++this.loadToken
    this.teardownLive()
    this.stopReconcile()
    const to = nowSec()
    const range = historyRange(this.interval, to)
    let bars: Bar[]
    try {
      bars = await live.getBars({
        symbol: this.symbolInfo.symbol,
        exchange: this.symbolInfo.exchange,
        interval: this.interval,
        from: range.from,
        to: range.to
      })
    } catch (error) {
      if (this.stale(gen) || token !== this.loadToken) return
      // The previous bars stay, un-subscribed. A blank chart with an
      // epoch-anchored live series under it told the user less than the last
      // good series and a toast do.
      this.cb.onToast(describe(error), "error")
      return
    }
    if (this.stale(gen) || token !== this.loadToken) return
    this.rawBars = bars
    this.noMoreHistory = false
    this.box = bars.length > 0 ? this.boxSizeFor(bars[bars.length - 1].close) : null
    const view = frame ? null : this.captureView()
    this.setPriceData()
    if (frame) this.frameViewport()
    else this.restoreView(view)
    this.lastPrice = bars.length > 0 ? bars[bars.length - 1].close : null
    if (this.lastPrice !== null) this.cb.onLastPrice(this.lastPrice)
    this.writeLegend(null)
    if (bars.length === 0) {
      // Nothing to anchor a live bucket to, and a series that starts from a
      // tick has no open, no history and no session grid.
      this.cb.onToast(
        `no ${this.interval} history for ${this.symbolInfo.exchange}:${this.symbolInfo.symbol}`,
        "info"
      )
    } else {
      this.subscribeLive()
      this.scheduleReconcile()
    }
    void this.refreshQuote(token)
  }

  private async refreshQuote(token: number): Promise<void> {
    try {
      const quote = await getQuote(this.symbolInfo.symbol, this.symbolInfo.exchange)
      if (this.destroyed || token !== this.loadToken) return
      this.prevClose = quote.prevClose
      if (quote.ltp !== null && this.lastPrice === null) {
        this.lastPrice = quote.ltp
        this.cb.onLastPrice(quote.ltp)
      }
      this.writeLegend(null)
    } catch {
      // The previous close is decoration on the readout, not chart data.
    }
  }

  private subscribeLive(): void {
    const live = this.live
    if (live === null) return
    const seed = this.rawBars.length > 0 ? this.rawBars[this.rawBars.length - 1] : undefined
    this.offBars = live.subscribeBars(
      {
        symbol: this.symbolInfo.symbol,
        exchange: this.symbolInfo.exchange,
        interval: this.interval
      },
      (bar) => this.onLiveBar(bar),
      {
        seedFrom: seed,
        onSnapshot: (price) => this.onSnapshotPrice(price),
        onResync: () => this.reconcileNow()
      }
    )
  }

  /** A price that must not become a bar: the subscribe-time snapshot, or a
   *  reading stamped before the forming bar opened. */
  private onSnapshotPrice(price: number): void {
    if (this.destroyed) return
    this.lastPrice = price
    this.cb.onLastPrice(price)
    this.writeLegend(null)
  }

  private onLiveBar(bar: Bar): void {
    const price = this.price
    if (this.destroyed || price === null) return
    const last = this.rawBars.length > 0 ? this.rawBars[this.rawBars.length - 1] : null
    if (last !== null && bar.time === last.time) {
      this.rawBars[this.rawBars.length - 1] = bar
    } else if (last === null || bar.time > last.time) {
      this.rawBars.push(bar)
    } else {
      // Older than the last bar. The builder already folded it if policy said
      // to; anything still arriving here is a correction the chart cannot place.
      return
    }
    this.lastPrice = bar.close
    this.reportWs("live")
    const now = Date.now()
    if (now - this.pushedPriceAt >= PRICE_PUSH_MS) {
      this.pushedPriceAt = now
      this.cb.onLastPrice(bar.close)
    }
    const def = chartTypeDef(this.chartTypeId)
    if (def.transform !== undefined) {
      // A movement-driven type re-buckets the whole series: one raw tick can
      // close several bricks or none, so there is no single element to update.
      this.setPriceData()
    } else {
      price.update(bar)
      this.volume?.update({
        time: bar.time,
        open: 0,
        high: bar.volume ?? 0,
        low: 0,
        close: bar.volume ?? 0
      })
      this.shownBars = this.rawBars
    }
    this.writeLegend(null)
  }

  /* the history reconcile */

  private stopReconcile(): void {
    if (this.reconcileTimer !== null) {
      clearTimeout(this.reconcileTimer)
      this.reconcileTimer = null
    }
  }

  private scheduleReconcile(): void {
    this.stopReconcile()
    const delay = RECONCILE_BASE_MS + Math.random() * RECONCILE_JITTER_MS
    this.reconcileTimer = setTimeout(() => void this.runReconcile(), delay)
  }

  /** The stream just restarted: whatever closed during the gap was never built
   *  from ticks, so re-ask now rather than sit on the hole for half a minute. */
  private reconcileNow(): void {
    if (this.offBars === null) return
    this.stopReconcile()
    this.reconcileTimer = setTimeout(() => void this.runReconcile(), 0)
  }

  private async runReconcile(): Promise<void> {
    this.reconcileTimer = null
    const live = this.live
    if (live === null || this.destroyed || this.offBars === null) return
    if (this.rawBars.length === 0) return
    const token = this.loadToken
    const req = {
      symbol: this.symbolInfo.symbol,
      exchange: this.symbolInfo.exchange,
      interval: this.interval
    }
    const to = nowSec()
    try {
      const fresh = await live.getBars({
        ...req,
        from: to - Math.min(RECONCILE_MAX_DAYS, lookbackDays(this.interval)) * 86400,
        to
      })
      if (this.destroyed || token !== this.loadToken) return
      if (this.applyReconcile(fresh)) {
        this.setPriceData()
        this.writeLegend(null)
      }
    } catch {
      // The next cycle retries. A reconcile that fails is a chart that is a
      // few bars behind the broker, which is where it was before it ran.
    }
    if (this.destroyed || token !== this.loadToken || this.offBars === null) return
    this.scheduleReconcile()
  }

  /** Snap the closed bars to the broker's, fill closed buckets the stream never
   *  built, and correct the forming bar's volume through the builder. Returns
   *  whether anything changed. */
  private applyReconcile(fresh: Bar[]): boolean {
    const bars = this.rawBars
    if (bars.length === 0 || fresh.length === 0) return false
    const forming = bars[bars.length - 1].time
    const byTime = new Map(fresh.map((bar) => [bar.time, bar]))
    let changed = false
    for (let i = 0; i < bars.length; i++) {
      const bar = bars[i]
      if (bar.time >= forming) break
      const truth = byTime.get(bar.time)
      if (truth !== undefined && !sameBar(bar, truth)) {
        bars[i] = truth
        changed = true
      }
    }
    // Closed buckets the client has nothing for: the socket dropped, the tab
    // was hidden, the machine slept. Candles either side are fine, so the
    // chart showed a clean hole. A bucket newer than the forming bar is filled
    // only once the clock says it has closed; the one still open belongs to
    // the stream, which is fresher than a poll.
    const seconds = intervalSeconds(this.interval)
    const now = nowSec()
    const known = new Set(bars.map((bar) => bar.time))
    const earliest = bars[0].time
    const missing = fresh.filter((bar) => {
      if (bar.time <= earliest || known.has(bar.time)) return false
      if (bar.time < forming) return true
      return seconds !== null && bar.time + seconds <= now
    })
    if (missing.length > 0) {
      this.rawBars = [...bars, ...missing].sort((a, b) => a.time - b.time)
      changed = true
    }
    // The forming bar keeps its OHLC from the ticks, which are fresher than a
    // poll and must not jump backwards to it. Volume inside a bar only grows,
    // so the higher reading is the later one, and it goes through the builder:
    // the next tick folds into the builder's copy, and a volume patched into
    // rawBars alone was written straight back over.
    const current = this.rawBars[this.rawBars.length - 1]
    const truth = byTime.get(forming)
    if (truth !== undefined && current.time === forming) {
      const volume = Math.max(truth.volume ?? 0, current.volume ?? 0)
      if (volume !== (current.volume ?? 0)) {
        const patched = { ...current, volume }
        this.rawBars[this.rawBars.length - 1] = patched
        this.live?.reseedForming(
          {
            symbol: this.symbolInfo.symbol,
            exchange: this.symbolInfo.exchange,
            interval: this.interval
          },
          patched
        )
        changed = true
      }
    }
    return changed
  }

  private async pageHistory(): Promise<void> {
    const chart = this.chart
    const feed = this.feed
    if (
      chart === null ||
      feed === null ||
      this.loadingOlder ||
      this.noMoreHistory ||
      this.rawBars.length === 0
    ) {
      // setHistoryLoader latches until this is called, so every exit reports.
      chart?.historyLoadComplete()
      return
    }
    this.loadingOlder = true
    const token = this.loadToken
    const oldest = this.rawBars[0].time
    try {
      const to = oldest - 1
      const range = historyRange(this.interval, to)
      // Through the cache: an older page is closed bars only, which is the one
      // kind of series the cache is right about.
      const older = await feed.getBars({
        symbol: this.symbolInfo.symbol,
        exchange: this.symbolInfo.exchange,
        interval: this.interval,
        from: range.from,
        to
      })
      if (this.destroyed || token !== this.loadToken || this.chart === null) return
      // Trust nothing about the window the broker actually returned: a re-sent
      // overlapping page would grow the array without moving the left edge.
      const fresh = older.filter((bar) => bar.time < oldest)
      if (fresh.length === 0) {
        this.noMoreHistory = true
        return
      }
      const before = this.chart.getVisibleLogicalRange()
      const countBefore = this.shownBars.length
      this.rawBars = [...fresh, ...this.rawBars]
      this.setPriceData()
      // Measured rather than assumed to be fresh.length: a movement-driven type
      // turns raw bars into its own number of elements, so the axis grows by an
      // amount only the transform knows.
      const inserted = this.shownBars.length - countBefore
      if (inserted > 0 && Number.isFinite(before.from) && Number.isFinite(before.to)) {
        this.chart.setVisibleLogicalRange({
          from: before.from + inserted,
          to: before.to + inserted
        })
      }
    } catch (error) {
      this.cb.onToast(describe(error), "error")
    } finally {
      this.loadingOlder = false
      this.chart?.historyLoadComplete()
    }
  }

  /* the OHLC readout, written straight to the DOM */

  private buildLegend(): void {
    this.legendEl.textContent = ""
    const make = (): HTMLSpanElement => {
      const span = document.createElement("span")
      span.style.marginRight = "12px"
      span.style.whiteSpace = "nowrap"
      this.legendEl.appendChild(span)
      return span
    }
    const title = make()
    title.style.fontWeight = "600"
    this.legend = { title, ohlc: make(), volume: make(), change: make() }
    this.styleLegend()
  }

  private styleLegend(): void {
    // Held rather than read per write: writeLegend runs on every crosshair move.
    this.legendPalette = legendColors(this.getTheme())
    const nodes = this.legend
    if (nodes === null) return
    nodes.title.style.color = this.legendPalette.text
    nodes.volume.style.color = this.legendPalette.muted
  }

  private writeLegend(bar: Bar | null): void {
    const nodes = this.legend
    if (nodes === null) return
    const bars = this.shownBars
    const shown = bar ?? (bars.length > 0 ? bars[bars.length - 1] : null)
    const info = this.symbolInfo
    nodes.title.textContent = `${info.exchange}:${info.symbol}  ${this.interval}`
    const colors = this.legendPalette
    if (shown === null) {
      nodes.ohlc.textContent = ""
      nodes.volume.textContent = ""
      nodes.change.textContent = ""
      return
    }
    const decimals = this.decimals()
    const price = (value: number): string => formatNumber(value, decimals)
    nodes.ohlc.textContent =
      `O ${price(shown.open)}  H ${price(shown.high)}` +
      `  L ${price(shown.low)}  C ${price(shown.close)}`
    nodes.ohlc.style.color = shown.close >= shown.open ? colors.up : colors.down
    const traded = shown.volume
    nodes.volume.textContent = traded === undefined ? "" : `Vol ${compactVolume(traded)}`
    const reference = this.lastPrice ?? shown.close
    if (this.prevClose !== null && this.prevClose > 0) {
      const change = ((reference - this.prevClose) / this.prevClose) * 100
      nodes.change.textContent = `${change >= 0 ? "+" : ""}${formatNumber(change, 2)}%`
      nodes.change.style.color = change >= 0 ? colors.up : colors.down
    } else {
      nodes.change.textContent = ""
    }
  }

  /* ambient context and analyst commands */

  context(): ChartContext {
    const chart = this.chart
    const bars = this.shownBars
    let visibleFrom: number | null = null
    let visibleTo: number | null = null
    if (chart !== null && bars.length > 0) {
      const range = chart.getVisibleLogicalRange()
      const layer = chart.dataLayer
      // A logical index shifts when older history pages in, so the range is
      // converted to UTC seconds here and never travels as an index.
      const from = layer.indexToTimeFloat(range.from)
      const to = layer.indexToTimeFloat(range.to)
      if (Number.isFinite(from)) visibleFrom = Math.round(from)
      if (Number.isFinite(to)) visibleTo = Math.round(to)
    }
    return {
      symbol: this.symbolInfo.symbol,
      exchange: this.symbolInfo.exchange,
      interval: this.interval,
      chartType: this.chartTypeId,
      barCount: bars.length,
      firstTime: bars.length > 0 ? bars[0].time : null,
      lastTime: bars.length > 0 ? bars[bars.length - 1].time : null,
      visibleFrom,
      visibleTo,
      lastPrice: this.lastPrice,
      indicators: this.listIndicators(),
      theme: this.getTheme(),
      analystGroups: [...this.aiGroups.keys()]
    }
  }

  async apply(command: ChartCommand): Promise<void> {
    switch (command.op) {
      case "draw":
        await this.applyGroup(command.group, command.shapes, command.append === true)
        return
      case "clear":
        this.clearGroup(command.group)
        return
      case "set_symbol":
        await this.setSymbol(command.symbol, command.exchange)
        return
      case "set_interval":
        await this.setInterval(command.interval)
        return
      case "set_chart_type":
        await this.setChartType(command.chartType)
        return
      case "add_indicator":
        await this.addIndicator(command.indicatorId, command.settings)
        return
      case "remove_indicator":
        this.removeIndicatorFor(command.instanceId, command.indicatorId)
        return
      case "update_indicator":
        this.updateIndicatorFor(command.indicatorId, command.instanceId, command.settings)
        return
      case "focus":
        this.focus(command.from, command.to)
        return
      default:
        // A newer backend naming an op this build does not have. Ignoring one
        // command is recoverable; throwing loses the whole turn.
        return
    }
  }

  async snapshot(): Promise<string | null> {
    const chart = this.chart
    if (chart === null) return null
    try {
      // takeScreenshot composites every pane; the browser's own "save image"
      // would capture the clicked pane's overlay canvas alone.
      return chart.takeScreenshot().toDataURL("image/png")
    } catch {
      return null
    }
  }

  private focus(from: number, to: number): void {
    const chart = this.chart
    if (chart === null || this.shownBars.length === 0) return
    if (this.container.clientWidth <= 0) {
      // The engine's setVisibleLogicalRange returns before doing anything at
      // width 0, so a focus applied to a hidden page was simply gone. Held
      // until the next resize to a real width.
      this.pendingFocus = { from, to }
      return
    }
    this.pendingFocus = null
    const layer = chart.dataLayer
    let left = layer.timeToIndexFloat(Math.min(from, to))
    let right = layer.timeToIndexFloat(Math.max(from, to))
    if (!Number.isFinite(left) || !Number.isFinite(right)) return
    const pad = Math.max(2, (right - left) * 0.12)
    left -= pad
    right += pad
    if (right - left < 6) {
      const mid = (left + right) / 2
      left = mid - 3
      right = mid + 3
    }
    chart.setVisibleLogicalRange({ from: left, to: right })
    // Drawings contribute nothing to autoscale, so the price range can only be
    // re-measured from the bars now in view. Re-arming autoscale is what makes
    // that happen: any earlier axis drag switched it off for good.
    chart.setAutoScale(true)
  }

  private onResize(payload: unknown): void {
    const pending = this.pendingFocus
    if (pending === null) return
    const width = (payload as { width?: unknown }).width
    if (typeof width !== "number" || width <= 0) return
    this.pendingFocus = null
    this.focus(pending.from, pending.to)
  }

  /* analyst markup */

  /** Put a group's markup on the chart. Replaces the group unless `append`,
   *  which keeps what is there and adds to it. */
  private async applyGroup(
    group: string,
    shapes: AnnotationShape[],
    append: boolean
  ): Promise<void> {
    const draw = await this.ensureDrawing()
    if (draw === null) return
    const palette = tonePalette(this.getTheme())
    this.mutingDraw = true
    try {
      const before = append ? (this.aiGroups.get(group) ?? []) : []
      if (!append) this.removeGroup(draw, group)
      // An appended id continues the group's numbering, from whichever of two
      // floors is higher. The highest id still on the chart keeps a new drawing
      // off an existing one: add() does no duplicate check and would draw
      // both under one id. The count a from-zero rebuild of the stored shapes
      // yields keeps retintAiDrawings, which rebuilds every group from zero,
      // naming the same drawings after a hand-deleted one has left a gap.
      const first = append
        ? Math.max(
            nextGroupIndex(draw.toJSON().map((drawing) => drawing.id), group),
            buildAnnotations(group, before, palette, 0).length
          )
        : 0
      const drawings = buildAnnotations(group, shapes, palette, 0, first)
      for (const drawing of drawings) {
        try {
          // add(), not fromJSON: fromJSON replaces the whole model and wipes
          // undo, redo and selection, so applying the analyst's markup would
          // throw away the user's own history. One add per shape also means the
          // markup can be undone shape by shape. Each add is an undo entry, and
          // so is each remove above; UNDO_DEPTH is sized for that.
          draw.add(drawing)
        } catch (error) {
          this.cb.onToast(describe(error), "error")
        }
      }
      this.aiGroups.set(group, [...before, ...shapes])
      this.storeAiGroups()
    } finally {
      this.mutingDraw = false
    }
    this.afterDrawChange()
    // Markup can land off-screen, and drawings never expand a scale to reach it.
    // Only when it actually would: re-framing a group the user is already
    // looking at yanks the viewport for nothing.
    const span = annotationSpan(shapes)
    if (span !== null && !this.spanIsVisible(span.from, span.to)) {
      this.focus(span.from, span.to)
    }
  }

  private spanIsVisible(from: number, to: number): boolean {
    const chart = this.chart
    if (chart === null || this.shownBars.length === 0) return false
    const range = chart.getVisibleLogicalRange()
    const layer = chart.dataLayer
    const left = layer.indexToTimeFloat(range.from)
    const right = layer.indexToTimeFloat(range.to)
    if (!Number.isFinite(left) || !Number.isFinite(right)) return false
    return from >= left && to <= right
  }

  private removeGroup(draw: DrawingController, group: string): void {
    // toJSON is a snapshot, so removing while walking it is safe. drawings() is
    // the live array and would be mutated underneath the loop.
    for (const drawing of draw.toJSON()) {
      if (inGroup(drawing.id, group)) draw.remove(drawing.id)
    }
  }

  private clearGroup(group?: string): void {
    const mine = (id: string): boolean =>
      group === undefined ? isAiDrawing(id) : inGroup(id, group)
    const draw = this.draw
    if (draw === null) {
      // No controller yet (the tier is still loading, or a rebuild is between
      // charts): the snapshot it will be restored from is edited instead, so
      // the markup does not come back with the controller.
      this.drawJson = this.drawJson.filter((drawing) => !mine(drawing.id))
      this.store("draw", JSON.stringify(this.drawJson))
    } else {
      this.mutingDraw = true
      try {
        for (const drawing of draw.toJSON()) {
          if (mine(drawing.id)) draw.remove(drawing.id)
        }
      } finally {
        this.mutingDraw = false
      }
    }
    if (group === undefined) this.aiGroups.clear()
    else this.aiGroups.delete(group)
    this.storeAiGroups()
    this.afterDrawChange()
  }

  /** Re-colour the analyst's drawings for the active theme.
   *
   *  DrawingStyle stores a literal colour string, so nothing re-tints itself: a
   *  theme swap leaves AI markup on the old palette until it is written again.
   *  The ids are deterministic, so the shapes are rebuilt against the new
   *  palette and only the style is pushed across, which merges. Every update
   *  is an undo entry; unchanged colours are skipped so a swap back costs none.
   */
  private retintAiDrawings(): void {
    const draw = this.draw
    if (draw === null || this.aiGroups.size === 0) return
    const palette = tonePalette(this.getTheme())
    this.mutingDraw = true
    try {
      for (const [group, shapes] of this.aiGroups) {
        for (const drawing of buildAnnotations(group, shapes, palette, 0)) {
          const existing = draw.get(drawing.id)
          if (existing === undefined) continue
          // An unchanged colour would still cost an undo snapshot.
          if (existing.style.color === drawing.style.color) continue
          draw.update(drawing.id, { style: drawing.style })
        }
      }
    } finally {
      this.mutingDraw = false
    }
    this.afterDrawChange()
  }

  /* symbol, interval, chart type, theme */

  async searchSymbols(query: string, exchange?: string): Promise<SymbolRow[]> {
    try {
      return await searchSymbols(query, exchange)
    } catch (error) {
      this.cb.onToast(describe(error), "error")
      return []
    }
  }

  async setSymbol(symbol: string, exchange: string): Promise<void> {
    if (symbol === "" || exchange === "") return
    if (symbol === this.target.symbol && exchange === this.target.exchange) return
    this.target = { symbol, exchange }
    // Analyst geometry belongs to one instrument and one timeframe. A RELIANCE
    // envelope has no meaning on SBIN.
    this.clearGroup(undefined)
    await this.enqueue((gen) => this.loadView(gen, true), true)
  }

  async setInterval(interval: string): Promise<void> {
    if (interval === this.interval) return
    const offered = flattenIntervals(this.intervals)
    if (offered.length > 0 && !offered.includes(interval)) {
      this.cb.onToast(`the broker does not offer a ${interval} interval`, "error")
      return
    }
    if (!isKnownInterval(interval)) {
      // resolveInterval would throw at subscribe time. History still loads, so
      // the chart is usable; say plainly what will not work rather than guessing
      // a bucket size, which is the defect the throw exists to prevent.
      this.cb.onToast(`live bars cannot be aggregated for ${interval}`, "error")
    } else if (intervalSeconds(interval) === null) {
      this.cb.onToast(`${interval} has no fixed length, so its last bar updates in place`, "info")
    }
    this.interval = interval
    this.store("interval", interval)
    // As for a symbol change: anchors computed on 5m bars say nothing on daily.
    this.clearGroup(undefined)
    this.buildLegend()
    this.cb.onViewChanged({ interval: this.interval, chartType: this.chartTypeId })
    await this.enqueue((gen) => this.loadView(gen, false), true)
  }

  async setChartType(chartType: string): Promise<void> {
    const def = CHART_TYPES.find((entry) => entry.value === chartType)
    if (def === undefined) {
      this.cb.onToast(`there is no chart type called "${chartType}"`, "error")
      return
    }
    // Serialised behind any load in flight, never superseded by one: the
    // rebuild reads whatever bars are loaded when its turn comes.
    await this.enqueue(async () => {
      if (def.transform !== undefined && (await this.ensureTransformTier()) === null) return
      if (this.destroyed) return
      this.chartTypeId = def.value
      this.store("charttype", def.value)
      this.cb.onViewChanged({ interval: this.interval, chartType: this.chartTypeId })
      // A series cannot change type in place, so this is the one action that
      // rebuilds. build() snapshots the drawings and re-applies everything else.
      this.build(true)
    }, false)
  }

  chartTypes(): ChartTypeOption[] {
    return CHART_TYPES.map((entry) => ({
      value: entry.value,
      label: entry.label,
      derived: entry.transform !== undefined
    }))
  }

  applyTheme(): void {
    const chart = this.chart
    if (chart === null) return
    const mode = this.getTheme()
    // A live swap: setTheme repaints everything and every series picks up the
    // new defaults without being touched.
    chart.setTheme(readChartTheme(mode))
    this.volume?.applyOptions({ color: volumeColor(mode) })
    this.price?.applyOptions(this.priceStyle(chartTypeDef(this.chartTypeId), mode))
    this.retintAiDrawings()
    this.styleLegend()
    this.writeLegend(null)
  }

  /* indicators */

  private listIndicators(): ChartIndicator[] {
    const chart = this.chart
    if (chart === null) return []
    return chart.indicators().map((instance) => ({
      instanceId: instance.id,
      indicatorId: instance.indicatorId,
      name: instance.name,
      paneIndex: instance.paneIndex,
      settings: { ...instance.settings() }
    }))
  }

  private syncIndicators(): void {
    const chart = this.chart
    if (chart === null || this.applyingIndicators) return
    this.tracked = chart.indicators().map((instance) => ({
      indicatorId: instance.indicatorId,
      settings: { ...instance.settings() }
    }))
    this.store("indicators", JSON.stringify(this.tracked))
    this.cb.onIndicators(this.listIndicators())
  }

  private async reapplyIndicators(gen: number): Promise<void> {
    if (this.tracked.length === 0) return
    if (!(await this.ensureIndicatorTier())) return
    const chart = this.chart
    if (chart === null || this.destroyed || gen !== this.buildGen) return
    // Re-adding walks the tracked list, so a sync mid-loop would read a
    // half-applied chart and truncate it.
    this.applyingIndicators = true
    const failed: string[] = []
    try {
      for (const record of this.tracked) {
        if (!hasIndicator(record.indicatorId)) {
          failed.push(record.indicatorId)
          continue
        }
        try {
          chart.addIndicator(record.indicatorId, record.settings)
        } catch {
          // One indicator that will not rebuild must not cost the others.
          failed.push(record.indicatorId)
        }
      }
    } finally {
      this.applyingIndicators = false
    }
    if (failed.length === 0) {
      this.syncIndicators()
      return
    }
    // The failed ones stay tracked: a sync here would rebuild the list from
    // the chart and persist the layout without them, and the user's saved
    // indicator would be silently gone on the next visit.
    this.cb.onToast(`could not restore ${failed.join(", ")}`, "error")
    this.cb.onIndicators(this.listIndicators())
  }

  async indicatorCatalogue(): Promise<IndicatorOption[]> {
    if (!(await this.ensureIndicatorTier())) return []
    return registeredIndicators().map((descriptor) => ({
      id: descriptor.id,
      name: descriptor.name,
      category: descriptor.category ?? "Other"
    }))
  }

  async addIndicator(id: string, settings?: Record<string, unknown>): Promise<void> {
    if (!(await this.ensureIndicatorTier())) return
    const chart = this.chart
    if (chart === null || this.destroyed) return
    // Probe rather than catch: 102 ids are registered and none of them are
    // derivable from the display name, so a wrong id is the common case.
    if (!hasIndicator(id)) {
      this.cb.onToast(`there is no indicator called "${id}"`, "error")
      return
    }
    try {
      chart.addIndicator(id, settings ?? {})
    } catch (error) {
      this.cb.onToast(describe(error), "error")
      return
    }
    this.syncIndicators()
  }

  removeIndicator(instanceId: string): void {
    const chart = this.chart
    if (chart === null) return
    // A pane emptied by this removal is dropped by the engine, which emits
    // paneRemoved; the drawing layers are re-homed from that event.
    chart.removeIndicator(instanceId)
    this.syncIndicators()
  }

  /** The chart dropped a pane and renumbered the ones below it.
   *
   *  The drawing controller keys its layers by pane index, so its layer for the
   *  dropped pane is now attached to nothing and every later layer is one pane
   *  off. The controller is rebuilt from its own JSON with the indices moved
   *  down; a drawing that lived on the dropped pane has no pane left and goes
   *  with it. fromJSON resets undo and redo, which is the cost of the rebuild.
   */
  private rehomeDrawings(removed: number): void {
    if (this.destroyed || removed <= 0) return
    this.detachDrawing()
    this.drawJson = this.drawJson
      .filter((drawing) => drawing.paneIndex !== removed)
      .map((drawing) =>
        drawing.paneIndex > removed ? { ...drawing, paneIndex: drawing.paneIndex - 1 } : drawing
      )
    this.store("draw", JSON.stringify(this.drawJson))
    void this.ensureDrawing()
  }

  private removeIndicatorFor(instanceId?: string, indicatorId?: string): void {
    const chart = this.chart
    if (chart === null) return
    if (instanceId !== undefined) {
      this.removeIndicator(instanceId)
      return
    }
    if (indicatorId === undefined) return
    // No instance named: drop the most recently added instance of that study,
    // which is the one "remove the RSI" means when several are on the chart.
    const matches = chart.indicators().filter((one) => one.indicatorId === indicatorId)
    const last = matches[matches.length - 1]
    if (last !== undefined) this.removeIndicator(last.id)
  }

  /** The analyst restyling an indicator that is already on the chart. */
  private updateIndicatorFor(
    indicatorId: string,
    instanceId: string | undefined,
    settings: Record<string, unknown>
  ): void {
    const chart = this.chart
    if (chart === null) return
    const instances = chart.indicators()
    // The named instance first. A stale id (the analyst read it a turn ago
    // and the user has since re-added the study) falls through to the first
    // instance of the study, which is the one "shade the bollinger" means.
    const named =
      instanceId === undefined ? undefined : instances.find((one) => one.id === instanceId)
    const instance = named ?? instances.find((one) => one.indicatorId === indicatorId)
    if (instance === undefined) {
      this.cb.onToast(`${indicatorId} is not on the chart`, "error")
      return
    }
    try {
      // setSettings merges the patch and recomputes; it validates nothing, so
      // the catch is for a value the indicator's own maths rejects.
      this.applyIndicatorSettings(instance.id, settings)
    } catch (error) {
      this.cb.onToast(describe(error), "error")
    }
  }

  async openIndicatorSettings(instanceId: string): Promise<void> {
    if (!(await this.ensureIndicatorTier())) return
    const chart = this.chart
    if (chart === null) return
    const instance = chart.indicators().find((one) => one.id === instanceId)
    if (instance === undefined || !hasIndicator(instance.indicatorId)) return
    const descriptor = getIndicator(instance.indicatorId)
    const values = instance.settings()
    // The descriptor's own inputs and the generated style inputs stay apart so
    // a form can tab them; one component then covers every indicator.
    this.cb.onIndicatorSettings({
      instanceId,
      name: instance.name,
      inputs: descriptor.inputs.map((input) => toField(input, values)),
      styleInputs: indicatorStyleInputs(descriptor).map((input) => toField(input, values))
    })
  }

  applyIndicatorSettings(instanceId: string, patch: Record<string, unknown>): void {
    const chart = this.chart
    if (chart === null) return
    const instance = chart.indicators().find((one) => one.id === instanceId)
    if (instance === undefined) return
    instance.setSettings(patch)
    this.syncIndicators()
  }

  async resetIndicatorSettings(instanceId: string): Promise<void> {
    if (!(await this.ensureIndicatorTier())) return
    const chart = this.chart
    if (chart === null) return
    const instance = chart.indicators().find((one) => one.id === instanceId)
    if (instance === undefined || !hasIndicator(instance.indicatorId)) return
    instance.setSettings(indicatorDefaults(getIndicator(instance.indicatorId)))
    this.syncIndicators()
    await this.openIndicatorSettings(instanceId)
  }

  /* drawing */

  private detachDrawing(): void {
    const draw = this.draw
    if (draw === null) return
    for (const off of this.drawOff) off()
    this.drawOff.length = 0
    try {
      this.drawJson = draw.toJSON()
      draw.destroy()
    } catch {
      // The chart is already gone. Keep the last snapshot taken.
    }
    this.draw = null
  }

  private async ensureDrawing(): Promise<DrawingController | null> {
    if (this.draw !== null) return this.draw
    const tier = await this.ensureDrawTier()
    const chart = this.chart
    if (tier === null || chart === null || this.destroyed) return null
    // A second caller may have attached while the tier was in flight.
    if (this.draw !== null) return this.draw
    // The controller's constructor restores whatever the chart's own drawings
    // slot holds BEFORE fromJSON below runs, and a controller re-attached to
    // the same chart finds the previous controller's last sync there, pane
    // indices included. Measured on a pane removal: the stale index conjured
    // an empty pane before the remapped list could replace it. The slot is
    // written first, so the constructor restores the list this class holds.
    chart.setDrawingState(this.drawJson)
    const controller = new tier.DrawingController(chart, {
      magnet: this.magnet,
      stayInDrawingMode: false,
      // Analyst maintenance costs undo entries. Replacing a group is one remove
      // and one add per shape, a theme retint is one update per shape whose
      // colour changed, and 1.9.2 offers no batch API, so a full fix (one entry
      // per analyst operation) is not available. The depth absorbs it instead:
      // at the default 50, one thirty-shape analysis evicted the user's own
      // edits and Ctrl+Z after a theme swap reverted an analyst colour.
      historyLimit: UNDO_DEPTH
    })
    this.draw = controller
    if (this.drawJson.length > 0) {
      try {
        controller.fromJSON(this.drawJson)
      } catch {
        // A shape from an older build. Better empty than a controller that
        // throws on every repaint.
        this.drawJson = []
      }
    }
    if (this.drawTool !== null) {
      try {
        controller.setTool(this.drawTool)
      } catch {
        this.drawTool = null
      }
    }
    for (const name of ["draw:tool", "draw:add", "draw:update", "draw:remove", "draw:select"]) {
      this.drawOff.push(chart.on(name, () => this.afterDrawChange()))
    }
    this.drawOff.push(
      chart.on("draw:add", (payload) => {
        const drawing = (payload as { drawing?: Drawing }).drawing
        if (drawing === undefined || isAiDrawing(drawing.id)) return
        if (!TEXT_TOOLS.has(drawing.tool)) return
        const text = drawing.style.text ?? ""
        this.cb.onDrawTextEdit({ id: drawing.id, tool: drawing.tool, text })
      })
    )
    // The restored tool and drawing count were set before the listeners existed,
    // so the toolbar is told once here or it opens showing an empty rail.
    this.afterDrawChange()
    return controller
  }

  private drawSelection(draw: DrawingController): DrawSelection | null {
    const id = draw.selected()
    if (id === null) return null
    const drawing = draw.get(id)
    if (drawing === undefined) return null
    return {
      id: drawing.id,
      tool: drawing.tool,
      hasText: TEXT_TOOLS.has(drawing.tool),
      color: drawing.style.color,
      lineWidth: drawing.style.lineWidth,
      lineStyle: drawing.style.lineStyle,
      locked: drawing.locked === true
    }
  }

  private aiCount(): number {
    let count = 0
    for (const drawing of this.drawJson) if (isAiDrawing(drawing.id)) count += 1
    return count
  }

  private afterDrawChange(): void {
    const draw = this.draw
    if (draw === null || this.mutingDraw) return
    this.drawTool = draw.activeTool()
    this.drawJson = draw.toJSON()
    this.store("draw", JSON.stringify(this.drawJson))
    this.cb.onDrawState({
      activeTool: this.drawTool,
      canUndo: draw.canUndo(),
      canRedo: draw.canRedo(),
      magnet: this.magnet,
      count: this.drawJson.length,
      aiCount: this.aiCount()
    })
    this.cb.onDrawSelection(this.drawSelection(draw))
  }

  async setDrawTool(toolId: string | null): Promise<void> {
    const draw = await this.ensureDrawing()
    if (draw === null) return
    try {
      draw.setTool(toolId)
    } catch {
      this.cb.onToast(`there is no drawing tool called "${toolId ?? ""}"`, "error")
      return
    }
    this.drawTool = draw.activeTool()
    this.afterDrawChange()
  }

  drawTools(): { group: string; tools: { id: string; name: string; shortcut?: string }[] }[] {
    const tier = this.drawTier
    if (tier === null) return []
    const known = new Map(tier.registeredDrawingTools().map((tool) => [tool.id, tool]))
    const out: { group: string; tools: { id: string; name: string; shortcut?: string }[] }[] = []
    const placed = new Set<string>()
    for (const [group, ids] of DRAW_GROUPS) {
      const tools: { id: string; name: string; shortcut?: string }[] = []
      for (const id of ids) {
        const tool = known.get(id)
        if (tool === undefined) continue
        placed.add(id)
        tools.push({ id: tool.id, name: tool.name, shortcut: tool.shortcut })
      }
      if (tools.length > 0) out.push({ group, tools })
    }
    // Anything the registry has that this grouping does not name. A tool added
    // by a later release reaches the rail rather than disappearing from it.
    const rest: { id: string; name: string; shortcut?: string }[] = []
    for (const tool of known.values()) {
      if (placed.has(tool.id)) continue
      rest.push({ id: tool.id, name: tool.name, shortcut: tool.shortcut })
    }
    if (rest.length > 0) out.push({ group: "Other", tools: rest })
    return out
  }

  updateDrawing(id: string, patch: Record<string, unknown>): void {
    const draw = this.draw
    if (draw === null) return
    const style: Record<string, unknown> = {}
    const change: { locked?: boolean; visible?: boolean; style?: DrawingStyle } = {}
    for (const [key, value] of Object.entries(patch)) {
      if (key === "locked") {
        if (typeof value === "boolean") change.locked = value
        continue
      }
      if (key === "visible") {
        if (typeof value === "boolean") change.visible = value
        continue
      }
      // points is deliberately not accepted: update(id, { points }) REPLACES the
      // anchor list while update(id, { style }) merges, so a partial points array
      // would silently reshape the drawing.
      if (key === "points") continue
      style[key] = value
    }
    if (Object.keys(style).length > 0) change.style = style as DrawingStyle
    draw.update(id, change)
    this.afterDrawChange()
  }

  removeDrawing(id: string): void {
    const draw = this.draw
    if (draw === null) return
    draw.remove(id)
    this.afterDrawChange()
  }

  /** Remove the analyst's markup only. A drawing the user placed by hand is
   *  never touched: the toolbar deletes those one at a time. */
  clearDrawings(): void {
    this.clearGroup(undefined)
  }

  undo(): void {
    this.draw?.undo()
    this.afterDrawChange()
  }

  redo(): void {
    this.draw?.redo()
    this.afterDrawChange()
  }

  setMagnet(on: boolean): void {
    this.magnet = on
    this.draw?.setOptions({ magnet: on })
    this.store("magnet", on ? "1" : "0")
    if (this.draw === null) {
      // The rail can toggle this before the controller exists (the tier is
      // still loading on a fresh page) and afterDrawChange reports nothing
      // without one, so the rail's own state is reported from here.
      this.cb.onDrawState({
        activeTool: this.drawTool,
        canUndo: false,
        canRedo: false,
        magnet: on,
        count: this.drawJson.length,
        aiCount: this.aiCount()
      })
      return
    }
    this.afterDrawChange()
  }

  /* pane furniture */

  setVolumeVisible(on: boolean): void {
    this.volumeVisible = on
    this.volume?.applyOptions({ visible: on })
    this.store("volume", on ? "1" : "0")
  }

  resetScale(): void {
    // Fits the content and re-enables autoscale on every pane, which is the only
    // way back once an axis drag has switched it off.
    this.chart?.resetScale()
  }

  setPriceScaleMode(mode: "linear" | "logarithmic" | "percentage" | "indexed-to-100"): void {
    this.scaleMode = mode
    this.store("scale", mode)
    // percentage and indexed-to-100 behave exactly like linear until the pane
    // feeds the scale a baseline, which it does on the next autoscale pass.
    this.chart?.setPriceScaleOptions({ mode }, "primary")
  }
}
