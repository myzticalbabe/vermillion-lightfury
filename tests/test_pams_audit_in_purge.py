# coding: utf-8
"""
test_pams_audit_in_purge.py — Locks Phase 11.3 N2 PAMS audit integration
=========================================================================

Validates that ``purge_stale_rc_scripts()`` emits PAMS records per deletion,
per stuck-file, and per batch completion. Backward-compat: PAMS unavailability
must NOT crash the cleanup contract ("never throws on cleanup failure").

Closes Phase 10.10.1 nit **N2** that was deferred from Phase 10.10.1 to
Phase 11.3.

Stdlib-only. No new deps.
"""
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def old_rc(tmp_path):
    """Create an old `.rc` file with controlled mtime in tmp_path.

    Returns the file Path. The file's age is set to 25 hours (so older than
    the default 24-hour purge cutoff).
    """
    rc = tmp_path / f"msf_test_{int(time.time() * 1000)}.rc"
    rc.write_text("use test\nexit\n")
    mtime = time.time() - (25 * 3600)
    os.utime(rc, (mtime, mtime))
    return rc


@pytest.fixture
def fresh_rc(tmp_path):
    """Create a fresh `.rc` file (1 minute old) — younger than purge cutoff."""
    rc = tmp_path / f"msf_fresh_{int(time.time() * 1000)}.rc"
    rc.write_text("use test\nexit\n")
    mtime = time.time() - 60
    os.utime(rc, (mtime, mtime))
    return rc


@pytest.fixture
def stub_pams(monkeypatch):
    """Replace module-level `_pams` with a list-backed MagicMock recorder.

    Returns the recorder; ``records`` list attribute holds the captured calls.
    """
    import config.shared_dir as sd
    recorder = MagicMock()
    captured = []

    def record_event(event_type, data, source=None, severity=None):
        captured.append({
            "event_type": event_type,
            "data": data,
            "source": source,
            "severity": severity,
        })

    recorder.record_event.side_effect = record_event
    recorder.records = captured
    monkeypatch.setattr(sd, "_pams", recorder)
    return recorder


@pytest.fixture
def no_pams(monkeypatch):
    """Force _pams = None (PAMS offline) — must not crash cleanup."""
    import config.shared_dir as sd
    monkeypatch.setattr(sd, "_pams", None)


# ── T1: per-file deletion record ─────────────────────────────────────────────

def test_T1_file_deleted_record_emitted_per_deletion(
    tmp_path, old_rc, stub_pams
):
    """Each successful deletion emits one ``file_deleted`` PAMS record."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import purge_stale_rc_scripts

    deleted = purge_stale_rc_scripts(
        log_dir=str(tmp_path), max_age_hours=24, throttle_seconds=0,
    )
    assert deleted == 1
    assert not old_rc.exists()

    file_records = [
        r for r in stub_pams.records
        if r["event_type"] == "shared_dir.purge_stale_rc_scripts.file_deleted"
    ]
    assert len(file_records) == 1, (
        f"expected 1 file_deleted record, got {len(file_records)}: {file_records}"
    )
    rec = file_records[0]
    assert rec["data"]["log_dir"] == str(tmp_path)
    assert rec["data"]["deleted_path"] == str(old_rc)
    assert rec["data"]["max_age_hours"] == 24
    assert rec["data"]["age_seconds"] >= 25 * 3600 - 1  # ~25h, allow 1s slack
    assert rec["source"] == "SharedDirManager"
    assert rec["severity"] == "info"


# ── T2: batch summary record ────────────────────────────────────────────────

def test_T2_batch_completed_summary_emitted_when_deleted_positive(
    tmp_path, old_rc, stub_pams
):
    """After the deletion loop, a ``batch_completed`` summary record is emitted."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import purge_stale_rc_scripts

    purge_stale_rc_scripts(
        log_dir=str(tmp_path), max_age_hours=24, throttle_seconds=0,
    )

    batch_records = [
        r for r in stub_pams.records
        if r["event_type"] == "shared_dir.purge_stale_rc_scripts.batch_completed"
    ]
    assert len(batch_records) == 1
    rec = batch_records[0]
    assert rec["data"]["deleted_count"] == 1
    assert rec["data"]["max_age_hours"] == 24
    assert rec["source"] == "SharedDirManager"
    assert rec["severity"] == "info"


# ── T3: no records when nothing is eligible ──────────────────────────────────

