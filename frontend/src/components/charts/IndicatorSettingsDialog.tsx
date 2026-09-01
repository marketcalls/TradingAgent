/** One settings form for all hundred-odd indicators, and no code for any of them.
 *
 * The engine hands over two flat lists of `SettingsField`, the declared inputs
 * and the generated per-plot style keys, and this file renders whatever is in
 * them. There is deliberately not a single indicator id anywhere below: the day
 * a new study is registered, its dialog already exists. Anything that looked
 * like special-casing would be a bug waiting for the 103rd indicator.
 *
 * What it does know is the shape of a key. Generated style keys are
 * "<plot>:color", "<plot>:width" and so on, so the Style tab groups on the part
 * before the colon. That is structural, not per-indicator, and it is what turns
 * a flat run of thirty-five style fields into six short blocks that fit without
 * scrolling.
 *
 * Chrome decisions, each one taken once:
 *
 *   - A colour is a 28px rounded square, never a wide bar. When a value is not
 *     a plain hex the field degrades to a text input rather than a swatch that
 *     shows the wrong colour.
 *   - Booleans are a switch button, not a native checkbox, because a native
 *     checkbox paints its own accent colour straight through the theme.
 *   - Selects are native, because the option list has to be reachable by
 *     keyboard and by a screen reader, but with appearance-none and the app's
 *     own chevron so the closed control belongs to this app.
 *   - The dialog does not apply as you type. Edits collect in a draft and go
 *     over in one patch, so a half-typed period on a length field never reaches
 *     the chart. Apply is the last action bottom right; restore defaults sits
 *     bottom left, away from it.
 */

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent
} from "react"
import { createPortal } from "react-dom"
import { ChevronDown, Palette, SlidersHorizontal, X } from "lucide-react"
import type { IndicatorSettingsRequest, SettingsField } from "../../lib/charts/terminal-api"
import { cn, labelize } from "../../lib/format"

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

/** Used only when a `source` field arrives with no options of its own. The
 *  engine's own list, minus volume, which it deliberately omits from a UI. */
const SOURCE_FALLBACK: { label: string; value: unknown }[] = [
  { label: "open", value: "open" },
  { label: "high", value: "high" },
  { label: "low", value: "low" },
  { label: "close", value: "close" },
  { label: "hl2", value: "hl2" },
  { label: "hlc3", value: "hlc3" },
  { label: "ohlc4", value: "ohlc4" }
]

const TEXT_INPUT =
  "rounded-lg border border-input bg-background px-2.5 py-1 text-sm outline-none placeholder:text-muted-foreground focus:border-primary disabled:opacity-60"

/** The opaque hex an <input type="color"> can show, or null when it cannot. */
function hexOf(value: unknown): string | null {
  if (typeof value !== "string") return null
  const text = value.trim()
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(text)
  if (short) return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`.toLowerCase()
  const long = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/i.exec(text)
  if (long) return `#${long[1]}`.toLowerCase()
  const rgb = /^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})/i.exec(text)
  if (rgb) {
    const channel = (raw: string) => Math.min(255, Number(raw)).toString(16).padStart(2, "0")
    return `#${channel(rgb[1])}${channel(rgb[2])}${channel(rgb[3])}`
  }
  return null
}

function sameValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true
  if (left === null || left === undefined || right === null || right === undefined) return false
  return String(left) === String(right)
}

/** Ids have to survive being written into htmlFor, and generated style keys
 *  carry a colon. */
function safeKey(key: string): string {
  return key.replace(/[^a-zA-Z0-9_-]/g, "-")
}

/** Groups generated style keys by the plot they belong to. Purely structural:
 *  it reads the part before the colon and never the indicator. */
function groupFields(fields: SettingsField[]): { label: string; fields: SettingsField[] }[] {
  const groups: { label: string; fields: SettingsField[] }[] = []
  for (const field of fields) {
    const colon = field.key.indexOf(":")
    const label = colon > 0 ? labelize(field.key.slice(0, colon)) : "General"
    let group = groups.find((entry) => entry.label === label)
    if (!group) {
      group = { label, fields: [] }
      groups.push(group)
    }
    group.fields.push(field)
  }
  return groups
}

