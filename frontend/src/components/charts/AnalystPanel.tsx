/** The analyst column beside the chart.
 *
 * It renders the stream, nothing more: the transport, the event switch and
 * cancellation all live in useAgentStream, and the chart commands never come
 * through here at all. Markup is applied by the page the moment its frame lands,
 * which is why an envelope can appear on the canvas while this column still says
 * "Finding the swing points...". The two are independent streams and this one is
 * always the slower of them.
 *
 * Facts this file is built around:
 *   - The "Thought for 27s" clock is not measured here. This column unmounts
 *     when it is collapsed, and a measurement taken in it went with it: collapse
 *     mid-run and every later turn read 0s. The page owns the readings, keyed
 *     by turn index, and hands them down.
 *   - Likewise the index of the last assistant turn is the page's, since the
 *     page needs it for the clock. It is found by scanning back rather than
 *     taking messages.length - 1, so the timing survives a hook that appends
 *     the user turn without an empty assistant turn beside it.
 *   - Nothing sends until the chart has reported ready. A starter clicked
 *     before that went out with an empty context, and the analyst answered
 *     about no chart at all.
 *   - Memoised, with every callback it receives stable, so a page render that
 *     is not about the conversation (a symbol change, a toast) does not touch
 *     the transcript. Older turns are given one shared empty chip list so
 *     AnalystMessage's own memo holds for them.
 *   - The textarea autogrows by the pattern in Composer.tsx: height is reset to 0
 *     before scrollHeight is read, because "auto" leaves the old box in place and
 *     the field then never shrinks again.
 *   - The newest question is pinned to the top so the answer streams into empty
 *     space below it, the same trick App.tsx uses on the thread.
 */

import { memo, useCallback, useEffect, useMemo, useRef, type KeyboardEvent } from "react"
import { ArrowUp, PanelRightClose, Plus, Square } from "lucide-react"
import type { ChatMessage } from "../../lib/sse"
import type { Route } from "../Sidebar"
import { cn } from "../../lib/format"
import AnalystMessage from "./AnalystMessage"
import { suggestChips, type ActionChip } from "./ActionChips"

/** Drawn from what the analyst can actually do, not from what sounds impressive. */
const STARTERS = [
  "Analyse this chart",
  "What is the trend and momentum",
  "Draw the channel connecting the visible highs and lows",
  "Add supertrend 3,10"
]

/** One shared instance, so a turn with no chips keeps a referentially equal prop. */
const NO_CHIPS: ActionChip[] = []

interface AnalystPanelProps {
  messages: ChatMessage[]
  running: boolean
  /** True once the chart has reported ready. Until then nothing is sent. */
  ready: boolean
  /** Named back at the user in the header and in the follow-up chips, so a
   *  wrong-symbol answer is caught by reading one line. */
  symbol: string
  interval: string
  /** Index of the newest assistant turn, or -1. Owned by the page. */
  lastAssistant: number
  /** Elapsed seconds per turn index, measured by the page. */
  seconds: Record<number, number>
  /** True when the chart changed after the newest turn finished, so chips
   *  bound to what that turn drew are no longer offered. */
  chipsStale: boolean
  onSend: (text: string) => void
  onStop: () => void
  onReset: () => void
  onCollapse: () => void
  /** Present only when the host can route away from /charts. Without it, a
   *  navigating chip has nowhere to go and is not offered. */
  onNavigate?: (target: Route) => void
}

