/** Add and remove studies, with what is already on the chart in plain sight.
 *
 * A hundred-odd indicators is a search problem, not a browse problem, so the
 * field takes focus on open and filters on name, id and category at once. The
 * results stay grouped by category underneath, because a trader who does not
 * know the exact name still knows they want something from Volatility.
 *
 * The right-hand column is the reason this is one dialog rather than two. The
 * same indicator can be added several times (three EMAs is the normal case), so
 * "is it already on?" has no yes or no answer, only a count. Showing the live
 * instances beside the catalogue turns removing a duplicate into one click
 * instead of a hunt through the chart legend, and the count badge on a
 * catalogue row is what stops a fourth EMA being added by accident.
 *
 * Adding does not close the dialog: adding two studies in a row is far more
 * common than adding exactly one. Escape, the backdrop and Done all close it.
 *
 * The overlay is hand-built, like every other dialog here, since the app has no
 * dialog primitive: portal, scrim, Escape, backdrop click, focus in on open and
 * restored on close, and Tab cycling kept inside the panel.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent
} from "react"
import { createPortal } from "react-dom"
import { Plus, Search, Trash2, X } from "lucide-react"
import type { IndicatorOption } from "../../lib/charts/terminal-api"
import type { ChartIndicator } from "../../lib/charts/types"
import { cn } from "../../lib/format"

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

interface Placed {
  option: IndicatorOption
  /** Position in the flattened list, which is what the arrow keys walk. */
  index: number
}

function matches(option: IndicatorOption, query: string): boolean {
  if (!query) return true
  const needle = query.toLowerCase()
  return (
    option.name.toLowerCase().includes(needle) ||
    option.id.toLowerCase().includes(needle) ||
    option.category.toLowerCase().includes(needle)
  )
}

/** First-seen category order, with the flattened index stamped onto each row. */
function groupByCategory(options: IndicatorOption[]): { category: string; rows: Placed[] }[] {
  const groups: { category: string; rows: Placed[] }[] = []
  for (const option of options) {
    const key = option.category || "Other"
    let group = groups.find((entry) => entry.category === key)
    if (!group) {
      group = { category: key, rows: [] }
      groups.push(group)
    }
    group.rows.push({ option, index: 0 })
  }
  let index = 0
  for (const group of groups) {
    for (const placed of group.rows) placed.index = index++
  }
  return groups
}

interface IndicatorPickerDialogProps {
  open: boolean
  /** Empty until the catalogue has loaded. The search field says so rather than
   *  pretending nothing matched. */
  catalogue?: IndicatorOption[]
  active?: ChartIndicator[]
  onPick: (indicatorId: string) => void
  onRemove: (instanceId: string) => void
  onClose: () => void
}

