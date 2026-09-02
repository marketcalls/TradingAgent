/** The two follow-up chips under an answer.
 *
 * Two, never five. The reference recording offers exactly two, and both are
 * concrete actions on the object that was just drawn rather than a menu of
 * everything the analyst can do. A third chip turns a suggestion into a form.
 *
 * A chip declares what it is: a chip either re-prompts the analyst or navigates
 * away. The distinction is load-bearing on this page, because /charts cannot
 * reach a broker at all. When an answer starts talking about entries and stops,
 * the honest follow-up is a link to the chat page, which owns the order tools
 * and the confirmation gate, not a prompt that would be refused here.
 *
 * No SSE frame carries chips: the stream vocabulary in lib/sse.ts has no member
 * for them and this page does not own that file. They are therefore derived from
 * what the finished turn actually did, which is visible in its tool calls. That
 * is a narrower signal than a server-authored suggestion, and deliberately so: a
 * chip only appears for something the turn demonstrably performed.
 *
 * The tool names are exact sets, not patterns. A pattern that read "indicator"
 * matched list_chart_indicators, one that read "draw" matched clear_annotations,
 * and a chip offering to project a target from markup that had just been
 * cleared is worse than no chip. A new tool earns its chip by being named here.
 *
 * "Drew" additionally needs the page to have seen a draw frame that turn.
 * draw_envelope answers ok when it finds nothing to fit, so the tool result is
 * not proof that anything is on the canvas; the frame is.
 */

import type { ChatMessage } from "../../lib/sse"
import type { Route } from "../Sidebar"

export type ActionChip =
  /** Sends `prompt` back to the analyst as a new turn. */
  | { kind: "prompt"; label: string; prompt: string }
  /** Leaves /charts. The target is one of the shell's own routes, taken from
   *  Sidebar's Route so a chip cannot point at a surface that does not exist. */
  | { kind: "navigate"; label: string; target: Route }

/** Tools that put markup on the canvas. Exact names, see the header. */
const DREW = new Set([
  "draw_envelope",
  "draw_trendline",
  "draw_levels",
  "draw_zone",
  "project_targets"
])
/** The one tool that adds a study. Listing and removing are not adding. */
const ADDED_INDICATOR = new Set(["add_chart_indicator"])
/** Order language only, which is what the chat page exists for. "position" is
 *  deliberately absent: it is ordinary analyst prose ("price is in a position
 *  to break out") far more often than it is an instruction. */
const EXECUTION = /\b(buy|sell|entry|entries|stop[ -]?loss|square off)\b/i

function ranTool(message: ChatMessage, names: Set<string>): boolean {
  return (message.tools ?? []).some((call) => call.ok !== false && names.has(call.name))
}

interface ChartFacts {
  symbol: string
  interval: string
  /** True when the symbol or the view changed after this turn finished. The
   *  chips that act on "the structure you just drew" are then about a chart
   *  the user is no longer looking at, and are withheld. */
  stale: boolean
}

/** Up to two follow-ups for one finished answer. Empty while a turn is running,
 *  or when the turn produced nothing to follow up on. */
export function suggestChips(message: ChatMessage, chart: ChartFacts): ActionChip[] {
  if (message.role !== "assistant" || !message.content) return []
  // An answer that never reached its conclusion has nothing to follow up on.
  if (message.cutShort) return []

  if (!chart.stale && message.drew === true && ranTool(message, DREW)) {
    return [
      {
        kind: "prompt",
        label: "Project a target",
        prompt: "Project a target from the structure you just drew"
      },
      { kind: "prompt", label: "Clear the markup", prompt: "Clear the markup you drew" }
    ]
  }

  if (ranTool(message, ADDED_INDICATOR)) {
    return [
      {
        kind: "prompt",
        label: "Read the indicator",
        prompt: "What is that indicator saying right now?"
      },
      {
        kind: "prompt",
        label: "Draw the structure",
        prompt: "Draw the structure you can see on this chart"
      }
    ]
  }

  // Tested last: the tool-derived chips above are proof of an action, and this
  // one is only a reading of the prose.
  if (EXECUTION.test(message.content)) {
    return [
      {
        kind: "prompt",
        label: "What invalidates this",
        prompt: "What would invalidate this read?"
      },
      { kind: "navigate", label: "Take this to chat", target: "chat" }
    ]
  }

  const symbol = chart.symbol || "this chart"
  return [
    {
      kind: "prompt",
      label: "Draw what you see",
      prompt: `Draw the structure you can see on ${symbol}`
    },
    {
      kind: "prompt",
      label: "Trend and momentum",
      prompt: `What is the trend and momentum on the ${chart.interval || "current"} chart?`
    }
  ]
}

interface ActionChipsProps {
  chips: ActionChip[]
  /** Set while a run is open: a chip must not queue a second turn. */
  disabled?: boolean
  onSelect: (chip: ActionChip) => void
}

export default function ActionChips({ chips, disabled, onSelect }: ActionChipsProps) {
  if (chips.length === 0) return null

  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {chips.slice(0, 2).map((chip) => (
        <button
          key={`${chip.kind}:${chip.label}`}
          type="button"
          disabled={disabled}
          onClick={() => onSelect(chip)}
          title={chip.kind === "prompt" ? chip.prompt : `Go to ${chip.target}`}
          className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
        >
          {chip.label}
        </button>
      ))}
    </div>
  )
}