def test_T3_no_batch_or_file_records_when_all_fresh(
    tmp_path, fresh_rc, stub_pams
):
    """When all files are younger than cutoff, no PAMS records emitted (only noise passes)."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import purge_stale_rc_scripts

    deleted = purge_stale_rc_scripts(
        log_dir=str(tmp_path), max_age_hours=24, throttle_seconds=0,
    )
    assert deleted == 0
    assert fresh_rc.exists()

    file_records = [
        r for r in stub_pams.records
        if r["event_type"] == "shared_dir.purge_stale_rc_scripts.file_deleted"
    ]
    batch_records = [
        r for r in stub_pams.records
        if r["event_type"] == "shared_dir.purge_stale_rc_scripts.batch_completed"
    ]
    assert len(file_records) == 0, f"unexpected file records: {file_records}"
    assert len(batch_records) == 0, f"unexpected batch records: {batch_records}"


# ── T4: stuck-file warning record ───────────────────────────────────────────

def test_T4_stuck_file_warning_emitted_on_oserror(
    tmp_path, old_rc, stub_pams, monkeypatch
):
    """When os.remove raises OSError, a warning record is emitted with error details."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import purge_stale_rc_scripts

    # Simulate chmod-0444 / lock-contention: os.remove raises OSError
    def fake_remove(path):
        raise OSError(13, "Permission denied [test mocked]")
    monkeypatch.setattr("os.remove", fake_remove)

    deleted = purge_stale_rc_scripts(
        log_dir=str(tmp_path), max_age_hours=24, throttle_seconds=0,
    )
    assert deleted == 0
    assert old_rc.exists()  # nothing actually deleted

    warn_records = [
        r for r in stub_pams.records
        if r["event_type"] == "shared_dir.purge_stale_rc_scripts.stuck_file_warning"
    ]
    assert len(warn_records) == 1
    rec = warn_records[0]
    assert rec["data"]["blocked_path"].endswith(".rc")
    assert old_rc.name in rec["data"]["blocked_path"]  # basename stable across tests
    assert "Permission denied" in rec["data"]["error"]
    assert rec["severity"] == "warning"
    assert rec["source"] == "SharedDirManager"


# ── T5: PAMS unavailability does NOT crash cleanup ───────────────────────────

def test_T5_graceful_when_pams_unavailable(tmp_path, old_rc, no_pams):
    """With ``_pams = None``, cleanup MUST work end-to-end without crashing."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import purge_stale_rc_scripts

    # Even though PAMS is offline (no_pams fixture), the delete must succeed.
    deleted = purge_stale_rc_scripts(
        log_dir=str(tmp_path), max_age_hours=24, throttle_seconds=0,
    )
    assert deleted == 1
    assert not old_rc.exists()


# ── T6: throttled second call emits no records ─────────────────────────────

def test_T6_no_records_when_throttled(tmp_path, old_rc, stub_pams):
    """Within throttle window, second call returns 0 + emits zero records."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import purge_stale_rc_scripts
    import config.shared_dir as sd

    # Reset module-level throttle state — earlier tests in the same pytest
    # session may have populated `_LAST_RC_PURGE_TIME`, which would cause
    # T6's first call to be throttled (returns 0, file NOT deleted) and
    # confound the "first call deletes, second call throttles" contract.
    sd._LAST_RC_PURGE_TIME = 0.0

    # First call: clears throttle, deletes the file, emits records
    purge_stale_rc_scripts(
        log_dir=str(tmp_path), max_age_hours=24, throttle_seconds=999999,
    )
    assert not old_rc.exists()
    first_count = len(stub_pams.records)
    assert first_count >= 2, "expected at least file_deleted + batch_completed"

    # Recreate + second call within throttle window → 0 records
    old_rc.write_text("use test\nexit\n")
    mtime = time.time() - (25 * 3600)
    os.utime(old_rc, (mtime, mtime))
    stub_pams.records.clear()

    deleted = purge_stale_rc_scripts(
        log_dir=str(tmp_path), max_age_hours=24, throttle_seconds=999999,
    )
    assert deleted == 0
    assert len(stub_pams.records) == 0, (
        f"throttled call should emit 0 records, got {stub_pams.records}"
    )
    assert old_rc.exists()  # not deleted when throttled


# ── T7: _record_pams_purge helper never raises on backend failure ────────────

def test_T7_purge_helper_swallows_backend_failures(monkeypatch):
    """``_record_pams_purge`` itself never throws — a PAMS backend outage
    must not propagate into the cleanup's catch-all boundary."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import _record_pams_purge
    import config.shared_dir as sd

    class RaisingRecorder:
        def record_event(self, *args, **kwargs):
            raise RuntimeError("PAMS backend exploded [test mocked]")

    monkeypatch.setattr(sd, "_pams", RaisingRecorder())

    # Must NOT raise
    _record_pams_purge("file_deleted", {"foo": "bar"})
    _record_pams_purge("stuck_file_warning", {"foo": "bar"}, severity="warning")
    _record_pams_purge("batch_completed", {"foo": "bar"})


# ── T8 (bonus): helper short-circuits when _pams is None ─────────────────────

def test_T8_helper_short_circuits_when_pams_none(monkeypatch):
    """When ``_pams = None``, helper returns immediately (no NameError)."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import _record_pams_purge
    import config.shared_dir as sd
    monkeypatch.setattr(sd, "_pams", None)

    # Must NOT raise
    _record_pams_purge("any_event", {"k": "v"})