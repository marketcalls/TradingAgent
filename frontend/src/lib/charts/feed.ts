/** Market data, through the backend proxy, never the broker.
 *
 * The API key does not exist in the browser. OpenAlgoDataFeed and OpenAlgoWsFeed
 * both take a baseUrl plus an apiKey and put that key in every REST body and in
 * the WebSocket handshake, so neither can be used here at all. What is reusable
 * is everything downstream of the wire: the tolerant timestamp coercion, the
 * subscribe and unsubscribe frame formatters, the inbound message parser, the
 * backoff schedule and the candle builder. This file is the thin adapter that
 * puts those on top of /api/oa, and it sends no apikey field: the proxy injects
 * one server-side.
 *
 * Facts the transport is built around, every one measured against the live
 * OpenAlgo build (Zerodha) behind the relay, most recently out of hours on
 * 2026-09-02:
 *
 *   - History comes back as {"status":"success","data":[{timestamp,open,high,
 *     low,close,volume,oi}]} with timestamp in EPOCH SECONDS. Bar.time is UTC
 *     seconds, so the two agree, but rowTimeToUtcSeconds is still used because it
 *     is the coercion the library's own adapter uses and it also handles the
 *     epoch-millisecond and IST-string forms other OpenAlgo builds emit.
 *   - start_date and end_date are IST "YYYY-MM-DD" strings while from and to are
 *     UTC seconds, and the two disagree at every date boundary: 2026-09-01 00:30
 *     IST is 2026-08-31 19:00 UTC. The request is therefore widened by a day at
 *     each end and the response filtered back to the window actually asked for,
 *     rather than trusting the conversion to land on the same day.
 *   - SUBSCRIBE QUOTE (mode 2) FOR ANYTHING TRADEABLE, LTP for an index. A Quote
 *     frame carries ltp, the cumulative day volume, a timestamp in epoch
 *     milliseconds and the last traded quantity (last_trade_quantity in the
 *     protocol document, last_quantity from the Zerodha adapter). Depth carries
 *     bids, asks and ltp and no traded quantity at all, so while this file
 *     subscribed Depth every bar opened after page load read 0 volume. This is
 *     still ONE subscription per symbol: openalgo issue #1664 is about holding
 *     two modes on one symbol at once (Depth overwrote LTP and the LTP stream
 *     stopped), not about which mode. Nothing on the chart reads bid or ask.
 *   - Bars are built in the builder's day-delta mode: a bar's volume is the
 *     difference between cumulative readings, so it survives a missed tick, and
 *     the seeded history bar keeps its own volume because the baseline is
 *     measured from it (seed(bar, cumSoFar) sets it to cumSoFar - bar.volume).
 *     A day-delta builder with NO baseline hands the first same-bar tick's whole
 *     cumulative reading to that bar as its volume, so the baseline is always
 *     established before a tick is fed, from the first frame that carries one.
 *   - THE FIRST FRAME AFTER A SUBSCRIBE IS A SNAPSHOT, AND IT IS STAMPED NOW.
 *     OpenAlgo answers a subscribe with the last known tick inside a second at
 *     any hour. Measured at 01:58 IST, the frame carried the previous session's
 *     close, the day's volume, and a timestamp of 01:58 IST: the wall clock,
 *     not the last trade. Bucketing it opened a phantom bar at the current time
 *     on every symbol or interval flick, and reported it to the analyst as
 *     lastTime. So the first frame after every subscribe and resubscribe is
 *     price only, whatever it is stamped, and its cumulative volume seeds the
 *     day-delta baseline. A later frame stamped at or before the forming bar's
 *     open is a snapshot of the past and is price only too; a later frame with
 *     no timestamp at all is bucketed at the browser clock.
 *   - The builder is seeded from the last history bar. History ends INSIDE the
 *     forming bucket, so an unseeded builder opens a second bar for the same
 *     bucket: wrong open, volume restarted at zero, and two entries at one time,
 *     of which the data layer silently keeps the last. That is the red candle
 *     under a live green one.
 *   - An error frame names its cause in `code`. The relay answers a refused or
 *     malformed request with {type:"error",status:"error",code,message} and
 *     uses the very same shape for UPSTREAM_UNAVAILABLE, so the frame's type
 *     cannot tell a refused key from a broker that is down for a minute. Only
 *     AUTHENTICATION_ERROR, UPSTREAM_AUTH_REJECTED and OPENALGO_KEY_MISSING end
 *     the session; every other error is reported once and the reconnect loop
 *     keeps its backoff.
 *   - Every request carries a deadline: 15 seconds for history, 8 for the rest.
 *     The page runs analyst commands on one promise chain, and a stalled proxy
 *     request used to hang every later command behind it for the session.
 *   - The socket is the only tick source today. There is no REST quote polling
 *     while it is down: the last price stays where the last tick left it and the
 *     feed indicator says so.
 */

