#!/usr/bin/env python3
"""Vermillion LightFury — Comprehensive Function Benchmark
===========================================================
Benchmarks every public method across all VF modules.
Outputs JSON for dashboard consumption. Supports time-series
history tracking with --append-history flag.
"""
import sys, os, json, time, gc, traceback, re
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
# Cross-platform azazael path: on Kali, ROOT=/root/vermillion_lightfury, parent=/root
sys.path.insert(0, str(ROOT.parent / "azazael" / "src"))

# Cross-platform disk paths (imported from shared exceptions module)
from worms.exceptions import _IS_WINDOWS, _C_DRIVE, _E_DRIVE

MAX_HISTORY = 100  # rolling window cap

results = []
started = datetime.now().isoformat()

def bench(module_name: str, func_name: str, callable_fn, args=(), kwargs=None, warmup=1, runs=3):
    kwargs = kwargs or {}
    gc.collect()
    for _ in range(warmup):
        try:
            callable_fn(*args, **kwargs)
        except Exception:
            pass
    times = []
    error = None
    for _ in range(runs):
        gc.collect()
        t0 = time.perf_counter()
        try:
            result = callable_fn(*args, **kwargs)
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
        except Exception as e:
            error = str(e)[:200]
            break
    if times:
        avg = sum(times) / len(times)
        return {
            "module": module_name,
            "function": func_name,
            "avg_ms": round(avg * 1000, 2),
            "min_ms": round(min(times) * 1000, 2),
            "max_ms": round(max(times) * 1000, 2),
            "runs": len(times),
            "error": None,
        }
    else:
        return {
            "module": module_name,
            "function": func_name,
            "avg_ms": None,
            "min_ms": None,
            "max_ms": None,
            "runs": 0,
            "error": error,
        }

# ── WSLRecovery ──────────────────────────────────────────────────────────────
try:
    from worms.wsl_recovery import WSLRecovery
    wr = WSLRecovery()
    results.append(bench("WSLRecovery", "__init__", WSLRecovery))
    results.append(bench("WSLRecovery", "check_distro(kali)", lambda: wr.check_distro("kali")))
    results.append(bench("WSLRecovery", "check_all()", wr.check_all))
    results.append(bench("WSLRecovery", "get_summary()", wr.get_summary))
    results.append(bench("WSLRecovery", "_parse_wsl_list()", wr._parse_wsl_list))
    results.append(bench("WSLRecovery", "keepalive(kali)", lambda: wr.keepalive("kali")))
except Exception as e:
    results.append({"module": "WSLRecovery", "function": "IMPORT", "avg_ms": None, "error": str(e)[:200]})

# ── KaliHealth ───────────────────────────────────────────────────────────────
try:
    from worms.kali_health import KaliHealth
    kh = KaliHealth()
    results.append(bench("KaliHealth", "__init__", KaliHealth))
    results.append(bench("KaliHealth", "check()", kh.check, runs=1))
    results.append(bench("KaliHealth", "quick_check()", kh.quick_check, runs=3))
    results.append(bench("KaliHealth", "get_summary()", kh.get_summary))
    results.append(bench("KaliHealth", "preflight_check()", KaliHealth.preflight_check))
    results.append(bench("KaliHealth", "_check_drive()", kh._check_drive, runs=3))
    results.append(bench("KaliHealth", "_check_vhdx()", kh._check_vhdx, runs=3))
    results.append(bench("KaliHealth", "_check_kali_process()", kh._check_kali_process, runs=1))
    results.append(bench("KaliHealth", "_purge_stale_shared_logs()", kh._purge_stale_shared_logs, runs=1))
except Exception as e:
    results.append({"module": "KaliHealth", "function": "IMPORT", "avg_ms": None, "error": str(e)[:200]})

# ── NmapScanner ──────────────────────────────────────────────────────────────
try:
    from worms.nmap_scanner import NmapScanner
    ns = NmapScanner()
    results.append(bench("NmapScanner", "__init__", NmapScanner))
    results.append(bench("NmapScanner", "get_summary()", ns.get_summary))
    results.append(bench("NmapScanner", "_verify_kali_responsive()", ns._verify_kali_responsive, runs=1))
    results.append(bench("NmapScanner", "scan(localhost,quick)", lambda: ns.scan("127.0.0.1", scan_type="quick", timeout_sec=60), warmup=0, runs=1))
    results.append(bench("NmapScanner", "discover_live_hosts(localhost)", lambda: ns.discover_live_hosts("127.0.0.1/30", timeout=30), warmup=0, runs=1))
except Exception as e:
    results.append({"module": "NmapScanner", "function": "IMPORT", "avg_ms": None, "error": str(e)[:200]})

