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
