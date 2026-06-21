# coding: utf-8
"""
WSLRecovery — WSL Health & Recovery Worm for Azazael
=====================================================
Monitors all WSL2 distros, detects crashes, auto-restarts Kali,
runs e2fsck on corrupted VHDX filesystems, and integrates with
KaliHealth for proactive crash prevention.

Features:
- Distro health monitoring: Running / Stopped / Crashed detection
- Auto-restart: terminates + restarts crashed Kali with backoff
- VHDX integrity: e2fsck read-only check (or full repair with --repair)
- KaliHealth integration: gates health checks behind WSL liveness
- Crash history: tracks restart attempts and failure patterns
- PAMS audit: every recovery action recorded for forensics

Usage:
    >>> from worms.wsl_recovery import WSLRecovery
    >>> wr = WSLRecovery()
    >>> status = wr.check_all()
    >>> if not status.all_healthy:
    ...     result = wr.recover()

Registry:
    @register_worm(category="defense", priority=8, cooldown_seconds=90.0,
                   description="WSL health & recovery — auto-restart Kali, e2fsck VHDX, KaliHealth integration")
"""

import sys
import os
import json
import time
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
PRIMARY_DISTRO = "kali"
MONITORED_DISTROS = ["kali", "Ubuntu-26.04"]
KALI_VHDX_PATH = "E:\\Kali\\ext4.vhdx" if _IS_WINDOWS else "/mnt/e/Kali/ext4.vhdx"
CRASH_HISTORY_FILE = str(Path.home() / "vermillion_lightfury" / "wsl_recovery_history.json")

# Recovery thresholds
MAX_RESTART_ATTEMPTS = 3          # Max consecutive restarts before alert
DISTRO_STARTUP_TIMEOUT_SEC = 60   # Max wait for distro to come back
E2FSCK_TIMEOUT_SEC = 120          # Max wait for filesystem check
CRASH_WINDOW_MINUTES = 30         # Window for counting crash frequency

# ── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class WSLDistroStatus:
    """Health status of a single WSL distro."""
    name: str
    state: str            # Running / Stopped / Crashed / Unknown
    wsl_version: int
    responsive: bool
    last_error: str = ""
    checked_at: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.state == "Running" and self.responsive

    @property
    def alert_level(self) -> str:
        if self.state == "Crashed":
            return "critical"
        elif self.state == "Stopped":
            return "warning"
        elif not self.responsive:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "state": self.state,
            "wsl_version": self.wsl_version,
            "responsive": self.responsive,
            "alert": self.alert_level,
            "error": self.last_error[:200] if self.last_error else "",
        }


@dataclass
class VHDXFsckResult:
    """Result of an e2fsck filesystem check on the Kali VHDX."""
    path: str
    passed: bool
    read_only: bool
    errors_found: int
    errors_fixed: int
    output: str = ""
    duration_seconds: float = 0.0

    @property
    def alert_level(self) -> str:
        # Phase 13.9 SF3 fix: errors_found=-1 is an OPERATIONAL failure
        # sentinel (e2fsck could not even run). The previous `> 0` rule
        # silently demoted it to the `elif not passed: warning` branch,
        # downgrading a runtime failure to a benign warning. Using `!= 0`
        # lets the sentinel escalate to "critical" (matching the table in
        # tests/test_property_fuzz.py::test_SF3_vhdx_fsck_alert_levels_*).
        if not self.passed and self.errors_found != 0:
            return "critical"
        elif not self.passed:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "passed": self.passed,
            "read_only": self.read_only,
            "errors_found": self.errors_found,
            "errors_fixed": self.errors_fixed,
            "duration_sec": round(self.duration_seconds, 1),
            "alert": self.alert_level,
        }


@dataclass
class RecoveryAction:
    """A single recovery action performed."""
    action: str          # restart / terminate / fsck / fsck_repair
    distro: str
    success: bool
    detail: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "distro": self.distro,
            "success": self.success, "detail": self.detail[:500],
            "timestamp": self.timestamp,
        }


