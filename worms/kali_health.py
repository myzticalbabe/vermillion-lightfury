# coding: utf-8
"""
KaliHealth — Kali WSL Health Monitor Worm for Azazael
======================================================
Monitors E: drive free space, Kali RAM usage, VHDX file growth,
and provides early warning before another full-disk crash.

Features:
- E: drive space monitoring with threshold alerts
- VHDX file size tracking with growth rate detection
- Kali RAM/CPU usage monitoring via WSL
- Early warning system: yellow at 10% free, red at 5% free
- PAMS audit: every health check recorded for trend analysis
- Historical tracking: stores snapshots for growth rate calculation

Usage:
    >>> from vermillion_lightfury.worms.kali_health import KaliHealth
    >>> kh = KaliHealth()
    >>> status = kh.check()
    >>> if status["alerts"]:
    ...     print(f"WARNING: {status['alerts']}")

Registry:
    @register_worm(category="monitor", priority=8, cooldown_seconds=60.0,
                   description="Kali WSL health monitor — E: space, VHDX growth, RAM/CPU alerts")
"""

import sys
import os
import json
import time
import math
import subprocess
from datetime import datetime, timezone
import uuid  # Phase 11.5 — collision-free session_id suffix under 3.14t barrier race
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

# ── Paths ───────────────────────────────────────────────────────────────────
_AZAZAEL_SRC = str(Path(__file__).resolve().parent.parent.parent / "azazael" / "src")
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

# Local-thread mixin (lock allocator; listed BEFORE BaseWormMixin in class MRO).
from worms.mixins import ThreadSafeWormMixin  # noqa: F401

# ── PAMS ────────────────────────────────────────────────────────────────────
try:
    from CORE.PamsRecorder import PamsRecorder
    _pams = PamsRecorder()
except ImportError:
    _pams = None

# ── Configuration ───────────────────────────────────────────────────────────
# Cross-platform VHDX path (Kali WSL mounts E: at /mnt/e/)
from worms.exceptions import _IS_WINDOWS
KALI_VHDX_PATH = "E:\\Kali\\ext4.vhdx" if _IS_WINDOWS else "/mnt/e/Kali/ext4.vhdx"
HISTORY_FILE = str(Path.home() / "vermillion_lightfury" / "data/kali_health_history.json")

# Alert thresholds
E_DRIVE_CRITICAL_GB = 1.0      # Red alert: less than 1 GB free
E_DRIVE_WARNING_GB  = 3.0      # Yellow alert: less than 3 GB free
VHDX_GROWTH_RATE_WARN = 500    # Warning if VHDX grows >500 MB/day
RAM_USED_WARN_PCT = 90         # Warning if Kali RAM >90%
CPU_WARN_PCT = 85              # Warning if Kali CPU >85%

# ── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class DriveStatus:
    """E: drive health snapshot."""
    drive_letter: str
    total_gb: float
    free_gb: float
    used_gb: float
    used_pct: float
    filesystem: str
    accessible: bool

    @property
    def alert_level(self) -> str:
        """ok / warning / critical. Sentinel table — see MILESTONE Phase 13.9 SF7."""
        # Phase 13.9 SF7: NaN (missing read) → critical.
        if math.isnan(self.free_gb):
            return "critical"
        # Negative free space is physically impossible → critical.
        if self.free_gb < 0:
            return "critical"
        # +Inf (unbounded sentinel) — warn so the operator notices a
        # broken stat; not a stop-the-line critical like a missing read.
        if math.isinf(self.free_gb):
            return "warning"
        if self.free_gb <= E_DRIVE_CRITICAL_GB:
            return "critical"
        elif self.free_gb <= E_DRIVE_WARNING_GB:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": self.drive_letter, "total_gb": self.total_gb,
            "free_gb": self.free_gb, "used_pct": round(self.used_pct, 1),
            "filesystem": self.filesystem, "alert": self.alert_level,
        }


