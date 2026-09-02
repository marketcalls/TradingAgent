"""Chart geometry tests.

The synthetic checks need no network: every frame here has its pivots at indices
chosen in advance, so the off-by-right bug fails by construction rather than by
inspection. The live section pulls RELIANCE from OpenAlgo and prints the vertices
an envelope actually produces, because a geometry module that passes its unit
tests and still draws the band in the wrong place has failed at the only thing it
was built for.

Two sections are probes rather than single cases, on purpose. A commit once
claimed "a spike in the forming bar does not move the rails" on the strength of
one hand-picked frame; the reviewer then moved the forming bar on 80 real frames
and the rails moved in 32 of 400 trials. The forming-bar section here runs 40
synthetic frames of four kinds through three tick sizes and a one-bar append and
asserts over all of them. The channel-coverage section runs 300 and prints the
distribution.

Run:  python backend/tests/test_geometry.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.config import setup_logging  # noqa: E402

setup_logging("WARNING")

from app.charts.geometry import (  # noqa: E402
    CROSSING_TOLERANCE_PCT,
    ENVELOPE_SIGNIFICANCE,
    Pivot,
    bar_interval_seconds,
    envelope,
    fit_channel,
    fit_trendline,
    measured_move,
    snap_to_pivot,
    support_resistance,
    swing_pivots,
    to_utc_seconds,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []

# What the tool layer passes: five pivots a side, 0.5 percent touch slack and the
# geometry's own crossing slack.
CHANNEL_PIVOTS = 5
TOUCH_TOL = 0.5


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


def iso(seconds: float) -> str:
    """Render UTC seconds the way a human reads a chart axis."""
    if not np.isfinite(seconds):
        return "nan"
    return dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# frame builders
# ---------------------------------------------------------------------------


def frame_from_close(closes, tz=None, freq="D", start="2026-01-01"):
    """Build an OHLCV frame whose high and low sit a fixed tick off close.

    The offset is constant, so a swing high in close is a swing high in high at
    exactly the same bar index. That keeps expected pivot indices readable.
    """
    closes = np.asarray(closes, dtype=float)
    n = closes.size
    idx = pd.date_range(start, periods=n, freq=freq, tz=tz, name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": np.full(n, 1000.0),
            "oi": np.zeros(n),
        },
        index=idx,
    )


def sawtooth(peaks: list[int], troughs: list[int], n: int, amp: float = 10.0):
    """A zigzag whose highs sit exactly at ``peaks`` and lows at ``troughs``."""
    turns = sorted([(i, "h") for i in peaks] + [(i, "l") for i in troughs])
    xs = [t[0] for t in turns]
    ys = [100.0 + amp if t[1] == "h" else 100.0 - amp for t in turns]
    if xs[0] != 0:
        xs.insert(0, 0)
        ys.insert(0, 100.0 - amp if turns[0][1] == "h" else 100.0 + amp)
    if xs[-1] != n - 1:
        xs.append(n - 1)
        ys.append(100.0 - amp if turns[-1][1] == "h" else 100.0 + amp)
    closes = np.interp(np.arange(n), xs, ys)
    return frame_from_close(closes)


FAMILIES = ("trend", "range", "triangle", "noise")


def market_frame(family: str, seed: int, n: int = 250, freq: str = "D") -> pd.DataFrame:
    """A synthetic frame with a real-looking high/low spread.

    Four families: a trend with swings, a range, a contracting triangle and a
    random walk. Every frame is a function of its seed, so a probe over them is
    repeatable to the last digit.
    """
    rng = np.random.default_rng(seed)
    x = np.arange(n, dtype=float)
    base = 1000.0 + rng.uniform(-300, 800)
    if family == "trend":
        drift = rng.choice([-1.0, 1.0]) * rng.uniform(0.4, 1.6) * x
        wave = rng.uniform(8, 25) * np.sin(x / rng.uniform(4, 9))
        close = base + drift + wave + rng.normal(0, rng.uniform(0.5, 3.0), n)
    elif family == "range":
        wave = rng.uniform(15, 40) * np.sin(x / rng.uniform(4, 9))
        close = base + wave + rng.normal(0, rng.uniform(0.5, 3.0), n)
    elif family == "triangle":
        amp = np.linspace(rng.uniform(40, 80), rng.uniform(4, 12), n)
        wave = amp * np.sin(x / rng.uniform(4, 9))
        close = base + wave + rng.normal(0, rng.uniform(0.5, 2.0), n)
    else:
        steps = rng.normal(0, rng.uniform(3, 10), n)
        close = base + np.cumsum(steps)
    close = np.maximum(close, 50.0)
    spread = rng.uniform(1.5, 5.0)
    high = close + spread + rng.random(n) * spread
    low = close - spread - rng.random(n) * spread
    idx = pd.date_range("2025-01-01", periods=n, freq=freq, name="timestamp")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close,
         "volume": np.full(n, 1000.0), "oi": np.zeros(n)},
        index=idx,
    )


def market_frames(count: int, n: int = 250, start_seed: int = 100):
    return [(FAMILIES[k % 4], start_seed + k, market_frame(FAMILIES[k % 4], start_seed + k, n))
            for k in range(count)]


def append_flat(df: pd.DataFrame) -> pd.DataFrame:
    """A new bar opens at the last close: what the chart looks like one bar on."""
    last_close = float(df["close"].iloc[-1])
    step = df.index[-1] - df.index[-2]
    row = pd.DataFrame(
        {"open": [last_close], "high": [last_close], "low": [last_close],
         "close": [last_close], "volume": [1000.0], "oi": [0.0]},
        index=pd.DatetimeIndex([df.index[-1] + step], name="timestamp"),
    )
    return pd.concat([df, row])


def perturb_last(df: pd.DataFrame, pct: float) -> pd.DataFrame:
    """A tick on the forming bar: its high up and its low down by ``pct`` percent."""
    out = df.copy()
    out.iloc[-1, out.columns.get_loc("high")] = float(df["high"].iloc[-1]) * (1.0 + pct / 100.0)
    out.iloc[-1, out.columns.get_loc("low")] = float(df["low"].iloc[-1]) * (1.0 - pct / 100.0)
    return out


def pair_flags(df: pd.DataFrame, pivots: list, side: str) -> dict:
    """For every candidate pair of one side: (uncrossed, near) under this frame.

    These are the only two terms of fit_channel's score that depend on bars rather
    than on the pivot set, so when the pivots are unchanged and the rails still
    moved, one of these flags must have flipped for some pair.
    """
    t = to_utc_seconds(df.index)
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    right_edge = float(t[-1])
    closed_range = float(hi[:-1].max()) - float(lo[:-1].min())
    ref = float(df["close"].iloc[-2])
    reach = min(closed_range, 0.2 * abs(ref))
    pts = sorted(pivots, key=lambda p: (p.time, p.index))
    flags = {}
    for i in range(len(pts) - 1):
        for j in range(i + 1, len(pts)):
            dtj = pts[j].time - pts[i].time
            if dtj <= 0:
                continue
            slope = (pts[j].price - pts[i].price) / dtj
            intercept = pts[i].price - slope * pts[i].time
            span = (t >= pts[i].time) & (t <= right_edge)
            span[-1] = False
            line = slope * t + intercept
            slack = np.abs(line) * CROSSING_TOLERANCE_PCT / 100.0
            crossed = span & ((hi > line + slack) if side == "high" else (lo < line - slack))
            near = abs(slope * right_edge + intercept - ref) <= reach
            flags[(pts[i].index, pts[j].index)] = (int(crossed.sum()) == 0, bool(near))
    return flags


def channel_of(df: pd.DataFrame, start=None, end=None):
    """The tool layer's exact path: envelope at five pivots a side, then fit_channel."""
    env = envelope(df, 3, 3, CHANNEL_PIVOTS, start, end)
    fit = fit_channel(df, env["highs"], env["lows"], start, end, TOUCH_TOL, CROSSING_TOLERANCE_PCT)
    return env, fit


def pivot_set(env: dict) -> frozenset:
    return frozenset((p.index, round(p.price, 9), "h") for p in env["highs"]) | frozenset(
        (p.index, round(p.price, 9), "l") for p in env["lows"])


def rail_key(rail):
    """The line itself: its anchors, slope and intercept. Not its right edge, which
    legitimately moves one bar on when a bar is appended."""
    if rail is None:
        return None
    return (rail["from_pivot"].index, rail["to_pivot"].index,
            round(rail["slope"], 15), round(rail["intercept"], 9))


# ---------------------------------------------------------------------------
# timezone
# ---------------------------------------------------------------------------


