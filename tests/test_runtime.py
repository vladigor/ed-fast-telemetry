import json
import os
import sys
import tomllib
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, QTimer
from PySide6.QtWidgets import QApplication, QFrame, QScrollArea

from ed_fast_telemetry import DISPLAY_VERSION, __version__
from ed_fast_telemetry.app import DISCLAIMER, MainWindow, main
from ed_fast_telemetry.fast_telemetry import FastTelemetryController, RateMeter, key_hold_ms
from ed_fast_telemetry.screenshot_cleanup import ScreenshotCleaner
from ed_fast_telemetry.telemetry import HAS_LAT_LONG_FLAG


def test_versions_and_build_are_independent():
    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "pyproject.toml").read_text())
    assert manifest["project"]["version"] == __version__ == "0.1.0.dev0"
    assert DISPLAY_VERSION == "0.1.0-dev"
    assert "icuuc.dll" in (root / "ed_fast_telemetry.spec").read_text()
    assert "Split-Path" not in (root / "build.ps1").read_text()


def test_controller_focus_modifiers_rate_and_release(qtbot):
    now = [100.0]
    focused = [False]
    modifiers = ["Shift"]
    events = []
    controller = FastTelemetryController(
        is_foreground=lambda: focused[0], modifiers=lambda: modifiers,
        key_down=lambda: events.append("down") or True,
        key_up=lambda: events.append("up") or True, clock=lambda: now[0],
    )
    controller.set_active(True)
    controller._poll_timer.stop()
    assert not events
    focused[0] = True
    controller.tick()
    assert not events
    modifiers.clear()
    controller.tick()
    assert events == ["down"]
    controller._release()
    now[0] += 0.1
    controller.tick()
    controller.set_active(False)
    assert events == ["down", "up", "down", "up"]
    assert controller.press_count == 2
    controller.set_rate_hz(999)
    assert controller.rate_hz == 50
    assert key_hold_ms(50) == 10


def test_rejected_input_is_reported():
    controller = FastTelemetryController(
        is_foreground=lambda: True, modifiers=list, key_down=lambda: False,
    )
    controller.set_active(True)
    assert "rejected" in controller.status
    assert "presses per second sent" in controller.status
    assert "screenshots per second read" in controller.status
    assert "Hz" not in controller.status
    assert controller.press_count == 0
    controller.close()


def test_rate_meter_sliding_window():
    meter = RateMeter()
    meter.reset(0)
    for step in range(10):
        meter.add(step * 0.1)
    assert meter.rate(1) == 10
    assert meter.rate(5) == 0


def test_cleanup_preserves_old_hires_and_unrelated_files(tmp_path):
    now = [10000.0]
    cleaner = ScreenshotCleaner(tmp_path, clock=lambda: now[0], threaded=False)
    old = tmp_path / "Screenshot_0001.bmp"
    old.write_bytes(b"BM")
    os.utime(old, (9000, 9000))
    cleaner.start()
    for name in ("Screenshot_0002.bmp", "HighResScreenShot_0001.bmp", "Shortcut.lnk"):
        path = tmp_path / name
        path.write_bytes(b"BM")
        os.utime(path, (now[0], now[0]))
    now[0] += 2
    cleaner.run_once()
    assert not (tmp_path / "Screenshot_0002.bmp").exists()
    assert old.exists()
    assert (tmp_path / "HighResScreenShot_0001.bmp").exists()
    assert (tmp_path / "Shortcut.lnk").exists()
    cleaner.close()


def test_cleanup_retries_locked_file_and_keeps_grace(tmp_path, monkeypatch):
    now = [10000.0]
    cleaner = ScreenshotCleaner(tmp_path, clock=lambda: now[0], threaded=False)
    target = tmp_path / "Screenshot_0001.bmp"
    target.write_bytes(b"BM")
    cleaner.start()
    cleaner.queue(["\\ED_Pictures\\Screenshot_0001.bmp"])
    remove = os.remove

    def locked(path):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "remove", locked)
    cleaner.run_once()
    assert cleaner.waiting == 1
    assert "locked" in cleaner.error
    cleaner.stop()
    assert cleaner.running
    monkeypatch.setattr(os, "remove", remove)
    cleaner.run_once()
    assert not target.exists()
    assert cleaner.deleted == 1
    now[0] += 16
    cleaner.run_once()
    assert not cleaner.running


def make_window(tmp_path, qtbot):
    journal_dir = tmp_path / "journals"
    shots = tmp_path / "screenshots"
    journal_dir.mkdir(exist_ok=True)
    shots.mkdir(exist_ok=True)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    controller = FastTelemetryController(is_foreground=lambda: False)
    window = MainWindow(settings=settings, journal_directory=journal_dir,
                        screenshot_directory=shots, controller=controller)
    qtbot.addWidget(window)
    return window


