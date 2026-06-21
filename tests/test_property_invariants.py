# coding: utf-8
"""
tests/test_property_invariants.py — Phase 13.4 Property Invariant Tests
=========================================================================

Stress-tests the design invariants that production CLI invocations rely
on, across orders of magnitude more stress than Phase 13.3's 10-thread
barrier tests. These tests catch regressions on:

  P1  session_id format under 1000 sequential instantiations per class
        (6000 instantiations total — marked ``slow`` per pytest.ini)
  P2  session_id uniqueness across 100 concurrent barrier-sync instantiations
        (free-threaded 3.14t gate; skipped on GIL builds)
  P3  Lock invariant under 100+ AND 500-thread concurrent check() on ONE
        KaliHealth instance (lost-increment / list-append race regression)
  P5  session_id regex match: ``YYYYMMDD_HHMMSS_ffffff_<8-hex>``
  P5b session_id µs field is exactly 6 digits (locks down strftime ``%f```
        format — prevents silent truncation regression)
  P6  uuid8 suffix is 8 lowercase hex chars (anti-collision armor)
  P7  get_summary()["session_id"] matches instance._session_id
        (only for classes that have a get_summary method — excludes
         SharedDirManager which has none)

Note: P2 + P3 require Python 3.14t free-threaded; otherwise skipped.
"""

import importlib
import json
import re
import sys
import sysconfig
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _require_314t() -> None:
    """Skip test if not running on Python 3.14t free-threaded."""
    if sysconfig.get_config_var("Py_GIL_DISABLED") != 1:
        pytest.skip(
            "Property invariant concurrency tests require Python 3.14t "
            "free-threaded runtime (Py_GIL_DISABLED=1); skipping on this build.",
        )


# All 6 main classes — used for session_id format invariants (P1, P5, P5b, P6).
WORM_CLASSES_ALL: list[tuple[str, str]] = [
    ("worms.nmap_scanner", "NmapScanner"),
    ("worms.kali_health", "KaliHealth"),
    ("worms.metasploit_bridge", "MetasploitBridge"),
    ("worms.wsl_recovery", "WSLRecovery"),
    ("worms.benchmark_worm", "BenchmarkWorm"),
    ("config.shared_dir", "SharedDirManager"),
]

# Classes that expose get_summary() — P7 only applies to these (SharedDirManager
# doesn't have get_summary; it uses different inspection patterns).
WORM_CLASSES_WITH_GET_SUMMARY: list[tuple[str, str]] = [
    ("worms.nmap_scanner", "NmapScanner"),
    ("worms.kali_health", "KaliHealth"),
    ("worms.metasploit_bridge", "MetasploitBridge"),
    ("worms.wsl_recovery", "WSLRecovery"),
    ("worms.benchmark_worm", "BenchmarkWorm"),
]

# Phase 12.1 session_id regex (canonical lock-in)
SESSION_ID_RE = re.compile(r"^(\d{8})_(\d{6})_(\d{6})_([0-9a-f]{8})$")


# ── P1: format invariant under 1000 sequential instantiations ────────────
# Marked slow because 1000 × 6 classes = 6000 instantiations.

