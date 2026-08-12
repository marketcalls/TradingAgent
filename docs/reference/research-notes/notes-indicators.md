# OpenAlgo `ta` Indicator Library - Tool-Wrapper Reference

Ground truth: **installed package** `openalgo 2.0.3` at
`d:\AI Bootcamp 2026\Day08\openalgo\.venv\Lib\site-packages\openalgo\indicators\`.
Every signature, return shape, NaN warm-up and dtype below was obtained by
introspecting and **executing** the installed build (400-600 synthetic OHLCV bars),
not by reading the docs. Where the docs disagree, the source wins and the
discrepancy is listed in section 9.

---

## 1. Import and call convention

```python
from openalgo import ta          # ta is a singleton instance, NOT a module

df = client.history(symbol="SBIN", exchange="NSE", interval="5m",
                    start_date="2025-04-01", end_date="2025-04-08")

sma20 = ta.sma(df['close'], 20)                       # single output
macd_line, signal, hist = ta.macd(df['close'])        # tuple output
upper, mid, lower = ta.bbands(df['close'], 20, 2.0)   # tuple output
```

- `ta` is an instance of `openalgo.indicators.TechnicalAnalysis`, created at
  `openalgo/indicators/__init__.py:1650` as `ta = TechnicalAnalysis()` and
  re-exported by `openalgo/__init__.py` (`from .indicators import ta`,
  and listed in `__all__`).
- All indicator methods are **plain instance methods**. Every method delegates to a
  singleton indicator-class instance created in `TechnicalAnalysis.__init__`
  (e.g. `self._sma = SMA()`) and calls `<obj>.calculate(...)`. The 13 utilities are
  bound directly to the free functions in `utils.py`.
- All parameters can be passed **positionally or by keyword**. Keyword is strongly
  recommended for wrappers, because several methods have non-obvious positional
  order (see `ta.stochastic`, `ta.median_bands`).
- Accepted input types per series argument: `pd.Series`, `np.ndarray`, or `list`.

### ACTUAL callable count

```
len([m for m in dir(ta) if not m.startswith('_')]) == 127
```

**127 public callables** = 114 indicators + 13 utility functions.
The introduction doc claims "over 80 technical indicators"; the flow reference
claims "116 indicators" (that is 127 minus the 11 the Flow node cannot use).
**127 is the correct number of callable methods on `ta`.**

| Category (source module) | Count |
|---|---|
| Trend (`trend.py`) | 20 |
| Momentum (`momentum.py`) | 9 |
| Volatility (`volatility.py`) | 15 |
| Volume (`volume.py`) | 17 |
| Oscillators (`oscillators.py`) | 19 |
| Statistical (`statistics.py`) | 9 |
| Hybrid (`hybrid.py`) | 7 |
| TA-Lib extras (`talib_extra.py`) - **undocumented in the category .md files** | 18 |
| Utility (`utils.py`) | 13 |
| **TOTAL** | **127** |

---

## 2. Return-type contract (the single most important thing)

Defined in `indicators/base.py`, `BaseIndicator.validate_input` / `format_output`:

| Input type | Indicator output | Index |
|---|---|---|
| `pd.Series` | `pd.Series` | **preserved** (copied from the input index) |
| `np.ndarray` | `np.ndarray` | n/a |
| `list` | `np.ndarray` | n/a |

- Output is **always `float64`** (input is coerced via `.astype(np.float64)`), even
  for boolean-flavoured indicators such as `ta.fractals` and `ta.supertrend` direction.
- Output `pd.Series` has **`name = None`**. Set it yourself if you need a column name.
- **The 13 utility functions are the exception**: they *always* return a raw
  `np.ndarray` and **never** a `pd.Series`, even for `pd.Series` input. The pandas
  index is lost. See section 7.
- For multi-input indicators the returned index is taken from the **first**
  validated series argument (usually `high`), not from `close`.

---

## 3. Reading the tables

- **Required args**: positional, no default. The OHLCV series each one expects is named.
- **Optional args**: `name=default`.
- **Returns**: `Series` means one output (ndarray if input was ndarray/list);
  a tuple is written with its element names **in return order**.
- **Warm-up**: index of the first non-NaN element, measured on 400 bars with default
  params. `0` means no warm-up. This is indicative - it scales with the period args.

---

## 4. Indicator tables

### 4.1 Trend - `trend.py` (20)

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.sma` | `data` (close) | `period` **required, no default** | `Series` | period-1 |
| `ta.ema` | `data` (close) | `period` **required, no default** | `Series` | 0 |
| `ta.wma` | `data` (close) | `period` **required, no default** | `Series` | period-1 |
| `ta.dema` | `data` (close) | `period` **required, no default** | `Series` | 26 |
| `ta.tema` | `data` (close) | `period` **required, no default** | `Series` | 39 |
| `ta.hma` | `data` (close) | `period` **required, no default** | `Series` | 15 |
| `ta.vwma` | `data` (close), `volume` | `period` **required, no default** | `Series` | period-1 |
| `ta.alma` | `data` (close) | `period=21, offset=0.85, sigma=6.0` | `Series` | 20 |
| `ta.kama` | `data` (close) | `length=14, fast_length=2, slow_length=30` | `Series` | 14 |
| `ta.zlema` | `data` (close) | `period` **required, no default** | `Series` | 0 |
| `ta.t3` | `data` (close) | `period=21, v_factor=0.7` | `Series` | 0 |
| `ta.frama` | `high`, `low` | `period=26` | `Series` | 0 |
| `ta.trima` | `data` (close) | `period=20` | `Series` | 19 |
| `ta.mcginley` | `data` (close) | `period=14` | `Series` | 13 |
| `ta.vidya` | `data` (close) | `period=14, alpha=0.2` | `Series` | 14 |
| `ta.alligator` | `data` (close) | `jaw_period=13, jaw_shift=8, teeth_period=8, teeth_shift=5, lips_period=5, lips_shift=3` | `(jaw, teeth, lips)` | 20/12/7 |
| `ta.ma_envelopes` | `data` (close) | `period=20, percentage=2.5, ma_type='SMA'` | `(upper_envelope, middle_line, lower_envelope)` | 19 |
| `ta.supertrend` | `high`, `low`, `close` | `period=10, multiplier=3.0` | `(supertrend, direction)` | 9 |
| `ta.ichimoku` | `high`, `low`, `close` | `conversion_periods=9, base_periods=26, lagging_span2_periods=52, displacement=26` | `(conversion_line, base_line, leading_span_a, leading_span_b, lagging_span)` | 8/25/50/76/0 |
| `ta.ckstop` | `high`, `low`, `close` | `p=10, x=1.0, q=9` | `(stop_long, stop_short)` | 17 |