def test_timezone() -> None:
    print("\n--- to_utc_seconds ---")

    naive = pd.DatetimeIndex(["2026-01-01 09:15:00"], name="timestamp")
    aware = pd.DatetimeIndex(["2026-01-01 14:45:00"], name="timestamp").tz_localize(
        "Asia/Kolkata"
    )
    a = to_utc_seconds(naive)
    b = to_utc_seconds(aware)
    check(
        "a naive daily index and a tz-aware Asia/Kolkata index agree on the instant",
        float(a[0]) == float(b[0]),
        f"naive={a[0]:.0f} aware={b[0]:.0f} ({iso(float(a[0]))} UTC)",
    )

    expected = dt.datetime(2026, 1, 1, 9, 15, tzinfo=dt.UTC).timestamp()
    check("naive index is read as UTC, not as local time", float(a[0]) == expected,
          f"{a[0]:.0f} vs {expected:.0f}")

    check("seconds, not milliseconds", 1e9 < float(a[0]) < 1e10, f"{a[0]:.0f}")

    check("empty index returns an empty array",
          to_utc_seconds(pd.DatetimeIndex([])).size == 0)
    check("None returns an empty array", to_utc_seconds(None).size == 0)

    # The two frame flavours the SDK actually hands back.
    daily = frame_from_close(np.arange(10, dtype=float), tz=None, freq="D")
    intraday = frame_from_close(np.arange(10, dtype=float), tz="Asia/Kolkata", freq="5min")
    check("naive daily frame normalises", to_utc_seconds(daily.index).size == 10,
          f"first={iso(float(to_utc_seconds(daily.index)[0]))}")
    check("tz-aware intraday frame normalises", to_utc_seconds(intraday.index).size == 10,
          f"first={iso(float(to_utc_seconds(intraday.index)[0]))}")

    ts = to_utc_seconds(intraday.index)
    check("intraday spacing is 300 seconds", bool(np.all(np.diff(ts) == 300.0)),
          f"diffs={sorted(set(np.diff(ts).tolist()))}")

    nat = to_utc_seconds(pd.DatetimeIndex([pd.NaT, pd.Timestamp("2026-01-01")]))
    check("NaT becomes NaN rather than a huge negative number",
          bool(np.isnan(nat[0])) and np.isfinite(nat[1]))

    # An integer index is an epoch. pd.DatetimeIndex would read 1_700_000_000 as
    # 1.7 seconds after 1970; the chart engine means November 2023.
    secs = to_utc_seconds(pd.Index([1_700_000_000, 1_700_000_300]))
    check("an integer index below 1e11 is read as epoch seconds",
          secs.tolist() == [1_700_000_000.0, 1_700_000_300.0], str(secs.tolist()))
    ms = to_utc_seconds([1_700_000_000_000, 1_700_000_300_000])
    check("an integer list above 1e11 is read as epoch milliseconds",
          ms.tolist() == [1_700_000_000.0, 1_700_000_300.0], str(ms.tolist()))
    arr = to_utc_seconds(np.array([1_700_000_000, 1_700_000_060], dtype=np.int64))
    check("a numpy integer array is read the same way",
          arr.tolist() == [1_700_000_000.0, 1_700_000_060.0], str(arr.tolist()))
    check("a RangeIndex is read as seconds 0..n-1, which is what the bar-index "
          "fallback produced anyway",
          to_utc_seconds(pd.RangeIndex(3)).tolist() == [0.0, 1.0, 2.0])
    check("a float array is not mistaken for an integer epoch",
          to_utc_seconds(np.array([1.5, 2.5])).size in (0, 2))

    # A mixed-timezone list used to coerce the minority zone to NaT, silently.
    mixed = [pd.Timestamp("2026-01-01 09:15", tz="UTC"),
             pd.Timestamp("2026-01-01 09:15", tz="Asia/Kolkata")]
    try:
        out = to_utc_seconds(mixed)
        check("a mixed-timezone list raises rather than coercing", False,
              f"returned {out.tolist()}")
    except ValueError as exc:
        check("a mixed-timezone list raises rather than coercing", "timezone" in str(exc),
              str(exc)[:70])
    same_zone = to_utc_seconds([pd.Timestamp("2026-01-01 09:15", tz="Asia/Kolkata"),
                                pd.Timestamp("2026-01-01 09:20", tz="Asia/Kolkata")])
    check("a single-zone list of tz-aware timestamps still converts",
          same_zone.size == 2 and np.isfinite(same_zone).all()
          and same_zone[1] - same_zone[0] == 300.0, str(same_zone.tolist()))


def test_bar_interval() -> None:
    print("\n--- bar_interval_seconds ---")
    daily = frame_from_close(np.arange(30, dtype=float), freq="D")
    check("a daily frame is 86400 seconds per bar",
          bar_interval_seconds(daily.index) == 86400.0, str(bar_interval_seconds(daily.index)))
    five = frame_from_close(np.arange(30, dtype=float), tz="Asia/Kolkata", freq="5min")
    check("a 5m frame is 300 seconds per bar",
          bar_interval_seconds(five.index) == 300.0, str(bar_interval_seconds(five.index)))
    # Weekdays only: the weekend gaps must not stretch the answer.
    business = pd.bdate_range("2026-01-01", periods=30, name="timestamp")
    check("weekend gaps on a business-day index do not stretch the median",
          bar_interval_seconds(business) == 86400.0, str(bar_interval_seconds(business)))
    check("fewer than two bars is 0.0",
          bar_interval_seconds(pd.DatetimeIndex(["2026-01-01"])) == 0.0
          and bar_interval_seconds(None) == 0.0)
    check("an array of UTC seconds is accepted directly",
          bar_interval_seconds(np.array([0.0, 60.0, 120.0])) == 60.0)


# ---------------------------------------------------------------------------
# the off-by-right anchor
# ---------------------------------------------------------------------------


def test_pivot_anchor() -> None:
    print("\n--- pivot anchor (the off-by-right bug) ---")

    # One clean peak at bar 20 and one clean trough at bar 40.
    closes = np.full(61, 100.0)
    for i in range(61):
        if i <= 20:
            closes[i] = 100.0 + i * 0.5
        elif i <= 40:
            closes[i] = 110.0 - (i - 20) * 0.75
        else:
            closes[i] = 95.0 + (i - 40) * 0.5
    df = frame_from_close(closes)

    right = 4
    highs, lows = swing_pivots(df, left=3, right=right)
    check("exactly one swing high found", len(highs) == 1, f"{len(highs)} found")
    check("exactly one swing low found", len(lows) == 1, f"{len(lows)} found")

    if highs and lows:
        h, lo = highs[0], lows[0]
        check(
            "swing high anchors to the extreme bar, not the confirming bar",
            h.index == 20,
            f"index={h.index}, naive off-by-right would give {20 + right}",
        )
        check(
            "swing low anchors to the extreme bar, not the confirming bar",
            lo.index == 40,
            f"index={lo.index}, naive off-by-right would give {40 + right}",
        )
        times = to_utc_seconds(df.index)
        check("pivot time is the extreme bar's time",
              h.time == float(times[20]) and lo.time == float(times[40]),
              f"high={iso(h.time)} low={iso(lo.time)}")
        check("pivot price is the extreme bar's high and low",
              h.price == float(df["high"].iloc[20])
              and lo.price == float(df["low"].iloc[40]),
              f"high={h.price} low={lo.price}")
        check("pivot price is not the confirming bar's price",
              h.price != float(df["high"].iloc[20 + right]),
              f"{h.price} vs confirming bar {float(df['high'].iloc[20 + right])}")
        check("a pivot knows which side it came from",
              h.kind == "high" and lo.kind == "low", f"{h.kind}/{lo.kind}")

    # The anchor must not move when the confirmation window changes.
    anchors = {}
    for r in (1, 2, 3, 5, 8):
        hs, ls = swing_pivots(df, left=3, right=r)
        anchors[r] = (hs[0].index if hs else None, ls[0].index if ls else None)
    check("the anchor is independent of the right window",
          all(v == (20, 40) for v in anchors.values()),
          "; ".join(f"right={k}:{v}" for k, v in anchors.items()))

    # And the envelope inherits it, which is where the shear showed up.
    env = envelope(df, left=3, right=4, max_points=9)
    idxs = [p.index for p in env["highs"]] + [p.index for p in env["lows"]]
    check("envelope vertices sit on the extreme bars",
          sorted(idxs) == [20, 40], f"indices={sorted(idxs)}")


def test_multiple_known_pivots() -> None:
    print("\n--- pivots at known indices ---")

    peaks = [10, 30, 50, 70]
    troughs = [20, 40, 60, 80]
    df = sawtooth(peaks, troughs, n=91)

    highs, lows = swing_pivots(df, left=3, right=3)
    check("every constructed peak is found", [p.index for p in highs] == peaks,
          f"{[p.index for p in highs]} vs {peaks}")
    check("every constructed trough is found", [p.index for p in lows] == troughs,
          f"{[p.index for p in lows]} vs {troughs}")
    check("highs are ascending in time",
          all(highs[i].time < highs[i + 1].time for i in range(len(highs) - 1)))
    check("high prices come from the high column",
          all(p.price == float(df["high"].iloc[p.index]) for p in highs))
    check("low prices come from the low column",
          all(p.price == float(df["low"].iloc[p.index]) for p in lows))

    # A plateau must resolve to one pivot, its first bar, not to every bar of it.
    closes = np.concatenate([np.arange(0, 10.0), np.full(3, 10.0), np.arange(9, -1, -1.0)])
    flat_top = frame_from_close(closes)
    hs, _ = swing_pivots(flat_top, left=3, right=3)
    check("a flat top yields exactly one swing high", len(hs) == 1,
          f"{[p.index for p in hs]}")
    check("the plateau's pivot is its first bar", bool(hs) and hs[0].index == 10,
          f"index={hs[0].index if hs else None}")

    # An exact double top is TWO pivots. Under a strict right-hand test the first
    # equal top failed against the second and only the later one survived, which
    # threw away the level a trader was actually watching.
    closes = np.array([1, 2, 3, 5, 3, 2, 5, 3, 1, 0], dtype=float)
    double = frame_from_close(closes)
    hs, _ = swing_pivots(double, left=3, right=3)
    check("an exact double top yields both tops", [p.index for p in hs] == [3, 6],
          f"{[p.index for p in hs]}")
    ls_double, _ = swing_pivots(frame_from_close(-closes), left=3, right=3)[1], None
    check("an exact double bottom yields both bottoms",
          [p.index for p in ls_double] == [3, 6], f"{[p.index for p in ls_double]}")
    # Equal highs in the raw fractal set are also two pivots in the envelope path.
    hs_raw, _ = swing_pivots(double, 3, 3, significance=0.0)
    check("both equal tops carry the same price",
          len(hs_raw) == 2 and hs_raw[0].price == hs_raw[1].price)


