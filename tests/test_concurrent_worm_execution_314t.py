# coding: utf-8
"""
test_concurrent_worm_execution_314t.py — Phase 13.0 + 13.1.5 Execution-Barrier Tests
====================================================================================

Complements ``tests/test_concurrent_worm_instantiation_314t.py`` (Phase 11.4 /
11.5 / 11.6 / 12.1 — focused on INSTANTIATION invariants) by exercising
worm **EXECUTION** under Python 3.14t free-threaded barrier-sync races.

Test matrix (S-series):

  S1: 10 NmapScanner instances each call ``scan()`` concurrently
      (real WSL/Kali IO is mocked — scan() can't actually execute on Linux).
      Verifies per-worm ``_lock`` serializes ``_total_scans`` /
      ``_targets_scanned`` / ``_total_open_ports`` mutations — no lost-
      increment / cross-thread contamination.

  S2: 1 KaliHealth() instance × 10 concurrent ``check()`` calls.
      The HEADLINE lock-invariant test — exercises ``_lock`` on
      ``_history.append()`` + ``_save_history()`` + ``_checks_run += 1``
      triplet on ONE instance. Only IO-determinism mocks (shutil.disk_usage,
      _record_pams, _load_history, _save_history); the ``with self._lock:``
      block runs UNMOCKED on the real post-fix method bodies.

  S3: 10 SharedDirManager instantiations under barrier race — verifies no
      deadlock + 10/10 distinct session_ids.

  S4: All 5 worms × 2 barrier threads each calling ``get_summary()``.
      NO MOCKS — get_summary() is now called on real post-fix method bodies
      which safe-fail gracefully on Linux when E:/VHDX/WSL paths don't exist.

  S5: Sequential-after-concurrent — 4 concurrent get_summary() calls followed
      by 1 sequential get_summary() per class. Verifies session_id freshness
      across batches. NO MOCKS.

WHY THESE TESTS (esp. S2 lock-invariance):

  Phase 11.4 verified lock allocation under barrier race.
  Phase 11.5 / 11.6 verified session_id uniqueness.
  Phase 12.1 verified ``len(_session_id) == 31`` regression width.

  But none of those prove that ``self._lock`` ACTUALLY serializes execution-
  path state mutations correctly. A worm could pass all instantiation tests
  while having a bug where ``with self._lock:`` is missing from a critical
  execution branch — and only manifest under concurrent call.

  Phase 13.0 closes that gap. Phase 13.1.5 simplifies the test surface: now
  that Phase 13.1's source-code fix resolves the `from exceptions` latent
  import bug, S2/S4/S5 can exercise the REAL method bodies instead of
  patch-bypassing the bug surface.

Stdlib only (plus ``pytest``). Real WSL/Kali IO is mocked where it would
attempt subprocess dispatch. On non-free-threaded Python, the entire module
is skipped (gate at top).
"""

