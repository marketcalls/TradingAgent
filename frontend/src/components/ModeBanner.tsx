/** Always-visible analyzer mode indicator.
 *
 * Red means every order placed from this session is real. The value is polled
 * rather than pushed because the mode can also be changed from the OpenAlgo web
 * app, and a banner that only updates on chat activity would lie for minutes.
 */

import { useEffect, useState } from "react"
import { AlertTriangle, Loader2, ShieldCheck } from "lucide-react"
import { getMode, type ModeInfo, type TradingMode } from "../lib/api"
import { cn } from "../lib/format"

const POLL_MS = 15000

interface ModeBannerProps {
  onMode?: (info: ModeInfo) => void
}

const LABELS: Record<TradingMode, string> = {
  analyze: "ANALYZE",
  live: "LIVE",
  unknown: "MODE UNKNOWN"
}

export default function ModeBanner({ onMode }: ModeBannerProps) {
  const [info, setInfo] = useState<ModeInfo | null>(null)

  useEffect(() => {
    let active = true
    const poll = async () => {
      const next = await getMode()
      if (!active) return
      setInfo(next)
      onMode?.(next)
    }
    void poll()
    const timer = window.setInterval(() => void poll(), POLL_MS)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [onMode])

  const mode: TradingMode = info?.mode ?? "unknown"
  const tone =
    mode === "live"
      ? "border-danger-border bg-danger-soft text-danger"
      : mode === "analyze"
        ? "border-success-border bg-success-soft text-success"
        : "border-warn-border bg-warn-soft text-warn"

  return (
    <div className={cn("flex items-center gap-2 rounded-lg border px-2.5 py-1", tone)}>
      {!info ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : mode === "analyze" ? (
        <ShieldCheck className="h-3.5 w-3.5" />
      ) : (
        <AlertTriangle className="h-3.5 w-3.5" />
      )}
      <span className="text-xs font-semibold tracking-wide">{LABELS[mode]}</span>
      <span className="text-xs text-muted-foreground">
        {mode === "live"
          ? "orders are real"
          : mode === "analyze"
            ? "orders are simulated"
            : info?.note
              ? info.note
              : "checking the broker connection"}
      </span>
      {info?.broker ? <span className="text-xs text-muted-foreground">{info.broker}</span> : null}
    </div>
  )
}
