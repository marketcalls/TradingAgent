/** The app's CSS custom properties, rendered into a canvas palette.
 *
 * Two properties of the app's palette make this more than a lookup table, and
 * both are measured off src/index.css rather than assumed:
 *
 *   - Almost every non-text colour is an ALPHA OVERLAY on one base colour:
 *     --border is oklch(0.097 0 0 / 0.06) in light and oklch(0.994 0 89.876 /
 *     0.06) in dark. Painting that straight onto a canvas gives a nearly
 *     transparent pixel, and reading its RGB back gives near-black in both
 *     themes. Every token is therefore composited over the resolved --background
 *     first, so what the canvas receives is the colour the eye actually sees.
 *   - The values are oklch(). getComputedStyle hands oklch back as oklch, and a
 *     canvas 2D context that cannot parse it silently keeps the previous
 *     fillStyle rather than throwing. Every assignment is therefore sandwiched
 *     between a sentinel and a read-back, so an unparseable token falls to a
 *     known-good literal instead of painting the sentinel.
 *
 * A DrawingStyle stores a literal colour string, so nothing here re-tints an
 * existing drawing: the terminal has to re-issue its AI drawings on a theme
 * swap. tonePalette is the single place that decides what "bullish" looks like,
 * so that re-issue is a re-read of this file and not a second palette.
 *
 * --chart-up, --chart-down and --chart-grid are opaque tokens added to
 * index.css for the canvas specifically. They are read when present and fall
 * back to the library's own dark/light values when they are not, so this file
 * is correct against index.css with or without them.
 */

import { darkTheme, lightTheme, type ChartTheme } from "openalgo-charts"
import type { Tone } from "./types"

export type ThemeMode = "dark" | "light"

/** What a semantic tone looks like in the active theme. */
export interface TonePalette {
  bullish: string
  bearish: string
  neutral: string
  /** Label text that reads on the chart background. */
  text: string
}

/** A colour the canvas is certain to reject, so an invalid assignment is visible. */
const SENTINEL = "#010203"

let probe: HTMLSpanElement | null = null
let pixel: CanvasRenderingContext2D | null = null

function ensureProbe(): boolean {
  if (probe !== null && pixel !== null) return true
  if (typeof document === "undefined") return false
  const span = document.createElement("span")
  span.style.display = "none"
  document.body.appendChild(span)
  const canvas = document.createElement("canvas")
  canvas.width = 1
  canvas.height = 1
  const context = canvas.getContext("2d", { willReadFrequently: true })
  if (context === null) return false
  probe = span
  pixel = context
  return true
}

/** The declared value of a custom property on the root, or null when unset.
 *
 *  Read off documentElement because that is the element .dark and .light are
 *  toggled on, so the winning declaration is the one this returns. An unset
 *  property returns "", which is the only reliable absence check: assigning an
 *  undefined var() to a colour property computes to the inherited colour rather
 *  than to nothing.
 */
function token(name: string): string | null {
  if (typeof document === "undefined") return null
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return raw === "" ? null : raw
}

/** Assign a colour and report whether the canvas accepted it. */
function accepts(value: string): boolean {
  if (pixel === null) return false
  pixel.fillStyle = SENTINEL
  pixel.fillStyle = value
  return pixel.fillStyle !== SENTINEL
}

/** Paint `value` over `base` on a 1x1 canvas and read the result back as rgb().
 *
 *  `base` must already be opaque, or an alpha token composited onto it stays
 *  translucent and the read-back is wrong in the same way it was before.
 */
function composite(value: string, base: string, fallback: string): string {
  if (!ensureProbe() || pixel === null || probe === null) return fallback
  probe.style.color = ""
  probe.style.color = value
  const resolved = getComputedStyle(probe).color
  if (resolved === "") return fallback
  pixel.clearRect(0, 0, 1, 1)
  if (!accepts(base)) return fallback
  pixel.fillRect(0, 0, 1, 1)
  if (!accepts(resolved)) return fallback
  pixel.fillRect(0, 0, 1, 1)
  const data = pixel.getImageData(0, 0, 1, 1).data
  return `rgb(${data[0]}, ${data[1]}, ${data[2]})`
}

/** Any CSS colour as an opaque rgb() string the canvas is certain to parse.
 *
 *  Exported because the OHLC readout is written to a DOM node the terminal owns
 *  and has to tint itself: it cannot reach for a Tailwind class, and reading the
 *  token through here keeps it on the same palette as the canvas.
 */
export function resolveCssColor(value: string, fallback: string, over = "#000000"): string {
  return composite(value, over, fallback)
}

