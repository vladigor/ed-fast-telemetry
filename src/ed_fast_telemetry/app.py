from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QLockFile, QSettings, QStandardPaths, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from . import APP_DISPLAY_NAME, APP_NAME, __version__
from .fast_telemetry import DEFAULT_RATE_HZ, MAX_RATE_HZ, FastTelemetryController
from .screenshot_cleanup import ScreenshotCleaner, default_screenshot_directory
from .telemetry import JournalTail, StatusPublisher, default_journal_directory, planetary_screenshot

DISCLAIMER = (
    "Pressing Start sends F10 to Elite Dangerous only while the game window has focus. "
    "Screenshot journal positions and timestamps are copied into Status.json.\n\n"
    "Ordinary F10 screenshots taken while running are deleted, including manual ones. "
    "Older screenshots and hi-res (Shift+F10) screenshots are kept. Cleanup continues "
    "for 15 seconds after Stop to catch late writes; locked files are retried for 30 seconds.\n\n"
    "Turn off software that watches, converts or uploads Elite screenshots before starting. "
    "Other programs using F10 as a hotkey will also see these presses. F10 is held back "
    "while Shift, Ctrl, Alt or Windows is down; pressing a modifier during a key hold "
    "can still trigger a modified screenshot.\n\n"
    "Elite also writes Status.json. Updates are atomic and detected conflicts are skipped, "
    "but a small simultaneous-writer risk remains.\n\n"
    "There is little benefit in setting the rate higher than 10 updates per second for EDCoPilot time trials, "
    "as EDCoPilot limits how frequently it reads the status file."
)


