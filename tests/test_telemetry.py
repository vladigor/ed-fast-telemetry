import json
import os
from pathlib import Path

import pytest

from ed_fast_telemetry.telemetry import HAS_LAT_LONG_FLAG, JournalTail, StatusPublisher


def screenshot(**updates):
    event = {
        "event": "Screenshot", "timestamp": "2026-11-06T06:22:09Z",
        "Filename": "\\ED_Pictures\\Screenshot_0042.bmp",
        "System": "Sol", "Body": "Earth",
        "Latitude": 1.234567, "Longitude": 2.345678,
        "Heading": 123, "Altitude": 456.789,
    }
    event.update(updates)
    return event


def append(path: Path, event: dict):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


@pytest.fixture
def files(tmp_path):
    journal = tmp_path / "Journal.2026-11-06T000000.01.log"
    append(journal, {"event": "Location", "StarSystem": "Sol", "Body": "Earth"})
    status = tmp_path / "Status.json"
    status.write_text(json.dumps({
        "timestamp": "2026-11-06T06:22:09Z", "event": "Status",
        "Flags": HAS_LAT_LONG_FLAG, "Flags2": 123, "BodyName": "Earth",
        "Latitude": 10, "Longitude": 20, "Heading": 90, "Altitude": 100,
        "Fuel": {"FuelMain": 12.34}, "Unknown": [True, "kept"],
    }), encoding="utf-8")
    return journal, status


def test_tail_skips_history_but_retains_context(files):
    journal, _ = files
    append(journal, screenshot())
    tail = JournalTail(journal.parent)
    tail.start()
    assert tail.read() == []
    assert tail.system == "Sol"
    append(journal, screenshot())
    events = tail.read()
    assert len(events) == 1
    assert events[0]["_journal_system"] == "Sol"
    assert tail.read() == []


def test_tail_retains_partial_line_and_unicode(files):
    journal, _ = files
    tail = JournalTail(journal.parent)
    tail.start()
    line = json.dumps(screenshot(Body="Éarth"), ensure_ascii=False).encode("utf-8") + b"\n"
    split = line.index("É".encode("utf-8")) + 1
    with journal.open("ab") as handle:
        handle.write(line[:split])
    assert tail.read() == []
    with journal.open("ab") as handle:
        handle.write(line[split:])
    assert tail.read()[0]["Body"] == "Éarth"


def test_tail_rotation_and_truncation(files):
    journal, _ = files
    tail = JournalTail(journal.parent)
    tail.start()
    journal.write_text(json.dumps(screenshot()) + "\n", encoding="utf-8")
    # Ensure actual truncation, rather than overwriting with a longer file.
    tail.offset = journal.stat().st_size + 1
    assert tail.read()[0]["Body"] == "Earth"
    newer = journal.parent / "Journal.2026-11-07T000000.01.log"
    append(newer, {"event": "Location", "StarSystem": "Other", "Body": "Other planet"})
    append(newer, screenshot(System="Other", Body="Other planet"))
    later = journal.stat().st_mtime + 10
    os.utime(newer, (later, later))
    tail._scan_at = 0
    assert tail.read()[0]["_journal_system"] == "Other"
    assert tail.path == newer


def test_body_departure_invalidates_fix_but_retains_cleanup_filename(files):
    journal, _ = files
    tail = JournalTail(journal.parent)
    tail.start()
    append(journal, screenshot())
    append(journal, {"event": "LeaveBody"})
    event = tail.read()[0]
    assert event["_eligible"] is False
    assert event["Filename"].endswith("Screenshot_0042.bmp")


def test_invalid_line_is_reported_and_next_event_still_read(files):
    journal, _ = files
    tail = JournalTail(journal.parent)
    tail.start()
    with journal.open("a") as handle:
        handle.write("not-json\n")
    append(journal, screenshot())
    assert len(tail.read()) == 1
    assert "Invalid journal line" in tail.error


def test_missing_journal_reported(tmp_path):
    tail = JournalTail(tmp_path)
    tail.start()
    assert "No Journal" in tail.error
    assert tail.read() == []
    assert "No Journal" in tail.error


def test_publisher_preserves_other_fields_and_same_second_is_fresh(files):
    _, status = files
    original = json.loads(status.read_text())
    publisher = StatusPublisher(status)
    assert publisher.publish(screenshot())
    expected = dict(original)
    expected.update(Latitude=1.234567, Longitude=2.345678, Heading=123, Altitude=456.789)
    assert json.loads(status.read_text()) == expected
    assert publisher.updates == 1
    assert not list(status.parent.glob(".ed-fast-status-*.tmp"))


def test_publisher_updates_timestamp_and_preserves_missing_fields(files):
    _, status = files
    publisher = StatusPublisher(status)
    assert publisher.publish(screenshot(timestamp="2026-11-06T06:22:10Z", Heading=None, Altitude=None))
    data = json.loads(status.read_text())
    assert data["timestamp"] == "2026-11-06T06:22:10Z"
    assert data["Heading"] == 90
    assert data["Altitude"] == 100


@pytest.mark.parametrize("updates", [
    {"timestamp": "2026-11-06T06:22:08Z"}, {"Body": "Mars"},
    {"_journal_system": "Other"}, {"Latitude": None}, {"Longitude": 200},
    {"Latitude": float("nan")}, {"Latitude": True},
    {"timestamp": "invalid"}, {"_eligible": False},
])
def test_publisher_skips_invalid_or_mismatched_fix(files, updates):
    _, status = files
    original = status.read_bytes()
    publisher = StatusPublisher(status)
    assert not publisher.publish(screenshot(**updates))
    assert status.read_bytes() == original
    assert publisher.skipped == 1


def test_publisher_respects_no_coordinates_flag(files):
    _, status = files
    data = json.loads(status.read_text())
    data["Flags"] = 0
    status.write_text(json.dumps(data))
    publisher = StatusPublisher(status)
    assert not publisher.publish(screenshot())
    assert publisher.skipped == 1


@pytest.mark.parametrize("content", ["{", "[]", '{"timestamp": "invalid"}'])
def test_invalid_status_is_not_repaired(files, content):
    _, status = files
    status.write_text(content)
    publisher = StatusPublisher(status)
    assert not publisher.publish(screenshot())
    assert status.read_text() == content
    assert publisher.failed == 1
    assert "Could not update Status.json" in publisher.error


def test_missing_status_is_not_created(files):
    _, status = files
    status.unlink()
    publisher = StatusPublisher(status)
    assert not publisher.publish(screenshot())
    assert not status.exists()
    assert publisher.failed == 1


def test_locked_status_reports_error_and_removes_temporary(files, monkeypatch):
    _, status = files
    original = status.read_bytes()

    def locked(*args):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", locked)
    publisher = StatusPublisher(status)
    assert not publisher.publish(screenshot())
    assert "locked" in publisher.error
    assert status.read_bytes() == original
    assert not list(status.parent.glob(".ed-fast-status-*.tmp"))


def test_concurrent_status_change_is_skipped(files, monkeypatch):
    _, status = files
    real_read = Path.read_bytes
    calls = 0
    updated = status.read_text().replace('"Heading": 90', '"Heading": 999')

    def read(path):
        nonlocal calls
        if path == status:
            calls += 1
            if calls == 2:
                path.write_text(updated)
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    publisher = StatusPublisher(status)
    assert not publisher.publish(screenshot())
    assert status.read_text() == updated
    assert "changed; skipped" in publisher.error
