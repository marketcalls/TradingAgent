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
 *   - The elapsed time in "Thought for 27s" is measured here. No event carries
 *     it. The clock starts when the run opens, which is the closest observable
 *     instant to the first frame that the hook exposes, and stops the moment the
 *     first content token lands. Both readings are kept per turn index, because a
 *     finished turn keeps showing its own duration once the next one starts.
 *   - The turn index is found by scanning back for the last assistant message
 *     rather than taking messages.length - 1, so the timing survives a hook that
 *     appends the user turn without an empty assistant turn beside it.
 *   - The textarea autogrows by the pattern in Composer.tsx: height is reset to 0
 *     before scrollHeight is read, because "auto" leaves the old box in place and
 *     the field then never shrinks again.
 *   - The newest question is pinned to the top so the answer streams into empty
 *     space below it, the same trick App.tsx uses on the thread.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react"
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

interface AnalystPanelProps {
  messages: ChatMessage[]
  running: boolean
  /** Named back at the user in the header and in the follow-up chips, so a
   *  wrong-symbol answer is caught by reading one line. */
  symbol: string
  interval: string
  onSend: (text: string) => void
  onStop: () => void
  onReset: () => void
  onCollapse: () => void
  /** Present only when the host can route away from /charts. Without it, a
   *  navigating chip has nowhere to go and is not offered. */
  onNavigate?: (target: Route) => void
}

export default function AnalystPanel({
  messages,
  running,
  symbol,
  interval,
  onSend,
  onStop,
  onReset,
  onCollapse,
  onNavigate
}: AnalystPanelProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const startedRef = useRef<number | null>(null)
  const [seconds, setSeconds] = useState<Record<number, number>>({})

  const lastAssistant = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "assistant") return index
    }
    return -1
  }, [messages])

  const answered = lastAssistant >= 0 && Boolean(messages[lastAssistant].content)

  // Start on the first sign of the turn.
  useEffect(() => {
    if (running && startedRef.current === null) startedRef.current = Date.now()
  }, [running])

  // Stop when the answer begins, or when the run ends having produced none.
  useEffect(() => {
    const started = startedRef.current
    if (started === null) return
    if (running && !answered) return
    startedRef.current = null
    if (lastAssistant < 0) return
    const elapsed = Math.max(0, Math.round((Date.now() - started) / 1000))
    setSeconds((current) =>
      current[lastAssistant] === undefined ? { ...current, [lastAssistant]: elapsed } : current
    )
  }, [running, answered, lastAssistant])

  // A new thread drops every measurement with the transcript it belonged to.
  useEffect(() => {
    if (messages.length === 0) {
      startedRef.current = null
      setSeconds({})
    }
  }, [messages.length])

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
    if (!text || running) return
    onSend(text)
    element.value = ""
    resize()
  }, [running, onSend, resize])

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
      if (!running) onSend(chip.prompt)
    },
    [running, onSend, onNavigate]
  )

  // Only the newest answered turn offers follow-ups, and a navigating one is
  // dropped when the host gave no way to navigate.
  const chips = useMemo(() => {
    if (running || lastAssistant < 0) return []
    const suggested = suggestChips(messages[lastAssistant], { symbol, interval })
    return onNavigate ? suggested : suggested.filter((chip) => chip.kind !== "navigate")
  }, [running, lastAssistant, messages, symbol, interval, onNavigate])

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
                  onClick={() => onSend(starter)}
                  className="rounded-full border border-border px-2.5 py-1 text-left text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
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
              chips={index === lastAssistant ? chips : []}
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
            placeholder="Ask about this chart"
            className="scroll-thin max-h-[160px] w-full resize-none bg-transparent px-1.5 py-1 text-sm outline-none placeholder:text-muted-foreground"
          />
          <div className="flex items-center justify-between gap-2 px-0.5 pt-1">
            <span className="text-[11px] text-muted-foreground">Nothing here can place an order</span>
            <button
              type="button"
              onClick={running ? onStop : submit}
              className={cn(
                "flex h-7 w-7 items-center justify-center rounded-full text-primary-foreground",
                running ? "bg-danger" : "bg-primary"
              )}
              aria-label={running ? "Stop the run" : "Send the question"}
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