# ── MetasploitBridge ─────────────────────────────────────────────────────────
try:
    from worms.metasploit_bridge import MetasploitBridge
    mb = MetasploitBridge()
    results.append(bench("MetasploitBridge", "__init__", MetasploitBridge))
    results.append(bench("MetasploitBridge", "get_summary()", mb.get_summary))
    results.append(bench("MetasploitBridge", "_verify_kali_responsive()", mb._verify_kali_responsive, runs=1))
    # Phase 10.10 \u2014 pre-clean bench marker files BEFORE timing cleanup_old_scripts()
    # so the bench reflects the function's actual cost on a small directory, not the
    # accumulated cost of older bench runs (each previous benchmark_all.py invocation
    # dropped 3 .rc files into the shared dir via _save_resource_script()). Isolating
    # bench state from production state prevents the O(n) drift we just fixed.
    try:
        import glob
        _rc_dir = os.path.join(_C_DRIVE, "azazael_shared", "metasploit_logs") if _IS_WINDOWS else "/mnt/c/azazael_shared/metasploit_logs"
        for _f in glob.glob(os.path.join(_rc_dir, "msf_bench_test_*.rc")):
            try:
                os.remove(_f)
            except OSError:
                pass  # locked by running msfconsole, or already-removed race
    except Exception:
        pass
    results.append(bench("MetasploitBridge", "cleanup_old_scripts()", lambda: mb.cleanup_old_scripts(max_age_hours=24), runs=1))
    results.append(bench("MetasploitBridge", "_build_resource_script()", lambda: mb._build_resource_script("exploit/windows/smb/ms17_010_eternalblue", "10.0.0.1", "127.0.0.1")))
    results.append(bench("MetasploitBridge", "_save_resource_script()", lambda: mb._save_resource_script("use test\\nexit\\n", "bench_test")))
    # Phase 10.10 — warmup=2 + runs=2 averages out cold-start subprocess variance from WSL/msfconsole.
    results.append(bench("MetasploitBridge", "list_sessions()", mb.list_sessions, warmup=2, runs=2))
except Exception as e:
    results.append({"module": "MetasploitBridge", "function": "IMPORT", "avg_ms": None, "error": str(e)[:200]})

# ── SharedDirManager ─────────────────────────────────────────────────────────
try:
    from config.shared_dir import SharedDirManager
    sdm = SharedDirManager()
    results.append(bench("SharedDirManager", "__init__", SharedDirManager))
    results.append(bench("SharedDirManager", "ensure_directories()", sdm.ensure_directories))
    results.append(bench("SharedDirManager", "verify_access()", sdm.verify_access, runs=1))
    results.append(bench("SharedDirManager", "get_shared_path()", lambda: sdm.get_shared_path()))
    results.append(bench("SharedDirManager", "translate_path(win->wsl)", lambda: sdm.translate_path(os.path.join(_C_DRIVE, "azazael_shared", "nmap_scans"), target="wsl")))
    results.append(bench("SharedDirManager", "translate_path(wsl->win)", lambda: sdm.translate_path("/mnt/c/azazael_shared/nmap_scans", target="win")))
except Exception as e:
    results.append({"module": "SharedDirManager", "function": "IMPORT", "avg_ms": None, "error": str(e)[:200]})

# ── System Info ──────────────────────────────────────────────────────────────
import psutil
system = {
    "cpu_count": psutil.cpu_count(),
    "cpu_percent": psutil.cpu_percent(interval=0.1),
    "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
    "ram_used_gb": round(psutil.virtual_memory().used / (1024**3), 1),
    "ram_pct": psutil.virtual_memory().percent,
    "disk_free_gb": round(psutil.disk_usage(_C_DRIVE).free / (1024**3), 1),
}

try:
    e_usage = psutil.disk_usage(_E_DRIVE)
    system["e_drive_free_gb"] = round(e_usage.free / (1024**3), 1)
    system["e_drive_total_gb"] = round(e_usage.total / (1024**3), 1)
    system["e_drive_pct"] = e_usage.percent
except Exception:
    system["e_drive_free_gb"] = None
    system["e_drive_total_gb"] = None
    system["e_drive_pct"] = None

completed = datetime.now().isoformat()

output = {
    "benchmark_started": started,
    "benchmark_completed": completed,
    "system": system,
    "results": results,
    "summary": {
        "total_tests": len(results),
        "passed": sum(1 for r in results if r.get("error") is None and r.get("avg_ms") is not None),
        "failed": sum(1 for r in results if r.get("error") is not None),
        "skipped": sum(1 for r in results if r.get("avg_ms") is None and r.get("error") is None),
        "total_time_ms": round(sum(r["avg_ms"] for r in results if r.get("avg_ms")), 2),
        "slowest": max((r for r in results if r.get("avg_ms")), key=lambda r: r["avg_ms"], default=None),
        "fastest": min((r for r in results if r.get("avg_ms")), key=lambda r: r["avg_ms"], default=None),
    }
}