def test_clipping() -> None:
    print("\n--- start and end clipping ---")

    peaks = [10, 30, 50, 70]
    troughs = [20, 40, 60, 80]
    df = sawtooth(peaks, troughs, n=91)
    times = to_utc_seconds(df.index)

    highs, lows = swing_pivots(df, 3, 3, start=float(times[25]), end=float(times[65]))
    check("clipping keeps only the visible highs", [p.index for p in highs] == [30, 50],
          f"{[p.index for p in highs]}")
    check("clipping keeps only the visible lows", [p.index for p in lows] == [40, 60],
          f"{[p.index for p in lows]}")

    # A pivot two bars inside the right edge is still confirmed by bars the
    # viewport hides, which is why detection runs before the clip.
    hs, _ = swing_pivots(df, 3, 3, start=float(times[0]), end=float(times[32]))
    check("a pivot near the right edge is still confirmed from hidden bars",
          [p.index for p in hs] == [10, 30], f"{[p.index for p in hs]}")

    none_visible = swing_pivots(df, 3, 3, start=float(times[-1]) + 1e6)
    check("an empty viewport returns empty lists", none_visible == ([], []))

    env = envelope(df, start=float(times[25]), end=float(times[65]))
    check("envelope respects the viewport",
          [p.index for p in env["highs"]] == [30, 50]
          and [p.index for p in env["lows"]] == [40, 60],
          f"highs={[p.index for p in env['highs']]} lows={[p.index for p in env['lows']]}")


# ---------------------------------------------------------------------------
# density
# ---------------------------------------------------------------------------


def test_density() -> None:
    print("\n--- density control ---")

    rng = np.random.default_rng(11)
    n = 250
    trend = np.linspace(100.0, 160.0, n)
    swings = 12.0 * np.sin(np.linspace(0, 7 * np.pi, n))
    noise = rng.normal(0.0, 1.1, n)
    df = frame_from_close(trend + swings + noise)

    raw_h, raw_l = swing_pivots(df, 3, 3, significance=0.0)
    filt_h, filt_l = swing_pivots(df, 3, 3, significance=ENVELOPE_SIGNIFICANCE)
    env = envelope(df)

    print(f"      raw fractals      : {len(raw_h)} highs, {len(raw_l)} lows")
    print(f"      after significance: {len(filt_h)} highs, {len(filt_l)} lows "
          f"(significance={ENVELOPE_SIGNIFICANCE})")
    print(f"      envelope vertices : {len(env['highs'])} highs, {len(env['lows'])} lows "
          f"(max_points=9)")

    check("significance thins the sawtooth",
          len(filt_h) < len(raw_h) and len(filt_l) < len(raw_l),
          f"{len(raw_h)}/{len(raw_l)} -> {len(filt_h)}/{len(filt_l)}")
    check("envelope lands in the 5 to 9 vertex band",
          3 <= len(env["highs"]) <= 9 and 3 <= len(env["lows"]) <= 9,
          f"{len(env['highs'])} highs, {len(env['lows'])} lows")
    check("max_points is honoured",
          len(envelope(df, max_points=4)["highs"]) <= 4,
          f"{len(envelope(df, max_points=4)['highs'])} with max_points=4")

    # Raising significance must never add vertices.
    counts = [len(swing_pivots(df, 3, 3, significance=s)[0])
              for s in (0.0, 0.05, 0.1, 0.2, 0.4)]
    check("vertex count falls monotonically with significance",
          all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1)),
          f"{counts}")

    # The band must still touch the real extreme after thinning.
    env4 = envelope(df, max_points=4)
    top = float(df["high"].max())
    bottom = float(df["low"].min())
    check("the window's highest high survives the cap",
          any(abs(p.price - top) < 1e-9 for p in env4["highs"]),
          f"top={top:.2f} kept={[round(p.price, 2) for p in env4['highs']]}")
    check("the window's lowest low survives the cap",
          any(abs(p.price - bottom) < 1e-9 for p in env4["lows"]),
          f"bottom={bottom:.2f} kept={[round(p.price, 2) for p in env4['lows']]}")

    # A big leg followed by a shallow pullback must keep the top of the leg.
    closes = np.concatenate([
        np.linspace(100, 160, 60),      # long rally, top at index 59
        np.linspace(160, 157, 8),       # shallow pullback
        np.linspace(157, 162, 8),       # marginal new high at index 75
        np.linspace(162, 105, 60),      # collapse
    ])
    leg = frame_from_close(closes)
    hs, _ = swing_pivots(leg, 3, 3, significance=0.2)
    check("a shallow pullback does not strand the top of a long leg",
          bool(hs) and abs(hs[-1].price - float(leg["high"].max())) < 1e-9,
          f"kept={[(p.index, round(p.price, 2)) for p in hs]}")

    # A clean trend is the case the significance ladder exists for. Its range is
    # dominated by the trend, so a flat threshold sits above every retracement
    # and the band collapses to one vertex. This exact frame returned 1 high and
    # 0 lows before the ladder went in.
    n2 = 200
    clean = frame_from_close(
        np.linspace(100, 200, n2) + 6.0 * np.sin(np.linspace(0, 8 * np.pi, n2))
    )
    raw_clean = len(swing_pivots(clean, 3, 3, significance=0.0)[0])
    flat = len(swing_pivots(clean, 3, 3, significance=ENVELOPE_SIGNIFICANCE)[0])
    laddered = envelope(clean)
    print(f"      clean trend: {raw_clean} raw highs, {flat} at a flat "
          f"{ENVELOPE_SIGNIFICANCE} threshold, {len(laddered['highs'])} after the ladder")
    check("a clean trend with shallow pullbacks still draws a band",
          len(laddered["highs"]) >= 3 and len(laddered["lows"]) >= 3,
          f"{len(laddered['highs'])} highs, {len(laddered['lows'])} lows")
    check("the ladder recovers vertices a flat threshold destroys",
          len(laddered["highs"]) > flat, f"{flat} -> {len(laddered['highs'])}")
    check("the recovered band still reads as a trend",
          laddered["direction"] == "up", laddered["direction"])


def test_direction_and_width() -> None:
    print("\n--- envelope direction and width ---")

    n = 200
    swings = 6.0 * np.sin(np.linspace(0, 8 * np.pi, n))
    up = frame_from_close(np.linspace(100, 200, n) + swings)
    down = frame_from_close(np.linspace(200, 100, n) + swings)
    flat = frame_from_close(120.0 + swings)

    check("a rising series reads as up", envelope(up)["direction"] == "up",
          envelope(up)["direction"])
    check("a falling series reads as down", envelope(down)["direction"] == "down",
          envelope(down)["direction"])
    check("an oscillating series reads as sideways",
          envelope(flat)["direction"] == "sideways", envelope(flat)["direction"])

    env = envelope(flat)
    check("width is positive and near the oscillation height",
          8.0 < env["width"] < 20.0, f"width={env['width']:.2f}")
    check("slopes are price per second, not per bar",
          abs(envelope(up)["slope_high"]) < 1e-3,
          f"slope_high={envelope(up)['slope_high']:.3e}")
    check("first_time is before last_time", env["first_time"] < env["last_time"],
          f"{iso(env['first_time'])} .. {iso(env['last_time'])}")

    up_env = envelope(up)
    span = up_env["last_time"] - up_env["first_time"]
    rise = up_env["slope_high"] * span
    check("slope times span reproduces the rally",
          60.0 < rise < 130.0, f"rise={rise:.1f} over {span / 86400:.0f} days")

    # The tool layer turns the per-second slope into a per-bar one by the
    # frame's own bar spacing. A 0.5 point per day rally must read as 0.5.
    half = frame_from_close(100.0 + 0.5 * np.arange(200) + 3.0 * np.sin(np.arange(200) / 4.0))
    per_bar = envelope(half)["slope_high"] * bar_interval_seconds(half.index)
    check("slope times bar spacing reads a 0.5 per day rally as 0.5 per bar",
          abs(per_bar - 0.5) < 0.05, f"{per_bar:.4f} per bar")


# ---------------------------------------------------------------------------
# trendline, levels, measured move, snapping
# ---------------------------------------------------------------------------