interface FieldRowProps {
  field: SettingsField
  id: string
  value: unknown
  /** The in-progress text for number and text fields, so a half-typed value is
   *  not thrown away by a re-render. */
  raw: string | undefined
  onValue: (value: unknown) => void
  onRaw: (text: string) => void
}

function FieldRow({ field, id, value, raw, onValue, onRaw }: FieldRowProps) {
  const labelId = `${id}-label`

  let control = null

  if (field.type === "boolean") {
    const on = value === true
    control = (
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={on}
        aria-labelledby={labelId}
        onClick={() => onValue(!on)}
        className={cn(
          "relative h-5 w-9 shrink-0 rounded-full border border-input transition-colors",
          on ? "bg-primary" : "bg-input"
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 h-3.5 w-3.5 rounded-full transition-all",
            on ? "left-[1.125rem] bg-primary-foreground" : "left-0.5 bg-background"
          )}
        />
      </button>
    )
  } else if (field.type === "number") {
    control = (
      <input
        id={id}
        type="number"
        inputMode="decimal"
        min={field.min}
        max={field.max}
        step={field.step}
        value={raw ?? String(value ?? "")}
        onChange={(event) => {
          onRaw(event.target.value)
          const parsed = Number(event.target.value)
          if (event.target.value.trim() !== "" && Number.isFinite(parsed)) onValue(parsed)
        }}
        className={cn(TEXT_INPUT, "w-28 tabular-nums")}
      />
    )
  } else if (field.type === "color" && hexOf(value) !== null) {
    control = (
      <input
        id={id}
        type="color"
        value={hexOf(value) as string}
        onChange={(event) => onValue(event.target.value)}
        title={typeof value === "string" ? value : undefined}
        className="h-7 w-7 shrink-0 cursor-pointer rounded-md border border-input bg-background p-0 [&::-moz-color-swatch]:rounded [&::-moz-color-swatch]:border-0 [&::-webkit-color-swatch-wrapper]:p-0.5 [&::-webkit-color-swatch]:rounded [&::-webkit-color-swatch]:border-0"
      />
    )
  } else if (field.type === "select" || field.type === "source") {
    const options = field.options ?? (field.type === "source" ? SOURCE_FALLBACK : [])
    const index = options.findIndex((option) => sameValue(option.value, value))
    control = (
      <div className="relative">
        <select
          id={id}
          value={index === -1 ? "current" : String(index)}
          disabled={options.length === 0}
          onChange={(event) => {
            const picked = options[Number(event.target.value)]
            if (picked) onValue(picked.value)
          }}
          className="w-40 appearance-none rounded-lg border border-input bg-background py-1 pl-2.5 pr-7 text-sm outline-none focus:border-primary disabled:opacity-60"
        >
          {index === -1 ? (
            <option value="current">{value === undefined ? "not set" : String(value)}</option>
          ) : null}
          {options.map((option, position) => (
            <option key={`${option.label}-${position}`} value={String(position)}>
              {option.label}
            </option>
          ))}
        </select>
        <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3.5 w-3.5 shrink-0 -translate-y-1/2 text-muted-foreground" />
      </div>
    )
  } else {
    control = (
      <input
        id={id}
        type="text"
        value={raw ?? String(value ?? "")}
        onChange={(event) => {
          onRaw(event.target.value)
          onValue(event.target.value)
        }}
        className={cn(TEXT_INPUT, "w-40")}
      />
    )
  }

  return (
    <div className="flex items-center justify-between gap-3 border-t border-border py-1.5 first:border-t-0">
      <label id={labelId} htmlFor={id} className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
        {field.label}
      </label>
      <div className="shrink-0">{control}</div>
    </div>
  )
}

interface IndicatorSettingsDialogProps {
  /** Null when nothing is being edited. The dialog owns nothing else. */
  request: IndicatorSettingsRequest | null
  onApply: (patch: Record<string, unknown>) => void
  onReset: () => void
  onClose: () => void
}

