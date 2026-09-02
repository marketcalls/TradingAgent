/** Surface nav, thread list, new chat, delete, theme toggle and the model line.
 *
 * The list is non-critical: a failed load leaves it empty rather than blocking the
 * chat, which is the only thing the user actually came for.
 *
 * The two surfaces sit above the threads because threads belong to the chat and
 * not to the charts. Both stay mounted when you switch, so a half-written thread
 * and a loaded chart both survive a look at the other one.
 */

import { LineChart, MessageSquare, Moon, Plus, Sun, Trash2 } from "lucide-react"
import type { SessionRow } from "../lib/api"
import { cn, formatStamp } from "../lib/format"

export type Route = "chat" | "charts"

interface SidebarProps {
  route: Route
  onNavigate: (route: Route) => void
  sessions: SessionRow[]
  activeId: string | null
  busy: boolean
  theme: "dark" | "light"
  model: string | null
  onNewChat: () => void
  onOpen: (sessionId: string) => void
  onDelete: (sessionId: string) => void
  onToggleTheme: () => void
}

const NAV = [
  { route: "chat" as const, label: "Chat", icon: MessageSquare },
  { route: "charts" as const, label: "Charts", icon: LineChart }
]

export default function Sidebar({
  route,
  onNavigate,
  sessions,
  activeId,
  busy,
  theme,
  model,
  onNewChat,
  onOpen,
  onDelete,
  onToggleTheme
}: SidebarProps) {
  return (
    <aside className="flex h-full w-[264px] shrink-0 flex-col border-r border-border bg-sidebar">
      <div className="flex items-center gap-2 px-3 py-3">
        <div className="h-6 w-6 rounded-md bg-primary" />
        <span className="text-sm font-semibold">Trading Agent</span>
      </div>

      <div className="flex flex-col gap-0.5 px-3 pt-1">
        {NAV.map((item) => (
          <button
            key={item.route}
            type="button"
            onClick={() => {
              // Already here: navigating again only pushes a duplicate history
              // entry, and Back then appears to do nothing.
              if (route !== item.route) onNavigate(item.route)
            }}
            className={cn(
              "flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm",
              route === item.route
                ? "bg-muted font-medium text-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            )}
          >
            <item.icon className="h-4 w-4 shrink-0" />
            {item.label}
          </button>
        ))}
      </div>

      <div className="px-3 pt-3">
        <button
          type="button"
          onClick={onNewChat}
          disabled={busy}
          className="flex w-full items-center gap-2 rounded-lg border border-border px-2.5 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
        >
          <Plus className="h-4 w-4" />
          New chat
        </button>
      </div>

      <div className="px-3 pb-1 pt-4 text-xs text-muted-foreground">Threads</div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {sessions.length === 0 ? (
          <div className="px-1 py-2 text-xs text-muted-foreground">No threads yet</div>
        ) : (
          sessions.map((session) => (
            <div
              key={session.session_id}
              className={cn(
                "group flex items-center gap-1 rounded-lg px-1",
                activeId === session.session_id && "bg-muted"
              )}
            >
              <button
                type="button"
                onClick={() => onOpen(session.session_id)}
                disabled={busy}
                className="min-w-0 flex-1 truncate py-1.5 text-left text-sm disabled:opacity-50"
                title={session.title}
              >
                {session.title}
              </button>
              <span className="shrink-0 text-[11px] text-muted-foreground group-hover:hidden">
                {formatStamp(session.updated_at)}
              </span>
              <button
                type="button"
                onClick={() => onDelete(session.session_id)}
                disabled={busy}
                className="opacity-0 transition-opacity hover:!opacity-100 group-hover:opacity-60 disabled:opacity-30"
                aria-label="Delete this thread"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))
        )}
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2">
        <span className="min-w-0 truncate text-[11px] text-muted-foreground" title={model ?? ""}>
          {model ?? "model unknown"}
        </span>
        <button
          type="button"
          onClick={onToggleTheme}
          className="flex h-7 w-7 items-center justify-center rounded-lg border border-border hover:bg-muted"
          aria-label="Toggle the colour theme"
        >
          {theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
        </button>
      </div>
    </aside>
  )
}
