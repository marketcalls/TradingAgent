/** The one dense row above a full-bleed chart.
 *
 * Everything here is a header for the canvas below it, so height is the scarce
 * resource: compact paddings, text-xs labels, icon-only buttons wherever the
 * icon is unambiguous. A roomy toolbar steals bars from the chart, which is the
 * thing the trader actually came to look at.
 *
 * Two rules shape the rest of it:
 *
 *   - A control with no data behind it is disabled with its state still
 *     readable, never hidden. Before the engine reports back there are no
 *     intervals and no chart types, and the buttons still say which interval
 *     and which type are current. A control that vanishes and reappears is
 *     worse than one that is visibly not ready yet.
 *   - The websocket pill is the only place the feed's health is visible, so it
 *     is never silent. "live" is success, the two in-flight states are warn and
 *     shimmer rather than a spinner, "error" and "auth failed" are danger, and
 *     the two states the brief did not name get the reading they deserve:
 *     "open" is warn because a socket that carries no ticks is not live, and
 *     "closed" is muted because a deliberately closed feed is not a fault.
 *
 * The menus are built here rather than pulled from a kit because this app has
 * no dialog or popover primitive. Each one closes on Escape and on a pointer
 * outside it, moves focus into the list on open and back to its trigger on
 * close, and walks with the arrow keys.
 */

import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react"
import {
  Activity,
  ChartColumn,
  ChevronDown,
  RotateCcw,
  Scaling,
  Search,
  TriangleAlert,
  Wifi,
  WifiOff
} from "lucide-react"
import type { ChartTypeOption, IntervalGroups, WsState } from "../../lib/charts/terminal-api"
import { cn, EMPTY, formatPrice } from "../../lib/format"
import { chartTypeIcon, type ChartIcon } from "./chartIcons"

/** Mirrors the argument of `TerminalApi.setPriceScaleMode`. */
export type PriceScaleMode = "linear" | "logarithmic" | "percentage" | "indexed-to-100"

const TOOLBAR_BUTTON =
  "flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50"

const ICON_BUTTON =
  "flex h-7 w-7 items-center justify-center rounded-lg border border-border hover:bg-muted disabled:opacity-50"

const SCALE_MODES: { value: PriceScaleMode; label: string; short: string }[] = [
  { value: "linear", label: "Linear", short: "Linear" },
  { value: "logarithmic", label: "Logarithmic", short: "Log" },
  { value: "percentage", label: "Percent", short: "Percent" },
  { value: "indexed-to-100", label: "Indexed to 100", short: "Indexed" }
]

const INTERVAL_GROUP_LABELS: { key: keyof IntervalGroups; label: string }[] = [
  { key: "seconds", label: "Seconds" },
  { key: "minutes", label: "Minutes" },
  { key: "hours", label: "Hours" },
  { key: "days", label: "Days" },
  { key: "weeks", label: "Weeks" },
  { key: "months", label: "Months" }
]

interface MenuSection {
  label: string
  items: { value: string; label: string }[]
}

interface MenuProps {
  title: string
  buttonLabel: string
  icon: ChartIcon
  sections: MenuSection[]
  value: string
  disabled: boolean
  onSelect: (value: string) => void
}

