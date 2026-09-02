/** Patches to the chart engine's built-in indicators, registered over the same id.
 *
 * Facts this module is built around, measured against openalgo-charts 1.9.2:
 *   - bollinger registers with inputs length, stdDev, source, basisColor and
 *     bandColor, plots upper, basis and lower, and NO fills. Every other band
 *     indicator (envelope, donchian, keltner-channel, ma-channel,
 *     standard-error-bands) declares fills [{between: ["upper", "lower"],
 *     colorUpKey: "fillColor", colorDownKey: "fillColor", opacity: 0.05}] and a
 *     fillColor input. So "fill the bollinger bands" had nothing to set, and the
 *     analyst refused it with "this chart's indicator does not support band fill".
 *   - registerIndicator over an existing id replaces the entry in place: the
 *     registry stays at 102 and getIndicator returns the new descriptor. That is
 *     why the patch keeps the id "bollinger" rather than adding a second
 *     indicator: the picker, persisted layouts and the analyst's catalogue all see
 *     one Bollinger, and a layout saved before this module existed still restores
 *     because indicatorDefaults supplies fillColor from the new input.
 *   - indicatorStyleInputs is derived from plots, not inputs, so the per-plot
 *     style keys (upper:opacity, upper:width, ...) are unchanged by the patch.
 *   - getIndicator throws for an unknown id, which is what every id is before the
 *     "openalgo-charts/indicators" tier has been imported. The terminal calls
 *     applyIndicatorOverrides right after that import, every time; a call before
 *     it answers false and does nothing rather than throwing into the chart's boot.
 *
 * backend/app/charts/gen_indicator_catalogue.mjs applies the identical patch
 * before dumping the catalogue, so the analyst is told about fillColor, and
 * backend/tests/test_catalogue.py reads the BOLLINGER_* constants out of both
 * files by name and compares them: change one side and the test fails.
 */

import {
  getIndicator,
  hasIndicator,
  registerIndicator,
  type IndicatorDescriptor,
  type IndicatorFillSpec,
  type IndicatorInput,
} from "openalgo-charts"

export const BOLLINGER_ID = "bollinger"
export const BOLLINGER_FILL_KEY = "fillColor"
export const BOLLINGER_FILL_LABEL = "Fill"
export const BOLLINGER_FILL_OPACITY = 0.08
export const BOLLINGER_FILL_BETWEEN = ["upper", "lower"] as const

/** The input whose default the fill borrows, so an untouched fill matches the bands. */
const BOLLINGER_BAND_KEY = "bandColor"
const BOLLINGER_BAND_FALLBACK = "#4f8cff"

/** Apply every override. Idempotent: a second call finds the patch in place and
 * leaves it alone. Answers false when the indicators tier is not loaded yet, in
 * which case nothing was patched and the caller should call again after the import.
 */
export function applyIndicatorOverrides(): boolean {
  return overrideBollinger()
}

function isBollingerPatched(descriptor: IndicatorDescriptor): boolean {
  const hasInput = descriptor.inputs.some((input) => input.key === BOLLINGER_FILL_KEY)
  return hasInput && (descriptor.fills?.length ?? 0) > 0
}

function overrideBollinger(): boolean {
  if (!hasIndicator(BOLLINGER_ID)) return false
  const builtin = getIndicator(BOLLINGER_ID)
  if (isBollingerPatched(builtin)) return true
  const band = builtin.inputs.find((input) => input.key === BOLLINGER_BAND_KEY)
  const fillDefault = band && band.type === "color" ? band.default : BOLLINGER_BAND_FALLBACK
  const fillInput: IndicatorInput = {
    key: BOLLINGER_FILL_KEY,
    type: "color",
    label: BOLLINGER_FILL_LABEL,
    default: fillDefault,
  }
  const fill: IndicatorFillSpec = {
    between: BOLLINGER_FILL_BETWEEN,
    colorUpKey: BOLLINGER_FILL_KEY,
    colorDownKey: BOLLINGER_FILL_KEY,
    opacity: BOLLINGER_FILL_OPACITY,
  }
  const patched: IndicatorDescriptor = {
    ...builtin,
    inputs: [...builtin.inputs, fillInput],
    fills: [fill],
  }
  registerIndicator(patched)
  return true
}
