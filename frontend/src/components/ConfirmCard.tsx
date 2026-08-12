/** The confirmation gate. The most important component in the app.
 *
 * Its job is to make an unreviewed approval harder than a reviewed one:
 *   - the header says in plain words what will happen, and whether it is real,
 *   - every argument is a labelled row, never a collapsed JSON blob,
 *   - multi-leg orders show each leg separately with its resolved symbol,
 *   - the server-computed preview sits beside the arguments, because notional
 *     value, current position and available funds are the numbers that catch a
 *     mistake and the model does not produce them,
 *   - a live card is red and its Approve button stays disabled for 400ms, which
 *     defeats a reflex click on a card that appeared under the cursor,
 *   - turning analyzer mode off additionally requires typing GO LIVE,
 *   - and once submitted the card is frozen, so a stale card from a reloaded page
 *     cannot post a second decision for the same run.
 */

import { useEffect, useMemo, useState } from "react"
import { AlertTriangle, ShieldCheck } from "lucide-react"
import type { TradingMode } from "../lib/api"
import type { ConfirmRequirement, ConfirmState } from "../lib/sse"
import { cn, formatFieldValue, isPnlKey, isNumericValue, labelize, pnlClass, stringify } from "../lib/format"

const GO_LIVE_PHRASE = "GO LIVE"

interface ActionPhrase {
  live: string
  simulated: string
}

const ACTIONS: Record<string, ActionPhrase> = {
  place_order: { live: "Place a LIVE order", simulated: "Simulated order (analyze mode)" },
  place_smart_order: {
    live: "Place a LIVE smart order",
    simulated: "Simulated smart order (analyze mode)"
  },
  place_basket_order: {
    live: "Place a LIVE basket order",
    simulated: "Simulated basket order (analyze mode)"
  },
  place_split_order: {
    live: "Place a LIVE split order",
    simulated: "Simulated split order (analyze mode)"
  },
  place_options_order: {
    live: "Place a LIVE options order",
    simulated: "Simulated options order (analyze mode)"
  },
  place_options_multi_order: {
    live: "Place a LIVE multi-leg options order",
    simulated: "Simulated multi-leg options order (analyze mode)"
  },
  modify_order: { live: "Modify a LIVE order", simulated: "Simulated modification (analyze mode)" },
  cancel_order: { live: "Cancel a LIVE order", simulated: "Simulated cancellation (analyze mode)" },
  cancel_all_orders: {
    live: "Cancel every LIVE open order",
    simulated: "Simulated cancel-all (analyze mode)"
  },
  close_all_positions: {
    live: "Close every LIVE open position",
    simulated: "Simulated square-off (analyze mode)"
  },
  place_gtt_order: { live: "Place a GTT order", simulated: "Place a GTT order" },
  manage_gtt_order: { live: "Change a GTT order", simulated: "Change a GTT order" },
  send_alert: { live: "Send an alert", simulated: "Send an alert" }
}

const PREVIEW_ORDER = [
  "mode",
  "notional",
  "notional_value",
  "order_value",
  "ltp",
  "current_position",
  "position",
  "available_funds",
  "available_cash",
  "funds",
  "margin_required"
]

function toolNameOf(requirement: ConfirmRequirement): string {
  return requirement.tool_execution?.tool_name ?? "unknown_tool"
}

function argsOf(requirement: ConfirmRequirement): Record<string, unknown> {
  return requirement.tool_execution?.tool_args ?? {}
}

/** True when this requirement is the switch that makes every later order real. */
function wantsLiveMode(requirement: ConfirmRequirement): boolean {
  if (toolNameOf(requirement) !== "set_analyzer_mode") return false
  const args = argsOf(requirement)
  const analyze = args.analyze ?? args.analyzer ?? args.analyze_mode
  if (typeof analyze === "boolean") return !analyze
  if (typeof analyze === "string") return analyze.trim().toLowerCase() === "false"
  const live = args.live
  return live === true || live === "true"
}

function isGttTool(requirement: ConfirmRequirement): boolean {
  return toolNameOf(requirement).includes("gtt")
}

/** The preview carries the mode the backend priced this order under, which beats
 *  the banner's polled value: the banner can be a few seconds stale. */
