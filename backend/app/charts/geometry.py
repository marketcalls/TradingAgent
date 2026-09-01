"""Chart geometry: swing pivots, envelopes, trendlines, levels and snapping.

The analyst model narrates the numbers this module returns. It never invents an
anchor. When a user says "draw the channel for the move from 4446 to 3235", the
geometry decides where the lines go and the model reads them out, so a wrong
pivot here ships as a confident wrong answer. Accuracy is the whole point.

Runtime facts this module is built around, verified against the live OpenAlgo
instance with RELIANCE NSE:

  - THE TIMEZONE SPLIT. The OpenAlgo SDK localises intraday intervals to
    Asia/Kolkata and leaves D, W and M timezone-naive. Measured on the live
    server: a daily frame arrives with index dtype ``datetime64[s]`` and
    ``tz=None``, a 5m frame with ``datetime64[s, Asia/Kolkata]``. The chart
    engine has exactly one internal time model, UTC seconds, so every index
    goes through :func:`to_utc_seconds` once and nothing downstream ever sees a
    timezone. A naive index is treated as UTC-derived, because the server built
    it from an epoch. Every time this module returns is a float of UTC seconds.
    Never milliseconds, never a bar index.

  - THE PIVOT ANCHOR RULE. A pivot is only confirmed ``right`` bars after it
    occurs. The tempting implementation records the price at the bar that
    confirmed it, which is what OpenAlgo's own zones_with_draws.js does, and
    every shape it draws then sits ``right`` bars too far to the right. On an
    envelope that shears the whole band sideways, so the boundary no longer
    touches the highs it claims to touch. ``Pivot.index`` and ``Pivot.time``
    here always name the bar where the extreme actually occurred, never the bar
    that confirmed it. ``backend/tests/test_geometry.py`` pins this with
    synthetic frames whose pivots sit at known indices, so the naive version
    fails by construction.

  - DENSITY IS A FEATURE, NOT A SIDE EFFECT. A raw fractal detector on a short
    lookback returns a sawtooth that reads as noise rather than structure.
    ``significance`` prunes pivots whose retracement from the neighbouring
    opposite pivot is smaller than that fraction of the window's price range,
    using the classic zigzag walk, which keeps the true extreme of a long leg
    instead of the first wiggle after it. ``max_points`` then keeps the most
    significant vertices per side. Measured over six NSE names on 250 daily
    bars, the tuned default lands 5 to 9 vertices per boundary against 21 to 27
    raw fractals.

Input frames come from ``backend/app/openalgo/frames.py``: columns open, high,
low, close, volume, oi, a DatetimeIndex named timestamp, sorted ascending, no
duplicates, no NaN in OHLC. Nothing here raises on a degenerate frame. An empty
frame, a flat series, a three bar series and a single pivot series all return
empty or zeroed results, because a drawing tool that throws on a quiet chart is
worse than one that draws nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

__all__ = [
    "Pivot",
    "to_utc_seconds",
    "swing_pivots",
    "envelope",
    "fit_trendline",
    "support_resistance",
    "measured_move",
    "snap_to_pivot",
    "DEFAULT_LEFT",
    "DEFAULT_RIGHT",
    "ENVELOPE_SIGNIFICANCE",
]

#: Fractal half-widths. Three bars either side is the smallest window that
#: ignores a single outlier bar while still catching a two day reversal on a
#: daily frame.
DEFAULT_LEFT = 3
DEFAULT_RIGHT = 3

#: Starting retracement threshold for :func:`envelope`, as a fraction of the
#: window's high to low range. Tuned by sweeping 0.10 to 0.14 over the last 250
#: bars of RELIANCE, SBIN, INFY, TCS, HDFCBANK and ITC on both D and 5m. Raw
#: fractals give 21 to 27 vertices per side, which draws as a sawtooth. At 0.12
#: every daily frame in that set lands between 5 and 9 vertices per boundary and
#: ``max_points`` only binds on the choppiest names. At 0.10 the cap does the
#: work instead, at 0.14 clean trends thin out too far.
ENVELOPE_SIGNIFICANCE = 0.12

#: Vertices per boundary that :func:`envelope` tries to reach before it stops
#: relaxing the threshold.
ENVELOPE_MIN_VERTICES = 5

#: Why a ladder and not a single threshold. A fraction of the window range is the
#: wrong yardstick on a clean trend: when a stock runs 100 points with 6 point
#: pullbacks, the range is dominated by the trend, the threshold lands above
#: every retracement, and the band collapses to a single vertex. Measured on a
#: 200 bar synthetic trend, a flat 0.12 returned 1 high and 0 lows. So the
#: threshold starts at :data:`ENVELOPE_SIGNIFICANCE` and steps down until both
#: boundaries carry :data:`ENVELOPE_MIN_VERTICES`, which keeps the strictest
#: threshold the market will actually support. The last rung is 0, which still
#: alternates high and low but prunes nothing.
_SIGNIFICANCE_LADDER = (1.0, 0.75, 0.5, 0.35, 0.2, 0.1, 0.0)

#: Snapping uses a looser fractal than drawing does. A user's remembered number
#: is often a minor swing that a 3 bar window would discard.
SNAP_LEFT = 2
SNAP_RIGHT = 2

#: Two pivots are equally close for snapping purposes when their distances to
#: the user's number differ by less than this percentage of it. Measured on live
#: RELIANCE 5m: a user's "1308" sits 0.20 from a minor swing high at 1307.80 and
#: 0.40 from the frame's actual high at 1308.40. Nearest alone picks the minor
#: swing. Inside the band the more prominent pivot wins, which is the one the
#: user was reading off the chart.
_SNAP_TIE_PCT = 0.05

#: Pair search in :func:`fit_trendline` is quadratic. Beyond this many pivots the
#: most recent ones are used, because an old pivot rarely defines a live line.
_MAX_TRENDLINE_PIVOTS = 40

#: Trend versus range cutoff. The band must climb or fall by at least this much
#: of its own height across its span before it is called a trend.
_TREND_RATIO = 0.5

Kind = Literal["high", "low"]


@dataclass(frozen=True)
class Pivot:
    """One confirmed swing point.

    Attributes:
        index: Bar index in the frame the pivot was detected on. This is the bar
            where the extreme occurred, not the bar that confirmed it.
        time: UTC seconds for that same bar.
        price: The extreme itself, the bar's high for a swing high and its low
            for a swing low.
    """

    index: int
    time: float
    price: float

    def as_anchor(self) -> dict[str, float]:
        """Render the pivot as a wire anchor.

        Returns:
            A dict with ``time`` and ``price``, matching ``contract.Anchor``.
        """
        return {"time": self.time, "price": self.price}


@dataclass(frozen=True)
class _Cand:
    """A pivot plus the side it belongs to, used while pruning."""

    index: int
    time: float
    price: float
    kind: str

    def to_pivot(self) -> Pivot:
        return Pivot(index=self.index, time=self.time, price=self.price)


def to_utc_seconds(index: Any) -> np.ndarray:
    """Normalise any bar index to UTC seconds.

    This is the single normalisation point for the whole chart stack. OpenAlgo
    hands back intraday frames localised to Asia/Kolkata and daily, weekly and
    monthly frames timezone-naive, so both cases have to land on the same
    number for the same instant. A tz-aware index is converted to UTC. A naive
    index is treated as already UTC, because the server derived it from an
    epoch rather than from a wall clock.

    Args:
        index: A ``pd.DatetimeIndex``, or anything ``pd.DatetimeIndex`` accepts,
            such as a Series of timestamps or a list of ISO strings.

    Returns:
        A float64 array of UTC seconds since the epoch, the same length as the
        input. NaT becomes NaN. An empty or unparseable input returns an empty
        array rather than raising.
    """
    if index is None:
        return np.empty(0, dtype=float)

    if isinstance(index, pd.DatetimeIndex):
        idx = index
    else:
        try:
            idx = pd.DatetimeIndex(index)
        except (TypeError, ValueError):
            try:
                idx = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce"))
            except (TypeError, ValueError):
                return np.empty(0, dtype=float)

    if len(idx) == 0:
        return np.empty(0, dtype=float)

    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)

    # Microsecond resolution keeps sub-second bars honest and, unlike
    # nanoseconds, cannot overflow int64 on a far dated weekly frame.
    values = np.asarray(idx.values, dtype="datetime64[us]")
    seconds = values.astype("int64").astype(float) / 1_000_000.0
    nat = np.isnat(values)
    if nat.any():
        seconds = seconds.copy()
        seconds[nat] = np.nan
    return seconds


def _column(df: pd.DataFrame, name: str) -> np.ndarray:
    """Pull one price column as float64, falling back to close then to NaN."""
    for candidate in (name, "close"):
        if candidate in df.columns:
            try:
                return np.asarray(df[candidate].to_numpy(), dtype=float)
            except (TypeError, ValueError):
                continue
    return np.full(len(df), np.nan, dtype=float)


def _fractal_indices(
    values: np.ndarray, left: int, right: int, kind: Kind
) -> np.ndarray:
    """Bar indices whose value is the extreme of its own neighbourhood.

    The returned index is the centre of the window, which is the bar where the
    extreme actually occurred. Returning the window's right edge instead is the
    off-by-right bug this module exists to avoid.

    Args:
        values: Highs for a swing high search, lows for a swing low search.
        left: Bars required to the left.
        right: Bars required to the right before the pivot is confirmed.
        kind: "high" or "low".

    Returns:
        An int64 array of bar indices, ascending. Empty when the frame is
        shorter than the window.
    """
    n = int(values.size)
    width = left + right + 1
    if n < width or width < 1:
        return np.empty(0, dtype=np.int64)

    win = sliding_window_view(np.ascontiguousarray(values), width)
    centre = win[:, left]
    ok = np.isfinite(centre)

    if kind == "high":
        if left:
            ok &= centre >= win[:, :left].max(axis=1)
        if right:
            # Strict on the right so a plateau resolves to one pivot, its last
            # bar, rather than to every bar of the plateau.
            ok &= centre > win[:, left + 1:].max(axis=1)
    else:
        if left:
            ok &= centre <= win[:, :left].min(axis=1)
        if right:
            ok &= centre < win[:, left + 1:].min(axis=1)

    return np.nonzero(ok)[0].astype(np.int64) + left


def _window(
    times: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    start: float | None,
    end: float | None,
) -> dict[str, Any]:
    """Clip the frame to the visible range and measure it.

    Returns:
        A dict with ``mask`` (bool array over all bars), ``range``,
        ``first_time``, ``last_time``, ``last_close`` and ``count``. All scalars
        are zero when nothing falls inside the range.
    """
    n = int(times.size)
    empty = {
        "mask": np.zeros(n, dtype=bool),
        "range": 0.0,
        "first_time": 0.0,
        "last_time": 0.0,
        "last_close": 0.0,
        "count": 0,
    }
    if n == 0:
        return empty

    mask = np.isfinite(times)
    if start is not None:
        mask &= times >= float(start)
    if end is not None:
        mask &= times <= float(end)
    if not mask.any():
        return empty

    wh = highs[mask]
    wl = lows[mask]
    wt = times[mask]
    wc = closes[mask]

    top = np.nanmax(wh) if np.isfinite(wh).any() else np.nan
    bottom = np.nanmin(wl) if np.isfinite(wl).any() else np.nan
    span = float(top - bottom) if np.isfinite(top) and np.isfinite(bottom) else 0.0

    last_close = 0.0
    finite_close = wc[np.isfinite(wc)]
    if finite_close.size:
        last_close = float(finite_close[-1])

    return {
        "mask": mask,
        "range": max(span, 0.0),
        "first_time": float(wt[0]),
        "last_time": float(wt[-1]),
        "last_close": last_close,
        "count": int(mask.sum()),
    }


def _zigzag(cands: list[_Cand], threshold: float) -> list[_Cand]:
    """Prune pivots whose retracement is smaller than ``threshold``.

    The classic zigzag walk. A same-side candidate replaces the running pivot
    when it is more extreme, so the true top of a long leg survives even when
    the wiggles either side of it are tiny. An opposite-side candidate is only
    accepted when the move away from the running pivot clears the threshold.

    Args:
        cands: Fractals from both sides, ascending by bar index.
        threshold: Minimum retracement in price units.

    Returns:
        A strictly alternating high, low, high sequence, ascending by index.
    """
    kept: list[_Cand] = []
    for cand in cands:
        if not kept:
            kept.append(cand)
            continue
        last = kept[-1]
        if cand.kind == last.kind:
            more_extreme = (
                cand.price > last.price if cand.kind == "high" else cand.price < last.price
            )
            if more_extreme:
                kept[-1] = cand
        elif abs(cand.price - last.price) >= threshold:
            kept.append(cand)
    return kept


def _amplitudes(kept: list[_Cand]) -> list[float]:
    """Score each pivot by its largest adjacent leg.

    The larger of the two neighbouring legs, not the smaller, because the top of
    a big rally followed by a shallow pullback is the most important point on
    the chart and a minimum would throw it away.
    """
    out: list[float] = []
    for i, pivot in enumerate(kept):
        before = abs(pivot.price - kept[i - 1].price) if i > 0 else 0.0
        after = abs(pivot.price - kept[i + 1].price) if i + 1 < len(kept) else 0.0
        out.append(max(before, after))
    return out


def _raw_candidates(
    df: pd.DataFrame,
    left: int,
    right: int,
    start: float | None,
    end: float | None,
) -> tuple[list[_Cand], dict[str, Any]]:
    """Every confirmed fractal inside the visible range, plus the range itself.

    Fractals are detected across the whole frame and only then clipped, so a
    pivot sitting a couple of bars inside the right edge of the viewport is
    still confirmed by bars the user cannot see. Significance, in contrast, is
    measured against the visible range, because what counts as a meaningful
    retracement depends on how far the user is zoomed in.

    Returns:
        The candidates ascending by index, and the window measurements.
    """
    n = len(df)
    if n == 0:
        return [], _window(np.empty(0), np.empty(0), np.empty(0), np.empty(0), start, end)

    times = to_utc_seconds(df.index)
    if times.size != n:
        times = np.arange(n, dtype=float)
    highs = _column(df, "high")
    lows = _column(df, "low")
    closes = _column(df, "close")

    win = _window(times, highs, lows, closes, start, end)
    mask = win["mask"]
    if win["count"] == 0:
        return [], win

    left = max(int(left), 0)
    right = max(int(right), 0)

    hi_idx = _fractal_indices(highs, left, right, "high")
    lo_idx = _fractal_indices(lows, left, right, "low")

    cands: list[_Cand] = []
    for i in hi_idx:
        if mask[i]:
            cands.append(_Cand(int(i), float(times[i]), float(highs[i]), "high"))
    for i in lo_idx:
        if mask[i]:
            cands.append(_Cand(int(i), float(times[i]), float(lows[i]), "low"))
    cands.sort(key=lambda c: (c.index, 0 if c.kind == "high" else 1))
    return cands, win


def _detect(
    df: pd.DataFrame,
    left: int,
    right: int,
    significance: float,
    start: float | None,
    end: float | None,
) -> tuple[list[_Cand], dict[str, Any]]:
    """Confirmed fractals, pruned by ``significance``.

    Returns:
        The kept candidates ascending by index, and the window measurements.
    """
    cands, win = _raw_candidates(df, left, right, start, end)
    if cands and significance and significance > 0.0 and win["range"] > 0.0:
        cands = _zigzag(cands, float(significance) * win["range"])
    return cands, win


def _side_counts(cands: list[_Cand]) -> tuple[int, int]:
    """Count highs and lows in a candidate list."""
    highs = sum(1 for c in cands if c.kind == "high")
    return highs, len(cands) - highs


def swing_pivots(
    df: pd.DataFrame,
    left: int = DEFAULT_LEFT,
    right: int = DEFAULT_RIGHT,
    significance: float = 0.0,
    start: float | None = None,
    end: float | None = None,
) -> tuple[list[Pivot], list[Pivot]]:
    """Confirmed swing highs and lows.

    A bar is a swing high when its high is the largest in the window of ``left``
    bars before it and ``right`` bars after it. The pivot is anchored to that
    bar, not to the bar ``right`` steps later that confirmed it.

    Args:
        df: Cleaned OHLCV frame with a DatetimeIndex.
        left: Bars required to the left of the extreme.
        right: Bars required to the right before the pivot is confirmed. The
            last ``right`` bars of a frame can never produce a pivot.
        significance: Retracement filter, as a fraction of the visible range.
            0.0 returns every confirmed fractal. Anything above 0 runs the
            zigzag prune and the result strictly alternates high, low, high.
        start: Optional lower bound in UTC seconds. Clips to the viewport.
        end: Optional upper bound in UTC seconds.

    Returns:
        ``(highs, lows)``, each ascending by bar index. Both are empty when the
        frame is shorter than ``left + right + 1`` or nothing survives the clip.
    """
    cands, _ = _detect(df, left, right, significance, start, end)
    highs = [c.to_pivot() for c in cands if c.kind == "high"]
    lows = [c.to_pivot() for c in cands if c.kind == "low"]
    return highs, lows


def _linfit(times: np.ndarray, prices: np.ndarray) -> tuple[float, float]:
    """Least squares line through points in data space.

    Time is centred before the fit, because UTC seconds are around 1.8e9 and an
    uncentred normal equation loses most of its precision on the intercept.

    Returns:
        ``(slope, intercept)`` such that ``price = slope * utc_seconds +
        intercept``. Slope is price units per second.
    """
    if times.size < 2:
        return 0.0, float(prices[0]) if prices.size else 0.0
    t0 = float(times.mean())
    dt = times - t0
    denom = float((dt * dt).sum())
    if denom <= 0.0:
        return 0.0, float(prices.mean())
    slope = float((dt * (prices - prices.mean())).sum() / denom)
    intercept = float(prices.mean() - slope * t0)
    return slope, intercept


def _fit_side(pivots: list[Pivot]) -> tuple[float, float]:
    """Least squares line through one boundary's vertices."""
    if not pivots:
        return 0.0, 0.0
    times = np.array([p.time for p in pivots], dtype=float)
    prices = np.array([p.price for p in pivots], dtype=float)
    return _linfit(times, prices)


