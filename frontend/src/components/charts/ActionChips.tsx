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
 */

import type { ChatMessage } from "../../lib/sse"
import type { Route } from "../Sidebar"

export type ActionChip =
  /** Sends `prompt` back to the analyst as a new turn. */
  | { kind: "prompt"; label: string; prompt: string }
  /** Leaves /charts. The target is one of the shell's own routes, taken from
   *  Sidebar's Route so a chip cannot point at a surface that does not exist. */
  | { kind: "navigate"; label: string; target: Route }

/** Tool names that put markup on the canvas. Matched loosely because the tool
 *  set is still growing and a new drawing tool must not silently lose its chip. */
const DREW = /(draw|annotat|envelope|channel|trendline|level|fib|zone|marker|callout)/i
const INDICATOR = /indicator|supertrend|overlay/i
/** Language that only the chat page can act on, since nothing here places orders. */
const EXECUTION = /\b(buy|sell|entry|entries|order|stop loss|stoploss|position|square off)\b/i

function ranTool(message: ChatMessage, pattern: RegExp): boolean {
  return (message.tools ?? []).some((call) => call.ok !== false && pattern.test(call.name))
}

interface ChartFacts {
  symbol: string
  interval: string
}

/** Up to two follow-ups for one finished answer. Empty while a turn is running,
 *  or when the turn produced nothing to follow up on. */
export function suggestChips(message: ChatMessage, chart: ChartFacts): ActionChip[] {
  if (message.role !== "assistant" || !message.content) return []

  if (ranTool(message, DREW)) {
    return [
      {
        kind: "prompt",
        label: "Project a target",
        prompt: "Project a target from the structure you just drew"
      },
      { kind: "prompt", label: "Clear the markup", prompt: "Clear the markup you drew" }
    ]
  }

  if (ranTool(message, INDICATOR)) {
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
