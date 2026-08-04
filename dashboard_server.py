"""Localhost-only control panel for the app scheduler: view live job status and
toggle/run-now jobs without editing .env or restarting the scheduler daemon.

Bound to 127.0.0.1 only -- this has no authentication, so it must never be
exposed beyond the local machine. Actions are applied by writing to
.app_scheduler_overrides.json; the scheduler daemon picks them up on its next
poll tick (see app_scheduler.run_due_jobs), so it never runs a job concurrently
with the scheduler's own sequential NSE-pacing loop.
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from dotenv import load_dotenv

load_dotenv()

import app_scheduler  # noqa: E402
import scheduler_dashboard  # noqa: E402
from logging_config import setup_logging  # noqa: E402
from market_time import now_ist  # noqa: E402

log = setup_logging("dashboard_server")

HOST = "127.0.0.1"
PORT = int(os.environ.get("APP_DASHBOARD_PORT", "8787"))


def _known_job_names() -> set[str]:
    return {job.name for job in app_scheduler.configured_jobs()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _redirect_home(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(body)
        return {key: values[0] for key, values in parsed.items() if values}

    def do_GET(self):
        if self.path not in ("/", ""):
            self.send_response(404)
            self.end_headers()
            return
        body = scheduler_dashboard.render(interactive=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/toggle":
            self._handle_toggle()
        elif self.path == "/run-now":
            self._handle_run_now()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self._redirect_home()

    def _handle_toggle(self) -> None:
        form = self._read_form()
        job = form.get("job", "")
        if job not in _known_job_names():
            log.warning("toggle request for unknown job=%r ignored", job)
            return
        enabled = form.get("enabled") == "1"
        overrides = app_scheduler.load_overrides()
        overrides["enabled_overrides"][job] = enabled
        app_scheduler.save_overrides(overrides)
        log.info("job[%s] enabled override set to %s via dashboard", job, enabled)

    def _handle_run_now(self) -> None:
        form = self._read_form()
        job = form.get("job", "")
        if job not in _known_job_names():
            log.warning("run-now request for unknown job=%r ignored", job)
            return
        overrides = app_scheduler.load_overrides()
        overrides["force_run"][job] = now_ist().isoformat()
        app_scheduler.save_overrides(overrides)
        log.info("job[%s] run-now requested via dashboard", job)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("dashboard server listening on http://%s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
