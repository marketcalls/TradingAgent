/** The collapsible list of tool calls that sits above each answer.
 *
 * One row per call: what it was, the arguments it went out with, whether it came
 * back ok, how long it took, and the result on demand. Results that are row-shaped
 * render as a table, because an orderbook in a pre block is unreadable.
 */

import { useMemo, useState } from "react"
import { Check, ChevronDown, ChevronRight, Circle, X } from "lucide-react"
import type { ToolCall } from "../lib/sse"
import { cn, formatDuration, labelize, stringify } from "../lib/format"
import DataTable, { extractRows } from "./DataTable"

const RESULT_PREVIEW_CHARS = 600

export const TOOL_LABELS: Record<string, string> = {
  get_quote: "Fetching quote",
  get_quotes_bulk: "Fetching quotes",
  get_market_depth: "Reading market depth",
  get_history: "Loading price history",
  get_supported_intervals: "Checking supported intervals",
  get_market_calendar: "Checking the market calendar",
  search_symbols: "Searching symbols",
  get_symbol_info: "Reading symbol details",
  get_expiry_dates: "Listing expiries",
  list_instruments: "Listing instruments",
  get_funds: "Reading funds",
  get_orderbook: "Reading the orderbook",
  get_tradebook: "Reading the tradebook",
  get_positionbook: "Reading positions",
  get_holdings: "Reading holdings",
  calculate_margin: "Calculating margin",
  place_order: "Placing an order",
  place_smart_order: "Placing a smart order",
  place_basket_order: "Placing a basket order",
  place_split_order: "Placing a split order",
  modify_order: "Modifying an order",
  cancel_order: "Cancelling an order",
  cancel_all_orders: "Cancelling all orders",
  close_all_positions: "Closing all positions",
  get_order_status: "Checking order status",
  place_gtt_order: "Placing a GTT order",
  manage_gtt_order: "Managing a GTT order",
  get_option_chain: "Loading the option chain",
  resolve_option_symbol: "Resolving an option symbol",
  get_option_greeks: "Reading option greeks",
  get_synthetic_future: "Reading the synthetic future",
  place_options_order: "Placing an options order",
  place_options_multi_order: "Placing a multi-leg options order",
  list_indicators: "Listing indicators",
  describe_indicator: "Describing an indicator",
  compute_indicator: "Computing an indicator",
  compute_indicators: "Computing indicators",
  scan_indicator: "Scanning with an indicator",
  get_analyzer_status: "Checking analyzer mode",
  set_analyzer_mode: "Switching trading mode",
  get_sandbox_pnl: "Reading simulated P&L",
  ping: "Checking the connection",
  send_alert: "Sending an alert"
}

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? labelize(name)
}

interface ToolTimelineProps {
  tools: ToolCall[]
  running: boolean
}

function ToolRow({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false)
  const pending = call.ok === undefined
  const rows = useMemo(() => {
    if (!call.result) return null
    try {
      return extractRows(JSON.parse(call.result))
    } catch {
      // Plenty of tools return prose or a truncated blob; that is not a failure.
      return null
    }
  }, [call.result])

  const args = call.args ? Object.entries(call.args) : []
  const result = call.result ?? ""
  const longResult = result.length > RESULT_PREVIEW_CHARS

  return (
    <div className="border-t border-border px-3 py-2 first:border-t-0">
      <div className="flex items-center gap-2 text-xs">
        {pending ? (
          <Circle className="h-3 w-3 shrink-0 text-muted-foreground" />
        ) : call.ok ? (
          <Check className="h-3 w-3 shrink-0 text-success" />
        ) : (
          <X className="h-3 w-3 shrink-0 text-danger" />
        )}
        <span className={cn("font-medium", pending && "shimmer")}>{toolLabel(call.name)}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{call.name}</span>
        {call.duration !== undefined && call.duration !== null ? (
          <span className="ml-auto text-[11px] text-muted-foreground">{formatDuration(call.duration)}</span>
        ) : null}
      </div>

      {args.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 pl-5 font-mono text-[11px] text-muted-foreground">
          {args.map(([key, value]) => (
            <span key={key}>
              {key}: <span className="text-foreground">{stringify(value)}</span>
            </span>
          ))}
        </div>
      ) : null}

      {result ? (
        <div className="mt-2 pl-5">
          {rows ? (
            <DataTable rows={rows} maxRows={open ? 200 : 8} />
          ) : (
            <pre className="scroll-thin max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted p-2 font-mono text-[11px] text-muted-foreground">
              {open || !longResult ? result : `${result.slice(0, RESULT_PREVIEW_CHARS)}...`}
            </pre>
          )}
          {longResult || (rows && rows.length > 8) ? (
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              className="mt-1 text-[11px] text-muted-foreground hover:text-foreground"
            >
              {open ? "show less" : "show more"}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

export default function ToolTimeline({ tools, running }: ToolTimelineProps) {
  const [open, setOpen] = useState(false)
  if (tools.length === 0) return null

  const failures = tools.filter((call) => call.ok === false).length
  const pending = running && tools.some((call) => call.ok === undefined)

  return (
    <div className="mb-3 rounded-xl border border-border">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        <span className={cn("font-medium", pending && "shimmer")}>
          {pending ? "Working" : "Behind the scenes"}
        </span>
        <span>
          {tools.length} {tools.length === 1 ? "step" : "steps"}
        </span>
        {failures > 0 ? (
          <span className="text-danger">
            {failures} failed
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="border-t border-border">
          {tools.map((call) => (
            <ToolRow key={call.id} call={call} />
          ))}
        </div>
      ) : null}
    </div>
  )
}
