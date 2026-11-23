# coding: utf-8
"""
SharedDir — Cross-Platform Results Directory for Vermillion LightFury
=====================================================================
Manages C:\\azazael_shared\\ as the primary cross-platform data
exchange hub between Windows Azazael, Kali WSL, and Ubuntu WSL.

G:\\azazael_shared\\ is available as the archive mirror for cold storage
and cross-session persistence (exFAT SD card, 477 GB).

Features:
- Directory creation with proper permissions
- WSL path mapping (G:\\azazael_shared\\ ↔ /mnt/g/azazael_shared/)
- Subdirectory structure for organized tool output
- Write test to verify accessibility from all environments
- PAMS audit for setup verification

Usage:
    >>> from vermillion_lightfury.config.shared_dir import SharedDirManager
    >>> sdm = SharedDirManager()
    >>> sdm.ensure_directories()
    >>> sdm.verify_access()
"""

import os
import sys
import subprocess
import threading
import time
import uuid  # Phase 11.6 — collision-free session_id suffix under 3.14t barrier race (matches 5-worm convention)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ───────────────────────────────────────────────────────────────────
_AZAZAEL_SRC = str(Path(__file__).resolve().parent.parent.parent / "azazael" / "src")
if _AZAZAEL_SRC not in sys.path:
    sys.path.insert(0, _AZAZAEL_SRC)

# ── PAMS ────────────────────────────────────────────────────────────────────
try:
    from CORE.PamsRecorder import PamsRecorder
    _pams = PamsRecorder()
except ImportError:
    _pams = None


# ── Constants ───────────────────────────────────────────────────────────────
# Cross-platform shared directory (Kali WSL mounts C: at /mnt/c/, G: at /mnt/g/)
from worms.exceptions import _IS_WINDOWS
# PRIMARY shared directory on C: drive (always mounted in WSL as /mnt/c/)
SHARED_ROOT_WIN = "C:\\azazael_shared"
SHARED_ROOT_WSL = "/mnt/c/azazael_shared"

# ARCHIVE shared directory on G: drive (archive/cold storage, exFAT SD card 477 GB)
SHARED_G_WIN = "G:\\azazael_shared"
SHARED_G_WSL = "/mnt/g/azazael_shared"

SUBDIRS = [
    "nmap_scans",           # nmap XML/JSON output
    "health_reports",        # Kali health check snapshots
    "tool_outputs",          # General tool output (ffuf, gobuster, etc.)
    "metasploit_logs",       # MSF console session logs
    "packet_captures",       # tcpdump/scapy pcap files
    "wordlists_shared",      # Shared wordlists between tools
    "exploit_outputs",       # Exploit results and shell outputs
    "azazael_events",        # Cross-env event exchange (MemoryBus bridge)
    "temp",                  # Temporary files (auto-cleaned)
]

WSL_DISTROS = ["kali", "Ubuntu-26.04"]


