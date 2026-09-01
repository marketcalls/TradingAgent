/** The one status line that sits above an analyst answer.
 *
 * It is a line replaced in place, never a growing log, which is the whole point:
 * the reference recording shows a single sentence that changes as the turn moves
 * through its phases, then collapses to a duration once the answer starts.
 *
 *   submit to the first tool call   Thinking...
 *   while a tool runs              a human phrase for that tool, ending in ...
 *   once content arrives           Thought for 27s
 *
 * Facts this is built around:
 *   - The phrases come from ToolTimeline's TOOL_LABELS, extended here with the
 *     chart tools. Extending rather than rebuilding keeps one vocabulary: a tool
 *     that already reads well in the chat thread reads the same way here.
 *   - No event carries the elapsed time. Agno emits no such field and the SSE
 *     vocabulary in lib/sse.ts has nowhere to put one, so the number is measured
 *     client-side and passed in. This component only renders it.
 *   - The sweep is the .shimmer class, not a spinner: the word itself moves.
 *   - A tool that has already returned still names the phase until the next one
 *     opens, because a gap of a second showing nothing reads as a stall.
 */

import type { ChatMessage } from "../../lib/sse"
import { labelize } from "../../lib/format"
import { TOOL_LABELS } from "../ToolTimeline"

/** The chat vocabulary plus the chart analyst's own tools. */
const CHART_TOOL_LABELS: Record<string, string> = {
  ...TOOL_LABELS,
  get_chart_frame: "Reading the chart frame",
  list_chart_indicators: "Looking through the indicators",
  get_chart_context: "Reading the chart",
  capture_chart: "Looking at the chart",
  find_swing_points: "Finding the swing points",
  find_support_resistance: "Clustering support and resistance",
  find_trendlines: "Fitting the trendlines",
  detect_patterns: "Looking for patterns",
  project_target: "Projecting a target",
  analyse_trend: "Reading the trend",
  analyze_trend: "Reading the trend",
  analyse_momentum: "Reading momentum",
  analyze_momentum: "Reading momentum",
  analyse_structure: "Reading the structure",
  analyze_structure: "Reading the structure",
  analyse_volatility: "Reading volatility",
  analyze_volatility: "Reading volatility",
  draw_envelope: "Drawing the envelope",
  draw_channel: "Drawing the channel",
  draw_trendline: "Drawing the trendline",
  draw_level: "Marking the level",
  draw_levels: "Marking the levels",
  draw_zone: "Shading the zone",
  draw_fib: "Drawing the Fibonacci levels",
  draw_callout: "Labelling the chart",
  draw_marker: "Marking the pivot",
  annotate_chart: "Marking up the chart",
  clear_annotations: "Clearing the markup",
  clear_chart: "Clearing the markup",
  set_chart_symbol: "Switching the symbol",
  set_chart_interval: "Switching the timeframe",
  set_chart_type: "Switching the chart type",
  add_chart_indicator: "Adding the indicator",
  remove_chart_indicator: "Removing the indicator",
  focus_chart: "Moving the viewport"
}

/** A human phrase for one tool name, falling back to a readable form of the name. */
export function chartToolLabel(name: string): string {
  return CHART_TOOL_LABELS[name] ?? labelize(name)
}

interface StatusLineProps {
  /** The assistant turn this line belongs to. */
  message: ChatMessage
  /** True only while this turn's run is still open. */
  running: boolean
  /** Seconds measured from the first frame to the first content, or null while
   *  the turn is still live. */
  seconds: number | null
}

export default function StatusLine({ message, running, seconds }: StatusLineProps) {
  const tools = message.tools ?? []

  if (running && !message.content) {
    const open = tools.filter((call) => call.ok === undefined)
    const current = open.length > 0 ? open[open.length - 1] : tools[tools.length - 1]
    return (
      <div className="mb-2 text-sm">
        <span className="shimmer">
          {current ? `${chartToolLabel(current.name)}...` : "Thinking..."}
        </span>
      </div>
    )
  }

  if (seconds === null) return null

  return <div className="mb-2 text-xs text-muted-foreground">Thought for {seconds}s</div>
}
