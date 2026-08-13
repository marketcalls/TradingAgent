/** One turn in the thread.
 *
 * User turns get a bubble, assistant turns do not: the answer is the page, not a
 * card on it. Assistant markdown goes through remark-gfm because most trading
 * answers are tables. While the first token is still on its way a shimmering label
 * stands in for the answer. It only says Thinking when the model is genuinely
 * reasoning: a non-reasoning model, or one with thinking switched off, says Working
 * instead, because claiming otherwise would be a lie about what is happening.
 */

import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import type { ChatMessage } from "../lib/sse"
import ToolTimeline from "./ToolTimeline"

interface MessageProps {
  message: ChatMessage
  /** True only for the last assistant turn while its run is still open. */
  streaming: boolean
  /** True when this model reasons AND thinking is currently enabled. */
  thinking?: boolean
}

export default function Message({ message, streaming, thinking = false }: MessageProps) {
  if (message.role === "user") {
    return (
      <div className="mb-6 flex justify-end" data-user-msg>
        <div className="max-w-[80%] whitespace-pre-wrap rounded-xl bg-chat-user px-3 py-2 text-base">
          {message.content}
        </div>
      </div>
    )
  }

  const tools = message.tools ?? []
  const waiting = streaming && !message.content

  return (
    <div className="mb-6 pr-10">
      <ToolTimeline tools={tools} running={streaming} />
      {waiting ? (
        <span className="shimmer text-base">{thinking ? "Thinking" : "Working"}</span>
      ) : message.content ? (
        <div className="md text-base">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
        </div>
      ) : null}
      {(message.notices ?? []).map((notice, i) => (
        <div
          key={i}
          className="mt-3 rounded-xl border border-warn-border bg-warn-soft px-3 py-2 text-sm text-warn"
        >
          {notice.message}
        </div>
      ))}
      {message.error ? (
        <div className="mt-3 rounded-xl border border-danger-border px-3 py-2 text-sm text-danger">
          {message.error}
        </div>
      ) : null}
    </div>
  )
}
