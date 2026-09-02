/** The semantic markup vocabulary, translated into drawing payloads.
 *
 * Facts about the 1.9.2 drawing tier that decide the shape of every case below:
 *
 *   - "envelope" is the primary shape and is NOT a parallel channel. A channel
 *     has two straight rails; an envelope tracks real pivots, so its boundaries
 *     step and bend. It is emitted as ONE polyline whose points are the highs
 *     left to right followed by the lows reversed, with fill: true. The polyline
 *     tool closes the path and fills it when style.fill is true, and its own
 *     defaultStyle is { fill: false }, so the flag has to be passed explicitly.
 *   - A drawing renders only once it has max(1, tool.points) anchors, and short
 *     of that it is invisible AND un-hittable, with no error anywhere. That is
 *     why "channel" emits three anchors: parallel-channel declares points: 3 and
 *     reads the third as the rail offset, measured from the base line's midpoint.
 *   - The merge order in add() is controller.defaultStyle, then tool.defaultStyle,
 *     then the drawing's own style, BY SPREAD. Tool defaults beat the
 *     controller's, so every colour, fill and level list is set per drawing here
 *     rather than once on the controller. The fib tools ship fill: true,
 *     fillOpacity: 0.06, which is why "fib" passes fill: false for bare levels.
 *     And a key present as undefined replaces the tool's default with undefined
 *     (measured: { lineStyle: undefined } over a dashed default drew solid), so
 *     GroupBuilder.add strips undefined keys before the controller sees them.
 *   - Only some tools render style.text. rectangle and parallel-channel draw it
 *     through shapeLabel, callout and price-note through their own plates, and
 *     the line tools and the arrow marks draw none at all. A label on a line or
 *     an arrow therefore becomes a second drawing of the text tool rather than a
 *     style key that silently does nothing.
 *   - The text tool paints its glyphs with style.color (fontColor is read by the
 *     shape tools only) and anchors the plate's top-left corner on its single
 *     point. When backgroundColor is absent its plate is painted in the LIVE
 *     theme background, so a plated note follows a theme swap by itself; that
 *     key is left unset on purpose.
 *   - arrow-up and arrow-down take one anchor, put the tip on it and extend the
 *     body away from it (below for up, above for down), fill from fillColor and
 *     fall back to color, and stroke through the shared line helper, so lineWidth
 *     and lineStyle apply to them like any line.
 *   - The controller does NOT reject a non-finite anchor: add() stores whatever
 *     points it is given and the renderer maps NaN to NaN pixels, silently.
 *     The isFinitePoint filter below is the only guard, and it costs one bad
 *     anchor its drawing rather than putting an invisible, un-hittable shape on
 *     the chart that the group's clear then has to find.
 *   - add() does no duplicate-id check: a second drawing under an existing id is
 *     drawn beside the first and the pair is then removed together. Appending
 *     to a group therefore numbers its ids after the ones already on the chart
 *     (nextGroupIndex), never from zero.
 *   - No tool on the backend emits "envelope" today. The contract still carries
 *     the kind and the translation stays, so a backend that starts emitting it
 *     draws rather than hitting the unknown-kind branch.
 *
 * Colour comes from the theme unless the shape names one. DrawingStyle stores a
 * literal string, so a theme swap has to re-issue tone-coloured drawings, and it
 * can only do that if one function decides what a tone looks like. A literal
 * colour is written as given: colorOf returns it on either palette, so the
 * terminal's retint (which rebuilds every group and pushes only a changed
 * colour) leaves it alone by construction.
 *
 * The "notes" group is the one the backend appends to rather than replaces, so
 * several labels can coexist. It is an ordinary ai: group otherwise: the page's
 * clear-all removes it with the rest, and a clear naming it removes it alone.
 */

import type { Drawing, DrawingPoint, DrawingStyle } from "openalgo-charts/draw"
import { AI_PREFIX, aiDrawingId, type Anchor, type AnnotationShape, type Tone } from "./types"
import { toneColor, type TonePalette } from "./theme"

/** A drawing with the id already decided, ready for DrawingController.add. */
export type AiDrawing = Omit<Drawing, "id"> & { id: string }

/** Fib levels when the analyst names none. The tool's own default list. */
const DEFAULT_FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]

/** Fill strength for a band. Light enough that candles read through it. */
const BAND_OPACITY = 0.1

/** A note's plate. Nearly opaque, so the words read over candles; the tool's
 *  own default is 1, which hides the bar underneath entirely. */
const PLATE_OPACITY = 0.9

