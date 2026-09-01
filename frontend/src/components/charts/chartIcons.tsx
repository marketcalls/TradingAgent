/** Engine ids to glyphs. One map, one fallback, never a blank button.
 *
 * The chart engine names its chart types and its forty-odd drawing tools as
 * plain strings, and both lists arrive at runtime from `chartTypes()` and
 * `drawTools()`. Resolving them in one module rather than inside each toolbar
 * keeps a long switch out of every component, and gives every lookup a
 * fallback: an id this build has never seen still renders a glyph instead of an
 * empty square, which is the failure mode that makes a rail look broken.
 *
 * Lucide covers most of it. The handful it has no glyph for (a fibonacci
 * ladder, a horizontal ray, a parallel channel, a gann fan, a point and figure
 * column) are drawn here as inline SVG on lucide's own grid: 24 by 24, a 2px
 * currentColor stroke, round caps and joins. That is what makes them sit at the
 * same weight beside the real ones at h-3.5 w-3.5. Inline SVG only, never an
 * icon font and never a character pressed into service as a picture.
 */

import type { ReactNode } from "react"
import {
  Activity,
  ArrowUpRight,
  AudioWaveform,
  Brush,
  ChartArea,
  ChartCandlestick,
  ChartColumn,
  ChartLine,
  ChartNoAxesCombined,
  ChartScatter,
  Circle,
  Crosshair,
  Diamond,
  Flag,
  Highlighter,
  MessageCircle,
  MessageSquare,
  MousePointer2,
  MoveHorizontal,
  MoveUpRight,
  MoveVertical,
  Ruler,
  Signpost,
  Spline,
  Square,
  StickyNote,
  Table,
  Tag,
  TrendingDown,
  TrendingUp,
  Triangle,
  Type,
  Waves,
  Waypoints
} from "lucide-react"

/** Every icon here takes exactly one prop, so lucide's icons and the hand-drawn
 *  ones below are interchangeable at every call site. */
export type ChartIcon = (props: { className?: string }) => ReactNode

function Glyph({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      focusable="false"
      aria-hidden="true"
      className={className}
    >
      {children}
    </svg>
  )
}

function HorizontalLineGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 12h18" />
    </Glyph>
  )
}

function HorizontalRayGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M6 12h15" />
      <circle cx="4" cy="12" r="1.5" fill="currentColor" stroke="none" />
    </Glyph>
  )
}

function VerticalLineGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M12 3v18" />
    </Glyph>
  )
}

function ExtendedLineGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 19 21 5" />
      <circle cx="9" cy="15" r="1.4" fill="currentColor" stroke="none" />
      <circle cx="15" cy="9" r="1.4" fill="currentColor" stroke="none" />
    </Glyph>
  )
}

function ChannelGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 15 21 5" />
      <path d="M3 20 21 10" />
    </Glyph>
  )
}

function FibGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 5h18" />
      <path d="M3 10h18" />
      <path d="M3 14h18" />
      <path d="M3 19h18" />
    </Glyph>
  )
}

function GannGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M4 20h17" />
      <path d="M4 20 21 11" />
      <path d="M4 20 21 3" />
      <path d="M4 20 13 3" />
    </Glyph>
  )
}

function CyclesGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M4 5v14" />
      <path d="M10 5v14" />
      <path d="M20 5v14" />
    </Glyph>
  )
}

function OhlcBarGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M12 3v18" />
      <path d="M7 8h5" />
      <path d="M12 15h5" />
    </Glyph>
  )
}

function HighLowGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M8 4v16" />
      <path d="M16 8v9" />
    </Glyph>
  )
}

function StepGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 18h4v-5h4V9h4V5h6" />
    </Glyph>
  )
}

function PointFigureGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M4 7 9 12" />
      <path d="M9 7 4 12" />
      <circle cx="16.5" cy="15" r="3.5" />
    </Glyph>
  )
}

function SolidLineGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 12h18" />
    </Glyph>
  )
}

function DashedLineGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 12h18" strokeDasharray="5 4" />
    </Glyph>
  )
}

function DottedLineGlyph({ className }: { className?: string }) {
  return (
    <Glyph className={className}>
      <path d="M3 12h18" strokeDasharray="0.5 4" />
    </Glyph>
  )
}