import {
  CandleBuilder,
  backoffDelayMs,
  formatSubscribe,
  formatUnsubscribe,
  parseMessage,
  rowTimeToUtcSeconds,
  utcSecondsToIstDateString,
  type Bar,
  type BarsRequest,
  type DataFeed,
  type LtpEvent,
  type MarketDepth,
  type UnsubscribeFn,
  type WsMode
} from "openalgo-charts"
import { JSON_HEADERS, messageOf } from "../api"
import { intervalSeconds, normalizeIntervals, sessionAnchorFor } from "./intervals"
import type { IntervalGroups, SymbolRow, WsState } from "./terminal-api"

const OA_BASE = "/api/oa"

const DAY_SECONDS = 86400

/** History is the one request that can legitimately take a while: a year of
 *  daily bars is a broker round trip on their side too. */
const HISTORY_TIMEOUT_MS = 15000
const REQUEST_TIMEOUT_MS = 8000

/** Book depth requested for a depth readout. Five is what every adapter
 *  streams; more is negotiable per broker and buys nothing on a chart. */
const DEPTH_LEVEL = 5

/** Error codes that end the session. Retrying a refused key on a timer is how a
 *  key gets rate limited, so these stop the reconnect loop; everything else is
 *  about one request or one outage and is retried with backoff. */
const FATAL_ERROR_CODES = new Set([
  "AUTHENTICATION_ERROR",
  "UPSTREAM_AUTH_REJECTED",
  "OPENALGO_KEY_MISSING"
])

/** Exchanges that quote but do not trade, so they have no volume and no order
 *  book. They take LTP; a Quote subscription for one carries nothing extra. */
export const QUOTE_ONLY_EXCHANGES = new Set([
  "NSE_INDEX",
  "BSE_INDEX",
  "MCX_INDEX",
  "GLOBAL_INDEX"
])

export function isQuoteOnly(exchange: string): boolean {
  return QUOTE_ONLY_EXCHANGES.has(exchange.toUpperCase())
}

export function nowSec(): number {
  return Math.floor(Date.now() / 1000)
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (text === "") return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

/** fetch with a deadline, and a timeout reported in words rather than as the
 *  DOMException's own text. */
async function fetchWithDeadline(
  input: string,
  init: RequestInit,
  timeoutMs: number
): Promise<Response> {
  try {
    return await fetch(input, { ...init, signal: AbortSignal.timeout(timeoutMs) })
  } catch (error) {
    if ((error as { name?: unknown }).name === "TimeoutError") {
      throw new Error(`the proxy did not answer within ${Math.round(timeoutMs / 1000)} seconds`)
    }
    throw error
  }
}

async function post<T>(
  path: string,
  body: Record<string, unknown>,
  timeoutMs = REQUEST_TIMEOUT_MS
): Promise<T> {
  const response = await fetchWithDeadline(
    `${OA_BASE}/${path}`,
    { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) },
    timeoutMs
  )
  const payload = await readBody(response)
  if (!response.ok) {
    throw new Error(messageOf(payload, `the request failed with status ${response.status}`))
  }
  // OpenAlgo answers a rejected call with HTTP 200 and status "error", so the
  // status code alone does not say whether the request worked.
  if (payload !== null && typeof payload === "object") {
    if ((payload as { status?: unknown }).status === "error") {
      throw new Error(messageOf(payload, "the broker rejected the request"))
    }
  }
  return payload as T
}