/** A group id safe to embed in a drawing id.
 *
 *  "#" cannot appear because the hit-test parser splits an external id on it,
 *  so a group carrying one would make every drawing in it un-selectable. ":"
 *  cannot appear because it is this scheme's own separator: without that,
 *  clearing group "a" would also clear group "a:b", whose ids share the prefix.
 *
 *  Both are ENCODED rather than collapsed. Mapping "#", ":" and "-" all onto "-"
 *  gave "a#b", "a:b" and "a-b" one prefix, so clearing any of them cleared the
 *  other two. "-" is the escape character: a literal hyphen doubles, "#" becomes
 *  "-hash-" and ":" becomes "-colon-", which reads left to right without
 *  ambiguity, so distinct groups stay distinct.
 */
export function sanitizeGroup(group: string): string {
  const cleaned = group
    .trim()
    .replace(/-/g, "--")
    .replace(/#/g, "-hash-")
    .replace(/:/g, "-colon-")
  return cleaned === "" ? "group" : cleaned
}

/** The id prefix every drawing in one group shares. */
export function groupPrefix(group: string): string {
  return `${AI_PREFIX}:${sanitizeGroup(group)}:`
}

/** True for a drawing belonging to this group specifically. */
export function inGroup(id: string, group: string): boolean {
  return id.startsWith(groupPrefix(group))
}

/** The index the next drawing appended to this group should take: one past the
 *  highest already in use among `ids`, or 0 when the group has none. The index
 *  is the last segment of the id, and ":" cannot occur inside a sanitised
 *  group, so the slice after the prefix is the whole number. */
export function nextGroupIndex(ids: Iterable<string>, group: string): number {
  const prefix = groupPrefix(group)
  let next = 0
  for (const id of ids) {
    if (!id.startsWith(prefix)) continue
    const index = Number(id.slice(prefix.length))
    if (Number.isInteger(index) && index >= next) next = index + 1
  }
  return next
}

function isFinitePoint(anchor: Anchor): boolean {
  return Number.isFinite(anchor.time) && Number.isFinite(anchor.price)
}

function point(anchor: Anchor): DrawingPoint {
  return { time: anchor.time, price: anchor.price }
}

/** The anchor a label should sit on: the highest of the ones given. */
function highest(anchors: Anchor[]): Anchor | null {
  let best: Anchor | null = null
  for (const anchor of anchors) {
    if (best === null || anchor.price > best.price) best = anchor
  }
  return best
}

/** The tone a shape carries when it names none. An arrow's direction is its
 *  meaning, so an unlabelled up arrow is bullish rather than neutral grey. */
function impliedTone(shape: AnnotationShape): Tone | undefined {
  if (shape.kind === "arrow") return shape.direction === "down" ? "bearish" : "bullish"
  return undefined
}

/** The colour a shape draws in: the analyst's literal when given, else the
 *  tone resolved against the palette. */
function colorOf(shape: AnnotationShape, palette: TonePalette): string {
  const literal = shape.color
  if (typeof literal === "string" && literal.trim() !== "") return literal.trim()
  return toneColor(palette, shape.tone ?? impliedTone(shape))
}

/** Collects drawings for one group, handing out stable sequential ids. */
class GroupBuilder {
  private readonly out: AiDrawing[] = []
  private next: number

  constructor(
    private readonly group: string,
    private readonly paneIndex: number,
    firstIndex: number
  ) {
    this.next = firstIndex
  }

  add(tool: string, points: DrawingPoint[], style: DrawingStyle): void {
    // Undefined keys are dropped here rather than at each call site: a key
    // present as undefined survives the controller's spread merge and replaces
    // the tool's own default with nothing.
    const clean: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(style)) {
      if (value !== undefined) clean[key] = value
    }
    this.out.push({
      id: aiDrawingId(sanitizeGroup(this.group), this.next++),
      tool,
      points,
      style: clean as DrawingStyle,
      paneIndex: this.paneIndex
    })
  }

  /** A free-standing caption, for the shapes whose tool draws no text. */
  label(at: Anchor | null, text: string | undefined, color: string): void {
    if (at === null || text === undefined || text === "") return
    this.add("text", [point(at)], {
      text,
      color,
      fontColor: color,
      fontSize: 12,
      background: false,
      border: false,
      wrap: false
    })
  }

  drawings(): AiDrawing[] {
    return this.out
  }
}

function addEnvelope(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "envelope" }>,
  color: string
): void {
  const highs = shape.highs.filter(isFinitePoint)
  const lows = shape.lows.filter(isFinitePoint)
  // The path runs along the highs and back along the lows, so the polyline
  // closes into the band the pivots describe rather than into a triangle.
  const points = [...highs.map(point), ...lows.slice().reverse().map(point)]
  if (points.length < 3) return
  builder.add("polyline", points, {
    color,
    lineWidth: shape.lineWidth ?? 1.5,
    lineStyle: shape.lineStyle,
    fill: true,
    fillColor: color,
    fillOpacity: shape.fillOpacity ?? BAND_OPACITY
  })
  builder.label(highest(highs.length > 0 ? highs : lows), shape.label, color)
}