const CHART_TYPE_ICONS: Record<string, ChartIcon> = {
  candlestick: ChartCandlestick,
  "hollow-candle": ChartCandlestick,
  "volume-candle": ChartCandlestick,
  "heikin-ashi": ChartCandlestick,
  range: ChartCandlestick,
  bar: OhlcBarGlyph,
  "high-low": HighLowGlyph,
  line: ChartLine,
  "line-markers": ChartScatter,
  step: StepGlyph,
  area: ChartArea,
  "hlc-area": ChartArea,
  baseline: ChartNoAxesCombined,
  column: ChartColumn,
  histogram: ChartColumn,
  renko: Diamond,
  "line-break": StepGlyph,
  kagi: StepGlyph,
  "point-figure": PointFigureGlyph
}

const DRAW_TOOL_ICONS: Record<string, ChartIcon> = {
  "trend-line": TrendingUp,
  ray: MoveUpRight,
  "extended-line": ExtendedLineGlyph,
  "info-line": Ruler,
  "trend-angle": Triangle,
  arrow: ArrowUpRight,
  "horizontal-line": HorizontalLineGlyph,
  "horizontal-ray": HorizontalRayGlyph,
  "vertical-line": VerticalLineGlyph,
  "cross-line": Crosshair,
  "parallel-channel": ChannelGlyph,
  "disjoint-channel": ChannelGlyph,
  "flat-channel": ChannelGlyph,
  "regression-trend": ChartScatter,
  rectangle: Square,
  "rotated-rectangle": Diamond,
  ellipse: Circle,
  circle: Circle,
  triangle: Triangle,
  polyline: Waypoints,
  path: Waypoints,
  arc: Spline,
  curve: Spline,
  "double-curve": Spline,
  "sine-line": AudioWaveform,
  brush: Brush,
  highlighter: Highlighter,
  "cyclic-lines": CyclesGlyph,
  "time-cycles": CyclesGlyph,
  text: Type,
  note: StickyNote,
  comment: MessageSquare,
  balloon: MessageCircle,
  callout: MessageSquare,
  signpost: Signpost,
  "flag-mark": Flag,
  "price-label": Tag,
  "price-note": Tag,
  table: Table,
  measure: Ruler,
  "price-range": MoveVertical,
  "date-range": MoveHorizontal,
  forecast: Activity,
  "long-position": TrendingUp,
  "short-position": TrendingDown
}

/** Group names come from the engine and are free text, so this matches on a
 *  substring rather than an exact key. */
const DRAW_GROUP_MATCHERS: { match: string; icon: ChartIcon }[] = [
  { match: "fib", icon: FibGlyph },
  { match: "gann", icon: GannGlyph },
  { match: "channel", icon: ChannelGlyph },
  { match: "cycle", icon: CyclesGlyph },
  { match: "line", icon: TrendingUp },
  { match: "shape", icon: Square },
  { match: "brush", icon: Brush },
  { match: "annotation", icon: Type },
  { match: "text", icon: Type },
  { match: "note", icon: StickyNote },
  { match: "measure", icon: Ruler },
  { match: "projection", icon: Ruler },
  { match: "position", icon: TrendingUp },
  { match: "pattern", icon: Waypoints },
  { match: "wave", icon: Waves }
]

export function chartTypeIcon(value: string): ChartIcon {
  return CHART_TYPE_ICONS[value] ?? ChartCandlestick
}

export function drawToolIcon(id: string): ChartIcon {
  const exact = DRAW_TOOL_ICONS[id]
  if (exact) return exact
  if (id.startsWith("fib")) return FibGlyph
  if (id.includes("gann")) return GannGlyph
  if (id.includes("channel")) return ChannelGlyph
  if (id.includes("cycle")) return CyclesGlyph
  if (id.includes("text") || id.includes("label") || id.includes("note")) return Type
  if (id.includes("line")) return HorizontalLineGlyph
  return MousePointer2
}

/** The rail shows one button per group, so a group needs a glyph of its own.
 *  Falls back to the first tool in the group, which is nearly always the one a
 *  trader would recognise the group by. */
export function drawGroupIcon(group: string, firstToolId?: string): ChartIcon {
  const lowered = group.trim().toLowerCase()
  for (const entry of DRAW_GROUP_MATCHERS) {
    if (lowered.includes(entry.match)) return entry.icon
  }
  return firstToolId ? drawToolIcon(firstToolId) : MousePointer2
}

export function lineStyleIcon(style: string): ChartIcon {
  const lowered = style.trim().toLowerCase()
  if (lowered === "dashed") return DashedLineGlyph
  if (lowered === "dotted") return DottedLineGlyph
  return SolidLineGlyph
}