> `ta.frama` takes **high/low**, not close - easy to get wrong.
> `sma`, `ema`, `wma`, `dema`, `tema`, `hma`, `vwma`, `zlema` have **no default period**;
> a wrapper must supply it or the call raises `TypeError`.

### 4.2 Momentum - `momentum.py` (9)

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.rsi` | `data` (close) | `period=14` | `Series` | 14 |
| `ta.macd` | `data` (close) | `fast_period=12, slow_period=26, signal_period=9` | `(macd_line, signal_line, histogram)` | 0 |
| `ta.stochastic` | `high`, `low`, `close` | `k_period=14, smooth_k=3, d_period=3` | `(k_percent, d_percent)` | 15/17 |
| `ta.cci` | `high`, `low`, `close` | `period=20` | `Series` | 19 |
| `ta.williams_r` | `high`, `low`, `close` | `period=14` | `Series` | 13 |
| `ta.bop` | `open_prices`, `high`, `low`, `close` | - | `Series` | 0 |
| `ta.elderray` | `high`, `low`, `close` | `period=13` | `(bull_power, bear_power)` | 0 |
| `ta.fisher` | `high`, `low` | `length=9` | `(fisher, trigger)` | 8 |
| `ta.crsi` | `data` (close) | `lenrsi=3, lenupdown=2, lenroc=100` | `Series` | 99 |

> `ta.stochastic` positional order is `k_period, smooth_k, d_period`. The docs omit
> `smooth_k`, so `ta.stochastic(h, l, c, 14, 3)` sets **smooth_k**, not `d_period`.
> `ta.bop` takes **open** first - the only momentum method needing open.

### 4.3 Volatility - `volatility.py` (15)

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.atr` | `high`, `low`, `close` | `period=14` | `Series` | 13 |
| `ta.bbands` | `data` (close) | `period=20, std_dev=2.0` | `(upper_band, middle_band, lower_band)` | 19 |
| `ta.keltner` | `high`, `low`, `close` | `ema_period=20, atr_period=10, multiplier=2.0` | `(upper_channel, middle_line, lower_channel)` | 19 |
| `ta.donchian` | `high`, `low` | `period=20` | `(upper_channel, middle_line, lower_channel)` | 19 |
| `ta.chaikin` | `high`, `low` | `ema_period=10, roc_period=10` | `Series` | 19 |
| `ta.natr` | `high`, `low`, `close` | `period=14` | `Series` | 13 |
| `ta.ultimate_oscillator` | `high`, `low`, `close` | `period1=7, period2=14, period3=28` | `Series` | 27 |
| `ta.true_range` | `high`, `low`, `close` | - | `Series` | 0 |
| `ta.massindex` | `high`, `low` | `length=10` | `Series` | 9 |
| `ta.bbpercent` | `data` (close) | `period=20, std_dev=2.0` | `Series` | 19 |
| `ta.bbwidth` | `data` (close) | `period=20, std_dev=2.0` | `Series` | 19 |
| `ta.chandelier_exit` | `high`, `low`, `close` | `period=22, multiplier=3.0` | `(long_exit, short_exit)` | 21 |
| `ta.hv` | `close` | `length=10, annual=365, per=1` | `Series` | 10 |
| `ta.ulcerindex` | `data` (close) | `length=14, smooth_length=14, signal_length=52, signal_type='SMA', return_signal=False` | `Series`, or `(ulcer, signal)` if `return_signal=True` | **ALL NaN - BROKEN** |
| `ta.starc` | `high`, `low`, `close` | `ma_period=5, atr_period=15, multiplier=1.33` | `(upper_band, middle_line, lower_band)` | 14/4/14 |

> **`ta.rvi` is NOT in this category** despite the volatility doc. See section 8.