# ── Write single-run results ─────────────────────────────────────────────────
out_path = ROOT / "benchmarks/benchmark_results.json"
with open(out_path, "w") as f:
    json.dump(output, f, indent=2, default=str)
print(f"Benchmarks written to {out_path}")
print(f"Tests: {output['summary']['total_tests']} | Passed: {output['summary']['passed']} | Failed: {output['summary']['failed']}")
print(f"Slowest: {output['summary']['slowest']}")
print(f"Fastest: {output['summary']['fastest']}")


# ── History Append (--append-history) ────────────────────────────────────────
def append_to_history():
    """Append this run to benchmark_history.json, cap at MAX_HISTORY."""
    history_path = ROOT / "benchmarks/benchmark_history.json"
    history = []
    if history_path.exists():
        try:
            with open(history_path, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = []
    history.append(output)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2, default=str)
    print(f"History appended -> {history_path} ({len(history)} entries)")


# ── Dashboard Generation ─────────────────────────────────────────────────────
def generate_dashboard():
    """Regenerate benchmark_dashboard.html with embedded current + history data."""
    history_path = ROOT / "benchmarks/benchmark_history.json"
    history = []
    if history_path.exists():
        try:
            with open(history_path, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = []

    # Read template (never overwritten) and write generated dashboard
    template_path = ROOT / "benchmarks/dashboard_template.html"
    output_path = ROOT / "benchmarks/benchmark_dashboard.html"
    if not template_path.exists():
        print("Dashboard template not found, skipping generation.")
        return
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Embed data - replace placeholder comments
    data_json = json.dumps(output, default=str)
    history_json = json.dumps(history, default=str)

    html = html.replace(
        '/*__DATA_PLACEHOLDER__*/',
        f'const DATA = {data_json};'
    )
    html = html.replace(
        '/*__HISTORY_PLACEHOLDER__*/',
        f'const HISTORY = {history_json};'
    )

    # Write generated dashboard (separate file from template)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard regenerated -> {output_path}")


def assert_dashboard_title_consistency():
    """Sanity assertion: dashboard <title> drift + env-arch refs preserved.

    Catches:
      - Stale 'Quartet' worm-count remaining in <title>
      - Missing updated 'Quintet' worm-count in <title>
      - Accidentally removed env-architecture 'Quartet' refs in body

    On any mismatch, exits with code 1 to fail the build. Runs only when
    the dashboard was actually regenerated this invocation.
    """
    dashboard_path = ROOT / "benchmarks/benchmark_dashboard.html"
    if not dashboard_path.exists():
        # No regenerated dashboard on this run; nothing to assert.
        return

    content = dashboard_path.read_text(encoding="utf-8")

    NEW_TITLE = "Vermillion LightFury — Quintet Benchmark Dashboard"
    STALE_TITLE = "Vermillion LightFury — Quartet Benchmark Dashboard"
    EXPECTED_BODY_NEEDLES = [
        "Quartet Architecture",  # subtitle + section-heading (2 instances)
        "G:\\ Quartet",         # footer "G:\\ Quartet · Benchmarked..."
    ]

    title_match = re.search(r"<title>(.*?)</title>", content)
    title = title_match.group(1) if title_match else None

    failures = []
    if title is None:
        failures.append("No <title></title> tag found in dashboard file")
    else:
        if STALE_TITLE in title:
            failures.append(
                f"<title> contains stale worm-count 'Quartet' "
                f"(should be 'Quintet'): '{title}'"
            )
        if NEW_TITLE not in title:
            failures.append(
                f"<title> missing updated worm-count 'Quintet' "
                f"(expected contains: '{NEW_TITLE}'): '{title}'"
            )

    for needle in EXPECTED_BODY_NEEDLES:
        if needle not in content:
            failures.append(
                f"Body missing env-architecture ref '{needle}' "
                f"(accidentally removed?)"
            )

    if failures:
        print("\n[FAIL] Dashboard title sanity assertion FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nFix dashboard_template.html (or restore env-arch Quartet refs) "
            "and rerun the benchmark.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        qa = content.count("Quartet Architecture")
        gfq = content.count("G:\\ Quartet")
        print(
            f"[OK] Dashboard title assertion: <title>{title}</title>; "
            f"env-arch preserved (subtitle='Quartet Architecture' x{qa}, "
            f"footer='G:\\ Quartet' x{gfq})"
        )


# ── CLI Entry Point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--append-history" in args:
        append_to_history()
    if "--dashboard" in args or "--append-history" in args:
        generate_dashboard()
        assert_dashboard_title_consistency()
    if not args:
        print("Flags: --append-history  (append to benchmark_history.json & regenerate dashboard)")
        print("       --dashboard        (regenerate dashboard from existing history)")