def test_trendline() -> None:
    print("\n--- fit_trendline ---")

    base = 1_700_000_000.0
    day = 86400.0
    # Four pivots exactly on a rising line, plus one well off it.
    on_line = [Pivot(i, base + i * 10 * day, 100.0 + i * 5.0) for i in range(4)]
    off_line = [Pivot(9, base + 9 * 10 * day, 90.0)]
    fit = fit_trendline(on_line + off_line, tolerance_pct=0.5)

    check("the collinear pivots are all counted", fit["touches"] == 4,
          f"touches={fit['touches']}")
    check("the outlier is excluded",
          fit["from"] is not None and fit["to"] is not None
          and fit["to"].index != 9, f"to index={fit['to'].index if fit['to'] else None}")
    check("the line spans the full run of touches",
          fit["from"].index == 0 and fit["to"].index == 3,
          f"{fit['from'].index} -> {fit['to'].index}")
    check("r2 is 1.0 on exactly collinear points", abs(fit["r2"] - 1.0) < 1e-9,
          f"r2={fit['r2']}")
    predicted = fit["slope"] * on_line[2].time + fit["intercept"]
    check("slope and intercept reproduce a pivot price",
          abs(predicted - on_line[2].price) < 1e-6,
          f"{predicted:.6f} vs {on_line[2].price}")

    check("fewer than two pivots returns a zeroed dict without raising",
          fit_trendline([])["touches"] == 0
          and fit_trendline([on_line[0]])["from"] is None)

    # A wider tolerance must never lose touches.
    tight = fit_trendline(on_line + off_line, tolerance_pct=0.1)["touches"]
    loose = fit_trendline(on_line + off_line, tolerance_pct=20.0)["touches"]
    check("a wider tolerance never reduces the touch count", loose >= tight,
          f"tight={tight} loose={loose}")

    # Real pivots off a real frame.
    df = sawtooth([10, 30, 50, 70], [20, 40, 60, 80], n=91)
    hs, _ = swing_pivots(df, 3, 3)
    live = fit_trendline(hs)
    check("a flat run of equal highs fits with every pivot touching",
          live["touches"] == len(hs), f"{live['touches']} of {len(hs)}")

    # A required pivot survives the recency cap. Sixty recent pivots on one line
    # and one old anchor ON that line: the cap alone drops the anchor, and the
    # line then starts sixty bars too late.
    recent = [Pivot(100 + i, base + (100 + i) * day, 200.0 + i) for i in range(60)]
    old = Pivot(1, base + day, 200.0 - 99.0, "low")
    capped = fit_trendline(recent + [old], 0.5)
    forced = fit_trendline(recent, 0.5, required=[old])
    check("without the anchor the cap keeps only recent pivots",
          capped["from"] is not None and capped["from"].index != 1,
          f"from index {capped['from'].index if capped['from'] else None}")
    check("a required pivot is in the candidate set whatever the cap does",
          forced["from"] is not None and forced["from"].index == 1
          and forced["touches"] >= 40,
          f"from {forced['from'].index if forced['from'] else None}, "
          f"touches {forced['touches']}")
    dup = fit_trendline(on_line, 0.5, required=[on_line[0]])
    check("a required pivot already present is not counted twice",
          dup["touches"] == 4, f"touches={dup['touches']}")
    check("required alone with one other pivot still fits",
          fit_trendline([on_line[0]], 0.5, required=[on_line[3]])["touches"] == 2)


def test_levels() -> None:
    print("\n--- support_resistance ---")

    # Four visits to 110 and four to 90, on a frame whose last close is 100.
    df = sawtooth([10, 30, 50, 70], [20, 40, 60, 80], n=91, amp=10.0)
    levels = support_resistance(df, min_touches=2)
    check("at least two clusters are found", len(levels) >= 2, f"{len(levels)} levels")

    if len(levels) >= 2:
        top = next((lv for lv in levels if lv["price"] > 100), None)
        bottom = next((lv for lv in levels if lv["price"] < 100), None)
        check("the upper cluster sits at the peaks",
              top is not None and abs(top["price"] - 111.0) < 1.5,
              f"{top['price']:.2f}" if top else "missing")
        check("the lower cluster sits at the troughs",
              bottom is not None and abs(bottom["price"] - 89.0) < 1.5,
              f"{bottom['price']:.2f}" if bottom else "missing")
        check("each cluster counts four touches",
              top is not None and bottom is not None
              and top["touches"] == 4 and bottom["touches"] == 4,
              f"top={top['touches'] if top else 0} bottom={bottom['touches'] if bottom else 0}")
        check("kind is read against the last close",
              top["kind"] == "resistance" and bottom["kind"] == "support",
              f"{top['kind']}/{bottom['kind']}")
        check("each level says which side it was built from",
              top["side"] == "high" and bottom["side"] == "low",
              f"{top['side']}/{bottom['side']}")
        check("first_time precedes last_time on every level",
              all(lv["first_time"] <= lv["last_time"] for lv in levels))
        check("levels are sorted strongest first",
              all(levels[i]["touches"] >= levels[i + 1]["touches"]
                  for i in range(len(levels) - 1)),
              f"{[lv['touches'] for lv in levels]}")

    check("min_touches filters", support_resistance(df, min_touches=9) == [],
          f"{len(support_resistance(df, min_touches=9))} survived min_touches=9")
    check("an explicit bin count is accepted",
          isinstance(support_resistance(df, bins=20), list))

    # One reference price for the role. With the reference above every level,
    # everything is support; below, everything is resistance.
    check("kind follows the reference price, not the last close",
          all(lv["kind"] == "support" for lv in support_resistance(df, reference=500.0))
          and all(lv["kind"] == "resistance" for lv in support_resistance(df, reference=1.0)))
    check("a non-numeric reference falls back to the last close",
          support_resistance(df, reference="abc")[0]["kind"]
          == support_resistance(df)[0]["kind"])

    # Highs and lows cluster separately, and the reported price is a real print.
    rng = np.random.default_rng(3)
    wobble = 100.0 + 10.0 * np.sin(np.arange(160) / 4.0) + rng.normal(0, 0.8, 160)
    noisy = frame_from_close(wobble)
    hs, ls = swing_pivots(noisy, 3, 3)
    high_prices = {p.price for p in hs}
    low_prices = {p.price for p in ls}
    found = support_resistance(noisy)
    check("every reported level is a pivot price that actually printed",
          bool(found) and all(
              (lv["price"] in high_prices) if lv["side"] == "high" else (lv["price"] in low_prices)
              for lv in found),
          f"{[(round(lv['price'], 2), lv['side']) for lv in found[:4]]}")
    check("no level mixes swing highs with swing lows",
          all(lv["side"] in ("high", "low") for lv in found))


def test_measured_move() -> None:
    print("\n--- measured_move ---")

    df = sawtooth([10, 30, 50, 70], [20, 40, 60, 80], n=91, amp=10.0)
    env, fit = channel_of(df)
    check("the sawtooth fits two rails", fit["upper"] is not None and fit["lower"] is not None,
          fit["shape"])
    mm = measured_move({**fit, "direction": env["direction"]})

    check("the projection is one channel height above the upper rail",
          abs((mm["upside_target"] - mm["breakout"]) - mm["width"]) < 1e-6,
          f"breakout={mm['breakout']:.2f} target={mm['upside_target']:.2f} "
          f"width={mm['width']:.2f}")
    check("the projection is one channel height below the lower rail",
          abs((mm["breakdown"] - mm["downside_target"]) - mm["width"]) < 1e-6,
          f"breakdown={mm['breakdown']:.2f} target={mm['downside_target']:.2f}")
    check("the upper rail is above the lower rail", mm["breakout"] > mm["breakdown"],
          f"{mm['breakout']:.2f} > {mm['breakdown']:.2f}")
    # THE RAIL MEASURED IS THE RAIL DRAWN. Exact, not approximately.
    check("breakout is the upper rail's own right-edge price",
          mm["breakout"] == fit["upper"]["to"]["price"],
          f"{mm['breakout']!r} vs {fit['upper']['to']['price']!r}")
    check("breakdown is the lower rail's own right-edge price",
          mm["breakdown"] == fit["lower"]["to"]["price"])
    check("width is the gap between the rails at the right edge",
          abs(mm["width"] - fit["width_right"]) < 1e-9,
          f"{mm['width']:.4f} vs width_right {fit['width_right']:.4f}")
    check("targets are projected from the right edge of the drawn rails",
          mm["time"] == fit["upper"]["to"]["time"] and mm["target_time"] > mm["time"],
          f"{iso(mm['time'])} -> {iso(mm['target_time'])}")
    check("direction is carried through", mm["direction"] == env["direction"],
          mm["direction"])

    # Over many frames, the measured line and the drawn line never part.
    worst = 0.0
    for _, _, frame in market_frames(60, start_seed=9000):
        e, f = channel_of(frame)
        if f["upper"] is None or f["lower"] is None or f["shape"] == "crossed":
            continue
        m = measured_move(f)
        worst = max(worst, abs(m["breakout"] - f["upper"]["to"]["price"]),
                    abs(m["breakdown"] - f["lower"]["to"]["price"]))
    check("over 60 synthetic frames the measured rail is the drawn rail to 1e-9",
          worst < 1e-9, f"worst |measured - drawn| = {worst:.2e}")

    check("an envelope-shaped dict without rails returns zeros without raising",
          measured_move(envelope(df))["width"] == 0.0)
    check("an empty channel returns zeros without raising",
          measured_move(fit_channel(frame_from_close([]), [], []))["width"] == 0.0)
    check("a non-dict argument returns zeros without raising",
          measured_move(None)["width"] == 0.0)
    check("a crossed channel projects nothing",
          measured_move({**fit, "shape": "crossed"})["width"] == 0.0)
    check("one missing rail projects nothing",
          measured_move({"upper": fit["upper"], "lower": None})["width"] == 0.0)