function modeOf(requirement: ConfirmRequirement, fallback: TradingMode): TradingMode {
  const raw = requirement.preview?.mode
  if (typeof raw === "string") {
    const lowered = raw.trim().toLowerCase()
    if (lowered.includes("live") || lowered.includes("real")) return "live"
    if (lowered.includes("analy") || lowered.includes("sandbox") || lowered.includes("simul")) {
      return "analyze"
    }
  }
  return fallback
}

function headlineOf(requirement: ConfirmRequirement, mode: TradingMode): string {
  const tool = toolNameOf(requirement)
  if (tool === "set_analyzer_mode") {
    return wantsLiveMode(requirement) ? "Switch to LIVE trading" : "Switch back to analyze mode"
  }
  const phrase = ACTIONS[tool]
  if (phrase) return mode === "live" ? phrase.live : phrase.simulated
  const base = labelize(tool)
  return mode === "live" ? `${base} (LIVE)` : `${base} (analyze mode)`
}

function isLegArray(value: unknown): value is Record<string, unknown>[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((entry) => entry !== null && typeof entry === "object" && !Array.isArray(entry))
  )
}

function Field({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-6 border-t border-border py-1.5 first:border-t-0">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn("text-right text-sm font-medium tabular-nums", tone)}>{value}</span>
    </div>
  )
}

function FieldRows({ record }: { record: Record<string, unknown> }) {
  const entries = Object.entries(record)
  if (entries.length === 0) {
    return <div className="py-1.5 text-sm text-muted-foreground">no arguments</div>
  }
  return (
    <>
      {entries.map(([key, value]) => {
        if (isLegArray(value)) {
          return (
            <div key={key} className="border-t border-border py-2 first:border-t-0">
              <div className="mb-1.5 text-xs text-muted-foreground">
                {labelize(key)} ({value.length} {value.length === 1 ? "leg" : "legs"})
              </div>
              <div className="space-y-1.5">
                {value.map((leg, index) => (
                  <div key={index} className="rounded-lg border border-border px-2.5 py-1.5">
                    <div className="mb-1 text-xs font-medium text-muted-foreground">Leg {index + 1}</div>
                    {Object.entries(leg).map(([legKey, legValue]) => (
                      <Field
                        key={legKey}
                        label={labelize(legKey)}
                        value={formatFieldValue(legKey, legValue)}
                      />
                    ))}
                  </div>
                ))}
              </div>
            </div>
          )
        }
        if (Array.isArray(value)) {
          return (
            <Field
              key={key}
              label={labelize(key)}
              value={value.map((entry) => stringify(entry)).join(", ")}
            />
          )
        }
        if (value !== null && typeof value === "object") {
          return (
            <div key={key} className="border-t border-border py-2 first:border-t-0">
              <div className="mb-1 text-xs text-muted-foreground">{labelize(key)}</div>
              <div className="rounded-lg border border-border px-2.5 py-1.5">
                {Object.entries(value as Record<string, unknown>).map(([innerKey, innerValue]) => (
                  <Field
                    key={innerKey}
                    label={labelize(innerKey)}
                    value={formatFieldValue(innerKey, innerValue)}
                  />
                ))}
              </div>
            </div>
          )
        }
        return (
          <Field
            key={key}
            label={labelize(key)}
            value={formatFieldValue(key, value)}
            tone={isPnlKey(key) && isNumericValue(value) ? pnlClass(value) : undefined}
          />
        )
      })}
    </>
  )
}

