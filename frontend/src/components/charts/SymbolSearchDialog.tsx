/** Change the instrument without leaving the chart.
 *
 * There is no dialog primitive in this app, so the overlay is built by hand: a
 * portal to the body so a transformed ancestor cannot trap it, a scrim, a
 * centred panel, Escape and backdrop to close, focus moved into the field on
 * open and returned to whatever had it on close, and Tab cycling inside the
 * panel so a keyboard user cannot fall out into the chart behind.
 *
 * Three things it is careful about:
 *
 *   - The query is debounced by 200ms and every request carries a sequence
 *     number. A search that resolves after a newer one is dropped rather than
 *     painted, which is the bug that makes a fast typist see results for a
 *     prefix they already deleted.
 *   - Results are grouped by exchange in first-seen order, because the same
 *     ticker exists on NSE and BSE and picking the wrong one is a real order on
 *     the wrong book.
 *   - The list is a combobox, not a set of buttons: focus stays in the input,
 *     the arrow keys move aria-activedescendant, and Enter takes the highlighted
 *     row. Typing and choosing never fight over the caret.
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
import { Search, X } from "lucide-react"
import type { SymbolRow } from "../../lib/charts/terminal-api"
import { cn } from "../../lib/format"

const DEBOUNCE_MS = 200

/** A short, honest filter: these are the books OpenAlgo routes to. "Any" is the
 *  default so a wrong guess here never hides a symbol. */
const EXCHANGES = ["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"]

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

interface Placed {
  row: SymbolRow
  /** Position in the flattened list, which is what the arrow keys walk. */
  index: number
}

/** First-seen exchange order, and every row carries the index it will hold in
 *  the flattened list so the renderer never has to count as it goes. */
function groupByExchange(rows: SymbolRow[]): { exchange: string; rows: Placed[] }[] {
  const groups: { exchange: string; rows: Placed[] }[] = []
  for (const row of rows) {
    const key = row.exchange || "Other"
    let group = groups.find((entry) => entry.exchange === key)
    if (!group) {
      group = { exchange: key, rows: [] }
      groups.push(group)
    }
    group.rows.push({ row, index: 0 })
  }
  let index = 0
  for (const group of groups) {
    for (const placed of group.rows) placed.index = index++
  }
  return groups
}

interface SymbolSearchDialogProps {
  open: boolean
  onSearch: (query: string, exchange?: string) => Promise<SymbolRow[]>
  onPick: (row: SymbolRow) => void
  onClose: () => void
}

