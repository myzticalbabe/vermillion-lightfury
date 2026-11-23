# Vermillion LightFury — Azazael × Kali Orchestration Engine

**Version:** 1.0.0 | **Python:** 3.14t (free-threaded) | **Platform:** Kali Linux (WSL2)

Vermillion LightFury is the Kali-native bridge between the Azazael AI worm ecosystem and Kali Linux penetration testing tools. It runs directly on Kali at `/root/vermillion_lightfury/` alongside Azazael at `/root/azazael/`.

---

## Architecture

```
vermillion_lightfury/                        # v1.0.0  |  Python 3.14t  |  Kali Linux (WSL2)
├── __init__.py                  # Package marker
├── worms/
│   ├── __init__.py              # 5 worms + ThreadSafeWormMixin re-export
│   ├── exceptions.py            # Cross-platform failure exceptions (Phase 10.10.4)
│   ├── mixins.py                # ThreadSafeWormMixin (allocates self._lock)
│   ├── nmap_scanner.py          # NmapScanner — Kali nmap via subprocess
│   ├── kali_health.py           # KaliHealth — E: drive/VHDX/RAM watchdog
│   ├── metasploit_bridge.py     # MetasploitBridge — msfconsole + CheckpointWorm
│   ├── wsl_recovery.py          # WSLRecovery — auto-restart, e2fsck, liveness
│   └── benchmark_worm.py        # BenchmarkWorm — telemetry, 33-function runner
├── config/
│   ├── __init__.py
│   ├── shared_dir.py            # SharedDirManager + purge_stale_rc_scripts()
│   └── f11_patterns.py          # F11 mixed-slash regex (Phase 10.8)
├── build/
│   ├── build.bat                # Nuitka 6-target standalone .exe build
│   ├── check_goto_chain.py      # Pre-build goto-chain validator
│   ├── check_no_mixed_slash.py  # Pre-build F11 anti-pattern detector
│   └── _pathutil.py             # Idempotent sys.path-root helper
├── tests/                       # 39 pytest tests
├── regression_guard.py          # Regression guard CLI (--exclude-functions, --explain-exclude)
├── benchmark_all.py             # 33-function benchmark runner
├── pytest.ini                   # `slow` marker registration
├── data/                        # kali_health_history.json (Phase 10.10.4)
├── logs/                        # AUTO_DOWNLOADER.log (Phase 10.10.4)
├── benchmarks/                  # benchmark_results.json + benchmark_history.json + benchmark_dashboard.html + dashboard_template.html (Phase 10.10.4)
├── AUDIT.md                     # Audit report (Phase 10.1)
├── MILESTONE.md                 # Session milestone log
└── README.md                    # This file
```

## Worms

### 1. NmapScanner (`worms/nmap_scanner.py`)

Wraps `nmap` directly on Kali with structured XML output parsing, VulnScanner cross-referencing, and PAMS audit trail.

**Category:** security | **Priority:** 8 | **Cooldown:** 45s

```python
from vermillion_lightfury.worms.nmap_scanner import NmapScanner

ns = NmapScanner()
result = ns.scan("192.168.1.0/24", scan_type="syn")
print(result.summary())  # {'hosts_up': 5, 'open_ports': 23, ...}

# Parallel multi-target scanning (free-threaded)
results = ns.scan_multi(["192.168.1.1", "10.0.0.1", "example.com"], max_workers=4)

# Vulnerability scan
vulns = ns.vulnerability_scan("target.com")

# Full comprehensive scan
full = ns.full_scan("192.168.1.1")
```

**Scan types:** syn, version, scripts, vuln, aggressive, udp, ping, quick, full

### 2. KaliHealth (`worms/kali_health.py`)

Monitors E: drive free space, VHDX file growth rate, Kali RAM/CPU usage. Provides early warning alerts before another full-disk crash.

**Category:** monitor | **Priority:** 8 | **Cooldown:** 60s

```python
from vermillion_lightfury.worms.kali_health import KaliHealth

kh = KaliHealth()
status = kh.check()

# Critical/Warning/Healthy + alerts + recommendations
print(status.overall_health)  # 'healthy'
print(status.drive.free_gb)   # 1.28
print(status.alerts)           # ['WARNING: E: only 1.3 GB free']

# Continuous monitoring
for status in kh.continuous_monitor(interval_sec=300):
    if status.alerts:
        print(f"ALERT: {status.alerts}")
```

**Alert thresholds:**
- E: drive < 1 GB → **CRITICAL**
- E: drive < 3 GB → **WARNING**
- VHDX growth > 500 MB/day → **WARNING**
- Kali RAM > 90% → **CRITICAL**

### 3. MetasploitBridge (`worms/metasploit_bridge.py`)

Automated Metasploit exploit execution via Kali WSL with CheckpointWorm-compatible rollback and PAMS audit.

**Category:** security | **Priority:** 9 | **Cooldown:** 120s

