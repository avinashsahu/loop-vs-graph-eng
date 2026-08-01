"""Warm Integrated Filing - Governance history outside live scans."""

from __future__ import annotations

import argparse
import os
import time

import nse_data
from governance_filings import (
    get_governance_history,
    warm_governance_history,
)
from logging_config import setup_logging

log = setup_logging("warm_governance")


def _arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--universe-index")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument(
        "--symbol-delay-seconds",
        type=float,
        default=float(
            os.environ.get(
                "GOVERNANCE_SYMBOL_DELAY_SECONDS",
                "1",
            )
        ),
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _arguments(argv)
    symbols = [symbol.strip().upper() for symbol in args.symbols if symbol.strip()]
    if args.universe_index:
        symbols.extend(nse_data.get_index_symbols(args.universe_index))
    symbols = list(dict.fromkeys(symbols))
    status_by_symbol = {
        symbol: get_governance_history(symbol).get("status")
        for symbol in symbols
    }
    symbol_order = {symbol: index for index, symbol in enumerate(symbols)}
    due = sorted(
        (
            symbol
            for symbol in symbols
            if status_by_symbol[symbol] != "ready"
        ),
        key=lambda symbol: (
            status_by_symbol[symbol] != "pending",
            symbol_order[symbol],
        ),
    )[: max(args.limit, 0)]
    log.info(
        "governance warm: %d symbols, %d due in this batch",
        len(symbols),
        len(due),
    )
    failures = 0
    for index, symbol in enumerate(due):
        if index:
            time.sleep(args.symbol_delay_seconds)
        try:
            result = warm_governance_history(symbol)
        except Exception:
            failures += 1
            log.warning(
                "governance[%s]: warm failed; continuing",
                symbol,
                exc_info=True,
            )
            continue
        log.info(
            "governance[%s]: %s, %d periods, %d exceptions",
            symbol,
            result["status"],
            result.get("periods_available", 0),
            len(result.get("exceptions") or []),
        )
        if result["status"] == "unavailable":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
