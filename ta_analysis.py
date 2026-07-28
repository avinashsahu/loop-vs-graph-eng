import talib


def compute_indicators(hist):
    close = hist["close"].to_numpy(dtype=float)
    sma20 = talib.SMA(close, timeperiod=20)
    sma50 = talib.SMA(close, timeperiod=50)
    rsi14 = talib.RSI(close, timeperiod=14)
    macd, macd_signal, macd_hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    return {
        "close": round(close[-1], 2),
        "sma20": round(sma20[-1], 2),
        "sma50": round(sma50[-1], 2),
        "rsi14": round(rsi14[-1], 2),
        "macd": round(macd[-1], 2),
        "macd_signal": round(macd_signal[-1], 2),
        "macd_hist": round(macd_hist[-1], 2),
    }


# Daily counts most, matching the "weight the daily trend most heavily, use intraday to
# confirm timing" rule this scoring replaces an LLM call for.
_TIMEFRAME_WEIGHTS = {"D": 3, "30": 2, "15": 1, "5": 1}


def score_technical(indicators):
    """Deterministic replacement for what node_technical used to ask an LLM to judge.

    Live testing (both gemma4 and Fin-R1) showed LLMs aren't reliable at applying exact
    numeric thresholds here: gemma4 produced byte-identical verdicts regardless of RSI
    value (completely ignoring the stated RSI rule); Fin-R1 engaged with the numbers in
    its reasoning text but still never once flipped to BAD across 11 deliberately-
    bearish test cases, and in one case asserted "90.0 > 95.0" as fact. This isn't a
    model-quality problem -- threshold/comparison logic just doesn't belong in a
    free-text LLM prompt. Every comparison below is the same one the old prompt stated
    in English ("SMA20 above SMA50 is a bullish trend", "RSI above 70 is overbought",
    "positive MACD histogram is bullish momentum"), just applied exactly instead of
    hoping the model applies it.

    Returns a dict with the per-timeframe/RSI breakdown and a final GOOD/BAD verdict,
    so the log/email can show the same kind of "why" a verdict string used to carry.
    """
    breakdown = {}
    score = 0
    for tf, weight in _TIMEFRAME_WEIGHTS.items():
        ind = indicators[tf]
        # bool()/int() -- compute_indicators' values are numpy floats, and comparisons
        # against them produce numpy.bool_, which reprs as "np.True_" in log strings.
        sma_bullish = bool(ind["sma20"] > ind["sma50"])
        macd_bullish = bool(ind["macd_hist"] > 0)
        tf_score = int(weight * ((1 if sma_bullish else -1) + (1 if macd_bullish else -1)))
        breakdown[tf] = {"sma_bullish": sma_bullish, "macd_bullish": macd_bullish, "weighted_score": tf_score}
        score += tf_score

    # RSI only checked at the daily level -- it's a caution flag on the primary trend
    # read, not an intraday-timing signal like the other two.
    daily_rsi = indicators["D"]["rsi14"]
    rsi_note = "neutral"
    if daily_rsi > 70:
        rsi_note = "overbought (caution -- momentum may be exhausted)"
        score -= 2
    elif daily_rsi < 30:
        rsi_note = "oversold (conflicts with a genuine uptrend claim)"
        score -= 2

    return {
        "score": score,
        "verdict": "GOOD" if score > 0 else "BAD",
        "breakdown": breakdown,
        "daily_rsi": daily_rsi,
        "rsi_note": rsi_note,
    }