export default function IndicatorPickerDialog({
  open,
  catalogue,
  active,
  onPick,
  onRemove,
  onClose
}: IndicatorPickerDialogProps) {
  const [query, setQuery] = useState("")
  const [cursor, setCursor] = useState(0)

  const panelRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  const all = useMemo(() => catalogue ?? [], [catalogue])
  const live = useMemo(() => active ?? [], [active])

  useEffect(() => {
    if (!open) return
    setQuery("")
    setCursor(0)
    const previous = document.activeElement as HTMLElement | null
    const focus = window.setTimeout(() => inputRef.current?.focus(), 0)
    return () => {
      window.clearTimeout(focus)
      previous?.focus?.()
    }
  }, [open])

  const groups = useMemo(
    () => groupByCategory(all.filter((option) => matches(option, query.trim()))),
    [all, query]
  )
  const flat = useMemo(() => groups.flatMap((group) => group.rows.map((row) => row.option)), [groups])

  const counts = useMemo(() => {
    const map = new Map<string, number>()
    for (const instance of live) {
      map.set(instance.indicatorId, (map.get(instance.indicatorId) ?? 0) + 1)
    }
    return map
  }, [live])

  useEffect(() => {
    setCursor(0)
  }, [query])

  useEffect(() => {
    if (flat.length === 0) return
    listRef.current
      ?.querySelector(`[data-index="${Math.min(cursor, flat.length - 1)}"]`)
      ?.scrollIntoView({ block: "nearest" })
  }, [cursor, flat.length])

  if (!open) return null

  const move = (delta: number) => {
    if (flat.length === 0) return
    setCursor((current) => (current + delta + flat.length) % flat.length)
  }

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation()
      onClose()
      return
    }
    if (event.key === "ArrowDown") {
      event.preventDefault()
      move(1)
      return
    }
    if (event.key === "ArrowUp") {
      event.preventDefault()
      move(-1)
      return
    }
    if (event.key === "Enter" && document.activeElement === inputRef.current) {
      const option = flat[cursor]
      if (option) {
        event.preventDefault()
        onPick(option.id)
      }
      return
    }
    if (event.key !== "Tab") return
    const panel = panelRef.current
    if (!panel) return
    const targets = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
    if (targets.length === 0) return
    const first = targets[0]
    const last = targets[targets.length - 1]
    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    }
  }

  const onBackdrop = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) onClose()
  }

  return createPortal(
    <div
      onMouseDown={onBackdrop}
      className="fixed inset-0 z-50 flex items-start justify-center bg-background/80 p-4 pt-[8vh] backdrop-blur-sm"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="indicator-picker-title"
        onKeyDown={onKeyDown}
        className="flex max-h-[76vh] w-full max-w-[720px] flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-l"
      >
        <div className="flex items-center justify-between gap-2 px-4 pt-3">
          <h2 id="indicator-picker-title" className="text-sm font-semibold">
            Indicators
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex h-7 w-7 items-center justify-center rounded-lg border border-border hover:bg-muted"
          >
            <X className="h-3.5 w-3.5 shrink-0" />
          </button>
        </div>

        <div className="px-4 pt-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 shrink-0 -translate-y-1/2 text-muted-foreground" />
            <input
              ref={inputRef}
              type="text"
              role="combobox"
              aria-expanded={flat.length > 0}
              aria-controls="indicator-catalogue"
              aria-autocomplete="list"
              aria-activedescendant={flat.length > 0 ? `indicator-option-${cursor}` : undefined}
              disabled={all.length === 0}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={
                all.length === 0 ? "The catalogue has not loaded" : "Search by name, id or category"
              }
              className="w-full rounded-lg border border-input bg-background py-1.5 pl-8 pr-2.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary disabled:opacity-60"
            />
          </div>
        </div>

        <div className="grid min-h-0 flex-1 gap-3 px-4 pt-3 md:grid-cols-[1fr_15rem]">
          <div className="scroll-thin min-h-0 overflow-y-auto rounded-xl border border-border p-1">
            {all.length === 0 ? (
              <div className="px-2 py-3 text-sm text-muted-foreground">
                The indicator catalogue has not loaded yet.
              </div>
            ) : flat.length === 0 ? (
              <div className="px-2 py-3 text-sm text-muted-foreground">
                Nothing matched {query.trim()}.
              </div>
            ) : (
              <ul ref={listRef} id="indicator-catalogue" role="listbox" aria-label="Indicator catalogue">
                {groups.map((group) => (
                  <li key={group.category} role="presentation">
                    <div className="px-2 pb-1 pt-2 text-[11px] uppercase tracking-wide text-muted-foreground">
                      {group.category}
                    </div>
                    <ul role="presentation">
                      {group.rows.map(({ option, index }) => {
                        const selected = index === cursor
                        const count = counts.get(option.id) ?? 0
                        return (
                          <li
                            key={option.id}
                            id={`indicator-option-${index}`}
                            data-index={index}
                            role="option"
                            aria-selected={selected}
                            onMouseDown={(event) => {
                              event.preventDefault()
                              onPick(option.id)
                            }}
                            onMouseEnter={() => setCursor(index)}
                            className={cn(
                              "flex cursor-pointer items-baseline gap-2 rounded-lg px-2.5 py-1.5",
                              selected ? "bg-muted" : ""
                            )}
                          >
                            <Plus className="h-3 w-3 shrink-0 self-center text-muted-foreground" />
                            <span className="text-sm">{option.name}</span>
                            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">
                              {option.id}
                            </span>
                            {count > 0 ? (
                              <span className="shrink-0 rounded-lg bg-muted px-2.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
                                {count} on chart
                              </span>
                            ) : null}
                          </li>
                        )
                      })}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="scroll-thin min-h-0 overflow-y-auto rounded-xl border border-border p-1">
            <div className="px-2 pb-1 pt-2 text-[11px] uppercase tracking-wide text-muted-foreground">
              On this chart
            </div>
            {live.length === 0 ? (
              <div className="px-2 py-2 text-xs text-muted-foreground">Nothing added yet.</div>
            ) : (
              live.map((instance) => (
                <div
                  key={instance.instanceId}
                  className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 hover:bg-muted"
                >
                  <span className="min-w-0 flex-1 truncate text-xs">{instance.name}</span>
                  <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                    pane {instance.paneIndex}
                  </span>
                  <button
                    type="button"
                    onClick={() => onRemove(instance.instanceId)}
                    aria-label={`Remove ${instance.name}`}
                    title={`Remove ${instance.name}`}
                    className="shrink-0 text-muted-foreground hover:text-danger"
                  >
                    <Trash2 className="h-3.5 w-3.5 shrink-0" />
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="mt-3 flex items-center justify-between gap-2 border-t border-border px-4 py-2">
          <span className="text-[11px] text-muted-foreground">
            {all.length > 0
              ? `${flat.length} of ${all.length} shown, arrow keys to move, Enter to add`
              : "Waiting for the catalogue"}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body
  )
}
