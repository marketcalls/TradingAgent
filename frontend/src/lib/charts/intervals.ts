/** Interval tokens: history depth, live bucket alignment, and the picker's menu.
 *
 * Facts this module is built around:
 *   - The broker answers /intervals with what IT supports, and this deployment
 *     returns {"days":["D"],"hours":["1h"],"minutes":["1m","3m","5m","10m","15m",
 *     "30m"],"months":[],"seconds":[],"weeks":[]}. Three of the six groups come
 *     back empty, so a menu built from the raw payload has to drop empty groups
 *     rather than render three headers with nothing under them.
 *   - resolveInterval throws UnknownIntervalError since 1.4.0, deliberately: the
 *     old silent fall back to 60 seconds drew one-minute bars under a five-minute
 *     label with nothing anywhere saying so. Everything here probes with
 *     tryResolveInterval and answers null rather than guessing, and the picker
 *     validates against the broker's own list before the code reaches the feed.
 *   - A live bar bucket must align to the session, not to the epoch. NSE opens at
 *     09:15 IST, which is 03:45 UTC: 5m and 15m divide that, 10m, 30m and 1h do
 *     not. Epoch-anchored 30m buckets would open at 09:00 and 09:30 while the
 *     broker's open at 09:15 and 09:45, so every live bar would straddle two
 *     history bars. sessionAnchorFor therefore reads the anchor off the last
 *     history bar, which is by definition already on the broker's own grid, and
 *     stays correct across a day boundary for any interval dividing 24 hours.
 *     With no bar at all it answers the NSE open itself rather than the epoch,
 *     for the same reason.
 *   - A calendar code (a month, a quarter) has no seconds at all. February is 29
 *     days in 2024 and a New York month is not a Mumbai month, so intervalSeconds
 *     answers null for one instead of a plausible-looking 2592000.
 *   - An empty broker list is an unavailable list, not a broker with no
 *     intervals. /intervals can fail or answer nothing, and answering "D" to
 *     that discarded the saved interval every time the request stumbled.
 */

import { tryResolveInterval, type Bar } from "openalgo-charts"
import type { IntervalGroups } from "./terminal-api"

const GROUP_KEYS = ["seconds", "minutes", "hours", "days", "weeks", "months"] as const

type GroupKey = (typeof GROUP_KEYS)[number]

/** A fresh, empty set of groups. Never a shared constant: callers mutate menus. */
export function emptyIntervals(): IntervalGroups {
  return { seconds: [], minutes: [], hours: [], days: [], weeks: [], months: [] }
}

function readGroup(source: Record<string, unknown>, key: GroupKey): string[] {
  const value = source[key]
  if (!Array.isArray(value)) return []
  const out: string[] = []
  for (const entry of value) {
    if (typeof entry === "string" && entry !== "" && !out.includes(entry)) out.push(entry)
  }
  return out
}

/** Broker payload to the six groups, dropping anything that is not a string. */
export function normalizeIntervals(raw: unknown): IntervalGroups {
  const groups = emptyIntervals()
  if (!raw || typeof raw !== "object") return groups
  const source = raw as Record<string, unknown>
  for (const key of GROUP_KEYS) groups[key] = readGroup(source, key)
  return groups
}

/** Every code the broker offers, coarsest last, for validation and defaults. */
export function flattenIntervals(groups: IntervalGroups): string[] {
  const out: string[] = []
  for (const key of GROUP_KEYS) {
    for (const code of groups[key]) if (!out.includes(code)) out.push(code)
  }
  return out
}

/** The interval charted when nothing is saved and the broker offers it. */
export const DEFAULT_INTERVAL = "5m"

/** The interval to open on: the saved one if the broker still offers it, else 5m.
 *
 *  With no list to check against, the saved code (or the default) is kept as it
 *  is: forcing D on an empty list threw away the saved interval whenever the
 *  broker's list failed to load, and the terminal then charted daily bars under
 *  a saved 5m preference. The caller says the list was unavailable. */
export function pickInterval(groups: IntervalGroups, saved: string | null): string {
  const all = flattenIntervals(groups)
  if (all.length === 0) return saved ?? DEFAULT_INTERVAL
  if (saved !== null && all.includes(saved)) return saved
  if (all.includes(DEFAULT_INTERVAL)) return DEFAULT_INTERVAL
  if (groups.minutes.length > 0) return groups.minutes[0]
  return all[0]
}

/** Seconds per bar for a fixed-length code, or null for calendar, count and
 *  volume codes, and for anything the registry does not recognise. */
export function intervalSeconds(interval: string): number | null {
  const descriptor = tryResolveInterval(interval)
  if (descriptor === null) return null
  const bucketing = descriptor.bucketing
  return bucketing.mode === "interval" ? bucketing.seconds : null
}

/** True when the code resolves to something the live aggregator can bucket. */
export function isBucketableInterval(interval: string): boolean {
  return intervalSeconds(interval) !== null
}

/** How far back one history page reaches, in days.
 *
 *  Sized by bar count rather than by calendar span: two days of one-second bars
 *  is already 170,000 rows, and a year of them would be a download nobody asked
 *  for on a chart that shows two hundred bars at a time.
 */
export function lookbackDays(interval: string): number {
  const seconds = intervalSeconds(interval)
  if (seconds === null) return 10 * 365
  if (seconds < 60) return 2
  if (seconds <= 60) return 7
  if (seconds <= 300) return 30
  if (seconds <= 900) return 60
  if (seconds <= 1800) return 90
  if (seconds < 86400) return 180
  if (seconds < 604800) return 3 * 365
  return 8 * 365
}

/** The UTC-seconds window for one history page ending at `to`. */
export function historyRange(interval: string, to: number): { from: number; to: number } {
  return { from: to - lookbackDays(interval) * 86400, to }
}

/** 09:15 IST on the epoch day, in UTC seconds (03:45 UTC).
 *
 *  Every interval this deployment offers divides 24 hours, so a bucket grid
 *  stepped from here lands on 09:15 IST on every later day too, where a grid
 *  stepped from the epoch puts the 10m, 30m and 1h buckets at 09:10, 09:00 and
 *  09:00. The broker stamps daily bars at 00:00 UTC (measured against the live
 *  history endpoint), which only the seeded path below follows; the terminal
 *  does not subscribe live without a history bar, so this constant is the
 *  fallback for callers of the feed that do. */
export const NSE_OPEN_ANCHOR_SEC = 3 * 3600 + 45 * 60

/** Bucket alignment for the live builder, in UTC seconds.
 *
 *  The last history bar's own open time. It is already on whatever grid the
 *  broker buckets to, so a tick inside that bar folds into it rather than
 *  opening a second bar for the same minute, and every later bucket steps from
 *  a session-aligned origin instead of from the epoch. With no bar the anchor
 *  is the session open, never 0: an epoch anchor is exactly the misalignment
 *  the seeded path exists to avoid.
 */
export function sessionAnchorFor(lastBar: Bar | null): number {
  return lastBar === null ? NSE_OPEN_ANCHOR_SEC : lastBar.time
}