### 4.4 Volume - `volume.py` (17)

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.obv` | `close`, `volume` | - | `Series` | 0 |
| `ta.obv_smoothed` | `close`, `volume` | `ma_type='None', ma_length=20, bb_length=20, bb_mult=2.0` | `Series`; **`(obv_smoothed, bb_upper, bb_lower)` only when `ma_type='SMA + Bollinger Bands'`** | 0 |
| `ta.vwap` | `high`, `low`, `close`, `volume` | `anchor='Session', source='hlc3', stdev_mult_1=1.0, stdev_mult_2=2.0, stdev_mult_3=3.0, percent_mult_1=0.236, percent_mult_2=0.382, percent_mult_3=0.618` | `Series` (single - bands args are accepted but do **not** change the return) | 0 |
| `ta.mfi` | `high`, `low`, `close`, `volume` | `period=14` | `Series` | 13 |
| `ta.adl` | `high`, `low`, `close`, `volume` | - | `Series` | 0 |
| `ta.cmf` | `high`, `low`, `close`, `volume` | `period=20` | `Series` | 19 |
| `ta.emv` | `high`, `low`, `volume` | `length=14, divisor=10000` | `Series` | 14 |
| `ta.force_index` | `close`, `volume` | `length=13` | `Series` | 1 |
| `ta.nvi` | `close`, `volume` | - | `Series` | 0 |
| `ta.nvi_with_ema` | `close`, `volume` | `ema_length=255` | `(nvi, nvi_ema)` | 0 |
| `ta.pvi` | `close`, `volume` | `initial_value=100.0` | `Series` | 0 |
| `ta.pvi_with_signal` | `close`, `volume` | `initial_value=100.0, signal_type='EMA', signal_length=255` | `(pvi, signal)` | 0 |
| `ta.volosc` | `volume` **only** | `short_length=5, long_length=10, check_volume_validity=True` | `Series` | 0 |
| `ta.vroc` | `volume` **only** | `period=25` | `Series` | 25 |
| `ta.kvo` | `high`, `low`, `close`, `volume` | `trig_len=13, fast_x=34, slow_x=55` | `(kvo, trigger)` | 0 |
| `ta.pvt` | `close`, `volume` | - | `Series` | 0 |
| `ta.rvol` | `volume` **only** | `period=20` | `Series` | 19 |

> `ta.emv` takes `high, low, volume` - **no close**.
> `ta.volosc`, `ta.vroc`, `ta.rvol` take **volume as their only series**.
> `ta.obv_smoothed` valid `ma_type` (exact strings, case-sensitive):
> `"None"`, `"SMA"`, `"SMA + Bollinger Bands"`, `"EMA"`, `"SMMA (RMA)"`, `"WMA"`, `"VWMA"`.
> Anything else raises `ValueError: Unsupported ma_type: ...`.

### 4.5 Oscillators - `oscillators.py` (19)

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.cmo` | `data` (close) | `period=14` | `Series` | 14 |
| `ta.trix` | `data` (close) | `length=18` | `Series` | 1 |
| `ta.uo_oscillator` | `high`, `low`, `close` | `period1=7, period2=14, period3=28` | `Series` | 27 |
| `ta.awesome_oscillator` | `high`, `low` | `fast_period=5, slow_period=34` | `Series` | 33 |
| `ta.accelerator_oscillator` | `high`, `low` | `period=5` | `Series` | 37 |
| `ta.ppo` | `data` (close) | `fast_period=12, slow_period=26, signal_period=9` | `(ppo_line, signal_line, histogram)` | 0 |
| `ta.po` | `data` (close) | `fast_period=10, slow_period=20, ma_type='SMA'` | `Series` | 19 |
| `ta.dpo` | `data` (close) | `period=21, is_centered=False` | `Series` | 31 |
| `ta.aroon_oscillator` | `high`, `low` | `period=14` | `Series` | 14 |
| `ta.stochrsi` | `data` (close) | `rsi_period=14, stoch_period=14, k_period=3, d_period=3` | `(%K, %D)` | 14 |
| `ta.rvi` | `open_prices`, `high`, `low`, `close` | `period=10` | `(rvi, signal)` | 12/15 |
| `ta.cho` | `high`, `low`, `close`, `volume` | `fast_period=3, slow_period=10` | `Series` | 0 |
| `ta.chop` | `high`, `low`, `close` | `period=14` | `Series` | 13 |
| `ta.kst` | `data` (close) | `roclen1=10, roclen2=15, roclen3=20, roclen4=30, smalen1=10, smalen2=10, smalen3=10, smalen4=15, siglen=9` | `(kst, signal_line)` | 44/52 |
| `ta.tsi` | `data` (close) | `long_period=25, short_period=13, signal_period=13` | `(tsi, signal_line)` | 0 |
| `ta.vi` | `high`, `low`, `close` | `period=14` | `(vi_plus, vi_minus)` | **ALL NaN - BROKEN** |
| `ta.stc` | `data` (close) | `fast_length=23, slow_length=50, cycle_length=10, d1_length=3, d2_length=3` | `Series` | 0 |
| `ta.gator_oscillator` | `high`, `low` | `jaw_period=13, teeth_period=8, lips_period=5` | `(upper_histogram, lower_histogram)` | 20/12 |
| `ta.coppock` | `data` (close) | `wma_length=10, long_roc_length=14, short_roc_length=11` | `Series` | 14 |