function PreviewBlock({ preview }: { preview: Record<string, unknown> }) {
  const warnings = preview.warnings
  const entries = Object.entries(preview).filter(([key]) => key !== "warnings")
  entries.sort(([left], [right]) => {
    const a = PREVIEW_ORDER.indexOf(left)
    const b = PREVIEW_ORDER.indexOf(right)
    return (a === -1 ? PREVIEW_ORDER.length : a) - (b === -1 ? PREVIEW_ORDER.length : b)
  })
  const warningList = Array.isArray(warnings)
    ? warnings.map((entry) => stringify(entry)).filter((entry) => entry.trim())
    : typeof warnings === "string" && warnings.trim()
      ? [warnings]
      : []

  if (entries.length === 0 && warningList.length === 0) return null

  return (
    <div className="mt-3 rounded-lg border border-border bg-muted px-3 py-2">
      <div className="mb-1 text-xs font-medium text-muted-foreground">Checked by the server</div>
      <FieldRows record={Object.fromEntries(entries)} />
      {warningList.length > 0 ? (
        <ul className="mt-2 space-y-1 text-xs text-danger">
          {warningList.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

interface ConfirmCardProps {
  state: ConfirmState
  /** Mode from the banner, used when a requirement carries no preview of its own. */
  mode: TradingMode
  onSubmit: (decisions: Record<string, boolean>, notes: Record<string, string>) => void
}

export default function ConfirmCard({ state, mode, onSubmit }: ConfirmCardProps) {
  const { requirements, resolved, submitting } = state
  const [decisions, setDecisions] = useState<Record<string, boolean>>({})
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [goLiveText, setGoLiveText] = useState("")
  const [armed, setArmed] = useState(false)

  const dangerous = useMemo(
    () =>
      requirements.some(
        (requirement) =>
          modeOf(requirement, mode) === "live" ||
          wantsLiveMode(requirement) ||
          isGttTool(requirement)
      ),
    [requirements, mode]
  )
  const goLiveNeeded = useMemo(() => requirements.some(wantsLiveMode), [requirements])

  useEffect(() => {
    if (!dangerous) {
      setArmed(true)
      return
    }
    // A card that appears under a moving cursor must not be clickable on arrival.
    setArmed(false)
    const timer = window.setTimeout(() => setArmed(true), 400)
    return () => window.clearTimeout(timer)
  }, [dangerous, requirements])

  const single = requirements.length === 1
  const busy = resolved || submitting
  const goLiveOk = !goLiveNeeded || goLiveText.trim().toUpperCase() === GO_LIVE_PHRASE
  const allDecided = requirements.every((requirement) => decisions[requirement.id] !== undefined)
  const approveBlocked = busy || !armed || !goLiveOk

  const submit = (map: Record<string, boolean>) => {
    if (busy) return
    const trimmed: Record<string, string> = {}
    for (const [id, note] of Object.entries(notes)) {
      if (note.trim()) trimmed[id] = note.trim()
    }
    onSubmit(map, trimmed)
  }

  const submitSingle = (approved: boolean) => {
    const requirement = requirements[0]
    if (!requirement) return
    if (approved && approveBlocked) return
    submit({ [requirement.id]: approved })
  }

  const submitMany = () => {
    if (!allDecided) return
    const approvingAny = Object.values(decisions).some(Boolean)
    if (approvingAny && approveBlocked) return
    submit({ ...decisions })
  }

  if (requirements.length === 0) return null

  const tone = dangerous
    ? "border-danger-border bg-danger-soft"
    : mode === "unknown"
      ? "border-warn-border bg-warn-soft"
      : "border-success-border bg-success-soft"

  return (
    <div className={cn("mb-4 rounded-2xl border p-4", tone, busy && "opacity-70")}>
      <div className="flex items-start gap-2">
        {dangerous ? (
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
        ) : (
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-success" />
        )}
        <div className="min-w-0">
          <div className={cn("text-sm font-semibold", dangerous ? "text-danger" : "text-foreground")}>
            {single
              ? headlineOf(requirements[0], modeOf(requirements[0], mode))
              : `${requirements.length} actions need your approval`}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {dangerous
              ? "This reaches the broker with real money. Read every line before approving."
              : "Analyzer mode is on, so this is simulated and no money moves."}
          </div>
        </div>
      </div>

      <div className="mt-3 space-y-3">
        {requirements.map((requirement) => {
          const requirementMode = modeOf(requirement, mode)
          const decision = decisions[requirement.id]
          const preview = requirement.preview ?? null
          return (
            <div key={requirement.id} className="rounded-xl border border-border bg-background p-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="text-sm font-medium">{headlineOf(requirement, requirementMode)}</div>
                <div className="font-mono text-[11px] text-muted-foreground">
                  {toolNameOf(requirement)}
                </div>
              </div>

              {isGttTool(requirement) ? (
                <div className="mt-2 text-xs text-danger">
                  GTT cannot be rehearsed. Analyzer mode returns 501 for GTT, so this order is live
                  even while the rest of the app is simulated.
                </div>
              ) : null}

              {wantsLiveMode(requirement) ? (
                <div className="mt-2 text-xs text-danger">
                  Every order placed after this switch is real and reaches the broker.
                </div>
              ) : null}

              <div className="mt-2">
                <FieldRows record={argsOf(requirement)} />
              </div>

              {preview ? <PreviewBlock preview={preview as Record<string, unknown>} /> : null}

              <div className="mt-3">
                <label className="block text-xs text-muted-foreground" htmlFor={`note-${requirement.id}`}>
                  Note sent back to the model if you reject
                </label>
                <input
                  id={`note-${requirement.id}`}
                  type="text"
                  disabled={busy}
                  value={notes[requirement.id] ?? ""}
                  onChange={(event) =>
                    setNotes((current) => ({ ...current, [requirement.id]: event.target.value }))
                  }
                  placeholder="optional, for example: quantity is too large, use 10"
                  className="mt-1 w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary disabled:opacity-60"
                />
              </div>

              {!single ? (
                <div className="mt-3 flex items-center gap-2">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setDecisions((current) => ({ ...current, [requirement.id]: true }))}
                    className={cn(
                      "rounded-lg border px-3 py-1 text-xs font-medium",
                      decision === true
                        ? "border-transparent bg-primary text-primary-foreground"
                        : "border-input"
                    )}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setDecisions((current) => ({ ...current, [requirement.id]: false }))}
                    className={cn(
                      "rounded-lg border px-3 py-1 text-xs font-medium",
                      decision === false ? "border-transparent bg-danger text-primary-foreground" : "border-input"
                    )}
                  >
                    Reject
                  </button>
                  {resolved ? (
                    <span className="text-xs text-muted-foreground">
                      {state.decisions[requirement.id] ? "approved" : "rejected"}
                    </span>
                  ) : null}
                </div>
              ) : null}
            </div>
          )
        })}
      </div>

      {goLiveNeeded && !resolved ? (
        <div className="mt-3">
          <label className="block text-xs text-muted-foreground" htmlFor="go-live-confirm">
            Type {GO_LIVE_PHRASE} to enable the approve button
          </label>
          <input
            id="go-live-confirm"
            type="text"
            disabled={busy}
            value={goLiveText}
            onChange={(event) => setGoLiveText(event.target.value)}
            placeholder={GO_LIVE_PHRASE}
            className="mt-1 w-full rounded-lg border border-danger-border bg-background px-2.5 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus:border-danger disabled:opacity-60"
          />
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {resolved ? (
          <span className="text-sm font-medium">
            Decision submitted{submitting ? ", the run is resuming" : ""}
          </span>
        ) : single ? (
          <>
            <button
              type="button"
              disabled={approveBlocked}
              onClick={() => submitSingle(true)}
              className={cn(
                "rounded-lg px-4 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50",
                dangerous ? "bg-danger" : "bg-primary"
              )}
            >
              Approve
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => submitSingle(false)}
              className="rounded-lg border border-input px-4 py-1.5 text-sm font-medium disabled:opacity-50"
            >
              Reject
            </button>
          </>
        ) : (
          <button
            type="button"
            disabled={busy || !allDecided || (Object.values(decisions).some(Boolean) && approveBlocked)}
            onClick={submitMany}
            className={cn(
              "rounded-lg px-4 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50",
              dangerous ? "bg-danger" : "bg-primary"
            )}
          >
            Submit decisions
          </button>
        )}
        {!resolved && dangerous && !armed ? (
          <span className="text-xs text-muted-foreground">approve unlocks in a moment</span>
        ) : null}
        {!resolved && goLiveNeeded && !goLiveOk ? (
          <span className="text-xs text-muted-foreground">
            type {GO_LIVE_PHRASE} above to approve
          </span>
        ) : null}
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">run {state.runId}</span>
      </div>
    </div>
  )
}