@dataclass
class VHDXStatus:
    """VHDX file health snapshot."""
    path: str
    size_gb: float
    last_modified: str
    exists: bool
    growth_mb_since_last: float = 0.0

    @property
    def alert_level(self) -> str:
        if abs(self.growth_mb_since_last) > VHDX_GROWTH_RATE_WARN:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "size_gb": self.size_gb,
            "last_modified": self.last_modified,
            "growth_mb": round(self.growth_mb_since_last, 1),
            "alert": self.alert_level,
        }


@dataclass
class KaliProcessStatus:
    """Kali WSL resource usage snapshot."""
    total_ram_gb: float
    used_ram_gb: float
    free_ram_gb: float
    ram_used_pct: float
    cpu_percent: float
    uptime_minutes: float
    accessible: bool

    @property
    def alert_level(self) -> str:
        if self.ram_used_pct > RAM_USED_WARN_PCT:
            return "critical"
        elif self.ram_used_pct > 80:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ram_total_gb": self.total_ram_gb, "ram_used_pct": round(self.ram_used_pct, 1),
            "cpu_pct": round(self.cpu_percent, 1),
            "uptime_min": round(self.uptime_minutes, 1),
            "alert": self.alert_level,
        }


@dataclass
class KaliHealthStatus:
    """Complete Kali health assessment."""
    timestamp: str
    drive: DriveStatus | None = None
    vhdx: VHDXStatus | None = None
    kali_process: KaliProcessStatus | None = None
    alerts: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def overall_health(self) -> str:
        """healthy / warning / critical."""
        levels = []
        for component in [self.drive, self.vhdx, self.kali_process]:
            if component and hasattr(component, "alert_level"):
                levels.append(component.alert_level)
        if "critical" in levels:
            return "critical"
        if "warning" in levels:
            return "warning"
        return "healthy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall_health": self.overall_health,
            "drive": self.drive.to_dict() if self.drive else None,
            "vhdx": self.vhdx.to_dict() if self.vhdx else None,
            "kali_process": self.kali_process.to_dict() if self.kali_process else None,
            "alerts": self.alerts,
            "recommendations": self.recommendations,
        }


# ── KaliHealth Worm ────────────────────────────────────────────────────────