function Menu({ title, buttonLabel, icon: Icon, sections, value, disabled, onSelect }: MenuProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener("pointerdown", onPointerDown)
    return () => document.removeEventListener("pointerdown", onPointerDown)
  }, [open])

  useEffect(() => {
    if (!open) return
    const list = listRef.current
    if (!list) return
    const current = list.querySelector<HTMLButtonElement>("[data-current='true']")
    const first = list.querySelector<HTMLButtonElement>("[data-menu-item='true']")
    ;(current ?? first)?.focus()
  }, [open])

  const close = (returnFocus: boolean) => {
    setOpen(false)
    if (returnFocus) buttonRef.current?.focus()
  }

  const step = (delta: number) => {
    const list = listRef.current
    if (!list) return
    const items = Array.from(list.querySelectorAll<HTMLButtonElement>("[data-menu-item='true']"))
    if (items.length === 0) return
    const index = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = index === -1 ? 0 : (index + delta + items.length) % items.length
    items[next].focus()
  }

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation()
      close(true)
      return
    }
    if (event.key === "ArrowDown") {
      event.preventDefault()
      step(1)
      return
    }
    if (event.key === "ArrowUp") {
      event.preventDefault()
      step(-1)
      return
    }
    if (event.key === "Home") {
      event.preventDefault()
      step(0)
    }
  }

  const empty = sections.every((section) => section.items.length === 0)

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled || empty}
        aria-haspopup="menu"
        aria-expanded={open}
        title={title}
        onClick={() => setOpen((current) => !current)}
        className={cn(TOOLBAR_BUTTON, open && "bg-muted")}
      >
        <Icon className="h-3.5 w-3.5 shrink-0" />
        <span className="max-w-[9rem] truncate">{buttonLabel}</span>
        <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
      </button>

      {open ? (
        <div
          ref={listRef}
          role="menu"
          aria-label={title}
          onKeyDown={onKeyDown}
          className="scroll-thin absolute left-0 top-full z-40 mt-1 max-h-[60vh] w-52 overflow-y-auto rounded-xl border border-border bg-background p-1 shadow-l"
        >
          {sections.map((section) => (
            <div key={section.label} className="mb-1 last:mb-0">
              <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                {section.label}
              </div>
              {section.items.map((item) => {
                const active = item.value === value
                return (
                  <button
                    key={item.value}
                    type="button"
                    role="menuitemradio"
                    aria-checked={active}
                    data-menu-item="true"
                    data-current={active ? "true" : "false"}
                    onClick={() => {
                      onSelect(item.value)
                      close(true)
                    }}
                    className={cn(
                      "flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1 text-left text-xs",
                      active
                        ? "bg-primary font-medium text-primary-foreground"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground"
                    )}
                  >
                    <span className="truncate">{item.label}</span>
                  </button>
                )
              })}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function feedTone(state: WsState): { tone: string; icon: ChartIcon; busy: boolean } {
  switch (state) {
    case "live":
      return { tone: "text-success", icon: Wifi, busy: false }
    case "connecting":
    case "reconnecting":
      return { tone: "text-warn", icon: Wifi, busy: true }
    case "open":
      return { tone: "text-warn", icon: Wifi, busy: false }
    case "error":
    case "auth failed":
      return { tone: "text-danger", icon: TriangleAlert, busy: false }
    default:
      return { tone: "text-muted-foreground", icon: WifiOff, busy: false }
  }
}

interface ChartToolbarProps {
  symbol: string
  exchange: string
  /** The instrument's long name. Absent for plenty of symbols, so never assumed. */
  name?: string | null
  interval: string
  /** Null until the engine reports what the broker actually offers. */
  intervals?: IntervalGroups | null
  chartType: string
  chartTypes?: ChartTypeOption[]
  wsState: WsState
  lastPrice?: number | null
  volumeVisible: boolean
  priceScaleMode: PriceScaleMode
  onOpenSymbolSearch: () => void
  onInterval: (interval: string) => void
  onChartType: (chartType: string) => void
  onOpenIndicators: () => void
  onToggleVolume: (visible: boolean) => void
  onResetScale: () => void
  onPriceScaleMode: (mode: PriceScaleMode) => void
}

export default function ChartToolbar({
  symbol,
  exchange,
  name,
  interval,
  intervals,
  chartType,
  chartTypes,
  wsState,
  lastPrice,
  volumeVisible,
  priceScaleMode,
  onOpenSymbolSearch,
  onInterval,
  onChartType,
  onOpenIndicators,
  onToggleVolume,
  onResetScale,
  onPriceScaleMode
}: ChartToolbarProps) {
  const types = chartTypes ?? []

  const intervalSections: MenuSection[] = INTERVAL_GROUP_LABELS.map(({ key, label }) => ({
    label,
    items: (intervals?.[key] ?? []).map((entry) => ({ value: entry, label: entry }))
  })).filter((section) => section.items.length > 0)

  const typeSections: MenuSection[] = [
    {
      label: "Standard",
      items: types.filter((entry) => !entry.derived).map((entry) => ({ value: entry.value, label: entry.label }))
    },
    {
      label: "Derived",
      items: types.filter((entry) => entry.derived).map((entry) => ({ value: entry.value, label: entry.label }))
    }
  ].filter((section) => section.items.length > 0)

  const activeType = types.find((entry) => entry.value === chartType)
  const feed = feedTone(wsState)
  const FeedIcon = feed.icon
  const TypeIcon = chartTypeIcon(chartType)
  const scale = SCALE_MODES.find((entry) => entry.value === priceScaleMode) ?? SCALE_MODES[0]

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border bg-background px-2.5 py-1.5">
      <button
        type="button"
        onClick={onOpenSymbolSearch}
        title="Change the symbol"
        className="flex min-w-0 items-center gap-2 rounded-lg border border-border px-2.5 py-1 hover:bg-muted"
      >
        <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="text-sm font-semibold">{symbol || EMPTY}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{exchange || EMPTY}</span>
        {name ? (
          <span className="hidden max-w-[14rem] truncate text-xs text-muted-foreground lg:inline">
            {name}
          </span>
        ) : null}
      </button>

      <Menu
        title="Interval"
        buttonLabel={interval || EMPTY}
        icon={Activity}
        sections={intervalSections}
        value={interval}
        disabled={intervalSections.length === 0}
        onSelect={onInterval}
      />

      <Menu
        title="Chart type"
        buttonLabel={activeType?.label ?? chartType ?? EMPTY}
        icon={TypeIcon}
        sections={typeSections}
        value={chartType}
        disabled={typeSections.length === 0}
        onSelect={onChartType}
      />

      <button type="button" onClick={onOpenIndicators} title="Indicators" className={TOOLBAR_BUTTON}>
        <Activity className="h-3.5 w-3.5 shrink-0" />
        <span>Indicators</span>
      </button>

      <div className="ml-auto flex items-center gap-2">
        <span
          className="text-sm font-medium tabular-nums"
          title={lastPrice === null || lastPrice === undefined ? "No trade yet" : "Last traded price"}
        >
          {lastPrice === null || lastPrice === undefined ? EMPTY : formatPrice(lastPrice)}
        </span>

        <span
          role="status"
          title={`Market feed: ${wsState}`}
          className={cn(
            "flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-[11px]",
            feed.tone
          )}
        >
          <FeedIcon className="h-3 w-3 shrink-0" />
          <span className={cn(feed.busy && "shimmer")}>{wsState}</span>
        </span>

        <button
          type="button"
          aria-pressed={volumeVisible}
          aria-label="Show volume"
          title={volumeVisible ? "Hide volume" : "Show volume"}
          onClick={() => onToggleVolume(!volumeVisible)}
          className={cn(ICON_BUTTON, volumeVisible ? "bg-muted" : "text-muted-foreground")}
        >
          <ChartColumn className="h-3.5 w-3.5 shrink-0" />
        </button>

        <Menu
          title="Price scale"
          buttonLabel={scale.short}
          icon={Scaling}
          sections={[{ label: "Price scale", items: SCALE_MODES.map((entry) => ({ value: entry.value, label: entry.label })) }]}
          value={priceScaleMode}
          disabled={false}
          onSelect={(value) => onPriceScaleMode(value as PriceScaleMode)}
        />

        <button
          type="button"
          onClick={onResetScale}
          aria-label="Reset the scales"
          title="Reset the scales"
          className={cn(ICON_BUTTON, "text-muted-foreground hover:text-foreground")}
        >
          <RotateCcw className="h-3.5 w-3.5 shrink-0" />
        </button>
      </div>
    </div>
  )
}
