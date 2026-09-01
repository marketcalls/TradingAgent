/** The floating bar that appears when a drawing is selected.
 *
 * Deliberately position-neutral: it is an inline-flex bar and the page decides
 * where it sits, because only the page knows where the chart's own chrome is.
 *
 * With nothing selected there is no subject at all, so it renders nothing. That
 * is not the same as a control with no data, which is the case this bar is full
 * of and which it handles the other way round: a tool with no colour, no width
 * or no line style still shows those controls, disabled, with the reason in the
 * title. A control that disappears leaves the user guessing whether the tool
 * has the property or the app has a bug.
 *
 * The colour control is a 28px rounded square, never a wide bar, and it is a
 * restyled native colour input rather than a hand-rolled popover: the OS picker
 * is the only colour surface that is keyboard reachable and screen-reader
 * labelled without several hundred lines behind it. When the engine reports a
 * colour that is not a plain hex (an rgba string, an eight-digit hex) the
 * swatch shows the opaque part and the title carries the real value, and when
 * it cannot be read at all the swatch disables rather than lying.
 *
 * A locked drawing disables every style control but keeps the lock and the
 * delete live, which is the only way back out of the lock.
 */

import { Lock, Trash2, Type, Unlock } from "lucide-react"
import type { DrawSelection } from "../../lib/charts/terminal-api"
import { cn, labelize } from "../../lib/format"
import { drawToolIcon, lineStyleIcon } from "./chartIcons"

const WIDTHS = [1, 2, 3, 4]
const LINE_STYLES = ["solid", "dashed", "dotted"]

const SEGMENT_WRAPPER = "flex overflow-hidden rounded-lg border border-border"
const SEGMENT_ITEM = "px-2.5 py-1 text-[11px] tabular-nums disabled:opacity-40"
const SEGMENT_ON = "bg-primary font-medium text-primary-foreground"
const SEGMENT_OFF = "text-muted-foreground hover:bg-muted hover:text-foreground"
const BAR_BUTTON =
  "flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-border text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"

/** The opaque hex an <input type="color"> can actually show, or null. */
function hexOf(value: string | undefined): string | null {
  if (!value) return null
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

interface DrawingStyleBarProps {
  selection: DrawSelection | null
  onUpdate: (patch: Record<string, unknown>) => void
  onRemove: (id: string) => void
  onEditText: (id: string) => void
}

export default function DrawingStyleBar({
  selection,
  onUpdate,
  onRemove,
  onEditText
}: DrawingStyleBarProps) {
  if (!selection) return null

  const locked = selection.locked === true
  const hex = hexOf(selection.color)
  const ToolIcon = drawToolIcon(selection.tool)

  return (
    <div className="inline-flex items-center gap-2 rounded-xl border border-border bg-background p-1.5 shadow-l">
      <span className="flex items-center gap-1.5 pl-1 pr-1 text-xs">
        <ToolIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="max-w-[10rem] truncate font-medium">{labelize(selection.tool)}</span>
      </span>

      <input
        type="color"
        value={hex ?? "#000000"}
        disabled={locked || hex === null}
        onChange={(event) => onUpdate({ color: event.target.value })}
        aria-label="Line colour"
        title={
          hex === null
            ? selection.color
              ? `Colour ${selection.color} cannot be edited here`
              : "This tool has no colour"
            : `Line colour ${selection.color ?? hex}`
        }
        className="h-7 w-7 shrink-0 cursor-pointer rounded-md border border-input bg-background p-0 disabled:cursor-not-allowed disabled:opacity-40 [&::-moz-color-swatch]:rounded [&::-moz-color-swatch]:border-0 [&::-webkit-color-swatch-wrapper]:p-0.5 [&::-webkit-color-swatch]:rounded [&::-webkit-color-swatch]:border-0"
      />

      <div className={SEGMENT_WRAPPER} role="group" aria-label="Line width">
        {WIDTHS.map((width) => {
          const active = selection.lineWidth === width
          return (
            <button
              key={width}
              type="button"
              disabled={locked || selection.lineWidth === undefined}
              aria-pressed={active}
              title={`Line width ${width}`}
              onClick={() => onUpdate({ lineWidth: width })}
              className={cn(SEGMENT_ITEM, active ? SEGMENT_ON : SEGMENT_OFF)}
            >
              {width}
            </button>
          )
        })}
      </div>

      <div className={SEGMENT_WRAPPER} role="group" aria-label="Line style">
        {LINE_STYLES.map((style) => {
          const active = selection.lineStyle === style
          const StyleIcon = lineStyleIcon(style)
          return (
            <button
              key={style}
              type="button"
              disabled={locked || selection.lineStyle === undefined}
              aria-pressed={active}
              aria-label={style}
              title={`${style.charAt(0).toUpperCase()}${style.slice(1)} line`}
              onClick={() => onUpdate({ lineStyle: style })}
              className={cn("px-2.5 py-1 disabled:opacity-40", active ? SEGMENT_ON : SEGMENT_OFF)}
            >
              <StyleIcon className="h-3.5 w-3.5 shrink-0" />
            </button>
          )
        })}
      </div>

      {selection.hasText ? (
        <button
          type="button"
          disabled={locked}
          onClick={() => onEditText(selection.id)}
          aria-label="Edit the text"
          title="Edit the text"
          className={BAR_BUTTON}
        >
          <Type className="h-3.5 w-3.5 shrink-0" />
        </button>
      ) : null}

      <button
        type="button"
        aria-pressed={locked}
        onClick={() => onUpdate({ locked: !locked })}
        aria-label="Lock this drawing"
        title={locked ? "Unlock this drawing" : "Lock this drawing"}
        className={cn(BAR_BUTTON, locked && "bg-muted text-foreground")}
      >
        {locked ? <Lock className="h-3.5 w-3.5 shrink-0" /> : <Unlock className="h-3.5 w-3.5 shrink-0" />}
      </button>

      <button
        type="button"
        onClick={() => onRemove(selection.id)}
        aria-label="Delete this drawing"
        title="Delete this drawing"
        className={cn(BAR_BUTTON, "hover:text-danger")}
      >
        <Trash2 className="h-3.5 w-3.5 shrink-0" />
      </button>
    </div>
  )
}
