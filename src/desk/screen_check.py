"""Print a ticker's latest daily indicator values, to compare with Taz's TradingView screen.

Blueprint v2.3 step 3: "Done when SPY's values match your TradingView screen
with the same settings." Usage:

    python -m desk.screen_check bars.json

where bars.json is Webull's daily bar response (a list of bar rows).
"""

from __future__ import annotations

import json
import sys

from desk.bars import bars_from_webull, require
from desk.indicators import Settings, daily_features, latest

# (label on TradingView, column, decimals)
ROWS = [
    ("Close", "close", 2),
    ("SMA 20", "sma_20", 2), ("SMA 50", "sma_50", 2), ("SMA 200", "sma_200", 2),
    ("EMA 9", "ema_9", 2), ("EMA 21", "ema_21", 2),
    ("RSI 14", "rsi_14", 2), ("RSI 2", "rsi_2", 2),
    ("MACD 12/26/9: MACD", "macd", 3), ("MACD: signal", "macd_signal", 3), ("MACD: histogram", "macd_hist", 3),
    ("Stoch 14/3/3: %K", "stoch_k", 2), ("Stoch: %D", "stoch_d", 2),
    ("ATR 14", "atr_14", 3),
    ("ADX 14 (DMI 14/14)", "adx", 2), ("DMI: +DI", "plus_di", 2), ("DMI: -DI", "minus_di", 2),
    ("BB 20/2: upper", "bb_upper", 2), ("BB: lower", "bb_lower", 2),
    ("KC 20/2/ATR 10: upper", "kc_upper", 2), ("KC: lower", "kc_lower", 2),
]


def table(rows_json: list[dict], s: Settings = Settings()) -> list[tuple[str, str]]:
    df = require(bars_from_webull(rows_json), min_bars=260)
    # This diagnostic displays price indicators only. Unknown relative-volume
    # semantics must not suppress the screen comparison or be reported as verified.
    row = latest(daily_features(df, s)[[col for _, col, _ in ROWS]])
    date = df.index[-1].tz_convert("America/New_York").date().isoformat()
    return [("Bar", date), ("Bars loaded", str(len(df)))] + [
        (label, f"{row[col]:.{d}f}") for label, col, d in ROWS]


def main(argv: list[str]) -> int:
    with open(argv[1]) as f:
        for label, value in table(json.load(f)):
            print(f"{label:24} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
