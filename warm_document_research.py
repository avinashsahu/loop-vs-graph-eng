"""Warm annual-report / earnings document research outside live scans."""

from __future__ import annotations

import argparse
import os
import time

import nse_data
from document_research import (
    get_document_research,
    warm_document_research,
)
from logging_config import setup_logging

log = setup_logging("warm_document_research")


def _arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--universe-index")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--symbol-delay-seconds",
        type=float,
        default=float(
            os.environ.get(
                "DOCUMENT_RESEARCH_SYMBOL_DELAY_SECONDS",
                "2",
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
        symbol: get_document_research(symbol).get("status")
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
        "document research warm: %d symbols, %d due in this batch",
        len(symbols),
        len(due),
    )
    failures = 0
    for index, symbol in enumerate(due):
        if index:
            time.sleep(args.symbol_delay_seconds)
        try:
            result = warm_document_research(symbol)
        except Exception:
            failures += 1
            log.warning(
                "document_research[%s]: warm failed; continuing",
                symbol,
                exc_info=True,
            )
            continue
        counts = result.get("document_counts") or {}
        log.info(
            "document_research[%s]: %s, ready=%s failed=%s facts=%d",
            symbol,
            result["status"],
            counts.get("ready"),
            counts.get("failed"),
            len(result.get("facts") or []),
        )
        if result["status"] == "unavailable":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
