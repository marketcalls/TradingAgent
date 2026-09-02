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

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react"
import Sidebar, { type Route } from "./components/Sidebar"
import { cn } from "./lib/format"
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

/** Split out of the main bundle: the chart engine and its tiers are the largest
 *  thing in the app, and the chat page must not pay for them up front. The
 *  chunk is fetched on the first visit to /charts, which is also when the page
 *  is first mounted, so the split adds no second latch. */
const ChartsPage = lazy(() => import("./pages/ChartsPage"))

const THEME_KEY = "oa-theme"

/** Which surface a path selects.
 *
 *  Two surfaces, one shell, and no router: this app has never had one and a
 *  routing library would be its only dependency of that kind. pushState plus
 *  popstate covers both cases the app actually has.
 *
 *  One caveat worth knowing before deploying: Vite's dev server falls back to
 *  index.html for an unknown path, so a deep link to /charts works in dev. A
 *  plain static host will 404 on it and needs a rewrite rule. Navigating inside
 *  the app is unaffected either way. */
function routeOf(pathname: string): Route {
  return pathname.startsWith("/charts") ? "charts" : "chat"
}

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

/** localStorage key for the thinking preference of one specific model. */
function thinkingKey(model: string): string {
  return `oa-thinking:${model}`
}

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [mode, setMode] = useState<TradingMode>("unknown")
  const [reasoning, setReasoning] = useState<ReasoningInfo | null>(null)
  // The thinking preference is remembered PER MODEL. A choice made for one model says
  // nothing about another - "Off" is meaningful on Ollama and impossible on DeepSeek -
  // and a single shared key made one model's pick leak into every other model as a
  // sticky default that overrode the configured one.
  const [effort, setEffort] = useState<string>("")
  const [model, setModel] = useState<string | null>(null)
  const [health, setHealth] = useState<string | null>(null)
  const [theme, setTheme] = useState<Theme>(readTheme)
  const [route, setRoute] = useState<Route>(() => routeOf(window.location.pathname))

  const chartsSeen = useRef(false)

  // Back and forward have to work, so the browser owns the history and this
  // only mirrors it.
  useEffect(() => {
    const onPop = () => setRoute(routeOf(window.location.pathname))
    window.addEventListener("popstate", onPop)
    return () => window.removeEventListener("popstate", onPop)
  }, [])

  const navigate = useCallback((next: Route) => {
    window.history.pushState(null, "", next === "charts" ? "/charts" : "/")
    setRoute(next)
  }, [])

  // Latches on the first visit and never resets. See the render comment.
  const chartsMounted = route === "charts" || chartsSeen.current
  chartsSeen.current = chartsMounted

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
    if (info.model) setModel(info.model)

    const supported = info.reasoning?.supported ?? []
    if (supported.length === 0) return

    // Resolution order: this model's remembered choice, then the server's configured
    // default, then the lowest level offered. Anything not on offer is discarded, so
    // the control never shows nothing selected and never asks for the impossible.
    setEffort((current) => {
      if (current && supported.includes(current)) return current
      const remembered = info.model ? localStorage.getItem(thinkingKey(info.model)) : null
      if (remembered && supported.includes(remembered)) return remembered
      const serverDefault = info.reasoning?.current ?? ""
      return supported.includes(serverDefault) ? serverDefault : supported[0]
    })
  }, [])

  // Persist against the model it was chosen for, not globally.
  useEffect(() => {
    if (effort && model) localStorage.setItem(thinkingKey(model), effort)
  }, [effort, model])

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
        case "notice":
          // A server-side correction about this turn, for instance that the reply
          // described an order which was never actually placed.
          patchLast((message) => ({
            ...message,
            notices: [...(message.notices ?? []),
                      { level: event.level, message: event.message }],
          }))
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
        route={route}
        onNavigate={navigate}
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

      {/* The chart is mounted on first visit and never unmounted after that.
          Three things had to be true at once. Unmounting it would drop its
          websocket and every drawing on it, so it cannot come and go with the
          route. Mounting it at startup would build a chart inside a hidden
          element, where the container measures 0x0 and the engine has nothing to
          size itself against. And a user who never opens Charts should not pay
          for a websocket they are not watching. Latching on first visit satisfies
          all three: it is built once, while visible, and then only hidden. The
          page is told when it is hidden, because a window listener cannot tell
          display:none from visible and its shortcuts were firing from chat. */}
      {chartsMounted ? (
        <div className={cn("min-w-0 flex-1", route === "charts" ? "flex" : "hidden")}>
          <Suspense
            fallback={
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                <span className="shimmer">Loading the chart</span>
              </div>
            }
          >
            <ChartsPage onNavigate={navigate} active={route === "charts"} />
          </Suspense>
        </div>
      ) : null}

      <div className={cn("min-w-0 flex-1 flex-col", route === "chat" ? "flex" : "hidden")}>
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
