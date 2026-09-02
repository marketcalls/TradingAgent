/** The seam between the chart engine and React.
 *
 * The chart is a plain class behind a ref, never React state. A 60 fps tick
 * stream must not enter the render cycle, and the engine is a mutable object
 * graph with a running animation loop, which is not a value React should try to
 * compare. Only low-frequency, UI-shaped events cross back over, through the
 * callback bag below.
 *
 * This file is the contract. `ChartTerminal` implements it; the page and the
 * toolbar components consume it and know nothing else about the engine.
 */

import type { ChartCommand, ChartContext, ChartIndicator } from "./types"

/** One entry in the symbol search results. */
export interface SymbolRow {
  symbol: string
  exchange: string
  name?: string
  lotsize?: number | string
}

/** The intervals a broker actually offers, grouped for a menu. */
export interface IntervalGroups {
  seconds: string[]
  minutes: string[]
  hours: string[]
  days: string[]
  weeks: string[]
  months: string[]
}

/** One chart type in the switcher. */
export interface ChartTypeOption {
  value: string
  label: string
  /** Types that need the transform tier are grouped apart in the menu. */
  derived?: boolean
}

/** One indicator in the picker. */
export interface IndicatorOption {
  id: string
  name: string
  category: string
}

/** One field in a generated settings form. */
export interface SettingsField {
  key: string
  type: "number" | "boolean" | "color" | "text" | "select" | "source"
  label: string
  value: unknown
  default?: unknown
  min?: number
  max?: number
  step?: number
  options?: { label: string; value: unknown }[]
}

/** The payload behind an open indicator settings dialog. */
export interface IndicatorSettingsRequest {
  instanceId: string
  name: string
  inputs: SettingsField[]
  styleInputs: SettingsField[]
}

/** What the drawing toolbar needs to render itself. */
export interface DrawState {
  activeTool: string | null
  canUndo: boolean
  canRedo: boolean
  magnet: boolean
  /** Every drawing on the chart, the user's and the analyst's. */
  count: number
  /** The analyst's drawings only. The clear button acts on these and must not
   *  claim to clear "every drawing" when it leaves the user's alone. */
  aiCount?: number
}

/** The currently selected drawing, or null. */
export interface DrawSelection {
  id: string
  tool: string
  hasText: boolean
  color?: string
  lineWidth?: number
  lineStyle?: string
  locked?: boolean
}

export type WsState =
  | "connecting"
  | "open"
  | "live"
  | "reconnecting"
  | "closed"
  | "error"
  | "auth failed"

/** Low-frequency events the terminal pushes back to React.
 *
 * Every one of these is UI-shaped and rare. The crosshair readout is
 * deliberately absent: it is written straight to a DOM node the terminal owns,
 * because routing it through setState would re-render the page on every
 * pointer move.
 */
export interface TerminalCallbacks {
  onReady(info: { intervals: IntervalGroups; interval: string; chartType: string }): void
  onSymbolLoaded(sym: { symbol: string; exchange: string; name?: string }): void
  /** The interval or chart type changed from inside the terminal.
   *
   *  onReady carries both, but it fires once at startup. When the analyst changes
   *  either one the toolbar has no other way to hear about it, and it sat there
   *  reading 1h over a chart that had already reloaded as daily. */
  onViewChanged(view: { interval: string; chartType: string }): void
  onWsState(state: WsState): void
  onLastPrice(price: number): void
  onDrawState(state: DrawState): void
  onDrawSelection(selection: DrawSelection | null): void
  /** A text tool was placed and has nothing in it yet. */
  onDrawTextEdit(req: { id: string; tool: string; text: string }): void
  onIndicators(list: ChartIndicator[]): void
  onIndicatorSettings(req: IndicatorSettingsRequest): void
  onToast(message: string, tone: "info" | "error"): void
}

export interface TerminalOptions {
  /** The element the chart owns outright. The engine mutates its inline styles
   *  and ARIA attributes, so it must not be shared. */
  container: HTMLElement
  /** The DOM node the OHLC readout is written into. */
  legendEl: HTMLElement
  /** localStorage namespace. Must not collide with OpenAlgo's "oa-trading-". */
  storageKey: string
  getTheme(): "dark" | "light"
  callbacks: TerminalCallbacks
}

/** Everything the page and its toolbars can ask of the chart. */
export interface TerminalApi {
  init(): Promise<void>
  destroy(): void

  /** Ambient context for the analyst. Read fresh on every turn. */
  context(): ChartContext
  /** Apply one analyst command. Unknown ops are ignored, not thrown. */
  apply(command: ChartCommand): Promise<void>
  /** A PNG data URL of the current chart, for "analyse this screen". */
  snapshot(): Promise<string | null>

  searchSymbols(query: string, exchange?: string): Promise<SymbolRow[]>
  setSymbol(symbol: string, exchange: string): Promise<void>
  setInterval(interval: string): Promise<void>
  setChartType(chartType: string): Promise<void>
  chartTypes(): ChartTypeOption[]
  applyTheme(): void

  indicatorCatalogue(): Promise<IndicatorOption[]>
  addIndicator(id: string, settings?: Record<string, unknown>): Promise<void>
  removeIndicator(instanceId: string): void
  openIndicatorSettings(instanceId: string): Promise<void>
  applyIndicatorSettings(instanceId: string, patch: Record<string, unknown>): void
  resetIndicatorSettings(instanceId: string): Promise<void>

  setDrawTool(toolId: string | null): Promise<void>
  drawTools(): { group: string; tools: { id: string; name: string; shortcut?: string }[] }[]
  updateDrawing(id: string, patch: Record<string, unknown>): void
  removeDrawing(id: string): void
  clearDrawings(): void
  undo(): void
  redo(): void
  setMagnet(on: boolean): void

  setVolumeVisible(on: boolean): void
  resetScale(): void
  setPriceScaleMode(mode: "linear" | "logarithmic" | "percentage" | "indexed-to-100"): void
}
