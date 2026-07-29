#!/usr/bin/env bash
set -euo pipefail

# One NSE archive request contains the whole EQ market. Keep this deliberately
# slow and resumable: bhavcopy.py skips dates already present in SQLite.
export BHAVCOPY_BACKFILL_DAYS="${BHAVCOPY_BACKFILL_DAYS:-30}"
export BHAVCOPY_REQUEST_DELAY_SECONDS="${BHAVCOPY_REQUEST_DELAY_SECONDS:-2}"

uv run bhavcopy.py backfill "${1:-$BHAVCOPY_BACKFILL_DAYS}"
