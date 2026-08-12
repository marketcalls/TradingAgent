/** Fetch wrappers for the non-streaming backend endpoints.
 *
 * Everything here is deliberately defensive about response shape. The backend
 * normalizes OpenAlgo, but OpenAlgo itself reports analyzer mode under several
 * different keys depending on the endpoint, and a session list is worth showing
 * even when one row is malformed. Nothing in this module throws for a cosmetic
 * failure; only the calls whose result the caller acts on can throw.
 */

export type TradingMode = "analyze" | "live" | "unknown"

export interface ModeInfo {
  mode: TradingMode
  broker: string | null
  note: string | null
  /** False when the backend or the broker connection could not be reached. */
  reachable: boolean
}

export interface HealthInfo {
  ok: boolean
  model: string | null
  missingKeys: string[]
  openalgoReachable: boolean | null
  mode: TradingMode
}

export interface SessionRow {
  session_id: string
  title: string
  updated_at: number | string | null
}

export interface TranscriptMessage {
  role: "user" | "assistant"
  content: string
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

export const JSON_HEADERS = { "Content-Type": "application/json" }

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/** Pulls a human message out of whatever the backend put in the error body. */
export function messageOf(payload: unknown, fallback: string): string {
  const record = asRecord(payload)
  if (record) {
    for (const key of ["detail", "message", "error"]) {
      const value = record[key]
      if (typeof value === "string" && value.trim()) return value
    }
  }
  return fallback
}

/** Turns any thrown value into a sentence the UI can show. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message || error.name
  return String(error)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      // A non-JSON body is still usable as an error message.
      payload = null
    }
  }
  if (!response.ok) {
    throw new ApiError(
      messageOf(payload ?? text, `${path} returned status ${response.status}`),
      response.status
    )
  }
  return (payload ?? {}) as T
}

const ANALYZE_WORDS = ["analyze", "analyse", "analyzer", "analyser", "sandbox", "simulated"]

/** OpenAlgo reports the mode as a string on one endpoint and a boolean on another,
 *  sometimes nested under data. Read every shape rather than guessing one. */
export function normalizeMode(payload: unknown): ModeInfo {
  const root = asRecord(payload)
  const nested = root ? asRecord(root.data) : null
  const sources = [root, nested].filter((entry): entry is Record<string, unknown> => entry !== null)

  let mode: TradingMode = "unknown"
  let broker: string | null = null
  let note: string | null = null

  for (const source of sources) {
    const raw = source.mode ?? source.analyzer_mode_name
    if (typeof raw === "string" && raw.trim()) {
      const lowered = raw.trim().toLowerCase()
      if (ANALYZE_WORDS.some((word) => lowered.includes(word))) mode = "analyze"
      else if (lowered.includes("live") || lowered.includes("real")) mode = "live"
    }
    if (mode === "unknown") {
      const flag = source.analyze_mode ?? source.analyzer_mode ?? source.analyze
      if (typeof flag === "boolean") mode = flag ? "analyze" : "live"
      const live = source.live ?? source.orders_are_real
      if (typeof live === "boolean") mode = live ? "live" : "analyze"
    }
    if (!broker && typeof source.broker === "string" && source.broker.trim()) broker = source.broker
    if (!note && typeof source.message === "string" && source.message.trim()) note = source.message
  }

  const ok = root?.ok
  const status = root?.status
  const reachable = ok === false || status === "error" ? false : mode !== "unknown"
  return { mode, broker, note, reachable }
}

export async function getMode(): Promise<ModeInfo> {
  try {
    const payload = await request<unknown>("/api/mode")
    return normalizeMode(payload)
  } catch (error) {
    return { mode: "unknown", broker: null, note: describeError(error), reachable: false }
  }
}

export async function getHealth(): Promise<HealthInfo> {
  const payload = await request<Record<string, unknown>>("/api/health")
  const missing = payload.missing_keys
  const openalgo = payload.openalgo
  const openalgoRecord = asRecord(openalgo)
  const reachable =
    typeof openalgo === "boolean"
      ? openalgo
      : typeof openalgoRecord?.ok === "boolean"
        ? (openalgoRecord.ok as boolean)
        : null
  return {
    ok: payload.ok !== false,
    model: typeof payload.model === "string" ? payload.model : null,
    missingKeys: Array.isArray(missing) ? missing.filter((key): key is string => typeof key === "string") : [],
    openalgoReachable: reachable,
    mode: normalizeMode(payload).mode
  }
}

export async function listSessions(): Promise<SessionRow[]> {
  const payload = await request<Record<string, unknown>>("/api/sessions")
  const raw = Array.isArray(payload) ? payload : payload.items
  if (!Array.isArray(raw)) return []
  const rows: SessionRow[] = []
  for (const entry of raw) {
    const record = asRecord(entry)
    if (!record) continue
    const id = record.session_id ?? record.id
    if (typeof id !== "string" || !id) continue
    const title = record.title ?? record.session_name ?? record.name
    rows.push({
      session_id: id,
      title: typeof title === "string" && title.trim() ? title : "New chat",
      updated_at:
        typeof record.updated_at === "number" || typeof record.updated_at === "string"
          ? record.updated_at
          : null
    })
  }
  return rows
}

export async function getSessionMessages(sessionId: string): Promise<TranscriptMessage[]> {
  const payload = await request<Record<string, unknown>>(`/api/sessions/${encodeURIComponent(sessionId)}`)
  const raw = payload.messages
  if (!Array.isArray(raw)) return []
  const messages: TranscriptMessage[] = []
  for (const entry of raw) {
    const record = asRecord(entry)
    if (!record) continue
    const role = record.role
    const content = record.content
    if (role !== "user" && role !== "assistant") continue
    if (typeof content !== "string" || !content.trim()) continue
    messages.push({ role, content })
  }
  return messages
}

export async function deleteSession(sessionId: string): Promise<void> {
  await request<unknown>(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" })
}

/** Cancellation is best effort: a run that already finished answers with an error
 *  and there is nothing useful for the user to do about it. */
export async function cancelRun(runId: string): Promise<boolean> {
  try {
    const payload = await request<Record<string, unknown>>(
      `/api/chat/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" }
    )
    return payload.cancelled !== false
  } catch {
    return false
  }
}
