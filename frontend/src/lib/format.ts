/** Number, money and time rendering for Indian markets, plus the one class helper.
 *
 * Rules this module encodes, learned from the broker payloads it has to display:
 *   - Indian grouping is 2-2-3 (12,34,567.89), never 3-3-3, so every number goes
 *     through Intl.NumberFormat("en-IN") rather than the browser's own locale.
 *   - Large money reads as lakh and crore, not million and billion.
 *   - OpenAlgo returns numbers as strings often enough ("1,234.50", "", "-") that
 *     every entry point coerces defensively and falls back to a dash.
 *   - Exchange times are IST wherever the browser sits, so every time render pins
 *     timeZone to Asia/Kolkata. A naive "2026-08-12 15:04:05" carries no offset and
 *     is therefore assumed to already be IST.
 */

import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

const LOCALE = "en-IN"
const TIMEZONE = "Asia/Kolkata"

/** Unicode rupee sign. A currency symbol, not an icon. */
export const RUPEE = "₹"

/** What every formatter returns when a value cannot be read as a number or date. */
export const EMPTY = "-"

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

export function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null
  if (typeof value === "string") {
    const cleaned = value.replace(/[,\s]/g, "").replace(RUPEE, "")
    if (!cleaned) return null
    const parsed = Number(cleaned)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

export function isNumericValue(value: unknown): boolean {
  if (typeof value === "boolean") return false
  return toNumber(value) !== null
}

const formatters = new Map<string, Intl.NumberFormat>()

function grouped(minimum: number, maximum: number): Intl.NumberFormat {
  const key = `${minimum}:${maximum}`
  let formatter = formatters.get(key)
  if (!formatter) {
    formatter = new Intl.NumberFormat(LOCALE, {
      minimumFractionDigits: minimum,
      maximumFractionDigits: maximum
    })
    formatters.set(key, formatter)
  }
  return formatter
}

export function formatNumber(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return grouped(decimals, decimals).format(parsed)
}

/** Quantities are whole lots most of the time; fractional ones still need to show. */
export function formatQuantity(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return grouped(0, Number.isInteger(parsed) ? 0 : 4).format(parsed)
}

/** Prices carry the tick precision the caller knows about; two decimals otherwise. */
export function formatPrice(value: unknown, decimals = 2): string {
  return formatNumber(value, decimals)
}

export function formatCurrency(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed < 0 ? "-" : ""
  return `${sign}${RUPEE}${grouped(decimals, decimals).format(Math.abs(parsed))}`
}

export function formatSignedCurrency(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed > 0 ? "+" : parsed < 0 ? "-" : ""
  return `${sign}${RUPEE}${grouped(decimals, decimals).format(Math.abs(parsed))}`
}

export function formatSigned(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed > 0 ? "+" : parsed < 0 ? "-" : ""
  return `${sign}${grouped(decimals, decimals).format(Math.abs(parsed))}`
}

export function formatPercent(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return `${formatSigned(parsed, decimals)}%`
}

/** Lakh and crore grouping for headline figures: 1.25 Cr rather than 12,500,000. */
export function formatCompact(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed < 0 ? "-" : ""
  const size = Math.abs(parsed)
  if (size >= 1e7) return `${sign}${grouped(2, 2).format(size / 1e7)} Cr`
  if (size >= 1e5) return `${sign}${grouped(2, 2).format(size / 1e5)} L`
  if (size >= 1e3) return `${sign}${grouped(2, 2).format(size / 1e3)} K`
  return `${sign}${grouped(0, 2).format(size)}`
}

export function formatCompactCurrency(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed < 0 ? "-" : ""
  return `${sign}${RUPEE}${formatCompact(Math.abs(parsed))}`
}

/** Tailwind text colour for a signed figure. Flat zero stays neutral, not green. */
export function pnlClass(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null || parsed === 0) return "text-muted-foreground"
  return parsed > 0 ? "text-success" : "text-danger"
}

export function toDate(value: unknown): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === "number") {
    // Epoch seconds and epoch milliseconds both turn up; anything under 1e11 is seconds.
    const millis = Math.abs(value) < 1e11 ? value * 1000 : value
    const parsed = new Date(millis)
    return Number.isNaN(parsed.getTime()) ? null : parsed
  }
  if (typeof value === "string") {
    const text = value.trim()
    if (!text) return null
    if (/^-?\d+(\.\d+)?$/.test(text)) return toDate(Number(text))
    // "2026-08-12 15:04:05" is not ISO 8601 and Safari refuses it outright.
    const parsed = new Date(text.includes("T") ? text : text.replace(" ", "T"))
    return Number.isNaN(parsed.getTime()) ? null : parsed
  }
  return null
}

const dateTimeFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: TIMEZONE,
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
})

const timeFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: TIMEZONE,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
})

const dateFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: TIMEZONE,
  day: "2-digit",
  month: "short",
  year: "numeric"
})

export function formatDateTime(value: unknown): string {
  const parsed = toDate(value)
  return parsed ? dateTimeFormat.format(parsed) : EMPTY
}

export function formatTime(value: unknown): string {
  const parsed = toDate(value)
  return parsed ? timeFormat.format(parsed) : EMPTY
}

export function formatDate(value: unknown): string {
  const parsed = toDate(value)
  return parsed ? dateFormat.format(parsed) : EMPTY
}

/** Sidebar and timeline stamps: today shows a clock, older rows show the date. */
export function formatStamp(value: unknown): string {
  const parsed = toDate(value)
  if (!parsed) return EMPTY
  const now = new Date()
  const sameDay = dateFormat.format(parsed) === dateFormat.format(now)
  return sameDay ? timeFormat.format(parsed) : dateFormat.format(parsed)
}

export function formatDuration(seconds: unknown): string {
  const parsed = toNumber(seconds)
  if (parsed === null) return EMPTY
  if (parsed < 1) return `${Math.round(parsed * 1000)}ms`
  return `${grouped(1, 1).format(parsed)}s`
}

const LABEL_OVERRIDES: Record<string, string> = {
  pnl: "P&L",
  m2m: "M2M",
  mtm: "MTM",
  ltp: "LTP",
  oi: "Open interest",
  iv: "Implied volatility",
  ohlc: "OHLC",
  gtt: "GTT",
  id: "ID",
  orderid: "Order ID",
  order_id: "Order ID",
  pricetype: "Price type",
  price_type: "Price type",
  tradingsymbol: "Symbol",
  disclosedquantity: "Disclosed quantity",
  triggerprice: "Trigger price",
  splitsize: "Split size",
  run_id: "Run ID",
  session_id: "Session ID"
}

/** Turns a payload key into a human label. Trading payloads mix snake_case, flat
 *  lowercase ("pricetype") and camelCase in the same response. */
export function labelize(key: string): string {
  const trimmed = key.trim()
  const override = LABEL_OVERRIDES[trimmed.toLowerCase()]
  if (override) return override
  const spaced = trimmed
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
  if (!spaced) return trimmed
  return spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase()
}

const MONEY_KEY = /(price|value|amount|funds|cash|margin|notional|premium|charge|brokerage|collateral|balance|payin|payout|ltp|avg|average|close|open|high|low|prev)/i
const PNL_KEY = /(pnl|p_and_l|profit|loss|m2m|mtm|change|chg)/i
const QUANTITY_KEY = /(quantity|qty|lots|size|volume|count|slices|freeze)/i
const TIME_KEY = /(time|timestamp|_at$|date)/i
const PERCENT_KEY = /(percent|pct|_p$)/i

/** Formats a single field the way its name implies. Non-numeric values are passed
 *  through untouched, which is what keeps "price_type: MARKET" readable. */
export function formatFieldValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return EMPTY
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (typeof value === "object") return JSON.stringify(value)
  if (!isNumericValue(value)) {
    const text = String(value)
    if (TIME_KEY.test(key) && toDate(text)) return formatDateTime(text)
    return text || EMPTY
  }
  if (TIME_KEY.test(key) && !QUANTITY_KEY.test(key)) {
    const stamp = toDate(value)
    if (stamp && Math.abs(toNumber(value) ?? 0) > 1e8) return formatDateTime(value)
  }
  if (PERCENT_KEY.test(key)) return formatPercent(value)
  if (PNL_KEY.test(key)) return formatSignedCurrency(value)
  if (MONEY_KEY.test(key)) return formatCurrency(value)
  if (QUANTITY_KEY.test(key)) return formatQuantity(value)
  return formatNumber(value, Number.isInteger(toNumber(value)) ? 0 : 2)
}

export function isPnlKey(key: string): boolean {
  return PNL_KEY.test(key)
}

/** Renders any value as a single line, for argument rows and table cells. */
export function stringify(value: unknown): string {
  if (value === null || value === undefined) return EMPTY
  if (typeof value === "string") return value || EMPTY
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (typeof value === "number") return formatNumber(value, Number.isInteger(value) ? 0 : 2)
  return JSON.stringify(value)
}