def _keep_most_significant(
    cands: list[_Cand], kind: Kind, max_points: int
) -> list[Pivot]:
    """Take the ``max_points`` most significant pivots on one side.

    The window's own extreme is forced back in when the ranking dropped it. A
    band that misses the actual high of the move is the one error a trader spots
    instantly.

    Args:
        cands: The full alternating sequence, both sides.
        kind: "high" or "low".
        max_points: Cap per side. Zero or less means no cap.

    Returns:
        Pivots ascending by bar index.
    """
    amps = _amplitudes(cands)
    side = [(c, a) for c, a in zip(cands, amps) if c.kind == kind]
    if not side:
        return []

    if max_points > 0 and len(side) > max_points:
        ranked = sorted(side, key=lambda pair: (-pair[1], pair[0].index))[:max_points]
        extreme = (
            max(side, key=lambda pair: pair[0].price)
            if kind == "high"
            else min(side, key=lambda pair: pair[0].price)
        )
        if all(pair[0].index != extreme[0].index for pair in ranked):
            ranked[-1] = extreme
        side = ranked

    side.sort(key=lambda pair: pair[0].index)
    return [pair[0].to_pivot() for pair in side]


def envelope(
    df: pd.DataFrame,
    left: int = DEFAULT_LEFT,
    right: int = DEFAULT_RIGHT,
    max_points: int = 9,
    start: float | None = None,
    end: float | None = None,
) -> dict[str, Any]:
    """A closed band through the real swing points of the visible range.

    This is not a parallel channel. The upper boundary steps through the swing
    highs the detector found and the lower boundary through the swing lows, so
    the band bends the way the market did.

    Density is controlled twice. The retracement threshold starts at
    :data:`ENVELOPE_SIGNIFICANCE` and steps down the ladder until both
    boundaries carry :data:`ENVELOPE_MIN_VERTICES`, so a choppy market is
    thinned hard while a clean trend with shallow pullbacks still draws.
    ``max_points`` then caps whatever survives, keeping the most significant
    vertices per side.

    Args:
        df: Cleaned OHLCV frame with a DatetimeIndex.
        left: Bars required to the left of a pivot.
        right: Bars required to the right of a pivot.
        max_points: Vertex cap per boundary.
        start: Optional lower bound in UTC seconds.
        end: Optional upper bound in UTC seconds.

    Returns:
        A dict with:

        - ``highs``, ``lows``: lists of :class:`Pivot`, ascending by time. The
          caller reverses ``lows`` when closing the path.
        - ``slope_high``, ``slope_low``: least squares slopes of each boundary
          in price units per second.
        - ``direction``: "up", "down" or "sideways".
        - ``width``: the band's height in price units, measured between the two
          fitted boundaries at the midpoint of the span.
        - ``first_time``, ``last_time``: UTC seconds spanned by the vertices.

        With no pivots, ``highs`` and ``lows`` are empty and the scalars fall
        back to the window: check the two lists before drawing anything.
    """
    raw, win = _raw_candidates(df, left, right, start, end)

    cands = raw
    if raw and win["range"] > 0.0:
        base = ENVELOPE_SIGNIFICANCE * float(win["range"])
        for rung in _SIGNIFICANCE_LADDER:
            # Always prune the raw list. Pruning a pruned list would let one
            # harsh rung throw away vertices the next rung can never recover.
            cands = _zigzag(raw, rung * base)
            if min(_side_counts(cands)) >= ENVELOPE_MIN_VERTICES:
                break

    highs = _keep_most_significant(cands, "high", int(max_points))
    lows = _keep_most_significant(cands, "low", int(max_points))

    slope_high, intercept_high = _fit_side(highs)
    slope_low, intercept_low = _fit_side(lows)

    times = [p.time for p in highs] + [p.time for p in lows]
    if times:
        first_time = float(min(times))
        last_time = float(max(times))
    else:
        first_time = float(win["first_time"])
        last_time = float(win["last_time"])

    span = last_time - first_time
    mid = first_time + span / 2.0

    if highs and lows:
        upper = intercept_high + slope_high * mid if len(highs) > 1 else highs[0].price
        lower = intercept_low + slope_low * mid if len(lows) > 1 else lows[0].price
        width = abs(float(upper - lower))
    else:
        width = float(win["range"])

    slopes = []
    if len(highs) > 1:
        slopes.append(slope_high)
    if len(lows) > 1:
        slopes.append(slope_low)
    rise = (sum(slopes) / len(slopes)) * span if slopes and span > 0 else 0.0

    reference = width if width > 0 else float(win["range"])
    if reference <= 0 or abs(rise) < _TREND_RATIO * reference:
        direction = "sideways"
    else:
        direction = "up" if rise > 0 else "down"

    return {
        "highs": highs,
        "lows": lows,
        "slope_high": float(slope_high),
        "slope_low": float(slope_low),
        "direction": direction,
        "width": float(width),
        "first_time": first_time,
        "last_time": last_time,
    }


