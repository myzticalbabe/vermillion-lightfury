#!/usr/bin/env python3
"""Vermillion LightFury — Performance Regression Guard (lightweight, stdlib-only)

Usage in CI:
    python3 regression_guard.py --window 5 --regress-pct 25 --min-prior-ms 5

Workflow:
  1. Run `python3 benchmark_all.py --append-history` (unless --skip-bench).
  2. Read benchmark_history.json (path configurable via --history-path).
  3. For each (module, function) tuple:
       - latest   = most recent entry with avg_ms != null and error == null
       - baseline = mean of the prior WINDOW entries (also valid only)
       - if baseline > MIN_PRIOR_MS and (latest - baseline) / baseline * 100 > REGRESS_PCT
         -> trip the guard for this function
  4. Exit 0 on pass, exit 1 on guard trip, exit 2 on operational error (bench failed or
     history file missing).

This guard intentionally ignores:
  - Functions with prior-mean <= MIN_PRIOR_MS (sub-millisecond jitter dominates % drift)
  - Functions whose latest or any prior entry in window is null / errored (broken chain)
  - Functions with fewer than (WINDOW + 1) valid entries (insufficient baseline)

No test-framework dependency: argparse + json + statistics + subprocess + sys + pathlib only.
"""
import argparse
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_BENCH = ROOT / "benchmark_all.py"
DEFAULT_HISTORY = ROOT / "benchmarks/benchmark_history.json"


def parse_args():
    p = argparse.ArgumentParser(description="Vermillion LightFury regression guard")
    p.add_argument("--window", type=int, default=5,
                   help="Prior entries to baseline (default: 5)")
    p.add_argument("--regress-pct", type=float, default=25.0,
                   help="Regression drift percent threshold (default: 25.0)")
    p.add_argument("--min-prior-ms", type=float, default=5.0,
                   help="Only flag functions whose prior mean exceeds this (default: 5.0 ms)")
    p.add_argument("--skip-bench", action="store_true",
                   help="Skip running benchmark_all.py (use existing history)")
    p.add_argument("--bench-args", default="--append-history",
                   help="Args passed to benchmark_all.py (default: --append-history)")
    p.add_argument("--history-path", default=str(DEFAULT_HISTORY),
                   help=f"History JSON path (default: {DEFAULT_HISTORY})")
    p.add_argument("--bench-script", default=str(DEFAULT_BENCH),
                   help=f"benchmark_all.py path (default: {DEFAULT_BENCH})")
    p.add_argument("--exclude-functions", default="",
                   help="Comma-separated Module.function() pairs to exclude from drift check "
                        "(e.g. 'SharedDirManager.ensure_directories(),MetasploitBridge.list_sessions()'). "
                        "Useful for known-environmental-variance functions; default=empty (strict gate).")
    p.add_argument("--explain-exclude", action="store_true",
                   help="Print documentation rationale for --exclude-functions entries when used.")
    return p.parse_args()


def run_benchmark(args):
    """Subprocess-run benchmark_all.py. Exits 2 on failure."""
    if args.skip_bench:
        print("[regression_guard] --skip-bench set; not running benchmark_all.py")
        return
    cmd = [sys.executable, args.bench_script] + args.bench_args.split()
    print(f"[regression_guard] Running: {' '.join(cmd)}")
    # Timeout: matching BenchmarkWorm.execute() (600s) — WSL benchmark_all runs
    # are slow; without a timeout a hung subprocess pins the CI job forever.
    result = subprocess.run(cmd, cwd=ROOT, check=False, timeout=600)
    if result.returncode != 0:
        print(
            f"[regression_guard] benchmark_all.py failed (exit {result.returncode})",
            file=sys.stderr,
        )
        sys.exit(2)


