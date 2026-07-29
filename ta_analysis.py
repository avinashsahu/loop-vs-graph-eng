from dataclasses import dataclass

import numpy as np
import talib

_REQUIRED_TIMEFRAMES = ("D", "30", "15", "5")
_REQUIRED_COLUMNS = ("close", "high", "low")
_MINIMUM_BARS = 50


@dataclass(frozen=True)
class TechnicalAssessment:
    status: str
    verdict: str
    reason_codes: tuple[str, ...]
    indicators: dict
    evidence: dict

    def to_dict(self):
        # Indicators are persisted separately as `technical_indicators`; do not double
        # the trade-log payload by embedding the same nested values here.
        return {
            "status": self.status,
            "verdict": self.verdict,
            "reason_codes": list(self.reason_codes),
            "evidence": self.evidence,
        }


def evaluate_technical(histories):
    """Validate multi-timeframe bars, calculate indicators, and score one candidate."""
    reasons = []
    for timeframe in _REQUIRED_TIMEFRAMES:
        history = histories.get(timeframe)
        if history is None:
            reasons.append(f"MISSING_TIMEFRAME:{timeframe}")
            continue
        missing_columns = [
            column for column in _REQUIRED_COLUMNS if column not in history.columns
        ]
        if missing_columns:
            reasons.append(
                f"MISSING_COLUMNS:{timeframe}:{','.join(missing_columns)}"
            )
        if len(history) < _MINIMUM_BARS:
            reasons.append(f"INSUFFICIENT_BARS:{timeframe}")
        elif not missing_columns:
            recent_prices = history.loc[:, _REQUIRED_COLUMNS].tail(_MINIMUM_BARS)
            if not np.isfinite(recent_prices.to_numpy(dtype=float)).all():
                reasons.append(f"NON_FINITE_BARS:{timeframe}")

    if reasons:
        return TechnicalAssessment(
            status="invalid_data",
            verdict="BAD",
            reason_codes=tuple(reasons),
            indicators={},
            evidence={},
        )

    indicators = {
        timeframe: compute_indicators(histories[timeframe])
        for timeframe in _REQUIRED_TIMEFRAMES
    }
    indicator_reasons = []
    for timeframe, values in indicators.items():
        if not all(np.isfinite(value) for value in values.values()):
            indicator_reasons.append(f"NON_FINITE_INDICATORS:{timeframe}")
        elif values["atr14"] <= 0:
            indicator_reasons.append(f"NON_POSITIVE_ATR:{timeframe}")

    if indicator_reasons:
        return TechnicalAssessment(
            status="invalid_data",
            verdict="BAD",
            reason_codes=tuple(indicator_reasons),
            indicators={},
            evidence={},
        )

    evidence = score_technical(indicators)
    return TechnicalAssessment(
        status="ready",
        verdict=evidence["verdict"],
        reason_codes=(),
        indicators=indicators,
        evidence=evidence,
    )


def compute_indicators(hist):
    close = hist["close"].to_numpy(dtype=float)
    high = hist["high"].to_numpy(dtype=float)
    low = hist["low"].to_numpy(dtype=float)
    sma20 = talib.SMA(close, timeperiod=20)
    sma50 = talib.SMA(close, timeperiod=50)
    rsi14 = talib.RSI(close, timeperiod=14)
    macd, macd_signal, macd_hist = talib.MACD(
        close,
        fastperiod=12,
        slowperiod=26,
        signalperiod=9,
    )
    atr14 = talib.ATR(high, low, close, timeperiod=14)
    last_close = float(close[-1])
    latest_atr = float(atr14[-1])
    return {
        "close": last_close,
        "sma20": float(sma20[-1]),
        "sma50": float(sma50[-1]),
        "rsi14": float(rsi14[-1]),
        "macd": float(macd[-1]),
        "macd_signal": float(macd_signal[-1]),
        "macd_hist": float(macd_hist[-1]),
        "atr14": latest_atr,
        # ATR as a % of price -- volatility measure independent of the stock's price
        # level, used below to widen/narrow the RSI overbought/oversold bands.
        "atr_pct": latest_atr / last_close * 100,
    }


