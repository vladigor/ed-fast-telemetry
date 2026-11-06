from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PureWindowsPath

if os.name == "nt":
    import winreg


# Only the game's own sequential F10 screenshots are ever deleted; hi-res
# (Shift+F10) and any other files in the folder are left alone.
SCREENSHOT_NAME = re.compile(r"Screenshot_\d+\.bmp", re.IGNORECASE)
WORKER_WAKE_SECONDS = 0.25
SWEEP_INTERVAL_SECONDS = 2.0
# Keep cleaning after Fast telemetry stops: under disk pressure the game can
# finish writing queued screenshots some seconds later.
GRACE_SECONDS = 15.0
# Files the game is still writing are locked; retry them for this long.
RETRY_SECONDS = 30.0
# A swept file must be untouched for this long so a screenshot still being
# written is not removed before its journal event appears.
SWEEP_SETTLE_SECONDS = 1.0
# File times can be coarse, so treat anything slightly older than the start
# of a session as belonging to it.
START_SLACK_SECONDS = 2.0


def default_screenshot_directory() -> Path:
    """Folder Elite Dangerous calls ED_Pictures."""
    pictures: str | None = None
    if os.name == "nt":
        try:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                pictures, _ = winreg.QueryValueEx(key, "My Pictures")
                pictures = os.path.expandvars(pictures)
        except OSError:
            pictures = None
    base = Path(pictures) if pictures else Path.home() / "Pictures"
    return base / "Frontier Developments" / "Elite Dangerous"


def screenshot_file_name(journal_filename: str) -> str | None:
    """Return the bare file name for a journal Filename, if it is an F10 screenshot."""
    name = PureWindowsPath(str(journal_filename)).name
    return name if SCREENSHOT_NAME.fullmatch(name) else None


class ScreenshotCleaner:
    """Delete F10 screenshots written while Fast telemetry is active.

    Files named in live Screenshot journal events are deleted promptly, and a
    periodic sweep removes any F10 screenshot created since the session
    started whose event was missed. Work runs on a background thread because
    deletes can block while Windows is flushing the disk.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        threaded: bool = True,
        grace_seconds: float = GRACE_SECONDS,
    ) -> None:
        self.directory = Path(directory) if directory else default_screenshot_directory()
        self._clock = clock
        self._threaded = threaded
        self._grace_seconds = grace_seconds
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: dict[str, float] = {}
        self._abandoned: set[str] = set()
        self._since = 0.0
        self._stop_at: float | None = None
        self._next_sweep = 0.0
        self._closing = False
        self.running = False
        self.deleted = 0
        self.failed = 0
        self.error = ""

    @property
    def waiting(self) -> int:
        with self._lock:
            return len(self._pending)

    def start(self) -> None:
        with self._lock:
            now = self._clock()
            if not self.running:
                self._since = now - START_SLACK_SECONDS
                self._pending.clear()
                self._abandoned.clear()
                self.deleted = 0
                self.failed = 0
                self.error = ""
            self.running = True
            self._stop_at = None
            self._next_sweep = now + SWEEP_INTERVAL_SECONDS
            self._closing = False
        self._ensure_thread()

    def stop(self) -> None:
        """Keep accepting and sweeping for a grace period, then finish."""
        with self._lock:
            if self.running and self._stop_at is None:
                self._stop_at = self._clock() + self._grace_seconds
        self._wake.set()

    def close(self, timeout: float = 1.0) -> None:
        """Do a final clean-up pass now and stop the worker."""
        with self._lock:
            if not self.running:
                return
            self._closing = True
            self._next_sweep = 0.0
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        else:
            self.run_once()

    def queue(self, journal_filenames: Iterable[str]) -> None:
        with self._lock:
            if not self._accepting():
                return
            now = self._clock()
            added = False
            for journal_filename in journal_filenames:
                name = screenshot_file_name(journal_filename)
                if name is not None and name not in self._abandoned:
                    self._pending.setdefault(name, now)
                    added = True
        if added:
            self._wake.set()

    def run_once(self) -> None:
        with self._lock:
            if not self.running:
                return
            now = self._clock()
            sweep = self._closing or now >= self._next_sweep
            if sweep:
                self._next_sweep = now + SWEEP_INTERVAL_SECONDS
            since = self._since
            until = self._stop_at
        if sweep:
            found = self._sweep(since, now, until)
            with self._lock:
                for name in found:
                    if name not in self._abandoned:
                        self._pending.setdefault(name, now)
        with self._lock:
            pending = list(self._pending.items())
        for name, first_seen in pending:
            outcome = self._delete(name)
            with self._lock:
                if outcome == "deleted":
                    self.deleted += 1
                    self._pending.pop(name, None)
                elif outcome == "missing":
                    self._pending.pop(name, None)
                elif now - first_seen >= RETRY_SECONDS or self._closing:
                    self.failed += 1
                    self._abandoned.add(name)
                    self._pending.pop(name, None)
        with self._lock:
            finished = self._closing or (
                self._stop_at is not None and now >= self._stop_at and not self._pending
            )
            if finished:
                self.running = False
                self._stop_at = None
                self._closing = False

    def _accepting(self) -> bool:
        return self.running and not self._closing and (
            self._stop_at is None or self._clock() < self._stop_at
        )

    def _sweep(self, since: float, now: float, until: float | None = None) -> list[str]:
        found: list[str] = []
        try:
            with os.scandir(self.directory) as entries:
                for entry in entries:
                    if not SCREENSHOT_NAME.fullmatch(entry.name):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        modified = entry.stat(follow_symlinks=False).st_mtime
                    except OSError as error:
                        self.error = f"Could not inspect screenshot: {error}"
                        continue
                    if modified >= since and (until is None or modified <= until) and now - modified >= SWEEP_SETTLE_SECONDS:
                        found.append(entry.name)
        except OSError as error:
            self.error = f"Could not scan screenshots folder: {error}"
            return []
        return found

    def _delete(self, name: str) -> str:
        try:
            os.remove(self.directory / name)
        except FileNotFoundError:
            return "missing"
        except OSError as error:
            self.error = f"Could not delete {name}: {error}"
            # Usually still open in the game or another program; retry later.
            return "locked"
        self.error = ""
        return "deleted"

    def _ensure_thread(self) -> None:
        if not self._threaded:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="ED screenshot cleanup", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            self._wake.wait(WORKER_WAKE_SECONDS)
            self._wake.clear()
            self.run_once()
            with self._lock:
                if not self.running:
                    self._thread = None
                    return
