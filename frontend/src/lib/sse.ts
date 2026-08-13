/** The streaming client, and the vocabulary the whole UI accumulates from it.
 *
 * Facts the transport depends on:
 *   - The backend writes no "event:" lines. The discriminator is a "type" field
 *     inside each JSON payload, which is why this uses fetch plus getReader and
 *     not EventSource: EventSource cannot POST and could not carry the message.
 *   - Frames are "data: {...}\n\n" and a chunk boundary can land mid-line, so the
 *     trailing partial line is carried across reads.
 *   - A "confirm" frame is terminal for its request. The server closes the stream
 *     and the run stays paused until /api/chat/confirm resumes it, which then
 *     speaks the same vocabulary from a fresh response body.
 *   - Once the response headers are out the status is already 200, so a mid-stream
 *     failure arrives as an "error" frame rather than an HTTP error.
 */

import { JSON_HEADERS, messageOf } from "./api"

export interface ToolExecutionPayload {
  tool_name?: string
  tool_args?: Record<string, unknown>
  tool_call_id?: string
}

/** Server-computed context attached to each requirement. The named fields are the
 *  ones the confirm card promotes; anything else still renders, generically. */
export interface ConfirmPreview {
  mode?: string
  notional?: number | string
  notional_value?: number | string
  current_position?: unknown
  available_funds?: number | string
  ltp?: number | string
  warnings?: unknown
  [key: string]: unknown
}

export interface ConfirmRequirement {
  id: string
  tool_execution?: ToolExecutionPayload
  preview?: ConfirmPreview | null
  [key: string]: unknown
}

export interface StartEvent {
  type: "start"
  run_id: string
  session_id: string
}

export interface TokenEvent {
  type: "token"
  delta: string
}

export interface ToolStartEvent {
  type: "tool_start"
  id: string
  name: string
  args?: Record<string, unknown>
}

export interface ToolEndEvent {
  type: "tool_end"
  id: string
  name: string
  ok: boolean
  result?: string
  duration?: number | null
}

export interface ConfirmEvent {
  type: "confirm"
  run_id: string
  session_id: string
  requirements: ConfirmRequirement[]
}

export interface DoneEvent {
  type: "done"
  reason: "stop" | "cancelled" | "incomplete"
  session_id?: string
  run_id?: string
}

export interface ErrorEvent {
  type: "error"
  message: string
  kind?: string | null
}

/** A server-side correction, emitted when the reply and the tool calls disagree - for
 *  instance the model describing an order it never actually placed. */
export interface NoticeEvent {
  type: "notice"
  level: "warning" | "info"
  message: string
}

export type StreamEvent =
  | StartEvent
  | TokenEvent
  | ToolStartEvent
  | ToolEndEvent
  | ConfirmEvent
  | DoneEvent
  | ErrorEvent
  | NoticeEvent

/** One tool call as the timeline accumulates it: opened by tool_start, closed by
 *  tool_end. A call with ok still undefined is still running. */
export interface ToolCall {
  id: string
  name: string
  args?: Record<string, unknown>
  ok?: boolean
  result?: string
  duration?: number | null
}

export interface ConfirmState {
  runId: string
  sessionId: string
  requirements: ConfirmRequirement[]
  /** True once a decision has been posted; a resolved card can never post again. */
  resolved: boolean
  submitting: boolean
  decisions: Record<string, boolean>
}

export interface ChatMessage {
  role: "user" | "assistant"
  content: string
  tools?: ToolCall[]
  error?: string
  confirm?: ConfirmState
  /** Server-side corrections about this turn, shown under the answer. */
  notices?: { level: string; message: string }[]
}

export interface ChatStreamBody {
  message: string
  session_id?: string | null
  /** "" or omitted uses the server default; otherwise none|minimal|low|medium|high. */
  reasoning_effort?: string
}

export interface ConfirmStreamBody {
  run_id: string
  session_id: string
  decisions: Record<string, boolean>
  notes: Record<string, string>
  /** Resume with the same effort the paused leg ran under. */
  reasoning_effort?: string
}

export interface StreamOptions {
  signal?: AbortSignal
  onEvent: (event: StreamEvent) => void
}

function dispatch(line: string, onEvent: (event: StreamEvent) => void): void {
  const trimmed = line.trimEnd()
  if (!trimmed.startsWith("data:")) return
  const raw = trimmed.slice(5).trim()
  if (!raw) return
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    // A frame that does not parse is dropped: the stream is still usable and the
    // run is still running, so failing the whole read would lose the answer.
    return
  }
  if (!parsed || typeof parsed !== "object") return
  const candidate = parsed as { type?: unknown }
  if (typeof candidate.type !== "string") return
  onEvent(parsed as StreamEvent)
}

async function consume(response: Response, onEvent: (event: StreamEvent) => void): Promise<void> {
  if (!response.ok) {
    const text = await response.text()
    let payload: unknown = text
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
    onEvent({
      type: "error",
      message: messageOf(payload, `the request failed with status ${response.status}`),
      kind: `http_${response.status}`
    })
    return
  }
  const body = response.body
  if (!body) {
    onEvent({ type: "error", message: "the response arrived with no readable body", kind: "no_body" })
    return
  }
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split("\n")
    buffer = lines.pop() ?? ""
    for (const line of lines) dispatch(line, onEvent)
  }
  buffer += decoder.decode()
  // A stream cut short can leave one complete frame without its trailing newline.
  dispatch(buffer, onEvent)
}

export async function streamChat(body: ChatStreamBody, options: StreamOptions): Promise<void> {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
    signal: options.signal
  })
  await consume(response, options.onEvent)
}

/** Resumes a paused run. The response speaks the same event vocabulary, including
 *  another confirm frame if the model asks for a second approval. */
export async function streamConfirm(body: ConfirmStreamBody, options: StreamOptions): Promise<void> {
  const response = await fetch("/api/chat/confirm", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
    signal: options.signal
  })
  await consume(response, options.onEvent)
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError"
}
