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
 *     is confirmation-gated, but if a gated tool ever reaches it the run would
 *     pause server-side and this hook would look hung. So the frame is surfaced
 *     as an error rather than ignored.
 *   - "chart_command" arrives mid-run, before any prose. It is handed straight
 *     to the caller and never stored on a message: the canvas is the state, not
 *     the transcript.
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
          // reload would replay stale geometry against different bars.
          if (Array.isArray(event.commands)) optionsRef.current.onChartCommand?.(event.commands)
          break
        case "confirm":
          // Nothing on this surface is gated, so reaching here means a tool was
          // registered that should not have been. The run is paused server-side
          // and will never resume, so say so rather than appearing to hang.
          adoptSession(event.session_id)
          patchLast((message) => ({
            ...message,
            error:
              "this action needs approval, which this page cannot give. Use the chat page instead."
          }))
          break
        case "done":
          adoptSession(event.session_id)
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
