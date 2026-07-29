import argparse

from dotenv import load_dotenv

load_dotenv()

from logging_config import setup_logging
from market_time import is_market_hours
from nse_data import get_index_symbols
from shareholding import (
    NseShareholdingRequestError,
    queued_shareholding_symbols,
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
        "--queued",
        action="store_true",
        help="Warm symbols enqueued by cache-only live scans.",
    )
    parser.add_argument("--periods", type=int, default=5)
    parser.add_argument(
        "--allow-market-hours",
        action="store_true",
        help="Override the default guard that keeps this job out of NSE trading hours.",
    )
    return parser.parse_args()


def main():
    args = _arguments()
    if is_market_hours() and not args.allow_market_hours:
        raise SystemExit(
            "Refusing to warm during NSE market hours; retry after 15:30 IST or pass "
            "--allow-market-hours explicitly."
        )

    symbols = list(args.symbols)
    for index_name in args.index_names or ():
        symbols.extend(get_index_symbols(index_name))
    if args.queued:
        symbols.extend(queued_shareholding_symbols())
    symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol))
    if not symbols:
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
        except NseShareholdingRequestError as error:
            raise SystemExit(
                f"Stopping warmer after repeated/blocked NSE request: {error}"
            ) from error
        except Exception:
            failed.append(symbol)
            log.warning("shareholding[%s]: warm failed", symbol, exc_info=True)

    if failed:
        raise SystemExit(f"Shareholding warm failed for: {', '.join(failed)}")


if __name__ == "__main__":
    main()