export default function SymbolSearchDialog({ open, onSearch, onPick, onClose }: SymbolSearchDialogProps) {
  const [query, setQuery] = useState("")
  const [exchange, setExchange] = useState<string | null>(null)
  const [rows, setRows] = useState<SymbolRow[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [active, setActive] = useState(0)

  const panelRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const sequence = useRef(0)

  // A fresh dialog every time, so yesterday's query is never the starting point.
  useEffect(() => {
    if (!open) return
    setQuery("")
    setExchange(null)
    setRows([])
    setError(null)
    setActive(0)
    const previous = document.activeElement as HTMLElement | null
    const focus = window.setTimeout(() => inputRef.current?.focus(), 0)
    return () => {
      window.clearTimeout(focus)
      previous?.focus?.()
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const text = query.trim()
    if (text.length === 0) {
      sequence.current += 1
      setRows([])
      setSearching(false)
      setError(null)
      return
    }
    setSearching(true)
    // The ticket is taken at the keystroke, not when the timer fires. Taken
    // later, a request already in flight for the previous prefix still held the
    // newest ticket for the whole debounce window and painted its stale rows.
    const ticket = (sequence.current += 1)
    const timer = window.setTimeout(() => {
      onSearch(text, exchange ?? undefined)
        .then((result) => {
          if (ticket !== sequence.current) return
          setRows(Array.isArray(result) ? result : [])
          setError(null)
          setActive(0)
          setSearching(false)
        })
        .catch((reason: unknown) => {
          if (ticket !== sequence.current) return
          setRows([])
          setError(reason instanceof Error ? reason.message : "The search failed")
          setSearching(false)
        })
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [open, query, exchange, onSearch])

  const groups = useMemo(() => groupByExchange(rows), [rows])
  const flat = useMemo(() => groups.flatMap((group) => group.rows.map((placed) => placed.row)), [groups])

  useEffect(() => {
    if (flat.length === 0) return
    listRef.current
      ?.querySelector(`[data-index="${Math.min(active, flat.length - 1)}"]`)
      ?.scrollIntoView({ block: "nearest" })
  }, [active, flat.length])

  if (!open) return null

  const move = (delta: number) => {
    if (flat.length === 0) return
    setActive((current) => (current + delta + flat.length) % flat.length)
  }

  const pick = (row: SymbolRow) => {
    onPick(row)
    onClose()
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
    if (event.key === "Enter") {
      // Only from the field. On Close or an exchange chip, Enter is that
      // button's own click and must not load the highlighted row instead.
      if (document.activeElement !== inputRef.current) return
      const row = flat[active]
      if (row) {
        event.preventDefault()
        pick(row)
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
      className="fixed inset-0 z-50 flex items-start justify-center bg-background/80 p-4 pt-[12vh] backdrop-blur-sm"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="symbol-search-title"
        onKeyDown={onKeyDown}
        className="flex max-h-[70vh] w-full max-w-[560px] flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-l"
      >
        <div className="flex items-center justify-between gap-2 px-4 pt-3">
          <h2 id="symbol-search-title" className="text-sm font-semibold">
            Change symbol
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
              aria-controls="symbol-search-list"
              aria-autocomplete="list"
              aria-activedescendant={flat.length > 0 ? `symbol-option-${active}` : undefined}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search a symbol, for example RELIANCE"
              className="w-full rounded-lg border border-input bg-background py-1.5 pl-8 pr-2.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary"
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-1 px-4 pt-2">
          <span className="mr-1 text-[11px] uppercase tracking-wide text-muted-foreground">Exchange</span>
          <div className="flex overflow-hidden rounded-lg border border-border">
            {[null, ...EXCHANGES].map((entry) => {
              const selected = exchange === entry
              return (
                <button
                  key={entry ?? "any"}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => setExchange(entry)}
                  className={cn(
                    "px-2.5 py-1 text-[11px]",
                    selected
                      ? "bg-primary font-medium text-primary-foreground"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground"
                  )}
                >
                  {entry ?? "Any"}
                </button>
              )
            })}
          </div>
        </div>

        <div className="scroll-thin mt-2 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {error ? (
            <div className="px-2 py-3 text-sm text-danger">{error}</div>
          ) : query.trim().length === 0 ? (
            <div className="px-2 py-3 text-sm text-muted-foreground">
              Type at least one character to search.
            </div>
          ) : searching && flat.length === 0 ? (
            <div className="px-2 py-3 text-sm">
              <span className="shimmer">Searching</span>
            </div>
          ) : flat.length === 0 ? (
            <div className="px-2 py-3 text-sm text-muted-foreground">
              Nothing matched {query.trim()}
              {exchange ? ` on ${exchange}` : ""}.
            </div>
          ) : (
            <ul ref={listRef} id="symbol-search-list" role="listbox" aria-label="Search results">
              {groups.map((group) => (
                // A listbox's children must be options or groups; the visible
                // header is decoration and the group label carries the text.
                <li key={group.exchange} role="group" aria-label={group.exchange}>
                  <div
                    aria-hidden="true"
                    className="px-2 pb-1 pt-2 text-[11px] uppercase tracking-wide text-muted-foreground"
                  >
                    {group.exchange}
                  </div>
                  <ul role="presentation">
                    {group.rows.map(({ row, index }) => {
                      const selected = index === active
                      return (
                        <li
                          key={`${row.exchange}:${row.symbol}`}
                          id={`symbol-option-${index}`}
                          data-index={index}
                          role="option"
                          aria-selected={selected}
                          onMouseDown={(event) => {
                            event.preventDefault()
                            pick(row)
                          }}
                          onMouseEnter={() => setActive(index)}
                          className={cn(
                            "flex cursor-pointer items-baseline gap-2 rounded-lg px-2.5 py-1.5",
                            selected ? "bg-muted" : ""
                          )}
                        >
                          <span className="text-sm font-medium">{row.symbol}</span>
                          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                            {row.name ?? ""}
                          </span>
                          {row.lotsize !== undefined && row.lotsize !== null ? (
                            <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                              lot {row.lotsize}
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

        <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-2">
          <span className="text-[11px] text-muted-foreground">
            Arrow keys to move, Enter to load, Escape to close
          </span>
          {searching && flat.length > 0 ? (
            <span className="shimmer text-[11px]">Searching</span>
          ) : null}
        </div>
      </div>
    </div>,
    document.body
  )
}