@pytest.mark.slow
@pytest.mark.parametrize("module,class_name", WORM_CLASSES_ALL)
def test_P1_session_id_format_invariant_1000_sequential(
    module: str, class_name: str,
) -> None:
    """Every instantiation across 1000 sequential calls produces a 31-char
    session_id matching the canonical regex. Catches regressions in:
      - Length (Phase 12.1 invariant)
      - Format components (timestamp + uuid)
      - Lowercase-hex suffix
      - Cross-run uniqueness (uuid4 suffix guarantees this)
    """
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    seen: set[str] = set()
    for i in range(1000):
        inst = cls()
        sid = inst._session_id
        # Length
        assert len(sid) == 31, (
            f"{class_name} run #{i}: session_id len={len(sid)} != 31 "
            f"(sid={sid!r})"
        )
        # Pattern
        m = SESSION_ID_RE.match(sid)
        assert m is not None, (
            f"{class_name} run #{i}: session_id {sid!r} does NOT match "
            f"pattern YYYYMMDD_HHMMSS_ffffff_<8-hex>"
        )
        # Timestamp components parsable
        date_part, hms_part, micros_part, hex_suffix = m.groups()
        assert int(hms_part) <= 235959
        assert int(micros_part) <= 999999
        assert int(date_part) >= 20250101
        # hex suffix is lowercase
        assert hex_suffix == hex_suffix.lower() and len(hex_suffix) == 8
        seen.add(sid)
    assert len(seen) == 1000, (
        f"{class_name}: 1000 instantiations produced only {len(seen)} "
        f"distinct session_ids — uuid8 suffix not generating uniqueness!"
    )


# ── P2: 100 concurrent unique session_ids barrier ────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_CLASSES_ALL)
def test_P2_session_id_unique_under_concurrent_barrier(
    module: str, class_name: str,
) -> None:
    """100 concurrent threads barrier-sync-instantiate a class; expect
    100/100 distinct session_ids. uuid4 suffix makes this trivially true
    in single-thread but under 3.14t barrier race the µs-tick could
    collide — uuid4 closure defends."""
    _require_314t()
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    n = 100
    barrier = threading.Barrier(n)
    sids: list[str | None] = [None] * n

    def worker(slot: int) -> None:
        barrier.wait(timeout=30)
        sids[slot] = cls()._session_id

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert all(s is not None for s in sids), (
        f"{class_name}: some workers failed to instantiate {n} concurrent"
    )
    distinct = len(set(sids))
    assert distinct == n, (
        f"{class_name}: 100 concurrent barrier-instantiations produced "
        f"only {distinct}/{n} distinct session_ids (collision!)"
    )


# ── P3: lock stress at 100 and 500 threads ──────────────────────────────

@pytest.mark.parametrize("concurrent_threads", [100, 500])
def test_P3_lock_invariant_under_extreme_concurrent_stress(
    concurrent_threads: int,
) -> None:
    """KaliHealth.check() under high-concurrency stress on ONE instance.

    Headline lock-invariant: ``with self._lock:`` inside check() must
    serialize ALL of these simultaneously for N concurrent calls:
      - self._history.append()
      - self._save_history()
      - self._checks_run += 1

    After N concurrent calls, _checks_run must EXACTLY equal N
    (lost-increment signal if < N).
    """
    _require_314t()
    import importlib as _im
    from unittest.mock import patch
    mod = _im.import_module("worms.kali_health")

    fake_drive = type("F", (), {
        "total": 500 * 1024**3, "free": 100 * 1024**3, "used": 400 * 1024**3,
    })()
    backup_content: str | None = None
    history_path = mod.HISTORY_FILE
    if Path(history_path).exists():
        backup_content = Path(history_path).read_text(encoding="utf-8")

    try:
        with patch("shutil.disk_usage", return_value=fake_drive), \
             patch.object(mod.KaliHealth, "_record_pams", lambda *a, **k: None), \
             patch.object(mod.KaliHealth, "_load_history", return_value=[]), \
             patch.object(mod.KaliHealth, "_save_history", return_value=None):
            kh = mod.KaliHealth()
            n = concurrent_threads
            barrier = threading.Barrier(n)
            results: list[Any] = [None] * n

            def worker(slot: int) -> None:
                barrier.wait(timeout=60)
                try:
                    results[slot] = kh.check()
                except BaseException as e:  # noqa: BLE001
                    results[slot] = e

            threads = [
                threading.Thread(target=worker, args=(i,), name=f"P3-{i}")
                for i in range(n)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)

        for slot, r in enumerate(results):
            assert isinstance(r, mod.KaliHealthStatus), (
                f"P3 slot {slot}: {type(r).__name__}: {r!r}"
            )
        assert kh._checks_run == n, (
            f"P3 after {n} concurrent check(): _checks_run={kh._checks_run} "
            f"(expected {n}; lost-increment / lock-bypass regression)"
        )
        assert len(kh._history) == n, (
            f"P3 after {n} concurrent check(): _history size={len(kh._history)} "
            f"(expected {n}; list-append race / lock-bypass regression)"
        )
    finally:
        if backup_content is not None:
            Path(history_path).write_text(backup_content, encoding="utf-8")


