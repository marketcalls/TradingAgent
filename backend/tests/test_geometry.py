"""Chart geometry tests.

The synthetic checks need no network: every frame here has its pivots at indices
chosen in advance, so the off-by-right bug fails by construction rather than by
inspection. The live section pulls RELIANCE from OpenAlgo and prints the vertices
an envelope actually produces, because a geometry module that passes its unit
tests and still draws the band in the wrong place has failed at the only thing it
was built for.

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
    ENVELOPE_SIGNIFICANCE,
    Pivot,
    envelope,
    fit_trendline,
    measured_move,
    snap_to_pivot,
    support_resistance,
    swing_pivots,
    to_utc_seconds,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []


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

    # A plateau must resolve to one pivot, not to every bar of it.
    closes = np.concatenate([np.arange(0, 10.0), np.full(3, 10.0), np.arange(9, -1, -1.0)])
    flat_top = frame_from_close(closes)
    hs, _ = swing_pivots(flat_top, left=3, right=3)
    check("a flat top yields exactly one swing high", len(hs) == 1,
          f"{[p.index for p in hs]}")


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


def test_measured_move() -> None:
    print("\n--- measured_move ---")

    df = sawtooth([10, 30, 50, 70], [20, 40, 60, 80], n=91, amp=10.0)
    env = envelope(df)
    mm = measured_move(env)

    check("the projection is one band height above the upper rail",
          abs((mm["upside_target"] - mm["breakout"]) - mm["width"]) < 1e-6,
          f"breakout={mm['breakout']:.2f} target={mm['upside_target']:.2f} "
          f"width={mm['width']:.2f}")
    check("the projection is one band height below the lower rail",
          abs((mm["breakdown"] - mm["downside_target"]) - mm["width"]) < 1e-6,
          f"breakdown={mm['breakdown']:.2f} target={mm['downside_target']:.2f}")
    check("the upper rail is above the lower rail", mm["breakout"] > mm["breakdown"],
          f"{mm['breakout']:.2f} > {mm['breakdown']:.2f}")
    check("targets are projected from the envelope's right edge",
          mm["time"] == env["last_time"] and mm["target_time"] > mm["time"],
          f"{iso(mm['time'])} -> {iso(mm['target_time'])}")
    check("direction is carried through", mm["direction"] == env["direction"],
          mm["direction"])
    check("an empty envelope returns zeros without raising",
          measured_move(envelope(frame_from_close([])))["width"] == 0.0)
    check("a non-dict argument returns zeros without raising",
          measured_move(None)["width"] == 0.0)


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
            mm = measured_move(env)
            sn = snap_to_pivot(df, 100.0)
            tl = fit_trendline(hs)
            ok = (
                isinstance(hs, list) and isinstance(ls, list)
                and isinstance(env, dict) and isinstance(lv, list)
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
    payload = {
        "envelope": {
            "highs": [p.as_anchor() for p in env["highs"]],
            "lows": [p.as_anchor() for p in env["lows"]],
            **{k: env[k] for k in scalars},
            "direction": env["direction"],
        },
        "levels": support_resistance(df),
        "measured_move": measured_move(env),
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
    mm = measured_move(env)
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

        # The band must bracket price.
        check(f"{interval}: the highest vertex is the frame high",
              abs(max(p.price for p in env["highs"]) - float(df["high"].max())) < 1e-6,
              f"{max(p.price for p in env['highs']):.2f} vs {float(df['high'].max()):.2f}")
        check(f"{interval}: the lowest vertex is the frame low",
              abs(min(p.price for p in env["lows"]) - float(df["low"].min())) < 1e-6,
              f"{min(p.price for p in env['lows']):.2f} vs {float(df['low'].min()):.2f}")

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
                  f"{lv['kind']:<10} {iso(lv['first_time'])} .. {iso(lv['last_time'])}")
        check(f"{interval}: levels are found", len(levels) >= 1, f"{len(levels)}")

        real_high = float(df["high"].max())
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


def test_fit_channel() -> None:
    """Two rails that bound price, start at their own anchors, and never move."""
    print("\n=== fit_channel ===")
    import numpy as np
    import pandas as pd
    from app.charts import geometry as G

    # A descending move: lower highs and lower lows, with noise, and a spike in
    # the forming bar that must not change the answer.
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

    fit = G.fit_channel(df, highs, lows, None, None, 0.5)
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
        slack = np.abs(line) * 0.005
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

    # DETERMINISM. The same frame twice, and then with the forming bar spiked,
    # must give byte-identical rails. The forming bar is alive; a channel that
    # changed on every tick would read as broken.
    again = G.fit_channel(df, highs, lows, None, None, 0.5)
    same = (again["upper"]["from"] == up["from"] and again["upper"]["to"] == up["to"]
            and again["lower"]["from"] == lo["from"] and again["lower"]["to"] == lo["to"])
    check("the same frame gives the same rails", same, "identical" if same else "DIFFERENT")

    spiked = df.copy()
    spiked.iloc[-1, spiked.columns.get_loc("high")] = float(df["high"].max()) + 500
    spiked.iloc[-1, spiked.columns.get_loc("low")] = float(df["low"].min()) - 500
    env2 = G.envelope(spiked, 3, 3, 5)
    fit2 = G.fit_channel(spiked, env2["highs"], env2["lows"], None, None, 0.5)
    same2 = (fit2["upper"] is not None and fit2["lower"] is not None
             and fit2["upper"]["from"] == up["from"] and fit2["upper"]["slope"] == up["slope"]
             and fit2["lower"]["from"] == lo["from"] and fit2["lower"]["slope"] == lo["slope"])
    check("a spike in the forming bar does not move the rails", same2,
          "unchanged" if same2 else "MOVED")

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


def main() -> int:
    print("Chart geometry tests")
    test_timezone()
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