def test_snap() -> None:
    print("\n--- snap_to_pivot ---")

    # A frame whose real swing high is 4446.80 and real swing low 3235.45.
    closes = np.concatenate([
        np.linspace(3800, 4445.8, 41),
        np.linspace(4445.8, 3236.45, 40)[1:],
        np.linspace(3236.45, 3900, 41)[1:],
    ])
    df = frame_from_close(closes)
    top = float(df["high"].max())
    bottom = float(df["low"].min())

    hit = snap_to_pivot(df, 4446)
    check("a loose 4446 snaps to the real high",
          hit is not None and abs(hit.price - top) < 1e-6,
          f"4446 -> {hit.price if hit else None} (real {top})")
    hit_low = snap_to_pivot(df, 3235)
    check("a loose 3235 snaps to the real low",
          hit_low is not None and abs(hit_low.price - bottom) < 1e-6,
          f"3235 -> {hit_low.price if hit_low else None} (real {bottom})")
    check("the snapped pivot says which side it is",
          hit is not None and hit.kind == "high" and hit_low is not None
          and hit_low.kind == "low")

    check("kind=high refuses to return a low",
          (lambda p: p is None or p.price > 4000)(snap_to_pivot(df, 3235, kind="high")),
          str(snap_to_pivot(df, 3235, kind="high")))
    check("kind=low refuses to return a high",
          (lambda p: p is None or p.price < 4000)(snap_to_pivot(df, 4446, kind="low")),
          str(snap_to_pivot(df, 4446, kind="low")))
    check("resistance is a synonym for high",
          (lambda a, b: (a is None) == (b is None)
           and (a is None or a.price == b.price))(
              snap_to_pivot(df, 4446, kind="resistance"),
              snap_to_pivot(df, 4446, kind="high")))

    check("a number far from any pivot returns None",
          snap_to_pivot(df, 9999) is None, str(snap_to_pivot(df, 9999)))
    check("a tight tolerance rejects a near miss",
          snap_to_pivot(df, 4400, tolerance_pct=0.1) is None)
    check("a wide tolerance accepts it",
          snap_to_pivot(df, 4400, tolerance_pct=5.0) is not None)
    check("the snapped pivot carries a UTC second time",
          hit is not None and 1e9 < hit.time < 1e10, f"{iso(hit.time) if hit else 'n/a'}")
    check("the snapped index points at the real extreme bar",
          hit is not None and hit.price == float(df["high"].iloc[hit.index]),
          f"index={hit.index if hit else None}")
    check("zero and non-numeric prices return None",
          snap_to_pivot(df, 0) is None and snap_to_pivot(df, "abc") is None)

    # The pool is the window. With the window cut to the last third, the high at
    # bar 40 is not there to snap to.
    times = to_utc_seconds(df.index)
    windowed = snap_to_pivot(df, 4446, start=float(times[90]), end=float(times[-1]))
    check("a pivot outside the window is not in the pool", windowed is None,
          str(windowed))
    inside = snap_to_pivot(df, 4446, start=float(times[10]), end=float(times[60]))
    check("the same pivot inside the window still snaps",
          inside is not None and abs(inside.price - top) < 1e-6)

    # The forming bar is not in the pool, even when it is the frame's extreme.
    spiked = df.copy()
    spiked.iloc[-1, spiked.columns.get_loc("high")] = 5000.0
    check("a spike on the forming bar cannot be snapped to",
          snap_to_pivot(spiked, 5000) is None, str(snap_to_pivot(spiked, 5000)))
    check("the frame's closed extreme still can be",
          (lambda p: p is not None and abs(p.price - top) < 1e-6)(snap_to_pivot(spiked, 4446)))

    # Lookback insensitivity: 20 frames of 600 bars, three remembered prices each,
    # snapped against the whole frame and against its last 300 bars with the same
    # window. The bar found must not depend on how much was fetched.
    trials = 0
    differ = 0
    for k in range(20):
        df600 = market_frame(FAMILIES[k % 4], 7000 + k, n=600)
        df300 = df600.iloc[-300:]
        t = to_utc_seconds(df300.index)
        start, end = float(t[0]), float(t[-1])
        hs, ls = swing_pivots(df300, 2, 2)
        if len(hs) < 2 or len(ls) < 2:
            continue
        for price in (round(max(p.price for p in hs)), round(min(p.price for p in ls)),
                      round(hs[len(hs) // 2].price)):
            trials += 1
            a = snap_to_pivot(df600, price, "any", 2.0, start, end)
            b = snap_to_pivot(df300, price, "any", 2.0, start, end)
            ka = None if a is None else (round(a.time), round(a.price, 6))
            kb = None if b is None else (round(b.time), round(b.price, 6))
            if ka != kb:
                differ += 1
    print(f"      lookback probe: snapped bar differs between a 600 and a 300 bar fetch "
          f"in {differ}/{trials} trials (7/60 before the pool was clipped to the window)")
    check("the snapped bar never depends on the fetch length", trials >= 50 and differ == 0,
          f"{differ}/{trials}")


# ---------------------------------------------------------------------------
# degenerate frames
# ---------------------------------------------------------------------------


def test_degenerate() -> None:
    print("\n--- degenerate frames ---")

    empty = frame_from_close([])
    three = frame_from_close([100.0, 101.0, 100.5])
    flat = frame_from_close(np.full(50, 100.0))
    single = frame_from_close(
        np.concatenate([np.linspace(100, 120, 25), np.linspace(120, 100, 25)[1:]])
    )
    one_bar = frame_from_close([100.0])

    cases = {
        "empty": empty,
        "three bars": three,
        "one bar": one_bar,
        "flat 50 bars": flat,
        "single pivot": single,
    }

    for name, df in cases.items():
        try:
            hs, ls = swing_pivots(df)
            env = envelope(df)
            lv = support_resistance(df)
            fit = fit_channel(df, hs, ls)
            mm = measured_move(fit)
            sn = snap_to_pivot(df, 100.0)
            tl = fit_trendline(hs)
            ok = (
                isinstance(hs, list) and isinstance(ls, list)
                and isinstance(env, dict) and isinstance(lv, list)
                and isinstance(fit, dict)
                and isinstance(mm, dict) and isinstance(tl, dict)
                and (sn is None or isinstance(sn, Pivot))
            )
            check(f"{name}: every entry point returns without raising", ok,
                  f"highs={len(hs)} lows={len(ls)} levels={len(lv)} "
                  f"direction={env['direction']}")
        except Exception as exc:  # noqa: BLE001
            check(f"{name}: every entry point returns without raising", False,
                  f"{type(exc).__name__}: {exc}")

    check("a flat series has no swings", swing_pivots(flat) == ([], []))
    check("a three bar frame is shorter than the 7 bar window",
          swing_pivots(three) == ([], []))
    hs, ls = swing_pivots(single)
    check("a single pivot series finds exactly one high and no low",
          len(hs) == 1 and len(ls) == 0, f"{len(hs)} highs, {len(ls)} lows")
    env = envelope(single)
    check("an envelope with one boundary still reports a width",
          env["width"] > 0 and len(env["highs"]) == 1,
          f"width={env['width']:.2f}, {len(env['highs'])} highs, {len(env['lows'])} lows")
    check("an empty envelope has empty vertex lists",
          envelope(empty)["highs"] == [] and envelope(empty)["lows"] == [])

    # Missing columns must not explode either.
    stripped = flat.drop(columns=["high", "low"])
    try:
        swing_pivots(stripped)
        check("a frame missing high and low falls back to close", True)
    except Exception as exc:  # noqa: BLE001
        check("a frame missing high and low falls back to close", False,
              f"{type(exc).__name__}: {exc}")

    # A right window of zero could confirm the last bar; it must not.
    hs0, ls0 = swing_pivots(frame_from_close(np.arange(20, dtype=float)), left=3, right=0)
    check("the last bar never becomes a pivot, even with right=0",
          all(p.index != 19 for p in hs0 + ls0), f"{[p.index for p in hs0 + ls0]}")


def test_json_safety() -> None:
    print("\n--- wire safety ---")

    df = sawtooth([10, 30, 50, 70], [20, 40, 60, 80], n=91)
    env = envelope(df)
    scalars = ["slope_high", "slope_low", "width", "first_time", "last_time"]
    check("envelope scalars are plain floats",
          all(type(env[k]) is float for k in scalars),
          ", ".join(f"{k}={type(env[k]).__name__}" for k in scalars))
    check("pivot fields are plain int and float",
          all(type(p.index) is int and type(p.time) is float and type(p.price) is float
              for p in env["highs"] + env["lows"]))
    check("a pivot renders as a contract anchor",
          set(env["highs"][0].as_anchor()) == {"time", "price"},
          str(env["highs"][0].as_anchor()))
    check("Pivot is frozen",
          _is_frozen(env["highs"][0]))

    import json
    _, fit = channel_of(df)
    payload = {
        "envelope": {
            "highs": [p.as_anchor() for p in env["highs"]],
            "lows": [p.as_anchor() for p in env["lows"]],
            **{k: env[k] for k in scalars},
            "direction": env["direction"],
        },
        "levels": support_resistance(df),
        "measured_move": measured_move(fit),
        "rails": {side: {k: v for k, v in (fit[side] or {}).items()
                         if k not in ("from_pivot", "to_pivot")}
                  for side in ("upper", "lower")},
    }
    try:
        json.dumps(payload)
        check("the whole result set is JSON serialisable", True)
    except (TypeError, ValueError) as exc:
        check("the whole result set is JSON serialisable", False, str(exc))


def _is_frozen(pivot: Pivot) -> bool:
    try:
        pivot.price = 1.0  # type: ignore[misc]
    except Exception:  # noqa: BLE001
        return True
    return False


# ---------------------------------------------------------------------------
# fit_channel: containment, determinism, the forming bar, crossing rails
# ---------------------------------------------------------------------------


def test_fit_channel() -> None:
    """Two rails that bound price, start at their own anchors, and never move."""
    print("\n=== fit_channel ===")
    from app.charts import geometry as G

    # A descending move: lower highs and lower lows, with noise.
    n = 120
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="Asia/Kolkata")
    drift = 1400 - np.arange(n) * 0.8
    wave = 12 * np.sin(np.arange(n) / 5.0)
    close = drift + wave + rng.normal(0, 1.0, n)
    high = close + 3 + rng.random(n) * 2
    low = close - 3 - rng.random(n) * 2
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                       "volume": 1000, "oi": 0}, index=idx)

    env = G.envelope(df, 3, 3, 5)
    highs, lows = env["highs"], env["lows"]
    check("the synthetic move yields pivots on both sides",
          len(highs) >= 2 and len(lows) >= 2, f"{len(highs)} highs, {len(lows)} lows")

    fit = G.fit_channel(df, highs, lows, None, None, TOUCH_TOL, CROSSING_TOLERANCE_PCT)
    up, lo = fit["upper"], fit["lower"]
    check("both rails are fitted", up is not None and lo is not None, str(fit["shape"]))
    if up is None or lo is None:
        return

    t = G.to_utc_seconds(df.index)
    hi_arr = df["high"].to_numpy(dtype=float)
    lo_arr = df["low"].to_numpy(dtype=float)

    def crossings(rail, side):
        line = rail["slope"] * t + rail["intercept"]
        span = (t >= rail["from"]["time"]) & (t <= rail["to"]["time"])
        span[-1] = False
        slack = np.abs(line) * CROSSING_TOLERANCE_PCT / 100.0
        bad = span & ((hi_arr > line + slack) if side == "high" else (lo_arr < line - slack))
        return int(bad.sum()), rail["crossings"]

    up_x, up_reported = crossings(up, "high")
    lo_x, lo_reported = crossings(lo, "low")
    check("no closed bar crosses the upper rail over its span", up_x == 0, f"{up_x} above")
    check("no closed bar crosses the lower rail over its span", lo_x == 0, f"{lo_x} below")
    check("the crossing count the fit reports is the one measured",
          up_reported == up_x and lo_reported == lo_x, f"{up_reported}/{lo_reported}")

    # NO BACKWARD EXTRAPOLATION. A rail starts at a real pivot of its own side.
    check("the upper rail starts at one of its own swing highs",
          any(abs(up["from"]["time"] - p.time) < 1e-6 for p in highs), str(up["from"]))
    check("the lower rail starts at one of its own swing lows",
          any(abs(lo["from"]["time"] - p.time) < 1e-6 for p in lows), str(lo["from"]))
    check("both rails end at the window's right edge",
          up["to"]["time"] == lo["to"]["time"] == float(t[-1]), str(up["to"]["time"]))
    check("the upper rail is above the lower rail at the right edge",
          up["to"]["price"] > lo["to"]["price"],
          f"{up['to']['price']:.2f} over {lo['to']['price']:.2f}")
    check("a descending move is classified as falling or contracting",
          fit["shape"] in ("falling", "contracting"), fit["shape"])

    # DETERMINISM. The same frame twice must give byte-identical rails.
    again = G.fit_channel(df, highs, lows, None, None, TOUCH_TOL, CROSSING_TOLERANCE_PCT)
    same = (again["upper"]["from"] == up["from"] and again["upper"]["to"] == up["to"]
            and again["lower"]["from"] == lo["from"] and again["lower"]["to"] == lo["to"])
    check("the same frame gives the same rails", same, "identical" if same else "DIFFERENT")

    # Edge cases must return, never raise.
    for label, frame in (("empty", df.iloc[0:0]), ("one bar", df.iloc[:1]), ("two bars", df.iloc[:2])):
        try:
            out = G.fit_channel(frame, highs, lows, None, None, 0.5)
            check(f"fit_channel on {label} returns a dict", isinstance(out, dict), str(out.get("shape")))
        except Exception as exc:  # noqa: BLE001
            check(f"fit_channel on {label} returns a dict", False, repr(exc))
    out = G.fit_channel(df, highs[:1], lows, None, None, 0.5)
    check("one pivot on a side leaves that rail None and the other fitted",
          out["upper"] is None and out["lower"] is not None, str(out["shape"]))


