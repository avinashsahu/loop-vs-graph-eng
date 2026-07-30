#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# Catch up the configured recent bhavcopy window before the scan. Existing sessions
# require no NSE request, so this is safe even when the scheduler already refreshed it.
uv run bhavcopy.py backfill || true

# Grade prior decisions now that another completed session may be available. This is
# telemetry only: a damaged/missing local evaluation store must not prevent the scan.
if ! uv run evaluation.py update; then
    echo "warning: paper-outcome evaluation failed; continuing scan" >&2
fi

# IST explicitly -- the host machine's own system timezone may not be IST.
RUN_ID="overnight_$(TZ='Asia/Kolkata' date +%Y%m%d_%H%M)"
OVERNIGHT_INDEX="${NSE_OVERNIGHT_INDEX:-NIFTY NEXT 50}"
OVERNIGHT_LIMIT="${NSE_OVERNIGHT_SCAN_LIMIT:-}"
NSE_SCAN_LABEL="$RUN_ID" \
NSE_INDEX="$OVERNIGHT_INDEX" \
NSE_SCAN_LIMIT="$OVERNIGHT_LIMIT" \
uv run nse_trade_graph.py
uv run digest.py "$RUN_ID"