> `ta.po` and `ta.ma_envelopes` accept only `ma_type` in `{"SMA", "EMA"}`; anything
> else raises `ValueError: Unsupported MA type: ...`.
> `ta.rvi` here is **Relative Vigor Index** and needs full OHLC. See section 8.

### 4.6 Statistical - `statistics.py` (9)

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.linreg` | `data` (close) | `period=14` | `Series` | 13 |
| `ta.lrslope` | `data` (close) | `period=100, interval=1` | `Series` | 100 |
| `ta.correlation` | **`data1`, `data2` (TWO price series)** | `period=20` | `Series` | 19 |
| `ta.beta` | **`asset`, `market` (TWO price series)** | `period=252` | `Series` | 252 |
| `ta.variance` | `data` (close) | `lookback=20, mode='PR', ema_period=20, filter_lookback=20, ema_length=14, return_components=False` | `Series`; **`(variance, ema_variance, zscore, ema_zscore, stdev)` when `return_components=True`** | 19 |
| `ta.tsf` | `data` (close) | `period=14` | `Series` | 13 |
| `ta.median` | `data` (close) | `period=3` | `Series` | 2 |
| `ta.median_bands` | `high`, `low`, `close` | `source=None (default hl2), median_length=3, atr_length=14, atr_mult=2.0` | `(median, upper_band, lower_band, median_ema)` | 2/13/13/2 |
| `ta.mode` | `data` (close) | `period=20, bins=10` | `Series` | 19 |

> `ta.variance` `mode` must be `"PR"` or `"LR"`; anything else raises `ValueError`.
> `ta.median_bands` has `source` as the **4th positional** parameter - pass the
> tuning params by keyword or you will silently bind an int to `source`.

### 4.7 Hybrid - `hybrid.py` (7)

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.adx` | `high`, `low`, `close` | `period=14` | `(plus_di, minus_di, adx)` | 13/13/26 |
| `ta.aroon` | `high`, `low` | `period=25` | `(aroon_up, aroon_down)` | 25 |
| `ta.pivot_points` | `high`, `low`, `close` | - | `(pivot, r1, s1, r2, s2, r3, s3)` (7-tuple) | 0 |
| `ta.psar` | `high`, `low` | `acceleration=0.02, maximum=0.2` | **`Series` (single, NOT a tuple)** | 0 |
| `ta.dmi` | `high`, `low`, `close` | `period=14` | `(plus_di, minus_di)` | 13 |
| `ta.fractals` | `high`, `low` | `periods=2` | `(fractal_up, fractal_down)` - float64 0.0/1.0 flags | 0 |
| `ta.rwi` | `high`, `low`, `close` | `period=14` | `(rwi_high, rwi_low)` | 14 |

> `ta.adx` returns **(+DI, -DI, ADX)** - the ADX line is the **third** element, not the first.
> `ta.psar` takes only high/low and returns a **single** series, contradicting the docs.

### 4.8 TA-Lib compatibility extras - `talib_extra.py` (18)

These 18 exist on `ta` and work, but are **absent from every category .md doc**
(they appear only in `flow indicator reference.md`). All return a single `Series`
except `stochf`.

| `ta.<method>` | Required args (series) | Optional args (defaults) | Returns | Warm-up |
|---|---|---|---|---|
| `ta.mom` | `data` (close) | `period=10` | `Series` | 10 |
| `ta.rocp` | `data` (close) | `period=10` | `Series` | 10 |
| `ta.rocr` | `data` (close) | `period=10` | `Series` | 10 |
| `ta.rocr100` | `data` (close) | `period=10` | `Series` | 10 |
| `ta.midpoint` | `data` (close) | `period=14` | `Series` | 13 |
| `ta.apo` | `data` (close) | `fast_period=12, slow_period=26, ma_type='SMA'` | `Series` | 25 |
| `ta.medprice` | `high`, `low` | - | `Series` | 0 |
| `ta.typprice` | `high`, `low`, `close` | - | `Series` | 0 |
| `ta.wclprice` | `high`, `low`, `close` | - | `Series` | 0 |
| `ta.midprice` | `high`, `low` | `period=14` | `Series` | 13 |
| `ta.avgprice` | `open_prices`, `high`, `low`, `close` | - | `Series` | 0 |
| `ta.plus_dm` | `high`, `low` | `period=14` | `Series` | 13 |
| `ta.minus_dm` | `high`, `low` | `period=14` | `Series` | 13 |
| `ta.dx` | `high`, `low`, `close` | `period=14` | `Series` | 14 |
| `ta.adxr` | `high`, `low`, `close` | `period=14` | `Series` | 40 |
| `ta.stochf` | `high`, `low`, `close` | `fastk_period=5, fastd_period=3` | `(fastk, fastd)` | 6 |
| `ta.linregangle` | `data` (close) | `period=14` | `Series` | 13 |
| `ta.linregintercept` | `data` (close) | `period=14` | `Series` | 13 |

> `ta.apo` silently accepts an invalid `ma_type` (no validation) - unlike `po`/`ma_envelopes`.

---

## 5. Multi-input indicators (flag these in tool schemas)