def fit_trendline(pivots: list[Pivot], tolerance_pct: float = 0.5) -> dict[str, Any]:
    """Find the straight line that connects the most of these pivots.

    Every pair of pivots defines a candidate line and the pair with the most
    other pivots within ``tolerance_pct`` of it wins. The line runs through two
    real pivots rather than through a regression cloud, so the drawn line
    actually touches the bars it claims to touch. Ties go to the wider span,
    because a line across the whole window says more than one joining two
    adjacent swings, and then to the tighter residual.

    Args:
        pivots: Swing points from one side, from :func:`swing_pivots`. Passing
            both sides mixed together is allowed but rarely what you want.
        tolerance_pct: How close a pivot must sit to the line to count as a
            touch, as a percentage of the line's price at that time.

    Returns:
        A dict with ``from`` and ``to`` (:class:`Pivot` or None), ``slope``
        (price units per second), ``intercept``, ``touches`` and ``r2``. ``r2``
        is measured over the touching pivots only, so it answers "how collinear
        are the points this line claims", and is 1.0 by construction when there
        are fewer than three touches. Fewer than two pivots returns a zeroed
        dict rather than raising.
    """
    empty = {
        "from": None,
        "to": None,
        "slope": 0.0,
        "intercept": 0.0,
        "touches": 0,
        "r2": 0.0,
    }
    points = [p for p in (pivots or []) if np.isfinite(p.time) and np.isfinite(p.price)]
    if len(points) < 2:
        return empty

    points = sorted(points, key=lambda p: p.time)
    if len(points) > _MAX_TRENDLINE_PIVOTS:
        points = points[-_MAX_TRENDLINE_PIVOTS:]

    times = np.array([p.time for p in points], dtype=float)
    prices = np.array([p.price for p in points], dtype=float)
    tol_frac = max(float(tolerance_pct), 0.0) / 100.0

    best: tuple[int, float, float] | None = None
    best_pair: tuple[int, int] | None = None

    for i in range(len(points) - 1):
        for j in range(i + 1, len(points)):
            dt = times[j] - times[i]
            if dt <= 0:
                continue
            slope = (prices[j] - prices[i]) / dt
            intercept = prices[i] - slope * times[i]
            fitted = slope * times + intercept
            allowance = np.abs(fitted) * tol_frac
            residual = np.abs(prices - fitted)
            hits = residual <= allowance
            touches = int(hits.sum())
            span = float(dt)
            err = float((residual[hits] ** 2).sum())
            score = (touches, span, -err)
            if best is None or score > best:
                best = score
                best_pair = (i, j)

    if best_pair is None:
        return empty

    i, j = best_pair
    dt = times[j] - times[i]
    slope = float((prices[j] - prices[i]) / dt)
    intercept = float(prices[i] - slope * times[i])
    fitted = slope * times + intercept
    hits = np.abs(prices - fitted) <= np.abs(fitted) * tol_frac
    touches = int(hits.sum())

    y = prices[hits]
    f = fitted[hits]
    if y.size < 3:
        r2 = 1.0
    else:
        ss_tot = float(((y - y.mean()) ** 2).sum())
        ss_res = float(((y - f) ** 2).sum())
        r2 = 1.0 if ss_tot <= 0 else float(1.0 - ss_res / ss_tot)

    return {
        "from": points[i],
        "to": points[j],
        "slope": slope,
        "intercept": intercept,
        "touches": touches,
        "r2": r2,
    }


