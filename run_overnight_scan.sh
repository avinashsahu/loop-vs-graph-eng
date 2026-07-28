#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

RUN_ID="overnight_$(date +%Y%m%d_%H%M)"
NSE_SCAN_LABEL="$RUN_ID" NSE_INDEX="NIFTY TOTAL MKT" uv run nse_trade_graph.py
uv run digest.py "$RUN_ID"