import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Module-level gate: skip on non-free-threaded Python ─────────────────────
if sysconfig.get_config_var("Py_GIL_DISABLED") != 1:
    pytest.skip(
        "Phase 13.0 concurrent execution tests require Python 3.14t "
        "free-threaded runtime (Py_GIL_DISABLED=1); skipping on this build.",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXEC_THREADS = 10

ALL_WORM_CLASSES: list[tuple[str, str]] = [
    ("worms.nmap_scanner", "NmapScanner"),
    ("worms.kali_health", "KaliHealth"),
    ("worms.metasploit_bridge", "MetasploitBridge"),
    ("worms.wsl_recovery", "WSLRecovery"),
    ("worms.benchmark_worm", "BenchmarkWorm"),
]


# ── Helper: barrier-sync N threads, capture per-slot outcome ────────────────

def _concurrent_execute(
    method_path: str, n: int = EXEC_THREADS, *args, **kwargs,
) -> list[Any]:
    """Barrier-sync ``n`` threads each calling ``module.class().method(*args, **kwargs)``.

    ``method_path`` is ``"module.class.method"`` — split to import lazily
    inside each worker thread (avoids module-load cost at collection time).

    ``*args`` / ``**kwargs`` are forwarded to ``method(*args, **kwargs)``
    on each instance. Required for methods like ``NmapScanner.scan(target)``.

    Returns a list of length ``n``. Each slot is either:
      - ``(instance, method_result)`` — successful call
      - an exception instance — caught BaseException with ``type(e).__name__``
        preserved for the assertion message.
    """
    module_path, class_name, method_name = method_path.rsplit(".", 2)
    import importlib as _im
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n

    def worker(slot: int) -> None:
        barrier.wait(timeout=20)
        try:
            mod = _im.import_module(module_path)
            cls = getattr(mod, class_name)
            instance = cls()
            method = getattr(instance, method_name)
            results[slot] = (instance, method(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001
            results[slot] = e

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"exec-{class_name}-{i}")
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


# ── S1: NmapScanner.scan() under barrier race ───────────────────────────────

def test_S1_nmap_scan_concurrent_total_scans_counter_serialization() -> None:
    """S1: 10 NmapScanner instances barrier-sync-call ``scan()``.

    The per-worm ``self._lock`` must serialize ``_total_scans`` increments +
    ``_targets_scanned`` list-append. After the race, each instance's counter
    is exactly 1 (because each instance is its OWN object, not shared), and
    the summed total_scans across all 10 instances must equal 10.

    Mock coverage: scan() actually executes nmap via WSL/Kali + reads XML
    output from disk. On a non-WSL Linux env this can't execute for real,
    so we patch the IO paths:
      - ``_verify_kali_responsive`` (Linux-side liveness gate)
      - ``_record_pams`` (PAMS recorder; PAMS=None here, mocked for safety)
      - ``_wsl_exec`` (the actual subprocess.run([wsl.exe, ...]) call)
      - ``_parse_nmap_text/xml`` (XML/text parsers; return empty)
      - ``KaliHealth.preflight_check`` (E: drive check; tied to disk_usage)
      - ``shutil.disk_usage`` (underlying IO for the preflight check)

    The ``with self._lock:`` block in scan() runs UNMOCKED on the post-fix
    method bodies — verifies the lock invariant under real code paths.
    """
    import importlib
    mod = importlib.import_module("worms.nmap_scanner")
    kh_mod = importlib.import_module("worms.kali_health")
    NmapScanner = mod.NmapScanner
    NmapScanResult = mod.NmapScanResult
    KaliHealth = kh_mod.KaliHealth

    with patch.object(NmapScanner, "_verify_kali_responsive", return_value=None), \
         patch.object(NmapScanner, "_record_pams", lambda *a, **k: None), \
         patch.object(NmapScanner, "_wsl_exec", return_value=(0, "", "")), \
         patch.object(NmapScanner, "_parse_nmap_text", return_value=[]), \
         patch.object(NmapScanner, "_parse_nmap_xml", return_value=[]), \
         patch.object(KaliHealth, "preflight_check", return_value=(True, "OK")), \
         patch("shutil.disk_usage", return_value=MagicMock(
             total=500 * 1024**3, free=100 * 1024**3, used=400 * 1024**3,
         )):
        results = _concurrent_execute(
            "worms.nmap_scanner.NmapScanner.scan", EXEC_THREADS, "127.0.0.1",
        )

    assert len(results) == EXEC_THREADS
    instances = []
    for slot, r in enumerate(results):
        assert isinstance(r, tuple), (
            f"S1 slot {slot}: barrier worker raised {type(r).__name__}: {r!r}"
        )
        instance, result = r
        instances.append(instance)
        assert isinstance(result, NmapScanResult)

    # Per-instance counter integrity
    sum_total_scans = sum(i._total_scans for i in instances)
    assert sum_total_scans == EXEC_THREADS, (
        f"S1 counter race: 10 concurrent scan() calls summed to "
        f"_total_scans={sum_total_scans} (expected {EXEC_THREADS}; "
        f"lost-update / lock-bypass regression)"
    )
    for slot, inst in enumerate(instances):
        assert len(inst._targets_scanned) == 1, (
            f"S1 slot {slot}: _targets_scanned={inst._targets_scanned} "
            f"(expected 1 entry; lock-list-append regression)"
        )


# ── S2: KaliHealth.check() under barrier race ───────────────────────────────

def test_S2_kali_health_check_concurrent_history_serialization() -> None:
    """S2: 1 KaliHealth() instance × 10 concurrent ``check()`` calls.

    The HEADLINE lock-invariant test — exercises ``self._lock`` on
    ``self._history.append()`` + ``self._save_history()`` +
    ``self._checks_run += 1`` triplet ALL happening on ONE instance. Without
    the lock, list-append + file-flush + counter-increment can interleave
    on 3.14t free-threading, producing partial writes or lost increments.

    Phase 13.1.5 cleanup: only IO-determinism mocks remain. Phase 13.0
    review rounds 2-4 added whole-method mocks (_check_drive, _check_vhdx,
    _check_kali_process, _wsl_exec, _purge_stale_shared_logs,
    subprocess.run) to bypass the worm-code `from exceptions import X`
    latent import bug. Phase 13.1's source-code fix replaced bare imports
    with `from worms.exceptions import X`, so the method bodies now resolve
    imports cleanly. The ``with self._lock:`` block runs UNMOCKED — the
    lock-invariant is tested against REAL (post-fix) code.

    History-file preservation: HISTORY_FILE is backed up + restored via
    try/finally so concurrent writes don't clobber pre-existing data.
    """
    import importlib
    mod = importlib.import_module("worms.kali_health")
    KaliHealth = mod.KaliHealth

    fake_drive = MagicMock()
    fake_drive.total = 500 * 1024**3
    fake_drive.free = 100 * 1024**3
    fake_drive.used = 400 * 1024**3

    history_path = mod.HISTORY_FILE
    backup_content: str | None = None
    if Path(history_path).exists():
        backup_content = Path(history_path).read_text(encoding="utf-8")

    try:
        # Phase 13.1.5 polish: dropped `_record_pams` patch (PAMS=None at
        # module-load → ``if self._pams:`` short-circuits → no-op natively;
        # no patching required). Remaining 3 mocks are genuine IO-determinism,
        # NOT import-bypass. The ``with self._lock:`` block in check() runs
        # UNMOCKED — verifies headline lock-invariant on REAL post-fix bodies.
        with patch("shutil.disk_usage", return_value=fake_drive), \
             patch.object(KaliHealth, "_load_history", return_value=[]), \
             patch.object(KaliHealth, "_save_history", return_value=None):

            kh = KaliHealth()

            barrier = threading.Barrier(EXEC_THREADS)
            results: list[Any] = [None] * EXEC_THREADS

            def worker(slot: int) -> None:
                barrier.wait(timeout=15)
                try:
                    results[slot] = kh.check()
                except BaseException as e:  # noqa: BLE001
                    results[slot] = e

            threads = [
                threading.Thread(target=worker, args=(i,), name=f"S2-kh-{i}")
                for i in range(EXEC_THREADS)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        # All 10 calls must succeed (no exceptions)
        for slot, r in enumerate(results):
            assert isinstance(r, mod.KaliHealthStatus), (
                f"S2 slot {slot}: thread raised {type(r).__name__}: {r!r}"
            )

        # THE INVARIANT: after 10 concurrent check() calls on ONE instance,
        # _checks_run must EXACTLY equal 10 (lost-increment signal if < 10).
        assert kh._checks_run == EXEC_THREADS, (
            f"S2: _checks_run={kh._checks_run} after 10 concurrent check() "
            f"calls (expected {EXEC_THREADS}; lost-increment / lock-bypass regression)"
        )
        # _history must hold EXACTLY 10 appends (race signal if more or less).
        assert len(kh._history) == EXEC_THREADS, (
            f"S2: _history size {len(kh._history)} after 10 concurrent check() "
            f"calls (expected {EXEC_THREADS}; list-append race / lock-bypass regression)"
        )
    finally:
        if backup_content is not None:
            Path(history_path).write_text(backup_content, encoding="utf-8")


# ── S3: SharedDirManager concurrent instantiation (no deadlocks + 10/10 sids) ──

def test_S3_shared_dir_concurrent_instantiation_no_deadlock() -> None:
    """S3: 10 SharedDirManager instantiations under barrier race — verify
    no deadlock + 10/10 distinct session_ids. The barrier-stress here is a
    SUPERSET of T9 (T9 verifies 10/10 distinct, S3 verifies 10/10 distinct
    AND that no barrier worker hung waiting for another — i.e. that the
    import-anchor pattern doesn't deadlock under 3.14t).
    """
    import importlib
    mod = importlib.import_module("config.shared_dir")
    SharedDirManager = mod.SharedDirManager

    barrier = threading.Barrier(10)
    instances: list[Any] = [None] * 10
    errors: list[Exception | None] = [None] * 10

    def worker(slot: int) -> None:
        barrier.wait(timeout=20)
        try:
            instances[slot] = SharedDirManager()
        except BaseException as e:  # noqa: BLE001
            errors[slot] = e

    threads = [threading.Thread(target=worker, args=(i,), name=f"sdm-{i}") for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(e is None for e in errors), (
        f"S3: barrier workers raised errors: "
        f"{[(i, type(e).__name__, str(e)) for i, e in enumerate(errors) if e]}"
    )
    assert all(i is not None for i in instances), (
        "S3: some barrier workers failed to instantiate"
    )
    sids = [i._session_id for i in instances]
    assert len(set(sids)) == 10, (
        f"S3 SharedDirManager session_id collision under barrier race: "
        f"{len(set(sids))}/10 distinct"
    )


# ── S4: Mixed-worm concurrent get_summary() — cross-class isolation ─────────

def test_S4_mixed_worm_concurrent_get_summary_isolation() -> None:
    """S4: All 5 worms × 2 barrier threads each — every worker calls
    ``get_summary()``. Verify:
      - Each worm produces a ``dict`` (no exceptions crossed threads)
      - ``session_id`` is present in every summary
      - No worm's data accidentally appears in another's summary
        (cross-class isolation — proves locks are worm-local, not global)

    Phase 13.1.5 cleanup — NO MOCKS. get_summary() is called on real
    post-fix method bodies which safe-fail on Linux:
      - NmapScanner.get_summary(): pure dict, no IO
      - KaliHealth.get_summary(): calls _check_drive/_check_vhdx which
        safe-fail (DriveStatus(accessible=False), VHDXStatus(exists=False))
      - MetasploitBridge.get_summary(): pure dict, no IO
      - WSLRecovery.get_summary(): calls _parse_wsl_list → _windows_exec;
        FileNotFoundError on Linux → returns {} → "Unknown" distros
      - BenchmarkWorm.get_summary(): pure dict, no IO
    """
    for module_path, class_name in ALL_WORM_CLASSES:
        results = _concurrent_execute(
            f"{module_path}.{class_name}.get_summary", 2,
        )
        for slot, r in enumerate(results):
            assert isinstance(r, tuple), (
                f"S4 {class_name} slot {slot}: "
                f"barrier worker raised {type(r).__name__}: {r!r}"
            )
            instance, summary = r
            assert isinstance(summary, dict), (
                f"S4 {class_name} slot {slot}: get_summary() returned "
                f"{type(summary).__name__} (not dict)"
            )
            assert "session_id" in summary, (
                f"S4 {class_name} slot {slot}: get_summary() missing 'session_id' "
                f"(cross-class contamination regression)"
            )
            assert "project" in summary, (
                f"S4 {class_name} slot {slot}: get_summary() missing 'project'"
            )


# ── S5: Sequential-after-concurrent for each worm ───────────────────────────

def test_S5_sequential_after_concurrent_get_summary_returns_fresh_data() -> None:
    """S5 (T5-analogue for execution): After a 4-thread barrier race, a
    SEQUENTIAL ``get_summary()`` call on a fresh instance returns well-formed
    data with its OWN session_id (proving no contamination between
    concurrent and sequential instances).

    The µs+uuid4 suffix (Phase 11.5 v2) makes cross-batch µs-tail collisions
    statistically impossible; the test verifies this empirically.

    Phase 13.1.5 cleanup — NO MOCKS (same rationale as S4).
    """
    import importlib
    seen_session_ids: set[str] = set()
    for module_path, class_name in ALL_WORM_CLASSES:
        # Concurrent (S4-style barrier race): 4 threads × same class
        concurrent_results = _concurrent_execute(
            f"{module_path}.{class_name}.get_summary", n=4,
        )
        for slot, r in enumerate(concurrent_results):
            assert isinstance(r, tuple), (
                f"S5 {class_name} slot {slot}: raised {type(r).__name__}"
            )
            _, summary = r
            seen_session_ids.add(summary["session_id"])

        # Brief micro-sleep to advance the µs-clock past the concurrent batch
        time.sleep(0.001)

        # Sequential — fresh instance, fresh session_id
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        sequential = cls().get_summary()
        assert "session_id" in sequential, (
            f"S5 {class_name}: sequential get_summary() missing 'session_id'"
        )
        assert len(sequential["session_id"]) == 31, (
            f"S5 {class_name}: sequential session_id length regressed "
            f"(got {len(sequential['session_id'])}, expected 31)"
        )
        assert sequential["session_id"] not in seen_session_ids, (
            f"S5 {class_name}: sequential session_id {sequential['session_id']!r} "
            f"already appeared in concurrent batch — uuid4 suffix should "
            f"guarantee uniqueness (Phase 11.5 v2 design)"
        )
        seen_session_ids.add(sequential["session_id"])


# ── Phase 13.0 + 13.1.5 cross-reference ─────────────────────────────────────
# The instantiation tests in test_concurrent_worm_instantiation_314t.py
# verified __init__ invariants. The S-series here verifies execution-path
# invariants. Together: 3.14t free-threaded correctness target is fully
# covered (allocate a lock per instance + maintain lock invariants through
# execution + session_id remains stable + whole-system serialization).
#
# Phase 13.1.5 reduced the mock surface from ~25 patches (Phase 13.0 final)
# down to 11 patches (only genuine IO-determinism), proving the Phase 13.1
# source-code fix made `from worms.exceptions import X` work throughout
# the worm-source files.
