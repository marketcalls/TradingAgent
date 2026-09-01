/** The vertical tool rail down the left edge of the chart.
 *
 * Forty-odd tools do not fit in a column, so the rail shows one button per
 * group and the group opens a flyout. When the armed tool belongs to a group,
 * that group's button wears the tool's own glyph, so the rail always says what
 * is armed without the flyout being open.
 *
 * Keyboard rules, in the order they matter:
 *
 *   - Escape closes an open flyout; with no flyout open it disarms the tool and
 *     hands the pointer back to the chart. That is the escape hatch a user
 *     reaches for when a half-drawn trendline is following the cursor.
 *   - The engine ships a shortcut with some tools (Alt+H and friends). Those are
 *     honoured, matched on event.code as well as event.key, because Alt on a
 *     non-US layout turns the key into a symbol.
 *   - None of it fires while focus sits in an input, a textarea, a select or a
 *     contenteditable. The composer is one Tab away and stealing H from someone
 *     typing "short" is unforgivable.
 *
 * The undo, redo and clear buttons are disabled from the engine's own counters
 * rather than hidden, so an empty chart still shows the whole rail: a control
 * with nothing to act on reads as "nothing to undo", a missing one reads as a
 * broken build.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent
} from "react"
import { createPortal } from "react-dom"
import { Magnet, MousePointer2, PencilLine, Redo2, Trash2, Undo2 } from "lucide-react"
import type { DrawState } from "../../lib/charts/terminal-api"
import { cn } from "../../lib/format"
import { drawGroupIcon, drawToolIcon } from "./chartIcons"

interface DrawTool {
  id: string
  name: string
  shortcut?: string
}

interface DrawGroup {
  group: string
  tools: DrawTool[]
}

const IDLE: DrawState = {
  activeTool: null,
  canUndo: false,
  canRedo: false,
  magnet: false,
  count: 0
}

const RAIL_BUTTON =
  "flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-transparent text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"

const RAIL_ACTIVE = "border-border bg-primary text-primary-foreground hover:bg-primary hover:text-primary-foreground"

/** True while the caret is somewhere a keystroke means a character. */
function isEditing(): boolean {
  const element = document.activeElement as HTMLElement | null
  if (!element) return false
  const tag = element.tagName
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true
  return element.isContentEditable
}

/** Matches "Alt+H", "Ctrl+Shift+M" and the like against a real key event. */
function shortcutMatches(shortcut: string, event: KeyboardEvent): boolean {
  const parts = shortcut
    .split("+")
    .map((part) => part.trim().toLowerCase())
    .filter(Boolean)
  const key = parts[parts.length - 1]
  if (!key || key.length === 0) return false
  const wantsAlt = parts.includes("alt") || parts.includes("option")
  const wantsCtrl = parts.includes("ctrl") || parts.includes("control")
  const wantsShift = parts.includes("shift")
  const wantsMeta = parts.includes("meta") || parts.includes("cmd") || parts.includes("command")
  if (event.altKey !== wantsAlt) return false
  if (event.ctrlKey !== wantsCtrl) return false
  if (event.shiftKey !== wantsShift) return false
  if (event.metaKey !== wantsMeta) return false
  if (event.key.toLowerCase() === key) return true
  // Alt rewrites event.key on most non-US layouts, so fall back to the physical key.
  return event.code.toLowerCase() === `key${key}` || event.code.toLowerCase() === `digit${key}`
}

interface DrawingRailProps {
  groups?: DrawGroup[]
  /** Null until the engine has reported once. The rail still renders, disabled. */
  state?: DrawState | null
  onPickTool: (toolId: string | null) => void
  onUndo: () => void
  onRedo: () => void
  onToggleMagnet: (on: boolean) => void
  onClearAll: () => void
}

