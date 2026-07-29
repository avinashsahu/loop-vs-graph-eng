"""Persistent decision and outcome evaluation for paper-trading scans."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)
EVALUATOR_VERSION = "daily-reference-v2-target-aware"
PRICE_BASIS = "raw_unadjusted_bhavcopy"


@dataclass(frozen=True)
class DecisionReceipt:
    decision_id: str
    created: bool


@dataclass(frozen=True)
class ScanRunReceipt:
    run_id: str
    created: bool


@dataclass(frozen=True)
class OutcomeUpdateSummary:
    completed: int
    skipped_incomplete: int
    skipped_unevaluable: int


@dataclass(frozen=True)
class ImportSummary:
    imported: int
    existing: int
    invalid: int


class EvaluationLedger:
    """Small persistent boundary for immutable scan decisions."""

    def __init__(self, database_path: str):
        self.database_path = database_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    decision_timestamp TEXT NOT NULL,
                    decision_date TEXT NOT NULL,
                    scan_label TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    disposition TEXT,
                    reason_stage TEXT,
                    reason_code TEXT,
                    entry_price REAL,
                    stop_price REAL,
                    target_price REAL,
                    shares INTEGER,
                    technical_score REAL,
                    technical_verdict TEXT,
                    fundamental_verdict TEXT,
                    risk_verdict TEXT,
                    sentiment_verdict TEXT,
                    model_backend TEXT,
                    model_name TEXT,
                    llm_max_tokens INTEGER,
                    fundamental_llm_max_tokens INTEGER,
                    policy_version TEXT,
                    risk_plan_valid INTEGER NOT NULL DEFAULT 0,
                    raw_record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            decision_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(decisions)")
            }
            if "model_name" not in decision_columns:
                connection.execute("ALTER TABLE decisions ADD COLUMN model_name TEXT")
            if "disposition" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN disposition TEXT"
                )
            if "reason_stage" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN reason_stage TEXT"
                )
            if "reason_code" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN reason_code TEXT"
                )
            if "model_backend" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN model_backend TEXT"
                )
            if "llm_max_tokens" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN llm_max_tokens INTEGER"
                )
            if "fundamental_llm_max_tokens" not in decision_columns:
                connection.execute(
                    """
                    ALTER TABLE decisions
                    ADD COLUMN fundamental_llm_max_tokens INTEGER
                    """
                )
            if "risk_plan_valid" not in decision_columns:
                connection.execute(
                    """
                    ALTER TABLE decisions
                    ADD COLUMN risk_plan_valid INTEGER NOT NULL DEFAULT 0
                    """
                )
                legacy_plans = connection.execute(
                    """
                    SELECT decision_id, entry_price, stop_price, shares
                    FROM decisions
                    """
                ).fetchall()
                connection.executemany(
                    """
                    UPDATE decisions SET risk_plan_valid = 1
                    WHERE decision_id = ?
                    """,
                    [
                        (row["decision_id"],)
                        for row in legacy_plans
                        if self._is_valid_risk_plan(dict(row))
                    ],
                )
            if "policy_version" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN policy_version TEXT"
                )
            if "target_price" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN target_price REAL"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    run_id TEXT PRIMARY KEY,
                    scan_label TEXT NOT NULL,
                    policy_version TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_run_symbols (
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('requested', 'completed', 'failed')),
                    decision_id TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (run_id, symbol)
                )
                """
            )
            self._initialize_outcomes(connection)

    def _initialize_outcomes(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'outcomes'
            """
        ).fetchone()
        if existing:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(outcomes)")
            }
            if "methodology_key" not in columns:
                connection.execute("ALTER TABLE outcomes RENAME TO outcomes_v0")
                self._create_outcomes_table(connection)
                legacy_rows = connection.execute(
                    "SELECT * FROM outcomes_v0"
                ).fetchall()
                for row in legacy_rows:
                    methodology_key = self._methodology_key(
                        str(row["benchmark_symbol"]),
                        float(row["round_trip_cost_bps"]),
                        evaluator_version="legacy-v0",
                        price_basis=str(row["price_basis"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO outcomes (
                            decision_id, horizon_sessions, methodology_key,
                            evaluator_version, horizon_date, exit_price,
                            close_price, close_return_pct, gross_return_pct,
                            net_return_pct, benchmark_symbol,
                            benchmark_return_pct, excess_return_pct, mfe_pct,
                            mae_pct, stop_hit, stop_hit_date,
                            target_hit, target_hit_date, exit_reason,
                            round_trip_cost_bps, price_basis, evaluated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            row["decision_id"],
                            row["horizon_sessions"],
                            methodology_key,
                            "legacy-v0",
                            row["horizon_date"],
                            row["exit_price"],
                            row["close_price"],
                            row["close_return_pct"],
                            row["gross_return_pct"],
                            row["net_return_pct"],
                            row["benchmark_symbol"],
                            row["benchmark_return_pct"],
                            row["excess_return_pct"],
                            row["mfe_pct"],
                            row["mae_pct"],
                            row["stop_hit"],
                            row["stop_hit_date"],
                            0,
                            None,
                            "stop" if row["stop_hit"] else "horizon",
                            row["round_trip_cost_bps"],
                            row["price_basis"],
                            row["evaluated_at"],
                        ),
                    )
                connection.execute("DROP TABLE outcomes_v0")
                return
            if "target_hit" not in columns:
                connection.execute(
                    """
                    ALTER TABLE outcomes
                    ADD COLUMN target_hit INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "target_hit_date" not in columns:
                connection.execute(
                    "ALTER TABLE outcomes ADD COLUMN target_hit_date TEXT"
                )
            if "exit_reason" not in columns:
                connection.execute(
                    """
                    ALTER TABLE outcomes
                    ADD COLUMN exit_reason TEXT NOT NULL DEFAULT 'horizon'
                    """
                )
                connection.execute(
                    """
                    UPDATE outcomes SET exit_reason = 'stop'
                    WHERE stop_hit = 1
                    """
                )
            return
        self._create_outcomes_table(connection)

    @staticmethod
    def _create_outcomes_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE outcomes (
                decision_id TEXT NOT NULL,
                horizon_sessions INTEGER NOT NULL,
                methodology_key TEXT NOT NULL,
                evaluator_version TEXT NOT NULL,
                horizon_date TEXT NOT NULL,
                exit_price REAL NOT NULL,
                close_price REAL NOT NULL,
                close_return_pct REAL NOT NULL,
                gross_return_pct REAL NOT NULL,
                net_return_pct REAL NOT NULL,
                benchmark_symbol TEXT NOT NULL,
                benchmark_return_pct REAL NOT NULL,
                excess_return_pct REAL NOT NULL,
                mfe_pct REAL NOT NULL,
                mae_pct REAL NOT NULL,
                stop_hit INTEGER NOT NULL,
                stop_hit_date TEXT,
                target_hit INTEGER NOT NULL,
                target_hit_date TEXT,
                exit_reason TEXT NOT NULL,
                round_trip_cost_bps REAL NOT NULL,
                price_basis TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                PRIMARY KEY (
                    decision_id, horizon_sessions, methodology_key
                ),
                FOREIGN KEY (decision_id) REFERENCES decisions (decision_id)
            )
            """
        )

    def record_decision(self, record: dict[str, Any]) -> DecisionReceipt:
        timestamp = str(record["timestamp"])
        scan_label = str(record.get("scan_label") or "")
        symbol = str(record["symbol"]).upper()
        identity = f"{timestamp}\x1f{scan_label}\x1f{symbol}"
        decision_id = hashlib.sha256(identity.encode()).hexdigest()
        risk_plan = record.get("risk_plan") or {}
        assessment = record.get("technical_assessment") or {}
        evidence = assessment.get("evidence") or {}
        model_config = record.get("model_config") or {}
        risk_plan_valid = self._is_valid_risk_plan(risk_plan)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO decisions (
                    decision_id, decision_timestamp, decision_date, scan_label,
                    symbol, status, disposition, reason_stage, reason_code,
                    entry_price, stop_price, target_price, shares, technical_score,
                    technical_verdict, fundamental_verdict, risk_verdict,
                    sentiment_verdict, model_backend, model_name, llm_max_tokens,
                    fundamental_llm_max_tokens, policy_version, risk_plan_valid,
                    raw_record_json, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    decision_id,
                    timestamp,
                    datetime.fromisoformat(timestamp).date().isoformat(),
                    scan_label,
                    symbol,
                    str(record["status"]),
                    record.get("disposition"),
                    (record.get("decision_reason") or {}).get("stage"),
                    (record.get("decision_reason") or {}).get("code"),
                    risk_plan.get("entry_price"),
                    risk_plan.get("stop_price"),
                    risk_plan.get("target_price"),
                    risk_plan.get("shares"),
                    evidence.get("score"),
                    record.get("technical_verdict"),
                    record.get("fundamental_verdict"),
                    record.get("risk_verdict"),
                    record.get("sentiment_verdict"),
                    model_config.get("backend"),
                    model_config.get("name"),
                    model_config.get("max_tokens"),
                    model_config.get(
                        "fundamental_max_tokens",
                        model_config.get("max_tokens"),
                    ),
                    record.get("policy_version"),
                    int(risk_plan_valid),
                    json.dumps(record, sort_keys=True, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return DecisionReceipt(decision_id=decision_id, created=cursor.rowcount == 1)

    def start_scan_run(
        self,
        scan_label: str,
        symbols: list[str],
        policy_version: str | None,
        *,
        started_at: str | None = None,
    ) -> ScanRunReceipt:
        normalized_symbols = list(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in symbols
                if str(symbol).strip()
            )
        )
        if not normalized_symbols:
            raise ValueError("symbols must contain at least one symbol")
        timestamp = started_at or datetime.now(UTC).isoformat()
        datetime.fromisoformat(timestamp)
        label = scan_label.strip()
        identity = "\x1f".join(
            (timestamp, label, policy_version or "", *normalized_symbols)
        )
        run_id = hashlib.sha256(identity.encode()).hexdigest()

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO scan_runs (
                    run_id, scan_label, policy_version, started_at
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, label, policy_version, timestamp),
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO scan_run_symbols (
                    run_id, symbol, ordinal, status
                ) VALUES (?, ?, ?, 'requested')
                """,
                [
                    (run_id, symbol, ordinal)
                    for ordinal, symbol in enumerate(normalized_symbols)
                ],
            )
        return ScanRunReceipt(run_id=run_id, created=cursor.rowcount == 1)

    def finalize_scan_run(
        self,
        run_id: str,
        *,
        reason: str = "Scan ended before this symbol completed",
    ) -> None:
        completed_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scan_run_symbols
                SET status = 'failed',
                    error_type = 'IncompleteScan',
                    error_message = ?,
                    completed_at = ?
                WHERE run_id = ? AND status = 'requested'
                """,
                (reason[:500], completed_at, run_id),
            )
            cursor = connection.execute(
                """
                UPDATE scan_runs
                SET completed_at = COALESCE(completed_at, ?)
                WHERE run_id = ?
                """,
                (completed_at, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown scan run: {run_id}")

    def finalize_stale_scan_runs(
        self,
        stale_before: str,
        *,
        reason: str = "No terminal event before the stale-run timeout",
    ) -> int:
        datetime.fromisoformat(stale_before)
        completed_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            stale_runs = connection.execute(
                """
                SELECT run_id
                FROM scan_runs
                WHERE completed_at IS NULL
                  AND julianday(started_at) <= julianday(?)
                """,
                (stale_before,),
            ).fetchall()
            run_ids = [row["run_id"] for row in stale_runs]
            connection.executemany(
                """
                UPDATE scan_run_symbols
                SET status = 'failed',
                    error_type = 'RecoveredIncompleteScan',
                    error_message = ?,
                    completed_at = ?
                WHERE run_id = ? AND status = 'requested'
                """,
                [
                    (reason[:500], completed_at, run_id)
                    for run_id in run_ids
                ],
            )
            connection.executemany(
                """
                UPDATE scan_runs
                SET completed_at = COALESCE(completed_at, ?)
                WHERE run_id = ?
                """,
                [(completed_at, run_id) for run_id in run_ids],
            )
        return len(run_ids)

    def record_scan_symbol(
        self,
        run_id: str,
        symbol: str,
        *,
        decision_id: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        normalized_symbol = symbol.strip().upper()
        completed_at = datetime.now(UTC).isoformat()
        status = "failed" if error is not None else "completed"
        error_type = type(error).__name__ if error is not None else None
        error_message = str(error)[:500] if error is not None else None

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scan_run_symbols
                SET status = ?, decision_id = ?, error_type = ?,
                    error_message = ?, completed_at = ?
                WHERE run_id = ? AND symbol = ? AND status = 'requested'
                """,
                (
                    status,
                    decision_id,
                    error_type,
                    error_message,
                    completed_at,
                    run_id,
                    normalized_symbol,
                ),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT status FROM scan_run_symbols
                    WHERE run_id = ? AND symbol = ?
                    """,
                    (run_id, normalized_symbol),
                ).fetchone()
                if existing is None:
                    raise KeyError(
                        f"unknown scan run symbol: {run_id}/{normalized_symbol}"
                    )
                if existing["status"] != status:
                    raise ValueError(
                        f"scan run symbol already recorded as {existing['status']}"
                    )
            pending = connection.execute(
                """
                SELECT 1 FROM scan_run_symbols
                WHERE run_id = ? AND status = 'requested'
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if pending is None:
                connection.execute(
                    """
                    UPDATE scan_runs
                    SET completed_at = COALESCE(completed_at, ?)
                    WHERE run_id = ?
                    """,
                    (completed_at, run_id),
                )

    def scan_run_summary(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM scan_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown scan run: {run_id}")
            symbols = connection.execute(
                """
                SELECT symbol, status, decision_id, error_type, error_message
                FROM scan_run_symbols
                WHERE run_id = ?
                ORDER BY ordinal
                """,
                (run_id,),
            ).fetchall()
        counts: dict[str, int] = {}
        for row in symbols:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "run_id": run["run_id"],
            "scan_label": run["scan_label"],
            "policy_version": run["policy_version"],
            "started_at": run["started_at"],
            "completed_at": run["completed_at"],
            "counts": counts,
            "symbols": [dict(row) for row in symbols],
        }

    def status_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM decisions GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def import_jsonl(self, path: str | Path) -> ImportSummary:
        imported = 0
        existing = 0
        invalid = 0
        with Path(path).open() as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    receipt = self.record_decision(record)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    invalid += 1
                    log.warning(
                        "skipping invalid decision record at %s:%d: %s",
                        path,
                        line_number,
                        error,
                    )
                    continue
                if receipt.created:
                    imported += 1
                else:
                    existing += 1
        return ImportSummary(
            imported=imported,
            existing=existing,
            invalid=invalid,
        )

    def update_outcomes(
        self,
        bhavcopy_database_path: str,
        *,
        horizons: tuple[int, ...] = (1, 5, 10, 20),
        benchmark_symbol: str = "JUNIORBEES",
        round_trip_cost_bps: float = 30.0,
    ) -> OutcomeUpdateSummary:
        normalized_horizons = tuple(sorted(set(horizons)))
        if not normalized_horizons or any(horizon <= 0 for horizon in normalized_horizons):
            raise ValueError("horizons must contain positive session counts")
        round_trip_cost_bps = float(round_trip_cost_bps)
        if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps < 0:
            raise ValueError("round_trip_cost_bps must be finite and nonnegative")
        benchmark_symbol = benchmark_symbol.strip().upper()
        if not benchmark_symbol:
            raise ValueError("benchmark_symbol must not be empty")
        methodology_key = self._methodology_key(
            benchmark_symbol,
            round_trip_cost_bps,
        )

        with self._connect() as connection:
            decisions = connection.execute(
                """
                WITH actionable AS (
                    SELECT
                        decision_id, decision_date, symbol, entry_price,
                        stop_price, target_price, shares, risk_plan_valid,
                        decision_timestamp,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                decision_date,
                                symbol,
                                COALESCE(policy_version, '')
                            ORDER BY decision_timestamp, decision_id
                        ) AS signal_rank
                    FROM decisions
                    WHERE status IN ('proposed', 'flagged_for_review')
                      AND risk_plan_valid = 1
                )
                SELECT decision_id, decision_date, symbol, entry_price, stop_price,
                       target_price, shares, risk_plan_valid
                FROM actionable
                WHERE signal_rank = 1
                ORDER BY decision_timestamp, decision_id
                """
            ).fetchall()
            unevaluable = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT decision_date, symbol, policy_version
                        FROM decisions
                        WHERE status IN ('proposed', 'flagged_for_review')
                        GROUP BY
                            decision_date,
                            symbol,
                            COALESCE(policy_version, '')
                        HAVING MAX(risk_plan_valid) = 0
                    )
                    """
                ).fetchone()[0]
            )

        completed = 0
        skipped_incomplete = 0
        skipped_unevaluable = unevaluable * len(normalized_horizons)
        outcome_rows: list[tuple[Any, ...]] = []
        with sqlite3.connect(bhavcopy_database_path) as prices:
            prices.row_factory = sqlite3.Row
            for decision in decisions:
                entry_price = decision["entry_price"]
                max_horizon = normalized_horizons[-1]
                benchmark_bars = prices.execute(
                    """
                    SELECT date, open, close
                    FROM bhavcopy
                    WHERE symbol = ? AND date > ?
                    ORDER BY date
                    LIMIT ?
                    """,
                    (benchmark_symbol, decision["decision_date"], max_horizon),
                ).fetchall()
                bars = []
                if benchmark_bars:
                    bars = prices.execute(
                        """
                        SELECT date, open, high, low, close
                        FROM bhavcopy
                        WHERE symbol = ? AND date > ? AND date <= ?
                        ORDER BY date
                        """,
                        (
                            decision["symbol"],
                            decision["decision_date"],
                            benchmark_bars[-1]["date"],
                        ),
                    ).fetchall()
                bars_by_date = {row["date"]: row for row in bars}

                for horizon in normalized_horizons:
                    benchmark_window = benchmark_bars[:horizon]
                    if len(benchmark_window) < horizon:
                        skipped_incomplete += 1
                        continue
                    window = [
                        bars_by_date.get(row["date"]) for row in benchmark_window
                    ]
                    if any(row is None for row in benchmark_window):
                        skipped_incomplete += 1
                        continue
                    if any(row is None for row in window):
                        skipped_incomplete += 1
                        continue
                    if any(
                        not self._is_finite_positive(row[field])
                        for row in window
                        for field in ("open", "high", "low", "close")
                    ):
                        skipped_incomplete += 1
                        continue
                    if any(
                        not self._is_finite_positive(row[field])
                        for row in benchmark_window
                        for field in ("open", "close")
                    ):
                        skipped_incomplete += 1
                        continue

                    entry = float(entry_price)
                    stop_price = decision["stop_price"]
                    target_price = decision["target_price"]
                    stop_hit_date = None
                    target_hit_date = None
                    exit_reason = "horizon"
                    held_window = window
                    exit_price = float(window[-1]["close"])
                    for index, bar in enumerate(window):
                        open_price = float(bar["open"])
                        if (
                            stop_price is not None
                            and open_price <= float(stop_price)
                        ):
                            stop_hit_date = str(bar["date"])
                            exit_reason = "stop"
                            exit_price = open_price
                            held_window = window[: index + 1]
                            break
                        if (
                            target_price is not None
                            and open_price >= float(target_price)
                        ):
                            target_hit_date = str(bar["date"])
                            exit_reason = "target"
                            exit_price = open_price
                            held_window = window[: index + 1]
                            break
                        stop_touched = (
                            stop_price is not None
                            and bar["low"] is not None
                            and float(bar["low"]) <= float(stop_price)
                        )
                        target_touched = (
                            target_price is not None
                            and bar["high"] is not None
                            and float(bar["high"]) >= float(target_price)
                        )
                        if stop_touched:
                            stop_hit_date = str(bar["date"])
                            if target_touched:
                                target_hit_date = str(bar["date"])
                                exit_reason = "both_hit_stop_first"
                            else:
                                exit_reason = "stop"
                            exit_price = float(stop_price)
                            held_window = window[: index + 1]
                            break
                        if target_touched:
                            target_hit_date = str(bar["date"])
                            exit_reason = "target"
                            exit_price = float(target_price)
                            held_window = window[: index + 1]
                            break

                    final_close = float(window[-1]["close"])
                    gross_return_pct = (exit_price / entry - 1) * 100
                    net_return_pct = gross_return_pct - round_trip_cost_bps / 100
                    close_return_pct = (final_close / entry - 1) * 100
                    benchmark_open = float(benchmark_window[0]["open"])
                    benchmark_close = float(benchmark_window[-1]["close"])
                    benchmark_return_pct = (
                        benchmark_close / benchmark_open - 1
                    ) * 100
                    outcome_rows.append(
                        (
                            decision["decision_id"],
                            horizon,
                            methodology_key,
                            EVALUATOR_VERSION,
                            window[-1]["date"],
                            exit_price,
                            final_close,
                            close_return_pct,
                            gross_return_pct,
                            net_return_pct,
                            benchmark_symbol,
                            benchmark_return_pct,
                            net_return_pct - benchmark_return_pct,
                            (max(float(row["high"]) for row in held_window) / entry - 1)
                            * 100,
                            (min(float(row["low"]) for row in held_window) / entry - 1)
                            * 100,
                            int(stop_hit_date is not None),
                            stop_hit_date,
                            int(target_hit_date is not None),
                            target_hit_date,
                            exit_reason,
                            round_trip_cost_bps,
                            PRICE_BASIS,
                            datetime.now(UTC).isoformat(),
                        )
                    )
                    completed += 1

        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO outcomes (
                    decision_id, horizon_sessions, methodology_key,
                    evaluator_version, horizon_date, exit_price, close_price,
                    close_return_pct, gross_return_pct, net_return_pct,
                    benchmark_symbol, benchmark_return_pct, excess_return_pct,
                    mfe_pct, mae_pct, stop_hit, stop_hit_date,
                    target_hit, target_hit_date, exit_reason,
                    round_trip_cost_bps, price_basis, evaluated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT (
                    decision_id, horizon_sessions, methodology_key
                ) DO UPDATE SET
                    evaluator_version = excluded.evaluator_version,
                    horizon_date = excluded.horizon_date,
                    exit_price = excluded.exit_price,
                    close_price = excluded.close_price,
                    close_return_pct = excluded.close_return_pct,
                    gross_return_pct = excluded.gross_return_pct,
                    net_return_pct = excluded.net_return_pct,
                    benchmark_symbol = excluded.benchmark_symbol,
                    benchmark_return_pct = excluded.benchmark_return_pct,
                    excess_return_pct = excluded.excess_return_pct,
                    mfe_pct = excluded.mfe_pct,
                    mae_pct = excluded.mae_pct,
                    stop_hit = excluded.stop_hit,
                    stop_hit_date = excluded.stop_hit_date,
                    target_hit = excluded.target_hit,
                    target_hit_date = excluded.target_hit_date,
                    exit_reason = excluded.exit_reason,
                    round_trip_cost_bps = excluded.round_trip_cost_bps,
                    price_basis = excluded.price_basis,
                    evaluated_at = excluded.evaluated_at
                """,
                outcome_rows,
            )
        return OutcomeUpdateSummary(
            completed=completed,
            skipped_incomplete=skipped_incomplete,
            skipped_unevaluable=skipped_unevaluable,
        )

    def decision_outcomes(self, decision_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM outcomes
                WHERE decision_id = ?
                ORDER BY horizon_sessions, methodology_key
                """,
                (decision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def calibration_report(self) -> dict[str, Any]:
        with self._connect() as connection:
            decision_total = int(
                connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            )
            evaluable = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT
                            risk_plan_valid,
                            ROW_NUMBER() OVER (
                                PARTITION BY
                                    decision_date,
                                    symbol,
                                    COALESCE(policy_version, '')
                                ORDER BY decision_timestamp, decision_id
                            ) AS signal_rank
                        FROM decisions
                        WHERE status IN ('proposed', 'flagged_for_review')
                          AND risk_plan_valid = 1
                    )
                    WHERE signal_rank = 1
                    """
                ).fetchone()[0]
            )
            raw_evaluable = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM decisions
                    WHERE status IN ('proposed', 'flagged_for_review')
                      AND risk_plan_valid = 1
                    """
                ).fetchone()[0]
            )
            outcomes = connection.execute(
                """
                WITH canonical AS (
                    SELECT
                        decision_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                decision_date,
                                symbol,
                                COALESCE(policy_version, '')
                            ORDER BY decision_timestamp, decision_id
                        ) AS signal_rank
                    FROM decisions
                    WHERE status IN ('proposed', 'flagged_for_review')
                      AND risk_plan_valid = 1
                )
                SELECT outcomes.*, decisions.technical_score,
                       decisions.model_name,
                       COALESCE(
                           decisions.fundamental_llm_max_tokens,
                           CASE
                               WHEN json_valid(decisions.raw_record_json)
                               THEN CAST(
                                   json_extract(
                                       decisions.raw_record_json,
                                       '$.model_config.fundamental_max_tokens'
                                   ) AS INTEGER
                               )
                           END,
                           decisions.llm_max_tokens
                       ) AS llm_max_tokens,
                       decisions.model_backend, decisions.policy_version,
                       decisions.target_price
                FROM outcomes
                JOIN decisions USING (decision_id)
                JOIN canonical USING (decision_id)
                WHERE canonical.signal_rank = 1
                ORDER BY horizon_sessions, decision_id
                """
            ).fetchall()
            canonical_decisions = connection.execute(
                """
                WITH canonical AS (
                    SELECT
                        decision_id,
                        decision_date,
                        symbol,
                        policy_version,
                        status,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                decision_date,
                                symbol,
                                COALESCE(policy_version, '')
                            ORDER BY decision_timestamp, decision_id
                        ) AS signal_rank
                    FROM decisions
                    WHERE status IN ('proposed', 'flagged_for_review')
                      AND risk_plan_valid = 1
                )
                SELECT
                    decision_id, decision_date, symbol, policy_version, status
                FROM canonical
                WHERE signal_rank = 1
                ORDER BY decision_date, symbol, policy_version
                """
            ).fetchall()
            model_configs = connection.execute(
                """
                SELECT
                    model_backend,
                    model_name,
                    effective_max_tokens AS llm_max_tokens,
                    COUNT(*) AS count
                FROM (
                    SELECT
                        model_backend,
                        model_name,
                        COALESCE(
                            fundamental_llm_max_tokens,
                            CASE
                                WHEN json_valid(raw_record_json)
                                THEN CAST(
                                    json_extract(
                                        raw_record_json,
                                        '$.model_config.fundamental_max_tokens'
                                    ) AS INTEGER
                                )
                            END,
                            llm_max_tokens
                        ) AS effective_max_tokens
                    FROM decisions
                )
                GROUP BY model_backend, model_name, effective_max_tokens
                ORDER BY model_backend, model_name, effective_max_tokens
                """
            ).fetchall()
            policy_versions = connection.execute(
                """
                SELECT policy_version, COUNT(*) AS count
                FROM decisions
                GROUP BY policy_version
                ORDER BY policy_version
                """
            ).fetchall()
            reason_codes = connection.execute(
                """
                SELECT reason_stage, reason_code, COUNT(*) AS count
                FROM decisions
                WHERE reason_code IS NOT NULL
                GROUP BY reason_stage, reason_code
                ORDER BY reason_stage, reason_code
                """
            ).fetchall()

        by_horizon: dict[int, list[sqlite3.Row]] = {}
        score_bands: dict[
            str, dict[str, dict[int, list[sqlite3.Row]]]
        ] = {}
        methodology_performance: dict[str, dict[int, list[sqlite3.Row]]] = {}
        methodology_metadata: dict[str, sqlite3.Row] = {}
        methodology_score_bands: dict[
            str,
            dict[str, dict[str, dict[int, list[sqlite3.Row]]]],
        ] = {}
        methodology_models: dict[
            str,
            dict[
                tuple[str | None, str | None, int | None, str | None],
                dict[int, list[sqlite3.Row]],
            ],
        ] = {}
        model_performance: dict[
            tuple[str | None, str | None, int | None, str | None],
            dict[int, list[sqlite3.Row]],
        ] = {}
        for row in outcomes:
            horizon = int(row["horizon_sessions"])
            methodology_key = str(row["methodology_key"])
            methodology_metadata[methodology_key] = row
            by_horizon.setdefault(horizon, []).append(row)
            band = self._score_band(row["technical_score"])
            policy_key = str(row["policy_version"] or "unversioned")
            score_bands.setdefault(policy_key, {}).setdefault(
                band, {}
            ).setdefault(horizon, []).append(row)
            methodology_score_bands.setdefault(
                methodology_key, {}
            ).setdefault(policy_key, {}).setdefault(band, {}).setdefault(
                horizon, []
            ).append(row)
            model_key = (
                row["model_backend"],
                row["model_name"],
                row["llm_max_tokens"],
                row["policy_version"],
            )
            model_performance.setdefault(model_key, {}).setdefault(
                horizon, []
            ).append(row)
            methodology_models.setdefault(methodology_key, {}).setdefault(
                model_key, {}
            ).setdefault(horizon, []).append(row)
            methodology_performance.setdefault(methodology_key, {}).setdefault(
                horizon, []
            ).append(row)

        return {
            "decisions": {
                "total": decision_total,
                "status_counts": self.status_counts(),
                "evaluable": evaluable,
                "raw_evaluable": raw_evaluable,
                "repeated_evaluable": raw_evaluable - evaluable,
                "canonical": [dict(row) for row in canonical_decisions],
                "reason_codes": [
                    {
                        "stage": row["reason_stage"],
                        "code": row["reason_code"],
                        "count": int(row["count"]),
                    }
                    for row in reason_codes
                ],
                "model_configs": [
                    {
                        "backend": row["model_backend"],
                        "name": row["model_name"],
                        "max_tokens": row["llm_max_tokens"],
                        "count": int(row["count"]),
                    }
                    for row in model_configs
                ],
                "policy_versions": [
                    {
                        "version": row["policy_version"],
                        "count": int(row["count"]),
                    }
                    for row in policy_versions
                ],
            },
            "horizons": {
                str(horizon): self._summarize_outcomes(rows)
                for horizon, rows in sorted(by_horizon.items())
            }
            if len(methodology_performance) <= 1
            else {},
            "technical_score_bands": self._render_score_bands(score_bands)
            if len(methodology_performance) <= 1
            else {},
            "model_performance": self._render_model_performance(
                model_performance
            )
            if len(methodology_performance) <= 1
            else [],
            "methodology_performance": [
                {
                    "methodology_key": methodology_key,
                    "evaluator_version": methodology_metadata[
                        methodology_key
                    ]["evaluator_version"],
                    "benchmark_symbol": methodology_metadata[
                        methodology_key
                    ]["benchmark_symbol"],
                    "round_trip_cost_bps": methodology_metadata[
                        methodology_key
                    ]["round_trip_cost_bps"],
                    "price_basis": methodology_metadata[
                        methodology_key
                    ]["price_basis"],
                    "horizons": {
                        str(horizon): self._summarize_outcomes(rows)
                        for horizon, rows in sorted(horizons.items())
                    },
                    "technical_score_bands": self._render_score_bands(
                        methodology_score_bands[methodology_key]
                    ),
                    "model_performance": self._render_model_performance(
                        methodology_models[methodology_key]
                    ),
                }
                for methodology_key, horizons in sorted(
                    methodology_performance.items()
                )
            ],
            "methodology": {
                "scope": "selected_candidate_evaluation",
                "canonical_signal": (
                    "first proposed/review decision with a validated risk plan "
                    "per symbol, decision_date, and policy_version"
                ),
                "decision_cutoff": "bhavcopy sessions strictly after decision_date",
                "session_calendar": (
                    "benchmark dates; a missing stock bar makes the horizon incomplete"
                ),
                "entry_basis": "recorded risk_plan.entry_price reference, not a fill",
                "stop_fill": "session open when below stop, otherwise stop price",
                "target_fill": (
                    "session open when above target, otherwise target price"
                ),
                "same_bar_order": (
                    "both stop and target touched after open is treated as stop first"
                ),
                "stop_bar_excursion": (
                    "full daily stop bar included; intraday high/low order unknown"
                ),
                "benchmark_entry": "first aligned future session open",
                "price_basis": PRICE_BASIS,
                "cost_assumptions_bps": sorted(
                    {
                        float(row["round_trip_cost_bps"])
                        for row in outcomes
                    }
                ),
            },
        }

    @staticmethod
    def _summarize_outcomes(
        rows: list[sqlite3.Row],
    ) -> dict[str, float | int | None]:
        net_returns = [float(row["net_return_pct"]) for row in rows]
        gross_returns = [float(row["gross_return_pct"]) for row in rows]
        close_returns = [float(row["close_return_pct"]) for row in rows]
        target_eligible = [
            row
            for row in rows
            if row["target_price"] is not None
            and math.isfinite(float(row["target_price"]))
            and float(row["target_price"]) > 0
        ]
        return {
            "count": len(rows),
            "mean_gross_return_pct": statistics.fmean(gross_returns),
            "median_gross_return_pct": statistics.median(gross_returns),
            "mean_net_return_pct": statistics.fmean(net_returns),
            "median_net_return_pct": statistics.median(net_returns),
            "mean_horizon_close_return_pct": statistics.fmean(close_returns),
            "win_rate_pct": sum(value > 0 for value in net_returns)
            / len(rows)
            * 100,
            "stop_rate_pct": sum(int(row["stop_hit"]) for row in rows)
            / len(rows)
            * 100,
            "target_eligible_count": len(target_eligible),
            "target_rate_pct": (
                sum(row["exit_reason"] == "target" for row in target_eligible)
                / len(target_eligible)
                * 100
                if target_eligible
                else None
            ),
            "target_touch_rate_pct": (
                sum(int(row["target_hit"]) for row in target_eligible)
                / len(target_eligible)
                * 100
                if target_eligible
                else None
            ),
            "same_bar_ambiguity_rate_pct": (
                sum(
                    row["exit_reason"] == "both_hit_stop_first"
                    for row in target_eligible
                )
                / len(target_eligible)
                * 100
                if target_eligible
                else None
            ),
            "mean_excess_return_pct": statistics.fmean(
                float(row["excess_return_pct"]) for row in rows
            ),
            "mean_mfe_pct": statistics.fmean(
                float(row["mfe_pct"]) for row in rows
            ),
            "mean_mae_pct": statistics.fmean(
                float(row["mae_pct"]) for row in rows
            ),
        }

    @classmethod
    def _render_score_bands(
        cls,
        score_bands: dict[
            str, dict[str, dict[int, list[sqlite3.Row]]]
        ],
    ) -> dict[
        str, dict[str, dict[str, dict[str, float | int]]]
    ]:
        return {
            policy_version: {
                band: {
                    str(horizon): cls._summarize_outcomes(rows)
                    for horizon, rows in sorted(horizons.items())
                }
                for band, horizons in bands.items()
            }
            for policy_version, bands in score_bands.items()
        }

    @classmethod
    def _render_model_performance(
        cls,
        model_performance: dict[
            tuple[str | None, str | None, int | None, str | None],
            dict[int, list[sqlite3.Row]],
        ],
    ) -> list[dict[str, Any]]:
        return [
            {
                "backend": model_backend,
                "name": model_name,
                "max_tokens": max_tokens,
                "policy_version": policy_version,
                "horizons": {
                    str(horizon): cls._summarize_outcomes(rows)
                    for horizon, rows in sorted(horizons.items())
                },
            }
            for (
                model_backend,
                model_name,
                max_tokens,
                policy_version,
            ), horizons in sorted(
                model_performance.items(),
                key=lambda item: (
                    item[0][0] or "",
                    item[0][1] or "",
                    item[0][2] if item[0][2] is not None else -1,
                    item[0][3] or "",
                ),
            )
        ]

    @staticmethod
    def _score_band(score: float | None) -> str:
        if score is None:
            return "missing"
        if score <= 0:
            return "<=0"
        if score <= 0.25:
            return "(0,0.25]"
        if score <= 0.5:
            return "(0.25,0.50]"
        if score <= 0.75:
            return "(0.50,0.75]"
        return ">0.75"

    @staticmethod
    def _is_valid_risk_plan(risk_plan: dict[str, Any]) -> bool:
        try:
            entry = float(risk_plan["entry_price"])
            stop = float(risk_plan["stop_price"])
            shares = float(risk_plan["shares"])
            target_value = risk_plan.get("target_price")
            target = (
                float(target_value)
                if target_value is not None
                else None
            )
        except (KeyError, TypeError, ValueError):
            return False
        return (
            math.isfinite(entry)
            and math.isfinite(stop)
            and math.isfinite(shares)
            and entry > 0
            and 0 < stop < entry
            and shares > 0
            and shares.is_integer()
            and (
                target is None
                or (math.isfinite(target) and target > entry)
            )
        )

    @staticmethod
    def _is_finite_positive(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    @staticmethod
    def _methodology_key(
        benchmark_symbol: str,
        round_trip_cost_bps: float,
        *,
        evaluator_version: str = EVALUATOR_VERSION,
        price_basis: str = PRICE_BASIS,
    ) -> str:
        descriptor = json.dumps(
            {
                "benchmark_symbol": benchmark_symbol.strip().upper(),
                "evaluator_version": evaluator_version,
                "price_basis": price_basis,
                "round_trip_cost_bps": float(round_trip_cost_bps),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(descriptor.encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record and evaluate paper outcomes for NSE scan decisions."
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("EVALUATION_DB_PATH", "evaluation.db"),
        help="evaluation SQLite path (default: EVALUATION_DB_PATH/evaluation.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-jsonl",
        help="idempotently import the legacy/fallback trade log",
    )
    import_parser.add_argument(
        "path",
        nargs="?",
        default=os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl"),
    )

    update_parser = subparsers.add_parser(
        "update",
        help="evaluate horizons that have enough completed bhavcopy sessions",
    )
    update_parser.add_argument(
        "--bhavcopy-database",
        default=os.environ.get("BHAVCOPY_DB_PATH", "bhavcopy.db"),
    )
    update_parser.add_argument(
        "--benchmark",
        default=os.environ.get("EVALUATION_BENCHMARK_SYMBOL", "JUNIORBEES"),
    )
    update_parser.add_argument(
        "--round-trip-cost-bps",
        type=float,
        default=float(os.environ.get("EVALUATION_ROUND_TRIP_COST_BPS", "30")),
    )
    update_parser.add_argument(
        "--horizons",
        nargs="+",
        type=int,
        default=(1, 5, 10, 20),
        metavar="SESSIONS",
    )
    subparsers.add_parser("report", help="print the current calibration report")

    args = parser.parse_args(argv)
    ledger = EvaluationLedger(args.database)
    if args.command == "import-jsonl":
        result: Any = ledger.import_jsonl(args.path)
        payload = result.__dict__
    elif args.command == "update":
        result = ledger.update_outcomes(
            args.bhavcopy_database,
            horizons=tuple(args.horizons),
            benchmark_symbol=args.benchmark,
            round_trip_cost_bps=args.round_trip_cost_bps,
        )
        payload = result.__dict__
    else:
        payload = ledger.calibration_report()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