function dataOf(payload: unknown): unknown {
  if (payload === null || typeof payload !== "object") return null
  return (payload as { data?: unknown }).data ?? null
}

function toNumber(value: unknown): number {
  if (typeof value === "number") return value
  if (typeof value === "string") {
    const parsed = Number(value.replace(/,/g, ""))
    return Number.isFinite(parsed) ? parsed : Number.NaN
  }
  return Number.NaN
}

function toText(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined
}

/** What the browser is allowed to know about the proxy. */
export interface OaConfig {
  wsPath: string
  hostReachable: boolean
}

export async function getOaConfig(): Promise<OaConfig> {
  const response = await fetchWithDeadline(
    `${OA_BASE}/config`,
    { headers: JSON_HEADERS },
    REQUEST_TIMEOUT_MS
  )
  const payload = await readBody(response)
  if (!response.ok) {
    throw new Error(messageOf(payload, `the request failed with status ${response.status}`))
  }
  const record = (payload ?? {}) as { ws_path?: unknown; host_reachable?: unknown }
  return {
    wsPath: typeof record.ws_path === "string" ? record.ws_path : `${OA_BASE}/ws`,
    hostReachable: record.host_reachable !== false
  }
}

export async function getIntervals(): Promise<IntervalGroups> {
  const payload = await post<unknown>("intervals", {})
  return normalizeIntervals(dataOf(payload))
}

export async function searchSymbols(query: string, exchange?: string): Promise<SymbolRow[]> {
  const body: Record<string, unknown> = { query }
  if (exchange !== undefined && exchange !== "") body.exchange = exchange
  const payload = await post<unknown>("search", body)
  const rows = dataOf(payload)
  if (!Array.isArray(rows)) return []
  const out: SymbolRow[] = []
  for (const row of rows) {
    if (row === null || typeof row !== "object") continue
    const record = row as Record<string, unknown>
    const symbol = toText(record.symbol)
    const venue = toText(record.exchange)
    if (symbol === undefined || venue === undefined) continue
    out.push({
      symbol,
      exchange: venue,
      name: toText(record.name),
      lotsize: typeof record.lotsize === "number" ? record.lotsize : toText(record.lotsize)
    })
  }
  return out
}

/** Instrument metadata. tickSize decides how many decimals every price renders
 *  with, so it travels with the symbol rather than being guessed per pane. */
export interface SymbolInfo {
  symbol: string
  exchange: string
  name?: string
  lotsize?: number
  tickSize?: number
  quoteOnly: boolean
}

export async function getSymbolInfo(symbol: string, exchange: string): Promise<SymbolInfo> {
  const fallback: SymbolInfo = { symbol, exchange, quoteOnly: isQuoteOnly(exchange) }
  try {
    const payload = await post<unknown>("symbol", { symbol, exchange })
    const record = dataOf(payload)
    if (record === null || typeof record !== "object") return fallback
    const fields = record as Record<string, unknown>
    const lot = toNumber(fields.lotsize)
    const tick = toNumber(fields.tick_size)
    return {
      symbol: toText(fields.symbol) ?? symbol,
      exchange: toText(fields.exchange) ?? exchange,
      name: toText(fields.name),
      lotsize: Number.isFinite(lot) && lot > 0 ? lot : undefined,
      tickSize: Number.isFinite(tick) && tick > 0 ? tick : undefined,
      quoteOnly: isQuoteOnly(toText(fields.exchange) ?? exchange)
    }
  } catch {
    // Metadata is a nicety: a chart with default price precision still charts.
    return fallback
  }
}

/** One quote reading. Used for the previous close on the readout. */
export interface Quote {
  ltp: number | null
  prevClose: number | null
  /** Cumulative day volume, the same reading a Quote frame carries. */
  volume: number | null
}

