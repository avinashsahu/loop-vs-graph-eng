#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# IST explicitly -- the host machine's own system timezone may not be IST.
RUN_ID="overnight_$(TZ='Asia/Kolkata' date +%Y%m%d_%H%M)"
NSE_SCAN_LABEL="$RUN_ID" NSE_INDEX="NIFTY TOTAL MKT" uv run nse_trade_graph.py
uv run digest.py "$RUN_ID"