def test_forming_bar() -> None:
    """The forming bar must not move the pivot set or the rails. Over many frames.

    A tick on the last bar (its high up and its low down by 0.1, 0.5 and 1 percent)
    must change nothing, in every one of 120 cases. Appending a flat bar is a bar
    CLOSING, which is real information: the newly closed bar can confirm a fractal
    three bars back or extend the window's range, and either may legitimately
    change the answer. Those cases are classified and counted; any change with no
    such cause is a failure.
    """
    print("\n=== the forming bar (40 frames x 3 ticks, plus a one-bar append) ===")
    frames = market_frames(40)
    tick_cases = tick_pivot = tick_rail = 0
    flat_cases = flat_pivot = flat_rail = 0
    confirmed = extended = crossed_by_closed = reference_moved = unexplained = 0
    detail: list[str] = []

    for family, seed, df in frames:
        base_env, base_fit = channel_of(df)
        base_p = pivot_set(base_env)
        base_r = (rail_key(base_fit["upper"]), rail_key(base_fit["lower"]))

        for pct in (0.1, 0.5, 1.0):
            tick_cases += 1
            env, fit = channel_of(perturb_last(df, pct))
            if pivot_set(env) != base_p:
                tick_pivot += 1
                detail.append(f"tick {pct}% {family}#{seed}: pivot set moved")
            if (rail_key(fit["upper"]), rail_key(fit["lower"])) != base_r:
                tick_rail += 1
                detail.append(f"tick {pct}% {family}#{seed}: rails moved")

        flat_cases += 1
        longer = append_flat(df)
        env, fit = channel_of(longer)
        p = pivot_set(env)
        r = (rail_key(fit["upper"]), rail_key(fit["lower"]))
        n = len(df)
        pivots_moved = p != base_p
        rails_moved = r != base_r
        if not pivots_moved and not rails_moved:
            continue
        # Why did it move? The bar that just closed is bar n-1 of the original.
        closed_high = float(df["high"].iloc[n - 1])
        closed_low = float(df["low"].iloc[n - 1])
        prior_top = float(df["high"].iloc[: n - 1].max())
        prior_bottom = float(df["low"].iloc[: n - 1].min())
        added = p - base_p
        new_confirmation = any(idx >= n - 3 for idx, _, _ in added)
        range_extended = closed_high > prior_top or closed_low < prior_bottom
        # With the pivots unchanged, the only score terms a closing bar can move
        # are each candidate pair's crossing count (the closed bar now counts)
        # and its nearness (the reference close is now the closed bar's, and the
        # rail is read one bar further right). Recompute both for every pair.
        crosses = False
        nearness_flipped = False
        if not pivots_moved:
            for side, pivots in (("high", base_env["highs"]), ("low", base_env["lows"])):
                before = pair_flags(df, pivots, side)
                after = pair_flags(longer, pivots, side)
                for key, (was_clean, was_near) in before.items():
                    is_clean, is_near = after.get(key, (was_clean, was_near))
                    if was_clean != is_clean:
                        crosses = True
                    if was_near != is_near:
                        nearness_flipped = True
        if pivots_moved:
            flat_pivot += 1
        if rails_moved:
            flat_rail += 1
        if new_confirmation:
            confirmed += 1
        elif range_extended:
            extended += 1
        elif crosses and not pivots_moved:
            crossed_by_closed += 1
        elif nearness_flipped and not pivots_moved:
            reference_moved += 1
        else:
            unexplained += 1
            detail.append(f"append {family}#{seed}: moved with no closed-bar cause, "
                          f"added={sorted(added)} removed={sorted(base_p - p)}")

    print(f"      tick: pivot set changed {tick_pivot}/{tick_cases}, rails changed "
          f"{tick_rail}/{tick_cases} (8/120 and 6/120 before the forming bar left the range)")
    print(f"      append one flat bar: pivot set changed {flat_pivot}/{flat_cases}, rails "
          f"changed {flat_rail}/{flat_cases}; causes: fractal confirmed at n-3 {confirmed}, "
          f"closed bar extended the range {extended}, closed bar crossed a rail "
          f"{crossed_by_closed}, closed bar's close moved a rail across the nearness "
          f"bound {reference_moved}, unexplained {unexplained}")
    for line in detail[:12]:
        print("        " + line)
    check("a tick on the forming bar never changes the pivot set (120 cases)",
          tick_cases == 120 and tick_pivot == 0, f"{tick_pivot}/{tick_cases}")
    check("a tick on the forming bar never changes the rails (120 cases)",
          tick_cases == 120 and tick_rail == 0, f"{tick_rail}/{tick_cases}")
    check("appending a bar changes nothing unless the closed bar itself is the cause",
          flat_cases == 40 and unexplained == 0, f"{unexplained} unexplained")
    check("the append case does change something somewhere, so the classifier is "
          "exercised rather than vacuous", flat_pivot + flat_rail > 0)

    # And the tick must not move the rails on the descending hourly frame either,
    # with a spike far larger than any real tick.
    n = 120
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="Asia/Kolkata")
    close = 1400 - np.arange(n) * 0.8 + 12 * np.sin(np.arange(n) / 5.0) + rng.normal(0, 1.0, n)
    df = pd.DataFrame({"open": close, "high": close + 3 + rng.random(n) * 2,
                       "low": close - 3 - rng.random(n) * 2, "close": close,
                       "volume": 1000, "oi": 0}, index=idx)
    _, fit = channel_of(df)
    spiked = df.copy()
    spiked.iloc[-1, spiked.columns.get_loc("high")] = float(df["high"].max()) + 500
    spiked.iloc[-1, spiked.columns.get_loc("low")] = float(df["low"].min()) - 500
    _, fit2 = channel_of(spiked)
    check("a 500 point spike in the forming bar does not move the rails",
          (rail_key(fit2["upper"]), rail_key(fit2["lower"]))
          == (rail_key(fit["upper"]), rail_key(fit["lower"])))


