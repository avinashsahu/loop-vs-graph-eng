import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Callable


_ACTIONABLE_STATUSES = {"proposed", "flagged_for_review"}


class AlertLedgerStateError(RuntimeError):
    pass


class AlertLedger:
    """Persist status transitions and deliver each channel once per transition."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def observe_and_deliver(
        self,
        *,
        run_id: str,
        symbol: str,
        status: str,
        channels: dict[str, Callable[[], None]],
    ) -> dict[str, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with lock_path.open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                return self._observe_and_deliver(
                    run_id=run_id,
                    symbol=symbol,
                    status=status,
                    channels=channels,
                )
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _observe_and_deliver(
        self,
        *,
        run_id: str,
        symbol: str,
        status: str,
        channels: dict[str, Callable[[], None]],
    ) -> dict[str, str]:
        state = self._read()
        key = f"{run_id}:{symbol}"
        entry = state["alerts"].get(key)

        if entry is None or entry["status"] != status:
            entry = {"status": status, "delivered_channels": []}
            state["alerts"][key] = entry
            self._write(state)

        outcomes = {}
        if status not in _ACTIONABLE_STATUSES:
            return outcomes

        delivered = set(entry["delivered_channels"])
        for channel, send in channels.items():
            if channel in delivered:
                outcomes[channel] = "skipped_unchanged"
                continue
            try:
                send()
            except Exception as exc:
                outcomes[channel] = f"failed: {exc}"
                continue
            delivered.add(channel)
            entry["delivered_channels"] = sorted(delivered)
            self._write(state)
            outcomes[channel] = "delivered"
        return outcomes

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "alerts": {}}
        try:
            with self.path.open() as state_file:
                state = json.load(state_file)
        except (OSError, ValueError) as exc:
            raise AlertLedgerStateError(
                f"cannot read alert ledger {self.path}; refusing to replay alerts"
            ) from exc

        if not isinstance(state, dict) or state.get("version") != 1:
            raise AlertLedgerStateError(f"unsupported alert ledger format in {self.path}")
        alerts = state.get("alerts")
        if not isinstance(alerts, dict):
            raise AlertLedgerStateError(f"invalid alerts collection in {self.path}")
        for key, entry in alerts.items():
            if (
                not isinstance(key, str)
                or not isinstance(entry, dict)
                or not isinstance(entry.get("status"), str)
                or not isinstance(entry.get("delivered_channels"), list)
                or not all(isinstance(channel, str) for channel in entry["delivered_channels"])
            ):
                raise AlertLedgerStateError(
                    f"invalid alert entry in {self.path}; refusing to replay alerts"
                )
        return state

    def _write(self, state: dict) -> None:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w") as state_file:
                json.dump(state, state_file, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temp_path, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