export default function IndicatorSettingsDialog({
  request,
  onApply,
  onReset,
  onClose
}: IndicatorSettingsDialogProps) {
  const [tab, setTab] = useState<"inputs" | "style">("inputs")
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [raw, setRaw] = useState<Record<string, string>>({})

  const panelRef = useRef<HTMLDivElement>(null)
  const baseId = useId()
  const instanceId = request?.instanceId ?? null

  // A different instance is a different form. Anything half-typed belongs to the
  // instance it was typed for, so it goes with it.
  useEffect(() => {
    setDraft({})
    setRaw({})
    setTab("inputs")
  }, [instanceId])

  useEffect(() => {
    if (!request) return
    const previous = document.activeElement as HTMLElement | null
    const focus = window.setTimeout(
      () => panelRef.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus(),
      0
    )
    return () => {
      window.clearTimeout(focus)
      previous?.focus?.()
    }
  }, [request])

  const inputs = useMemo(() => request?.inputs ?? [], [request])
  const styleInputs = useMemo(() => request?.styleInputs ?? [], [request])
  const groups = useMemo(
    () => groupFields(tab === "inputs" ? inputs : styleInputs),
    [tab, inputs, styleInputs]
  )

  if (!request) return null

  const dirty = Object.keys(draft).length > 0

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation()
      onClose()
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

  const apply = () => {
    if (!dirty) return
    onApply({ ...draft })
    onClose()
  }

  const restore = () => {
    setDraft({})
    setRaw({})
    onReset()
  }

  const tabs: { key: "inputs" | "style"; label: string; count: number; icon: typeof Palette }[] = [
    { key: "inputs", label: "Inputs", count: inputs.length, icon: SlidersHorizontal },
    { key: "style", label: "Style", count: styleInputs.length, icon: Palette }
  ]

  return createPortal(
    <div
      onMouseDown={onBackdrop}
      className="fixed inset-0 z-50 flex items-start justify-center bg-background/80 p-4 pt-[8vh] backdrop-blur-sm"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${baseId}-title`}
        onKeyDown={onKeyDown}
        className="flex max-h-[76vh] w-full max-w-[560px] flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-l"
      >
        <div className="flex items-center justify-between gap-2 px-4 pt-3">
          <h2 id={`${baseId}-title`} className="min-w-0 truncate text-sm font-semibold">
            {request.name}
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
          <div className="flex overflow-hidden rounded-lg border border-border">
            {tabs.map((entry) => {
              const TabIcon = entry.icon
              const active = tab === entry.key
              return (
                <button
                  key={entry.key}
                  type="button"
                  disabled={entry.count === 0}
                  aria-pressed={active}
                  title={entry.count === 0 ? `${entry.label}: nothing to set` : entry.label}
                  onClick={() => setTab(entry.key)}
                  className={cn(
                    "flex items-center gap-1.5 px-2.5 py-1 text-xs disabled:opacity-40",
                    active ? "bg-primary font-medium text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"
                  )}
                >
                  <TabIcon className="h-3.5 w-3.5 shrink-0" />
                  <span>{entry.label}</span>
                  <span className="tabular-nums opacity-70">{entry.count}</span>
                </button>
              )
            })}
          </div>
        </div>

        <div className="scroll-thin mt-3 min-h-0 flex-1 overflow-y-auto px-4">
          {groups.length === 0 ? (
            <div className="py-3 text-sm text-muted-foreground">
              {inputs.length === 0 && styleInputs.length === 0
                ? "This indicator has nothing to configure."
                : "Nothing to set on this tab."}
            </div>
          ) : (
            groups.map((group) => (
              <div key={group.label} className="mb-3 last:mb-0">
                {groups.length > 1 ? (
                  <div className="pb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                    {group.label}
                  </div>
                ) : null}
                <div className="rounded-xl border border-border px-3 py-1">
                  {group.fields.map((field) => (
                    <FieldRow
                      key={field.key}
                      field={field}
                      id={`${baseId}-${safeKey(field.key)}`}
                      value={field.key in draft ? draft[field.key] : field.value}
                      raw={raw[field.key]}
                      onValue={(next) => setDraft((current) => ({ ...current, [field.key]: next }))}
                      onRaw={(text) => setRaw((current) => ({ ...current, [field.key]: text }))}
                    />
                  ))}
                </div>
              </div>
            ))
          )}
        </div>

        <div className="mt-3 flex items-center justify-between gap-2 border-t border-border px-4 py-2">
          <button
            type="button"
            onClick={restore}
            className="rounded-lg border border-input px-4 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            Restore defaults
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-input px-4 py-1.5 text-sm font-medium disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!dirty}
              onClick={apply}
              title={dirty ? "Apply the changes" : "Nothing changed yet"}
              className="rounded-lg bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body
  )
}
