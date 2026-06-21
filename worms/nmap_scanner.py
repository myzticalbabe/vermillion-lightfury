# coding: utf-8
"""
NmapScanner — Kali nmap Integration Worm for Azazael
=====================================================
Wraps 'wsl -d kali nmap' with structured output parsing,
VulnScanner cross-referencing, and PamsRecorder audit trail.

Features:
- WSL subprocess dispatch: runs nmap inside Kali WSL from Windows
- XML output parsing: extracts hosts, ports, services, OS guesses
- VulnScanner integration: cross-references findings with CVE patterns
- PAMS memory: every scan recorded with full audit trail
- Parallel scanning: free-threaded pool for multi-target scans
- Scan types: -sS (SYN), -sV (version), -sC (default scripts),
  --script vuln (vuln detection), -A (aggressive)

Usage:
    >>> from vermillion_lightfury.worms.nmap_scanner import NmapScanner
    >>> ns = NmapScanner()
    >>> result = ns.scan("192.168.1.0/24", scan_type="syn")
    >>> print(result.summary)

Registry:
    @register_worm(category="security", priority=8, cooldown_seconds=45.0,
                   description="Kali nmap scanner — WSL-dispatched port/vuln scanning with PAMS audit")
"""

import sys
import os
import json
import time
import socket
import subprocess
import xml.etree.ElementTree as ET
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
# G: drive paths (exFAT, may not be WSL-accessible)
_SHARED_DIR_G = "G:\\azazael_shared" if _IS_WINDOWS else "/mnt/g/azazael_shared"
_WSL_SHARED_G = "/mnt/g/azazael_shared"

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


# ── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class NmapPort:
    """Single port finding from an nmap scan."""
    port: int
    protocol: str          # tcp / udp
    state: str             # open / filtered / closed
    service: str = ""
    version: str = ""
    product: str = ""
    cpe: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port, "protocol": self.protocol,
            "state": self.state, "service": self.service,
            "version": self.version, "product": self.product,
        }

    @property
    def is_vulnerable_service(self) -> bool:
        """Heuristic: known-vulnerable service banners."""
        risky = [
            "vsftpd 2.3.4", "openssh 4.", "apache 2.2", "apache 2.4.7",
            "proftpd 1.3.5", "smb 3.0", "mysql 5.1", "telnet",
            "rpcbind", "java-rmi", "distcc", "unrealircd",
        ]
        return any(r in f"{self.service} {self.version}".lower() for r in risky)


@dataclass
class NmapHost:
    """Single host result from an nmap scan."""
    ip: str
    hostname: str = ""
    state: str = "unknown"       # up / down
    ports: list[NmapPort] = field(default_factory=list)
    os_guess: str = ""
    os_accuracy: int = 0
    mac: str = ""
    scan_duration: float = 0.0

    @property
    def open_port_count(self) -> int:
        return sum(1 for p in self.ports if p.state == "open")

    @property
    def vulnerable_port_count(self) -> int:
        return sum(1 for p in self.ports if p.state == "open" and p.is_vulnerable_service)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip, "hostname": self.hostname,
            "state": self.state, "open_ports": self.open_port_count,
            "ports": [p.to_dict() for p in self.ports],
            "os_guess": self.os_guess, "mac": self.mac,
        }


@dataclass
class NmapScanResult:
    """Complete nmap scan result with metadata."""
    target: str
    command: str
    scan_type: str
    start_time: str
    end_time: str
    duration_seconds: float
    hosts_scanned: int
    hosts_up: int
    hosts: list[NmapHost] = field(default_factory=list)
    scan_stats: dict[str, Any] = field(default_factory=dict)

    @property
    def total_open_ports(self) -> int:
        return sum(h.open_port_count for h in self.hosts)

    @property
    def total_vulnerable_ports(self) -> int:
        return sum(h.vulnerable_port_count for h in self.hosts)

    def summary(self) -> dict[str, Any]:
        return {
            "target": self.target, "scan_type": self.scan_type,
            "duration_sec": round(self.duration_seconds, 2),
            "hosts_scanned": self.hosts_scanned, "hosts_up": self.hosts_up,
            "open_ports": self.total_open_ports,
            "vulnerable_services": self.total_vulnerable_ports,
        }


# ── NmapScanner Worm ────────────────────────────────────────────────────────


