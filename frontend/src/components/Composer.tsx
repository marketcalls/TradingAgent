/** Input, send and stop.
 *
 * Enter sends, Shift and Enter makes a newline. The textarea autogrows: its height
 * is reset to 0 before scrollHeight is read, because "auto" leaves the old box in
 * place and the field then never shrinks again.
 */

import { useEffect, useRef, type KeyboardEvent } from "react"
import { ArrowUp, Square } from "lucide-react"
import { cn } from "../lib/format"

interface ComposerProps {
  onSend: (text: string) => void
  onStop: () => void
  running: boolean
  /** Set while a confirmation is pending: the paused run owns the conversation. */
  blocked?: boolean
  blockedHint?: string
}

export default function Composer({ onSend, onStop, running, blocked, blockedHint }: ComposerProps) {
  const ref = useRef<HTMLTextAreaElement>(null)

  const resize = () => {
    const element = ref.current
    if (!element) return
    element.style.height = "0px"
    element.style.height = `${element.scrollHeight}px`
  }

  useEffect(() => {
    resize()
  }, [])

  const submit = () => {
    const element = ref.current
    if (!element) return
    const text = element.value.trim()
    if (!text || running || blocked) return
    onSend(text)
    element.value = ""
    resize()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div
      className="rounded-2xl border border-border bg-background p-2 shadow-l"
      onClick={(event) => {
        if (!(event.target as HTMLElement).closest("button")) ref.current?.focus()
      }}
    >
      <textarea
        ref={ref}
        rows={1}
        disabled={blocked}
        onInput={resize}
        onKeyDown={onKeyDown}
        placeholder={blocked ? blockedHint ?? "waiting for your decision" : "Ask about quotes, positions or place an order"}
        className="scroll-thin max-h-[200px] w-full resize-none bg-transparent px-2 py-1.5 text-base outline-none placeholder:text-muted-foreground disabled:opacity-60"
      />
      <div className="flex items-center justify-between gap-2 px-1 pt-1">
        <span className="text-xs text-muted-foreground">
          {blocked ? blockedHint ?? "a confirmation is pending" : "Orders always ask before they run"}
        </span>
        <button
          type="button"
          onClick={running ? onStop : submit}
          disabled={blocked && !running}
          className={cn(
            "flex h-8 w-8 items-center justify-center rounded-full text-primary-foreground disabled:opacity-40",
            running ? "bg-danger" : "bg-primary"
          )}
          aria-label={running ? "Stop the run" : "Send the message"}
        >
          {running ? <Square className="h-3.5 w-3.5" /> : <ArrowUp className="h-4 w-4" />}
        </button>
      </div>
    </div>
  )
}
