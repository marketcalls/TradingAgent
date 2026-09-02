/** One turn of conversation with the agent, as a hook.
 *
 * This is the orchestration App.tsx grew, lifted out so a second surface can
 * hold its own conversation without duplicating the event switch. The chat page
 * and the chart page each mount their own instance: separate messages, separate
 * session, separate cancellation. They share the transport and nothing else.
 *
 * Facts it is built around, all of them load-bearing:
 *   - The backend writes no "event:" lines. The discriminator is a "type" field
 *     inside the JSON, so an unrecognised frame is dropped in silence rather
 *     than throwing. A new event type that is not handled here simply does
 *     nothing, which is quiet and hard to debug, so every case is explicit.
 *   - A "confirm" frame is terminal for its request. Nothing on the chart page
 *     is confirmation-gated, but if a gated tool ever reaches it the run pauses
 *     server-side waiting for an approval this surface cannot post. So the run
 *     is cancelled on the spot rather than left orphaned, and the turn carries a
 *     plain error instead of appearing to hang.
 *   - "chart_command" arrives mid-run, before any prose. It is handed straight
 *     to the caller and never stored on a message: the canvas is the state, not
 *     the transcript. The one thing kept is a flag that a draw landed, because
 *     the follow-up chips need to know markup exists and no tool result says so.
 *   - A "done" frame carries the reason the run ended. Anything but "stop" means
 *     the answer never reached its conclusion, and the turn says so rather than
 *     trailing off mid-sentence as if that were the whole reply.
 *   - The chart context is read at send time, not at mount. The user can change
 *     symbol or timeframe between turns and the analyst must see the chart as it
 *     is now.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { describeError } from "./api"
import { cancelRun } from "./api"
import { isAbortError, streamChat, type ChatMessage, type StreamEvent } from "./sse"

export interface AgentStream {
  messages: ChatMessage[]
  running: boolean
  sessionId: string | null
  send(text: string): Promise<void>
  stop(): void
  reset(): void
}

export interface AgentStreamOptions {
  /** Chart instructions, applied the moment they arrive. Called mid-run, before
   *  the answer begins, which is the whole point of the separate frame. */
  onChartCommand?: (commands: unknown[]) => void
  /** Ambient context attached to every send. Read fresh each time. */
  context?: () => unknown
  /** Reasoning effort, or "" for the server default. */
  effort?: string
}

/** True for a command that puts at least one shape on the canvas. A "draw" with
 *  no shapes replaces its group with nothing, which is a clear by another name. */
function drawsSomething(command: unknown): boolean {
  if (!command || typeof command !== "object") return false
  const candidate = command as { op?: unknown; shapes?: unknown }
  return candidate.op === "draw" && Array.isArray(candidate.shapes) && candidate.shapes.length > 0
}

export function useAgentStream(options: AgentStreamOptions = {}): AgentStream {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [running, setRunning] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)

  const sessionIdRef = useRef<string | null>(null)
  const runIdRef = useRef<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Mirrored into refs so `send` can read the current value without listing them
  // as dependencies and being rebuilt on every render.
  const optionsRef = useRef(options)
  optionsRef.current = options

  useEffect(() => {
    return () => {
      abortRef.current?.abort()
    }
  }, [])

  const patchLast = useCallback((patch: (message: ChatMessage) => ChatMessage) => {
    setMessages((previous) => {
      if (previous.length === 0) return previous
      const next = previous.slice()
      next[next.length - 1] = patch(next[next.length - 1])
      return next
    })
  }, [])

  const adoptSession = useCallback((id: string | undefined) => {
    if (!id) return
    sessionIdRef.current = id
    setSessionId((current) => current ?? id)
  }, [])

  const handleEvent = useCallback(
    (event: StreamEvent) => {
      switch (event.type) {
        case "start":
          runIdRef.current = event.run_id
          adoptSession(event.session_id)
          break
        case "token":
          if (event.delta) {
            patchLast((message) => ({ ...message, content: message.content + event.delta }))
          }
          break
        case "tool_start":
          patchLast((message) => ({
            ...message,
            tools: [...(message.tools ?? []), { id: event.id, name: event.name, args: event.args }]
          }))
          break
        case "tool_end":
          patchLast((message) => ({
            ...message,
            tools: (message.tools ?? []).map((call) =>
              call.id === event.id
                ? { ...call, ok: event.ok, result: event.result, duration: event.duration ?? null }
                : call
            )
          }))
          break
        case "chart_command":
          // Straight to the canvas. Never stored on the message: a redraw on
          // reload would replay stale geometry against different bars. Only the
          // fact that markup landed is kept, for the follow-up chips.
          if (Array.isArray(event.commands)) {
            optionsRef.current.onChartCommand?.(event.commands)
            if (event.commands.some(drawsSomething)) {
              patchLast((message) => (message.drew ? message : { ...message, drew: true }))
            }
          }
          break
        case "confirm":
          // Nothing on this surface is gated, so reaching here means a tool was
          // registered that should not have been. The server has closed the
          // stream and parked the run until someone approves it, which nothing
          // here can do. Cancel it so it is not left orphaned, and say why.
          adoptSession(event.session_id)
          runIdRef.current = null
          void cancelRun(event.run_id)
          patchLast((message) => ({ ...message, error: "this page cannot approve actions" }))
          break
        case "done":
          adoptSession(event.session_id)
          if (event.reason === "incomplete") {
            patchLast((message) => ({
              ...message,
              cutShort: true,
              notices: [
                ...(message.notices ?? []),
                { level: "warning", message: "The answer was cut short before it finished." }
              ]
            }))
          } else if (event.reason === "cancelled") {
            patchLast((message) => ({
              ...message,
              cutShort: true,
              notices: [
                ...(message.notices ?? []),
                { level: "info", message: "The run was cancelled before the answer finished." }
              ]
            }))
          }
          break
        case "error":
          patchLast((message) => ({ ...message, error: event.message || "the run failed" }))
          break
        case "notice":
          patchLast((message) => ({
            ...message,
            notices: [...(message.notices ?? []), { level: event.level, message: event.message }]
          }))
          break
      }
    },
    [adoptSession, patchLast]
  )

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || abortRef.current) return
      // Cleared before the request goes out. Until this run's start frame lands
      // the ref would still name the previous run, and a stop() in that window
      // would cancel a run that has already finished.
      runIdRef.current = null
      setMessages((previous) => [
        ...previous,
        { role: "user", content: trimmed },
        { role: "assistant", content: "" }
      ])
      setRunning(true)
      const controller = new AbortController()
      abortRef.current = controller
      try {
        await streamChat(
          {
            message: trimmed,
            session_id: sessionIdRef.current,
            reasoning_effort: optionsRef.current.effort ?? "",
            chart_context: optionsRef.current.context?.()
          },
          { signal: controller.signal, onEvent: handleEvent }
        )
      } catch (error) {
        if (!isAbortError(error)) {
          patchLast((message) => ({ ...message, error: describeError(error) }))
        }
      } finally {
        setRunning(false)
        abortRef.current = null
      }
    },
    [handleEvent, patchLast]
  )

  const stop = useCallback(() => {
    const runId = runIdRef.current
    abortRef.current?.abort()
    // Best effort: the local abort has already stopped the read, so a failure
    // here only leaves the server finishing a run nobody is listening to.
    if (runId) void cancelRun(runId)
  }, [])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    setMessages([])
    setSessionId(null)
    sessionIdRef.current = null
    runIdRef.current = null
  }, [])

  return { messages, running, sessionId, send, stop, reset }
}
