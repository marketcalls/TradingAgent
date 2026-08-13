"""One response shape for every tool, and safe serialization.

Rules learned from the reference implementation and confirmed against agno 2.8.7:
  - Agno never truncates a tool result and calls str() on the return value, so every
    tool returns a JSON string that this module has already size-capped.
  - A falsy return becomes an EMPTY tool message, which derails the model. Every path
    here returns a non-empty string.
  - NaN is not valid JSON. Everything goes through _clean() before json.dumps.

OpenAlgo itself is not uniform: most resources return {status, data}, order writes
return a flat {status, orderid}, batch writes return {status, results: [...]}, and a
few endpoints return CSV or plain text. envelope() collapses all of that.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_TOOL_CHARS = 12_000


def _clean(obj: Any) -> Any:
    """Recursively replace NaN/Infinity with None so json.dumps produces valid JSON."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def to_json(payload: Any, limit: int = MAX_TOOL_CHARS) -> str:
    """Serialize and hard-cap. Never returns an empty or falsy string."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(_clean(payload), default=str, separators=(",", ":"),
                          ensure_ascii=False)
    if not text:
        return '{"ok":false,"error":"no_data"}'
    if len(text) > limit:
        # Cutting mid-string would hand the model malformed JSON, so the overflow is
        # reported as a well-formed object instead. A tool hitting this is a design
        # problem in that tool: shape the payload down before it gets here.
        dropped = len(text) - limit
        return json.dumps({
            "ok": True,
            "truncated": True,
            "dropped_chars": dropped,
            "note": f"Result exceeded {limit} characters and was cut. Narrow the "
                    "request (filter, or ask for fewer items) to see all of it.",
            "partial_text": text[:limit],
        }, ensure_ascii=False)
    return text


def ok(data: Any, source: str, **extra: Any) -> dict:
    return {"ok": True, "source": source, "data": data, **extra}


def err(message: str, source: str, kind: str | None = None, **extra: Any) -> dict:
    return {"ok": False, "source": source, "error": message, "kind": kind, **extra}


def is_success(raw: Any) -> bool:
    return isinstance(raw, dict) and str(raw.get("status", "")).lower() == "success"


def envelope(raw: Any, source: str) -> dict:
    """Collapse any OpenAlgo response into {ok, source, data, error, mode}.

    Handles the four shapes the API actually uses:
      {status, data}              most reads
      {status, orderid}           order writes
      {status, results: [...]}    basket, split, options multi-leg
      {status, message}           errors and simple acknowledgements
    """
    if raw is None:
        return err("empty response", source)

    if not isinstance(raw, dict):
        # instruments/history return a DataFrame on success; callers convert first.
        return ok(raw, source)

    mode = raw.get("mode")
    status = str(raw.get("status", "")).lower()

    if status and status != "success":
        message = raw.get("message") or raw.get("error") or "request failed"
        return err(str(message), source, kind=raw.get("error_type"), mode=mode)

    if "data" in raw:
        payload = raw["data"]
    elif "results" in raw:
        payload = {"results": raw["results"],
                   **{k: v for k, v in raw.items()
                      if k not in ("status", "results", "mode")}}
    else:
        payload = {k: v for k, v in raw.items() if k not in ("status", "mode")}

    out = ok(payload, source)
    if mode is not None:
        out["mode"] = mode
    return out


def frame_summary(df, last_n: int = 5) -> dict:
    """Compact a candle DataFrame into something worth sending to a model.

    Returning 576 raw candles is both useless and expensive. Compute server-side,
    send conclusions plus a small tail.
    """
    if df is None or len(df) == 0:
        return {"bars": 0}
    cols = [c for c in ("open", "high", "low", "close", "volume", "oi") if c in df.columns]
    tail = df.tail(last_n)[cols]
    out = {
        "bars": int(len(df)),
        "first_timestamp": str(df.index[0]),
        "last_timestamp": str(df.index[-1]),
        "columns": cols,
        "last_close": float(df["close"].iloc[-1]) if "close" in df else None,
        "period_high": float(df["high"].max()) if "high" in df else None,
        "period_low": float(df["low"].min()) if "low" in df else None,
        "recent": [
            {"timestamp": str(idx), **{c: (None if _isnan(row[c]) else float(row[c]))
                                       for c in cols}}
            for idx, row in tail.iterrows()
        ],
    }
    if "close" in df and len(df) > 1:
        first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        if first:
            out["period_change_pct"] = round((last - first) / first * 100, 2)
    return out


def _isnan(v: Any) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


# --- Output hygiene ---------------------------------------------------------
#
# Model output goes straight into the chat, so it is normalised on the way out rather
# than merely discouraged in the prompt. Instructions are layer 1 and unreliable; this
# is deterministic. Three things get cleaned:
#
#   - typographic dashes and quotes, which look wrong in a plain terminal-styled UI and
#     are impossible to type back into a search box,
#   - emoji and pictographs, which the project bans everywhere,
#   - CJK characters, which for an Indian trading agent only ever appear as a defect.
#
# Currency symbols, accents and the rupee sign are deliberately left alone.

_PUNCTUATION_MAP = {
    "—": "-",   # em dash
    "–": "-",   # en dash
    "‒": "-",   # figure dash
    "―": "-",   # horizontal bar
    "−": "-",   # minus sign
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...",
    " ": " ",   # non-breaking space
    " ": " ", " ": " ", "​": "",
}
_PUNCTUATION_RE = re.compile("|".join(map(re.escape, _PUNCTUATION_MAP)))

_STRIP_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # emoji, pictographs, symbols
    "\U0001F000-\U0001F2FF"
    "☀-➿"           # misc symbols and dingbats
    "️⃣"            # variation selector, keycap
    "　-〿"           # CJK punctuation
    "一-鿿"           # CJK unified ideographs
    "぀-ヿ"           # hiragana, katakana
    "가-힯"           # hangul
    "]+"
)


def sanitize_text(text: str) -> str:
    """Normalise a chunk of model output. Safe to call per streaming delta."""
    if not text:
        return text
    cleaned = _PUNCTUATION_RE.sub(lambda m: _PUNCTUATION_MAP[m.group()], text)
    return _STRIP_RE.sub("", cleaned)
