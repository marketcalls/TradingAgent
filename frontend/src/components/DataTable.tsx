/** Sortable table for the row-shaped payloads: orderbook, positions, holdings,
 *  tradebook, option chain. Tool results arrive as JSON strings, so extractRows
 *  finds the row array wherever the endpoint happened to put it.
 */

import { useMemo, useState } from "react"
import { ChevronDown, ChevronUp } from "lucide-react"
import {
  cn,
  formatFieldValue,
  isNumericValue,
  isPnlKey,
  labelize,
  pnlClass,
  toNumber
} from "../lib/format"

export type Row = Record<string, unknown>

interface DataTableProps {
  rows: Row[]
  caption?: string
  maxRows?: number
  maxColumns?: number
}

const ROW_KEYS = [
  "data",
  "rows",
  "items",
  "orders",
  "positions",
  "holdings",
  "trades",
  "results",
  "bars",
  "chain",
  "legs",
  "symbols",
  "quotes"
]

function isRowArray(value: unknown): value is Row[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((entry) => entry !== null && typeof entry === "object" && !Array.isArray(entry))
  )
}

/** Returns the first array of objects reachable from the payload, or null when the
 *  payload is a scalar summary that a table would only make harder to read. */
export function extractRows(value: unknown, depth = 0): Row[] | null {
  if (isRowArray(value)) return value
  if (depth > 2 || !value || typeof value !== "object" || Array.isArray(value)) return null
  const record = value as Row
  for (const key of ROW_KEYS) {
    const candidate = record[key]
    if (isRowArray(candidate)) return candidate
    const nested = extractRows(candidate, depth + 1)
    if (nested) return nested
  }
  return null
}

function columnsOf(rows: Row[], limit: number): string[] {
  const seen: string[] = []
  for (const row of rows.slice(0, 20)) {
    for (const key of Object.keys(row)) {
      if (!seen.includes(key)) seen.push(key)
    }
  }
  return seen.slice(0, limit)
}

function isNumericColumn(rows: Row[], key: string): boolean {
  let numeric = 0
  let total = 0
  for (const row of rows.slice(0, 20)) {
    const value = row[key]
    if (value === null || value === undefined || value === "") continue
    total += 1
    if (isNumericValue(value)) numeric += 1
  }
  return total > 0 && numeric / total >= 0.6
}

export default function DataTable({ rows, caption, maxRows = 50, maxColumns = 12 }: DataTableProps) {
  const [sort, setSort] = useState<{ key: string; direction: "asc" | "desc" } | null>(null)
  const columns = useMemo(() => columnsOf(rows, maxColumns), [rows, maxColumns])
  const numericColumns = useMemo(() => {
    const map: Record<string, boolean> = {}
    for (const key of columns) map[key] = isNumericColumn(rows, key)
    return map
  }, [rows, columns])

  const sorted = useMemo(() => {
    if (!sort) return rows
    const factor = sort.direction === "asc" ? 1 : -1
    const numeric = numericColumns[sort.key]
    return rows.slice().sort((left, right) => {
      const a = left[sort.key]
      const b = right[sort.key]
      if (numeric) {
        const x = toNumber(a)
        const y = toNumber(b)
        if (x === null && y === null) return 0
        if (x === null) return 1
        if (y === null) return -1
        return (x - y) * factor
      }
      return String(a ?? "").localeCompare(String(b ?? "")) * factor
    })
  }, [rows, sort, numericColumns])

  const visible = sorted.slice(0, maxRows)

  const toggle = (key: string) =>
    setSort((current) => {
      if (!current || current.key !== key) return { key, direction: "asc" }
      if (current.direction === "asc") return { key, direction: "desc" }
      return null
    })

  if (rows.length === 0 || columns.length === 0) return null

  return (
    <div className="rounded-xl border border-border">
      {caption ? (
        <div className="border-b border-border px-3 py-2 text-xs text-muted-foreground">{caption}</div>
      ) : null}
      <div className="scroll-thin overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border">
              {columns.map((key) => (
                <th
                  key={key}
                  className={cn(
                    "whitespace-nowrap px-3 py-2 font-medium text-muted-foreground",
                    numericColumns[key] ? "text-right" : "text-left"
                  )}
                >
                  <button
                    type="button"
                    onClick={() => toggle(key)}
                    className="inline-flex items-center gap-1 hover:text-foreground"
                  >
                    <span>{labelize(key)}</span>
                    {sort?.key === key ? (
                      sort.direction === "asc" ? (
                        <ChevronUp className="h-3 w-3" />
                      ) : (
                        <ChevronDown className="h-3 w-3" />
                      )
                    ) : null}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, index) => (
              <tr key={index} className={index % 2 === 1 ? "bg-sidebar" : undefined}>
                {columns.map((key) => {
                  const value = row[key]
                  const coloured = isPnlKey(key) && isNumericValue(value)
                  return (
                    <td
                      key={key}
                      className={cn(
                        "whitespace-nowrap px-3 py-1.5",
                        numericColumns[key] ? "text-right tabular-nums" : "text-left",
                        coloured ? pnlClass(value) : undefined
                      )}
                    >
                      {formatFieldValue(key, value)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > visible.length ? (
        <div className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
          showing {visible.length} of {rows.length} rows
        </div>
      ) : null}
    </div>
  )
}
