# coding: utf-8
"""
test_concurrent_worm_instantiation_314t.py — Phase 11.4 concurrent lock-allocation invariant
==============================================================================================

Verifies that all 5 Vermillion LightFury worms can be instantiated concurrently
under Python 3.14t free-threaded (Py_GIL_DISABLED) without lock-aliasing, deadlock,
or lost thread identity.

Test matrix:
  T1: Each of 5 worms instantiates 10× in parallel (50 constructions total)
  T2: All 50 instances have DISTINCT ``_lock`` object ids (no aliasing)
  T3: All 50 worker threads have DISTINCT ``threading.get_ident()`` ids
  T4: Total wall-clock for 50 concurrent instantiations < 2 s (deadlock detector)
  T5: Sequential instantiation AFTER concurrent run succeeds with fresh locks

Free-threaded detection:
  Uses ``sysconfig.get_config_var("Py_GIL_DISABLED") == 1`` as module-level
  pytest.skip() gate so the test is fully skipped on GIL-enabled Python
  builds (where the test would be meaningless). On 3.14t free-threaded
  builds, the GIL is OFF and threads can execute Python code in truly
  parallel — the assertion below that is the regression target.

Stdlib only. No new deps. No fixtures (purposely): a worker closure over
``threading.Barrier`` is the most direct way to enforce simultaneous
__init__ entry across N threads (maximizes race-window collision window).

Cross-reference:
  Phase 10.7: Found #4 closed — counter locks
  Phase 10.8: ThreadSafeWormMixin introduced — allocates self._lock via super().__init__()
  Phase 10.9: C2 closed — BenchmarkWorm brought into mixin pattern
  Phase 11.4: this test — concurrent instantiation invariant under 3.14t
"""

import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# ── Module-level gate: skip on non-free-threaded Python ─────────────────────
# Py_GIL_DISABLED is set at Python build-time (PEP 703). On regular GIL
# builds it is 0 or absent. Either way, skip the whole module.
if sysconfig.get_config_var("Py_GIL_DISABLED") != 1:
    pytest.skip(
        "Phase 11.4 concurrent instantiation test requires Python 3.14t "
        "free-threaded runtime (Py_GIL_DISABLED=1); skipping on this build.",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Constants ────────────────────────────────────────────────────────────────

THREADS_PER_WORM = 10
ALL_WORM_CLASSES = [
    # (module_path, class_name) — import lazily in tests so worms/__init__
    # heavier side-effects (e.g. PAMS recorder instantiation) run inside
    # the pytest worker, not at pytest collection time.
    ("worms.nmap_scanner",      "NmapScanner"),
    ("worms.kali_health",       "KaliHealth"),
    ("worms.metasploit_bridge", "MetasploitBridge"),
    ("worms.wsl_recovery",      "WSLRecovery"),
    ("worms.benchmark_worm",    "BenchmarkWorm"),
]

# 50 concurrent instantiations must complete well under this wall-clock
# budget. 2 s is generous: production code shows each __init__ is microsecond-
# range, even on Kali WSL substrate with PAMS network IO. Exceeding this is
# the deadlock detector (T4).
CONCURRENT_DEADLINE_SEC = 2.0
# Per-thread join timeout — secondary safety against a hung worker.
THREAD_JOIN_TIMEOUT_SEC = 20.0


# ── Helper: barrier-sync N concurrent instantiations of a worm class ─────────

def _concurrent_instantiate(worm_class: type, n: int = THREADS_PER_WORM) -> list[Any]:
    """Construct ``n`` instances of ``worm_class`` in parallel under a
    threading.Barrier so all N threads enter __init__ simultaneously.

    Each instance (or a captured BaseException on failure) is stored at the
    thread's ``slot`` index in the returned list. Callers inspect results
    with ``isinstance(r, worm_class)`` — exceptions surface with their type
    name in the assertion message. Thread join is bounded by
    ``THREAD_JOIN_TIMEOUT_SEC`` so a deadlock cannot hang the test runner.
    """
    import importlib
    # Import lazily so the first test that needs each worm pays the import cost
    # without polluting module-load time of every other test.
    if isinstance(worm_class, str):
        module_path, class_name = worm_class.rsplit(".", 1)
        worm_class = getattr(importlib.import_module(module_path), class_name)

    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n  # type: ignore[list-item]

    def worker(slot: int) -> None:
        barrier.wait(timeout=10)
        try:
            results[slot] = worm_class()
        except BaseException as e:  # noqa: BLE001 — capture for diagnostic
            results[slot] = e

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"worm-{worm_class.__name__}-{i}")
        for i in range(n)
    ]
    deadline = time.perf_counter() + THREAD_JOIN_TIMEOUT_SEC
    for t in threads:
        t.start()
    for t in threads:
        remaining = max(0.1, deadline - time.perf_counter())
        t.join(timeout=remaining)
    return results