# Daily counts most, matching the "weight the daily trend most heavily, use intraday to
# confirm timing" rule this scoring replaces an LLM call for.
_TIMEFRAME_WEIGHTS = {"D": 3, "30": 2, "15": 1, "5": 1}

# Scale for the SMA20/50 gap: a 3% separation maxes out the trend score at +/-1. Picked
# because a 3% SMA20/50 gap is already a well-established trend on NSE large caps, not
# backtested against this app's own data (see README/commit for the sourcing caveat).
_SMA_GAP_SCALE_PCT = 3.0

# A family score below this magnitude is "not really saying anything" -- excluded from
# the confluence vote so it can't count as either agreement or disagreement. Same class
# of heuristic as the other constants here: reasonable, not backtested.
_FAMILY_ENGAGEMENT_THRESHOLD = 0.1


def _clip(value, lo=-1.0, hi=1.0):
    # float() up front -- indicators are numpy floats, and numpy comparisons/arithmetic
    # produce numpy scalars that repr as "np.float64(...)" in log strings.
    value = float(value)
    # NaN (not enough bars yet for a SMA50/ATR14 -- e.g. a recently-listed stock) must
    # not reach min()/max(): min(1.0, nan) silently returns 1.0 in Python (nan compares
    # False against everything), turning missing data into a fake maxed-out bullish
    # signal instead of "no signal." Found live: BAJAJ-AUTO's daily SMA50 was nan and
    # still produced trend_score=1.0, feeding a real (wrong) BUY proposal.
    if value != value:
        return 0.0
    return max(lo, min(hi, value))


def _adaptive_rsi_bands(atr_pct):
    """Overbought/oversold RSI band, widened or narrowed by recent volatility (ATR% of
    price) instead of Wilder's fixed 70/30 -- that fixed pair is an unvalidated 1978
    convention, not something derived from data. Volatility-adjusted bands are a real,
    established improvement direction (see README/commit), but the specific cutoffs
    below are a reasonable starting heuristic, not calibrated against this app's own
    universe -- that calibration needs a backtest harness over the bhavcopy history,
    which doesn't exist yet.
    """
    if atr_pct < 1.0:
        return 35.0, 65.0
    if atr_pct < 2.5:
        return 30.0, 70.0
    return 20.0, 80.0


def _rsi_penalty(rsi, lower, upper):
    """0 inside the neutral band; ramps toward -1 the further RSI sits beyond either
    edge, hitting -1 exactly at 0 or 100. Extremes on either side are treated as
    caution, matching the old rule ("overbought = momentum may be exhausted",
    "oversold = conflicts with a genuine uptrend claim") but graduated instead of a
    step function.
    """
    if rsi > upper:
        return -_clip((rsi - upper) / (100.0 - upper))
    if rsi < lower:
        return -_clip((lower - rsi) / lower)
    return 0.0


_TOTAL_TIMEFRAME_WEIGHT = sum(_TIMEFRAME_WEIGHTS.values())


