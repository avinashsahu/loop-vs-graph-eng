"""Warm the cache-only material-disclosure feed outside live scans."""

from __future__ import annotations

import argparse
import os
import time

import nse_data
from logging_config import setup_logging
from material_disclosures import (
    get_material_disclosures,
    warm_material_disclosures,
)

log = setup_logging("warm_disclosures")


def _arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--universe-index")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--symbol-delay-seconds",
        type=float,
        default=float(
            os.environ.get(
                "MATERIAL_DISCLOSURE_SYMBOL_DELAY_SECONDS",
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
        symbol: get_material_disclosures(symbol).get("status")
        for symbol in symbols
    }
    symbol_order = {
        symbol: index for index, symbol in enumerate(symbols)
    }
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
        "material disclosure warm: %d symbols, %d due in this batch",
        len(symbols),
        len(due),
    )
    failures = 0
    for index, symbol in enumerate(due):
        if index:
            time.sleep(args.symbol_delay_seconds)
        try:
            result = warm_material_disclosures(symbol)
        except Exception:
            failures += 1
            log.warning(
                "material_disclosures[%s]: warm failed; continuing",
                symbol,
                exc_info=True,
            )
            continue
        log.info(
            "material_disclosures[%s]: %s, %d events, %d rating actions",
            symbol,
            result["status"],
            len(result["events"]),
            len(result["credit_ratings"]),
        )
        if result["status"] == "unavailable":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