export async function getQuote(symbol: string, exchange: string): Promise<Quote> {
  const payload = await post<unknown>("quotes", { symbol, exchange })
  const record = dataOf(payload)
  if (record === null || typeof record !== "object") {
    return { ltp: null, prevClose: null, volume: null }
  }
  const fields = record as Record<string, unknown>
  const read = (value: unknown): number | null => {
    const parsed = toNumber(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return {
    ltp: read(fields.ltp),
    prevClose: read(fields.prev_close),
    volume: read(fields.volume)
  }
}

/** Absolute socket URL for a path the proxy named. */
export function socketUrl(path: string): string {
  if (/^wss?:\/\//i.test(path)) return path
  const secure = window.location.protocol === "https:"
  const prefix = path.startsWith("/") ? path : `/${path}`
  return `${secure ? "wss:" : "ws:"}//${window.location.host}${prefix}`
}

type DepthListener = (symbol: string, exchange: string, depth: MarketDepth) => void

/** A transient error frame, with the relay's or the server's own code. */
export type SocketErrorListener = (code: string, message: string) => void

interface Subscription {
  mode: WsMode
  symbol: string
  exchange: string
  depthLevel?: number
  refs: number
}

function subKey(mode: WsMode, symbol: string, exchange: string): string {
  return `${mode}:${symbol}:${exchange}`
}

/** The proxy socket.
 *
 *  No handshake frame is sent: the proxy authenticates upstream itself, and a
 *  browser that could send an api_key would be a browser that has one. A refusal
 *  is recognised by its code and nothing else. The relay's UPSTREAM_UNAVAILABLE
 *  and a bad subscribe arrive with the same type "error" as a refused key, and
 *  keying on the type marked the socket refused, permanently, on the first
 *  morning the broker feed was late: three codes end the session, every other
 *  error frame is reported once and the reconnect loop keeps its backoff.
 */
export class OaSocket {
  private socket: WebSocket | null = null
  private epoch = 0
  private attempts = 0
  private retry: ReturnType<typeof setTimeout> | null = null
  private closedByUser = false
  private refused = false
  private sawData = false
  private state: WsState = "closed"
  private readonly subs = new Map<string, Subscription>()
  private readonly ltpCbs = new Set<(event: LtpEvent) => void>()
  private readonly depthCbs = new Set<DepthListener>()
  private readonly stateCbs = new Set<(state: WsState) => void>()
  private readonly errorCbs = new Set<SocketErrorListener>()
  private readonly resubscribeCbs = new Set<() => void>()
  /** Transient error codes already reported since data last flowed. The relay
   *  refuses and closes on every retry while its upstream is down, so without
   *  this one outage would toast on every backoff step. */
  private readonly noticed = new Set<string>()

  constructor(private readonly url: string) {}

  currentState(): WsState {
    return this.state
  }

  onState(callback: (state: WsState) => void): () => void {
    this.stateCbs.add(callback)
    return () => {
      this.stateCbs.delete(callback)
    }
  }

  onLtp(callback: (event: LtpEvent) => void): () => void {
    this.ltpCbs.add(callback)
    return () => {
      this.ltpCbs.delete(callback)
    }
  }

  onDepth(
    callback: (symbol: string, exchange: string, depth: MarketDepth) => void
  ): () => void {
    this.depthCbs.add(callback)
    return () => {
      this.depthCbs.delete(callback)
    }
  }

  /** A transient error frame. Fired once per code until data flows again. */
  onError(callback: SocketErrorListener): () => void {
    this.errorCbs.add(callback)
    return () => {
      this.errorCbs.delete(callback)
    }
  }

  /** The subscriptions were (or are about to be) replayed: this socket
   *  reconnected, or the relay lost its upstream and is replaying them there.
   *  The next market frame per symbol is a snapshot, not a trade. */
  onResubscribe(callback: () => void): () => void {
    this.resubscribeCbs.add(callback)
    return () => {
      this.resubscribeCbs.delete(callback)
    }
  }

  private setState(next: WsState): void {
    if (this.state === next) return
    this.state = next
    for (const callback of this.stateCbs) {
      try {
        callback(next)
      } catch {
        // One bad listener must not take the socket down with it.
      }
    }
  }

  private notifyResubscribe(): void {
    for (const callback of this.resubscribeCbs) {
      try {
        callback()
      } catch {
        // As above.
      }
    }
  }

  connect(): void {
    if (this.refused || this.closedByUser) return
    if (this.socket !== null) return
    const epoch = ++this.epoch
    this.sawData = false
    this.setState(this.attempts === 0 ? "connecting" : "reconnecting")
    let socket: WebSocket
    try {
      socket = new WebSocket(this.url)
    } catch {
      this.setState("error")
      this.scheduleRetry()
      return
    }
    this.socket = socket
    socket.onopen = () => {
      if (epoch !== this.epoch) return
      const reconnected = this.attempts > 0
      this.attempts = 0
      this.setState("open")
      // Desired state, replayed in full. A queue of frames can hold two entries
      // for one key across a long outage; a map cannot.
      for (const sub of this.subs.values()) this.sendSubscribe(sub)
      // Only a reconnect is announced: a subscription made before the first
      // open is armed for its snapshot already, and announcing that open too
      // would cost a history reconcile right behind the history just loaded.
      if (reconnected) this.notifyResubscribe()
    }
    socket.onmessage = (event: MessageEvent) => {
      if (epoch !== this.epoch) return
      this.handle(event.data)
    }
    socket.onerror = () => {
      if (epoch !== this.epoch) return
      this.setState("error")
    }
    socket.onclose = () => {
      if (epoch !== this.epoch) return
      this.socket = null
      if (this.closedByUser || this.refused) {
        this.setState(this.refused ? "auth failed" : "closed")
        return
      }
      this.scheduleRetry()
    }
  }

  private scheduleRetry(): void {
    if (this.retry !== null) return
    const delay = backoffDelayMs(this.attempts, {
      baseDelayMs: 1000,
      maxDelayMs: 30000,
      jitter: true
    })
    this.attempts += 1
    this.setState("reconnecting")
    this.retry = setTimeout(() => {
      this.retry = null
      this.socket = null
      this.connect()
    }, delay)
  }

  private send(frame: string): void {
    const socket = this.socket
    if (socket === null || socket.readyState !== WebSocket.OPEN) return
    try {
      socket.send(frame)
    } catch {
      // A send onto a socket the browser has already given up on. The close
      // handler is about to run and replay everything anyway.
    }
  }

  private sendSubscribe(sub: Subscription): void {
    this.send(formatSubscribe(sub.mode, sub.symbol, sub.exchange, sub.depthLevel))
  }

  private handle(raw: unknown): void {
    if (typeof raw !== "string") return
    if (raw === "ping") {
      this.send(JSON.stringify({ action: "pong" }))
      return
    }
    let parsed: unknown
    try {
      parsed = JSON.parse(raw)
    } catch {
      return
    }
    if (parsed === null || typeof parsed !== "object") return
    if ((parsed as { type?: unknown }).type === "ping") {
      this.send(JSON.stringify({ action: "pong" }))
      return
    }
    const message = parseMessage(parsed)
    if (message === null) {
      this.handleControl(parsed as Record<string, unknown>)
      return
    }
    if (!this.sawData) {
      this.sawData = true
      // Data is recovery: the next outage is news again.
      this.noticed.clear()
    }
    if (message.kind === "ltp") {
      for (const callback of this.ltpCbs) callback(message.event)
      return
    }
    for (const callback of this.depthCbs) {
      callback(message.symbol, message.exchange, message.depth)
    }
  }

  /** A frame that is not market data: an ack, a relay notice, or an error. */
  private handleControl(frame: Record<string, unknown>): void {
    const type = typeof frame.type === "string" ? frame.type.toLowerCase() : ""
    const status = typeof frame.status === "string" ? frame.status.toLowerCase() : ""
    if (type === "proxy" && status === "reconnecting") {
      // The relay lost its upstream and will replay this socket's subscriptions
      // when it has one again. This socket never closes, so the frames that
      // follow open with a snapshot exactly as a fresh subscribe does.
      this.notifyResubscribe()
      return
    }
    if (type !== "error" && status !== "error") return
    const code = typeof frame.code === "string" ? frame.code : ""
    const message =
      typeof frame.message === "string" && frame.message !== ""
        ? frame.message
        : "the market data feed reported an error"
    if (FATAL_ERROR_CODES.has(code)) {
      // At any time, not only before the first tick: a key revoked mid-session
      // comes back as UPSTREAM_AUTH_REJECTED after the relay's own reconnect.
      this.refused = true
      this.setState("auth failed")
      this.socket?.close()
      return
    }
    if (this.noticed.has(code)) return
    this.noticed.add(code)
    for (const callback of this.errorCbs) {
      try {
        callback(code, message)
      } catch {
        // As above.
      }
    }
  }

  /** Take a share in one stream. Reference counted, because the chart and a
   *  depth readout can want the same book and neither may cut the other off. */
  subscribe(mode: WsMode, symbol: string, exchange: string, depthLevel?: number): void {
    const key = subKey(mode, symbol, exchange)
    const existing = this.subs.get(key)
    if (existing !== undefined) {
      existing.refs += 1
      // Depth is negotiated upward only: shrinking a book under a consumer that
      // is still reading it is the failure the count exists to prevent.
      if (depthLevel !== undefined && (existing.depthLevel ?? 0) < depthLevel) {
        existing.depthLevel = depthLevel
        this.sendSubscribe(existing)
      }
      return
    }
    const sub: Subscription = { mode, symbol, exchange, depthLevel, refs: 1 }
    this.subs.set(key, sub)
    this.sendSubscribe(sub)
  }

  unsubscribe(mode: WsMode, symbol: string, exchange: string): void {
    const key = subKey(mode, symbol, exchange)
    const existing = this.subs.get(key)
    if (existing === undefined) return
    existing.refs -= 1
    if (existing.refs > 0) return
    this.subs.delete(key)
    this.send(formatUnsubscribe(mode, symbol, exchange))
  }

  close(): void {
    this.closedByUser = true
    if (this.retry !== null) {
      clearTimeout(this.retry)
      this.retry = null
    }
    this.epoch += 1
    this.subs.clear()
    const socket = this.socket
    this.socket = null
    if (socket !== null) {
      try {
        socket.close()
      } catch {
        // Already closing.
      }
    }
    this.setState("closed")
  }
}

/** Map one history payload to bars, clipped to the window actually requested. */
function mapBars(rows: unknown, from: number, to: number): Bar[] {
  if (!Array.isArray(rows)) return []
  const byTime = new Map<number, Bar>()
  for (const row of rows) {
    if (row === null || typeof row !== "object") continue
    const record = row as Record<string, unknown>
    const stamp = record.timestamp ?? record.time
    if (stamp === undefined || stamp === null) continue
    if (typeof stamp !== "number" && typeof stamp !== "string") continue
    const time = rowTimeToUtcSeconds(stamp)
    if (!Number.isFinite(time) || time < from || time > to) continue
    const open = toNumber(record.open)
    const high = toNumber(record.high)
    const low = toNumber(record.low)
    const close = toNumber(record.close)
    if (!Number.isFinite(open) || !Number.isFinite(high)) continue
    if (!Number.isFinite(low) || !Number.isFinite(close)) continue
    const volume = toNumber(record.volume)
    // Last one wins, which is what the data layer would do anyway; doing it here
    // means the feed never hands a duplicate time downstream at all.
    byTime.set(time, {
      time,
      open,
      high,
      low,
      close,
      volume: Number.isFinite(volume) ? volume : 0
    })
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

/** Options subscribeBars accepts beyond the DataFeed contract's two arguments.
 *  withBarCache forwards extra arguments untouched, so this survives the hop. */
export interface SubscribeBarsOptions {
  /** The last history bar. Seeds the builder so the first tick continues its
   *  bucket instead of opening a second bar inside it. */
  seedFrom?: Bar
  /** A frame that carries a price but must not touch a bar: the subscribe-time
   *  snapshot, or a tick stamped at or before the forming bar's open. */
  onSnapshot?: (price: number) => void
  /** The stream restarted (this socket reconnected, or the relay resubscribed
   *  upstream) and its snapshot has arrived. Whatever closed during the gap was
   *  never built from ticks, so the caller re-asks history at once. */
  onResync?: () => void
}

/** The live end of one subscription, for a caller that has to correct the bar
 *  the builder is holding. */
interface LiveHandle {
  reseed(bar: Bar): void
}

function liveKey(req: BarsRequest): string {
  return `${req.symbol}:${req.exchange}:${req.interval}`
}

function cumulativeOf(event: LtpEvent): number | null {
  const volume = event.volume
  return typeof volume === "number" && Number.isFinite(volume) && volume >= 0 ? volume : null
}

/** History over /api/oa/history plus live bars off the proxy socket. */
export class ProxyDataFeed implements DataFeed {
  private readonly handles = new Map<string, LiveHandle>()

  constructor(private readonly socket: OaSocket) {}

  async getBars(req: BarsRequest): Promise<Bar[]> {
    const { from, to } = req
    if (from === undefined || to === undefined) {
      throw new Error("a history request needs both a from and a to")
    }
    const payload = await post<unknown>(
      "history",
      {
        symbol: req.symbol,
        exchange: req.exchange,
        interval: req.interval,
        // Widened by a day at each end: the dates are IST wall clock and the
        // bounds are UTC seconds, so the two disagree across every midnight.
        start_date: utcSecondsToIstDateString(from - DAY_SECONDS),
        end_date: utcSecondsToIstDateString(to + DAY_SECONDS)
      },
      HISTORY_TIMEOUT_MS
    )
    return mapBars(dataOf(payload), from, to)
  }

  subscribeBars(
    req: BarsRequest,
    onBar: (bar: Bar) => void,
    opts?: SubscribeBarsOptions
  ): UnsubscribeFn {
    const seconds = intervalSeconds(req.interval)
    const seed = opts?.seedFrom
    const quoteOnly = isQuoteOnly(req.exchange)
    // Quote for anything that trades: it is the one mode that carries the
    // cumulative day volume the day-delta builder diffs. An index has no volume
    // to carry, so it takes the lighter LTP stream and ltq-sum, which sums the
    // nothing it is given to 0 rather than diffing an absent reading.
    const mode: WsMode = quoteOnly ? "LTP" : "Quote"
    const builder =
      seconds === null
        ? null
        : new CandleBuilder({
            intervalSec: seconds,
            volumeMode: quoteOnly ? "ltq-sum" : "day-delta",
            lateTickPolicy: "foldIntoBar",
            sessionAnchorSec: sessionAnchorFor(seed ?? null)
          })
    if (builder !== null && seed !== undefined) builder.seed(seed)
    // A code with no fixed length (a registered calendar or tick bar) has no
    // bucket to compute, so its ticks fold into the last history bar and stop
    // there rather than being bucketed by a rule this file invented.
    const holding: Bar | null = builder === null && seed !== undefined ? { ...seed } : null

    // The first frame after every subscribe is the server's snapshot of the
    // last known tick, stamped with the wall clock. It is consumed here: price
    // for the readout, cumulative volume for the baseline, never a bar.
    let awaitingSnapshot = true
    // Set by a resubscribe notice: the snapshot that follows one means the
    // stream restarted, and the caller has a gap to reconcile.
    let rearmed = false
    // The last cumulative day volume seen, so a reseed keeps its baseline and a
    // frame that arrives without one does not zero the bar.
    let lastCum: number | null = null

    const formingOpen = (): number | null => {
      if (builder !== null) return builder.current()?.time ?? null
      return holding?.time ?? null
    }

    /** Establish the day-delta baseline against the bar the builder holds. */
    const baseline = (cum: number): void => {
      lastCum = cum
      if (builder === null) return
      const current = builder.current()
      if (current !== null) builder.seed(current, cum)
    }

    const push = (event: LtpEvent): void => {
      const price = event.ltp
      if (!Number.isFinite(price) || price <= 0) return
      const cum = cumulativeOf(event)
      const stamped = event.timeSec > 0
      const time = stamped ? event.timeSec : nowSec()
      if (awaitingSnapshot) {
        awaitingSnapshot = false
        if (cum !== null && !quoteOnly) baseline(cum)
        opts?.onSnapshot?.(price)
        if (rearmed) {
          rearmed = false
          opts?.onResync?.()
        }
        return
      }
      const open = formingOpen()
      if (stamped && open !== null && time <= open) {
        // Stamped at or before the forming bar's open: a reading of the past.
        // It moves the readout and nothing else.
        opts?.onSnapshot?.(price)
        return
      }
      if (builder !== null) {
        // A baseline before the first tick, whatever the snapshot carried: a
        // day-delta builder without one hands the first same-bar reading to the
        // bar as its entire volume.
        if (!quoteOnly && cum !== null && lastCum === null) baseline(cum)
        const update = builder.onTick({
          time,
          price,
          ltq: event.ltq,
          cumDayVolume: cum ?? lastCum ?? undefined
        })
        if (cum !== null) lastCum = cum
        if (update !== null) onBar(update.bar)
        return
      }
      if (holding === null) return
      if (price > holding.high) holding.high = price
      if (price < holding.low) holding.low = price
      holding.close = price
      onBar({ ...holding })
    }

    const mine = (symbol: string, exchange: string): boolean =>
      symbol === req.symbol && (exchange === "" || exchange === req.exchange)

    const offLtp = this.socket.onLtp((event) => {
      if (!mine(event.symbol, event.exchange)) return
      push(event)
    })
    const offResubscribe = this.socket.onResubscribe(() => {
      awaitingSnapshot = true
      rearmed = true
    })

    const key = liveKey(req)
    const handle: LiveHandle = {
      reseed: (bar) => {
        if (builder !== null) {
          // With the cumulative reading kept, the baseline becomes lastCum
          // minus the corrected volume, so the next tick's delta lands on top
          // of the correction rather than on top of the stale volume.
          if (lastCum !== null) builder.seed(bar, lastCum)
          else builder.seed(bar)
        } else if (holding !== null) {
          Object.assign(holding, bar)
        }
      }
    }
    this.handles.set(key, handle)

    this.socket.subscribe(mode, req.symbol, req.exchange)

    return () => {
      offLtp()
      offResubscribe()
      // Only this subscription's own handle: a second subscriber to the same
      // series may have replaced it, and must keep its own.
      if (this.handles.get(key) === handle) this.handles.delete(key)
      this.socket.unsubscribe(mode, req.symbol, req.exchange)
    }
  }

  /** Replace the bar the live builder is holding for this series.
   *
   *  The builder folds every later tick into its own copy of the forming bar,
   *  so a correction written to the caller's array alone is overwritten by the
   *  next tick. Returns false when no subscription is live for the request. */
  reseedForming(req: BarsRequest, bar: Bar): boolean {
    const handle = this.handles.get(liveKey(req))
    if (handle === undefined) return false
    handle.reseed({ ...bar })
    return true
  }

  subscribeDepth(
    req: BarsRequest,
    onDepth: (depth: MarketDepth) => void,
    opts?: { depthLevel?: number }
  ): UnsubscribeFn {
    const off = this.socket.onDepth((symbol, exchange, depth) => {
      if (symbol !== req.symbol) return
      if (exchange !== "" && exchange !== req.exchange) return
      onDepth(depth)
    })
    const level = opts?.depthLevel ?? DEPTH_LEVEL
    this.socket.subscribe("Depth", req.symbol, req.exchange, level)
    return () => {
      off()
      this.socket.unsubscribe("Depth", req.symbol, req.exchange)
    }
  }
}