interface Resolved {
  background: string
  foreground: string
  muted: string
  border: string
  input: string
  up: string
  down: string
  neutral: string
  grid: string
}

/** Resolved palettes, by mode.
 *
 *  Rasterizing is nine getComputedStyle calls and nine canvas round-trips, and
 *  the crosshair readout asks for colours at pointer rate. The tokens can only
 *  change when the theme class on the root flips, which changes the mode, so a
 *  memo keyed on the mode is exact rather than merely cheap.
 */
const cache = new Map<ThemeMode, Resolved>()

/** Drop the memo, for a host that swaps the stylesheet without changing mode. */
export function refreshThemeCache(): void {
  cache.clear()
}

function resolve(mode: ThemeMode): Resolved {
  const hit = cache.get(mode)
  if (hit !== undefined) return hit
  const value = compute(mode)
  // Never cache the answer from a run that had no canvas: it is all fallbacks,
  // and a later call in a real document would keep getting them.
  if (probe !== null && pixel !== null) cache.set(mode, value)
  return value
}

function compute(mode: ThemeMode): Resolved {
  const base = mode === "dark" ? darkTheme : lightTheme
  // The ground everything else is composited over. It is opaque in both themes,
  // so which literal it is composited over itself does not matter.
  const ground = mode === "dark" ? "#000000" : "#ffffff"
  const background = composite(token("--background") ?? base.background, ground, base.background)
  const over = (name: string, fallback: string): string =>
    composite(token(name) ?? fallback, background, fallback)
  const upToken = token("--chart-up") ?? token("--success") ?? base.upColor
  const up = composite(upToken, background, base.upColor)
  const down = composite(
    token("--chart-down") ?? token("--danger") ?? base.downColor,
    background,
    base.downColor
  )
  return {
    background,
    foreground: over("--foreground", base.axisText),
    muted: over("--muted-foreground", base.axisText),
    border: over("--border", base.axisLine),
    input: over("--input", base.grid),
    up,
    down,
    neutral: over("--primary", base.lineColor),
    grid: composite(token("--chart-grid") ?? token("--border") ?? base.grid, background, base.grid)
  }
}

/** The canvas palette for the app's active theme.
 *
 *  DEFAULT_THEME is lightTheme despite the ChartOptions JSDoc claiming dark, so
 *  the caller must pass this explicitly on createChart and again on setTheme.
 *  Everything not overridden here (the area gradients, the last-price tag text)
 *  is left on the library's own dark or light value, which is already tuned to
 *  the background it is being drawn on.
 */
export function readChartTheme(mode: ThemeMode): ChartTheme {
  const base = mode === "dark" ? darkTheme : lightTheme
  const c = resolve(mode)
  return {
    ...base,
    background: c.background,
    grid: c.grid,
    axisText: c.muted,
    axisLine: c.border,
    paneSeparator: c.border,
    crosshair: c.muted,
    upColor: c.up,
    downColor: c.down,
    wickUpColor: c.up,
    wickDownColor: c.down,
    lineColor: c.neutral,
    baselineTopLine: c.up,
    baselineBottomLine: c.down,
    lastPriceUp: c.up,
    lastPriceDown: c.down,
    buy: c.up,
    sell: c.down,
    profit: c.up,
    loss: c.down
  }
}

/** Colours for the semantic markup vocabulary. */
export function tonePalette(mode: ThemeMode): TonePalette {
  const c = resolve(mode)
  return { bullish: c.up, bearish: c.down, neutral: c.neutral, text: c.foreground }
}

/** One tone resolved. Kept beside the palette so nothing else maps the union. */
export function toneColor(palette: TonePalette, tone: Tone | undefined): string {
  if (tone === "bullish") return palette.bullish
  if (tone === "bearish") return palette.bearish
  return palette.neutral
}

/** The volume histogram's colour.
 *
 *  Its own token rather than a theme key: the histogram renderer is the one
 *  series colour ChartTheme cannot reach, falling back to a hardcoded #3a4666
 *  whatever the palette. --input is the app's quietest visible overlay, which is
 *  what a volume pane wants: present, and never competing with price.
 */
export function volumeColor(mode: ThemeMode): string {
  return resolve(mode).input
}

/** Text colours for the OHLC readout the terminal writes into its own DOM node. */
export interface LegendColors {
  up: string
  down: string
  muted: string
  text: string
}

export function legendColors(mode: ThemeMode): LegendColors {
  const c = resolve(mode)
  return { up: c.up, down: c.down, muted: c.muted, text: c.foreground }
}