@register_worm(
    category="security",
    priority=8,
    cooldown_seconds=45.0,
    description="Kali nmap scanner — WSL-dispatched port/vulnerability scanning with VulnScanner cross-reference and PAMS audit trail",
)
class NmapScanner(ThreadSafeWormMixin, BaseWormMixin):
    """Worm that executes nmap scans inside Kali WSL from Windows Azazael.

    Integrates with:
    - VulnScanner worm (cross-reference findings with CVE DB)
    - PamsRecorder (complete audit trail of every scan)
    - G: shared directory (scan output exchange between Windows ↔ WSL)
    """

    VERSION = "1.0.0"
    __summary_method__ = "get_summary"

    SCAN_TYPES = {
        "syn":       ["-sS", "-T4"],
        "version":   ["-sV", "--version-intensity", "5"],
        "scripts":   ["-sC"],
        "vuln":      ["--script", "vuln"],
        "aggressive": ["-A"],
        "udp":       ["-sU", "--top-ports", "100"],
        "ping":      ["-sn"],
        "quick":     ["-sS", "-T5", "--top-ports", "20"],
        "full":      ["-sS", "-sV", "-sC", "-A", "-O"],
    }

    def __init__(self):
        super().__init__()  # F4: initialise self._lock via ThreadSafeWormMixin (wraps _targets_scanned, _total_scans, _total_open_ports)
        self._pams = _pams        # Phase 11.5 — µs+uuid8 hex for collision-free 3.14t barrier-sync ids.
        # Format: YYYYMMDD_HHMMSS_ffffff_<8-hex> (31 chars; uuid suffix defeats
        # clock_gettime-micros-tick collisions under free-threaded race).
        self._session_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        # Phase 12.1 — len==31 regression guard. Filesystem consumers (nmap_<sid>_<ts>.xml,
        # msf_<sid>_<ts>.rc, _access_test_<sid>.txt) and PAMS payloads assume 31 chars.
        # A future refactor that drops the uuid suffix, shortens timestamp, or appends
        # underscore-chars would silently break filename length invariants. Fail-fast at
        # __init__ is cheaper than at file-write race time. Asserts are skipped under
        # python -O / Nuitka .exe optimization mode; production CSV/Jenkins runs use -O.
        assert len(self._session_id) == 31, (
            f"_session_id {self._session_id!r} is not 31 chars "
            f"(format regressed — see MILESTONE Phase 11.5 v2 / Phase 12.1)"
        )
        self._targets_scanned: list[str] = [] 
        self._total_scans: int = 0
        self._total_open_ports: int = 0

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
                # Running from Windows — wrap in WSL
                wsl_cmd = ["wsl.exe", "-d", "kali", "--"] + cmd_args
                result = subprocess.run(
                    wsl_cmd, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return (-1, "", f"TIMEOUT after {timeout_sec}s")
        except FileNotFoundError:
            return (-2, "", "WSL not found — is Kali WSL installed?" if not _IS_KALI_NATIVE else "Command not found")
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
            event_prefix="nmap_scanner",
        )

    def _record_pams(self, event_type: str, data: dict[str, Any], severity: str = "info"):
        """Record to PAMS memory."""
        if self._pams:
            try:
                self._pams.record_event(
                    f"nmap_scanner.{event_type}", data,
                    source="NmapScanner", severity=severity,
                )
            except Exception:
                pass

    # ── Scan Execution ──────────────────────────────────────────────────────

    def scan(
        self,
        target: str,
        scan_type: str = "syn",
        extra_args: list[str] | None = None,
        timeout_sec: int = 600,
        output_xml: bool = True,
    ) -> NmapScanResult:
        """Execute an nmap scan against a target via Kali WSL.

        Args:
            target: IP, subnet, or hostname to scan
            scan_type: One of SCAN_TYPES keys (syn, version, scripts, vuln, etc.)
            extra_args: Additional nmap arguments
            timeout_sec: Timeout for the scan
            output_xml: If True, parse XML output; otherwise parse human-readable

        Returns:
            NmapScanResult with structured findings

        Raises:
            KaliUnresponsiveError: if Kali is down and auto-recovery fails
            RuntimeError: if E: drive critically low (circuit breaker)
        """
        # ── Pre-flight gates ──
        self._verify_kali_responsive()

        from worms.kali_health import KaliHealth
        ok, reason = KaliHealth.preflight_check()
        if not ok:
            raise RuntimeError(f"Circuit breaker: {reason}")
        elif "low" in reason.lower():
            self._record_pams("low_space_warning", {"reason": reason}, severity="warning")

        start_time = datetime.now()

        # Build nmap command
        scan_args = list(self.SCAN_TYPES.get(scan_type, self.SCAN_TYPES["syn"]))
        if extra_args:
            scan_args.extend(extra_args)

        # XML output for structured parsing — use consistent timestamp and fallback path
        ts = int(time.time())
        wsl_shared = _WSL_SHARED
        xml_path = f"{wsl_shared}/nmap_{self._session_id}_{ts}.xml"
        nmap_cmd = ["nmap"] + scan_args + ["-oX", xml_path, target]

        self._record_pams("scan_started", {
            "target": target, "scan_type": scan_type,
            "command": " ".join(nmap_cmd),
        })

        # Run scan
        returncode, stdout, stderr = self._wsl_exec(nmap_cmd, timeout_sec)

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        # Parse results
        hosts: list[NmapHost] = []

        if output_xml:
            # Try reading XML from shared dir first
            win_shared = _SHARED_DIR
            win_xml = os.path.join(win_shared, f"nmap_{self._session_id}_{ts}.xml")
            if returncode == 0 and os.path.exists(win_xml):
                with open(win_xml, "r", encoding="utf-8", errors="replace") as f:
                    hosts = self._parse_nmap_xml(f.read())
            elif stdout and returncode == 0:
                # Fallback: parse XML from stdout
                try:
                    hosts = self._parse_nmap_xml(stdout)
                except Exception:
                    pass

        if not hosts:
            # Fallback: parse human-readable output
            hosts = self._parse_nmap_text(stdout)

        # Build result
        hosts_up = sum(1 for h in hosts if h.state == "up")
        result = NmapScanResult(
            target=target,
            command=" ".join(nmap_cmd),
            scan_type=scan_type,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            duration_seconds=duration,
            hosts_scanned=len(hosts),
            hosts_up=hosts_up,
            hosts=hosts,
            scan_stats={
                "returncode": returncode,
                "open_ports_total": sum(h.open_port_count for h in hosts),
                "vulnerable_services": sum(h.vulnerable_port_count for h in hosts),
                "stderr_length": len(stderr),
            },
        )

        # Update worm stats — all under F4 lock so concurrent scanner instances
        # cannot lose increments or append during concurrent scan() calls.
        with self._lock:
            self._total_scans += 1
            self._targets_scanned.append(target)
            self._total_open_ports += result.total_open_ports

        # PAMS audit
        self._record_pams("scan_completed", {
            "target": target, "scan_type": scan_type,
            "duration_sec": round(duration, 2),
            "hosts_up": hosts_up, "open_ports": result.total_open_ports,
            "vulnerable_services": result.total_vulnerable_ports,
        })

        # VulnScanner cross-reference (record for downstream analysis)
        if result.total_vulnerable_ports > 0:
            self._record_pams("vulnerable_services_detected", {
                "target": target,
                "count": result.total_vulnerable_ports,
                "services": [
                    {"host": h.ip, "ports": [
                        {"port": p.port, "service": p.service, "version": p.version}
                        for p in h.ports if p.is_vulnerable_service
                    ]}
                    for h in hosts if h.vulnerable_port_count > 0
                ],
            }, severity="warning")

        return result

    def scan_multi(
        self,
        targets: list[str],
        scan_type: str = "syn",
        max_workers: int = 4,
        timeout_sec: int = 600,
    ) -> dict[str, NmapScanResult]:
        """Scan multiple targets in parallel using free-threaded pool.

        Args:
            targets: List of IPs/subnets to scan
            scan_type: Scan type for all targets
            max_workers: Max concurrent WSL nmap instances
            timeout_sec: Per-scan timeout

        Returns:
            Dict mapping target -> NmapScanResult
        """
        results: dict[str, NmapScanResult] = {}
        self._record_pams("multi_scan_started", {
            "targets": len(targets), "scan_type": scan_type, "workers": max_workers,
        })

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.scan, t, scan_type, timeout_sec=timeout_sec): t
                for t in targets
            }
            for future in as_completed(futures):
                target = futures[future]
                try:
                    results[target] = future.result(timeout=timeout_sec + 60)
                except Exception as e:
                    self._record_pams("scan_failed", {
                        "target": target, "error": str(e),
                    }, severity="error")
                    results[target] = NmapScanResult(
                        target=target, command="", scan_type=scan_type,
                        start_time="", end_time="", duration_seconds=0,
                        hosts_scanned=0, hosts_up=0,
                    )

        self._record_pams("multi_scan_completed", {
            "targets_total": len(targets), "successful": len(results),
        })

        return results

    # ── XML Parsing ───────────────────────────────────────────────────────

    def _parse_nmap_xml(self, xml_content: str) -> list[NmapHost]:
        """Parse nmap XML output into structured NmapHost objects."""
        hosts: list[NmapHost] = []
        try:
            root = ET.fromstring(xml_content)
            for host_elem in root.findall("host"):
                # Status
                status_elem = host_elem.find("status")
                state = status_elem.get("state", "unknown") if status_elem is not None else "unknown"

                # Address
                addr_elem = host_elem.find("address")
                ip = addr_elem.get("addr", "") if addr_elem is not None else ""
                addr_type = addr_elem.get("addrtype", "") if addr_elem is not None else ""

                hostname = ""
                hostnames_elem = host_elem.find("hostnames")
                if hostnames_elem is not None:
                    hn_elem = hostnames_elem.find("hostname")
                    if hn_elem is not None:
                        hostname = hn_elem.get("name", "")

                # MAC
                mac = ""
                for addr in host_elem.findall("address"):
                    if addr.get("addrtype") == "mac":
                        mac = addr.get("addr", "")
                        break

                # Ports
                ports: list[NmapPort] = []
                ports_elem = host_elem.find("ports")
                if ports_elem is not None:
                    for port_elem in ports_elem.findall("port"):
                        port_id = int(port_elem.get("portid", 0))
                        protocol = port_elem.get("protocol", "tcp")

                        state_elem = port_elem.find("state")
                        port_state = state_elem.get("state", "unknown") if state_elem is not None else "unknown"

                        service_elem = port_elem.find("service")
                        service = ""
                        version = ""
                        product = ""
                        cpe = ""
                        if service_elem is not None:
                            service = service_elem.get("name", "")
                            product = service_elem.get("product", "")
                            version = service_elem.get("version", "")
                            cpe_elem = service_elem.find("cpe")
                            cpe = cpe_elem.text if cpe_elem is not None else ""

                        ports.append(NmapPort(
                            port=port_id, protocol=protocol, state=port_state,
                            service=service, version=version,
                            product=product, cpe=cpe,
                        ))

                # OS guess
                os_guess = ""
                os_accuracy = 0
                os_elem = host_elem.find("os")
                if os_elem is not None:
                    osm_elem = os_elem.find("osmatch")
                    if osm_elem is not None:
                        os_guess = osm_elem.get("name", "")
                        os_accuracy = int(osm_elem.get("accuracy", 0))

                hosts.append(NmapHost(
                    ip=ip, hostname=hostname, state=state,
                    ports=ports, os_guess=os_guess,
                    os_accuracy=os_accuracy, mac=mac,
                ))
        except ET.ParseError as e:
            self._record_pams("xml_parse_error", {"error": str(e)}, severity="error")

        return hosts

    def _parse_nmap_text(self, text: str) -> list[NmapHost]:
        """Parse human-readable nmap output (fallback)."""
        hosts: list[NmapHost] = []
        current_host: NmapHost | None = None

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue

            # "Nmap scan report for 192.168.1.1"
            if "Nmap scan report for" in line:
                target = line.split("Nmap scan report for")[1].strip()
                ip = target.split()[0] if " " in target else target
                hostname = target if "(" in target else ""
                if "(" in hostname:
                    hostname = hostname[:hostname.index("(")].strip()
                current_host = NmapHost(ip=ip, hostname=hostname, state="up")
                hosts.append(current_host)

            # "22/tcp open ssh OpenSSH 8.9p1"
            elif current_host and "/tcp" in line or "/udp" in line:
                parts = line.split()
                port_str = parts[0].split("/")[0]
                protocol = parts[0].split("/")[1]
                state = parts[1] if len(parts) > 1 else "unknown"
                service = parts[2] if len(parts) > 2 else ""
                version = " ".join(parts[3:]) if len(parts) > 3 else ""

                try:
                    current_host.ports.append(NmapPort(
                        port=int(port_str), protocol=protocol,
                        state=state, service=service, version=version,
                    ))
                except ValueError:
                    pass

        return hosts

    # ── Discovery & Utility ───────────────────────────────────────────────

    def discover_live_hosts(self, subnet: str, timeout: int = 120) -> list[str]:
        """Ping-sweep a subnet to find live hosts.

        Returns list of IP addresses.
        """
        result = self.scan(subnet, scan_type="ping", timeout_sec=timeout)
        return [h.ip for h in result.hosts if h.state == "up"]

    def quick_top_ports(self, target: str) -> NmapScanResult:
        """Fast scan of top 100 ports."""
        return self.scan(target, scan_type="quick")

    def vulnerability_scan(self, target: str) -> NmapScanResult:
        """Run nmap vuln scripts against a target."""
        return self.scan(target, scan_type="vuln", timeout_sec=900)

    def full_scan(self, target: str) -> NmapScanResult:
        """Comprehensive scan: SYN + version + scripts + OS detection."""
        return self.scan(target, scan_type="full", timeout_sec=1800)

    # ── Summary ──────────────────────────────────────────────────────────

    def get_summary(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "project": "Vermillion LightFury",
            "session_id": self._session_id,
            "total_scans": self._total_scans,
            "total_open_ports": self._total_open_ports,
            "targets_scanned": len(self._targets_scanned),
            "scan_types_available": list(self.SCAN_TYPES.keys()),
            "pams_connected": self._pams is not None,
        }

    def execute(self) -> dict[str, Any]:
        """Standard worm execution entry point."""
        return {
            "success": True,
            "method": "get_summary",
            "result": self.get_summary(),
        }