def score_technical(indicators):
    """Deterministic replacement for what node_technical used to ask an LLM to judge.

    Live testing (both gemma4 and Fin-R1) showed LLMs aren't reliable at applying exact
    numeric thresholds here: gemma4 produced byte-identical verdicts regardless of RSI
    value (completely ignoring the stated RSI rule); Fin-R1 engaged with the numbers in
    its reasoning text but still never once flipped to BAD across 11 deliberately-
    bearish test cases, and in one case asserted "90.0 > 95.0" as fact. Threshold and
    comparison logic doesn't belong in a free-text LLM prompt -- it belongs here.

    Two upgrades over a plain weighted point-sum:
    1. Graduated scores. SMA trend and MACD momentum are scored by how far apart the
       values are (as a fraction of a scale, clipped to +/-1), not just which side of
       zero they land on -- a 0.1% SMA gap and a 3% SMA gap used to count the same.
    2. Confluence across signal roles, not across timeframes. Trend
       (SMA) and momentum (MACD) are each computed at 4 timeframes to weight daily
       heaviest and use intraday for timing -- but those 4 readings are correlated (same
       price series), not 4 independent opinions. Treating them as separate "votes"
       double- and quadruple-counts the same underlying trend. So each family is first
       collapsed to one weighted-average family score across its 4 timeframes, and
       confluence is measured across trend, momentum, and an RSI extreme penalty. These
       all derive from price and are not statistically independent. Verdict is GOOD only
       if the total is positive AND at least 2 of the "engaged" roles -- those with
       |score| above a small deadzone, so near-zero noise doesn't get a vote -- agree on
       direction. In practice RSI is neutral or negative, so bullish confirmation
       requires both engaged trend and momentum to be positive. A first
       attempt defined "agreement" relative to the total score's own sign, which is a
       circular definition: if total_score > 0 then agreeing weight necessarily exceeds
       disagreeing weight by simple algebra, so that check could never fail. Requiring
       an independent majority of engaged families (not compared to the sum they
       produce) is what actually makes confluence a real constraint instead of a
       relabeling of "score > 0" -- confirmed with a trend/momentum divergence case
       (strong bullish trend, real bearish momentum, RSI neutral) that has a positive
       weighted score but only 1 of 2 engaged families agreeing: correctly BAD.
    """
    breakdown = {}
    trend_weighted_sum = 0.0
    momentum_weighted_sum = 0.0

    daily_atr_pct = indicators["D"]["atr_pct"]
    rsi_lower, rsi_upper = _adaptive_rsi_bands(daily_atr_pct)

    for tf, weight in _TIMEFRAME_WEIGHTS.items():
        ind = indicators[tf]
        sma_gap_pct = (ind["sma20"] - ind["sma50"]) / ind["sma50"] * 100
        trend_score = _clip(sma_gap_pct / _SMA_GAP_SCALE_PCT)

        atr = ind["atr14"] or 1e-9  # guard against a zero-ATR flat series
        momentum_score = _clip(ind["macd_hist"] / (atr * 0.5))

        breakdown[tf] = {"trend_score": round(trend_score, 3), "momentum_score": round(momentum_score, 3)}
        trend_weighted_sum += weight * trend_score
        momentum_weighted_sum += weight * momentum_score

    # Weighted average (not sum) -- each family score stays on the same +/-1 scale as
    # its inputs, comparable to the RSI family score below.
    trend_family = trend_weighted_sum / _TOTAL_TIMEFRAME_WEIGHT
    momentum_family = momentum_weighted_sum / _TOTAL_TIMEFRAME_WEIGHT

    daily_rsi = float(indicators["D"]["rsi14"])
    rsi_family = _rsi_penalty(daily_rsi, rsi_lower, rsi_upper)

    if daily_rsi > rsi_upper:
        rsi_note = f"overbought vs adaptive band >{rsi_upper:.0f} (caution -- momentum may be exhausted)"
    elif daily_rsi < rsi_lower:
        rsi_note = f"oversold vs adaptive band <{rsi_lower:.0f} (conflicts with a genuine uptrend claim)"
    else:
        rsi_note = "neutral"

    families = {"trend": trend_family, "momentum": momentum_family, "rsi": rsi_family}
    total_score = sum(families.values())

    # Confluence: at least 2 of the (up to 3) "engaged" families -- magnitude above the
    # deadzone, so a near-zero reading can't vote either way -- must independently agree
    # on direction. Independent of total_score's own sign, unlike an earlier version
    # that compared each family against the sum they produced (circular -- see
    # docstring). A lone strong family with no confirmation is exactly what "confluence"
    # (multiple signals agreeing) is meant to exclude, even if the raw sum is positive.
    engaged = {k: v for k, v in families.items() if abs(v) > _FAMILY_ENGAGEMENT_THRESHOLD}
    agree_count = sum(1 for v in engaged.values() if v > 0)
    disagree_count = len(engaged) - agree_count
    confluence_ratio = agree_count / len(engaged) if engaged else 0.0
    verdict = "GOOD" if (total_score > 0 and len(engaged) >= 2 and agree_count > disagree_count) else "BAD"

    return {
        "score": round(total_score, 3),
        "verdict": verdict,
        "confluence_ratio": round(confluence_ratio, 2),
        "engaged_families": len(engaged),
        "families": {k: round(v, 3) for k, v in families.items()},
        "breakdown": breakdown,
        "daily_rsi": daily_rsi,
        "rsi_note": rsi_note,
        "rsi_band": (rsi_lower, rsi_upper),
    }