def support_resistance(
    df: pd.DataFrame,
    bins: int = 0,
    min_touches: int = 2,
    start: float | None = None,
    end: float | None = None,
) -> list[dict[str, Any]]:
    """Horizontal levels where swing points cluster.

    Swing highs and lows in the visible range are grouped by price. A group is a
    level and the number of pivots in it is its touch count. Grouping is
    agglomerative rather than histogram binning, so two pivots a rupee apart are
    never split by an arbitrary bin edge; ``bins`` sets the width of a group as
    a fraction of the visible range instead of placing the edges.

    Args:
        df: Cleaned OHLCV frame with a DatetimeIndex.
        bins: Group width control. The tolerance is ``range / bins``. 0 picks a
            value from the bar count, between 12 and 48.
        min_touches: Drop levels touched fewer times than this.
        start: Optional lower bound in UTC seconds.
        end: Optional upper bound in UTC seconds.

    Returns:
        A list of dicts with ``price``, ``touches``, ``kind``, ``first_time``
        and ``last_time``, sorted by strength: touch count first, most recently
        touched first on a tie. ``kind`` is read against the last close in the
        window, so a level above current price is resistance and one below it is
        support, which is what a trader reads off the chart now. Empty when no
        cluster reaches ``min_touches``.
    """
    cands, win = _detect(df, DEFAULT_LEFT, DEFAULT_RIGHT, 0.0, start, end)
    if not cands or win["range"] <= 0:
        return []

    if int(bins) > 0:
        effective = int(bins)
    else:
        effective = int(min(48, max(12, win["count"] // 8)))
    tolerance = float(win["range"]) / max(effective, 1)
    if tolerance <= 0:
        return []

    ordered = sorted(cands, key=lambda c: c.price)
    groups: list[list[_Cand]] = [[ordered[0]]]
    for cand in ordered[1:]:
        if cand.price - groups[-1][0].price <= tolerance:
            groups[-1].append(cand)
        else:
            groups.append([cand])

    reference = float(win["last_close"])
    levels: list[dict[str, Any]] = []
    for group in groups:
        if len(group) < max(int(min_touches), 1):
            continue
        prices = [c.price for c in group]
        times = [c.time for c in group]
        price = float(sum(prices) / len(prices))
        levels.append(
            {
                "price": price,
                "touches": len(group),
                "kind": "resistance" if price >= reference else "support",
                "first_time": float(min(times)),
                "last_time": float(max(times)),
            }
        )

    levels.sort(key=lambda lv: (-lv["touches"], -lv["last_time"]))
    return levels


def measured_move(env: dict[str, Any]) -> dict[str, Any]:
    """Project the envelope's width off each boundary.

    The textbook rule for a band or rectangle: a break of the upper boundary
    targets one band height above it, a break of the lower boundary targets one
    band height below. Both boundaries are evaluated at the envelope's right
    edge, so the targets sit where the band actually ends rather than where it
    started.

    Args:
        env: The dict returned by :func:`envelope`.

    Returns:
        A dict with ``width``, ``direction``, ``time`` (the right edge, UTC
        seconds), ``target_time`` (the right edge plus the pattern's own
        duration, a seat for the target label), ``breakout`` and ``breakdown``
        (the two boundary prices at the right edge), and ``upside_target`` and
        ``downside_target``. Every value is 0.0 when the envelope has no
        vertices.
    """
    blank = {
        "width": 0.0,
        "direction": "sideways",
        "time": 0.0,
        "target_time": 0.0,
        "breakout": 0.0,
        "breakdown": 0.0,
        "upside_target": 0.0,
        "downside_target": 0.0,
    }
    if not isinstance(env, dict):
        return blank

    highs = list(env.get("highs") or [])
    lows = list(env.get("lows") or [])
    if not highs and not lows:
        return blank

    width = float(env.get("width") or 0.0)
    first_time = float(env.get("first_time") or 0.0)
    last_time = float(env.get("last_time") or 0.0)
    duration = max(last_time - first_time, 0.0)

    def boundary(pivots: list[Pivot], fallback: float) -> float:
        if not pivots:
            return fallback
        if len(pivots) == 1:
            return float(pivots[0].price)
        slope, intercept = _fit_side(pivots)
        return float(intercept + slope * last_time)

    top_seed = max((p.price for p in highs), default=0.0)
    bottom_seed = min((p.price for p in lows), default=0.0)
    breakout = boundary(highs, bottom_seed + width)
    breakdown = boundary(lows, top_seed - width)

    return {
        "width": width,
        "direction": str(env.get("direction") or "sideways"),
        "time": last_time,
        "target_time": last_time + duration,
        "breakout": breakout,
        "breakdown": breakdown,
        "upside_target": breakout + width,
        "downside_target": breakdown - width,
    }


def snap_to_pivot(
    df: pd.DataFrame,
    price: float,
    kind: str = "any",
    tolerance_pct: float = 2.0,
) -> Pivot | None:
    """Map a loose number onto the real pivot near it.

    A user says "the move from 4446" when the swing high printed at 4446.80.
    Drawing at 4446 puts the anchor off the candle, so the number is snapped to
    the nearest confirmed pivot before anything is drawn. Detection is looser
    here than for drawing, two bars either side, because a remembered price is
    often a minor swing that a three bar window discards, and the frame's own
    extremes are tested as well, since an extreme inside the last two bars can
    never be a confirmed fractal.

    Args:
        df: Cleaned OHLCV frame with a DatetimeIndex.
        price: The user's approximate number.
        kind: "high", "low" or "any". "resistance" and "support" are accepted as
            synonyms for "high" and "low".
        tolerance_pct: How far the real pivot may sit from the number, as a
            percentage of the number.

    Returns:
        The nearest matching :class:`Pivot`, or None when nothing sits inside
        the tolerance. Two pivots within :data:`_SNAP_TIE_PCT` of each other
        count as equally close, and then the more prominent one wins, followed
        by the more recent.
    """
    try:
        target = float(price)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(target) or target == 0.0 or len(df) == 0:
        return None

    wanted = str(kind or "any").strip().lower()
    wanted = {"resistance": "high", "support": "low"}.get(wanted, wanted)
    if wanted not in ("high", "low", "any"):
        wanted = "any"

    cands, _ = _detect(df, SNAP_LEFT, SNAP_RIGHT, 0.0, None, None)
    pool = [c for c in cands if wanted == "any" or c.kind == wanted]

    times = to_utc_seconds(df.index)
    highs = _column(df, "high")
    lows = _column(df, "low")
    if times.size == len(df):
        if wanted in ("high", "any") and np.isfinite(highs).any():
            i = int(np.nanargmax(highs))
            pool.append(_Cand(i, float(times[i]), float(highs[i]), "high"))
        if wanted in ("low", "any") and np.isfinite(lows).any():
            i = int(np.nanargmin(lows))
            pool.append(_Cand(i, float(times[i]), float(lows[i]), "low"))

    if not pool:
        return None

    allowance = abs(target) * max(float(tolerance_pct), 0.0) / 100.0
    nearest = min(abs(c.price - target) for c in pool)
    if nearest > allowance:
        return None

    # Everything this close is the same number as far as the user is concerned.
    band = abs(target) * _SNAP_TIE_PCT / 100.0
    finalists = [c for c in pool if abs(c.price - target) <= nearest + band]

    # Prominence measured toward each pivot's own extreme, so a high and a low
    # can be ranked against each other without one side always winning.
    centre = 0.0
    if np.isfinite(highs).any() and np.isfinite(lows).any():
        centre = (float(np.nanmax(highs)) + float(np.nanmin(lows))) / 2.0

    def prominence(c: _Cand) -> float:
        return c.price - centre if c.kind == "high" else centre - c.price

    best = max(finalists, key=lambda c: (prominence(c), c.index))
    return best.to_pivot()