class SharedDirManager:
    """Manages the G:\\azazael_shared\\ cross-platform directory.

    Ensures directories exist with proper structure, verifies
    accessibility from both Windows and WSL, and provides
    path translation between Windows ↔ WSL.
    """

    VERSION = "1.0.0"

    def __init__(self):
        self._pams = _pams
        # Phase 11.6 — µs+uuid8 hex for collision-free 3.14t barrier-sync ids.
        # Format: YYYYMMDD_HHMMSS_ffffff_<8-hex> (31 chars; matches the 5-worm
        # convention from Phase 11.5 v2). Used in f"_access_test_{self._session_id}.txt"
        # filename at line 225 — under barrier-race concurrent verify_access(),
        # uuid hex suffix defends against filename collision.
        self._session_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        # Phase 12.1 — len==31 regression guard. Same rationale as the 5-worm
        # `__init__` assertion; SharedDirManager was added in Phase 11.6 so the
        # guard is co-located to make the symmetric invariant explicit.
        assert len(self._session_id) == 31, (
            f"_session_id {self._session_id!r} is not 31 chars "
            f"(format regressed — see MILESTONE Phase 11.5 v2 / Phase 12.1)"
        )

    def _record(self, event_type: str, data: dict[str, Any], severity: str = "info"):
        if self._pams:
            try:
                self._pams.record_event(
                    f"shared_dir.{event_type}", data,
                    source="SharedDirManager", severity=severity,
                )
            except Exception:
                pass

    def _wsl_exec(self, distro: str, command: str, timeout_sec: int = 15) -> tuple[int, str, str]:
        """Execute a shell command inside a WSL distro.

        When running natively on Kali and targeting Kali, uses direct bash.
        Otherwise, delegates to wsl.exe for cross-distro commands.
        """
        from worms.exceptions import _IS_KALI_NATIVE
        try:
            if _IS_KALI_NATIVE and distro == "kali":
                result = subprocess.run(
                    ["bash", "-c", command],
                    capture_output=True, text=True, timeout=timeout_sec,
                )
            else:
                result = subprocess.run(
                    ["wsl.exe", "-d", distro, "--", "bash", "-c", command],
                    capture_output=True, text=True, timeout=timeout_sec,
                )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return (-1, "", str(e))

    def ensure_directories(self) -> dict[str, Any]:
        """Create all shared subdirectories.

        Returns status dict with created/existing counts.
        """
        created = []
        existing = []
        failed = []

        # Determine the correct local root for this OS environment
        root_path = SHARED_ROOT_WIN if _IS_WINDOWS else SHARED_ROOT_WSL

        # Create base directory
        try:
            os.makedirs(root_path, exist_ok=True)
        except Exception as e:
            failed.append({"path": root_path, "error": str(e)})
            self._record("create_failed", {"path": root_path, "error": str(e)}, severity="error")
            return {"created": 0, "existing": 0, "failed": 1, "root_ok": False}

        # Phase 10.10 — fast-path: if all 9 SUBDIRS already exist on a single
        # scandir syscall, skip the 9-iteration isdir/makedirs loop entirely.
        # Cuts steady-state cost from ~12ms to ~2ms (one opendir() vs 9 stat
        # syscalls). Falls back to the loop if scandir fails.
        all_exist_fast = False
        try:
            with os.scandir(root_path) as entries:
                found = {entry.name for entry in entries if entry.is_dir()}
            if all(subdir in found for subdir in SUBDIRS):
                existing = list(SUBDIRS)
                all_exist_fast = True
        except OSError:
            pass

        if not all_exist_fast:
            for subdir in SUBDIRS:
                full_path = os.path.join(root_path, subdir)
                if os.path.isdir(full_path):
                    existing.append(subdir)
                else:
                    try:
                        os.makedirs(full_path, exist_ok=True)
                        created.append(subdir)
                    except Exception as e:
                        failed.append({"path": subdir, "error": str(e)})

        # Create a README
        readme_path = os.path.join(root_path, "README.txt")
        if not os.path.exists(readme_path):
            with open(readme_path, "w") as f:
                f.write(f"""Vermillion LightFury — Shared Results Directory
================================================
Created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Project: Vermillion LightFury (Azazael × Kali Orchestration)

Windows Path:  {SHARED_ROOT_WIN}
Kali WSL Path: {SHARED_ROOT_WSL}
Ubuntu WSL:    {SHARED_ROOT_WSL}

Subdirectories:
  nmap_scans/         — Nmap XML/JSON scan output
  health_reports/      — Kali health check snapshots
  tool_outputs/        — ffuf, gobuster, sqlmap, etc.
  metasploit_logs/     — MSF console session logs
  packet_captures/     — tcpdump/scapy pcaps
  wordlists_shared/    — Shared wordlists
  exploit_outputs/     — Exploit results
  azazael_events/      — Cross-environment event exchange
  temp/                — Temporary files (auto-cleaned)

This directory is accessible from:
  - Windows: {SHARED_ROOT_WIN}
  - Kali WSL: {SHARED_ROOT_WSL}
  - Ubuntu WSL: {SHARED_ROOT_WSL}
""")

        result = {
            "root_path": SHARED_ROOT_WIN,
            "wsl_path": SHARED_ROOT_WSL,
            "created": len(created),
            "existing": len(existing),
            "failed": len(failed),
            "failed_details": failed,
            "subdirs": SUBDIRS,
            "root_ok": True,
        }

        self._record("directories_ensured", result)
        return result

    def verify_access(self) -> dict[str, Any]:
        """Verify the shared directory is accessible from all environments.

        Returns dict with per-environment test results.
        """
        results: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "windows": {"accessible": False, "details": ""},
            "kali_wsl": {"accessible": False, "details": ""},
            "ubuntu_wsl": {"accessible": False, "details": ""},
            "all_ok": False,
        }

        # Windows test
        if os.path.isdir(SHARED_ROOT_WIN):
            try:
                test_file = os.path.join(SHARED_ROOT_WIN, "temp", f"_access_test_{self._session_id}.txt")
                with open(test_file, "w") as f:
                    f.write("Windows write test OK")
                with open(test_file, "r") as f:
                    content = f.read()
                os.remove(test_file)
                results["windows"] = {"accessible": True, "details": "Read/write OK"}
            except Exception as e:
                results["windows"] = {"accessible": False, "details": str(e)}
        else:
            results["windows"] = {"accessible": False, "details": f"Path not found: {SHARED_ROOT_WIN}"}

        # Kali WSL test
        rc, stdout, stderr = self._wsl_exec(
            "kali",
            f"test -d {SHARED_ROOT_WSL} && echo 'EXISTS' && "
            f"echo 'test' > {SHARED_ROOT_WSL}/temp/_kali_test.txt && "
            f"cat {SHARED_ROOT_WSL}/temp/_kali_test.txt && "
            f"rm {SHARED_ROOT_WSL}/temp/_kali_test.txt",
            timeout_sec=15,
        )
        results["kali_wsl"] = {
            "accessible": rc == 0 and "test" in stdout,
            "details": stdout[:200] if rc == 0 else (stdout or stderr)[:200],
        }

        # Ubuntu WSL test
        rc2, stdout2, stderr2 = self._wsl_exec(
            "Ubuntu-26.04",
            f"test -d {SHARED_ROOT_WSL} && echo 'EXISTS' && "
            f"echo 'test' > {SHARED_ROOT_WSL}/temp/_ubuntu_test.txt && "
            f"cat {SHARED_ROOT_WSL}/temp/_ubuntu_test.txt && "
            f"rm {SHARED_ROOT_WSL}/temp/_ubuntu_test.txt",
            timeout_sec=15,
        )
        results["ubuntu_wsl"] = {
            "accessible": rc2 == 0 and "test" in stdout2,
            "details": stdout2[:200] if rc2 == 0 else (stdout2 or stderr2)[:200],
        }

        results["all_ok"] = all(
            results[env]["accessible"]
            for env in ["windows", "kali_wsl", "ubuntu_wsl"]
        )

        self._record(
            "access_verified",
            results,
            severity="info" if results["all_ok"] else "warning",
        )

        return results

    def translate_path(self, path: str, target: str = "wsl") -> str:
        """Translate paths between Windows and WSL.

        Args:
            path: Path to translate
            target: 'wsl' (Windows→WSL) or 'win' (WSL→Windows)

        Returns translated path string.
        """
        if target == "wsl":
            # G:\path → /mnt/g/path
            if path.upper().startswith("G:"):
                return "/mnt/g" + path[2:].replace("\\", "/")
            elif path.upper().startswith("C:"):
                return "/mnt/c" + path[2:].replace("\\", "/")
            elif path.upper().startswith("E:"):
                return "/mnt/e" + path[2:].replace("\\", "/")
            return path.replace("\\", "/")

        elif target == "win":
            # /mnt/g/path → G:\path
            if path.startswith("/mnt/g/"):
                return "G:\\" + path[7:].replace("/", "\\")
            elif path.startswith("/mnt/c/"):
                return "C:\\" + path[7:].replace("/", "\\")
            elif path.startswith("/mnt/e/"):
                return "E:\\" + path[7:].replace("/", "\\")
            return path.replace("/", "\\")

        return path

    def get_shared_path(self, subdir: str | None = None) -> dict[str, str]:
        """Get paths to shared directory in all formats."""
        base_win = SHARED_ROOT_WIN if subdir is None else os.path.join(SHARED_ROOT_WIN, subdir)
        base_wsl = SHARED_ROOT_WSL if subdir is None else f"{SHARED_ROOT_WSL}/{subdir}"
        return {
            "windows": base_win,
            "kali": base_wsl,
            "ubuntu": base_wsl,
        }

    def run_setup(self) -> dict[str, Any]:
        """Run full setup: create dirs + verify access.

        Returns comprehensive status report.
        """
        dirs = self.ensure_directories()
        access = self.verify_access()
        return {
            "directories": dirs,
            "access": access,
            "timestamp": datetime.now().isoformat(),
            "version": self.VERSION,
            "project": "Vermillion LightFury",
        }


