from __future__ import annotations

import json
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HAS_LAT_LONG_FLAG = 1 << 21
SAVED_GAMES_GUID = "{4C5C32FF-BB9D-43B0-BF4C-2A725E4234A0}"


def default_journal_directory() -> Path:
    saved_games: str | None = None
    if os.name == "nt":
        import winreg

        try:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, SAVED_GAMES_GUID)
                saved_games = os.path.expandvars(value)
        except OSError:
            saved_games = None
    base = Path(saved_games) if saved_games else Path.home() / "Saved Games"
    return base / "Frontier Developments" / "Elite Dangerous"


def journal_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def planetary_screenshot(event: dict[str, Any]) -> bool:
    latitude = number(event.get("Latitude"))
    longitude = number(event.get("Longitude"))
    return (
        event.get("event") == "Screenshot"
        and journal_time(event.get("timestamp")) is not None
        and isinstance(event.get("Body"), str)
        and bool(event["Body"].strip())
        and latitude is not None and -90 <= latitude <= 90
        and longitude is not None and -180 <= longitude <= 180
    )


class JournalTail:
    """Read complete new lines, retaining an unfinished line for the next poll."""

    RESCAN_SECONDS = 1.0
    HISTORY_BYTES = 8 * 1024 * 1024

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path: Path | None = None
        self.offset = 0
        self.system: str | None = None
        self.error = ""
        self._scan_at = 0.0

    def _latest(self, force: bool = False) -> Path | None:
        now = time.monotonic()
        if not force and now < self._scan_at and self.path is not None and self.path.exists():
            return self.path
        self._scan_at = now + self.RESCAN_SECONDS
        newest: tuple[float, str] | None = None
        with os.scandir(self.directory) as entries:
            for entry in entries:
                if entry.name.startswith("Journal.") and entry.name.endswith(".log") and entry.is_file():
                    candidate = (entry.stat().st_mtime, entry.name)
                    if newest is None or candidate > newest:
                        newest = candidate
        return self.directory / newest[1] if newest else None

    def start(self) -> None:
        """Prime context from recent history without publishing old screenshots."""
        self.path = None
        self.offset = 0
        self.system = None
        self.error = ""
        try:
            self.path = self._latest(force=True)
            if self.path is None:
                self.error = f"No Journal.*.log found in {self.directory}"
                return
            size = self.path.stat().st_size
            with self.path.open("rb") as handle:
                handle.seek(max(0, size - self.HISTORY_BYTES))
                if handle.tell():
                    handle.readline()
                for line in handle:
                    self._event(line)
            self.offset = size
        except OSError as error:
            self.error = f"Could not read journal: {error}"

    def _event(self, line: bytes) -> dict[str, Any] | None:
        try:
            event = json.loads(line.decode("utf-8-sig"))
        except (UnicodeError, ValueError) as error:
            self.error = f"Invalid journal line: {error}"
            return None
        if not isinstance(event, dict):
            self.error = "Invalid journal line: expected a JSON object"
            return None
        system = event.get("StarSystem")
        if isinstance(system, str):
            self.system = system
        return event

    def read(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        self.error = ""
        try:
            latest = self._latest()
            if latest is None:
                self.error = f"No Journal.*.log found in {self.directory}"
                return events
            if latest != self.path or latest.stat().st_size < self.offset:
                self.path = latest
                self.offset = 0
                self.system = None
            with latest.open("rb") as handle:
                handle.seek(self.offset)
                while True:
                    position = handle.tell()
                    line = handle.readline()
                    if not line or not line.endswith(b"\n"):
                        self.offset = position
                        break
                    event = self._event(line)
                    if event is not None and event.get("event") in {"LeaveBody", "FSDJump", "CarrierJump", "Location"}:
                        for screenshot in events:
                            screenshot["_eligible"] = False
                    if event is not None and event.get("event") == "Screenshot":
                        # Keep the system context at this event, not at a later
                        # jump that may be present in the same journal read.
                        event["_journal_system"] = self.system
                        event["_eligible"] = True
                        events.append(event)
        except OSError as error:
            self.error = f"Could not read journal: {error}"
        return events


class StatusPublisher:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.updates = 0
        self.skipped = 0
        self.failed = 0
        self.error = ""

    def reset(self) -> None:
        self.updates = self.skipped = self.failed = 0
        self.error = ""

    def publish(self, event: dict[str, Any]) -> bool:
        """Atomically merge one fresh planetary fix into the game's status."""
        self.error = ""
        temporary: Path | None = None
        try:
            if not planetary_screenshot(event) or event.get("_eligible") is False:
                self.skipped += 1
                return False
            original = self.path.read_bytes()
            data = json.loads(original.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("expected a Status.json object")
            timestamp = journal_time(data.get("timestamp"))
            moment = journal_time(event.get("timestamp"))
            if timestamp is None or moment is None:
                raise ValueError("missing or invalid status timestamp")
            same_body = str(data.get("BodyName", "")).strip().casefold() == event["Body"].strip().casefold()
            system = event.get("_journal_system")
            same_system = not system or not event.get("System") or str(system).casefold() == str(event["System"]).casefold()
            flags = data.get("Flags")
            has_coordinates = isinstance(flags, int) and bool(flags & HAS_LAT_LONG_FLAG)
            if not same_body or not same_system or not has_coordinates or moment < timestamp:
                self.skipped += 1
                return False
            data["timestamp"] = event["timestamp"]
            data["Latitude"] = event["Latitude"]
            data["Longitude"] = event["Longitude"]
            for key in ("Heading", "Altitude"):
                if key in event and event[key] is not None:
                    if number(event[key]) is None:
                        raise ValueError(f"invalid Screenshot {key}")
                    data[key] = event[key]
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".ed-fast-status-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
            # No shared lock protocol exists with Elite. This check reduces,
            # but cannot eliminate, simultaneous-writer conflicts.
            if self.path.read_bytes() != original:
                self.skipped += 1
                self.error = "Status.json changed; skipped telemetry write"
                return False
            os.replace(temporary, self.path)
            self.updates += 1
            return True
        except (OSError, ValueError, UnicodeError) as error:
            self.failed += 1
            self.error = f"Could not update Status.json: {error}"
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    self.error = f"Could not remove status temporary file: {error}"