**Full OHLC (open + high + low + close)** - 3:
`ta.bop`, `ta.rvi`, `ta.avgprice`

**high + low + close + volume** - 5:
`ta.mfi`, `ta.adl`, `ta.cmf`, `ta.cho`, `ta.kvo`, `ta.vwap` *(6 with vwap)*

**high + low + close** - 21:
`ta.supertrend`, `ta.ichimoku`, `ta.ckstop`, `ta.stochastic`, `ta.cci`,
`ta.williams_r`, `ta.elderray`, `ta.atr`, `ta.keltner`, `ta.natr`,
`ta.ultimate_oscillator`, `ta.true_range`, `ta.chandelier_exit`, `ta.starc`,
`ta.chop`, `ta.vi`, `ta.uo_oscillator`, `ta.median_bands`, `ta.adx`, `ta.dmi`,
`ta.rwi`, `ta.typprice`, `ta.wclprice`, `ta.dx`, `ta.adxr`, `ta.stochf`

**high + low only (no close)** - 13:
`ta.frama`, `ta.donchian`, `ta.chaikin`, `ta.massindex`, `ta.fisher`,
`ta.awesome_oscillator`, `ta.accelerator_oscillator`, `ta.aroon_oscillator`,
`ta.gator_oscillator`, `ta.aroon`, `ta.psar`, `ta.fractals`, `ta.medprice`,
`ta.midprice`, `ta.plus_dm`, `ta.minus_dm`

**high + low + volume (no close)** - 1: `ta.emv`

**close + volume** - 8:
`ta.obv`, `ta.obv_smoothed`, `ta.force_index`, `ta.nvi`, `ta.nvi_with_ema`,
`ta.pvi`, `ta.pvi_with_signal`, `ta.pvt`, `ta.vwma` *(vwma = data+volume+period)*

**volume only** - 3: `ta.volosc`, `ta.vroc`, `ta.rvol`

### Two independent PRICE series (need two symbols / two columns)

| Method | Args | Meaning |
|---|---|---|
| `ta.correlation(data1, data2, period=20)` | two arbitrary series | rolling Pearson correlation |
| `ta.beta(asset, market, period=252)` | asset vs benchmark | rolling beta |

These two cannot be driven from a single OHLCV frame in the usual way - a tool
wrapper must accept a **second symbol** (or a second column) and fetch/align it.
The utility functions `crossover`, `crossunder`, `cross`, `exrem`, `flip`,
`valuewhen` also take two series, but those are normally two *derived* series.

---

## 6. Broken / degenerate methods in the installed build

Verified by execution on 400 and 600 bars:

| Method | Behaviour | Root cause |
|---|---|---|
| `ta.vi(high, low, close, period=14)` | returns `(all-NaN, all-NaN)` | `_backend.vi` builds `vmp`/`vmm` with `NaN` at index 0, then feeds them to `_backend.rolling_sum`, which is implemented with `np.cumsum`. A single leading NaN poisons the whole cumulative sum. `_backend.py:1041-1063`, `rolling_sum` at `_backend.py:1893`. |
| `ta.ulcerindex(data, ...)` | returns all-NaN (and `(all-NaN, all-NaN)` with `return_signal=True`) | same class of bug: `highest(data, length)` yields a NaN warm-up prefix, then `sma(dd**2, smooth_length)` (cumsum-based) propagates NaN over the entire array. `_backend.py:1842`. |

The repo's own generated `flow indicator reference.md` independently confirms this:
> "`median_bands`, `ulcerindex`, `vi` - The installed build returns no usable value for these."

**However, `ta.median_bands` actually works.** I executed it on 400 bars: it returns a
valid 4-tuple with only 2/13/13/2 NaN warm-up values and finite values through the last
bar. Its exclusion from the Flow node is a Flow-node limitation (optional `source` arg /
4 outputs), not a broken calculation. Only **`vi` and `ulcerindex` are genuinely broken.**

A tool wrapper should either exclude `ta.vi` and `ta.ulcerindex` or return an explicit
"not available in this build" error rather than a wall of nulls.

---

## 7. Utility functions - exact signatures (`utils.py`, 13)

All 13 return a **raw `np.ndarray`**, never a `pd.Series`, regardless of input type.
**The pandas index is discarded.**

| Signature | Return dtype | Warm-up | Notes |
|---|---|---|---|
| `ta.crossover(series1, series2)` | `ndarray[bool]` | none | True on the bar where s1 crosses **above** s2 |
| `ta.crossunder(series1, series2)` | `ndarray[bool]` | none | True on the bar where s1 crosses **below** s2 |
| `ta.cross(series1, series2)` | `ndarray[bool]` | none | either direction |
| `ta.highest(data, period)` | `ndarray[float64]` | period-1 | `period` is **required, no default** |
| `ta.lowest(data, period)` | `ndarray[float64]` | period-1 | `period` is **required, no default** |
| `ta.change(data, length=1)` | `ndarray[float64]` | `length` | `data[i] - data[i-length]` |
| `ta.roc(data, length)` | `ndarray[float64]` | `length` | **`length` required, no default**; percentage change |
| `ta.stdev(data, period)` | `ndarray[float64]` | period-1 | `period` **required, no default** |
| `ta.exrem(primary, secondary)` | `ndarray[bool]` | none | removes repeated `primary` signals until a `secondary` fires |
| `ta.flip(primary, secondary)` | `ndarray[bool]` | none | latching toggle: on at `primary`, off at `secondary` |
| `ta.valuewhen(expr, array, n=1)` | `ndarray[float64]` | until nth occurrence | value of `array` at the nth most recent bar where `expr` was true |
| `ta.rising(data, length)` | `ndarray[bool]` | none | **`length` required, no default** |
| `ta.falling(data, length)` | `ndarray[bool]` | none | **`length` required, no default** |