# ── CLI ───────────────────────────────────────────────────────────────────

# ── Shared .rc cleanup utility (used by MetasploitBridge + KaliHealth) ────────

def _record_pams_purge(event_type: str, data: dict[str, Any], severity: str = "info"):
    """Module-level PAMS recorder for purge_stale_rc_scripts() (Phase 11.3 N2 nit).

    Mirrors per-worm `_record_pams` method pattern but at module scope —
    purge_stale_rc_scripts() is a module-level function (no `self`), so it
    cannot use the worm-class `self._pams` indirection. Instead, the module-
    level `_pams` global (top of file) is used.

    Source for all emitted events is `"SharedDirManager"` (distinguishes from
    per-worm senders). Event prefix is `shared_dir.purge_stale_rc_scripts.`
    so downstream consumers can filter the cleanup codepath specifically.

    Never throws — a PAMS backend outage cannot abort the cleanup's
    "never throws on cleanup failure" contract. Caller-friendly: pass plain
    data dicts; severity defaults to "info".

    Records emitted by this helper:
      - file_deleted       (per successful deletion; severity=info)
      - stuck_file_warning (per OSError on os.remove; severity=warning)
      - batch_completed    (summary after loop, only if deleted > 0; severity=info)
    """
    if _pams:
        try:
            _pams.record_event(
                f"shared_dir.purge_stale_rc_scripts.{event_type}",
                data,
                source="SharedDirManager",
                severity=severity,
            )
        except Exception:
            pass


