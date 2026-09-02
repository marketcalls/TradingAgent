/**
 * Dump the chart engine's indicator registry to JSON for the Python toolkit.
 *
 * The chart's indicator set is the BROWSER's, not the backend's. openalgo-charts
 * registers 102 indicators, each with a machine-readable `inputs` array carrying key,
 * label, type, default and (for numbers) min/max/step. That is exactly what
 * "add supertrend 3,10" needs in order to resolve two bare numbers onto two named
 * settings and then bounds-check them.
 *
 * Hand-maintaining that table would rot on the next chart release, so it is generated.
 * Run after any openalgo-charts upgrade:
 *
 *   node backend/app/charts/gen_indicator_catalogue.mjs
 *
 * Importing "openalgo-charts/indicators" is what populates the registry: that entry
 * point is declared sideEffects in the package manifest and its module body calls
 * registerIndicator for every built-in. Importing only the core entry yields an empty
 * list, which is the one failure mode worth naming here.
 *
 * Beside `inputs`, each entry carries what the chart can style but the descriptor
 * does not list as an input:
 *   - style_inputs: the per-plot appearance keys indicatorStyleInputs derives
 *     (upper:opacity, upper:width, upper:lineStyle, upper:type, ...) plus the colour
 *     keys those plots read. One entry per key: the generator repeats a shared colour
 *     key once per plot that uses it (bollinger's bandColor appears under both BB
 *     Upper and BB Lower), and a settings dict has one slot per key.
 *   - fill: {has, color_key, opacity} summarising the descriptor's first fills[] entry,
 *     so the tool knows which indicators shade and which key colours the shading.
 *   - fills: every fills[] entry, for the few that declare more than one (vwap's three
 *     bands, supertrend's two, cci's level band and its own bollinger).
 *
 * The bollinger override below is applied BEFORE the dump. See its comment.
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..", "..");
const PKG = join(ROOT, "frontend", "node_modules", "openalgo-charts");
const OUT = join(HERE, "indicator_catalogue.json");

async function load() {
  // Resolved through the on-disk path rather than a bare specifier, because this
  // script lives under backend/ and node's resolver would not find a package
  // installed under frontend/node_modules.
  const core = await import(pathToFileURL(join(PKG, "dist", "openalgo-charts.mjs")).href);
  await import(pathToFileURL(join(PKG, "dist", "openalgo-charts.indicators.mjs")).href);
  // Registers the point-figure and kagi chart types. Heikin-Ashi and Renko live in
  // this entry too but are BAR TRANSFORMS, not chart types, so they never appear in
  // registeredChartTypes and set_chart_type cannot select them.
  await import(pathToFileURL(join(PKG, "dist", "openalgo-charts.transform.mjs")).href);
  const version = JSON.parse(
    await import("node:fs").then((fs) => fs.readFileSync(join(PKG, "package.json"), "utf8")),
  ).version;
  return { core, version };
}

function cleanInput(input) {
  const out = { key: input.key, type: input.type, label: input.label };
  if (input.default !== undefined) out.default = input.default;
  if (input.min !== undefined) out.min = input.min;
  if (input.max !== undefined) out.max = input.max;
  if (input.step !== undefined) out.step = input.step;
  if (input.group !== undefined) out.group = input.group;
  if (Array.isArray(input.options)) {
    out.options = input.options.map((o) => ({ label: o.label, value: o.value }));
  }
  return out;
}

const { core, version } = await load();

if (typeof core.registeredIndicators !== "function") {
  console.error("registeredIndicators is not exported by this build of openalgo-charts");
  process.exit(1);
}

if (!core.registeredIndicators().length) {
  console.error("registry is empty: the indicators side-effect entry did not load");
  process.exit(1);
}

/**
 * The bollinger override.
 *
 * openalgo-charts 1.9.2 registers bollinger with NO fills, while every other band
 * indicator (envelope, donchian, keltner-channel, ma-channel, standard-error-bands)
 * declares fills [{between: ["upper", "lower"], colorUpKey: "fillColor",
 * colorDownKey: "fillColor", opacity: 0.05}] and a fillColor input. The browser
 * patches the built-in over the same id in
 * frontend/src/lib/charts/indicatorOverrides.ts, and this script applies the same
 * patch before dumping so the catalogue advertises what the chart will accept.
 *
 * The constants below are read out of both files by name and compared by
 * backend/tests/test_catalogue.py: change one side and the test fails.
 */
