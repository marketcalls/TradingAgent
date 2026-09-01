/** The chart engine, as a plain class React never looks inside.
 *
 * There is no React import in this file and there must never be one. The engine
 * is a mutable object graph with a running animation loop, and a depth stream
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
 *     away, so they are snapshotted before and re-applied in the rebuild tail.
 *     Only a chart-type change rebuilds: setTheme is a live swap and an interval
 *     change is a data reload, both of which keep the user's zoom.
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
 *     and re-arms autoscale, which is the whole remedy the engine offers.
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
import { annotationSpan, buildAnnotations, inGroup } from "./annotations"
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
import {
  emptyIntervals,
  flattenIntervals,
  historyRange,
  intervalSeconds,
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
  private interval = "5m"
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

  private loadToken = 0
  private loadingOlder = false
  private noMoreHistory = false
  private destroyed = false

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

    try {
      this.intervals = await getIntervals()
    } catch (error) {
      this.cb.onToast(describe(error), "error")
    }
    if (this.destroyed) return
    this.interval = pickInterval(this.intervals, this.interval)

    const socket = new OaSocket(socketUrl(wsPath))
    this.socket = socket
    this.sessionOff.push(socket.onState((state) => this.reportWs(state)))
    this.live = new ProxyDataFeed(socket)
    // Warm loading matters here because the same series is reloaded constantly:
    // interval pills, a symbol flicked and flicked back, a page refresh. The
    // cache never stores the forming bar, so a hit is short by at most the bar
    // the live subscription is about to supply anyway.
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

    this.build()
    await this.loadSymbolInfo(this.symbolInfo.symbol, this.symbolInfo.exchange)
    if (this.destroyed) return
    await this.reload()
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

  private build(): void {
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
      legendOffset: { top: 34, left: 10 }
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
    this.frameViewport()

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
    chart.setHistoryLoader(() => void this.pageHistory())

    this.buildLegend()
    this.writeLegend(null)
    void this.restoreAfterBuild()
  }

  /** Everything a rebuild threw away, put back in the order it is wanted. */
  private async restoreAfterBuild(): Promise<void> {
    await this.ensureDrawing()
    if (this.destroyed) return
    await this.reapplyIndicators()
  }

  /* data */

  private boxSize(): number {
    const last = this.rawBars.length > 0 ? this.rawBars[this.rawBars.length - 1].close : 100
    const tick = this.tickSize()
    const stepped = Math.round((last * 0.0015) / tick) * tick
    return Math.max(tick, Number(stepped.toFixed(this.decimals())))
  }

  private makeTransform(kind: TransformKind, tier: TransformTier): ISeriesTransform {
    const box = this.boxSize()
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

  private async loadSymbolInfo(symbol: string, exchange: string): Promise<void> {
    const info = await getSymbolInfo(symbol, exchange)
    if (this.destroyed) return
    this.symbolInfo = info
    this.store("symbol", info.symbol)
    this.store("exchange", info.exchange)
    this.price?.applyOptions({ precision: this.decimals() })
    this.buildLegend()
    this.cb.onSymbolLoaded({ symbol: info.symbol, exchange: info.exchange, name: info.name })
  }

  /** Reload history for the current symbol and interval, then resubscribe.
   *
   *  A token guards against a slow response for a symbol the user has already
   *  moved off: without it, an interval flicked twice can land the first
   *  response after the second and chart the wrong timeframe.
   */
  private async reload(): Promise<void> {
    const feed = this.feed
    if (feed === null) return
    const token = ++this.loadToken
    this.teardownLive()
    const to = nowSec()
    const range = historyRange(this.interval, to)
    let bars: Bar[] = []
    try {
      bars = await feed.getBars({
        symbol: this.symbolInfo.symbol,
        exchange: this.symbolInfo.exchange,
        interval: this.interval,
        from: range.from,
        to: range.to
      })
    } catch (error) {
      if (this.destroyed || token !== this.loadToken) return
      this.cb.onToast(describe(error), "error")
    }
    if (this.destroyed || token !== this.loadToken) return
    this.rawBars = bars
    this.noMoreHistory = false
    this.setPriceData()
    this.frameViewport()
    this.lastPrice = bars.length > 0 ? bars[bars.length - 1].close : null
    if (this.lastPrice !== null) this.cb.onLastPrice(this.lastPrice)
    this.writeLegend(null)
    this.subscribeLive()
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
      { seedFrom: seed }
    )
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
      theme: this.getTheme()
    }
  }

  async apply(command: ChartCommand): Promise<void> {
    switch (command.op) {
      case "draw":
        await this.applyGroup(command.group, command.shapes)
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

  /* analyst markup */

  private async applyGroup(group: string, shapes: AnnotationShape[]): Promise<void> {
    const draw = await this.ensureDrawing()
    if (draw === null) return
    this.mutingDraw = true
    try {
      this.removeGroup(draw, group)
      const drawings = buildAnnotations(group, shapes, tonePalette(this.getTheme()), 0)
      for (const drawing of drawings) {
        try {
          // add(), not fromJSON: fromJSON replaces the whole model and wipes
          // undo, redo and selection, so applying the analyst's markup would
          // throw away the user's own history. One add per shape also means the
          // markup can be undone shape by shape.
          draw.add(drawing)
        } catch (error) {
          this.cb.onToast(describe(error), "error")
        }
      }
      this.aiGroups.set(group, [...shapes])
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
    const draw = this.draw
    if (draw === null) return
    this.mutingDraw = true
    try {
      for (const drawing of draw.toJSON()) {
        const mine = group === undefined ? isAiDrawing(drawing.id) : inGroup(drawing.id, group)
        if (mine) draw.remove(drawing.id)
      }
    } finally {
      this.mutingDraw = false
    }
    if (group === undefined) this.aiGroups.clear()
    else this.aiGroups.delete(group)
    this.afterDrawChange()
  }

  /** Re-colour the analyst's drawings for the active theme.
   *
   *  DrawingStyle stores a literal colour string, so nothing re-tints itself: a
   *  theme swap leaves AI markup on the old palette until it is written again.
   *  The ids are deterministic, so the shapes are rebuilt against the new
   *  palette and only the style is pushed across, which merges.
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
    if (symbol === this.symbolInfo.symbol && exchange === this.symbolInfo.exchange) return
    this.teardownLive()
    this.prevClose = null
    this.lastPrice = null
    await this.loadSymbolInfo(symbol, exchange)
    if (this.destroyed) return
    await this.reload()
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
    this.buildLegend()
    await this.reload()
  }

  async setChartType(chartType: string): Promise<void> {
    const def = CHART_TYPES.find((entry) => entry.value === chartType)
    if (def === undefined) {
      this.cb.onToast(`there is no chart type called "${chartType}"`, "error")
      return
    }
    if (def.transform !== undefined && (await this.ensureTransformTier()) === null) return
    if (this.destroyed) return
    this.chartTypeId = def.value
    this.store("charttype", def.value)
    // A series cannot change type in place, so this is the one action that
    // rebuilds. build() snapshots the drawings and re-applies everything else.
    this.build()
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

  private async reapplyIndicators(): Promise<void> {
    if (this.tracked.length === 0) return
    if (!(await this.ensureIndicatorTier())) return
    const chart = this.chart
    if (chart === null || this.destroyed) return
    // Re-adding walks the tracked list, so a sync mid-loop would read a
    // half-applied chart and truncate it.
    this.applyingIndicators = true
    try {
      for (const record of this.tracked) {
        if (!hasIndicator(record.indicatorId)) continue
        try {
          chart.addIndicator(record.indicatorId, record.settings)
        } catch {
          // One indicator that will not rebuild must not cost the others.
        }
      }
    } finally {
      this.applyingIndicators = false
    }
    this.syncIndicators()
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
    chart.removeIndicator(instanceId)
    this.syncIndicators()
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
    const controller = new tier.DrawingController(chart, {
      magnet: this.magnet,
      stayInDrawingMode: false,
      // Raised well above the default 50 so a thirty-shape analysis cannot push
      // the user's own edits off the undo stack.
      historyLimit: 200
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
      this.chartOff.push(chart.on(name, () => this.afterDrawChange()))
    }
    this.chartOff.push(
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
      count: this.drawJson.length
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