def purge_stale_rc_scripts(log_dir: str = (r"C:\azazael_shared\metasploit_logs" if _IS_WINDOWS else "/mnt/c/azazael_shared/metasploit_logs"),
                           max_age_hours: int = 168,  # 7 days default
                           throttle_seconds: int = 3600) -> int:
    """Delete .rc resource scripts older than max_age_hours from a directory.

    Shared deduplicated utility called by both MetasploitBridge.cleanup_old_scripts()
    and KaliHealth._purge_stale_shared_logs().  Throttled to run at most once per
    throttle_seconds to avoid unnecessary directory scans.

    Returns number of files deleted (0 if throttled or no files found).
    """
    # Throttle — atomic read-check + claim under _RC_PURGE_LOCK so concurrent
    # callers under 3.14t free-threading cannot both pass the boundary check
    # and double-delete. File deletion runs outside the lock: listdir is the
    # source of truth and deleted files don't reappear.
    # `global` is required because we read AND rebind the timestamp; bare
    # assignment without `global` would shadow it as a local. The lock
    # serializes the read-check + claim sequence.
    global _LAST_RC_PURGE_TIME
    with _RC_PURGE_LOCK:
        now = time.time()
        if _LAST_RC_PURGE_TIME > 0 and (now - _LAST_RC_PURGE_TIME) < throttle_seconds:
            return 0
        _LAST_RC_PURGE_TIME = now

    if not os.path.isdir(log_dir):
        return 0

    cutoff = now - (max_age_hours * 3600)
    deleted = 0
    try:
        # Phase 10.10: use os.scandir() so each per-file stat is cached by the
        # dir-open syscall — eliminates the 2-per-file-syscall pattern in the
        # previous os.listdir + os.path.getmtime loop. On /mnt/c NTFS-3G each
        # individual stat was ~50ms; with scandir the same scan is a single
        # opendir() syscall regardless of file count.
        #
        # Phase 10.10.3: `follow_symlinks=False` deliberately does NOT follow
        # symlinks — diverges from the previous `os.path.getmtime()` behavior
        # (which called `os.stat`, default follow-symlinks). For `.rc` files
        # (always plain files written via `open(path, "w")`) this is irrelevant
        # in practice; flagging the divergence so future maintainers don't get
        # confused if symlinked `.rc` files ever appear.
        with os.scandir(log_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(".rc"):
                    continue
                try:
                    # Phase 11.3 (micro-opt, reviewer nit): cache the stat
                    # result so `age_seconds` doesn't re-call entry.stat() a
                    # second time. On /mnt/c NTFS-3G each stat is ~50ms; this
                    # halves the per-deleted-file syscall cost — same
                    # syscall-reduction rationale as Phase 10.10's scandir
                    # refactor.
                    stat = entry.stat(follow_symlinks=False)
                    if stat.st_mtime < cutoff:
                        os.remove(entry.path)
                        deleted += 1
                        age_seconds = now - stat.st_mtime
                        # Phase 11.3 N2: emit per-file PAMS audit record so
                        # deletion events surface in PAMS (operator alerting
                        # alongside the stderr log line).
                        _record_pams_purge("file_deleted", {
                            "log_dir": log_dir,
                            "deleted_path": entry.path,
                            "age_seconds": round(age_seconds, 1),
                            "max_age_hours": max_age_hours,
                        })
                except OSError as e:
                    # Phase 10.10.3: surface stuck-file errors to operator awareness.
                    # Without this, a chmod-0444 .rc file (or any other OSError-cause
                    # like a held file lock) would disappear silently from operator
                    # visibility. Logs to stderr so CI logs / PAMS audit pick it up.
                    print(
                        f"[purge_stale_rc_scripts] cannot delete {entry.path}: {e}",
                        file=sys.stderr,
                    )
                    # Phase 11.3: mirror the stuck-file event into PAMS so a PAMS-
                    # based alert pipeline (severity=warning) can fire without forcing
                    # operators to grep CI stderr logs. Best-effort + never-crash.
                    _record_pams_purge("stuck_file_warning", {
                        "log_dir": log_dir,
                        "blocked_path": entry.path,
                        "error": str(e),
                    }, severity="warning")
    except Exception:
        pass  # Never throw on cleanup failure

    # Phase 11.3: batch summary record — emitted only when something was
    # actually deleted. Zero-deletion cases (all fresh, all throttled, all
    # stuck) emit no batch event because nothing meaningful happened from
    # an audit-trail perspective.
    if deleted > 0:
        _record_pams_purge("batch_completed", {
            "log_dir": log_dir,
            "deleted_count": deleted,
            "max_age_hours": max_age_hours,
        })

    return deleted


# Thread-safe throttle for purge_stale_rc_scripts() — pair (timestamp, lock).
# Concurrent callers under 3.14t free-threading can no longer both pass the
# throttle check at the boundary case. See function body for the critical
# section. Lock acquired only for read-check + claim; purge itself is O(n)
# directory walk and stays outside the lock.
_RC_PURGE_LOCK = threading.Lock()
_LAST_RC_PURGE_TIME: float = 0.0


# ── Shared Kali liveness verifier (used by NmapScanner + MetasploitBridge) ──

def verify_kali_responsive(
    wsl_exec_fn,
    record_pams_fn,
    event_prefix: str,
    timeout_sec: int = 15,
) -> None:
    """Verify Kali WSL is reachable. Attempts auto-recovery if not.
    
    Shared deduplicated utility called by both NmapScanner._verify_kali_responsive()
    and MetasploitBridge._verify_kali_responsive().
    
    Args:
        wsl_exec_fn: Callable(command_list, timeout_sec) -> (rc, stdout, stderr)
        record_pams_fn: Callable(event_type, data_dict, severity) -> None
        event_prefix: PAMS event prefix (e.g. 'nmap_scanner' or 'metasploit_bridge')
        timeout_sec: Initial liveness check timeout
    
    Raises:
        KaliUnresponsiveError if recovery fails.
    """
    rc, stdout, _ = wsl_exec_fn("echo OK", timeout_sec=timeout_sec)
    if rc == 0 and "OK" in stdout:
        return  # All good
    
    # Attempt auto-recovery once
    record_pams_fn(f"{event_prefix}.kali_unresponsive", {"rc": rc, "stdout": stdout[:200]}, severity="warning")
    try:
        from worms.wsl_recovery import WSLRecovery
    except ImportError as e:
        record_pams_fn(f"{event_prefix}.wsl_recovery_import_failed", {"error": str(e)}, severity="error")
        # Import KaliUnresponsiveError (from worms.exceptions — Phase 13.1)
        from worms.exceptions import KaliUnresponsiveError
        raise KaliUnresponsiveError(
            f"Kali WSL unresponsive and WSLRecovery unavailable: {e}"
        )
    try:
        wr = WSLRecovery()
        recovery = wr.recover(auto_restart=True)
        if recovery.actions_taken:
            record_pams_fn(f"{event_prefix}.auto_recovery_attempted", {
                "actions": [a.to_dict() for a in recovery.actions_taken],
            })
    except Exception as e:
        record_pams_fn(f"{event_prefix}.auto_recovery_failed", {"error": str(e)}, severity="error")
    
    # Retest
    rc2, stdout2, _ = wsl_exec_fn("echo OK", timeout_sec=30)
    if rc2 != 0 or "OK" not in stdout2:
        from worms.exceptions import KaliUnresponsiveError
        raise KaliUnresponsiveError(
            f"Kali WSL unresponsive after auto-recovery (rc={rc2}). "
            f"Run WSLRecovery manually: wsl -d kali -- bash"
        )


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="SharedDir — Cross-Platform Results Directory Manager (Vermillion LightFury)",
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    setup_p = sub.add_parser("setup", help="Create directories and verify access")
    setup_p.add_argument("--json", action="store_true", help="Output as JSON")

    check_p = sub.add_parser("check", help="Verify access from all environments")

    sub.add_parser("paths", help="Show all shared paths")

    translate_p = sub.add_parser("translate", help="Translate path between Windows ↔ WSL")
    translate_p.add_argument("path", help="Path to translate")
    translate_p.add_argument("--to", choices=["wsl", "win"], default="wsl", help="Target format")

    args = parser.parse_args()
    sdm = SharedDirManager()

    if args.command == "setup":
        result = sdm.run_setup()
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            dirs = result["directories"]
            access = result["access"]
            print(f"Shared Directory Setup — {result['timestamp']}")
            print(f"  Root: {dirs['root_path']}")
            print(f"  Created: {dirs['created']} | Existing: {dirs['existing']} | Failed: {dirs['failed']}")
            print(f"  Access:")
            for env in ["windows", "kali_wsl", "ubuntu_wsl"]:
                a = access[env]
                print(f"    {env:<15}: {'OK' if a['accessible'] else 'FAIL — ' + a['details'][:80]}")

    elif args.command == "check":
        access = sdm.verify_access()
        all_ok = all(access[env]["accessible"] for env in ["windows", "kali_wsl", "ubuntu_wsl"])
        print(f"All environments OK: {'YES' if all_ok else 'NO'}")
        for env in ["windows", "kali_wsl", "ubuntu_wsl"]:
            a = access[env]
            print(f"  {env:<15}: {'+' if a['accessible'] else 'X'} {a['details'][:60]}")

    elif args.command == "paths":
        paths = sdm.get_shared_path()
        print("Shared Directory Paths:")
        for env, path in paths.items():
            print(f"  {env}: {path}")

    elif args.command == "translate":
        result = sdm.translate_path(args.path, target=args.to)
        print(f"  Input:  {args.path}")
        print(f"  Output: {result}")

    else:
        print(f"SharedDir v{sdm.VERSION} — Vermillion LightFury")
        print(f"  Commands: setup, check, paths, translate")
        print(f"  Windows:  {SHARED_ROOT_WIN}")
        print(f"  WSL:      {SHARED_ROOT_WSL}")
        print(f"  Subdirs:  {len(SUBDIRS)}")


if __name__ == "__main__":
    main()