const BOLLINGER_ID = "bollinger";
const BOLLINGER_FILL_KEY = "fillColor";
const BOLLINGER_FILL_LABEL = "Fill";
const BOLLINGER_FILL_OPACITY = 0.08;
const BOLLINGER_FILL_BETWEEN = ["upper", "lower"];
const BOLLINGER_BAND_KEY = "bandColor";
const BOLLINGER_BAND_FALLBACK = "#4f8cff";

function applyIndicatorOverrides() {
  if (!core.hasIndicator(BOLLINGER_ID)) return;
  const builtin = core.getIndicator(BOLLINGER_ID);
  const hasInput = builtin.inputs.some((i) => i.key === BOLLINGER_FILL_KEY);
  if (hasInput && (builtin.fills || []).length) return;
  const band = builtin.inputs.find((i) => i.key === BOLLINGER_BAND_KEY);
  const fillDefault = band && band.type === "color" ? band.default : BOLLINGER_BAND_FALLBACK;
  // registerIndicator over an existing id replaces the entry in place: the registry
  // stays at 102 and getIndicator returns this descriptor.
  core.registerIndicator({
    ...builtin,
    inputs: [
      ...builtin.inputs,
      { key: BOLLINGER_FILL_KEY, type: "color", label: BOLLINGER_FILL_LABEL, default: fillDefault },
    ],
    fills: [
      {
        between: BOLLINGER_FILL_BETWEEN,
        colorUpKey: BOLLINGER_FILL_KEY,
        colorDownKey: BOLLINGER_FILL_KEY,
        opacity: BOLLINGER_FILL_OPACITY,
      },
    ],
  });
}

applyIndicatorOverrides();

const descriptors = core.registeredIndicators();

function styleInputs(d) {
  if (typeof core.indicatorStyleInputs !== "function") return [];
  const byKey = new Map();
  for (const input of core.indicatorStyleInputs(d)) {
    if (!byKey.has(input.key)) byKey.set(input.key, cleanInput(input));
  }
  return [...byKey.values()];
}

function fillEntries(d) {
  return (d.fills || []).map((f) => ({
    between: [...f.between],
    color_up_key: f.colorUpKey ?? null,
    color_down_key: f.colorDownKey ?? null,
    opacity: typeof f.opacity === "number" ? f.opacity : null,
  }));
}

function fillSummary(fills) {
  const first = fills[0];
  return {
    has: fills.length > 0,
    color_key: first ? first.color_up_key : null,
    opacity: first ? first.opacity : null,
  };
}

const indicators = {};
for (const d of descriptors) {
  // Colour and text inputs are presentation, not analysis. They are kept so the tool
  // can still report a full settings dict, but the numeric and boolean ones are what
  // a spoken request like "supertrend 3,10" can ever land on.
  const inputs = (d.inputs || []).map(cleanInput);
  const fills = fillEntries(d);
  indicators[d.id] = {
    id: d.id,
    name: d.name,
    category: d.category || "",
    placement: d.placement,
    inputs,
    // Positional order for bare numeric arguments: declaration order of the number
    // inputs, which is the order the settings panel shows them in.
    numeric_keys: inputs.filter((i) => i.type === "number").map((i) => i.key),
    plots: (d.plots || []).map((p) => p.key || p.id).filter(Boolean),
    style_inputs: styleInputs(d),
    fill: fillSummary(fills),
    fills,
  };
}

const chartTypes =
  typeof core.registeredChartTypes === "function" ? core.registeredChartTypes() : [];

const payload = {
  generated_by: "backend/app/charts/gen_indicator_catalogue.mjs",
  chart_engine: "openalgo-charts",
  chart_engine_version: version,
  count: Object.keys(indicators).length,
  chart_types: chartTypes,
  indicators,
};

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, JSON.stringify(payload, null, 2) + "\n", "utf8");
console.log(`wrote ${payload.count} indicators from openalgo-charts ${version} to ${OUT}`);
