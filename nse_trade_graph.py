import json
import os
from datetime import datetime, timedelta

import talib
from nsemine.historical import get_stock_historical_data
from nsemine.live import get_stock_live_quotes

from llm import call_llm
from logging_config import setup_logging

MAX_ITERS = 3
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")

log = setup_logging("nse")


def _verdict_prompt(question):
    return f"{question}\nReply with exactly one word first: GOOD or BAD. Then, on the same line, a short reason."


def node_fetch(state):
    state["iters"] += 1
    state["quote"] = get_stock_live_quotes(state["symbol"])
    # 150 calendar days ~ 100 trading sessions — enough lookback for SMA50/MACD(12,26,9).
    start = datetime.now() - timedelta(days=150)
    state["hist"] = get_stock_historical_data(state["symbol"], start_datetime=start, interval="D")
    return "technical", state


def _compute_indicators(hist):
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


def node_technical(state):
    ind = _compute_indicators(state["hist"])
    state["technical_indicators"] = ind
    log.info("iter=%d indicators=%s", state["iters"], ind)

    verdict = call_llm(
        _verdict_prompt(
            f"Technical indicators for {state['symbol']}: close={ind['close']}, "
            f"SMA20={ind['sma20']}, SMA50={ind['sma50']}, RSI14={ind['rsi14']}, "
            f"MACD={ind['macd']}, MACD_signal={ind['macd_signal']}, MACD_hist={ind['macd_hist']}.\n"
            "Rules of thumb: SMA20 above SMA50 is a bullish trend; RSI above 70 is overbought "
            "(caution), below 30 is oversold; positive MACD histogram is bullish momentum.\n"
            "Is there a clear short-term uptrend/momentum worth considering a BUY?"
        ),
        mode="check",
    )
    state["technical_verdict"] = verdict
    log.info("iter=%d technical_verdict=%r", state["iters"], verdict)

    if verdict.startswith("GOOD"):
        return "risk", state
    return "technical_retry_guard", state


def node_technical_retry_guard(state):
    if state["iters"] >= MAX_ITERS:
        log.warning("technical check never GOOD after MAX_ITERS=%d", MAX_ITERS)
        return "abort", state
    return "fetch", state


def node_risk(state):
    quote = state["quote"]
    # nsemine's upper_circuit/lower_circuit fields are swapped — correct on read.
    lower_circuit = quote["upper_circuit"]
    upper_circuit = quote["lower_circuit"]

    if not (0 < state["risk_pct"] <= 25):
        state["risk_verdict"] = f"BAD: risk_pct={state['risk_pct']} outside sane 0-25% bound"
        log.warning(state["risk_verdict"])
        return "abort", state

    day_low = state["hist"].iloc[-1]["low"]
    if day_low <= lower_circuit * 1.02:
        state["risk_verdict"] = f"BAD: price near lower circuit ({day_low} vs {lower_circuit})"
        log.warning(state["risk_verdict"])
        return "abort", state

    position_size = state["principal"] * (state["risk_pct"] / 100)
    price = quote["open"]
    state["position_size"] = position_size
    state["max_shares"] = int(position_size // price)
    state["risk_verdict"] = (
        f"GOOD: {state['risk_pct']}% of principal={state['principal']}, "
        f"lower_circuit={lower_circuit}, upper_circuit={upper_circuit}"
    )
    log.info(state["risk_verdict"])
    return "sentiment", state


def node_sentiment(state):
    quote = state["quote"]
    verdict = call_llm(
        _verdict_prompt(
            f"Stock {state['symbol']} ({quote['name']}) is {quote['changepct']}% today, sector {quote['sector']}. "
            "Does this look like a reasonable entry point (not a crash, not an extreme spike)?"
        ),
        mode="check",
    )
    state["sentiment_verdict"] = verdict
    log.info("sentiment_verdict=%r", verdict)

    if verdict.startswith("GOOD"):
        return "propose", state
    return "flag_review", state


def node_propose(state):
    state["status"] = "proposed"
    state["proposal"] = (
        f"PROPOSAL (not executed): BUY {state['symbol']} — up to {state['max_shares']} shares "
        f"(~₹{state['position_size']:.0f}, {state['risk_pct']}% of ₹{state['principal']:.0f} principal). "
        "Confirm manually before placing any order."
    )
    return "log", state


def node_flag_review(state):
    state["status"] = "flagged_for_review"
    state["proposal"] = (
        f"FLAGGED FOR MANUAL REVIEW: sentiment check failed for {state['symbol']} "
        f"— {state['sentiment_verdict']!r}"
    )
    return "log", state


def node_abort(state):
    state["status"] = "aborted"
    state["proposal"] = None
    return "log", state


def node_log(state):
    record = {
        "timestamp": datetime.now().isoformat(),
        "symbol": state["symbol"],
        "principal": state["principal"],
        "risk_pct": state["risk_pct"],
        "iters": state["iters"],
        "technical_indicators": state.get("technical_indicators"),
        "technical_verdict": state.get("technical_verdict"),
        "risk_verdict": state.get("risk_verdict"),
        "sentiment_verdict": state.get("sentiment_verdict"),
        "status": state["status"],
        "proposal": state["proposal"],
    }
    with open(TRADE_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    log.info("status=%s proposal=%r", state["status"], state["proposal"])
    return None, state


GRAPH = {
    "fetch": node_fetch,
    "technical": node_technical,
    "technical_retry_guard": node_technical_retry_guard,
    "risk": node_risk,
    "sentiment": node_sentiment,
    "propose": node_propose,
    "flag_review": node_flag_review,
    "abort": node_abort,
    "log": node_log,
}


def run(symbol, principal, risk_pct=10.0, start="fetch"):
    state = {
        "symbol": symbol,
        "principal": principal,
        "risk_pct": risk_pct,
        "iters": 0,
        "quote": None,
        "hist": None,
        "technical_indicators": None,
        "technical_verdict": None,
        "risk_verdict": None,
        "sentiment_verdict": None,
        "status": None,
        "proposal": None,
    }
    node = start

    while node is not None:
        log.debug("node=%s", node)
        fn = GRAPH[node]
        node, state = fn(state)

    return state


if __name__ == "__main__":
    symbol = os.environ.get("NSE_SYMBOL", "RELIANCE")
    principal = float(os.environ.get("NSE_PRINCIPAL", "100000"))
    risk_pct = float(os.environ.get("NSE_RISK_PCT", "10"))

    final_state = run(symbol, principal, risk_pct)
    log.info("final: %s", final_state["proposal"])