# ── T1: Each worm instantiates 10× concurrently without raising ─────────────

@pytest.mark.parametrize(
    "module_path,class_name",
    ALL_WORM_CLASSES,
    ids=[cname for _m, cname in ALL_WORM_CLASSES],
)
def test_T1_worm_instantiates_10x_concurrently(module_path: str, class_name: str) -> None:
    """T1: 10 threads barrier-sync-instantiate ``class_name``. All must succeed."""
    import importlib
    mod = importlib.import_module(module_path)
    worm_class = getattr(mod, class_name)
    results = _concurrent_instantiate(worm_class)
    assert len(results) == THREADS_PER_WORM, (
        f"{class_name}: expected {THREADS_PER_WORM} successful instantiations, got {len(results)}"
    )
    for slot, instance in enumerate(results):
        assert isinstance(instance, worm_class), (
            f"{class_name}[slot {slot}]: wrong type {type(instance).__name__}"
        )


# ── T2: All 50 instances have distinct _lock object identities ───────────────

def test_T2_50_concurrent_instances_have_distinct_lock_ids() -> None:
    """T2: No two of the 50 concurrently-constructed instances share a `_lock`.

    This is the headline invariant: ``ThreadSafeWormMixin.__init__`` must allocate
    a fresh ``threading.Lock()`` per instance, never a class-level shared object.
    Each ``threading.Lock()`` call is guaranteed to return a distinct object;
    a regression where someone refactors to ``cls._shared_lock = Lock()``
    would mean all 10 instances of a given worm share the same lock id.
    """
    import importlib
    all_instances: list[Any] = []
    for module_path, class_name in ALL_WORM_CLASSES:
        mod = importlib.import_module(module_path)
        worm_class = getattr(mod, class_name)
        all_instances.extend(_concurrent_instantiate(worm_class))
    assert len(all_instances) == 5 * THREADS_PER_WORM, (
        f"expected {5 * THREADS_PER_WORM} instances, got {len(all_instances)}"
    )

    # Identity check: id() uniqueness across all 50 _lock objects.
    lock_ids = [id(w._lock) for w in all_instances]
    # Sanity: each instance must have a _lock attribute.
    assert all(getattr(w, "_lock", None) is not None for w in all_instances), (
        "at least one concurrent instance is missing self._lock — "
        "ThreadSafeWormMixin allocation regressed"
    )
    unique_lock_ids = set(lock_ids)
    collision_count = len(lock_ids) - len(unique_lock_ids)
    assert collision_count == 0, (
        f"lock-aliasing detected: {collision_count} of {len(lock_ids)} "
        f"instances share a _lock object (lock IDs are not unique)"
    )


# ── T3: 50 worker threads have distinct threading.get_ident() values ─────────