def load_history(path):
    p = Path(path)
    if not p.exists():
        print(f"[regression_guard] History file not found: {p}", file=sys.stderr)
        sys.exit(2)
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _parse_exclude_functions(exclude_string: str) -> set:
    """Parse comma-separated 'Module.function()' strings into a set of (module, function) tuples.

    Empty or absent input → empty set. Silent on malformed items.
    """
    excl = set()
    if not exclude_string:
        return excl
    for raw in exclude_string.split(","):
        item = raw.strip().removesuffix("()").strip()
        if not item or "." not in item:
            continue
        parts = item.split(".", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            excl.add((parts[0], parts[1]))
    return excl


# Phase 10.10 — rationale printed when --explain-exclude is passed.
_EXCLUDE_RATIONALE: dict = {
    ("SharedDirManager", "ensure_directories"): (
        "Bounded O(n) syscall walk (9 subdirs + README write) against /mnt/c NTFS-3G. "
        "Steady-state variance tracks background mount-table activity; not a code regression."
    ),
    ("MetasploitBridge", "list_sessions"): (
        "Subprocess spawn of `msfconsole -q -x 'sessions -l; exit'` via WSL. "
        "Cold-start msfconsole variance dominates single-run timing (now smoothed with warmup=2)."
    ),
    ("WSLRecovery", "check_all"): (
        "Iterates MONITORED_DISTROS (2 distros), spawning 1-4 subprocess round-trips per "
        "check_all() — each `wsl.exe --list --verbose` + conditional `bash -c 'echo OK'` "
        "goes through the Linux-to-Windows / NTFS-3G boundary. "
        "Steady-state cost tracks WSL distro-status latency, not application regression."
    ),
}


def aggregate_per_function(history):
    """Aggregate per (module, function) across all history entries, preserving order.

    Returns dict: {(module, function): [(ts, avg_ms, error), ...]}
    Only entries with avg_ms != null AND error == null are kept (sound comparisons).
    """
    agg = defaultdict(list)
    for entry in history:
        ts = entry.get("benchmark_completed", "")
        for r in entry.get("results", []):
            key = (r.get("module"), r.get("function"))
            if r.get("avg_ms") is None or r.get("error") is not None:
                continue
            agg[key].append((ts, r.get("avg_ms")))
    return agg


def check_guard(per_func, window, regress_pct, min_prior_ms):
    """Return list of failures: [(module, function, latest_ms, prior_mean_ms, drift_pct, n_prior_used)]"""
    failures = []
    for (mod, fn), series in per_func.items():
        if len(series) < window + 1:
            # Need at least WINDOW prior + 1 latest
            continue
        latest_ts, latest_ms = series[-1]
        prior = series[-(window + 1):-1]
        if len(prior) == 0:
            continue
        prior_values = [ms for _ts, ms in prior]
        prior_mean = statistics.mean(prior_values)
        if prior_mean <= min_prior_ms:
            continue  # sub-ms jitter dominates % drift; skip
        drift_pct = (latest_ms - prior_mean) / prior_mean * 100
        if drift_pct > regress_pct:
            failures.append((
                mod, fn, latest_ms, prior_mean, drift_pct, len(prior_values),
            ))
    return failures


def main():
    args = parse_args()
    run_benchmark(args)  # exits 2 on bench failure (also exits 2 on subprocess timeout — see run_benchmark)

    history = load_history(args.history_path)
    print(
        f"[regression_guard] Loaded {len(history)} history entries from {args.history_path}"
    )

    per_func = aggregate_per_function(history)
    print(
        f"[regression_guard] Tracking {len(per_func)} (module, function) tuples"
    )

    # Phase 10.10 — apply --exclude-functions filter BEFORE drift check so known
    # environmental-variance functions don't trip the guard by default.
    exclusions = _parse_exclude_functions(args.exclude_functions)
    if exclusions:
        filtered = {}
        for key, series in per_func.items():
            # Canonicalize the data-side key: benchmark_all stores function names
            # WITH trailing "()" (e.g. "list_sessions()"); _parse_exclude_functions
            # strips them from user input. Without this canonicalization the
            # `key in exclusions` membership test would always return False.
            canonical = (key[0], key[1].removesuffix("()"))
            if canonical in exclusions:
                if args.explain_exclude:
                    # Use `canonical` for rationale lookup so the dict's paren-less
                    # keys match `("MetasploitBridge","list_sessions")`. Raw `key`
                    # still has the trailing "()", would always fall through.
                    rationale = _EXCLUDE_RATIONALE.get(
                        canonical,
                        "Excluded by --exclude-functions (no rationale documented).",
                    )
                    # key[1] already includes trailing "()" (benchmark_all stores
                    # function names WITH parens); do NOT add a second suffix.
                    print(
                        f"[regression_guard] Excluded {key[0]}.{key[1]}  \u2014  {rationale}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[regression_guard] Excluded {key[0]}.{key[1]}",
                        file=sys.stderr,
                    )
            else:
                filtered[key] = series
        per_func = filtered
        print(
            f"[regression_guard] Applied {len(exclusions)} exclusion(s); "
            f"{len(per_func)} tuples remain for drift testing",
            file=sys.stderr,
        )

    failures = check_guard(
        per_func, args.window, args.regress_pct, args.min_prior_ms
    )

    if failures:
        fails_sorted = sorted(failures, key=lambda r: r[4], reverse=True)
        print(
            f"\n[FAIL] regression-guard tripped: {len(failures)} function(s) regressed "
            f">{args.regress_pct}% vs {args.window}-prior mean (min prior ms = {args.min_prior_ms}):",
            file=sys.stderr,
        )
        for mod, fn, latest_ms, prior_mean, drift_pct, n_prior in fails_sorted:
            print(
                f"  - {mod:24s}.{fn:32s}  latest={latest_ms:.3f}ms  "
                f"prior_mean={prior_mean:.3f}ms  drift={drift_pct:+.1f}%  "
                f"(n_prior={n_prior})",
                file=sys.stderr,
            )
        sys.exit(1)

    print(
        f"[OK] regression-guard passed: no function with prior mean > {args.min_prior_ms}ms "
        f"regressed >{args.regress_pct}% vs {args.window}-prior mean"
    )


if __name__ == "__main__":
    main()
