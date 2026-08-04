"""Render a static status.html snapshot of scheduler jobs, run history, and
recent intraday alerts, for a quick "is everything actually running" check.

Regenerated every scheduler tick (see app_scheduler.main) and can also be run
standalone: ``uv run scheduler_dashboard.py``.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import app_scheduler  # noqa: E402
from market_time import now_ist  # noqa: E402

DASHBOARD_PATH = Path(os.environ.get("APP_SCHEDULER_DASHBOARD_PATH", "dashboard.html"))
INTRADAY_ALERT_STATE_PATH = os.environ.get(
    "INTRADAY_ALERT_STATE_PATH",
    ".intraday_alert_state.json",
)
HISTORY_TAIL = 40


def _resolve(path: Path | str) -> Path:
    path = Path(path)
    return path if path.is_absolute() else app_scheduler.REPO_ROOT / path


def _read_history_tail(limit: int = HISTORY_TAIL) -> list[dict]:
    target = _resolve(app_scheduler.HISTORY_PATH)
    if not target.exists():
        return []
    lines = target.read_text().splitlines()[-limit:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    entries.reverse()
    return entries


def _read_intraday_alerts() -> dict:
    target = _resolve(INTRADAY_ALERT_STATE_PATH)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text()).get("alerts", {})
    except (ValueError, OSError):
        return {}


def _status_badge(status: str | None) -> str:
    colors = {
        "success": "#2e7d32",
        "unavailable": "#8d8d8d",
        "failed": "#c62828",
        "running": "#1565c0",
    }
    color = colors.get(status or "", "#8d8d8d")
    label = html.escape(status or "never run")
    return f'<span class="badge" style="background:{color}">{label}</span>'


def _job_rows(jobs: list[dict], *, interactive: bool = False) -> str:
    rows = []
    for job in jobs:
        record = job["last_record"] or {}
        flags = []
        if record.get("failure_alerted"):
            flags.append("failure alerted")
        if record.get("stale_alerted"):
            flags.append("STALE ALERT SENT")
        if job.get("override") is not None:
            flags.append(f"override: {'enabled' if job['override'] else 'disabled'}")
        if job.get("force_run_requested"):
            flags.append("run-now queued")
        enabled_badge = (
            '<span class="badge" style="background:#2e7d32">enabled</span>'
            if job["enabled"]
            else '<span class="badge" style="background:#616161">disabled</span>'
        )
        due_badge = (
            '<span class="badge" style="background:#1565c0">due now</span>'
            if job["due_now"]
            else ""
        )
        name = html.escape(job["name"])
        controls_cell = ""
        if interactive:
            toggle_label = "Disable" if job["enabled"] else "Enable"
            toggle_value = "0" if job["enabled"] else "1"
            run_disabled = " disabled" if job.get("force_run_requested") else ""
            controls_cell = (
                "<td>"
                f'<form method="post" action="/toggle" style="display:inline">'
                f'<input type="hidden" name="job" value="{name}">'
                f'<input type="hidden" name="enabled" value="{toggle_value}">'
                f'<button type="submit">{toggle_label}</button>'
                "</form> "
                f'<form method="post" action="/run-now" style="display:inline">'
                f'<input type="hidden" name="job" value="{name}">'
                f'<button type="submit"{run_disabled}>'
                f'{"Queued..." if job.get("force_run_requested") else "Run now"}</button>'
                "</form>"
                "</td>"
            )
        rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td>{enabled_badge}</td>"
            f"<td>{_status_badge(record.get('status'))} {due_badge}</td>"
            f"<td>{html.escape(str(record.get('occurrence') or job['current_occurrence'] or '-'))}</td>"
            f"<td>{html.escape(record.get('finished_at') or '-')}</td>"
            f"<td>{html.escape(str(record.get('return_code'))) if record.get('return_code') is not None else '-'}</td>"
            f"<td>{html.escape(', '.join(flags))}</td>"
            f"{controls_cell}"
            "</tr>"
        )
    return "\n".join(rows)


def _history_rows(history: list[dict]) -> str:
    rows = []
    for entry in history:
        rows.append(
            "<tr>"
            f"<td>{html.escape(entry.get('attempted_at', '-'))}</td>"
            f"<td>{html.escape(entry.get('job', '-'))}</td>"
            f"<td>{_status_badge(entry.get('status'))}</td>"
            f"<td>{html.escape(str(entry.get('occurrence', '-')))}</td>"
            f"<td>{html.escape(str(entry.get('return_code')))}</td>"
            "</tr>"
        )
    return "\n".join(rows) or "<tr><td colspan=5>No history recorded yet.</td></tr>"


def _alert_rows(alerts: dict) -> str:
    rows = []
    for key, alert in sorted(alerts.items(), reverse=True)[:40]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(key)}</td>"
            f"<td>{html.escape(alert.get('status', '-'))}</td>"
            f"<td>{html.escape(', '.join(alert.get('delivered_channels', [])) or 'none')}</td>"
            "</tr>"
        )
    return "\n".join(rows) or "<tr><td colspan=3>No intraday alerts recorded yet.</td></tr>"


def render(*, interactive: bool = False) -> str:
    generated_at = now_ist().isoformat()
    jobs = app_scheduler.job_status_summary()
    history = _read_history_tail()
    alerts = _read_intraday_alerts()
    controls_header = "<th>Controls</th>" if interactive else ""
    refresh_note = (
        "live &middot; toggles/run-now take effect on the scheduler's next tick"
        if interactive
        else "refreshes every 30s"
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>NSE Stock Picker &mdash; Scheduler Dashboard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; background: #0e0e10; color: #e8e8e8; }}
  @media (prefers-color-scheme: light) {{ body {{ background: #fafafa; color: #1a1a1a; }} }}
  h1 {{ font-size: 1.3rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 2.5rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #333; }}
  @media (prefers-color-scheme: light) {{ th, td {{ border-bottom: 1px solid #ddd; }} }}
  th {{ opacity: 0.7; font-weight: 600; }}
  .badge {{ color: white; border-radius: 4px; padding: 0.1rem 0.5rem; font-size: 0.75rem; white-space: nowrap; }}
  .meta {{ opacity: 0.6; font-size: 0.8rem; }}
  button {{ font-size: 0.75rem; cursor: pointer; }}
</style>
</head>
<body>
<h1>NSE Stock Picker &mdash; Scheduler Dashboard</h1>
<p class="meta">Generated {html.escape(generated_at)} IST &middot; {refresh_note}</p>

<h2>Configured jobs</h2>
<table>
<tr><th>Job</th><th>Enabled</th><th>Last status</th><th>Occurrence</th><th>Finished at</th><th>Return code</th><th>Notes</th>{controls_header}</tr>
{_job_rows(jobs, interactive=interactive)}
</table>

<h2>Recent run history (last {HISTORY_TAIL})</h2>
<table>
<tr><th>Attempted at</th><th>Job</th><th>Status</th><th>Occurrence</th><th>Return code</th></tr>
{_history_rows(history)}
</table>

<h2>Recent intraday alerts</h2>
<table>
<tr><th>Symbol / scan</th><th>Status</th><th>Delivered channels</th></tr>
{_alert_rows(alerts)}
</table>

</body>
</html>
"""


def write_dashboard() -> Path:
    target = _resolve(DASHBOARD_PATH)
    target.write_text(render(interactive=False))
    return target


def main() -> int:
    path = write_dashboard()
    print(f"Dashboard written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
