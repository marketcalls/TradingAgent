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
 *     then the drawing's own style. Tool defaults BEAT the controller's, so
 *     every colour, fill and level list is set per drawing here rather than once
 *     on the controller. The fib tools ship fill: true, fillOpacity: 0.06, which
 *     is why "fib" passes fill: false for bare levels.
 *   - Only some tools render style.text. rectangle and parallel-channel draw it
 *     through shapeLabel, callout and price-note through their own plates, and
 *     the line tools draw none at all. A label on a line therefore becomes a
 *     second drawing of the text tool rather than a style key that silently does
 *     nothing.
 *   - A non-finite anchor is rejected outright by the controller. Filtering here
 *     costs one bad anchor its drawing instead of throwing part-way through a
 *     group and leaving the chart half marked up.
 *
 * Colour is resolved from the theme, never written as a hex: DrawingStyle stores
 * a literal string, so a theme swap has to re-issue these drawings, and it can
 * only do that if one function decides what a tone looks like.
 */

import type { Drawing, DrawingPoint, DrawingStyle } from "openalgo-charts/draw"
import { AI_PREFIX, aiDrawingId, type Anchor, type AnnotationShape } from "./types"
import { toneColor, type TonePalette } from "./theme"

/** A drawing with the id already decided, ready for DrawingController.add. */
export type AiDrawing = Omit<Drawing, "id"> & { id: string }

/** Fib levels when the analyst names none. The tool's own default list. */
const DEFAULT_FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]

/** Fill strength for a band. Light enough that candles read through it. */
const BAND_OPACITY = 0.1

/** A group id safe to embed in a drawing id.
 *
 *  "#" is removed because the hit-test parser splits an external id on it, so a
 *  group carrying one would make every drawing in it un-selectable. ":" is
 *  removed because it is this scheme's own separator: without that, clearing
 *  group "a" would also clear group "a:b", whose ids share the prefix.
 */
export function sanitizeGroup(group: string): string {
  const cleaned = group.replace(/[#:]/g, "-").trim()
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

/** Collects drawings for one group, handing out stable sequential ids. */
class GroupBuilder {
  private readonly out: AiDrawing[] = []
  private next = 0

  constructor(
    private readonly group: string,
    private readonly paneIndex: number
  ) {}

  add(tool: string, points: DrawingPoint[], style: DrawingStyle): void {
    this.out.push({
      id: aiDrawingId(sanitizeGroup(this.group), this.next++),
      tool,
      points,
      style,
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
    lineWidth: 1.5,
    fill: true,
    fillColor: color,
    fillOpacity: BAND_OPACITY
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
    lineWidth: 1.5,
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
    lineWidth: 1.5,
    lineStyle: "dashed",
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
    lineWidth: 1.5,
    fill: true,
    fillColor: color,
    fillOpacity: BAND_OPACITY,
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
    lineWidth: 1.5,
    fill: true,
    fillColor: color,
    fillOpacity: 0.08,
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
    lineWidth: 1,
    levels: levels.length > 0 ? levels : DEFAULT_FIB_LEVELS,
    showLabels: true,
    // The tool ships fill: true, fillOpacity: 0.06. Bare levels read better over
    // candles than seven tinted bands do.
    fill: false
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
  // and the bubble sits on the second.
  builder.add("callout", [point(shape.at), point(shape.seat)], {
    color,
    fontColor: color,
    lineWidth: 1.5,
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
  builder.add("price-note", [point(shape.at)], {
    color,
    fontColor: color,
    lineWidth: 1.5,
    fontSize: 11,
    text: shape.text
  })
}

/** Translate one analyst group into the drawings that render it.
 *
 *  Returns them in emission order, ids already assigned, so the caller can add
 *  them one at a time. Per-drawing add() rather than fromJSON is deliberate:
 *  fromJSON replaces the whole model and wipes undo, redo and selection, which
 *  would throw away the user's own history to place the analyst's markup.
 */
export function buildAnnotations(
  group: string,
  shapes: readonly AnnotationShape[],
  palette: TonePalette,
  paneIndex = 0
): AiDrawing[] {
  const builder = new GroupBuilder(group, paneIndex)
  for (const shape of shapes) {
    const color = toneColor(palette, shape.tone)
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
        see(shape.at.time)
        break
      default:
        break
    }
  }
  return from <= to ? { from, to } : null
}
