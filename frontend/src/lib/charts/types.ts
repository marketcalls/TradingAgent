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
 *   AnnotationShape  the markup vocabulary. Semantic first: a shape says
 *                    "envelope" and "bearish", and the chart maps the tone to the
 *                    active theme, so a theme swap re-tints AI markup instead of
 *                    stranding it on last session's palette. The optional
 *                    ShapeStyle fields are the exception, added because "draw it
 *                    in red, dashed" had no way to travel: a literal colour, dash
 *                    or width is written as given and is the same in both
 *                    themes. Anchors are computed server-side; the model never
 *                    writes a number that lands on the canvas.
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

/** How a line is stroked. The chart engine's own three patterns. */
export type LineStyle = "solid" | "dashed" | "dotted"

/** Optional visual overrides carried by every shape. Each one is written as
 *  given when present and falls back to the kind's own default when absent:
 *  colour to the tone palette, the rest to the drawing tool's defaults. */
export interface ShapeStyle {
  /** A colour literal (hex or rgb). Overrides the tone. */
  color?: string
  lineStyle?: LineStyle
  lineWidth?: number
  /** 0..1, for the kinds that shade an area (a text note's plate included). */
  fillOpacity?: number
}

/** The markup vocabulary. One shape may become several drawings on the chart. */
export type AnnotationShape = ShapeStyle &
  (
    /** The primary shape: a closed band through real swing points. Highs run
     *  left to right, lows are reversed and appended, and the path is filled.
     *  Not a parallel channel - the boundaries step and bend, tracking the
     *  pivots. */
    | { kind: "envelope"; highs: Anchor[]; lows: Anchor[]; label?: string; tone?: Tone }
    | {
        kind: "trendline"
        from: Anchor
        to: Anchor
        extendRight?: boolean
        label?: string
        tone?: Tone
      }
    /** A horizontal level. `ray` starts it at `time` instead of spanning the pane. */
    | { kind: "level"; price: number; time: number; ray?: boolean; label?: string; tone?: Tone }
    | { kind: "zone"; from: Anchor; to: Anchor; label?: string; tone?: Tone }
    /** A straight parallel channel, for when the user explicitly asks for one.
     *  `offset` is the signed price distance from the base line to the second
     *  rail. */
    | { kind: "channel"; from: Anchor; to: Anchor; offset: number; label?: string; tone?: Tone }
    | { kind: "fib"; from: Anchor; to: Anchor; levels?: number[]; label?: string; tone?: Tone }
    /** A labelled bubble that sits clear of price, with a leader line to `at`. */
    | { kind: "callout"; at: Anchor; seat: Anchor; text: string; tone?: Tone }
    /** A dot plus a price label, for naming a single pivot. */
    | { kind: "marker"; at: Anchor; text: string; tone?: Tone }
    /** An arrow mark with its tip on `at`: "up" sits under the anchor pointing
     *  at it, "down" hangs above it. Without a tone, up is bullish and down is
     *  bearish. `text` becomes a caption beside the mark. */
    | { kind: "arrow"; at: Anchor; direction: "up" | "down"; text?: string; tone?: Tone }
    /** A free-standing note on a plate, its top-left corner on `at`. */
    | { kind: "text"; at: Anchor; text: string; tone?: Tone }
  )

/** Everything the analyst can do to the chart. */
export type ChartCommand =
  /** Add markup under a group id. Replaces that group, leaves other groups and
   *  every hand-placed drawing untouched. With `append`, the group is kept and
   *  the shapes are added to it: the "notes" group accumulates one note per
   *  call this way, and a clear of that group removes them all. */
  | { op: "draw"; group: string; shapes: AnnotationShape[]; append?: boolean }
  /** Remove one group, or every AI group when `group` is absent. Never touches a
   *  drawing the user placed by hand. */
  | { op: "clear"; group?: string }
  | { op: "set_symbol"; symbol: string; exchange: string }
  | { op: "set_interval"; interval: string }
  | { op: "set_chart_type"; chartType: string }
  | { op: "add_indicator"; indicatorId: string; settings?: Record<string, unknown> }
  | { op: "remove_indicator"; instanceId?: string; indicatorId?: string }
  /** Restyle an indicator already on the chart. `instanceId` names one
   *  instance; failing that, the first instance of `indicatorId` is patched. */
  | {
      op: "update_indicator"
      indicatorId: string
      instanceId?: string
      settings: Record<string, unknown>
    }
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
  /** The analyst groups whose markup is still on the chart, so the backend can
   *  tell what it drew earlier from what the user has since cleared. Absent on
   *  an older chart build. */
  analystGroups?: string[]
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