def test_T3_50_concurrent_threads_have_distinct_identities() -> None:
    """T3: barrier-sync 50 workers, then verify each recorded a unique ``ident``.

    This decouples thread-identity distinctness from worm class instantiation,
    so an issue with the worm's __init__ does not mask an issue with
    ``threading.Barrier`` worker scheduling (or vice versa).
    """
    n = 5 * THREADS_PER_WORM  # 50
    barrier = threading.Barrier(n)
    idents: list[int | None] = [None] * n  # type: ignore[list-item]

    def identity_recorder(slot: int) -> None:
        barrier.wait(timeout=10)
        idents[slot] = threading.get_ident()

    threads = [
        threading.Thread(target=identity_recorder, args=(i,), name=f"ident-{i}")
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(i is not None for i in idents), (
        f"some threads failed to record their ident: {sum(1 for i in idents if i is None)} of {n}"
    )
    unique_idents = set(idents)
    assert len(unique_idents) == n, (
        f"thread ident collision: {n - len(unique_idents)} of {n} threads "
        f"had duplicate idents (id collision would indicate a Python runtime "
        f"threading bug — please report)"
    )


# ── T4: 50 concurrent instantiations complete in < 2 s (deadlock detector) ───

def test_T4_concurrent_instantiation_completes_under_2_seconds() -> None:
    """T4: Wall-clock budget. Exceeding 2 s for 50 instantiations is the deadlock
    or thrashing indicator — lock contention, infinite spin, or a hang.
    """
    import importlib
    start = time.perf_counter()
    for module_path, class_name in ALL_WORM_CLASSES:
        mod = importlib.import_module(module_path)
        worm_class = getattr(mod, class_name)
        _concurrent_instantiate(worm_class)
    elapsed = time.perf_counter() - start
    assert elapsed < CONCURRENT_DEADLINE_SEC, (
        f"50 concurrent instantiations took {elapsed:.2f}s "
        f"(> {CONCURRENT_DEADLINE_SEC:.1f}s indicates deadlock or thrashing)"
    )


# ── T5: Sequential instantiation after the concurrent run still works ────────

def test_T5_sequential_instantiation_after_concurrent_run_still_works() -> None:
    """T5: After the barrier race, sequential instantiation must yield fresh
    ``_lock`` objects (not aliased to anything seen in the race).
    """
    import importlib
    mod = importlib.import_module("worms.kali_health")
    kh_class = mod.KaliHealth

    race_results = _concurrent_instantiate(kh_class)
    race_lock_ids = {id(r._lock) for r in race_results}

    sequential = [kh_class() for _ in range(THREADS_PER_WORM)]
    for slot, instance in enumerate(sequential):
        assert isinstance(instance, kh_class), (
            f"sequential slot {slot}: wrong type {type(instance).__name__}"
        )
        assert getattr(instance, "_lock", None) is not None, (
            f"sequential slot {slot}: missing _lock attribute"
        )
        # Fresh lock — never aliased to anything in the race set
        assert id(instance._lock) not in race_lock_ids, (
            f"sequential slot {slot}: lock id {id(instance._lock)} was used in the race "
            f"(should be fresh — possible aliasing regression)"
        )


# ── T6 (Phase 11.5 v2): per-worm session_id is well-formed AND unique under concurrent run ─

@pytest.mark.parametrize(
    "module_path,class_name",
    ALL_WORM_CLASSES,
    ids=[cname for _m, cname in ALL_WORM_CLASSES],
)
def test_T6_per_worm_session_id_microsecond_plus_uuid_format(module_path: str, class_name: str) -> None:
    """T6: 10/10 distinct ``_session_id`` per worm under barrier-sync.

    Format: ``YYYYMMDD_HHMMSS_ffffff_<8-hex>`` (31 chars; rationale in
    MILESTONE Phase 11.5). uuid hex suffix defeats micro-tick collisions
    under 3.14t free-threaded race.
    """
    import importlib
    import re
    mod = importlib.import_module(module_path)
    worm_class = getattr(mod, class_name)
    results = _concurrent_instantiate(worm_class)
    pattern = re.compile(r"^\d{8}_\d{6}_\d{6}_[0-9a-f]{8}$")  # YYYYMMDD_HHMMSS_ffffff_<8-hex>
    sids: list[str] = []
    for slot, instance in enumerate(results):
        sid = getattr(instance, "_session_id", None)
        assert isinstance(sid, str), (
            f"{class_name}[slot {slot}]: _session_id missing or not a string (got {type(sid).__name__})"
        )
        assert sid, f"{class_name}[slot {slot}]: _session_id is empty"
        assert pattern.match(sid), (
            f"{class_name}[slot {slot}]: _session_id {sid!r} does not match YYYYMMDD_HHMMSS_ffffff_<8-hex>"
        )
        sids.append(sid)
    # Phase 11.6 — uniqueness error message now references v1 collision
    # metric so a future regression hit self-documents the upgrade history.
    unique_sids = set(sids)
    assert len(unique_sids) == THREADS_PER_WORM, (
        f"{class_name}: {THREADS_PER_WORM - len(unique_sids)} of {THREADS_PER_WORM} "
        f"instances share a _session_id under barrier race — uuid4 hex suffix "
        f"should guarantee distinctness (v1 saw 4 collisions at 46/50 with µs-only)"
    )


def test_T6b_combined_50_instances_all_have_distinct_session_ids() -> None:
    """T6b: 50/50 distinct ``_session_id`` across all 5 worms × 10 threads.

    Combined check — even with shared-micro collisions, the uuid suffix
    guarantees per-instance uniqueness across the project.
    """
    import importlib
    all_sids: list[str] = []
    for module_path, class_name in ALL_WORM_CLASSES:
        mod = importlib.import_module(module_path)
        worm_class = getattr(mod, class_name)
        for instance in _concurrent_instantiate(worm_class):
            all_sids.append(instance._session_id)
    assert len(all_sids) == 5 * THREADS_PER_WORM
    unique_all_sids = set(all_sids)
    collision_count = len(all_sids) - len(unique_all_sids)
    assert collision_count == 0, (
        f"50-way _session_id uniqueness violated: {collision_count} collisions "
        f"({len(unique_all_sids)}/{len(all_sids)} unique) — uuid4 hex suffix "
        f"should have produced 50/50 distinct ids (v1 saw 4 collisions at 46/50 with µs-only)"
    )


# ── T9 (Phase 11.6): SharedDirManager session_id is also collision-free under barrier race ─

def test_T9_shared_dir_manager_session_ids_distinct_under_barrier_race() -> None:
    """T9: SharedDirManager also uses µs+UUID8 session_id format (Phase 11.6
    consistency debt closed). 10/10 distinct under barrier-sync.

    SharedDirManager is not a worm (lives in config/, not worms/) so was
    excluded from Phase 11.5's 5-worm scope. Phase 11.6 mirrors the worm
    pattern: µs timestamp + 8-hex-char uuid suffix → 24-char format.

    Reuses `_concurrent_instantiate()` rather than inlining barrier/thread
    logic — SharedDirManager has a no-arg __init__ so it satisfies the
    helper's contract (no candidate else duplicates the 40-LOC barrier pattern).
    """
    import re
    from config.shared_dir import SharedDirManager

    pattern = re.compile(r"^\d{8}_\d{6}_\d{6}_[0-9a-f]{8}$")  # YYYYMMDD_HHMMSS_ffffff_<8-hex>
    results = _concurrent_instantiate(SharedDirManager)
    sids: list[str] = [r._session_id for r in results]
    for slot, sid in enumerate(sids):
        assert isinstance(sid, str), (
            f"SharedDirManager slot {slot}: _session_id is not a string (got {type(sid).__name__})"
        )
        assert sid, f"SharedDirManager slot {slot}: _session_id is empty"
        assert pattern.match(sid), (
            f"SharedDirManager slot {slot}: _session_id {sid!r} does not match YYYYMMDD_HHMMSS_ffffff_<8-hex>"
        )
    unique_sids = set(sids)
    assert len(unique_sids) == THREADS_PER_WORM, (
        f"SharedDirManager: {THREADS_PER_WORM - len(unique_sids)} of {THREADS_PER_WORM} "
        f"instances share a _session_id under barrier race — uuid4 hex suffix "
        f"should guarantee distinctness (v1 saw 4 collisions at 46/50 with µs-only; "
        f"see MILESTONE.md Phase 11.5)"
    )


# ── T10 (Phase 12.1): len==31 regression guard exercised on all 6 classes ───
# The full class matrix includes SharedDirManager (Phase 11.6 closed the
# consistency debt pre-Phase 12.1). The worm __init__ guards (Phase 12.1) and
# SharedDirManager __init__ guard all enforce `len == 31`; T10 is the
# cross-class witness that catches a regression in ALL six at once.

ALL_INIT_GUARDED_CLASSES: list[tuple[str, str, bool]] = [
    # (module_path, class_name, use_lazy_import) — use_lazy_import=True means
    # import inside the test worker (avoids heavier module-load costs at
    # pytest collection time). All 6 classes follow this pattern.
    ("worms.nmap_scanner",      "NmapScanner",      True),
    ("worms.kali_health",       "KaliHealth",       True),
    ("worms.metasploit_bridge", "MetasploitBridge", True),
    ("worms.wsl_recovery",      "WSLRecovery",      True),
    ("worms.benchmark_worm",    "BenchmarkWorm",    True),
    ("config.shared_dir",       "SharedDirManager", True),
]


@pytest.mark.parametrize(
    "module_path,class_name,_use_lazy",
    ALL_INIT_GUARDED_CLASSES,
    ids=[cname for _mp, cname, _u in ALL_INIT_GUARDED_CLASSES],
)
def test_T10_all_six_classes_len_equals_31_and_matches_regex(
    module_path: str, class_name: str, _use_lazy: bool,
) -> None:
    """T10: All 6 classes with the Phase 12.1 __init__ len==31 regression guard
    satisfy the invariant at instantiation time plus the YYYYMMDD_HHMMSS_ffffff_<8-hex>
    regex match. A regression in any one is caught by ONE failing test."""
    import importlib
    import re
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    pattern = re.compile(r"^\d{8}_\d{6}_\d{6}_[0-9a-f]{8}$")  # YYYYMMDD_HHMMSS_ffffff_<8-hex>

    sid = cls()._session_id

    assert isinstance(sid, str), (
        f"{class_name}: _session_id is not a string (got {type(sid).__name__})"
    )
    assert len(sid) == 31, (
        f"{class_name}: _session_id {sid!r} has length {len(sid)} \u2014 expected 31. "
        f"Phase 12.1 regression guard fired (would have failed at __init__ in dev mode; "
        f"may be stripped under python -O / Nuitka .exe production builds \u2014 see MILESTONE Phase 12.1). "
        f"Format spec: YYYYMMDD_HHMMSS_ffffff_<8-hex>"
    )
    assert pattern.match(sid), (
        f"{class_name}: _session_id {sid!r} does not match YYYYMMDD_HHMMSS_ffffff_<8-hex>"
    )


def test_T10b_combined_pattern_guard_matches_all_6() -> None:
    """T10b: Aggregate check across all 6 classes simultaneously.
    Single failing class surfaces the regression in one test, not six.
    """
    import importlib
    import re
    pattern = re.compile(r"^\d{8}_\d{6}_\d{6}_[0-9a-f]{8}$")

    bad: list[str] = []
    for module_path, class_name, _ in ALL_INIT_GUARDED_CLASSES:
        try:
            cls = getattr(importlib.import_module(module_path), class_name)
            sid = cls()._session_id
            if not isinstance(sid, str) or len(sid) != 31 or not pattern.match(sid):
                bad.append(f"{class_name}: len={len(sid) if isinstance(sid, str) else '?'} sid={sid!r}")
        except BaseException as e:  # noqa: BLE001
            bad.append(f"{class_name}: instantiation raised {type(e).__name__}: {e}")

    assert not bad, (
        f"Phase 12.1 len==31 regression guard violated across classes: "
        f"{bad} \u2014 see MILESTONE Phase 12.1 for the rationale + format spec."
    )