@register_worm(
    category="monitor",
    priority=8,
    cooldown_seconds=60.0,
    description="Kali WSL health monitor — E: free space, VHDX growth, RAM/CPU breach alerts",
)
class KaliHealth(ThreadSafeWormMixin, BaseWormMixin):
    """Worm that monitors Kali WSL health across all three dimensions:
    host drive space, VHDX file growth, and Kali RAM/CPU usage.

    Records every snapshot to PAMS for trend analysis and provides
    early warning alerts before another full-disk crash occurs.
    """

    VERSION = "1.0.0"
    __summary_method__ = "get_summary"

    def __init__(self):
        super().__init__()  # F4: initialise self._lock via ThreadSafeWormMixin (wraps _checks_run)
        self._pams = _pams
        # Phase 11.5 — µs+uuid8 hex for collision-free 3.14t barrier-sync ids.
        # Format: YYYYMMDD_HHMMSS_ffffff_<8-hex> (31 chars; uuid suffix defeats
        # clock_gettime-micros-tick collisions under free-threaded race).
        self._session_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        # Phase 12.1 — len==31 regression guard. See init in nmap_scanner for rationale.
        assert len(self._session_id) == 31, (
            f"_session_id {self._session_id!r} is not 31 chars "
            f"(format regressed — see MILESTONE Phase 11.5 v2 / Phase 12.1)"
        )
        self._history: list[dict[str, Any]] = self._load_history()
        self._checks_run: int = 0
        self._last_alert_time: float = 0.0

    def _load_history(self) -> list[dict[str, Any]]:
        """Load previous health check history."""
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r") as f:
                    return json.load(f)[-100:]  # Keep last 100
        except Exception:
            pass
        return []

    def _save_history(self):
        """Save health check history."""
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(self._history, f, indent=2, default=str)
        except Exception:
            pass

    def _record_pams(self, event_type: str, data: dict[str, Any], severity: str = "info"):
        """Record to PAMS memory."""
        if self._pams:
            try:
                self._pams.record_event(
                    f"kali_health.{event_type}", data,
                    source="KaliHealth", severity=severity,
                )
            except Exception:
                pass

    def _wsl_exec(self, command: str, timeout_sec: int = 30) -> tuple[int, str, str]:
        """Execute a shell command on Kali. Direct when running natively, WSL wrapper from Windows."""
        from worms.exceptions import _IS_KALI_NATIVE
        try:
            if _IS_KALI_NATIVE:
                # Running ON Kali — direct bash call, no WSL wrapper needed
                result = subprocess.run(
                    ["bash", "-c", command],
                    capture_output=True, text=True, timeout=timeout_sec,
                )
            else:
                result = subprocess.run(
                    ["wsl.exe", "-d", "kali", "--", "bash", "-c", command],
                    capture_output=True, text=True, timeout=timeout_sec,
                )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return (-1, "", "TIMEOUT")
        except FileNotFoundError:
            return (-2, "", "WSL not found")
        except Exception as e:
            return (-3, "", str(e))

    @staticmethod
    def _resolve_linux_fstype(path: str) -> str:
        """Resolve filesystem type for a Linux mount point by parsing /proc/mounts.

        Returns the fstype of the longest-matching mountpoint (e.g. "ext4",
        "9p", "ntfs", "vfat"), or "unknown" if /proc/mounts cannot be read or
        no mount matches. Linux-only — caller should gate on _IS_WINDOWS.

        /proc/mounts is preferred over spawning `mount` because it is a single
        file read (no subprocess overhead) and is the kernel's authoritative
        mount table. Longest-prefix matching handles nested mounts correctly
        (e.g. /mnt/c/foo reports the inner bind-mount fstype, not the outer).
        """
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                best_match: str = "unknown"
                best_len: int = 0
                for raw_line in f:
                    parts = raw_line.split()
                    if len(parts) < 3:
                        continue
                    mount_point, fstype = parts[1], parts[2]
                    # Match: exact OR `path` is a descendant of `mount_point`.
                    # mp_norm rstrip prevents /mnt from matching /mnt2.
                    # Special-case the root ("/") since concatenating "/" + "/"
                    # would yield "//" which never matches a normalized path.
                    is_root = (mount_point == "/")
                    mp_norm = mount_point.rstrip("/")
                    if is_root:
                        matched = (path == "/") or path.startswith("/")
                        anchor_len = 1
                    elif path == mp_norm or path.startswith(mp_norm + "/"):
                        matched = True
                        anchor_len = len(mp_norm)
                    else:
                        matched = False
                        anchor_len = 0
                    if matched and anchor_len > best_len:
                        best_len = anchor_len
                        best_match = fstype
                return best_match
        except (FileNotFoundError, PermissionError, OSError):
            return "unknown"

    def _check_drive(self) -> DriveStatus:
        """Check E: drive free space and accessibility.

        Uses shutil.disk_usage for cross-platform compatibility — works on
        both Windows (E:\\) and Kali Linux (/mnt/e/).
        On Linux, filesystem type is resolved by parsing /proc/mounts
        (kernel-authoritative, no subprocess overhead).
        """
        from worms.exceptions import _E_DRIVE
        fs_type = "unknown"
        if not _IS_WINDOWS:
            fs_type = self._resolve_linux_fstype(_E_DRIVE)

        try:
            import shutil

            usage = shutil.disk_usage(_E_DRIVE)
            total = usage.total / (1024**3)
            free = usage.free / (1024**3)
            used = usage.used / (1024**3)
            pct = (used / total * 100) if total > 0 else 100

            return DriveStatus(
                drive_letter="E:", total_gb=round(total, 2),
                free_gb=round(free, 2), used_gb=round(used, 2),
                used_pct=round(pct, 1), filesystem=fs_type, accessible=True,
            )

        except Exception as e:
            self._record_pams("drive_check_error", {"error": str(e)}, severity="error")

        return DriveStatus(
            drive_letter="E:", total_gb=0, free_gb=0, used_gb=0,
            used_pct=100, filesystem=fs_type, accessible=False,
        )

    def _check_vhdx(self) -> VHDXStatus:
        """Check VHDX file size and growth rate."""
        growth = 0.0
        size_gb = 0.0
        last_mod = ""
        exists = False

        try:
            if os.path.exists(KALI_VHDX_PATH):
                exists = True
                stat = os.stat(KALI_VHDX_PATH)
                size_gb = stat.st_size / (1024**3)
                last_mod = datetime.fromtimestamp(stat.st_mtime).isoformat()

                # Calculate growth from history
                if self._history:
                    last_snapshot = self._history[-1]
                    last_vhdx = last_snapshot.get("vhdx", {})
                    last_size = last_vhdx.get("size_gb", 0) if isinstance(last_vhdx, dict) else 0
                    if last_size > 0:
                        growth = (size_gb - last_size) * 1024  # Convert to MB

        except Exception as e:
            self._record_pams("vhdx_check_error", {"error": str(e)}, severity="error")

        return VHDXStatus(
            path=KALI_VHDX_PATH, size_gb=round(size_gb, 2),
            last_modified=last_mod, exists=exists,
            growth_mb_since_last=round(growth, 1),
        )

    def _check_kali_process(self) -> KaliProcessStatus:
        """Check Kali WSL RAM and CPU usage."""
        # Run memory and load checks separately for reliable parsing
        rc, mem_stdout, _ = self._wsl_exec(
            "free -b | grep Mem:",
            timeout_sec=10,
        )
        rc2, load_stdout, _ = self._wsl_exec(
            "cat /proc/loadavg",
            timeout_sec=10,
        )
        rc3, uptime_raw, _ = self._wsl_exec(
            "cat /proc/uptime",
            timeout_sec=10,
        )

        if rc == 0 and mem_stdout:
            # Parse free -b output: Mem: total used free shared buff/cache available
            mem_parts = mem_stdout.split()
            if len(mem_parts) >= 3:
                mem_total = float(mem_parts[1]) / (1024**3)
                mem_used = float(mem_parts[2]) / (1024**3)
                free_mem = mem_total - mem_used
                ram_pct = (mem_used / mem_total * 100) if mem_total > 0 else 0

                # Parse load average (1 min) — first field of /proc/loadavg
                cpu = 0.0
                if rc2 == 0 and load_stdout:
                    load_parts = load_stdout.split()
                    if len(load_parts) > 0:
                        cpu = float(load_parts[0]) / 16 * 100  # 16 CPUs

                # Parse uptime — first field of /proc/uptime (seconds, then idle seconds)
                uptime_min = 0.0
                if rc3 == 0 and uptime_raw:
                    uptime_first = uptime_raw.split()[0]
                    uptime_min = float(uptime_first) / 60

                return KaliProcessStatus(
                    total_ram_gb=round(mem_total, 2),
                    used_ram_gb=round(mem_used, 2),
                    free_ram_gb=round(free_mem, 2),
                    ram_used_pct=round(ram_pct, 1),
                    cpu_percent=round(cpu, 1),
                    uptime_minutes=round(uptime_min, 1),
                    accessible=True,
                )

        return KaliProcessStatus(
            total_ram_gb=0, used_ram_gb=0, free_ram_gb=0,
            ram_used_pct=0, cpu_percent=0, uptime_minutes=0,
            accessible=False,
        )

    # ── Main Health Check ─────────────────────────────────────────────────

    def check(self) -> KaliHealthStatus:
        """Run a complete Kali health check across all dimensions.

        Returns:
            KaliHealthStatus with alerts and recommendations.
        """
        timestamp = datetime.now().isoformat()

        # Run all checks
        drive = self._check_drive()
        vhdx = self._check_vhdx()
        kali = self._check_kali_process()

        alerts: list[str] = []
        recommendations: list[str] = []

        # ── Drive Alerts ──
        if not drive.accessible:
            alerts.append("E: drive is INACCESSIBLE — Kali cannot boot!")
            recommendations.append("Check E: drive connection. Run chkdsk E: /f")
        elif drive.free_gb <= E_DRIVE_CRITICAL_GB:
            alerts.append(f"CRITICAL: E: only {drive.free_gb:.2f} GB free — risk of crash!")
            recommendations.append("IMMEDIATE: Free space on E: or compact VHDX. Run: apt clean && fstrim /")
        elif drive.free_gb <= E_DRIVE_WARNING_GB:
            alerts.append(f"WARNING: E: only {drive.free_gb:.2f} GB free — running low")
            recommendations.append("Consider compacting VHDX or migrating log files to G: drive")

        # ── VHDX Alerts ──
        if not vhdx.exists:
            alerts.append("VHDX file NOT FOUND at E:\\Kali\\ext4.vhdx — Kali may be unregistered!")
            recommendations.append("Run: wsl --list --verbose to verify Kali is installed")
        elif vhdx.growth_mb_since_last > VHDX_GROWTH_RATE_WARN:
            alerts.append(f"VHDX growing rapidly: +{vhdx.growth_mb_since_last:.0f} MB since last check")
            recommendations.append("Check Kali for runaway logs, Docker images, or large file operations")

        # ── Kali Process Alerts ──
        if not kali.accessible:
            alerts.append("Kali WSL is NOT accessible — may be stopped or crashed")
            recommendations.append("Run: wsl -d kali -- bash -c 'echo test' to verify")
        elif kali.ram_used_pct > RAM_USED_WARN_PCT:
            alerts.append(f"Kali RAM usage CRITICAL: {kali.ram_used_pct:.0f}%")
            recommendations.append("Reduce concurrent Kali processes or allocate more RAM to WSL (.wslconfig)")
        elif kali.ram_used_pct > 80:
            alerts.append(f"Kali RAM usage HIGH: {kali.ram_used_pct:.0f}%")

        if kali.cpu_percent > CPU_WARN_PCT:
            alerts.append(f"Kali CPU usage HIGH: {kali.cpu_percent:.0f}%")

        # ── Build Result ──
        status = KaliHealthStatus(
            timestamp=timestamp,
            drive=drive, vhdx=vhdx, kali_process=kali,
            alerts=alerts, recommendations=recommendations,
        )

        # Save to history — all 3 mutations under the same lock so
        # concurrent check() calls cannot interleave the list-append,
        # the disk flush, and the counter increment.
        with self._lock:
            self._history.append(status.to_dict())
            self._save_history()
            self._checks_run += 1

        # PAMS audit
        self._record_pams(
            "health_check",
            status.to_dict(),
            severity=status.overall_health if status.overall_health != "healthy" else "info",
        )

        self._purge_stale_shared_logs()

        return status

    # ── Shared Log Maintenance ─────────────────────────────────────────────

    def _purge_stale_shared_logs(self):
        """Delete .rc resource scripts older than 7 days from shared metasploit_logs/.

        Delegates to shared purge_stale_rc_scripts() in config.shared_dir.
        Throttled at the module level to run at most once per hour.
        """
        try:
            from config.shared_dir import purge_stale_rc_scripts
            deleted = purge_stale_rc_scripts(
                log_dir=r"C:\azazael_shared\metasploit_logs" if _IS_WINDOWS else "/mnt/c/azazael_shared/metasploit_logs",
                max_age_hours=168,  # 7 days
                throttle_seconds=3600,  # 1 hour
            )
            if deleted > 0:
                self._record_pams("stale_log_purge", {"deleted": deleted})
        except Exception as e:
            self._record_pams("stale_log_purge_error", {"error": str(e)}, severity="warning")

    def quick_check(self) -> KaliHealthStatus:
        """Fast check with just the critical metrics (drive space only)."""
        drive = self._check_drive()
        return KaliHealthStatus(
            timestamp=datetime.now().isoformat(),
            drive=drive,
            alerts=(
                [f"E: only {drive.free_gb:.2f} GB free — critical!"]
                if drive.alert_level == "critical"
                else []
            ),
        )

    def continuous_monitor(self, interval_sec: int = 300, max_checks: int = 0):
        """Run continuous health monitoring.

        Args:
            interval_sec: Seconds between checks
            max_checks: Max checks (0 = infinite)

        Yields:
            KaliHealthStatus for each check cycle.
        """
        checks = 0
        while max_checks == 0 or checks < max_checks:
            status = self.check()
            yield status
            checks += 1
            if status.alerts:
                self._record_pams(
                    "continuous_alert",
                    {"alerts": status.alerts, "check_number": checks},
                    severity="warning",
                )
            time.sleep(interval_sec)

    # ── Summary ────────────────────────────────────────────────────────────

    def get_summary(self) -> dict[str, Any]:
        """Return health monitor status summary."""
        # Quick check
        drive = self._check_drive()
        vhdx = self._check_vhdx()

        return {
            "version": self.VERSION,
            "project": "Vermillion LightFury",
            "session_id": self._session_id,
            "checks_run": self._checks_run,
            "e_drive": {
                "free_gb": drive.free_gb,
                "total_gb": drive.total_gb,
                "used_pct": drive.used_pct,
                "alert": drive.alert_level,
            },
            "vhdx": {
                "size_gb": vhdx.size_gb,
                "exists": vhdx.exists,
                "growth_mb": vhdx.growth_mb_since_last,
            },
            "history_size": len(self._history),
            "pams_connected": self._pams is not None,
        }

    def execute(self) -> dict[str, Any]:
        """Standard worm execution entry point — runs full health check."""
        status = self.check()
        return {
            "success": True,
            "method": "check",
            "result": status.to_dict(),
        }

    # ── Circuit Breaker ────────────────────────────────────────────────────

    @classmethod
    def preflight_check(cls) -> tuple[bool, str]:
        """Pre-flight gate: check if E: drive has enough space to operate.

        Returns (ok, reason). If ok=False, all exploit/scan worms should abort.
        Uses shutil.disk_usage for cross-platform compatibility with a
        fallback that returns True (permissive) if the check errors.

        Thresholds:
          < 0.5 GB free → BLOCK (critical — OS-level risk)
          < 2.0 GB free → WARN but pass (degraded, still operable)
        """
        try:
            import shutil
            usage = shutil.disk_usage("E:\\")
            free_gb = usage.free / (1024 ** 3)
            if free_gb < 0.5:
                return False, f"E: drive has only {free_gb:.2f} GB free — BLOCKED for safety"
            elif free_gb < 2.0:
                return True, f"E: drive low ({free_gb:.2f} GB free) — proceeding with caution"
            return True, f"E: drive OK ({free_gb:.2f} GB free)"
        except Exception as e:
            # Permissive fallback: if we can't check, don't block operations
            return True, f"preflight check errored (permissive): {e}"


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="KaliHealth — Kali WSL Health Monitor (Vermillion LightFury)",
    )
    # Phase 13.7: required=True makes argparse exit 2 with "the following
    # arguments are required: command" when no subcommand is provided.
    # Without this, the worm silently fell through to a banner-printing
    # else branch — masking invalid input. The `if len(sys.argv) == 1`
    # fallback below preserves the no-args UX (banner + summary) for F1/F5/F7.
    sub = parser.add_subparsers(dest="command", required=True, help="Command")

    check_p = sub.add_parser("check", help="Run full health check")
    check_p.add_argument("--json", action="store_true", help="Output as JSON")

    quick_p = sub.add_parser("quick", help="Quick E: drive check")

    watch_p = sub.add_parser("watch", help="Continuous monitoring")
    watch_p.add_argument("-i", "--interval", type=int, default=300, help="Seconds between checks")
    watch_p.add_argument("-n", "--max-checks", type=int, default=0, help="Max checks (0=infinite)")

    # Phase 13.7: no-args UX fallback. argparse requires a subcommand
    # (required=True above) — but `python -m worms.kali_health` (no args)
    # should still print the banner + E: drive summary at exit 0, matching
    # the pre-Phase-13.7 behavior relied on by F1/F5/F7 tests.
    if len(sys.argv) == 1:
        kh = KaliHealth()
        print(f"KaliHealth v{KaliHealth.VERSION} — Vermillion LightFury")
        print(f"  Commands: check, quick, watch")
        print(f"  VHDX: {KALI_VHDX_PATH}")
        print(f"  History: {HISTORY_FILE}")
        summary = kh.get_summary()
        print(f"  E: {summary['e_drive']['free_gb']:.1f} GB free [{summary['e_drive']['alert']}]")
        return

    args = parser.parse_args()
    kh = KaliHealth()

    if args.command == "check":
        status = kh.check()
        if args.json:
            print(json.dumps(status.to_dict(), indent=2, default=str))
        else:
            print(f"Kali Health Check — {status.timestamp}")
            print(f"  Overall: {status.overall_health.upper()}")
            if status.drive:
                print(f"  E: drive: {status.drive.free_gb:.1f} GB free / {status.drive.total_gb:.0f} GB ({status.drive.used_pct:.0f}%) [{status.drive.alert_level}]")
            if status.vhdx:
                print(f"  VHDX: {status.vhdx.size_gb:.1f} GB {'(not found!)' if not status.vhdx.exists else ''}")
            if status.kali_process:
                print(f"  Kali: RAM {status.kali_process.ram_used_pct:.0f}% CPU {status.kali_process.cpu_percent:.0f}%")
            if status.alerts:
                print(f"\n  {'!' * 3} ALERTS:")
                for a in status.alerts:
                    print(f"    - {a}")
            if status.recommendations:
                print(f"\n  Recommendations:")
                for r in status.recommendations:
                    print(f"    * {r}")

    elif args.command == "quick":
        result = kh.quick_check()
        can_boot = result.drive.accessible and result.drive.free_gb > 0.5
        print(f"E: {result.drive.free_gb:.1f} GB free — {'OK' if can_boot else 'CRITICAL'}")

    elif args.command == "watch":
        print(f"Continuous monitoring every {args.interval}s — Ctrl+C to stop")
        try:
            for status in kh.continuous_monitor(interval_sec=args.interval, max_checks=args.max_checks):
                icon = {"healthy": "+", "warning": "!!", "critical": "XX"}.get(status.overall_health, "?")
                print(f"[{status.timestamp[:19]}] {icon} {status.overall_health.upper()} | "
                      f"E: {status.drive.free_gb:.1f}GB free | "
                      f"VHDX: {status.vhdx.size_gb:.1f}GB | "
                      f"Kali RAM: {status.kali_process.ram_used_pct:.0f}%")
                if status.alerts:
                    for a in status.alerts:
                        print(f"  !! {a}")
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")

    else:
        print(f"KaliHealth v{KaliHealth.VERSION} — Vermillion LightFury")
        print(f"  Commands: check, quick, watch")
        print(f"  VHDX: {KALI_VHDX_PATH}")
        print(f"  History: {HISTORY_FILE}")
        summary = kh.get_summary()
        print(f"  E: {summary['e_drive']['free_gb']:.1f} GB free [{summary['e_drive']['alert']}]")


if __name__ == "__main__":
    main()
