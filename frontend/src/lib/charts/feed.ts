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
 * Facts the transport is built around, every one verified against the live
 * OpenAlgo build on 127.0.0.1:5000:
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
 *   - SUBSCRIBE DEPTH FOR ANYTHING TRADEABLE, and read the price off depth.ltp.
 *     Depth looks like order-book machinery and is the obvious thing to skip on a
 *     read-only chart. Skipping it freezes the chart. OpenAlgo's own terminal
 *     shipped a dual LTP+Depth subscribe and it broke on brokers whose adapters
 *     track one mode per symbol: Depth overwrote LTP, the LTP stream stopped, and
 *     the chart froze while depth kept flowing (openalgo issue #1664). One
 *     subscription per symbol, and ltp is a first-class field of every mode-3
 *     payload. Indices have no order book, so the quote-only exchanges take LTP.
 *   - A depth frame carries neither a timestamp nor a last-traded quantity, so
 *     the depth path buckets at the wall clock and the forming bar's volume stays
 *     at whatever history reported for it. A wrong volume would be worse than a
 *     late one.
 *   - The builder is seeded from the last history bar. History ends INSIDE the
 *     forming bucket, so an unseeded builder opens a second bar for the same
 *     bucket: wrong open, volume restarted at zero, and two entries at one time,
 *     of which the data layer silently keeps the last. That is the red candle
 *     under a live green one.
 */

import {
  CandleBuilder,
  backoffDelayMs,
  classifyAuthAck,
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

/** Book depth requested for a tradeable instrument. Five is what every adapter
 *  streams; more is negotiable per broker and buys nothing on a chart. */
const DEPTH_LEVEL = 5

/** Exchanges that quote but do not trade, so they have no order book at all.
 *  Subscribing Depth for one gets no frames and the chart never ticks. */
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

async function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${OA_BASE}/${path}`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body)
  })
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
  const response = await fetch(`${OA_BASE}/config`, { headers: JSON_HEADERS })
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

/** One quote reading. Used for the previous close, and as the fallback price
 *  source while the socket is down. */
export interface Quote {
  ltp: number | null
  prevClose: number | null
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
 *  browser that could send an api_key would be a browser that has one. An auth
 *  refusal relayed by the proxy is still honoured, because retrying a refused
 *  key on a timer is how a key gets rate limited, but only while the connection
 *  has carried no data: after that an error frame is about one request, not
 *  about the session, and tearing the socket down for it would lose the stream.
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
      this.attempts = 0
      this.setState("open")
      // Desired state, replayed in full. A queue of frames can hold two entries
      // for one key across a long outage; a map cannot.
      for (const sub of this.subs.values()) this.sendSubscribe(sub)
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
      if (!this.sawData && classifyAuthAck(parsed) === "failed") {
        this.refused = true
        this.setState("auth failed")
        this.socket?.close()
      }
      return
    }
    this.sawData = true
    if (message.kind === "ltp") {
      for (const callback of this.ltpCbs) callback(message.event)
      return
    }
    for (const callback of this.depthCbs) {
      callback(message.symbol, message.exchange, message.depth)
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
}

/** History over /api/oa/history plus live bars off the proxy socket. */
export class ProxyDataFeed implements DataFeed {
  constructor(private readonly socket: OaSocket) {}

  async getBars(req: BarsRequest): Promise<Bar[]> {
    const { from, to } = req
    if (from === undefined || to === undefined) {
      throw new Error("a history request needs both a from and a to")
    }
    const payload = await post<unknown>("history", {
      symbol: req.symbol,
      exchange: req.exchange,
      interval: req.interval,
      // Widened by a day at each end: the dates are IST wall clock and the
      // bounds are UTC seconds, so the two disagree across every midnight.
      start_date: utcSecondsToIstDateString(from - DAY_SECONDS),
      end_date: utcSecondsToIstDateString(to + DAY_SECONDS)
    })
    return mapBars(dataOf(payload), from, to)
  }

  subscribeBars(
    req: BarsRequest,
    onBar: (bar: Bar) => void,
    opts?: SubscribeBarsOptions
  ): UnsubscribeFn {
    const seconds = intervalSeconds(req.interval)
    const seed = opts?.seedFrom
    const builder =
      seconds === null
        ? null
        : new CandleBuilder({
            intervalSec: seconds,
            volumeMode: "ltq-sum",
            lateTickPolicy: "foldIntoBar",
            sessionAnchorSec: sessionAnchorFor(seed ?? null)
          })
    if (builder !== null && seed !== undefined) builder.seed(seed)
    // A code with no fixed length (a registered calendar or tick bar) has no
    // bucket to compute, so its ticks fold into the last history bar and stop
    // there rather than being bucketed by a rule this file invented.
    const holding: Bar | null = builder === null && seed !== undefined ? { ...seed } : null

    const push = (price: number, ltq: number | undefined, timeSec: number): void => {
      if (!Number.isFinite(price) || price <= 0) return
      if (builder !== null) {
        const update = builder.onTick({ time: timeSec, price, ltq })
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

    const quoteOnly = isQuoteOnly(req.exchange)
    const mode: WsMode = quoteOnly ? "LTP" : "Depth"
    const off =
      quoteOnly
        ? this.socket.onLtp((event) => {
            if (!mine(event.symbol, event.exchange)) return
            push(event.ltp, event.ltq, event.timeSec > 0 ? event.timeSec : nowSec())
          })
        : this.socket.onDepth((symbol, exchange, depth) => {
            if (!mine(symbol, exchange)) return
            // depth.ltp, not bids[0]: the top of a one-sided book is not a
            // trade. Depth frames carry no timestamp and no traded quantity, so
            // the bucket is the browser's clock and the bar's volume stays where
            // history left it. A browser clock skewed by less than one interval
            // changes nothing, because the tick still lands in the bar history
            // seeded; a skew that crosses a bucket boundary opens the next bar
            // early, which is the price of a stream that carries no time.
            push(depth.ltp, undefined, nowSec())
          })
    this.socket.subscribe(mode, req.symbol, req.exchange, quoteOnly ? undefined : DEPTH_LEVEL)

    return () => {
      off()
      this.socket.unsubscribe(mode, req.symbol, req.exchange)
    }
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