@dataclass
class RecoveryStatus:
    """Complete WSL recovery assessment."""
    timestamp: str
    distros: list[WSLDistroStatus] = field(default_factory=list)
    fsck_result: VHDXFsckResult | None = None
    actions_taken: list[RecoveryAction] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def all_healthy(self) -> bool:
        return all(d.is_healthy for d in self.distros)

    @property
    def overall_health(self) -> str:
        levels = [d.alert_level for d in self.distros]
        if self.fsck_result:
            levels.append(self.fsck_result.alert_level)
        if "critical" in levels:
            return "critical"
        if "warning" in levels:
            return "warning"
        return "healthy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall_health": self.overall_health,
            "all_healthy": self.all_healthy,
            "distros": [d.to_dict() for d in self.distros],
            "fsck": self.fsck_result.to_dict() if self.fsck_result else None,
            "actions_taken": [a.to_dict() for a in self.actions_taken],
            "alerts": self.alerts,
            "recommendations": self.recommendations,
        }


# ── WSLRecovery Worm ───────────────────────────────────────────────────────

@register_worm(
    category="defense",
    priority=8,
    cooldown_seconds=90.0,
    description="WSL health & recovery — auto-restart Kali, e2fsck VHDX, KaliHealth integration",
)
class WSLRecovery(ThreadSafeWormMixin, BaseWormMixin):
    """Worm that monitors and recovers WSL2 distros.

    Integrates with:
    - KaliHealth (gates health checks on WSL liveness; triggers diagnosis after crash)
    - PamsRecorder (full audit trail of every restart and fsck)
    - Shared directory (crash history for trend analysis)
    """

    VERSION = "1.0.0"
    __summary_method__ = "get_summary"

    def __init__(self):
        super().__init__()  # initialise self._lock via ThreadSafeWormMixin (wraps _restart_attempts, _last_restart_time, _crash_history, _checks_run, _recoveries_performed)
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
        self._restart_attempts: dict[str, int] = {}     # distro → count
        self._last_restart_time: dict[str, float] = {}   # distro → timestamp
        self._crash_history: list[dict[str, Any]] = self._load_crash_history()
        self._checks_run: int = 0
        self._recoveries_performed: int = 0

    def _load_crash_history(self) -> list[dict[str, Any]]:
        try:
            if os.path.exists(CRASH_HISTORY_FILE):
                with open(CRASH_HISTORY_FILE, "r") as f:
                    return json.load(f)[-200:]  # Keep last 200
        except Exception:
            pass
        return []

    def _save_crash_history(self):
        os.makedirs(os.path.dirname(CRASH_HISTORY_FILE), exist_ok=True)
        try:
            with open(CRASH_HISTORY_FILE, "w") as f:
                json.dump(self._crash_history, f, indent=2, default=str)
        except Exception:
            pass

    def _record_pams(self, event_type: str, data: dict[str, Any], severity: str = "info"):
        if self._pams:
            try:
                self._pams.record_event(
                    f"wsl_recovery.{event_type}", data,
                    source="WSLRecovery", severity=severity,
                )
            except Exception:
                pass

    def _wsl_exec(self, distro: str, command: str, timeout_sec: int = 30) -> tuple[int, str, str]:
        """Execute a command inside a WSL distro. Direct when native+Kali, WSL wrapper otherwise."""
        from worms.exceptions import _IS_KALI_NATIVE
        try:
            if _IS_KALI_NATIVE and distro == "kali":
                # Running ON Kali — direct bash call for Kali target
                result = subprocess.run(
                    ["bash", "-c", command],
                    capture_output=True, text=True, timeout=timeout_sec,
                )
            else:
                # Cross-distro or Windows — use WSL wrapper
                result = subprocess.run(
                    ["wsl.exe", "-d", distro, "--", "bash", "-c", command],
                    capture_output=True, text=True, timeout=timeout_sec,
                )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return (-1, "", f"TIMEOUT after {timeout_sec}s")
        except FileNotFoundError:
            return (-2, "", "WSL not found")
        except Exception as e:
            return (-3, "", str(e))

    def _windows_exec(self, args: list[str], timeout_sec: int = 30) -> tuple[int, str, str]:
        """Execute a Windows command (wsl.exe management commands).

        On Kali WSL, bare 'wsl' is not on PATH — must use 'wsl.exe'.
        This method auto-resolves 'wsl' → 'wsl.exe' when running natively.
        """
        from worms.exceptions import _IS_KALI_NATIVE
        resolved = list(args)
        if _IS_KALI_NATIVE and resolved and resolved[0] == "wsl":
            resolved[0] = "wsl.exe"
        try:
            result = subprocess.run(
                resolved, capture_output=True, text=True, timeout=timeout_sec,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return (-1, "", f"TIMEOUT after {timeout_sec}s")
        except FileNotFoundError:
            return (-2, "", "WSL executable not found")
        except Exception as e:
            return (-3, "", str(e))

    # ── Distro Health Checks ────────────────────────────────────────────────

    def _parse_wsl_list(self) -> dict[str, dict[str, Any]]:
        """Parse 'wsl --list --verbose' output into a dict of distro info."""
        rc, stdout, _ = self._windows_exec(
            ["wsl", "--list", "--verbose"],
            timeout_sec=15,
        )
        distros: dict[str, dict[str, Any]] = {}
        if rc != 0 or not stdout:
            return distros

        # Strip null bytes and carriage returns (wsl.exe can emit
        # null-padded and/or CRLF output on Windows)
        stdout = stdout.replace("\x00", "").replace("\r", "")

        for line in stdout.splitlines():
            # Lines look like: "  kali                   Running         2"
            # Or:              "* Ubuntu-26.04           Stopped         2"
            line = line.lstrip("* ").strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 3 and parts[-1].isdigit():
                name = " ".join(parts[:-2])
                state = parts[-2]
                version = int(parts[-1])
                distros[name] = {"state": state, "wsl_version": version}

        return distros

    def check_distro(self, distro: str) -> WSLDistroStatus:
        """Check health of a single WSL distro.

        Determines: Running, Stopped, or Crashed (listed as Running but unresponsive).
        When running natively on Kali and checking Kali itself, returns Running
        directly — wsl.exe --list doesn't show the currently-running distro.
        """
        from worms.exceptions import _IS_KALI_NATIVE
        if _IS_KALI_NATIVE and distro == "kali":
            # Skip wsl.exe --list (doesn't show self), but still verify liveness
            rc, stdout, _ = self._wsl_exec(distro, "echo OK", timeout_sec=10)
            responsive = rc == 0 and "OK" in stdout
            return WSLDistroStatus(
                name=distro,
                state="Running" if responsive else "Crashed",
                wsl_version=2, responsive=responsive,
                last_error="" if responsive else f"Unresponsive (rc={rc})",
                checked_at=datetime.now().isoformat(),
            )

        wsl_info = self._parse_wsl_list()
        info = wsl_info.get(distro, {})
        wsl_state = info.get("state", "Unknown")
        wsl_version = info.get("wsl_version", 0)
        responsive = False
        last_error = ""

        if wsl_state == "Running":
            # Verify it's actually responsive — a "Running" distro can be hung
            rc, stdout, stderr = self._wsl_exec(
                distro, "echo OK",
                timeout_sec=10,
            )
            if rc == 0 and "OK" in stdout:
                responsive = True
            else:
                # Running but unresponsive = crashed
                wsl_state = "Crashed"
                last_error = stderr[:200] if stderr else f"Unresponsive (rc={rc})"

        return WSLDistroStatus(
            name=distro,
            state=wsl_state,
            wsl_version=wsl_version,
            responsive=responsive,
            last_error=last_error,
            checked_at=datetime.now().isoformat(),
        )

    def check_all(self) -> RecoveryStatus:
        """Check health of all monitored WSL distros.

        Returns:
            RecoveryStatus with per-distro health and any alerts.
        """
        timestamp = datetime.now().isoformat()
        distros: list[WSLDistroStatus] = []
        alerts: list[str] = []
        recommendations: list[str] = []

        for distro in MONITORED_DISTROS:
            status = self.check_distro(distro)
            distros.append(status)

            if status.state == "Crashed":
                alerts.append(f"{distro} is CRASHED (listed as Running but unresponsive)")
                # Check crash frequency
                recent_crashes = sum(
                    1 for c in self._crash_history
                    if c.get("distro") == distro
                    and (time.time() - c.get("timestamp_epoch", 0)) < CRASH_WINDOW_MINUTES * 60
                )
                if recent_crashes >= MAX_RESTART_ATTEMPTS:
                    alerts.append(
                        f"{distro} has crashed {recent_crashes} times in {CRASH_WINDOW_MINUTES}min "
                        f"— possible VHDX corruption or resource exhaustion"
                    )
                    recommendations.append(f"Run fsck on {distro} VHDX and check E: drive free space")
                elif recent_crashes > 0:
                    recommendations.append(f"Auto-restart {distro} — {recent_crashes} crashes in window")
                else:
                    recommendations.append(f"Attempt to restart {distro} via wsl --terminate + restart")

            elif status.state == "Stopped":
                alerts.append(f"{distro} is STOPPED")
                recommendations.append(f"Start {distro}: wsl -d {distro}")

            elif not status.responsive:
                alerts.append(f"{distro} is Running but SLOW to respond")
                recommendations.append(f"Check {distro} CPU/RAM usage — may be overloaded")

        with self._lock:
            self._checks_run += 1
        self._record_pams("check_all", {
            "distros": [d.to_dict() for d in distros],
            "all_healthy": all(d.is_healthy for d in distros),
        })

        return RecoveryStatus(
            timestamp=timestamp,
            distros=distros,
            alerts=alerts,
            recommendations=recommendations,
        )

    # ── Recovery Actions ────────────────────────────────────────────────────

    def restart_distro(self, distro: str) -> RecoveryAction:
        """Terminate and restart a WSL distro.

        Uses wsl --terminate (graceful SIGTERM) then verifies the distro
        comes back online within DISTRO_STARTUP_TIMEOUT_SEC.

        Returns:
            RecoveryAction with success/failure and detail.
        """
        # Check crash frequency — don't infinite-loop restart a dying distro
        with self._lock:
            recent_crashes = sum(
                1 for c in self._crash_history
                if c.get("distro") == distro
                and (time.time() - c.get("timestamp_epoch", 0)) < CRASH_WINDOW_MINUTES * 60
            )
            if recent_crashes >= MAX_RESTART_ATTEMPTS:
                detail = (
                    f"REFUSED: {distro} has crashed {recent_crashes} times in "
                    f"{CRASH_WINDOW_MINUTES}min. Run fsck before retrying."
                )
                action = RecoveryAction(
                    action="restart_refused", distro=distro,
                    success=False, detail=detail,
                )
                self._record_pams("restart_refused", action.to_dict(), severity="critical")
                return action

            self._restart_attempts[distro] = self._restart_attempts.get(distro, 0) + 1

        self._record_pams("restart_started", {"distro": distro})

        # Step 1: Terminate
        rc, _, _ = self._windows_exec(
            ["wsl", "--terminate", distro],
            timeout_sec=15,
        )
        if rc != 0:
            action = RecoveryAction(
                action="terminate", distro=distro,
                success=False, detail=f"wsl --terminate failed (rc={rc})",
            )
            self._record_pams("terminate_failed", action.to_dict(), severity="error")
            return action

        # Step 2: Wait for termination to take effect
        time.sleep(3)

        # Step 3: Start the distro and verify responsiveness
        start_time = time.time()
        rc, stdout, stderr = self._wsl_exec(
            distro, "echo OK",
            timeout_sec=DISTRO_STARTUP_TIMEOUT_SEC,
        )

        elapsed = time.time() - start_time
        ok = rc == 0 and "OK" in stdout

        if ok:
            detail = f"Restarted successfully in {elapsed:.1f}s"
            success = True
            # F4: _last_restart_time is read by get_summary() and downstream
            # throttle checks; guard the write so concurrent restarts cannot
            # race on the timestamp.
            with self._lock:
                self._last_restart_time[distro] = time.time()
        else:
            detail = f"Failed to come back after {elapsed:.1f}s: {stderr[:200]}"
            success = False

        # Record crash if restart failed — guarded under F4 lock so
        # concurrent restart_distro() calls cannot interleave crash-history
        # appends (pre-existing gap surfaced by F4 audit, not a regression).
        if not success:
            with self._lock:
                self._crash_history.append({
                    "distro": distro, "timestamp": datetime.now().isoformat(),
                    "timestamp_epoch": time.time(), "action": "restart_failed",
                    "error": stderr[:200],
                })
            self._save_crash_history()

        action = RecoveryAction(
            action="restart" if success else "restart_failed",
            distro=distro, success=success, detail=detail,
        )

        self._record_pams(
            "restart_completed",
            action.to_dict(),
            severity="info" if success else "critical",
        )

        return action

    def fsck_kali(self, read_only: bool = True) -> VHDXFsckResult:
        """Run e2fsck on the Kali VHDX filesystem.

        When read_only=True: checks filesystem integrity without modifying
        anything (safe while Kali is running via -n flag).

        When read_only=False: performs auto-repair with -p flag. Requires
        Kali to be stopped first (terminates it automatically). Kali is
        NOT restarted after repair — caller must restart it.

        Returns:
            VHDXFsckResult with pass/fail, error counts, and raw output.
        """
        start = time.time()
        combined = ""

        if not read_only:
            # Repair mode — must stop Kali first
            self._windows_exec(["wsl", "--terminate", "kali"], timeout_sec=15)
            time.sleep(3)

        if read_only:
            # Try running e2fsck inside Kali on the root device first
            rc, stdout, stderr = self._wsl_exec(
                "kali",
                "sudo e2fsck -nf /dev/sdd 2>&1",
                timeout_sec=E2FSCK_TIMEOUT_SEC,
            )
            combined = stdout + "\n" + stderr

            if rc > 4:
                # Distro might not be running or /dev/sdd doesn't exist.
                # Only fall back to direct VHDX mount if Kali is NOT running
                # — mounting an in-use VHDX causes filesystem corruption.
                kali_alive = self.check_distro("kali").state == "Running"
                if kali_alive:
                    rc, stdout, stderr = (-3, "",
                        f"e2fsck failed (rc={rc}) but Kali is running — "
                        "cannot mount VHDX directly")
                    combined = stdout + "\n" + stderr
                else:
                    rc2, _, _ = self._windows_exec(
                        ["wsl", "--mount", KALI_VHDX_PATH, "--vhd", "--bare"],
                        timeout_sec=30,
                    )
                    time.sleep(3)
                    if rc2 == 0:
                        # Deterministic device discovery: blkid diff before/after mount
                        _, pre_devs, _ = self._wsl_exec(
                            "Ubuntu-26.04", "sudo blkid -o device 2>/dev/null | sort",
                            timeout_sec=15,
                        )
                        pre_set = set(pre_devs.strip().splitlines()) if pre_devs.strip() else set()
                        time.sleep(3)
                        _, post_devs, _ = self._wsl_exec(
                            "Ubuntu-26.04", "sudo blkid -o device 2>/dev/null | sort",
                            timeout_sec=15,
                        )
                        post_set = set(post_devs.strip().splitlines()) if post_devs.strip() else set()
                        new_devs = post_set - pre_set
                        if len(new_devs) == 1:
                            block_dev = list(new_devs)[0].replace("/dev/", "")
                            rc, stdout, stderr = self._wsl_exec(
                                "Ubuntu-26.04",
                                f"sudo e2fsck -nf /dev/{block_dev} 2>&1",
                                timeout_sec=E2FSCK_TIMEOUT_SEC,
                            )
                            combined = stdout + "\n" + stderr
                        else:
                            rc, stdout, stderr = (-3, "",
                                f"blkid diff found {len(new_devs)} new devices (expected 1) — aborting")
                            combined = stdout + "\n" + stderr
                        self._windows_exec(
                            ["wsl", "--unmount", KALI_VHDX_PATH],
                            timeout_sec=15,
                        )
                    else:
                        rc, stdout, stderr = (-3, "", "Failed to mount VHDX for direct access")
                        combined = stdout + "\n" + stderr
        else:
            # Repair mode — use direct VHDX mount (distro is stopped)
            rc_mount, _, _ = self._windows_exec(
                ["wsl", "--mount", KALI_VHDX_PATH, "--vhd", "--bare"],
                timeout_sec=30,
            )
            if rc_mount == 0:
                # Deterministic device discovery: blkid diff before/after mount
                _, pre_devs, _ = self._wsl_exec(
                    "Ubuntu-26.04", "sudo blkid -o device 2>/dev/null | sort",
                    timeout_sec=15,
                )
                pre_set = set(pre_devs.strip().splitlines()) if pre_devs.strip() else set()
                time.sleep(3)
                _, post_devs, _ = self._wsl_exec(
                    "Ubuntu-26.04", "sudo blkid -o device 2>/dev/null | sort",
                    timeout_sec=15,
                )
                post_set = set(post_devs.strip().splitlines()) if post_devs.strip() else set()
                new_devs = post_set - pre_set
                if len(new_devs) == 1:
                    block_dev = list(new_devs)[0].replace("/dev/", "")
                    rc, stdout, stderr = self._wsl_exec(
                        "Ubuntu-26.04",
                        f"sudo e2fsck -pf /dev/{block_dev} 2>&1",
                        timeout_sec=E2FSCK_TIMEOUT_SEC,
                    )
                    combined = stdout + "\n" + stderr
                else:
                    rc, stdout, stderr = (-3, "",
                        f"blkid diff found {len(new_devs)} new devices (expected 1) — aborting")
                    combined = stdout + "\n" + stderr
                self._windows_exec(
                    ["wsl", "--unmount", KALI_VHDX_PATH],
                    timeout_sec=15,
                )
            else:
                rc, stdout, stderr = (-3, "", "Failed to mount VHDX for repair")
                combined = stdout + "\n" + stderr

        duration = time.time() - start

        # Parse e2fsck result purely from exit code (not heuristic line counting).
        # e2fsck exit codes: 0=clean, 1=errors corrected, 4=errors left unfixed,
        # 8=operational error, 16=usage/syntax error.
        # With -n (read-only): rc=4 means errors found but not fixed.
        # With -p (auto-repair): rc=1 means errors were fixed.
        errors_found = 0
        errors_fixed = 0
        if rc == 4:
            errors_found = 1  # Errors detected (precise count only from parsing)
        elif rc == 1:
            errors_fixed = 1
        elif rc == 8:
            errors_found = -1  # Operational error — couldn't check
        passed = (rc == 0) if read_only else (rc in (0, 1))

        result = VHDXFsckResult(
            path=KALI_VHDX_PATH,
            passed=passed,
            read_only=read_only,
            errors_found=errors_found,
            errors_fixed=errors_fixed,
            output=combined[:3000],
            duration_seconds=duration,
        )

        self._record_pams(
            "fsck_completed",
            result.to_dict(),
            severity="info" if result.passed else "warning",
        )

        return result

    # ── Auto-Recovery ──────────────────────────────────────────────────────

    def recover(self, auto_restart: bool = True, run_fsck: bool = False) -> RecoveryStatus:
        """Auto-detect WSL issues and attempt recovery.

        Recovery flow:
        1. Check all distros
        2. If Kali is Crashed → terminate + restart (with backoff)
        3. If Kali is Stopped → attempt to start
        4. Update distro states in returned status to reflect recovery
        5. If run_fsck=True → run e2fsck read-only check

        Args:
            auto_restart: Automatically restart crashed/stopped distros
            run_fsck: Run e2fsck after recovery attempt

        Returns:
            RecoveryStatus with all actions taken and updated distro states.
        """
        # Step 1: Check all distros
        status = self.check_all()
        actions: list[RecoveryAction] = []
        recheck: WSLDistroStatus | None = None

        if not auto_restart:
            status.actions_taken = actions
            return status

        # Step 2: Start stopped or restart crashed Kali
        for distro_status in status.distros:
            distro = distro_status.name

            if distro_status.state == "Stopped" and distro == PRIMARY_DISTRO:
                # Just start — no terminate needed for clean stop
                self._record_pams("start_stopped", {"distro": distro})
                rc, stdout, _ = self._wsl_exec(distro, "echo OK", timeout_sec=DISTRO_STARTUP_TIMEOUT_SEC)
                ok = rc == 0 and "OK" in stdout
                action = RecoveryAction(
                    action="start" if ok else "start_failed", distro=distro,
                    success=ok, detail=f"{'Started OK' if ok else 'Failed to start: ' + stdout[:100]}",
                )
                actions.append(action)
                if ok:
                    with self._lock:
                        self._recoveries_performed += 1
                    recheck = self.check_distro(distro)
            elif distro_status.state == "Crashed" and distro == PRIMARY_DISTRO:
                action = self.restart_distro(distro)
                actions.append(action)

                if action.success:
                    with self._lock:
                        self._recoveries_performed += 1
                    # Re-check after restart to get updated state
                    recheck = self.check_distro(distro)
                    if not recheck.is_healthy:
                        status.alerts.append(
                            f"{distro} restarted but still unhealthy "
                            f"(state={recheck.state}, responsive={recheck.responsive})"
                        )
                        status.recommendations.append(
                            f"Run fsck on {distro} VHDX — possible filesystem corruption"
                        )
                else:
                    status.alerts.append(f"Failed to restart {distro}: {action.detail[:100]}")

        # Step 3: Update distro states in returned status to reflect recovery
        if recheck is not None:
            for i, ds in enumerate(status.distros):
                if ds.name == PRIMARY_DISTRO:
                    status.distros[i] = recheck

        # Step 4: Optional fsck (note: Kali stays stopped after repair mode)
        if run_fsck:
            fsck_result = self.fsck_kali(read_only=True)
            status.fsck_result = fsck_result
            if not fsck_result.passed:
                status.alerts.append(
                    f"e2fsck found filesystem issues on Kali VHDX (exit code indicates errors)"
                )
                status.recommendations.append(
                    "Run WSLRecovery.fsck_kali(read_only=False) to auto-repair filesystem"
                )

        status.actions_taken = actions
        self._record_pams("recovery_completed", status.to_dict())

        return status

    # ── WSL Keepalive ───────────────────────────────────────────────────────

    def keepalive(self, distro: str = "kali", timeout_sec: int = 10):
        """Ping a WSL distro to prevent auto-termination from inactivity.

        WSL2 auto-terminates distros after a short idle period. This sends a
        single lightweight 'echo OK' ping (~1s) to keep the distro alive.
        The caller is responsible for scheduling repeated calls at the desired
        interval (e.g., every 5 minutes via a worm scheduler or cron).

        Returns True if the distro responded, False otherwise.
        """
        rc, stdout, _ = self._wsl_exec(distro, "echo OK", timeout_sec=10)
        ok = rc == 0 and "OK" in stdout
        self._record_pams("keepalive", {
            "distro": distro, "alive": ok, "rc": rc,
        })
        return ok

    # ── KaliHealth Integration ──────────────────────────────────────────────

    def integrate_with_kali_health(self) -> dict[str, Any]:
        """Gate KaliHealth check behind WSL liveness verification.

        Checks if Kali is healthy first, then delegates to KaliHealth
        for full diagnostics. If Kali is crashed, attempts recovery first.

        Returns:
            Dict with wsl_status, kali_health_result, and actions_taken.
        """
        # Step 1: Verify WSL liveness
        kali_status = self.check_distro(PRIMARY_DISTRO)

        if not kali_status.is_healthy:
            # Attempt recovery first
            recovery = self.recover(auto_restart=True)
            kali_status = self.check_distro(PRIMARY_DISTRO)

            if not kali_status.is_healthy:
                return {
                    "wsl_healthy": False,
                    "kali_status": kali_status.to_dict(),
                    "kali_health_result": None,
                    "actions_taken": [a.to_dict() for a in recovery.actions_taken],
                    "message": "Kali is not healthy — KaliHealth check skipped",
                    "recommendation": "Run fsck on Kali VHDX or check E: drive space",
                }

        # Step 2: Delegate to KaliHealth for full diagnostics
        try:
            from worms.kali_health import KaliHealth
            kh = KaliHealth()
            kh_result = kh.check()
            return {
                "wsl_healthy": True,
                "kali_status": kali_status.to_dict(),
                "kali_health_result": kh_result.to_dict(),
                "actions_taken": [],
                "message": f"Kali is healthy — full diagnostics: {kh_result.overall_health}",
            }
        except ImportError:
            return {
                "wsl_healthy": True,
                "kali_status": kali_status.to_dict(),
                "kali_health_result": None,
                "actions_taken": [],
                "message": "Kali is healthy but KaliHealth is unavailable",
            }

    # ── Summary ────────────────────────────────────────────────────────────

    def get_summary(self) -> dict[str, Any]:
        """Return WSL recovery status summary."""
        wsl_info = self._parse_wsl_list()

        return {
            "version": self.VERSION,
            "project": "Vermillion LightFury",
            "session_id": self._session_id,
            "checks_run": self._checks_run,
            "recoveries_performed": self._recoveries_performed,
            "monitored_distros": MONITORED_DISTROS,
            "current_state": {
                name: wsl_info.get(name, {}).get("state", "Unknown")
                for name in MONITORED_DISTROS
            },
            "restart_attempts": dict(self._restart_attempts),
            "crash_history_size": len(self._crash_history),
            "pams_connected": self._pams is not None,
        }

    def execute(self) -> dict[str, Any]:
        """Standard worm execution — run check_all and report."""
        status = self.check_all()
        return {
            "success": True,
            "method": "check_all",
            "result": status.to_dict(),
        }


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="WSLRecovery — WSL Health & Recovery (Vermillion LightFury)",
    )
    # Phase 13.7: required=True propagates argparse errors as exit-2;
    # no-args UX is preserved by the len(sys.argv)==1 fallback before
    # parser.parse_args() (else-branch below becomes dead code after the fix).
    sub = parser.add_subparsers(dest="command", required=True, help="Command")

    check_p = sub.add_parser("check", help="Check all WSL distros")
    check_p.add_argument("--distro", default="", help="Check a specific distro")

    recover_p = sub.add_parser("recover", help="Auto-detect and fix WSL issues")
    recover_p.add_argument("--no-restart", action="store_true", help="Skip auto-restart")
    recover_p.add_argument("--fsck", action="store_true", help="Run e2fsck after recovery")

    fsck_p = sub.add_parser("fsck", help="Check Kali VHDX filesystem integrity")
    fsck_p.add_argument("--repair", action="store_true", help="Auto-repair (stops Kali first!)")

    restart_p = sub.add_parser("restart", help="Restart a WSL distro")
    restart_p.add_argument("distro", nargs="?", default="kali", help="Distro to restart")

    sub.add_parser("status", help="Show WSL distro states")
    sub.add_parser("kali-health", help="Run KaliHealth via WSL liveness gate")

    # Phase 13.7: no-args UX fallback. WSLRecovery summary path.
    if len(sys.argv) == 1:
        wr = WSLRecovery()
        print(f"WSLRecovery v{WSLRecovery.VERSION} — Vermillion LightFury")
        s = wr.get_summary()
        print(f"  Distros: {s['current_state']}")
        print(f"  Recoveries: {s['recoveries_performed']}")
        print(f"  Crash history: {s['crash_history_size']} events")
        print(f"  PAMS: {'connected' if s['pams_connected'] else 'unavailable'}")
        return

    args = parser.parse_args()
    wr = WSLRecovery()

    if args.command == "check":
        if args.distro:
            status = wr.check_distro(args.distro)
            print(f"{status.name}: {status.state} (responsive={status.responsive}, v{status.wsl_version})")
            if status.last_error:
                print(f"  Error: {status.last_error}")
        else:
            result = wr.check_all()
            print(f"WSL Health Check — {result.timestamp[:19]}")
            print(f"  Overall: {result.overall_health.upper()}")
            for d in result.distros:
                icon = {"Running": "+", "Stopped": "o", "Crashed": "XX"}.get(d.state, "?")
                print(f"  {icon} {d.name}: {d.state} (v{d.wsl_version})")
            if result.alerts:
                print(f"\n  Alerts:")
                for a in result.alerts:
                    print(f"    !! {a}")
            if result.recommendations:
                print(f"\n  Recommendations:")
                for r in result.recommendations:
                    print(f"    * {r}")

    elif args.command == "recover":
        result = wr.recover(auto_restart=not args.no_restart, run_fsck=args.fsck)
        print(f"Recovery — {result.timestamp[:19]}")
        print(f"  Overall: {result.overall_health.upper()}")
        for action in result.actions_taken:
            icon = "+" if action.success else "XX"
            print(f"  {icon} {action.action}: {action.detail[:100]}")
        if result.alerts:
            for a in result.alerts:
                print(f"  !! {a}")

    elif args.command == "fsck":
        print("Running e2fsck on Kali VHDX...")
        result = wr.fsck_kali(read_only=not args.repair)
        print(f"  Path:    {result.path}")
        print(f"  Passed:  {result.passed}")
        print(f"  Mode:    {'read-only' if result.read_only else 'auto-repair'}")
        print(f"  Errors:  {result.errors_found} found, {result.errors_fixed} fixed")
        print(f"  Duration: {result.duration_seconds:.1f}s")
        if result.errors_found > 0 and result.read_only:
            print(f"  Hint: Run with --repair to auto-fix errors (stops Kali first)")

    elif args.command == "restart":
        print(f"Restarting {args.distro}...")
        action = wr.restart_distro(args.distro)
        print(f"  {'+' if action.success else 'XX'} {action.detail[:200]}")

    elif args.command == "status":
        wsl_info = wr._parse_wsl_list()
        for name in MONITORED_DISTROS:
            info = wsl_info.get(name, {})
            state = info.get("state", "Unknown")
            icon = {"Running": "+", "Stopped": "o"}.get(state, "?")
            print(f"  {icon} {name}: {state} (v{info.get('wsl_version', '?')})")

    elif args.command == "kali-health":
        result = wr.integrate_with_kali_health()
        print(f"WSL Healthy: {result['wsl_healthy']}")
        print(f"Message:     {result['message']}")
        if result.get("kali_health_result"):
            kh = result["kali_health_result"]
            print(f"KaliHealth:  {kh.get('overall_health', 'unknown')}")
            drive = kh.get("drive", {})
            if drive:
                print(f"  E: drive:  {drive.get('free_gb', '?')} GB free")
        if result.get("recommendation"):
            print(f"Recommendation: {result['recommendation']}")

    else:
        print(f"WSLRecovery v{WSLRecovery.VERSION} — Vermillion LightFury")
        s = wr.get_summary()
        print(f"  Distros: {s['current_state']}")
        print(f"  Recoveries: {s['recoveries_performed']}")
        print(f"  Crash history: {s['crash_history_size']} events")
        print(f"  PAMS: {'connected' if s['pams_connected'] else 'unavailable'}")


if __name__ == "__main__":
    main()