# ── Export to JSON ────────────────────────────────────────────────────────

def save_scan_result(result: NmapScanResult, filepath: str | None = None):
    """Save scan result to JSON in the shared directory."""
    if filepath is None:
        filepath = os.path.join(_SHARED_DIR, f"scan_{result.target.replace('/', '_')}_{int(time.time())}.json")

    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({
            "target": result.target,
            "scan_type": result.scan_type,
            "duration_sec": result.duration_seconds,
            "hosts": [h.to_dict() for h in result.hosts],
            "summary": result.summary(),
        }, f, indent=2, default=str)

    return filepath


# ── CLI Entry Point ──────────────────────────────────────────────────────────
def main():
    """Phase 13.7: argparse-based CLI with required subparsers.

    Replaces the legacy __main__ block which silently ignored sys.argv.
    `python -m worms.nmap_scanner` (no args) still prints the banner via
    the len() fallback; unknown/invalid subcommands now exit 2 cleanly.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="NmapScanner — Kali Nmap Scanner (Vermillion LightFury)",
    )
    sub = parser.add_subparsers(dest="command", required=True, help="Command")

    scan_p = sub.add_parser("scan", help="Run an nmap scan")
    scan_p.add_argument("target", help="Target IP, subnet, or hostname")
    scan_p.add_argument(
        "--type", choices=list(NmapScanner.SCAN_TYPES), default="syn",
        help="Scan type (default: syn)",
    )
    scan_p.add_argument("--timeout", type=int, default=600, help="Per-scan timeout (sec)")

    sub.add_parser("summary", help="Print worm summary")
    sub.add_parser("preflight", help="Run E: drive preflight gate (KaliHealth)")

    # No-args UX: preserve the banner-printing behavior the existing F1/F5/F7
    # tests rely on. argparse required=True fires for missing/invalid subcommand,
    # but `python -m worms.nmap_scanner` (no args) still prints the banner.
    if len(sys.argv) == 1:
        ns = NmapScanner()
        print(f"NmapScanner v{NmapScanner.VERSION} — Vermillion LightFury")
        print(f"  Scan types: {', '.join(NmapScanner.SCAN_TYPES)}")
        print(f"  WSL Kali: wsl -d kali nmap ...")
        print(f"  Shared dir: {_SHARED_DIR}")
        print(f"  PAMS: {'connected' if _pams else 'unavailable'}")
        print(f"  Session: {ns._session_id}")
        return

    args = parser.parse_args()
    ns = NmapScanner()
    if args.command == "scan":
        # Phase 13.9: tighten the previous greedy `except Exception` so
        # real diagnostic tracebacks (RuntimeError, AttributeError,
        # MemoryError, etc.) propagate to Python's default sys.excepthook
        # with full backtrace — letting operators debug nmap/WSL failures
        # instead of getting a silently-swallowed "scan failed: X" line.
        # The narrow set below covers the known recoverable failure modes
        # (Kali unreachable, target unresolvable, bad arg validation); any
        # unexpected exception type still surfaces as a traceback + nonzero
        # exit, which is the correct CLI behavior for unrecovered errors.
        try:
            result = ns.scan(args.target, scan_type=args.type, timeout_sec=args.timeout)
            print(json.dumps(result.summary(), default=str, indent=2))
        except (KaliUnresponsiveError, OSError, ValueError, TypeError) as e:
            print(f"scan failed: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "summary":
        print(json.dumps(ns.get_summary(), default=str, indent=2))
    elif args.command == "preflight":
        from worms.kali_health import KaliHealth
        ok, reason = KaliHealth.preflight_check()
        print(f"preflight ok={ok!r}: {reason}")
        if not ok:
            sys.exit(1)
    # No `else:` branch: required=True guarantees args.command is valid.


if __name__ == "__main__":
    main()