export default function DrawingRail({
  groups,
  state,
  onPickTool,
  onUndo,
  onRedo,
  onToggleMagnet,
  onClearAll
}: DrawingRailProps) {
  const [openGroup, setOpenGroup] = useState<string | null>(null)
  // Viewport coordinates for the open flyout. The rail scrolls, and an absolutely
  // positioned child of a scrolling ancestor is clipped by it: the menu is 224px
  // wide and the rail is 28px, so it was being cropped to a blank sliver. It only
  // looked right on a fresh load, before the rail had enough tools to scroll.
  const [anchor, setAnchor] = useState<{ top: number; left: number } | null>(null)
  const railRef = useRef<HTMLDivElement>(null)
  const flyoutRef = useRef<HTMLDivElement>(null)

  const list = useMemo(() => groups ?? [], [groups])
  const draw = state ?? IDLE

  const shortcuts = useMemo(
    () =>
      list.flatMap((group) =>
        group.tools
          .filter((tool) => typeof tool.shortcut === "string" && tool.shortcut.trim().length > 0)
          .map((tool) => ({ id: tool.id, shortcut: tool.shortcut as string }))
      ),
    [list]
  )

  useEffect(() => {
    if (!openGroup) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      // The flyout is portalled out of the rail, so railRef no longer contains it.
      // Without this second test a pointerdown on a tool closed the menu before its
      // click landed, and picking a tool did nothing at all.
      if (railRef.current?.contains(target) || flyoutRef.current?.contains(target)) return
      setOpenGroup(null)
      setAnchor(null)
    }
    document.addEventListener("pointerdown", onPointerDown)
    return () => document.removeEventListener("pointerdown", onPointerDown)
  }, [openGroup])

  useEffect(() => {
    if (!openGroup) return
    flyoutRef.current?.querySelector<HTMLButtonElement>("[data-flyout-item='true']")?.focus()
  }, [openGroup])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isEditing()) return
      if (event.key === "Escape") {
        if (openGroup) {
          setAnchor(null)
          setOpenGroup(null)
          return
        }
        if (draw.activeTool) onPickTool(null)
        return
      }
      for (const entry of shortcuts) {
        if (shortcutMatches(entry.shortcut, event)) {
          event.preventDefault()
          setOpenGroup(null)
          onPickTool(entry.id)
          return
        }
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [openGroup, draw.activeTool, shortcuts, onPickTool])

  const stepFlyout = (delta: number) => {
    const flyout = flyoutRef.current
    if (!flyout) return
    const items = Array.from(flyout.querySelectorAll<HTMLButtonElement>("[data-flyout-item='true']"))
    if (items.length === 0) return
    const index = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = index === -1 ? 0 : (index + delta + items.length) % items.length
    items[next].focus()
  }

  const onFlyoutKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation()
      setOpenGroup(null)
      return
    }
    if (event.key === "ArrowDown") {
      event.preventDefault()
      stepFlyout(1)
      return
    }
    if (event.key === "ArrowUp") {
      event.preventDefault()
      stepFlyout(-1)
    }
  }

  return (
    <div
      ref={railRef}
      className="flex h-full w-10 shrink-0 flex-col items-center gap-1 border-r border-border bg-sidebar py-1.5"
    >
      <button
        type="button"
        aria-pressed={draw.activeTool === null}
        aria-label="Cursor, no drawing tool"
        title="Cursor (Escape)"
        onClick={() => {
          setOpenGroup(null)
          onPickTool(null)
        }}
        className={cn(RAIL_BUTTON, draw.activeTool === null && RAIL_ACTIVE)}
      >
        <MousePointer2 className="h-3.5 w-3.5 shrink-0" />
      </button>

      <div className="scroll-thin flex min-h-0 flex-1 flex-col items-center gap-1 overflow-y-auto py-1">
        {list.length === 0 ? (
          <button
            type="button"
            disabled
            title="The drawing tools have not loaded yet"
            aria-label="The drawing tools have not loaded yet"
            className={RAIL_BUTTON}
          >
            <PencilLine className="h-3.5 w-3.5 shrink-0" />
          </button>
        ) : null}

        {list.map((group) => {
          const armed = group.tools.find((tool) => tool.id === draw.activeTool)
          const GroupIcon = armed
            ? drawToolIcon(armed.id)
            : drawGroupIcon(group.group, group.tools[0]?.id)
          const open = openGroup === group.group
          return (
            <div key={group.group} className="relative">
              <button
                type="button"
                aria-haspopup="menu"
                aria-expanded={open}
                aria-label={group.group}
                title={armed ? `${group.group}: ${armed.name}` : group.group}
                disabled={group.tools.length === 0}
                onClick={(event) => {
                  if (open) {
                    setOpenGroup(null)
                    setAnchor(null)
                    return
                  }
                  const rect = event.currentTarget.getBoundingClientRect()
                  // Keep the menu on screen when a group near the bottom is opened.
                  const height = Math.min(window.innerHeight * 0.6, group.tools.length * 28 + 40)
                  setAnchor({
                    top: Math.max(8, Math.min(rect.top, window.innerHeight - height - 8)),
                    left: rect.right + 4
                  })
                  setOpenGroup(group.group)
                }}
                className={cn(RAIL_BUTTON, armed && RAIL_ACTIVE, open && !armed && "bg-muted text-foreground")}
              >
                <GroupIcon className="h-3.5 w-3.5 shrink-0" />
              </button>

              {open && anchor ? createPortal(
                <div
                  ref={flyoutRef}
                  role="menu"
                  aria-label={group.group}
                  onKeyDown={onFlyoutKeyDown}
                  style={{ position: "fixed", top: anchor.top, left: anchor.left }}
                  className="scroll-thin z-50 max-h-[60vh] w-56 overflow-y-auto rounded-xl border border-border bg-background p-1 shadow-l"
                >
                  <div className="px-2.5 py-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                    {group.group}
                  </div>
                  {group.tools.map((tool) => {
                    const ToolIcon = drawToolIcon(tool.id)
                    const active = tool.id === draw.activeTool
                    return (
                      <button
                        key={tool.id}
                        type="button"
                        role="menuitemradio"
                        aria-checked={active}
                        data-flyout-item="true"
                        onClick={() => {
                          setOpenGroup(null)
                          setAnchor(null)
                          onPickTool(tool.id)
                        }}
                        className={cn(
                          "flex w-full items-center gap-2 rounded-lg px-2.5 py-1 text-left text-xs",
                          active
                            ? "bg-primary font-medium text-primary-foreground"
                            : "text-muted-foreground hover:bg-muted hover:text-foreground"
                        )}
                      >
                        <ToolIcon className="h-3.5 w-3.5 shrink-0" />
                        <span className="min-w-0 flex-1 truncate">{tool.name}</span>
                        {tool.shortcut ? (
                          <span
                            className={cn(
                              "shrink-0 font-mono text-[11px]",
                              active ? "text-primary-foreground" : "text-muted-foreground"
                            )}
                          >
                            {tool.shortcut}
                          </span>
                        ) : null}
                      </button>
                    )
                  })}
                </div>,
                document.body
              ) : null}
            </div>
          )
        })}
      </div>

      <div className="flex shrink-0 flex-col items-center gap-1 border-t border-border pt-1.5">
        <button
          type="button"
          disabled={!draw.canUndo}
          onClick={onUndo}
          aria-label="Undo"
          title={draw.canUndo ? "Undo" : "Nothing to undo"}
          className={RAIL_BUTTON}
        >
          <Undo2 className="h-3.5 w-3.5 shrink-0" />
        </button>
        <button
          type="button"
          disabled={!draw.canRedo}
          onClick={onRedo}
          aria-label="Redo"
          title={draw.canRedo ? "Redo" : "Nothing to redo"}
          className={RAIL_BUTTON}
        >
          <Redo2 className="h-3.5 w-3.5 shrink-0" />
        </button>
        <button
          type="button"
          aria-pressed={draw.magnet}
          onClick={() => onToggleMagnet(!draw.magnet)}
          aria-label="Snap to price"
          title={draw.magnet ? "Snap to price is on" : "Snap to price is off"}
          className={cn(RAIL_BUTTON, draw.magnet && RAIL_ACTIVE)}
        >
          <Magnet className="h-3.5 w-3.5 shrink-0" />
        </button>
        <button
          type="button"
          disabled={draw.count === 0}
          onClick={onClearAll}
          aria-label="Clear every drawing"
          title={draw.count === 0 ? "No drawings to clear" : `Clear every drawing (${draw.count})`}
          className={cn(RAIL_BUTTON, "hover:text-danger")}
        >
          <Trash2 className="h-3.5 w-3.5 shrink-0" />
        </button>
      </div>
    </div>
  )
}