class MainWindow(QWidget):
    def __init__(
        self, *, settings: QSettings | None = None, journal_directory: Path | None = None,
        screenshot_directory: Path | None = None, controller: FastTelemetryController | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(760, 730)
        self.settings = settings if settings is not None else QSettings(APP_NAME, APP_NAME)
        self.journal = JournalTail(journal_directory or default_journal_directory())
        self.publisher = StatusPublisher(self.journal.directory / "Status.json")
        self.cleaner = ScreenshotCleaner(screenshot_directory or default_screenshot_directory())
        self.controller = controller or FastTelemetryController(self, cleaner=self.cleaner)
        if controller is not None:
            self.controller.cleaner = self.cleaner

        self.setObjectName("telemetryWindow")
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        background, surface, foreground, muted, border = (
            ("#181c23", "#242a34", "#f1f5f9", "#b5c0d0", "#495366") if dark else
            ("#f3f5f8", "#ffffff", "#182433", "#526173", "#d2dae4")
        )
        self.setStyleSheet(f"""
            QWidget#telemetryWindow, QWidget#content {{
                background: {background}; color: {foreground};
            }}
            QLabel {{ color: {foreground}; background: transparent; }}
            QLabel#subtitle, QLabel#hint {{ color: {muted}; }}
            QLabel#title {{ font-size: 22px; font-weight: 600; }}
            QLabel#panelTitle {{ font-weight: 600; font-size: 14px; }}
            QFrame#panel {{
                background: {surface}; border: 1px solid {border}; border-radius: 8px;
            }}
            QFrame#warning {{ background: {surface}; border: 1px solid {border};
                border-left: 4px solid #c58a17; border-radius: 8px; }}
            QScrollArea {{ border: none; background: {background}; }}
            QPushButton#startStop {{
                color: white; background: #176449; border: 2px solid transparent;
                border-radius: 6px; padding: 9px 24px; font-weight: 600;
            }}
            QPushButton#startStop:hover {{ background: #12513b; }}
            QPushButton#startStop:checked {{ background: #a93232; }}
            QPushButton#startStop:checked:hover {{ background: #882727; }}
            QPushButton#startStop:focus {{ border-color: #f3c969; }}
            QSpinBox {{ background: {surface}; color: {foreground};
                border: 1px solid {border}; border-radius: 5px; padding: 7px; }}
            QSpinBox:focus {{ border: 2px solid #397cc1; }}
            QLabel#state {{ color: {muted}; font-weight: 600; }}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("Faster planetary positions for apps that read Status.json.")
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        guidance = QFrame()
        guidance.setObjectName("warning")
        guidance_layout = QVBoxLayout(guidance)
        guidance_layout.setContentsMargins(16, 14, 16, 14)
        guidance_title = QLabel("Before you start")
        guidance_title.setObjectName("panelTitle")
        guidance_layout.addWidget(guidance_title)
        explanation = QLabel(DISCLAIMER)
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        explanation.setWordWrap(True)
        guidance_layout.addWidget(explanation)
        layout.addWidget(guidance)

        control_panel = QFrame()
        control_panel.setObjectName("panel")
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(16, 14, 16, 14)
        controls = QHBoxLayout()
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("startStop")
        self.start_button.setCheckable(True)
        self.start_button.setMinimumWidth(120)
        self.start_button.setToolTip("Start sending F10 when Elite Dangerous has focus. Stop ends input and status updates.")
        controls.addWidget(self.start_button)
        frequency_label = QLabel("Updates per second")
        controls.addWidget(frequency_label)
        self.rate = QSpinBox()
        frequency_label.setBuddy(self.rate)
        self.rate.setRange(1, MAX_RATE_HZ)
        self.rate.setAccessibleName("Updates per second")
        self.rate.setToolTip("Request 1-50 updates per second (default 10). Each request takes a screenshot. Actual updates may be slower. Changes apply immediately and are remembered.")
        try:
            saved_rate = int(self.settings.value("rate_hz", DEFAULT_RATE_HZ))
        except (ValueError, TypeError):
            saved_rate = DEFAULT_RATE_HZ
        self.rate.setValue(max(1, min(MAX_RATE_HZ, saved_rate)))
        self.controller.set_rate_hz(self.rate.value())
        controls.addWidget(self.rate)
        controls.addStretch()
        self.state_label = QLabel("Stopped")
        self.state_label.setObjectName("state")
        controls.addWidget(self.state_label)
        control_layout.addLayout(controls)
        hint = QLabel("After Start, return to the game. F10 pauses whenever another window has focus.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        control_layout.addWidget(hint)
        layout.addWidget(control_panel)

        diagnostics_panel = QFrame()
        diagnostics_panel.setObjectName("panel")
        diagnostics_layout = QVBoxLayout(diagnostics_panel)
        diagnostics_layout.setContentsMargins(16, 14, 16, 14)
        diagnostics_title = QLabel("Live diagnostics")
        diagnostics_title.setObjectName("panelTitle")
        diagnostics_layout.addWidget(diagnostics_title)
        self.status = QLabel("Stopped")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setAccessibleName("Live telemetry diagnostics")
        diagnostics_layout.addWidget(self.status)
        layout.addWidget(diagnostics_panel)

        paths_panel = QFrame()
        paths_panel.setObjectName("panel")
        paths_layout = QVBoxLayout(paths_panel)
        paths_layout.setContentsMargins(16, 14, 16, 14)
        paths_title = QLabel("Detected folders")
        paths_title.setObjectName("panelTitle")
        paths_layout.addWidget(paths_title)
        paths = QLabel(f"Journal / Status: {self.journal.directory}\nScreenshots: {self.cleaner.directory}")
        paths.setTextFormat(Qt.TextFormat.PlainText)
        paths.setWordWrap(True)
        paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        paths_layout.addWidget(paths)
        layout.addWidget(paths_panel)
        layout.addStretch()

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._changed)
        self.watcher.directoryChanged.connect(self._changed)
        self.read_timer = QTimer(self)
        self.read_timer.setSingleShot(True)
        self.read_timer.setInterval(40)
        self.read_timer.timeout.connect(self.poll)
        self.fallback_timer = QTimer(self)
        self.fallback_timer.setInterval(250)
        self.fallback_timer.timeout.connect(self.poll)
        self.display_timer = QTimer(self)
        self.display_timer.setInterval(500)
        self.display_timer.timeout.connect(self.refresh_status)
        self.display_timer.start()
        self.start_button.toggled.connect(self.set_running)
        self.rate.valueChanged.connect(self.set_rate)

    def set_rate(self, rate: int) -> None:
        self.controller.set_rate_hz(rate)
        self.settings.setValue("rate_hz", rate)

    def set_running(self, running: bool) -> None:
        if running:
            self.journal.start()
            self.publisher.reset()
            self.fallback_timer.start()
            self._refresh_watch()
        self.controller.set_active(running)
        self.start_button.setText("Stop" if running else "Start")
        self.refresh_status()

    def _refresh_watch(self) -> None:
        wanted = {str(self.journal.directory)} if self.journal.directory.is_dir() else set()
        if self.journal.path is not None and self.journal.path.exists():
            wanted.add(str(self.journal.path))
        current = set(self.watcher.files()) | set(self.watcher.directories())
        if current - wanted:
            self.watcher.removePaths(list(current - wanted))
        if wanted - current:
            failed = self.watcher.addPaths(list(wanted - current))
            if failed:
                self.journal.error = f"Could not watch {', '.join(failed)}; using polling"

    def _changed(self, _path: str) -> None:
        if not self.read_timer.isActive():
            self.read_timer.start()

    def poll(self) -> None:
        if not self.controller.active and not self.cleaner.running:
            self.fallback_timer.stop()
            self.read_timer.stop()
            watched = self.watcher.files() + self.watcher.directories()
            if watched:
                self.watcher.removePaths(watched)
            return
        events = self.journal.read()
        self._refresh_watch()
        self.controller.delete_screenshots([
            event["Filename"] for event in events if isinstance(event.get("Filename"), str)
        ])
        if self.controller.active:
            fixes = [event for event in events if planetary_screenshot(event) and event.get("_eligible") is not False]
            self.controller.record_screenshots(len(events))
            if fixes:
                self.publisher.publish(fixes[-1])

    def refresh_status(self) -> None:
        self.state_label.setText(
            "Enabled" if self.controller.active else
            "Cleaning up" if self.cleaner.running else "Stopped"
        )
        if self.controller.active:
            self.controller.refresh_status()
            text = self.controller.status
            text += f" | {self.controller.screenshot_count} screenshots read"
        else:
            text = (
                f"Stopped: {self.controller.press_count} F10 sent | "
                f"{self.controller.screenshot_count} screenshots read | "
                f"{self.cleaner.deleted} screenshots deleted"
            )
            if self.cleaner.running:
                text += " | finishing screenshot cleanup"
        text += f" | {self.publisher.updates} status updates"
        if self.publisher.skipped:
            text += f" | {self.publisher.skipped} status writes skipped"
        if self.publisher.failed:
            text += f" | {self.publisher.failed} status writes failed"
        if self.cleaner.waiting:
            text += f" | {self.cleaner.waiting} deletes pending"
        if not self.controller.active and self.cleaner.failed:
            text += f" | {self.cleaner.failed} screenshots could not be deleted"
        for error in (self.journal.error, self.publisher.error, self.cleaner.error):
            if error:
                text += f"\n{error}"
        self.status.setText(text)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.read_timer.stop()
        self.fallback_timer.stop()
        self.display_timer.stop()
        self.controller.close()
        self.settings.sync()
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--journal-directory", type=Path)
    parser.add_argument("--screenshot-directory", type=Path)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    lock_directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    try:
        lock_directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        QMessageBox.critical(None, APP_NAME, f"Could not create application data folder: {error}")
        return 1
    lock = QLockFile(str(lock_directory / "instance.lock"))
    if not lock.tryLock(0):
        QMessageBox.warning(None, APP_NAME, "Another instance is running, or the application lock is unavailable.")
        return 1
    window = MainWindow(journal_directory=args.journal_directory, screenshot_directory=args.screenshot_directory)
    window.show()
    try:
        return app.exec()
    finally:
        lock.unlock()