function addTrendline(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "trendline" }>,
  color: string
): void {
  if (!isFinitePoint(shape.from) || !isFinitePoint(shape.to)) return
  builder.add("trend-line", [point(shape.from), point(shape.to)], {
    color,
    lineWidth: shape.lineWidth ?? 1.5,
    lineStyle: shape.lineStyle,
    extendLeft: false,
    extendRight: shape.extendRight === true
  })
  builder.label(shape.to, shape.label, color)
}

function addLevel(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "level" }>,
  color: string
): void {
  const anchor: Anchor = { time: shape.time, price: shape.price }
  if (!isFinitePoint(anchor)) return
  builder.add(shape.ray === true ? "horizontal-ray" : "horizontal-line", [point(anchor)], {
    color,
    lineWidth: shape.lineWidth ?? 1.5,
    // Dashed by default so a level reads as a reference rather than a trade.
    lineStyle: shape.lineStyle ?? "dashed",
    showLabels: true
  })
  builder.label(anchor, shape.label, color)
}

function addZone(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "zone" }>,
  color: string
): void {
  if (!isFinitePoint(shape.from) || !isFinitePoint(shape.to)) return
  // rectangle draws style.text itself, through shapeLabel, so the caption is a
  // style key here and not a second drawing.
  builder.add("rectangle", [point(shape.from), point(shape.to)], {
    color,
    lineWidth: shape.lineWidth ?? 1.5,
    lineStyle: shape.lineStyle,
    fill: true,
    fillColor: color,
    fillOpacity: shape.fillOpacity ?? BAND_OPACITY,
    text: shape.label,
    fontColor: color,
    fontSize: 12,
    textPosition: "inside"
  })
}

function addChannel(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "channel" }>,
  color: string
): void {
  if (!isFinitePoint(shape.from) || !isFinitePoint(shape.to)) return
  if (!Number.isFinite(shape.offset)) return
  // The third anchor is the rail: parallel-channel measures its y against the
  // midpoint of the base line, so the anchor sits at the mid time and the mid
  // price plus the offset. Two anchors would render nothing and hit-test to
  // nothing, silently.
  const rail: DrawingPoint = {
    time: (shape.from.time + shape.to.time) / 2,
    price: (shape.from.price + shape.to.price) / 2 + shape.offset
  }
  builder.add("parallel-channel", [point(shape.from), point(shape.to), rail], {
    color,
    lineWidth: shape.lineWidth ?? 1.5,
    lineStyle: shape.lineStyle,
    fill: true,
    fillColor: color,
    fillOpacity: shape.fillOpacity ?? 0.08,
    text: shape.label,
    fontColor: color,
    fontSize: 12
  })
}

function addFib(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "fib" }>,
  color: string
): void {
  if (!isFinitePoint(shape.from) || !isFinitePoint(shape.to)) return
  const levels = (shape.levels ?? DEFAULT_FIB_LEVELS).filter((level) => Number.isFinite(level))
  builder.add("fib-retracement", [point(shape.from), point(shape.to)], {
    color,
    lineWidth: shape.lineWidth ?? 1,
    lineStyle: shape.lineStyle,
    levels: levels.length > 0 ? levels : DEFAULT_FIB_LEVELS,
    showLabels: true,
    // The tool ships fill: true, fillOpacity: 0.06. Bare levels read better over
    // candles than seven tinted bands do, so the bands come back only when the
    // analyst asks for a shade by naming an opacity.
    fill: shape.fillOpacity !== undefined,
    fillColor: color,
    fillOpacity: shape.fillOpacity
  })
  builder.label(shape.to, shape.label, color)
}

function addCallout(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "callout" }>,
  color: string
): void {
  if (!isFinitePoint(shape.at) || !isFinitePoint(shape.seat)) return
  // callout reads its anchors as [target, seat]: the tail points at the first
  // and the bubble sits on the second. The bubble and tail are filled in
  // `color` at full opacity; lineWidth and lineStyle travel for the record
  // but the tool reads neither.
  builder.add("callout", [point(shape.at), point(shape.seat)], {
    color,
    fontColor: color,
    lineWidth: shape.lineWidth ?? 1.5,
    lineStyle: shape.lineStyle,
    fillOpacity: shape.fillOpacity,
    fontSize: 12,
    text: shape.text
  })
}

