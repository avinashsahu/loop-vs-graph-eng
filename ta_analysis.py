import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import talib

_REQUIRED_TIMEFRAMES = ("D", "30", "15", "5")
_REQUIRED_COLUMNS = ("close", "high", "low")
_MINIMUM_BARS = 50
BASELINE_TECHNICAL_POLICY_ID = "technical-confluence-v1"
REVISED_TECHNICAL_POLICY_ID = "technical-relative-participation-v2"
_POLICY_PATH = Path(__file__).with_name("technical_policies.json")
_KNOWN_FAMILIES = {
    "trend",
    "momentum",
    "rsi",
    "relative_strength",
    "participation",
}


@dataclass(frozen=True)
class TechnicalPolicy:
    policy_id: str
    description: str
    timeframe_weights: tuple[tuple[str, float], ...]
    families: tuple[str, ...]
    sma_gap_scale_pct: float
    macd_atr_scale: float
    family_engagement_threshold: float
    relative_strength_lookback: int
    relative_strength_scale_pct: float
    minimum_daily_turnover: float
    participation_score: float
    rsi_atr_thresholds: tuple[float, float]
    rsi_bands: tuple[tuple[float, float], ...]
    enforce_daily_anchor: bool
    fingerprint: str

    def timeframe_weight(self, timeframe):
        return dict(self.timeframe_weights)[timeframe]

    @property
    def total_timeframe_weight(self):
        return sum(weight for _, weight in self.timeframe_weights)


@dataclass(frozen=True)
class TechnicalObservations:
    """The completed-bar inputs shared by live evaluation and historical replay."""

    histories: dict
    benchmark_daily: object | None = None
    delivery_trend: dict | None = None


@dataclass(frozen=True)
class TechnicalReplaySample:
    symbol: str
    observed_at: datetime
    observations: TechnicalObservations
    forward_return_pct: float


@dataclass(frozen=True)
class TechnicalReplayResult:
    policy_id: str
    policy_fingerprint: str
    sample_count: int
    signal_count: int
    gross_return_pct: float
    transaction_cost_pct: float
    net_return_pct: float


def _load_technical_policies():
    raw_policies = json.loads(_POLICY_PATH.read_text())
    policies = {}
    for policy_id, raw in raw_policies.items():
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        policy = TechnicalPolicy(
            policy_id=policy_id,
            description=raw["description"],
            timeframe_weights=tuple(
                (timeframe, float(raw["timeframe_weights"][timeframe]))
                for timeframe in _REQUIRED_TIMEFRAMES
            ),
            families=tuple(raw["families"]),
            sma_gap_scale_pct=float(raw["sma_gap_scale_pct"]),
            macd_atr_scale=float(raw["macd_atr_scale"]),
            family_engagement_threshold=float(
                raw["family_engagement_threshold"]
            ),
            relative_strength_lookback=int(raw["relative_strength_lookback"]),
            relative_strength_scale_pct=float(
                raw["relative_strength_scale_pct"]
            ),
            minimum_daily_turnover=float(raw["minimum_daily_turnover"]),
            participation_score=float(raw["participation_score"]),
            rsi_atr_thresholds=tuple(
                float(value) for value in raw["rsi_atr_thresholds"]
            ),
            rsi_bands=tuple(
                tuple(float(value) for value in band)
                for band in raw["rsi_bands"]
            ),
            enforce_daily_anchor=bool(raw["enforce_daily_anchor"]),
            fingerprint=hashlib.sha256(canonical.encode()).hexdigest()[:16],
        )
        unknown_families = set(policy.families) - _KNOWN_FAMILIES
        if unknown_families:
            raise ValueError(
                f"{policy_id} has unknown indicator families: "
                f"{sorted(unknown_families)}"
            )
        if (
            policy.enforce_daily_anchor
            and policy.timeframe_weight("D")
            < sum(
                policy.timeframe_weight(timeframe)
                for timeframe in ("30", "15", "5")
            )
        ):
            raise ValueError(
                f"{policy_id} lets combined intraday weights outweigh daily"
            )
        policies[policy_id] = policy
    return policies


