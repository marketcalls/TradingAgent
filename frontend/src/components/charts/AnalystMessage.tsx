/** One turn in the analyst panel.
 *
 * The same shape as Message.tsx in the chat thread, narrowed for a 380px column:
 * a user question gets a bubble, an answer does not, and the answer is streamed
 * markdown through react-markdown with remark-gfm and the shared `.md` class, so
 * a table in an analyst answer renders exactly as it does in the thread.
 *
 * Two differences from the thread, both deliberate:
 *   - The tool timeline is replaced by StatusLine. In a side panel beside a chart
 *     a growing list of expandable tool cards competes with the thing the user is
 *     actually looking at, so the tools collapse to one sentence.
 *   - Body text is text-sm rather than text-base. The column is less than half
 *     the width of the thread and the chart is the primary surface.
 *
 * The user bubble carries data-user-msg because the panel pins the newest
 * question to the top by querying for it, the same trick App.tsx uses.
 *
 * A confirmation can never legitimately arrive here: /charts holds no tool that
 * mutates broker state. If one does, the run is paused on the server with no card
 * to resolve it, so the turn says so plainly rather than looking merely idle.
 */

import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import type { ChatMessage } from "../../lib/sse"
import ActionChips, { type ActionChip } from "./ActionChips"
import StatusLine from "./StatusLine"

interface AnalystMessageProps {
  message: ChatMessage
  /** True only for the last assistant turn while its run is still open. */
  running: boolean
  /** Client-side elapsed seconds for this turn, or null while it is still live. */
  seconds: number | null
  /** Follow-ups for this turn. Only the newest answer is given any. */
  chips: ActionChip[]
  onChip: (chip: ActionChip) => void
}

export default function AnalystMessage({
  message,
  running,
  seconds,
  chips,
  onChip
}: AnalystMessageProps) {
  if (message.role === "user") {
    return (
      <div className="mb-4 flex justify-end" data-user-msg>
        <div className="max-w-[90%] whitespace-pre-wrap rounded-xl bg-chat-user px-2.5 py-1.5 text-sm">
          {message.content}
        </div>
      </div>
    )
  }

  return (
    <div className="mb-5">
      <StatusLine message={message} running={running} seconds={seconds} />

      {message.content ? (
        <div className="md text-sm">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
        </div>
      ) : null}

      {(message.notices ?? []).map((notice, index) => (
        <div
          key={index}
          className="mt-2 rounded-lg border border-warn-border bg-warn-soft px-2.5 py-1.5 text-xs text-warn"
        >
          {notice.message}
        </div>
      ))}

      {message.confirm && !message.confirm.resolved ? (
        <div className="mt-2 rounded-lg border border-warn-border bg-warn-soft px-2.5 py-1.5 text-xs text-warn">
          This asked for an approval, and nothing on the charts page can approve one. Open the chat
          page to act on it.
        </div>
      ) : null}

      {message.error ? (
        <div className="mt-2 rounded-lg border border-danger-border px-2.5 py-1.5 text-xs text-danger">
          {message.error}
        </div>
      ) : null}

      <ActionChips chips={chips} disabled={running} onSelect={onChip} />
    </div>
  )
}