```python
from vermillion_lightfury.worms.metasploit_bridge import MetasploitBridge

mb = MetasploitBridge()
result = mb.exploit("eternalblue", "192.168.1.10")   # alias → ms17_010_eternalblue
print(result.status)  # 'exploited' / 'failed' / 'error' / 'timeout'

# Multi-target parallel (free-threaded) — real msfconsole attempts; alias resolves to full module path
results = mb.exploit_multi(["10.0.0.1", "10.0.0.2"], "eternalblue")

# Scan → auto-pick exploit per open port
report = mb.scan_and_exploit("10.0.0.0/24")

# Roll back to pre-exploit state if needed
mb.rollback()
```

**Aliases:** eternalblue, bluekeep, log4shell, ssh_brute, smb_login, ftp_login, http_login + 3 post-modules.

### 4. WSLRecovery (`worms/wsl_recovery.py`)

Monitors WSL2 distros, auto-restarts crashed Kali, runs e2fsck on VHDX; integrates with KaliHealth behind a liveness gate.

**Category:** defense | **Priority:** 8 | **Cooldown:** 90s

```python
from vermillion_lightfury.worms.wsl_recovery import WSLRecovery

wr = WSLRecovery()
status = wr.check_all()        # kali + Ubuntu-26.04
if not status.all_healthy:
    wr.recover(auto_restart=True)  # terminate + restart with backoff
    wr.fsck_kali(read_only=True)   # e2fsck -nf (safe on live VHDX)
```

**CLI:** `check` (--distro), `recover` (--no-restart, --fsck), `fsck` (--repair), `restart <distro>`, `status`, `kali-health`.

### 5. BenchmarkWorm (`worms/benchmark_worm.py`)

Weekly telemetry worm — runs `benchmark_all.py --append-history` via subprocess; appends to `benchmarks/benchmark_history.json` (100-entry cap); monitors history size for PAMS alerts.

**Category:** telemetry | **Priority:** 5 | **Cooldown:** 604800s (1 week)

```python
from vermillion_lightfury.worms.benchmark_worm import BenchmarkWorm

bw = BenchmarkWorm()
result = bw.execute()
print(f"Passed: {result['data']['passed']} | Elapsed: {result['data']['elapsed_sec']}s")
```

**Scope:** 33 functions across WSLRecovery + KaliHealth + NmapScanner + MetasploitBridge + SharedDirManager.

---

## Session ID Format

Every `__init__` in Vermillion LightFury (5 worms + `SharedDirManager`) generates a `_session_id` from `f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"` — **31 chars** in the format `YYYYMMDD_HHMMSS_ffffff_<8-hex>` (e.g., `20260620_153045_123456_a1b2c3d4`).

```
Format:   YYYYMMDD_HHMMSS_ffffff_<8-hex>   (31 chars; 8+1+6+1+6+1+8)
Example:  20260620_153045_123456_a1b2c3d4
```

The microsecond-resolution UTC timestamp + 8-hex-char uuid suffix (32 bits of `/dev/urandom`) defeats potential collisions under Python 3.14t free-threaded barrier-sync races — verified 100% distinct by `tests/test_concurrent_worm_instantiation_314t.py` T6 (10/10 per worm across 5 worms), T6b (50/50 across 5 worms combined), and T9 (`SharedDirManager` 10/10). The lock-allocation foundation underpinning this is `ThreadSafeWormMixin` in `worms/mixins.py`; `SharedDirManager` (in `config/shared_dir.py`) uses the same convention since Phase 11.6 (project-wide consistency debt closed).

**Filename usage** (session-id appears verbatim in these shared-directory paths):

- `nmap_scanner.py` → `nmap_<sid>_<ts>.xml`
- `metasploit_bridge.py` → `msf_<name-or-exploit>_<sid>_<ts>.rc`
- `shared_dir.py::SharedDirManager` → `_access_test_<sid>.txt`

**Regression guarantees** (Phase 12.1 — fail-fast on length regress):

- Every `__init__` enforces `len(_session_id) == 31` via an inline assert. A future refactor that accidentally drops the uuid suffix, shortens the timestamp field, or appends extra chars would corrupt downstream filename consumers and break PAMS payload cross-references.
- The guard is co-located with the format assignment so any worm author editing these methods sees it next to the spec it protects.
- `tests/test_concurrent_worm_instantiation_314t.py` T10 instantiates all 6 classes (5 worms + `SharedDirManager`) and asserts `len == 31` + regex match — a single failing test surfaces a regression across the whole project.

---

## Cross-Platform Helpers

### SharedDirManager (`config/shared_dir.py`)

Sets up and manages `/mnt/c/azazael_shared/` as the cross-platform data exchange hub (Windows C:, Kali WSL, Ubuntu WSL). G:\ is the archive mirror.

```python
from vermillion_lightfury.config.shared_dir import SharedDirManager

sdm = SharedDirManager()
sdm.run_setup()            # creates 9 subdirs + writes README
access = sdm.verify_access()
# {'windows': True, 'kali_wsl': True, 'ubuntu_wsl': True}
sdm.translate_path("/mnt/g/azazael_shared/nmap_scans/", target="win")
# → G:\azazael_shared\nmap_scans\
```

