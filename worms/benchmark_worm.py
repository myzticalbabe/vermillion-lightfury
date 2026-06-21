# coding: utf-8
"""
BenchmarkWorm — Weekly Function Benchmark Runner
=================================================
Runs benchmark_all.py --append-history on a weekly cadence.
Records performance trends to benchmark_history.json and
regenerates the HTML dashboard.

Registered with the Azazael worm registry so SpawnDiversityEngine
can auto-schedule it alongside other VF monitor/telemetry worms.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
import uuid  # Phase 11.5 — collision-free session_id suffix under 3.14t barrier race
from pathlib import Path
from typing import Any, Dict

# ── Path Resolution ──────────────────────────────────────────────────────────
_VF_ROOT = Path(__file__).resolve().parent.parent  # vermillion_lightfury/
_AZAZAEL_SRC = str(_VF_ROOT.parent / "azazael" / "src")
if _AZAZAEL_SRC not in sys.path:
    sys.path.insert(0, _AZAZAEL_SRC)

# ── Worm Registry ───────────────────────────────────────────────────────────
try:
    from CORE.worm_registry import register_worm, BaseWormMixin
    _REGISTRY = True
except ImportError:
    _REGISTRY = False
    class BaseWormMixin:
        VERSION = "0.0.0"
    def register_worm(category='', priority=0, cooldown_seconds=0.0, description='', tags=None):
        return lambda cls: cls

# ── Shared Mixins ───────────────────────────────────────────────────────────
from worms.mixins import ThreadSafeWormMixin


@register_worm(
    category="telemetry",
    priority=5,
    cooldown_seconds=604800,  # 1 week
    description="Weekly function benchmark runner — 33 functions across 5 VF modules, appends to time-series history and regenerates dashboard",
)
class BenchmarkWorm(ThreadSafeWormMixin, BaseWormMixin):
    """Runs benchmark_all.py --append-history on a weekly schedule.

    Collects execution timing data for all 33 public functions across
    WSLRecovery, KaliHealth, NmapScanner, MetasploitBridge, and
    SharedDirManager.  Appends results to benchmark_history.json (capped
    at 100 entries) and regenerates the Chart.js dashboard.
    """

    VERSION = "1.0.0"
    __summary_method__ = "get_summary"

    HISTORY_PATH = _VF_ROOT / "benchmarks/benchmark_history.json"
    HISTORY_MAX_ENTRIES = 100   # warn at/above this count
    HISTORY_MAX_SIZE_MB = 1.0   # warn at/above this file size

    BENCHMARK_SCRIPT = str(_VF_ROOT / "benchmark_all.py")

    def __init__(self):
        super().__init__()  # ThreadSafeWormMixin → self._lock = threading.Lock(); BaseWormMixin (no-op)
        self._last_run: datetime | None = None
        self._last_results: Dict[str, Any] = {}
        self._total_runs: int = 0
        # Phase 11.5 — µs+uuid8 hex for collision-free 3.14t barrier-sync ids.
        # Format: YYYYMMDD_HHMMSS_ffffff_<8-hex> (31 chars; uuid suffix defeats
        # clock_gettime-micros-tick collisions under free-threaded race).
        self._session_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        # Phase 12.1 — len==31 regression guard. See init in nmap_scanner for rationale.
        assert len(self._session_id) == 31, (
            f"_session_id {self._session_id!r} is not 31 chars "
            f"(format regressed — see MILESTONE Phase 11.5 v2 / Phase 12.1)"
        )
        self._history_alert: Dict[str, Any] | None = None
        with self._lock:
            self._load_history_stats()
            self._check_history_size()

    # ── Worm execute() ──────────────────────────────────────────────────────
    def execute(self) -> dict[str, Any]:
        """Standard worm execution entry point.

        Runs benchmark_all.py --append-history via subprocess.
        Returns structured status dict with pass/fail counts and timings.
        """
        if not os.path.exists(self.BENCHMARK_SCRIPT):
            return {
                "success": False,
                "error": f"Benchmark script not found: {self.BENCHMARK_SCRIPT}",
                "worm": "BenchmarkWorm",
                "version": self.VERSION,
            }

        start = time.perf_counter()
        try:
            result = subprocess.run(
                [sys.executable, self.BENCHMARK_SCRIPT, "--append-history"],
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes — WSL calls can be slow
                cwd=str(_VF_ROOT),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            elapsed = time.perf_counter() - start
            self._last_run = datetime.now(timezone.utc)
            with self._lock:
                self._total_runs += 1

            # Parse output for pass/fail counts
            passed = 0
            failed = 0
            for line in result.stdout.splitlines():
                if "Passed:" in line and "Failed:" in line:
                    parts = line.replace("|", " ").split()
                    for i, p in enumerate(parts):
                        if p == "Passed:" and i + 1 < len(parts):
                            try: passed = int(parts[i + 1])
                            except ValueError: pass
                        if p == "Failed:" and i + 1 < len(parts):
                            try: failed = int(parts[i + 1])
                            except ValueError: pass

            self._last_results = {
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "passed": passed,
                "failed": failed,
                "elapsed_sec": round(elapsed, 2),
                "stdout_tail": result.stdout.strip()[-500:] if result.stdout else "",
                "stderr_tail": result.stderr.strip()[-200:] if result.stderr else "",
            }

            # Check history size and record all to PAMS
            self._check_history_size()
            self._maybe_record_pams(result.returncode == 0, passed, failed, elapsed)

            return {
                "success": result.returncode == 0,
                "data": self._last_results,
                "worm": "BenchmarkWorm",
                "version": self.VERSION,
                "total_runs": self._total_runs,
            }

        except subprocess.TimeoutExpired:
            elapsed = time.perf_counter() - start
            return {
                "success": False,
                "error": f"Benchmark timed out after {elapsed:.0f}s",
                "worm": "BenchmarkWorm",
                "version": self.VERSION,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)[:200],
                "worm": "BenchmarkWorm",
                "version": self.VERSION,
            }

    # ── PAMS Recording ─────────────────────────────────────────────────────
    def _maybe_record_pams(self, ok: bool, passed: int, failed: int, elapsed: float):
        """Record execution telemetry to PAMS if available."""
        try:
            from CORE.PamsRecorder import PamsRecorder
            recorder = PamsRecorder()
            recorder.record_event(
                source="BenchmarkWorm",
                event_type="benchmark_run",
                data={
                    "success": ok,
                    "passed": passed,
                    "failed": failed,
                    "elapsed_sec": round(elapsed, 2),
                    "total_runs": self._total_runs,
                },
            )
        except Exception:
            pass  # PAMS is optional — never crash on recording failure

    # ── History Stats ──────────────────────────────────────────────────────
    def _load_history_stats(self):
        """Load run count from existing benchmark_history.json.

        Uses line-based counting for efficiency — counts JSON array entries
        via newline-delimited 'benchmark_started' keys without loading
        the full JSON into memory (~60x faster, ~0.1ms vs ~6ms for 5 entries).

        NOTE: caller MUST already hold ``self._lock``. Do not acquire the
        lock inside this method — nested acquisition would deadlock.
        """
        if self.HISTORY_PATH.exists():
            try:
                # Count entries without loading full JSON — scan for 
                # lines containing '"benchmark_started"' which appears
                # once per history entry
                count = 0
                with open(self.HISTORY_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        if '"benchmark_started"' in line:
                            count += 1
                self._total_runs = count
            except (OSError, IOError):
                self._total_runs = 0

    def _check_history_size(self):
        """Check benchmark_history.json size and surface alerts via PAMS.

        Warns when the file exceeds HISTORY_MAX_SIZE_MB (1 MB) or
        HISTORY_MAX_ENTRIES (100 entries).  Stores the alert on the
        instance so get_summary() can surface it.

        NOTE: caller MUST already hold ``self._lock``. Do not acquire the
        lock inside this method (or its callees like _maybe_record_pams_alert)
        — nested acquisition would deadlock.
        """
        if not self.HISTORY_PATH.exists():
            self._history_alert = None
            return

        alerts = []
        try:
            size_mb = self.HISTORY_PATH.stat().st_size / (1024 * 1024)
            if size_mb >= self.HISTORY_MAX_SIZE_MB:
                alerts.append(f"History file size {size_mb:.1f} MB exceeds {self.HISTORY_MAX_SIZE_MB} MB limit")

            with open(self.HISTORY_PATH, "r") as f:
                history = json.load(f)
            entries = len(history)
            if entries >= self.HISTORY_MAX_ENTRIES:
                alerts.append(f"History entries {entries} at/above {self.HISTORY_MAX_ENTRIES} cap")

            if alerts:
                self._history_alert = {
                    "severity": "warning",
                    "size_mb": round(size_mb, 2),
                    "entries": entries,
                    "alerts": alerts,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                self._maybe_record_pams_alert(alerts, size_mb, entries)
            else:
                self._history_alert = None
        except (json.JSONDecodeError, IOError, OSError) as e:
            self._history_alert = {
                "severity": "error",
                "error": f"Could not check history file: {e}",
            }

    def _maybe_record_pams_alert(self, alerts, size_mb, entries):
        """Record a history-size alert to PAMS."""
        try:
            from CORE.PamsRecorder import PamsRecorder
            recorder = PamsRecorder()
            recorder.record_event(
                source="BenchmarkWorm",
                event_type="history_size_alert",
                data={
                    "severity": "warning",
                    "size_mb": round(size_mb, 2),
                    "entries": entries,
                    "alerts": alerts,
                },
            )
        except Exception:
            pass

    # ── Summary ────────────────────────────────────────────────────────────
    def get_summary(self) -> dict[str, Any]:
        """Return summary suitable for worm registry display."""
        base = {
            "status": "ok",
            "worm": "BenchmarkWorm",
            "version": self.VERSION,
            "project": "Vermillion LightFury",
            "total_runs": self._total_runs,
            "session_id": self._session_id,
        }
        if self._last_run:
            base["last_run"] = self._last_run.isoformat()
        if self._last_results:
            base["last_results"] = self._last_results
        if self._history_alert:
            base["history_alert"] = self._history_alert
        return base


# ── CLI Entry Point ──────────────────────────────────────────────────────────
# ── CLI Entry Point ──────────────────────────────────────────────────────────
def main():
    """Phase 13.7: argparse-based CLI with required subparsers.

    Replaces the legacy __main__ block which silently ignored sys.argv.
    `python -m worms.benchmark_worm` (no args) still prints the banner via
    the len() fallback; unknown/invalid subcommands now exit 2 cleanly.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="BenchmarkWorm — Weekly Benchmark Runner (Vermillion LightFury)",
    )
    sub = parser.add_subparsers(dest="command", required=True, help="Command")

    run_p = sub.add_parser("run", help="Run benchmark_all.py --append-history")

    sub.add_parser("summary", help="Print worm summary")

    # No-args UX: preserve the banner-printing behavior the existing F1/F5/F7
    # tests rely on. argparse required=True fires for missing/invalid subcommand.
    if len(sys.argv) == 1:
        bw = BenchmarkWorm()
        print(f"BenchmarkWorm v{bw.VERSION} — Vermillion LightFury")
        print(f"  Script: {bw.BENCHMARK_SCRIPT}")
        print(f"  Total runs tracked: {bw._total_runs}")
        print(f"  Cooldown: 604800s (1 week)")
        return

    args = parser.parse_args()
    bw = BenchmarkWorm()
    if args.command == "run":
        result = bw.execute()
        print(json.dumps(result, default=str, indent=2))
    elif args.command == "summary":
        print(json.dumps(bw.get_summary(), default=str, indent=2))
    # No `else:` branch: required=True guarantees args.command is valid.


if __name__ == "__main__":
    main()