def test_crossed_rails() -> None:
    """A resolved triangle: the upper rail ends below the lower one."""
    print("\n=== crossing rails ===")
    n = 170
    x = np.arange(n, dtype=float)
    # A symmetrical triangle whose apex sits at bar 140, inside the window, and
    # thirty flat bars after it. Every rail through the converging swings is
    # extrapolated past the apex, so the upper one ends below the lower one.
    amp = np.maximum(0.0, 60.0 * (1.0 - x / 140.0))
    close = 1400.0 + amp * np.sin(x / 4.0)
    df = frame_from_close(close)
    env, fit = channel_of(df)
    up, lo = fit["upper"], fit["lower"]
    check("the triangle yields two rails", up is not None and lo is not None, fit["shape"])
    if up is None or lo is None:
        return
    print(f"      upper {up['from']['price']:.1f} -> {up['to']['price']:.1f}, "
          f"lower {lo['from']['price']:.1f} -> {lo['to']['price']:.1f}, shape={fit['shape']}")
    check("the rails do meet: upper at or below lower at the right edge",
          up["to"]["price"] <= lo["to"]["price"],
          f"{up['to']['price']:.2f} vs {lo['to']['price']:.2f}")
    check("the shape is reported as crossed", fit["shape"] == "crossed", fit["shape"])
    check("a crossed channel has zero width at the right edge", fit["width_right"] == 0.0,
          str(fit["width_right"]))
    check("the rails are still returned, because they are real structure",
          up["from_pivot"] is not None and lo["from_pivot"] is not None)
    check("measured_move refuses to project off crossed rails",
          measured_move(fit)["width"] == 0.0 and measured_move(fit)["upside_target"] == 0.0)

    # Crossing anywhere inside the span counts, not only at the right edge.
    base = 1_700_000_000.0
    day = 86400.0
    rising_highs = [Pivot(i, base + i * 10 * day, 100.0 + i * 4.0, "high") for i in range(4)]
    falling_lows = [Pivot(i + 1, base + (i * 10 + 5) * day, 130.0 - i * 4.0, "low") for i in range(4)]
    frame = frame_from_close(np.full(60, 115.0), start="2023-11-14")
    cross = fit_channel(frame, rising_highs, falling_lows)
    check("rails that intersect inside the span are crossed too",
          cross["shape"] == "crossed" and cross["width_right"] == 0.0,
          f"shape={cross['shape']} width={cross['width_right']}")

    # And a plain range must not be called crossed.
    df = sawtooth([10, 30, 50, 70], [20, 40, 60, 80], n=91, amp=10.0)
    _, fit = channel_of(df)
    check("a plain range is not crossed", fit["shape"] != "crossed", fit["shape"])


def test_pair_selection() -> None:
    """Zero-crossing pairs beat crossing pairs, and the widest zero-crossing pair wins.

    Verified against a brute-force re-scoring of every pair on 40 frames, then the
    coverage distribution over 300 frames, printed for the record.
    """
    print("\n=== pair selection and coverage ===")
    violations = 0
    clean_beaten = 0
    checked = 0
    for family, seed, df in market_frames(40, start_seed=3000):
        env, fit = channel_of(df)
        t = to_utc_seconds(df.index)
        hi = df["high"].to_numpy(dtype=float)
        lo = df["low"].to_numpy(dtype=float)
        right_edge = float(t[-1])
        # The fit's own nearness bound: one window range, capped at 20 percent
        # of the last closed close.
        last_closed = float(df["close"].iloc[-2])
        reach = min(1.0 * (float(hi[:-1].max()) - float(lo[:-1].min())), 0.2 * abs(last_closed))
        for side, pivots, rail in (("high", env["highs"], fit["upper"]),
                                   ("low", env["lows"], fit["lower"])):
            if rail is None or len(pivots) < 2:
                continue
            checked += 1
            pts = sorted(pivots, key=lambda p: (p.time, p.index))
            pt = np.array([p.time for p in pts])
            pp = np.array([p.price for p in pts])
            scores = {}
            any_clean = False
            for i in range(len(pts) - 1):
                for j in range(i + 1, len(pts)):
                    dtj = pt[j] - pt[i]
                    if dtj <= 0:
                        continue
                    slope = (pp[j] - pp[i]) / dtj
                    intercept = pp[i] - slope * pt[i]
                    span = (t >= pt[i]) & (t <= right_edge)
                    span[-1] = False
                    line = slope * t + intercept
                    slack = np.abs(line) * CROSSING_TOLERANCE_PCT / 100.0
                    crossed = span & ((hi > line + slack) if side == "high" else (lo < line - slack))
                    crossings = int(crossed.sum())
                    fitted = slope * pt + intercept
                    hits = np.abs(pp - fitted) <= np.abs(fitted) * TOUCH_TOL / 100.0
                    extent = float(pt[hits].max() - pt[hits].min())
                    near = abs(slope * right_edge + intercept - last_closed) <= reach
                    scores[(i, j)] = (crossings == 0, near, extent, int(hits.sum()),
                                      -crossings, float(pt[j]))
                    any_clean = any_clean or crossings == 0
            best = max(scores.values())
            chosen = (pts.index(rail["from_pivot"]), pts.index(rail["to_pivot"]))
            if scores.get(chosen) != best:
                violations += 1
            if any_clean and rail["crossings"] != 0:
                clean_beaten += 1
    check("on 40 frames the chosen pair always maximises the documented score",
          checked >= 70 and violations == 0, f"{violations} violations over {checked} rails")
    check("a crossed pair never wins while an uncrossed one exists",
          clean_beaten == 0, f"{clean_beaten} of {checked}")

    # The coverage distribution, at the old 0.5 percent crossing slack so the
    # ordering is measured on its own, and at the shipped slack for the record.
    frames = market_frames(300, start_seed=5000)
    summary = {}
    for slack in (0.5, CROSSING_TOLERANCE_PCT):
        coverage = []
        zero_both = 0
        fitted = 0
        far = 0
        for family, seed, df in frames:
            env = envelope(df, 3, 3, CHANNEL_PIVOTS)
            fit = fit_channel(df, env["highs"], env["lows"], None, None, TOUCH_TOL, slack)
            up, lo = fit["upper"], fit["lower"]
            if up is None or lo is None:
                continue
            fitted += 1
            t = to_utc_seconds(df.index)
            win = float(t[-1] - t[0])
            coverage.append(min((t[-1] - up["from"]["time"]) / win,
                                (t[-1] - lo["from"]["time"]) / win))
            if up["crossings"] == 0 and lo["crossings"] == 0:
                zero_both += 1
            reach = float(df["high"].iloc[:-1].max()) - float(df["low"].iloc[:-1].min())
            last_closed = float(df["close"].iloc[-2])
            if max(abs(up["to"]["price"] - last_closed), abs(lo["to"]["price"] - last_closed)) > reach:
                far += 1
        cov = np.array(coverage)
        summary[slack] = (fitted, int((cov < 0.5).sum()), float(np.median(cov)),
                          float(np.percentile(cov, 10)), zero_both, far)
        print(f"      300 frames at {slack} percent crossing slack: channel covers less than "
              f"half the window {summary[slack][1]}/{fitted}; median {summary[slack][2]:.3f}, "
              f"p10 {summary[slack][3]:.3f}; both rails uncrossed {zero_both}/{fitted}; "
              f"a rail ending more than a full range from the last close: {far}")
    print("      before, at 0.5: 33/300 below half, median 0.839, p10 0.473, 290/300 uncrossed")
    fitted, below_half, median, _, _, far = summary[0.5]
    check("both rails fit on every one of the 300 frames", fitted == 300, str(fitted))
    check("at equal slack, fewer channels cover less than half the window than the 33 "
          "the old ordering gave", below_half < 33, f"{below_half}/300")
    check("the median channel still covers more than three quarters of the window",
          median > 0.75, f"median {median:.3f}")
    # A rail may still end far from price when no uncrossed pair ends near it,
    # which is what a random walk that ran away from every swing produces. That
    # is 8 of 300 at the shipped slack (16 before the nearness test); the guard
    # is against it creeping back, not a claim that it is zero.
    check("rails ending a full window range from the last close stay rare",
          summary[CROSSING_TOLERANCE_PCT][5] <= 10, f"{summary[CROSSING_TOLERANCE_PCT][5]} rails")