Argument names matter for keyword calls: `series1/series2` (crossover, crossunder,
cross), `primary/secondary` (exrem, flip), `expr/array/n` (valuewhen), `data/period`
(highest, lowest, stdev), `data/length` (change, roc, rising, falling).

**Note on `ta.roc`**: the introduction doc lists ROC under *Oscillators*, but the
installed `ta.roc` is the **utility** function - it returns an ndarray and its
`length` argument has **no default**. The oscillator-family rate-of-change methods
are `ta.rocp`, `ta.rocr`, `ta.rocr100`, `ta.mom` (all `period=10`, all return Series).

---

## 8. Name collisions and duplicates

### 8.1 RVI - the important one

`indicators/__init__.py:20` imports the volatility RVI under an alias:

```python
from .volatility import (..., RVI as VolatilityRVI, ...)
from .oscillators import (..., RVI, ...)
```

Then:
```python
self._rvi     = RVI()   # line 108 - Oscillator RVI (Relative Vigor Index)
self._rvi_osc = RVI()   # line 147 - the SAME oscillator class again
...
def rvi(self, open_prices, high, low, close, period=10):
    return self._rvi_osc.calculate(...)   # line 1164
```

- `ta.rvi` is the **Relative Vigor Index**: `ta.rvi(open_prices, high, low, close, period=10) -> (rvi, signal)`.
- `VolatilityRVI` (**Relative Volatility Index**) is imported but **never instantiated
  and never exposed on `ta`**. There is no way to reach it through `ta`.
  Advanced users can do `from openalgo.indicators.volatility import RVI`.
- `self._rvi` and `self._rvi_osc` are two instances of the *same* class - a redundant
  leftover, not two different indicators.

### 8.2 ULTOSC vs UO - genuine duplicate

- `ta.ultimate_oscillator` -> `volatility.ULTOSC`
- `ta.uo_oscillator` -> `oscillators.UO`

Two different classes in two different modules, identical signatures
`(high, low, close, period1=7, period2=14, period3=28)`, and **numerically identical
output** (verified with `np.allclose(..., equal_nan=True) == True`). Expose only one
as a tool, or alias them.

### 8.3 Other duplicate-looking pairs (NOT duplicates)

- `ta.nvi` / `ta.nvi_with_ema` and `ta.pvi` / `ta.pvi_with_signal` - same underlying
  class (`NVI`, `PVI`); the `_with_*` variant calls a different method and returns a
  2-tuple instead of a Series.
- `ta.adx` (3-tuple, includes ADX) vs `ta.dmi` (2-tuple, +DI/-DI only) vs `ta.dx` /
  `ta.adxr` (TA-Lib singles) - four related but distinct methods.
- `ta.roc` (utility, ndarray) vs `ta.rocp`/`ta.rocr`/`ta.rocr100`/`ta.mom` (TA-Lib, Series).
- `ta.fractals` is `WilliamsFractals`; `ta.psar` is `SAR`; `ta.ckstop` is
  `ChandeKrollStop`; `ta.hv` is `HistoricalVolatility`; `ta.variance` is `VAR`;
  `ta.correlation` is `CORREL`. Method names do **not** match the doc's class names.

### 8.4 Doc-name -> actual-method mapping for the non-obvious ones

| Doc name | Actual `ta.<method>` |
|---|---|
| MovingAverageEnvelopes | `ta.ma_envelopes` |
| ChandeKrollStop | `ta.ckstop` |
| McGinley | `ta.mcginley` |
| WilliamsR | `ta.williams_r` |
| ElderRay | `ta.elderray` |
| BollingerBands | `ta.bbands` |
| ULTOSC | `ta.ultimate_oscillator` |
| UO | `ta.uo_oscillator` |
| TRANGE | `ta.true_range` |
| MASS | `ta.massindex` |
| ChandelierExit | `ta.chandelier_exit` |
| HistoricalVolatility | `ta.hv` |
| UlcerIndex | `ta.ulcerindex` |
| OBVSmoothed | `ta.obv_smoothed` |
| FI | `ta.force_index` |
| KlingerVolumeOscillator | `ta.kvo` |
| PriceVolumeTrend | `ta.pvt` |
| AO / AC | `ta.awesome_oscillator` / `ta.accelerator_oscillator` |
| AROONOSC | `ta.aroon_oscillator` |
| GatorOscillator | `ta.gator_oscillator` |
| CORREL | `ta.correlation` |
| VAR | `ta.variance` |
| MedianBands | `ta.median_bands` |
| SAR | `ta.psar` |
| WilliamsFractals | `ta.fractals` |
| VI | `ta.vi` |

---

## 9. Docs vs installed source - discrepancies