def test_ui_disclaimer_default_and_persistence(tmp_path, qtbot):
    window = make_window(tmp_path, qtbot)
    assert "deleted" in DISCLAIMER
    assert window.rate.value() == 10
    assert window.rate.maximum() == 50
    assert window.rate.suffix() == ""
    assert window.rate.accessibleName() == "Updates per second"
    assert "Hz" not in DISCLAIMER
    window.rate.setValue(25)
    assert window.controller.rate_hz == 25
    reopened = make_window(tmp_path, qtbot)
    assert reopened.rate.value() == 25
    assert reopened.start_button.text() == "Start"
    assert not reopened.controller.active


def test_ui_panels_and_explicit_state(tmp_path, qtbot):
    window = make_window(tmp_path, qtbot)
    window.show()
    assert len(window.findChildren(QFrame, "panel")) == 3
    assert window.findChild(QFrame, "warning") is not None
    assert window.findChild(QScrollArea).widgetResizable()
    assert window.state_label.text() == "Stopped"
    window.start_button.click()
    assert window.state_label.text() == "Enabled"
    assert window.start_button.isChecked()
    window.start_button.click()
    assert window.state_label.text() == "Cleaning up"
    assert not window.start_button.isChecked()
    window.cleaner.close()
    window.refresh_status()
    assert window.state_label.text() == "Stopped"


def test_end_to_end_start_watch_publish_delete_stop(tmp_path, qtbot):
    window = make_window(tmp_path, qtbot)
    journal = window.journal.directory / "Journal.2026-11-06T000000.01.log"
    journal.write_text('{"event":"Location","StarSystem":"Sol","Body":"Earth"}\n')
    status = window.publisher.path
    status.write_text(json.dumps({
        "timestamp": "2026-11-06T06:22:08Z", "event": "Status",
        "BodyName": "Earth", "Flags": HAS_LAT_LONG_FLAG, "Heading": 90, "Fuel": 12,
    }))
    image = window.cleaner.directory / "Screenshot_0001.bmp"
    window.start_button.click()
    assert window.controller.active
    assert window.start_button.text() == "Stop"
    image.write_bytes(b"BM")
    event = {
        "event": "Screenshot", "timestamp": "2026-11-06T06:22:09Z",
        "Filename": "\\ED_Pictures\\Screenshot_0001.bmp", "Body": "Earth",
        "System": "Sol", "Latitude": 1.234567, "Longitude": 2.345678,
    }
    with journal.open("a") as handle:
        handle.write(json.dumps(event) + "\n")
    qtbot.waitUntil(lambda: window.publisher.updates == 1, timeout=3000)
    qtbot.waitUntil(lambda: not image.exists(), timeout=3000)
    data = json.loads(status.read_text())
    assert data["Latitude"] == 1.234567
    assert data["Fuel"] == 12
    assert data["Heading"] == 90
    window.refresh_status()
    assert "1 status updates" in window.status.text()
    assert "1 screenshots read" in window.status.text()
    original = status.read_bytes()
    window.poll()
    assert window.publisher.updates == 1  # No watcher feedback write.
    window.start_button.click()
    assert not window.controller.active
    with journal.open("a") as handle:
        event["Latitude"] = 9
        handle.write(json.dumps(event) + "\n")
    window.poll()
    assert status.read_bytes() == original
    assert window.cleaner.running
    window.close()
    assert not window.controller._poll_timer.isActive()
    assert not window.cleaner.running


def test_watch_throttle_does_not_restart_on_each_signal(tmp_path, qtbot):
    window = make_window(tmp_path, qtbot)
    window.read_timer.setInterval(1000)
    window._changed("")
    timer_id = window.read_timer.timerId()
    window._changed("")
    assert window.read_timer.timerId() == timer_id


def test_sweep_does_not_delete_screenshots_after_grace(tmp_path):
    now = [10000.0]
    cleaner = ScreenshotCleaner(tmp_path, clock=lambda: now[0], threaded=False)
    cleaner.start()
    cleaner.stop()
    target = tmp_path / "Screenshot_0001.bmp"
    target.write_bytes(b"BM")
    os.utime(target, (10016, 10016))
    now[0] = 10018
    cleaner.run_once()
    assert target.exists()
    assert not cleaner.running


def test_failed_deletes_are_not_queued_by_every_sweep(tmp_path, monkeypatch):
    now = [10000.0]
    cleaner = ScreenshotCleaner(tmp_path, clock=lambda: now[0], threaded=False)
    target = tmp_path / "Screenshot_0001.bmp"
    target.write_bytes(b"BM")
    os.utime(target, (now[0], now[0]))
    cleaner.start()
    cleaner.queue([target.name])

    def locked(path):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "remove", locked)
    now[0] += 31
    cleaner.run_once()
    assert cleaner.failed == 1
    cleaner.queue([target.name])
    now[0] += 2
    cleaner.run_once()
    assert cleaner.failed == 1
    assert cleaner.waiting == 0


def test_real_entry_point_starts_and_releases_instance_lock(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ed-fast-telemetry"])
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda _: str(tmp_path))
    app = QApplication.instance()
    assert app is not None
    QTimer.singleShot(20, app.closeAllWindows)
    assert main() == 0
    assert not (tmp_path / "instance.lock").exists()
