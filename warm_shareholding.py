import argparse
import os
import time

from dotenv import load_dotenv

load_dotenv()

from logging_config import setup_logging
from market_time import is_market_hours
from nse_data import get_index_symbols
from shareholding import (
    NseShareholdingRequestError,
    due_shareholding_universe_symbols,
    queued_shareholding_symbols,
    record_shareholding_universe_attempt,
    seed_shareholding_universe,
    warm_shareholding_history,
)

log = setup_logging("warm_shareholding")


def _arguments():
    parser = argparse.ArgumentParser(
        description="Slowly warm NSE XBRL shareholding histories into Aerospike."
    )
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--index", dest="index_names", action="append")
    parser.add_argument(
        "--universe-index",
        dest="universe_index_names",
        action="append",
        help=(
            "persist this index's membership and warm the next due batch; "
            "repeatable and resumable"
        ),
    )
    parser.add_argument(
        "--queued",
        action="store_true",
        help="Warm symbols enqueued by cache-only live scans.",
    )
    parser.add_argument("--periods", type=int, default=5)
    parser.add_argument(
        "--limit",
        type=int,
        help="process at most this many deduplicated symbols",
    )
    parser.add_argument(
        "--allow-market-hours",
        action="store_true",
        help="Override the default guard that keeps this job out of NSE trading hours.",
    )
    return parser.parse_args()


def main():
    args = _arguments()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")
    refresh_after_days = int(
        os.environ.get("APP_XBRL_UNIVERSE_REFRESH_DAYS", "30")
    )
    if refresh_after_days <= 0:
        raise SystemExit("APP_XBRL_UNIVERSE_REFRESH_DAYS must be greater than zero")
    incomplete_retry_days = int(
        os.environ.get("APP_XBRL_UNIVERSE_INCOMPLETE_RETRY_DAYS", "7")
    )
    if incomplete_retry_days <= 0:
        raise SystemExit(
            "APP_XBRL_UNIVERSE_INCOMPLETE_RETRY_DAYS must be greater than zero"
        )
    if is_market_hours() and not args.allow_market_hours:
        raise SystemExit(
            "Refusing to warm during NSE market hours; retry after 15:30 IST or pass "
            "--allow-market-hours explicitly."
        )

    symbols = list(args.symbols)
    universe_memberships: dict[str, set[str]] = {}
    for index_name in args.index_names or ():
        symbols.extend(get_index_symbols(index_name))
    if args.queued:
        symbols.extend(queued_shareholding_symbols())
    for index_name in args.universe_index_names or ():
        constituents = get_index_symbols(index_name)
        index_delay = float(
            os.environ.get("NSE_XBRL_CALL_DELAY_SECONDS", "2")
        )
        if index_delay > 0:
            time.sleep(index_delay)
        if not constituents:
            raise SystemExit(
                f"Index {index_name!r} returned no constituents; universe was not changed"
            )
        active = seed_shareholding_universe(index_name, constituents)
        due = due_shareholding_universe_symbols(
            index_name,
            limit=args.limit or active,
            refresh_after_days=refresh_after_days,
            incomplete_retry_days=incomplete_retry_days,
        )
        log.info(
            "shareholding universe[%s]: %d active, %d due in this batch",
            index_name,
            active,
            len(due),
        )
        symbols.extend(due)
        for symbol in due:
            universe_memberships.setdefault(symbol, set()).add(index_name)
    symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol))
    if args.limit is not None:
        symbols = symbols[: args.limit]
    if not symbols:
        if (
            args.queued
            or args.universe_index_names
        ) and not args.symbols and not args.index_names:
            log.info("no queued or due shareholding symbols")
            return
        raise SystemExit("Provide one or more symbols or --index.")

    failed = []
    for symbol in symbols:
        try:
            history = warm_shareholding_history(symbol, args.periods)
            log.info(
                "shareholding[%s]: warmed %d periods (%s)",
                symbol,
                len(history.periods),
                history.status,
            )
            for universe in universe_memberships.get(symbol, ()):
                record_shareholding_universe_attempt(
                    universe,
                    symbol,
                    complete=history.complete,
                    periods_available=len(history.periods),
                )
        except NseShareholdingRequestError as error:
            raise SystemExit(
                f"Stopping warmer after repeated/blocked NSE request: {error}"
            ) from error
        except Exception:
            if symbol not in universe_memberships:
                failed.append(symbol)
            log.warning("shareholding[%s]: warm failed", symbol, exc_info=True)

    if failed:
        raise SystemExit(f"Shareholding warm failed for: {', '.join(failed)}")


if __name__ == "__main__":
    main()
