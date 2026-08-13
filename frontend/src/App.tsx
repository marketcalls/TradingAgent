/** Layout, session state and stream orchestration.
 *
 * Facts this file is built around:
 *   - A confirm frame closes the stream. The run stays paused on the server until
 *     /api/chat/confirm resumes it, so the UI must stop treating itself as running
 *     while still holding the card, and the composer is blocked until a decision
 *     is posted: a new message would abandon the paused run.
 *   - The resumed run speaks the same event vocabulary, so both legs share one
 *     event handler. A fresh assistant turn is pushed on resume so the answer
 *     continues below the card rather than growing above it.
 *   - The model reasons for roughly 1.7 seconds before the first content token,
 *     which is why an empty assistant turn is pushed up front and shows Thinking.
 *   - Tool cards do not survive a reload: transcripts are text only.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import Sidebar from "./components/Sidebar"
import ModeBanner from "./components/ModeBanner"
import ThinkingSelector from "./components/ThinkingSelector"
import Message from "./components/Message"
import ConfirmCard from "./components/ConfirmCard"
import Composer from "./components/Composer"
import {
  cancelRun,
  deleteSession,
  describeError,
  getHealth,
  getSessionMessages,
  listSessions,
  type ModeInfo,
  type SessionRow,
  type TradingMode, type ReasoningInfo } from "./lib/api"
import {
  isAbortError,
  streamChat,
  streamConfirm,
  type ChatMessage,
  type ConfirmState,
  type StreamEvent
} from "./lib/sse"

const THEME_KEY = "oa-theme"

const STARTERS = [
  "What is my current P&L and which positions are open?",
  "Quote RELIANCE and compute a 14 period RSI on daily bars",
  "Show the NIFTY option chain for the nearest expiry",
  "List my holdings with current value and day change"
]

type Theme = "dark" | "light"

function readTheme(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light"
}

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [mode, setMode] = useState<TradingMode>("unknown")
  const [reasoning, setReasoning] = useState<ReasoningInfo | null>(null)
  // Thinking defaults to off. The server's own default is adopted only when the model
  // cannot be silenced, so the stored preference never asks for something impossible.
  const [effort, setEffort] = useState<string>(
    () => localStorage.getItem("oa-thinking") ?? "none"
  )
  const [model, setModel] = useState<string | null>(null)
  const [health, setHealth] = useState<string | null>(null)
  const [theme, setTheme] = useState<Theme>(readTheme)

  const runIdRef = useRef<string | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const pendingConfirm = messages.reduce<ConfirmState | null>(
    (found, message) => (message.confirm && !message.confirm.resolved ? message.confirm : found),
    null
  )

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

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await listSessions())
    } catch {
      // The sidebar is non-critical; a failed list must not disturb the thread.
    }
  }, [])

  useEffect(() => {
    void refreshSessions()
    void (async () => {
      try {
        const info = await getHealth()
        setModel(info.model)
        if (info.missingKeys.length > 0) {
          setHealth(`missing configuration: ${info.missingKeys.join(", ")}`)
        } else if (info.openalgoReachable === false) {
          setHealth("the OpenAlgo server is not reachable, so market data and orders will fail")
        }
      } catch (error) {
        setHealth(describeError(error))
      }
    })()
  }, [refreshSessions])

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark")
    document.documentElement.classList.toggle("light", theme !== "dark")
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      // Private browsing can refuse storage; the theme still applies for this tab.
    }
  }, [theme])

  // Pins the new question to the top so the answer streams into empty space below.
  useEffect(() => {
    const container = scrollRef.current
    if (!container) return
    const nodes = container.querySelectorAll("[data-user-msg]")
    const last = nodes[nodes.length - 1] as HTMLElement | undefined
    if (!last) return
    container.scrollTo({ top: last.offsetTop - container.offsetTop - 16, behavior: "smooth" })
  }, [messages.length])

  const onMode = useCallback((info: ModeInfo) => {
    setMode(info.mode)
    setReasoning(info.reasoning)
    // A stored preference can be impossible for the current model, for instance "none"
    // on a model that always reasons. Fall back to the server default in that case.
    const supported = info.reasoning?.supported ?? []
    if (supported.length > 0) {
      setEffort((current) =>
        supported.includes(current) ? current : info.reasoning?.current || supported[0]
      )
    }
  }, [])

  useEffect(() => {
    localStorage.setItem("oa-thinking", effort)
  }, [effort])

  // Only claim the model is thinking when it can AND the user has not switched it off.
  const thinkingActive = Boolean(reasoning?.modelThinks) && effort !== "none"

  const effortRef = useRef(effort)
  useEffect(() => {
    effortRef.current = effort
  }, [effort])

  const handleEvent = useCallback(
    (event: StreamEvent) => {
      switch (event.type) {
        case "start":
          runIdRef.current = event.run_id
          adoptSession(event.session_id)
          break
        case "token":
          if (event.delta) patchLast((message) => ({ ...message, content: message.content + event.delta }))
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
        case "confirm":
          runIdRef.current = event.run_id
          adoptSession(event.session_id)
          patchLast((message) => ({
            ...message,
            confirm: {
              runId: event.run_id,
              sessionId: event.session_id,
              requirements: event.requirements ?? [],
              resolved: false,
              submitting: false,
              decisions: {}
            }
          }))
          break
        case "done":
          adoptSession(event.session_id)
          break
        case "error":
          patchLast((message) => ({ ...message, error: event.message || "the run failed" }))
          break
      }
    },
    [adoptSession, patchLast]
  )

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || running || pendingConfirm) return
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
            reasoning_effort: effortRef.current,
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
        void refreshSessions()
      }
    },
    [running, pendingConfirm, handleEvent, patchLast, refreshSessions]
  )

  const patchConfirm = useCallback((runId: string, patch: Partial<ConfirmState>) => {
    setMessages((previous) =>
      previous.map((message) =>
        message.confirm && message.confirm.runId === runId
          ? { ...message, confirm: { ...message.confirm, ...patch } }
          : message
      )
    )
  }, [])

  const submitConfirm = useCallback(
    async (state: ConfirmState, decisions: Record<string, boolean>, notes: Record<string, string>) => {
      if (running || state.resolved || state.submitting) return
      // Marked resolved before the request goes out: a second click, or a card left
      // over from a reloaded page, must never post a decision twice.
      patchConfirm(state.runId, { resolved: true, submitting: true, decisions })
      setRunning(true)
      setMessages((previous) => [...previous, { role: "assistant", content: "" }])
      const controller = new AbortController()
      abortRef.current = controller
      try {
        await streamConfirm(
          {
            run_id: state.runId,
            session_id: state.sessionId,
            decisions,
            notes,
            reasoning_effort: effortRef.current,
          },
          { signal: controller.signal, onEvent: handleEvent }
        )
      } catch (error) {
        if (!isAbortError(error)) {
          patchLast((message) => ({ ...message, error: describeError(error) }))
        }
      } finally {
        patchConfirm(state.runId, { submitting: false })
        setRunning(false)
        abortRef.current = null
        void refreshSessions()
      }
    },
    [running, handleEvent, patchConfirm, patchLast, refreshSessions]
  )

  const stop = useCallback(async () => {
    const runId = runIdRef.current
    abortRef.current?.abort()
    if (runId) await cancelRun(runId)
  }, [])

  const newChat = useCallback(() => {
    if (running) return
    setMessages([])
    setSessionId(null)
    sessionIdRef.current = null
    runIdRef.current = null
  }, [running])

  const openSession = useCallback(
    async (id: string) => {
      if (running) return
      try {
        const transcript = await getSessionMessages(id)
        setMessages(transcript.map((entry) => ({ role: entry.role, content: entry.content })))
        setSessionId(id)
        sessionIdRef.current = id
        runIdRef.current = null
      } catch (error) {
        setHealth(describeError(error))
      }
    },
    [running]
  )

  const removeSession = useCallback(
    async (id: string) => {
      if (running) return
      try {
        await deleteSession(id)
      } catch {
        // Already gone is the same outcome the user asked for.
      }
      if (sessionIdRef.current === id) newChat()
      void refreshSessions()
    },
    [running, newChat, refreshSessions]
  )

  const lastIndex = messages.length - 1

  return (
    <div className="flex h-full bg-background text-foreground">
      <Sidebar
        sessions={sessions}
        activeId={sessionId}
        busy={running}
        theme={theme}
        model={model}
        onNewChat={newChat}
        onOpen={(id) => void openSession(id)}
        onDelete={(id) => void removeSession(id)}
        onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-border px-4 py-2">
          <ModeBanner onMode={onMode} />
          {health ? <span className="truncate text-xs text-danger">{health}</span> : null}
          <div className="ml-auto">
            <ThinkingSelector
              reasoning={reasoning}
              value={effort}
              onChange={setEffort}
              disabled={running}
            />
          </div>
        </header>

        <div ref={scrollRef} className="scroll-thin min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-thread px-4 py-8">
            {messages.length === 0 ? (
              <div className="pt-16">
                <h1 className="text-2xl font-semibold">OpenAlgo Trading Agent</h1>
                <p className="mt-2 text-sm text-muted-foreground">
                  Quotes, indicators, positions and order execution through OpenAlgo. Every order
                  asks for your approval first.
                </p>
                <div className="mt-6 flex flex-wrap gap-2">
                  {STARTERS.map((starter) => (
                    <button
                      key={starter}
                      type="button"
                      onClick={() => void send(starter)}
                      className="rounded-full border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      {starter}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((message, index) => (
                <div key={index}>
                  <Message
                    message={message}
                    streaming={running && index === lastIndex}
                    thinking={thinkingActive}
                  />
                  {message.confirm ? (
                    <ConfirmCard
                      state={message.confirm}
                      mode={mode}
                      onSubmit={(decisions, notes) =>
                        void submitConfirm(message.confirm as ConfirmState, decisions, notes)
                      }
                    />
                  ) : null}
                </div>
              ))
            )}
          </div>
        </div>

        <div className="px-4 pb-5">
          <div className="mx-auto w-full max-w-composer">
            <Composer
              onSend={(text) => void send(text)}
              onStop={() => void stop()}
              running={running}
              blocked={Boolean(pendingConfirm)}
              blockedHint="approve or reject the pending action to continue"
            />
          </div>
        </div>
      </div>
    </div>
  )
}