| # | Doc claim | Installed reality | Severity |
|---|---|---|---|
| 1 | `volatility.md`: `ta.rvi(data, stdev_period=10, rsi_period=14)` -> single array (Relative **Volatility** Index); example `ta.rvi(df['close'])` | `ta.rvi(open_prices, high, low, close, period=10)` -> **2-tuple** (Relative **Vigor** Index). `ta.rvi(df['close'])` raises `TypeError: missing 3 required positional arguments`. | **Critical** |
| 2 | `hybrid.md`: `ta.psar(...)` returns `(sar_values, trend_direction)` | returns a **single** Series. Unpacking into 2 raises `ValueError`/iterates the Series. | **Critical** |
| 3 | `hybrid.md`: Aroon `period` default = 14 | actual default is **25** | High |
| 4 | `momentum.md`: `ta.stochastic` params listed as `k_period, d_period` | actual `k_period=14, smooth_k=3, d_period=3` - `smooth_k` sits **between** them | High |
| 5 | `introduction.md`: "over 80 technical indicators"; `flow indicator reference.md`: "116 indicators" | **127** public callables on `ta` (114 indicators + 13 utilities) | Medium |
| 6 | `utility.md` examples treat utility results as pandas Series (`position_long.iloc[-1]`, `df['X'] = ...` then `.iloc`) | utilities return **`np.ndarray`**; `.iloc` raises `AttributeError` | High |
| 7 | `introduction.md` lists ROC under Oscillators | `ta.roc` is the **utility** function (ndarray, `length` has no default) | Medium |
| 8 | `introduction.md` lists RVI in both Volatility and Oscillators as if both are reachable | only the **oscillator** RVI is reachable via `ta` | High |
| 9 | The 18 `talib_extra.py` methods are absent from all category docs | they exist and work on `ta` | Medium |
| 10 | `trend.md` says WMA returns `numpy.ndarray` while SMA/EMA return `pandas.Series` | **all** return a Series for Series input - the doc is internally inconsistent | Low |
| 11 | `flow indicator reference.md` lists `median_bands` as returning "no usable value" | `ta.median_bands` computes correctly (valid 4-tuple, finite to the last bar) | Medium |
| 12 | Docs never mention `ta.vi` usage | `ta.vi` exists but returns all-NaN | Medium |
| 13 | `volume.md` describes `ta.vwap` bands | `ta.vwap` accepts the 6 band multiplier args but returns a **single** Series; bands need `ta._vwap.calculate_with_bands(...)` | Medium |
| 14 | `introduction.md` lists 20 trend / 9 momentum / 16 volatility / 15 volume / 19 oscillator / 9 statistical / 7 hybrid | volatility is **15** (RVI unreachable), volume is **17** (`nvi_with_ema`, `pvi_with_signal`, `obv_smoothed` counted separately) | Low |

Also: `from openalgo.indicators import *` **raises**
`AttributeError: module 'openalgo.indicators' has no attribute 'ROC'` because
`__all__` lists `'ROC'` (and `'RVI'` twice) but `ROC` is never imported. Use
`from openalgo import ta` instead - that path is unaffected.

---

## 10. Everything else a tool-wrapper author must know

### 10.1 NaN warm-up
- Indicators emit a **leading NaN warm-up region** rather than raising or truncating.
  The output is always the **same length as the input**. Warm-up length per indicator
  is in the tables above.
- Some warm-ups are long: `ta.beta` needs **253** bars for its first value
  (`period=252`), `ta.lrslope` needs 101 (`period=100`), `ta.crsi` needs 100
  (`lenroc=100`), `ta.pvi_with_signal` / `ta.nvi_with_ema` use `signal_length=255`.
  A wrapper that fetches 100 bars and calls `ta.beta` gets a `ValueError`, and one that
  fetches 300 bars gets almost entirely NaN.
- Tuple elements can have **different** warm-ups (e.g. `ta.adx` -> 13/13/26,
  `ta.ichimoku` -> 8/25/50/76/0, `ta.starc` -> 14/4/14).
- **Serialise NaN as `null`, not `NaN`** - bare `NaN` is invalid JSON. For an agent
  tool, returning only the last N non-NaN values is usually the right move.

### 10.2 NaN in the INPUT is catastrophic
Because `_backend`'s `sma` / `rolling_sum` are `np.cumsum`-based, **one NaN anywhere in
the input poisons every subsequent output value**. Measured on 300 bars with a single
NaN injected at index 50:

| Call | NaN count in output |
|---|---|
| `ta.sma(c, 14)` | **263 / 300** (everything from index 50 onward) |
| `ta.ema(c, 14)` | **250 / 300** |
| `ta.rsi(c, 14)` | 14 (robust) |

Wrappers **must** clean input first - `df = df.dropna()` or `ffill()` - before calling
any indicator. Broker history endpoints routinely return gaps.

### 10.3 Input dtype and type rules
- Accepted: `pd.Series`, `np.ndarray`, `list`. Anything else raises
  `TypeError: Invalid input type: ...`.
- Input is coerced with `.astype(np.float64)`. Integer Series and `object`-dtype Series
  of numbers both work. A Series containing strings raises at cast time.
- Empty input raises `ValueError: Input data cannot be empty`.

