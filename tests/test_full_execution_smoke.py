# coding: utf-8
"""
test_full_execution_smoke — Locks Phase 10.10.2 FULL EXECUTION TEST matrix
==========================================================================

This suite captures the 10 smoke probes that were hand-piped in the Phase 10.10.2
FULL EXECUTION TEST (a 6-phase comprehensive shell session). Moving them into
pytest means future sessions can validate production correctness with::

    python3 -m pytest tests/test_full_execution_smoke.py -v

instead of bespoke shell-piping of probes.

Each ``test_FN_*`` validates a single guarantee about production code:
construct-ability, API shape, or CLI affordance presence. They are NOT exhaustive
integration tests — they are the cheap, fast probes that confirm the public
contract is still satisfied.

Stdlib-only. No new deps.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT = 60


# ── F1: NmapScanner ──────────────────────────────────────────────────────────

def test_F1_nmap_scanner_constructs_and_allocates_lock():
    """NmapScanner constructs + allocates ``self._lock`` via mixin chain."""
    sys.path.insert(0, str(ROOT))
    from worms.nmap_scanner import NmapScanner

    ns = NmapScanner()
    assert hasattr(ns, "_lock"), "NmapScanner must allocate _lock via ThreadSafeWormMixin"
    # get_summary() (if present) returns a dict — public API contract.
    summary = ns.get_summary() if hasattr(ns, "get_summary") else {}
    assert isinstance(summary, dict), "get_summary must return dict"


# ── F2: KaliHealth ────────────────────────────────────────────────────────────

def test_F2_kali_health_constructs_and_allocates_lock():
    """KaliHealth constructs + allocates ``self._lock``."""
    sys.path.insert(0, str(ROOT))
    from worms.kali_health import KaliHealth

    kh = KaliHealth()
    assert hasattr(kh, "_lock"), "KaliHealth must allocate _lock via ThreadSafeWormMixin"


# ── F3: MetasploitBridge ──────────────────────────────────────────────────────

def test_F3_metasploit_bridge_constructs_and_allocates_lock():
    """MetasploitBridge constructs + allocates ``self._lock``."""
    sys.path.insert(0, str(ROOT))
    from worms.metasploit_bridge import MetasploitBridge

    mb = MetasploitBridge()
    assert hasattr(mb, "_lock"), "MetasploitBridge must allocate _lock"


# ── F4: WSLRecovery ───────────────────────────────────────────────────────────

def test_F4_wsl_recovery_constructs_and_allocates_lock():
    """WSLRecovery constructs + allocates ``self._lock``."""
    sys.path.insert(0, str(ROOT))
    from worms.wsl_recovery import WSLRecovery

    wr = WSLRecovery()
    assert hasattr(wr, "_lock"), "WSLRecovery must allocate _lock"


# ── F5: BenchmarkWorm ─────────────────────────────────────────────────────────

def test_F5_benchmark_worm_constructs_and_allocates_lock():
    """BenchmarkWorm constructs + allocates ``self._lock``."""
    sys.path.insert(0, str(ROOT))
    from worms.benchmark_worm import BenchmarkWorm

    bw = BenchmarkWorm()
    assert hasattr(bw, "_lock"), "BenchmarkWorm must allocate _lock (Phase 10.9 C2 fix)"
    assert hasattr(bw, "_total_runs"), "BenchmarkWorm must have _total_runs counter"


# ── F6: ThreadSafeWormMixin ───────────────────────────────────────────────────

def test_F6_thread_safe_worm_mixin_allocates_lock_via_super():
    """``super().__init__()`` in mixin allocates ``self._lock`` for subclasses."""
    sys.path.insert(0, str(ROOT))
    from worms.mixins import ThreadSafeWormMixin

    class _TestM(ThreadSafeWormMixin):
        def __init__(self):
            super().__init__()

    tm = _TestM()
    assert hasattr(tm, "_lock"), "ThreadSafeWormMixin must allocate _lock via super().__init__()"


# ── F7: config.shared_dir public API ──────────────────────────────────────────

def test_F7_purge_stale_rc_scripts_handles_nonexistent_log_dir():
    """``purge_stale_rc_scripts`` on a nonexistent log_dir returns 0 (int)."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import purge_stale_rc_scripts

    purged = purge_stale_rc_scripts(
        log_dir="/tmp/this_dir_should_not_exist_4smoke_xyz",
        max_age_hours=24,
        throttle_seconds=0,
    )
    assert isinstance(purged, int), f"purge must return int, got {type(purged).__name__}"
    assert purged == 0, f"purge on nonexistent dir must return 0, got {purged}"


# ── F8: SharedDirManager.ensure_directories return shape ──────────────────────

