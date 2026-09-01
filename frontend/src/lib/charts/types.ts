/** The wire contract between the chart and the analyst. Both sides mirror this.
 *
 * Three vocabularies live here, and they are deliberately separate:
 *
 *   ChartContext     what the browser tells the backend about the chart the user
 *                    is looking at. Injected into every turn as ambient context,
 *                    never asked for. The reference interaction supplies no
 *                    symbol, exchange, interval or date, so all four come from
 *                    here or the feature does not work.
 *
 *   AnnotationShape  the markup vocabulary. Semantic, not visual: a shape says
 *                    "envelope" and "bearish", never a hex colour. The chart maps
 *                    tone to the active theme, so a theme swap re-tints AI markup
 *                    instead of stranding it on last session's palette. Anchors
 *                    are computed server-side; the model never writes a number
 *                    that lands on the canvas.
 *
 *   ChartCommand     everything the analyst can do to the chart. Drawing is one
 *                    op among several, because "add supertrend 3,10" and "switch
 *                    to 15 minute" travel the same path as "draw the channel".
 *
 * Times are UTC seconds everywhere, matching the chart engine's single internal
 * time model. Never milliseconds, never bar indices: an index shifts when older
 * history pages in, and a shape anchored to one slides off the bars it describes.
 */

/** One anchor in data space. */
export interface Anchor {
  /** UTC seconds. May sit between bars, or past the last one. */
  time: number
  price: number
}

/** Semantic colour. The chart resolves it against the active theme. */
export type Tone = "bullish" | "bearish" | "neutral"

/** The markup vocabulary. One shape may become several drawings on the chart. */
export type AnnotationShape =
  /** The primary shape: a closed band through real swing points. Highs run left
   *  to right, lows are reversed and appended, and the path is filled. Not a
   *  parallel channel - the boundaries step and bend, tracking the pivots. */
  | { kind: "envelope"; highs: Anchor[]; lows: Anchor[]; label?: string; tone?: Tone }
  | { kind: "trendline"; from: Anchor; to: Anchor; extendRight?: boolean; label?: string; tone?: Tone }
  /** A horizontal level. `ray` starts it at `time` instead of spanning the pane. */
  | { kind: "level"; price: number; time: number; ray?: boolean; label?: string; tone?: Tone }
  | { kind: "zone"; from: Anchor; to: Anchor; label?: string; tone?: Tone }
  /** A straight parallel channel, for when the user explicitly asks for one.
   *  `offset` is the signed price distance from the base line to the second rail. */
  | { kind: "channel"; from: Anchor; to: Anchor; offset: number; label?: string; tone?: Tone }
  | { kind: "fib"; from: Anchor; to: Anchor; levels?: number[]; label?: string; tone?: Tone }
  /** A labelled bubble that sits clear of price, with a leader line to `at`. */
  | { kind: "callout"; at: Anchor; seat: Anchor; text: string; tone?: Tone }
  /** A dot plus a price label, for naming a single pivot. */
  | { kind: "marker"; at: Anchor; text: string; tone?: Tone }

/** Everything the analyst can do to the chart. */
export type ChartCommand =
  /** Add markup under a group id. Replaces that group, leaves other groups and
   *  every hand-placed drawing untouched. */
  | { op: "draw"; group: string; shapes: AnnotationShape[] }
  /** Remove one group, or every AI group when `group` is absent. Never touches a
   *  drawing the user placed by hand. */
  | { op: "clear"; group?: string }
  | { op: "set_symbol"; symbol: string; exchange: string }
  | { op: "set_interval"; interval: string }
  | { op: "set_chart_type"; chartType: string }
  | { op: "add_indicator"; indicatorId: string; settings?: Record<string, unknown> }
  | { op: "remove_indicator"; instanceId?: string; indicatorId?: string }
  /** Move the viewport to a time range, so markup drawn off-screen is visible.
   *  Drawings never expand the price scale themselves. */
  | { op: "focus"; from: number; to: number }

/** One indicator currently on the chart. */
export interface ChartIndicator {
  instanceId: string
  indicatorId: string
  name: string
  paneIndex: number
  settings: Record<string, unknown>
}

/** What the analyst is told about the chart, every turn. */
export interface ChartContext {
  symbol: string
  exchange: string
  interval: string
  chartType: string
  /** Bars currently loaded, not bars visible. */
  barCount: number
  /** UTC seconds of the first and last loaded bar. */
  firstTime: number | null
  lastTime: number | null
  /** UTC seconds at the edges of the viewport. "Draw the visible highs and lows"
   *  is clipped to this, so it has to travel with the request. */
  visibleFrom: number | null
  visibleTo: number | null
  lastPrice: number | null
  indicators: ChartIndicator[]
  theme: "dark" | "light"
}

/** The id prefix that marks a drawing as the analyst's rather than the user's.
 *  The hit-test parser splits an external id on "#", so a group id must not
 *  contain one. */
export const AI_PREFIX = "ai"

/** True for a drawing the analyst placed. */
export function isAiDrawing(id: string): boolean {
  return id.startsWith(`${AI_PREFIX}:`)
}

/** Stable id for one drawing inside a group. */
export function aiDrawingId(group: string, index: number): string {
  return `${AI_PREFIX}:${group}:${index}`
}