### 10.4 Period arguments are strictly `int`
`BaseIndicator.validate_period` does `isinstance(period, int)`:

| Passed | Result |
|---|---|
| `14` | OK |
| `14.0` | `TypeError: Period must be an integer, got <class 'float'>` |
| `np.int64(14)` | `TypeError: Period must be an integer, got <class 'numpy.int64'>` |
| `"14"` | `TypeError` |

This bites hard: **JSON tool arguments decode numbers to `float`**, and values pulled
from a DataFrame are `np.int64`. Always `int(period)` in the wrapper before calling.

Other period errors:
- `period <= 0` -> `ValueError: Period must be positive, got 0`
- `period > len(data)` -> `ValueError: Period (14) cannot be greater than data length (5)`

### 10.5 Index handling and alignment
- Output Series index is copied from the **first** validated series argument.
- `BaseIndicator.align_arrays` checks **length only**, never index equality. Passing
  `high` and `close` with the same length but different indexes produces **silently
  wrong results** with the first argument's index. Always slice all series from one
  DataFrame.
- Mismatched lengths raise
  `ValueError: All arrays must have the same length. Got lengths: [300, 295]`.
- Duplicate / unsorted indexes are accepted without complaint (calculations are
  purely positional). Sort by time before calling.
- Output Series `.name` is `None`.

### 10.6 Conditional return arity - handle before unpacking
Three methods change their return shape based on an argument:

| Method | Single output when | Tuple when |
|---|---|---|
| `ta.obv_smoothed` | any `ma_type` except the BB one | `ma_type='SMA + Bollinger Bands'` -> 3-tuple |
| `ta.variance` | `return_components=False` (default) | `return_components=True` -> 5-tuple |
| `ta.ulcerindex` | `return_signal=False` (default) | `return_signal=True` -> 2-tuple |

A generic wrapper must branch on these, not on the type annotation.

### 10.7 Type annotations are unreliable
Many methods are annotated `-> np.ndarray` yet return a `pd.Series` for Series input
(e.g. `ta.atr`, `ta.rsi`, `ta.macd`). Some methods carry **no annotations at all**
(`adxr`, `alligator`, `apo`, `avgprice`, `bbpercent`, `bbwidth`, `chandelier_exit`,
`chop`, `dx`, `fractals`, `gator_oscillator`, `hv`, `kst`, `linregangle`,
`linregintercept`, `ma_envelopes`, `mcginley`, `medprice`, `midpoint`, `midprice`,
`minus_dm`, `mom`, `plus_dm`, `rocp`, `rocr`, `rocr100`, `rwi`, `starc`, `stc`,
`stochf`, `trima`, `tsi`, `typprice`, `ulcerindex`, `vidya`, `wclprice`).
**Do not generate tool schemas from `typing.get_type_hints` - use
`inspect.signature` for names/defaults and the tables above for return shapes.**

### 10.8 Backend
`openalgo.indicators._backend` reports `HAVE_RUST = True` in this install - the hot
loops are a compiled Rust extension. `_warmup()` is a no-op. Calls are fast and
thread-safe for read-only use; `ta` is a **module-level singleton**, so do not mutate
its `_*` attributes from concurrent tool calls.

### 10.9 Validated string-enum parameters
| Parameter | Valid values | Invalid value behaviour |
|---|---|---|
| `ta.obv_smoothed(ma_type=)` | `"None"`, `"SMA"`, `"SMA + Bollinger Bands"`, `"EMA"`, `"SMMA (RMA)"`, `"WMA"`, `"VWMA"` | `ValueError` |
| `ta.po(ma_type=)`, `ta.ma_envelopes(ma_type=)` | `"SMA"`, `"EMA"` only | `ValueError` |
| `ta.variance(mode=)` | `"PR"`, `"LR"` | `ValueError` |
| `ta.pvi_with_signal(signal_type=)` | `"EMA"`, `"SMA"` | `ValueError` |
| `ta.apo(ma_type=)` | `"SMA"`, `"EMA"` documented | **no validation** - silently accepts junk |
| `ta.ulcerindex(signal_type=)` | `"SMA"`, `"EMA"` | **no validation** |
| `ta.vwap(anchor=)`, `ta.vwap(source=)` | `"Session"/"Week"/"Month"/"Year"/"Day"`, `"hlc3"/"hl2"/"ohlc4"/"close"` | **no validation** - silently accepts junk |

Validate these in the wrapper; the library will not.

### 10.10 Suggested wrapper skeleton

```python
import numpy as np, pandas as pd
from openalgo import ta

def call_indicator(name, df, **params):
    fn = getattr(ta, name)                       # 127 valid names
    params = {k: (int(v) if isinstance(v, float) and v.is_integer() else v)
              for k, v in params.items()}        # JSON floats -> int (10.4)
    df = df.dropna().sort_index()                # NaN poisoning + ordering (10.2/10.5)
    out = fn(*series_args_for(name, df), **params)
    parts = out if isinstance(out, tuple) else (out,)   # conditional arity (10.6)
    return {n: pd.Series(p).where(pd.notna(p), None).tolist()
            for n, p in zip(output_names(name), parts)} # NaN -> null (10.1)
```

`output_names(name)` should come from a hand-built map seeded by the tuple element
names in section 4 - they are not derivable at runtime.