# ── P5: regex match across all 6 classes ─────────────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_CLASSES_ALL)
def test_P5_session_id_matches_canvas_regex(module: str, class_name: str) -> None:
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    inst = cls()
    sid = inst._session_id
    m = SESSION_ID_RE.match(sid)
    assert m is not None, (
        f"{class_name}._session_id {sid!r} does NOT match pattern "
        f"YYYYMMDD_HHMMSS_ffffff_<8-hex>"
    )
    assert len(m.groups()) == 4


# ── P5b: microsecond field is exactly 6 digits ──────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_CLASSES_ALL)
def test_P5b_microsecond_field_is_6_digits(module: str, class_name: str) -> None:
    """The 6-digit µs field (between the 6-digit HHMMSS and the 8-hex suffix)
    must be exactly 6 digits — locks down the strftime ``%f`` format
    invariant. If someone replaces ``%f`` with ``%S.something`` or truncates,
    the µs field would drift in length and break consumers.
    """
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    sid = cls()._session_id
    parts = sid.split("_")
    # Expected format: YYYYMMDD_HHMMSS_ffffff_<hex8> → 4 parts
    assert len(parts) == 4, (
        f"{class_name}.session_id {sid!r} doesn't have 4 underscored parts "
        f"(got {len(parts)})"
    )
    micros = parts[2]
    assert len(micros) == 6, (
        f"{class_name}.session_id µs field {micros!r} length={len(micros)} != 6 "
        f"(strftime %f regressed)"
    )
    assert micros.isdigit(), (
        f"{class_name}.session_id µs field {micros!r} contains non-digit chars"
    )
    assert 0 <= int(micros) <= 999999, (
        f"{class_name}.session_id µs field {micros!r} not in [0, 999999]"
    )


# ── P6: uuid suffix is 8 lowercase hex ───────────────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_CLASSES_ALL)
def test_P6_uuid_suffix_is_8_lowercase_hex(module: str, class_name: str) -> None:
    """The 8-char hex suffix after the underscore MUST be lowercase hex."""
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    sid = cls()._session_id
    suffix = sid.rsplit("_", 1)[-1]
    assert len(suffix) == 8, (
        f"{class_name}.session_id suffix {suffix!r} length != 8"
    )
    assert all(c in "0123456789abcdef" for c in suffix), (
        f"{class_name}.session_id suffix {suffix!r} contains non-lowercase-hex"
    )


# ── P7: get_summary session_id match (only for classes with get_summary) ──

@pytest.mark.parametrize("module,class_name", WORM_CLASSES_WITH_GET_SUMMARY)
def test_P7_summary_session_id_matches_session_id_attr(
    module: str, class_name: str,
) -> None:
    """If a worm's get_summary() returns a 'session_id' field, it MUST
    match the instance's own _session_id attribute. Excludes
    SharedDirManager which doesn't expose get_summary().
    """
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    inst = cls()
    summary = inst.get_summary()
    if "session_id" in summary:
        assert summary["session_id"] == inst._session_id, (
            f"{class_name}.get_summary()['session_id']={summary['session_id']!r} "
            f"differs from inst._session_id={inst._session_id!r} "
            f"(cross-attribution bug)"
        )


if __name__ == "__main__":
    import subprocess as _sp
    import sys as _s
    _s.exit(_sp.call([_s.executable, "-m", "pytest", __file__, "-v"]))