function addMarker(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "marker" }>,
  color: string
): void {
  if (!isFinitePoint(shape.at)) return
  // price-note is the dot plus a price plate: it reads the price off the anchor
  // rather than storing one, so dragging it re-reads instead of going stale.
  // lineWidth is the leader; fillOpacity is the plate (the tool's own 0.95).
  builder.add("price-note", [point(shape.at)], {
    color,
    fontColor: color,
    lineWidth: shape.lineWidth ?? 1.5,
    lineStyle: shape.lineStyle,
    fillOpacity: shape.fillOpacity,
    fontSize: 11,
    text: shape.text
  })
}

function addArrow(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "arrow" }>,
  color: string
): void {
  if (!isFinitePoint(shape.at)) return
  // The tip lands on the anchor and the body extends away from it, so an up
  // arrow on a swing low sits under the bar and points at it. The mark draws
  // no text, so the caption is a second drawing.
  builder.add(shape.direction === "down" ? "arrow-down" : "arrow-up", [point(shape.at)], {
    color,
    fillColor: color,
    lineWidth: shape.lineWidth ?? 1.5,
    lineStyle: shape.lineStyle,
    fillOpacity: shape.fillOpacity
  })
  builder.label(shape.at, shape.text, color)
}

function addText(
  builder: GroupBuilder,
  shape: Extract<AnnotationShape, { kind: "text" }>,
  color: string
): void {
  if (!isFinitePoint(shape.at) || shape.text === "") return
  // A plated note: the plate is what lets it read over candles, and it is left
  // in the theme's own background (no backgroundColor) so a theme swap repaints
  // it without a retint. The border is the tone, one lineWidth wide. The
  // shape's fillOpacity is the plate's, since the plate is the only area a
  // note has.
  builder.add("text", [point(shape.at)], {
    text: shape.text,
    color,
    fontColor: color,
    fontSize: 12,
    lineWidth: shape.lineWidth ?? 1,
    lineStyle: shape.lineStyle,
    background: true,
    backgroundOpacity: shape.fillOpacity ?? PLATE_OPACITY,
    border: true,
    wrap: true
  })
}

/** Translate one analyst group into the drawings that render it.
 *
 *  Returns them in emission order, ids already assigned from `firstIndex`, so
 *  the caller can add them one at a time. Per-drawing add() rather than
 *  fromJSON is deliberate: fromJSON replaces the whole model and wipes undo,
 *  redo and selection, which would throw away the user's own history to place
 *  the analyst's markup. A caller appending to a group that is already on the
 *  chart passes nextGroupIndex() as `firstIndex`, so the new ids continue the
 *  numbering instead of landing on drawings that are still there.
 */
export function buildAnnotations(
  group: string,
  shapes: readonly AnnotationShape[],
  palette: TonePalette,
  paneIndex = 0,
  firstIndex = 0
): AiDrawing[] {
  const builder = new GroupBuilder(group, paneIndex, firstIndex)
  for (const shape of shapes) {
    const color = colorOf(shape, palette)
    switch (shape.kind) {
      case "envelope":
        addEnvelope(builder, shape, color)
        break
      case "trendline":
        addTrendline(builder, shape, color)
        break
      case "level":
        addLevel(builder, shape, color)
        break
      case "zone":
        addZone(builder, shape, color)
        break
      case "channel":
        addChannel(builder, shape, color)
        break
      case "fib":
        addFib(builder, shape, color)
        break
      case "callout":
        addCallout(builder, shape, color)
        break
      case "marker":
        addMarker(builder, shape, color)
        break
      case "arrow":
        addArrow(builder, shape, color)
        break
      case "text":
        addText(builder, shape, color)
        break
      default:
        // An unknown kind is a newer backend talking to an older chart. Dropping
        // one shape is recoverable; throwing loses the whole analysis.
        break
    }
  }
  return builder.drawings()
}

/** The time span a group covers, for the focus command. Null when it has none. */
export function annotationSpan(
  shapes: readonly AnnotationShape[]
): { from: number; to: number } | null {
  let from = Number.POSITIVE_INFINITY
  let to = Number.NEGATIVE_INFINITY
  const see = (time: number): void => {
    if (!Number.isFinite(time)) return
    if (time < from) from = time
    if (time > to) to = time
  }
  for (const shape of shapes) {
    switch (shape.kind) {
      case "envelope":
        for (const anchor of shape.highs) see(anchor.time)
        for (const anchor of shape.lows) see(anchor.time)
        break
      case "trendline":
      case "zone":
      case "channel":
      case "fib":
        see(shape.from.time)
        see(shape.to.time)
        break
      case "level":
        see(shape.time)
        break
      case "callout":
        see(shape.at.time)
        see(shape.seat.time)
        break
      case "marker":
      case "arrow":
      case "text":
        see(shape.at.time)
        break
      default:
        break
    }
  }
  return from <= to ? { from, to } : null
}
