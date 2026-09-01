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

const descriptors = core.registeredIndicators();
if (!descriptors.length) {
  console.error("registry is empty: the indicators side-effect entry did not load");
  process.exit(1);
}

const indicators = {};
for (const d of descriptors) {
  // Colour and text inputs are presentation, not analysis. They are kept so the tool
  // can still report a full settings dict, but the numeric and boolean ones are what
  // a spoken request like "supertrend 3,10" can ever land on.
  const inputs = (d.inputs || []).map(cleanInput);
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
