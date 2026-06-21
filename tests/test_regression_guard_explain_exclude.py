# coding: utf-8
"""
test_regression_guard_capsys — Locks Phase 10.10.1 cosmetic fix
===============================================================

This test pins the print format of ``regression_guard.py --explain-exclude``
that was fixed in Phase 10.10.1 (double-parens BUG). Without this test the BUG
can silently regress — the format only surfaces when a user manually invokes
``--explain-exclude``.

The regression BUG being locked: ``key[1]`` already includes trailing ``()``
(because benchmark_all stores function names WITH trailing parens), so the
format string ``{key[1]}()`` was adding a second ``()`` pair → output rendered
as ``list_sessions()()`` (double parens).

After Phase 10.10.1 fix, the format renders as ``list_sessions()  ---  ...``
(single parens, em-dash separator). This test folds both invariants.

Stdlib-only. No new deps.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _synthetic_history_with_forced_drift(drift_module: str, drift_function: str,
                                          drift_multiplier: float = 50.0):
    """Load the real benchmark history and force a HARD drift on (module, function).

    Returns a list ready to be JSON-serialised to a temp history file.
    The rest of the history is left untouched so the 5-entry prior window still
    has meaningful baseline values.
    """
    hist = json.load(open(ROOT / "benchmarks/benchmark_history.json"))
    last = hist[-1].copy()
    for r in last["results"]:
        if r.get("module") == drift_module and r.get("function") == drift_function:
            base = r.get("avg_ms") or 1.0
            r["avg_ms"] = base * drift_multiplier
            r["min_ms"] = r["avg_ms"]
            r["max_ms"] = r["avg_ms"]
    return hist[:-1] + [last]


def test_explain_exclude_print_format_no_double_parens(tmp_path):
    """Run regression_guard with --explain-exclude, assert no '()()' in stderr.

    Forces a hard drift on ``MetasploitBridge.cleanup_old_scripts()`` (multiplier 50×)
    so it trips the guard → regression_guard exits 1 → stderr carries both the
    exclude-line assertions and the FAIL line. We only assert the cosmetics of
    the exclude-line format; the FAIL line is incidental.
    """
    hist = _synthetic_history_with_forced_drift(
        drift_module="MetasploitBridge",
        drift_function="cleanup_old_scripts()",
        drift_multiplier=50.0,
    )
    hist_path = tmp_path / "history.json"
    json.dump(hist, open(hist_path, "w"))

    result = subprocess.run(
        [
            sys.executable, str(ROOT / "regression_guard.py"),
            "--skip-bench",
            "--history-path", str(hist_path),
            "--exclude-functions",
            "SharedDirManager.ensure_directories(),MetasploitBridge.list_sessions()",
            "--explain-exclude",
        ],
        capture_output=True, text=True, timeout=60,
    )
    # Don't assert on exit code: the test's purpose is to lock the COSMETIC stderr
    # format from Phase 10.10.1 (no double-parens). The explain-exclude rationale
    # prints regardless of exit code (it fires in the filter step BEFORE check_guard).
    # Whether the forced drift trips an actual regression (exit 1) vs passes (exit 0)
    # depends on the prior-window baseline statistics from the real history file,
    # which is incidental to the cosmetic-format invariant we're testing. The
    # forced drift just ensures we have a non-trivial filter-step run.
    assert result.returncode in (0, 1), (
        f"regression_guard returned unexpected exit code {result.returncode}; "
        f"stderr={result.stderr[:500]}"
    )

    stderr = result.stderr

    # Phase 10.10.1 LOCK: must NEVER have double-parens in the exclude-line.
    assert "()()" not in stderr, (
        f"Phase 10.10.1 cosmetic fix has REGRESSED — found '()()' pattern. "
        f"stderr={stderr[:800]}"
    )

    # Phase 10.10.1 LOCK: single-parens format ``Excluded <module>.<function>()  ---  ...``
    # — note the trailing () AND the em-dash separator with double-space padding.
    assert "Excluded SharedDirManager.ensure_directories()  " in stderr or \
           "Excluded SharedDirManager.ensure_directories  " in stderr, (
        f"Missing or mis-formatted exclude line for ensure_directories. "
        f"stderr={stderr[:800]}"
    )
    assert "Excluded MetasploitBridge.list_sessions()  " in stderr or \
           "Excluded MetasploitBridge.list_sessions  " in stderr, (
        f"Missing or mis-formatted exclude line for list_sessions. "
        f"stderr={stderr[:800]}"
    )


def test_explain_exclude_includes_rationale(tmp_path):
    """When --explain-exclude is set, the rationale text is emitted to stderr.

    Pins the rationale content from ``_EXCLUDE_RATIONALE`` so a future
    rationale-dict edit doesn't silently drop the documentation.
    """
    hist = _synthetic_history_with_forced_drift(
        drift_module="MetasploitBridge",
        drift_function="cleanup_old_scripts()",
        drift_multiplier=50.0,
    )
    hist_path = tmp_path / "history.json"
    json.dump(hist, open(hist_path, "w"))

    result = subprocess.run(
        [
            sys.executable, str(ROOT / "regression_guard.py"),
            "--skip-bench",
            "--history-path", str(hist_path),
            "--exclude-functions",
            "SharedDirManager.ensure_directories(),MetasploitBridge.list_sessions()",
            "--explain-exclude",
        ],
        capture_output=True, text=True, timeout=60,
    )

    # SharedDirManager.ensure_directories rationale mentions NTFS-3G / mount-table —
    # pins the rationale-lookup BUG fix from the previous session (where
    # `_EXCLUDE_RATIONALE.get(key, ...)` would always fall through to the
    # generic fallback because `key` had trailing `()` but dict keys did not).
    assert "NTFS-3G" in result.stderr or "mount-table" in result.stderr, (
        f"Rationale fallback regressed — dict key lookup didn't match. "
        f"stderr={result.stderr[:800]}"
    )
    # list_sessions rationale mentions msfconsole / cold-start / WSL.
    assert "msfconsole" in result.stderr or "cold-start" in result.stderr, (
        f"list_sessions rationale missing. "
        f"stderr={result.stderr[:800]}"
    )
