from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable
from functools import cache
from pathlib import PureWindowsPath
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from .screenshot_cleanup import ScreenshotCleaner


ELITE_PROCESS_NAMES = frozenset({"elitedangerous64.exe"})
DEFAULT_RATE_HZ = 10
MAX_RATE_HZ = 50
FOCUS_POLL_INTERVAL_MS = 100
STATUS_REFRESH_MS = 500
RATE_WINDOW_SECONDS = 2.0
# Hold the key long enough that a game sampling keyboard state once per frame
# still sees the press, but never longer than half the press interval.
MAX_KEY_HOLD_MS = 50
F10_SCAN_CODE = 0x44
# Shift+F10 takes a slow high-resolution screenshot, and Alt+F10 is a common
# overlay hotkey (for example NVIDIA Instant Replay), so never press F10 while
# the player holds a modifier.
MODIFIER_KEYS = (
    (0x10, "Shift"),
    (0x11, "Ctrl"),
    (0x12, "Alt"),
    (0x5B, "Win"),
    (0x5C, "Win"),
)


def press_interval_ms(rate_hz: int) -> float:
    return 1000.0 / max(1, rate_hz)


def key_hold_ms(rate_hz: int) -> int:
    return max(1, min(MAX_KEY_HOLD_MS, int(press_interval_ms(rate_hz) // 2)))


@cache
def _win32() -> Any:
    """Load and type the Win32 functions once rather than on every tick."""
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class Api:
        pass

    api = Api()
    api.ctypes = ctypes
    api.wintypes = wintypes
    api.user32 = user32
    api.kernel32 = kernel32
    api.KEYBDINPUT = KEYBDINPUT
    api.INPUT = INPUT
    return api


_foreground_cache: dict[str, Any] = {"hwnd": None, "name": None}


def _foreground_process_name() -> str | None:
    if os.name != "nt":
        return None
    api = _win32()
    hwnd = api.user32.GetForegroundWindow()
    if not hwnd:
        return None
    # Window handles are unique while the window exists, so the process name
    # only needs looking up when the foreground window changes.
    if hwnd == _foreground_cache["hwnd"]:
        return _foreground_cache["name"]
    pid = api.wintypes.DWORD()
    api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
    name: str | None = None
    if pid.value:
        process_query_limited_information = 0x1000
        handle = api.kernel32.OpenProcess(process_query_limited_information, False, pid.value)
        if handle:
            try:
                size = api.wintypes.DWORD(1024)
                buffer = api.ctypes.create_unicode_buffer(size.value)
                if api.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, api.ctypes.byref(size)
                ):
                    name = PureWindowsPath(buffer.value).name
            finally:
                api.kernel32.CloseHandle(handle)
    _foreground_cache["hwnd"] = hwnd
    _foreground_cache["name"] = name
    return name


def elite_is_foreground() -> bool:
    try:
        name = _foreground_process_name()
    except OSError:
        return False
    return name is not None and name.lower() in ELITE_PROCESS_NAMES


def held_modifiers() -> list[str]:
    if os.name != "nt":
        return []
    user32 = _win32().user32
    held: list[str] = []
    for virtual_key, label in MODIFIER_KEYS:
        if user32.GetAsyncKeyState(virtual_key) & 0x8000 and label not in held:
            held.append(label)
    return held


def _send_f10(key_up: bool) -> bool:
    if os.name != "nt":
        return False
    api = _win32()
    input_keyboard = 1
    keyeventf_keyup = 0x0002
    keyeventf_scancode = 0x0008
    flags = keyeventf_scancode | (keyeventf_keyup if key_up else 0)
    event = api.INPUT(type=input_keyboard)
    # Scan codes reach games that read raw/DirectInput keyboard data.
    event.union.ki = api.KEYBDINPUT(0, F10_SCAN_CODE, flags, 0, 0)
    return api.user32.SendInput(1, api.ctypes.byref(event), api.ctypes.sizeof(api.INPUT)) == 1


def send_f10_down() -> bool:
    return _send_f10(key_up=False)


def send_f10_up() -> bool:
    return _send_f10(key_up=True)


class RateMeter:
    """Events per second over a sliding window."""

    def __init__(self, window_seconds: float = RATE_WINDOW_SECONDS) -> None:
        self.window = window_seconds
        self._events: deque[float] = deque()
        self._started: float | None = None

    def reset(self, now: float) -> None:
        self._events.clear()
        self._started = now

    def add(self, now: float, count: int = 1) -> None:
        self._events.extend([now] * count)
        self._trim(now)

    def rate(self, now: float) -> float:
        self._trim(now)
        if self._started is None:
            return 0.0
        span = min(self.window, now - self._started)
        # Too short a span after starting would give a wildly noisy figure.
        if span < 0.25:
            return 0.0
        return len(self._events) / span

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()


class FastTelemetryController(QObject):
    """Press F10 in Elite Dangerous at a capped rate while active.

    Each press makes the game write a Screenshot journal event with the
    current planetary coordinates. Presses are sent only while the Elite
    window is in the foreground and no modifier key is held, both rechecked
    before every press.
    """

    status_changed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        is_foreground: Callable[[], bool] = elite_is_foreground,
        modifiers: Callable[[], list[str]] = held_modifiers,
        key_down: Callable[[], bool] = send_f10_down,
        key_up: Callable[[], bool] = send_f10_up,
        clock: Callable[[], float] = time.monotonic,
        rate_hz: int = DEFAULT_RATE_HZ,
        cleaner: ScreenshotCleaner | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_foreground = is_foreground
        self._modifiers = modifiers
        self._key_down = key_down
        self._key_up = key_up
        self._clock = clock
        self.cleaner = cleaner
        self.active = False
        self.press_count = 0
        self.screenshot_count = 0
        self.modifier_skips = 0
        self.status = ""
        self.status_publish_error = ""
        self._last_press: float | None = None
        self._key_held = False
        self._waiting = ""
        self._held_labels: list[str] = []
        self._send_ms: float | None = None
        self._sent_meter = RateMeter()
        self._read_meter = RateMeter()
        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._poll_timer.timeout.connect(self.tick)
        self._release_timer = QTimer(self)
        self._release_timer.setSingleShot(True)
        self._release_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._release_timer.timeout.connect(self._release)
        # Status text is refreshed on a slow timer rather than per press so
        # high rates do not spend main-thread time relaying out the label.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(STATUS_REFRESH_MS)
        self._status_timer.timeout.connect(self.refresh_status)
        self.set_rate_hz(rate_hz)

    @property
    def interval_ms(self) -> float:
        return press_interval_ms(self.rate_hz)

    def set_rate_hz(self, rate_hz: int) -> None:
        self.rate_hz = max(1, min(MAX_RATE_HZ, int(rate_hz)))
        # Presses happen on poll ticks, so poll at least as often as the rate.
        self._poll_timer.setInterval(
            max(1, min(FOCUS_POLL_INTERVAL_MS, round(self.interval_ms)))
        )
        self._release_timer.setInterval(key_hold_ms(self.rate_hz))

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        if active:
            now = self._clock()
            self.press_count = 0
            self.screenshot_count = 0
            self.modifier_skips = 0
            self._send_ms = None
            self._waiting = ""
            self._last_press = None
            self._sent_meter.reset(now)
            self._read_meter.reset(now)
            if self.cleaner is not None:
                self.cleaner.start()
            self._poll_timer.start()
            self._status_timer.start()
            self.tick()
            self.refresh_status()
        else:
            self._poll_timer.stop()
            self._status_timer.stop()
            self._release()
            if self.cleaner is not None:
                self.cleaner.stop()
            self._set_status("")

    def close(self) -> None:
        self.set_active(False)
        if self.cleaner is not None:
            self.cleaner.close()

    def delete_screenshots(self, journal_filenames: list[str]) -> None:
        """Queue screenshots named by live journal events for deletion.

        The cleaner ignores them unless Fast telemetry is (or very recently
        was) active, so manual screenshots at other times are kept.
        """
        if self.cleaner is not None and journal_filenames:
            self.cleaner.queue(journal_filenames)

    def record_screenshots(self, count: int) -> None:
        """Note Screenshot journal events read while active."""
        if self.active and count > 0:
            self.screenshot_count += count
            self._read_meter.add(self._clock(), count)

    def tick(self) -> None:
        if not self.active or self._key_held:
            return
        if not self._is_foreground():
            self._waiting = "waiting for Elite Dangerous focus"
            return
        now = self._clock()
        # Half a poll tick of tolerance stops timer jitter from skipping a
        # whole tick when the poll interval equals the press interval.
        tolerance_ms = self._poll_timer.interval() / 2
        if (
            self._last_press is not None
            and (now - self._last_press) * 1000 + tolerance_ms < self.interval_ms
        ):
            return
        held = self._modifiers()
        if held:
            if self._waiting != "modifier":
                self.modifier_skips += 1
            self._waiting = "modifier"
            self._held_labels = held
            return
        self._last_press = now
        started = time.perf_counter()
        accepted = self._key_down()
        self._note_send_time(started)
        if not accepted:
            self._waiting = "rejected"
            return
        self._waiting = ""
        self._key_held = True
        self.press_count += 1
        self._sent_meter.add(now)
        self._release_timer.start()

    def _release(self) -> None:
        self._release_timer.stop()
        if self._key_held:
            self._key_held = False
            # Always release, even if focus moved, so F10 is never left held down.
            started = time.perf_counter()
            if not self._key_up():
                self._waiting = "Windows rejected the F10 key release"
            self._note_send_time(started)

    def _note_send_time(self, started: float) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._send_ms = (
            elapsed_ms if self._send_ms is None else 0.8 * self._send_ms + 0.2 * elapsed_ms
        )

    def refresh_status(self) -> None:
        if not self.active:
            return
        now = self._clock()
        parts = [
            f"Fast telemetry: {self.press_count} F10 sent",
            f"{self._sent_meter.rate(now):.1f} presses per second sent",
            f"{self._read_meter.rate(now):.1f} screenshots per second read",
        ]
        if self._send_ms is not None:
            parts.append(f"key send {self._send_ms:.1f} ms")
        if self._waiting == "modifier":
            labels = "/".join(self._held_labels) or "a modifier"
            parts.append(f"holding F10 while {labels} is down")
        elif self._waiting == "rejected":
            parts.append("Windows rejected the F10 key press")
        elif self._waiting:
            parts.append(self._waiting)
        if self.modifier_skips:
            parts.append(f"{self.modifier_skips} delayed by modifier keys")
        if self.cleaner is not None:
            parts.append(f"{self.cleaner.deleted} screenshots deleted")
            if self.cleaner.failed:
                parts.append(f"{self.cleaner.failed} could not be deleted")
        if self.status_publish_error:
            parts.append(self.status_publish_error)
        self._set_status(" · ".join(parts))

    def _set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.status_changed.emit(text)