function AnalystPanel({
  messages,
  running,
  ready,
  symbol,
  interval,
  lastAssistant,
  seconds,
  chipsStale,
  onSend,
  onStop,
  onReset,
  onCollapse,
  onNavigate
}: AnalystPanelProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const container = scrollRef.current
    if (!container) return
    const nodes = container.querySelectorAll("[data-user-msg]")
    const last = nodes[nodes.length - 1] as HTMLElement | undefined
    if (!last) return
    container.scrollTo({ top: last.offsetTop - container.offsetTop - 12, behavior: "smooth" })
  }, [messages.length])

  const resize = useCallback(() => {
    const element = inputRef.current
    if (!element) return
    element.style.height = "0px"
    element.style.height = `${element.scrollHeight}px`
  }, [])

  useEffect(() => {
    resize()
  }, [resize])

  const submit = useCallback(() => {
    const element = inputRef.current
    if (!element) return
    const text = element.value.trim()
    if (!text || running || !ready) return
    onSend(text)
    element.value = ""
    resize()
  }, [running, ready, onSend, resize])

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault()
        submit()
      }
    },
    [submit]
  )

  const onChip = useCallback(
    (chip: ActionChip) => {
      if (chip.kind === "navigate") {
        onNavigate?.(chip.target)
        return
      }
      if (!running && ready) onSend(chip.prompt)
    },
    [running, ready, onSend, onNavigate]
  )

  // Only the newest answered turn offers follow-ups, and a navigating one is
  // dropped when the host gave no way to navigate.
  const chips = useMemo(() => {
    if (running || lastAssistant < 0) return NO_CHIPS
    const suggested = suggestChips(messages[lastAssistant], { symbol, interval, stale: chipsStale })
    const offered = onNavigate ? suggested : suggested.filter((chip) => chip.kind !== "navigate")
    return offered.length === 0 ? NO_CHIPS : offered
  }, [running, lastAssistant, messages, symbol, interval, chipsStale, onNavigate])

  return (
    <aside className="flex h-full w-[380px] shrink-0 flex-col border-l border-border bg-sidebar">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0">
          <div className="text-sm font-semibold">Chart analyst</div>
          <div className="truncate text-[11px] text-muted-foreground">
            {symbol ? `${symbol} on ${interval || "the loaded interval"}` : "waiting for the chart"}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={onReset}
            disabled={running || messages.length === 0}
            className="flex h-7 w-7 items-center justify-center rounded-lg border border-border hover:bg-muted disabled:opacity-40"
            aria-label="Start a new analyst thread"
            title="New thread"
          >
            <Plus className="h-3.5 w-3.5 shrink-0" />
          </button>
          <button
            type="button"
            onClick={onCollapse}
            className="flex h-7 w-7 items-center justify-center rounded-lg border border-border hover:bg-muted"
            aria-label="Collapse the analyst panel"
            title="Collapse"
          >
            <PanelRightClose className="h-3.5 w-3.5 shrink-0" />
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {messages.length === 0 ? (
          <div>
            <p className="text-sm text-muted-foreground">
              Ask about the chart in front of you. The symbol, the interval and the visible range
              travel with every question, so there is no need to repeat them.
            </p>
            <div className="mt-4 flex flex-col items-start gap-2">
              {STARTERS.map((starter) => (
                <button
                  key={starter}
                  type="button"
                  disabled={!ready}
                  onClick={() => onSend(starter)}
                  title={ready ? starter : "Waiting for the chart to load"}
                  className="rounded-full border border-border px-2.5 py-1 text-left text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                >
                  {starter}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((message, index) => (
            <AnalystMessage
              key={index}
              message={message}
              running={running && index === lastAssistant}
              seconds={seconds[index] ?? null}
              chips={index === lastAssistant ? chips : NO_CHIPS}
              onChip={onChip}
            />
          ))
        )}
      </div>

      <div className="px-3 pb-3">
        <div
          className="rounded-xl border border-border bg-background p-2 shadow-l"
          onClick={(event) => {
            if (!(event.target as HTMLElement).closest("button")) inputRef.current?.focus()
          }}
        >
          <textarea
            ref={inputRef}
            rows={1}
            onInput={resize}
            onKeyDown={onKeyDown}
            placeholder={ready ? "Ask about this chart" : "Waiting for the chart to load"}
            className="scroll-thin max-h-[160px] w-full resize-none bg-transparent px-1.5 py-1 text-sm outline-none placeholder:text-muted-foreground"
          />
          <div className="flex items-center justify-between gap-2 px-0.5 pt-1">
            <span className="text-[11px] text-muted-foreground">Nothing here can place an order</span>
            <button
              type="button"
              disabled={!running && !ready}
              onClick={running ? onStop : submit}
              className={cn(
                "flex h-7 w-7 items-center justify-center rounded-full text-primary-foreground disabled:opacity-40",
                running ? "bg-danger" : "bg-primary"
              )}
              aria-label={running ? "Stop the run" : "Send the question"}
              title={
                running ? "Stop the run" : ready ? "Send the question" : "Waiting for the chart to load"
              }
            >
              {running ? (
                <Square className="h-3 w-3 shrink-0" />
              ) : (
                <ArrowUp className="h-3.5 w-3.5 shrink-0" />
              )}
            </button>
          </div>
        </div>
      </div>
    </aside>
  )
}

export default memo(AnalystPanel)