_TECHNICAL_POLICIES = _load_technical_policies()


def select_technical_policy(policy_id):
    """Resolve a persisted policy identity or fail instead of silently defaulting."""
    try:
        return _TECHNICAL_POLICIES[policy_id]
    except KeyError as error:
        raise ValueError(f"Unknown technical policy: {policy_id}") from error


@dataclass(frozen=True)
class TechnicalAssessment:
    status: str
    verdict: str
    reason_codes: tuple[str, ...]
    indicators: dict
    evidence: dict
    policy_id: str
    policy_fingerprint: str

    def to_dict(self):
        # Indicators are persisted separately as `technical_indicators`; do not double
        # the trade-log payload by embedding the same nested values here.
        return {
            "status": self.status,
            "verdict": self.verdict,
            "reason_codes": list(self.reason_codes),
            "evidence": self.evidence,
            "policy_id": self.policy_id,
            "policy_fingerprint": self.policy_fingerprint,
        }


def evaluate_technical(observations, policy=None):
    """Validate and score one completed-bar observation under an explicit policy."""
    policy = policy or select_technical_policy(BASELINE_TECHNICAL_POLICY_ID)
    if isinstance(observations, dict):
        observations = TechnicalObservations(histories=observations)
    histories = observations.histories
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
            policy_id=policy.policy_id,
            policy_fingerprint=policy.fingerprint,
        )

    if "relative_strength" in policy.families:
        benchmark = observations.benchmark_daily
        required_benchmark_bars = policy.relative_strength_lookback + 1
        if benchmark is None:
            reasons.append("MISSING_BENCHMARK")
        elif "close" not in benchmark.columns:
            reasons.append("MISSING_BENCHMARK_CLOSE")
        elif len(benchmark) < required_benchmark_bars:
            reasons.append("INSUFFICIENT_BENCHMARK_BARS")
        elif not np.isfinite(
            benchmark["close"].tail(required_benchmark_bars).to_numpy(dtype=float)
        ).all():
            reasons.append("NON_FINITE_BENCHMARK_BARS")

    if reasons:
        return TechnicalAssessment(
            status="invalid_data",
            verdict="BAD",
            reason_codes=tuple(reasons),
            indicators={},
            evidence={},
            policy_id=policy.policy_id,
            policy_fingerprint=policy.fingerprint,
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
            policy_id=policy.policy_id,
            policy_fingerprint=policy.fingerprint,
        )

    evidence = score_technical(indicators, observations, policy)
    return TechnicalAssessment(
        status="ready",
        verdict=evidence["verdict"],
        reason_codes=(),
        indicators=indicators,
        evidence=evidence,
        policy_id=policy.policy_id,
        policy_fingerprint=policy.fingerprint,
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


def _adaptive_rsi_bands(atr_pct, policy):
    """Overbought/oversold RSI band, widened or narrowed by recent volatility (ATR% of
    price) instead of Wilder's fixed 70/30 -- that fixed pair is an unvalidated 1978
    convention, not something derived from data. Volatility-adjusted bands are a real,
    established improvement direction (see README/commit), but the specific cutoffs
    below are a reasonable starting heuristic, not calibrated against this app's own
    universe -- that calibration needs a backtest harness over the bhavcopy history,
    which doesn't exist yet.
    """
    low_threshold, high_threshold = policy.rsi_atr_thresholds
    if atr_pct < low_threshold:
        return policy.rsi_bands[0]
    if atr_pct < high_threshold:
        return policy.rsi_bands[1]
    return policy.rsi_bands[2]


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


def _relative_strength_score(observations, policy):
    lookback = policy.relative_strength_lookback
    stock_close = observations.histories["D"]["close"].to_numpy(dtype=float)
    benchmark_close = observations.benchmark_daily["close"].to_numpy(dtype=float)
    stock_return = stock_close[-1] / stock_close[-(lookback + 1)] - 1.0
    benchmark_return = (
        benchmark_close[-1] / benchmark_close[-(lookback + 1)] - 1.0
    )
    relative_return_pct = (
        ((1.0 + stock_return) / (1.0 + benchmark_return)) - 1.0
    ) * 100.0
    return (
        _clip(relative_return_pct / policy.relative_strength_scale_pct),
        {
            "lookback_sessions": lookback,
            "stock_return_pct": round(stock_return * 100.0, 3),
            "benchmark_return_pct": round(benchmark_return * 100.0, 3),
            "relative_return_pct": round(relative_return_pct, 3),
        },
    )


def _participation_score(delivery_trend, policy):
    evidence = {
        "available": False,
        "liquid": None,
        "delivery_volume_confirmation": False,
        "delivery_percentage_directional": False,
    }
    if not delivery_trend or delivery_trend.get("status") != "ready":
        return 0.0, evidence

    recent_volume = float(delivery_trend.get("recent_avg_total_volume") or 0.0)
    baseline_volume = float(
        delivery_trend.get("baseline_avg_total_volume") or 0.0
    )
    vwap = float(delivery_trend.get("latest_vwap") or 0.0)
    turnover = recent_volume * vwap
    liquid = turnover >= policy.minimum_daily_turnover
    volume_expanded = recent_volume > baseline_volume * 1.1
    delivery_volume_rising = (
        delivery_trend.get("delivery_volume_trend") == "rising"
    )
    interpretation = delivery_trend.get("interpretation")
    evidence.update(
        {
            "available": True,
            "liquid": liquid,
            "recent_daily_turnover": round(turnover, 2),
            "minimum_daily_turnover": policy.minimum_daily_turnover,
            "total_volume_expanded": volume_expanded,
            "delivery_volume_confirmation": delivery_volume_rising,
            "interpretation": interpretation,
        }
    )

    if not liquid:
        return -policy.participation_score, evidence
    if (
        delivery_volume_rising
        and volume_expanded
        and interpretation == "possible_accumulation"
    ):
        return policy.participation_score, evidence
    if delivery_volume_rising and interpretation == "possible_distribution":
        return -policy.participation_score, evidence
    # Delivery percentage describes the share of volume delivered, not whether buyers
    # or sellers initiated it. Without delivery-volume and price confirmation it stays
    # neutral, even when the percentage itself is rising.
    return 0.0, evidence


def score_technical(indicators, observations=None, policy=None):
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
    policy = policy or select_technical_policy(BASELINE_TECHNICAL_POLICY_ID)
    observations = observations or TechnicalObservations(histories={})
    breakdown = {}
    trend_weighted_sum = 0.0
    momentum_weighted_sum = 0.0

    daily_atr_pct = indicators["D"]["atr_pct"]
    rsi_lower, rsi_upper = _adaptive_rsi_bands(daily_atr_pct, policy)

    for tf, weight in policy.timeframe_weights:
        ind = indicators[tf]
        sma_gap_pct = (ind["sma20"] - ind["sma50"]) / ind["sma50"] * 100
        trend_score = _clip(sma_gap_pct / policy.sma_gap_scale_pct)

        atr = ind["atr14"] or 1e-9  # guard against a zero-ATR flat series
        momentum_score = _clip(
            ind["macd_hist"] / (atr * policy.macd_atr_scale)
        )

        breakdown[tf] = {
            "trend_score": round(trend_score, 3),
            "momentum_score": round(momentum_score, 3),
        }
        trend_weighted_sum += weight * trend_score
        momentum_weighted_sum += weight * momentum_score

    # Weighted average (not sum) -- each family score stays on the same +/-1 scale as
    # its inputs, comparable to the RSI family score below.
    trend_family = trend_weighted_sum / policy.total_timeframe_weight
    momentum_family = momentum_weighted_sum / policy.total_timeframe_weight

    daily_rsi = float(indicators["D"]["rsi14"])
    rsi_family = _rsi_penalty(daily_rsi, rsi_lower, rsi_upper)

    if daily_rsi > rsi_upper:
        rsi_note = f"overbought vs adaptive band >{rsi_upper:.0f} (caution -- momentum may be exhausted)"
    elif daily_rsi < rsi_lower:
        rsi_note = f"oversold vs adaptive band <{rsi_lower:.0f} (conflicts with a genuine uptrend claim)"
    else:
        rsi_note = "neutral"

    candidate_families = {
        "trend": trend_family,
        "momentum": momentum_family,
        "rsi": rsi_family,
    }
    relative_strength = None
    if "relative_strength" in policy.families:
        relative_strength_score, relative_strength = _relative_strength_score(
            observations,
            policy,
        )
        candidate_families["relative_strength"] = relative_strength_score
    participation = None
    if "participation" in policy.families:
        participation_score, participation = _participation_score(
            observations.delivery_trend,
            policy,
        )
        candidate_families["participation"] = participation_score

    families = {
        family: candidate_families[family] for family in policy.families
    }
    total_score = sum(families.values())

    # Confluence: at least 2 of the (up to 3) "engaged" families -- magnitude above the
    # deadzone, so a near-zero reading can't vote either way -- must independently agree
    # on direction. Independent of total_score's own sign, unlike an earlier version
    # that compared each family against the sum they produced (circular -- see
    # docstring). A lone strong family with no confirmation is exactly what "confluence"
    # (multiple signals agreeing) is meant to exclude, even if the raw sum is positive.
    engaged = {
        key: value
        for key, value in families.items()
        if abs(value) > policy.family_engagement_threshold
    }
    agree_count = sum(1 for v in engaged.values() if v > 0)
    disagree_count = len(engaged) - agree_count
    confluence_ratio = agree_count / len(engaged) if engaged else 0.0
    verdict = "GOOD" if (total_score > 0 and len(engaged) >= 2 and agree_count > disagree_count) else "BAD"

    return {
        "policy_id": policy.policy_id,
        "policy_fingerprint": policy.fingerprint,
        "timeframe_weights": dict(policy.timeframe_weights),
        "indicator_families": list(policy.families),
        "score": round(total_score, 3),
        "verdict": verdict,
        "confluence_ratio": round(confluence_ratio, 2),
        "engaged_families": len(engaged),
        "families": {k: round(v, 3) for k, v in families.items()},
        "breakdown": breakdown,
        "daily_rsi": daily_rsi,
        "rsi_note": rsi_note,
        "rsi_band": (rsi_lower, rsi_upper),
        "relative_strength": relative_strength,
        "participation": participation,
    }


def replay_technical_policies(
    samples,
    policy_ids,
    round_trip_cost_bps,
):
    """Replay policies on one canonical observation per symbol/session.

    The earliest observation is retained when a live scanner sampled the same symbol
    repeatedly on one day. Reported returns are means across GOOD signals; round-trip
    costs are deducted from every signal in percentage-point units.
    """
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be non-negative")

    canonical_samples = {}
    for sample in sorted(samples, key=lambda item: item.observed_at):
        key = (sample.symbol.upper(), sample.observed_at.date())
        canonical_samples.setdefault(key, sample)

    results = {}
    transaction_cost_pct = float(round_trip_cost_bps) / 100.0
    for policy_id in policy_ids:
        policy = select_technical_policy(policy_id)
        gross_returns = []
        for sample in canonical_samples.values():
            assessment = evaluate_technical(sample.observations, policy)
            if assessment.status == "ready" and assessment.verdict == "GOOD":
                gross_returns.append(float(sample.forward_return_pct))
        signal_count = len(gross_returns)
        gross_return_pct = (
            sum(gross_returns) / signal_count if signal_count else 0.0
        )
        results[policy_id] = TechnicalReplayResult(
            policy_id=policy.policy_id,
            policy_fingerprint=policy.fingerprint,
            sample_count=len(canonical_samples),
            signal_count=signal_count,
            gross_return_pct=gross_return_pct,
            transaction_cost_pct=(
                transaction_cost_pct if signal_count else 0.0
            ),
            net_return_pct=(
                gross_return_pct - transaction_cost_pct
                if signal_count
                else 0.0
            ),
        )
    return results