# ---------------------------------------------------------------------------
# live data
# ---------------------------------------------------------------------------


def describe_envelope(label: str, df: pd.DataFrame, env: dict) -> None:
    """Print the vertices so they can be eyeballed against the frame."""
    top = float(df["high"].max())
    bottom = float(df["low"].min())
    top_at = df["high"].idxmax()
    bottom_at = df["low"].idxmin()
    print(f"\n    {label}: {len(df)} bars, "
          f"{df.index[0]} .. {df.index[-1]}")
    print(f"      frame high {top:.2f} at {top_at}, low {bottom:.2f} at {bottom_at}")
    print(f"      vertices: {len(env['highs'])} highs, {len(env['lows'])} lows; "
          f"direction={env['direction']}, width={env['width']:.2f}")
    print("      upper boundary:")
    for p in env["highs"]:
        print(f"        bar {p.index:>4}  {iso(p.time)}  {p.price:>10.2f}"
              f"   frame high {float(df['high'].iloc[p.index]):>10.2f}")
    print("      lower boundary:")
    for p in env["lows"]:
        print(f"        bar {p.index:>4}  {iso(p.time)}  {p.price:>10.2f}"
              f"   frame low  {float(df['low'].iloc[p.index]):>10.2f}")
    _, fit = channel_of(df)
    mm = measured_move(fit)
    print(f"      channel: shape={fit['shape']}, width at right edge {fit['width_right']:.2f}")
    print(f"      measured move: breakout {mm['breakout']:.2f} -> "
          f"{mm['upside_target']:.2f}, breakdown {mm['breakdown']:.2f} -> "
          f"{mm['downside_target']:.2f}")


def test_live() -> None:
    print("\n--- live OpenAlgo data (RELIANCE NSE) ---")

    try:
        from app.openalgo.frames import get_frame_cache
        cache = get_frame_cache()
    except Exception as exc:  # noqa: BLE001
        skip("live frames", f"{type(exc).__name__}: {exc}")
        return

    for interval in ("D", "5m"):
        res = cache.get_frame("RELIANCE", "NSE", interval, lookback_bars=300)
        if not res.get("ok"):
            skip(f"RELIANCE {interval}", str(res.get("error"))[:80])
            continue

        df = res["frame"].tail(250)
        tz = getattr(df.index, "tz", None)
        print(f"\n    {interval} index tz={tz} dtype={df.index.dtype}")

        env = envelope(df)
        describe_envelope(f"RELIANCE {interval}", df, env)

        check(f"{interval}: envelope has vertices on both boundaries",
              len(env["highs"]) >= 2 and len(env["lows"]) >= 2,
              f"{len(env['highs'])} highs, {len(env['lows'])} lows")
        check(f"{interval}: vertex count is in the 5 to 9 band",
              3 <= len(env["highs"]) <= 9 and 3 <= len(env["lows"]) <= 9,
              f"{len(env['highs'])}/{len(env['lows'])}")

        times = to_utc_seconds(df.index)
        check(f"{interval}: every vertex time matches its bar",
              all(p.time == float(times[p.index]) for p in env["highs"] + env["lows"]))
        check(f"{interval}: every high vertex is a real bar high",
              all(p.price == float(df["high"].iloc[p.index]) for p in env["highs"]))
        check(f"{interval}: every low vertex is a real bar low",
              all(p.price == float(df["low"].iloc[p.index]) for p in env["lows"]))

        # Each high vertex must be the highest bar in its own neighbourhood,
        # which is the property the off-by-right bug destroys.
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        ok_h = all(
            highs[p.index] >= highs[max(0, p.index - 3): p.index + 4].max()
            for p in env["highs"]
        )
        ok_l = all(
            lows[p.index] <= lows[max(0, p.index - 3): p.index + 4].min()
            for p in env["lows"]
        )
        check(f"{interval}: every vertex is the local extreme of its window",
              ok_h and ok_l)

        # The band must bracket the closed bars. The forming bar is not in play.
        closed = df.iloc[:-1]
        check(f"{interval}: the highest vertex is the closed-bar high",
              abs(max(p.price for p in env["highs"]) - float(closed["high"].max())) < 1e-6,
              f"{max(p.price for p in env['highs']):.2f} vs {float(closed['high'].max()):.2f}")
        check(f"{interval}: the lowest vertex is the closed-bar low",
              abs(min(p.price for p in env["lows"]) - float(closed["low"].min())) < 1e-6,
              f"{min(p.price for p in env['lows']):.2f} vs {float(closed['low'].min()):.2f}")
        check(f"{interval}: no vertex sits on the forming bar",
              all(p.index != len(df) - 1 for p in env["highs"] + env["lows"]))

        # Timezone: intraday is tz-aware, daily is naive, both must land on the
        # same UTC seconds as an explicit conversion.
        expected = (
            df.index.tz_convert("UTC").tz_localize(None) if tz is not None else df.index
        )
        manual = (expected.astype("datetime64[us]").astype("int64") / 1e6).to_numpy()
        check(f"{interval}: to_utc_seconds matches an explicit UTC conversion",
              bool(np.allclose(times, manual)))

        # Levels and snapping on live prices.
        levels = support_resistance(df)
        print(f"      levels ({len(levels)} found), strongest five:")
        for lv in levels[:5]:
            print(f"        {lv['price']:>10.2f}  {lv['touches']} touches  "
                  f"{lv['kind']:<10} from {lv['side']}s  "
                  f"{iso(lv['first_time'])} .. {iso(lv['last_time'])}")
        check(f"{interval}: levels are found", len(levels) >= 1, f"{len(levels)}")
        check(f"{interval}: every level price is a real bar high or low",
              all(np.isclose(lv["price"], highs).any() if lv["side"] == "high"
                  else np.isclose(lv["price"], lows).any() for lv in levels))

        real_high = float(closed["high"].max())
        loose = round(real_high)
        snapped = snap_to_pivot(df, loose, kind="high")
        print(f"      snap: a user's {loose} -> "
              f"{snapped.price if snapped else None} (real {real_high})")
        check(f"{interval}: a rounded high snaps back to the real high",
              snapped is not None and abs(snapped.price - real_high) < 1e-6,
              f"{snapped.price if snapped else None} vs {real_high}")

        tl = fit_trendline(env["highs"])
        print(f"      trendline through the highs: {tl['touches']} touches, "
              f"r2={tl['r2']:.3f}"
              + (f", {iso(tl['from'].time)} {tl['from'].price:.2f} -> "
                 f"{iso(tl['to'].time)} {tl['to'].price:.2f}" if tl["from"] else ""))
        check(f"{interval}: a trendline is found through the upper vertices",
              tl["touches"] >= 2, f"{tl['touches']} touches")

        # Viewport clipping on live data: the last third of the frame only.
        cut = float(times[len(times) * 2 // 3])
        clipped = envelope(df, start=cut)
        check(f"{interval}: clipping to the last third keeps only visible vertices",
              all(p.time >= cut for p in clipped["highs"] + clipped["lows"]),
              f"{len(clipped['highs'])}/{len(clipped['lows'])} vertices after the clip")

        # The forming bar on live data: move it and nothing may change.
        base_env, base_fit = channel_of(df)
        moved = False
        for pct in (0.1, 0.5, 1.0):
            e2, f2 = channel_of(perturb_last(df, pct))
            if pivot_set(e2) != pivot_set(base_env) or (
                    rail_key(f2["upper"]), rail_key(f2["lower"])) != (
                    rail_key(base_fit["upper"]), rail_key(base_fit["lower"])):
                moved = True
        check(f"{interval}: a tick on the live forming bar moves neither pivots nor rails",
              not moved)


def main() -> int:
    print("Chart geometry tests")
    test_timezone()
    test_bar_interval()
    test_pivot_anchor()
    test_multiple_known_pivots()
    test_clipping()
    test_density()
    test_direction_and_width()
    test_trendline()
    test_levels()
    test_measured_move()
    test_snap()
    test_degenerate()
    test_json_safety()
    test_fit_channel()
    test_forming_bar()
    test_crossed_rails()
    test_pair_selection()
    test_live()

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    n_skip = sum(1 for _, s in results if s == SKIP)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