def test_F8_shared_dir_manager_ensure_directories_returns_dict_with_int_counts():
    """``ensure_directories`` returns a dict with int counts (created/existing/failed) + root_ok flag."""
    sys.path.insert(0, str(ROOT))
    from config.shared_dir import SharedDirManager

    sdm = SharedDirManager()
    result = sdm.ensure_directories()
    assert isinstance(result, dict), f"ensure_directories must return dict, got {type(result).__name__}"

    # Phase 10.10 fix: returns int counts (len() of the underlying lists), not lists themselves.
    for key in ("created", "existing", "failed"):
        assert key in result, f"ensure_directories missing key {key!r}"
        assert isinstance(result[key], int), (
            f"ensure_directories[{key!r}] must be int count, got "
            f"type={type(result[key]).__name__}, value={result[key]!r}"
        )
    assert "root_ok" in result, "ensure_directories must include root_ok boolean"
    # All 9 SUBDIRS exist on a fresh setup — created should be 0 in steady-state.
    assert result["existing"] >= 9, f"expect >= 9 existing subdirs, got {result['existing']}"
    assert result["failed"] == 0, "fresh setup should have 0 failed subdirs"
    assert result["root_ok"] is True, f"root_ok must be True on successful setup, got {result['root_ok']}"


# ── F9: F11 regex catches the canonical bug shape ─────────────────────────────

def test_F9_f11_regex_catches_canonical_bug_rejects_safe_code():
    """``has_f11_anti_pattern`` catches the F11 canonical bug, rejects safe f-strings."""
    sys.path.insert(0, str(ROOT))
    from config.f11_patterns import has_f11_anti_pattern

    # The F11 canonical bug: ``f"{var}\\\\subdir\\\\{var2}"`` — f-string interpolation
    # with two-backslash segments between them.
    canonical_bug = 'f"{SHARED_DIR}\\\\metasploit_logs\\\\{filename}"'
    safe_fstring = 'f"hello {x}"'
    safe_fstring_subscript = 'f"results:\\n{x}"'  # single backslash + literal next char

    assert has_f11_anti_pattern(canonical_bug), (
        f"F11 regex failed to catch canonical bug shape: {canonical_bug!r}"
    )
    assert not has_f11_anti_pattern(safe_fstring), (
        f"F11 regex false-positive on safe f-string: {safe_fstring!r}"
    )
    assert not has_f11_anti_pattern(safe_fstring_subscript), (
        f"F11 regex false-positive on safe f-string: {safe_fstring_subscript!r}"
    )


# ── F10: regression_guard CLI affordances ─────────────────────────────────────

def test_F10_regression_guard_cli_has_exclude_functions_and_explain_exclude_flags():
    """``regression_guard --help`` exposes ``--exclude-functions`` + ``--explain-exclude``."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "regression_guard.py"), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert "--exclude-functions" in result.stdout, (
        f"Missing --exclude-functions flag in --help output:\n{result.stdout}"
    )
    assert "--explain-exclude" in result.stdout, (
        f"Missing --explain-exclude flag in --help output:\n{result.stdout}"
    )


# ── F11: benchmark_all output shape ───────────────────────────────────────────

@pytest.mark.slow
def test_F11_benchmark_all_history_appends_valid_entry(tmp_path):
    """``benchmark_all`` produces a valid benchmark_history entry on append-history run."""
    history_path = ROOT / "benchmarks/benchmark_history.json"
    pre_len = len(json.load(open(history_path)))

    result = subprocess.run(
        [sys.executable, str(ROOT / "benchmark_all.py"), "--append-history"],
        capture_output=True, text=True, timeout=600,
    )
    # 33/33 records expected; tolerate exit 0 as success.
    assert result.returncode == 0, (
        f"benchmark_all --append-history failed: exit={result.returncode}\n"
        f"stderr={result.stderr[:500]}"
    )

    post_len = len(json.load(open(history_path)))
    assert post_len == pre_len + 1, (
        f"benchmark_all should append 1 entry, got delta={post_len - pre_len}"
    )


# ── F12: shared imports via worms.__init__ ────────────────────────────────────

def test_F12_worms_init_reexports_six_public_names():
    """``worms.__init__`` re-exports all 6 public names."""
    sys.path.insert(0, str(ROOT))
    import worms

    expected = {
        "NmapScanner", "KaliHealth", "MetasploitBridge", "WSLRecovery",
        "BenchmarkWorm", "ThreadSafeWormMixin",
    }
    actual = set(worms.__all__)
    assert actual == expected, f"worms.__all__ mismatch: expected={expected}, actual={actual}"


# ── F13: exceptions module exports the canonical class ────────────────────────

def test_F13_exceptions_module_exports_kali_unresponsive_error():
    """``exceptions.KaliUnresponsiveError`` importable + instantiable."""
    sys.path.insert(0, str(ROOT))
    from worms.exceptions import KaliUnresponsiveError

    e = KaliUnresponsiveError("test message")
    assert isinstance(e, Exception)
    assert str(e) == "test message"