**Subdirectories** (under `/mnt/c/azazael_shared/`):

| Directory | Purpose |
|---|---|
| `nmap_scans/` | Nmap XML/JSON output |
| `health_reports/` | Kali health check snapshots |
| `tool_outputs/` | ffuf, gobuster, sqlmap output |
| `metasploit_logs/` | MSF console session logs |
| `packet_captures/` | tcpdump/scapy pcaps |
| `wordlists_shared/` | Shared wordlists |
| `exploit_outputs/` | Exploit results |
| `azazael_events/` | Cross-env event exchange |
| `temp/` | Temporary files |

---

## Thread-Safe Mixin

All 5 worms inherit from `ThreadSafeWormMixin` (`:worms/mixins.py`) which allocates `self._lock = threading.Lock()` in `__init__` via the MRO super chain; verified under barrier-sync workload in `tests/test_concurrent_worm_instantiation_314t.py` (50 distinct lock ids across 5 worms × 10 threads).

---

## Integration with Azazael

Both worms auto-register with the Azazael worm registry:

```python
import sys
sys.path.insert(0, "/root/azazael/src")
sys.path.insert(0, "/root/vermillion_lightfury")

from CORE.worm_registry import discover_worms, WORM_REGISTRY
discover_worms()

# Both worms now appear in registry
for name, info in WORM_REGISTRY.items():
    if "Kali" in name or "Nmap" in name:
        print(f"  [{info['category']}] {name} p={info['priority']}")
```

---

## .exe Production (Nuitka)

Build standalone .exe files with zero Python dependency:

```bash
cd /root/vermillion_lightfury/build
# Build scripts run natively on Kali
```

**Prerequisites:**
1. Python 3.14t active
2. Nuitka 3.14t patches applied: `python azazael/docs/scripts/patch_nuitka_314t.py --apply`
3. Azazael importable (add `azazael/src/` to PYTHONPATH)

**Expected .exe sizes:**
- NmapScanner: 8-12 MB (pure Python + subprocess)
- KaliHealth: 6-10 MB (minimal deps)
- SharedDirManager: 5-8 MB (filesystem only)

---

## Cross-Environment Data Flow

| Worm | Source | Operation | Sink directory |
|---|---|---|---|
| **NmapScanner** | Kali live | `nmap -oX` via subprocess | `nmap_scans/` |
| **KaliHealth** | Kali live | `free` + E: drive probe | `health_reports/` |
| **MetasploitBridge** | exploit wrapper | `msfconsole -r <rc>` | `metasploit_logs/` |
| **WSLRecovery** | WSL2 distros | `wsl --list` + `e2fsck` | `~/wsl_recovery_history.json` (local file) |
| **BenchmarkWorm** | weekly run | `benchmark_all.py --append-history` | `benchmarks/` |

All 5 worms write to **/mnt/c/azazael_shared/** (primary, NTFS-3G). The **G: archive mirror**
at `/mnt/g/azazael_shared/` (exFAT SD, 477 GB, ~16.5 MB/s) is read-mostly — used for
cold storage and cross-session persistence, NOT for hot worm output paths.```

        WINDOWS                       KALI / UBUNTU (WSL)
        C:\azazael_                  /mnt/c/azazael_
        shared\        ◄── RW ──►    shared/  (NTFS-3G, hot)
                                       ▼  (one-way archive push)
                                    /mnt/g/azazael_
                                    shared/   (G:, exFAT,
                                    archive mirror — read-mostly)
```

Azazael GUI on Windows reads the primary C: dir after each worm invocation;
Ubuntu WSL hosts the ML inference pipeline that reads nmap XML + hashdump outputs.

---

## Quick Start

```bash
# 1. Set up shared directory
python -m vermillion_lightfury.config.shared_dir setup

# 2. Check Kali health
python -m vermillion_lightfury.worms.kali_health check

# 3. Run an nmap scan — NmapScanner is API-only (no CLI subparsers); see `### 1. NmapScanner` above

# 4. Run a MetasploitBridge exploit sequence (positional target arg)
python -m vermillion_lightfury.worms.metasploit_bridge exploit 192.168.1.10

# 5. WSL health check + auto-recover if a distro is Stopped/Crashed
python -m vermillion_lightfury.worms.wsl_recovery check

# 6. Run the 33-function benchmark suite + append to history
python -m vermillion_lightfury.benchmark_all --append-history

# 7. Continuous health monitoring
python -m vermillion_lightfury.worms.kali_health watch --interval 300
```

---

## Project: Vermillion LightFury

A bridge between the Azazael AI worm ecosystem (77 worms, 17 categories, Python 3.14t free-threading) and Kali Linux penetration testing tools, running natively on Kali Linux at `/root/vermillion_lightfury/` with Azazael at `/root/azazael/`. G:\ is the archive drive for cold storage and cross-session persistence.
