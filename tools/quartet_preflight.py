# coding: utf-8
"""
tools/quartet_preflight.py — Quartet Architecture Pre-Flight Gate
=================================================================

Phase 14.3: CI / session-start gate that asserts all 4 quartet pillars
are reachable, with `vermillion_lightfury` deployable on each. Returns
loud failure (nonzero exit + colored table) when any pillar is missing,
so a degraded environment is never silently worked in.

Pillars
-------
KALI    Canonical WSL distro/filesystem boot rootfs (this host)
/mnt/c/Users/clint/vermillion_lightfury
                                  Windows 11 user-clint drvfs sync
/mnt/wsl/Ubuntu/.../vermillion_lightfury
                                  WSL Ubuntu side (bridges mount only when Running)
/mnt/g/vermillion_lightfury       G:\\ archive drive mount (drvfs, writable)

Detection strictness
--------------------
Default (lenient):
  Treats UBUNTU registered-but-STOPPED as a yellow WARN (exit 2).
  Treats UBUNTU absent from WSL or KALI missing as a hard FAIL (exit 1).
--strict:
  Any UBUNTU-down → exit 1. Use in CI to refuse to start when the
  quartet is incomplete.

Exit codes
----------
0  All 4 pillars reachable + vermillion detected on each.
1  ≥1 pillar OS-unreachable (no drvfs mount, no WSL distro, missing KALI).
2  ≥1 pillar reachable but vermillion reference missing/drift (lenient only).

Output modes
------------
(default)  human-readable table to stdout, errors to stderr
--json     JSON blob on stdout
--quiet    silent (exit-code only) — shell `&&` chain friendly
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence


# ── Pillar definitions ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Pillar:
    name: str
    vermillion_path: Path
    probe_kind: str  # 'fs' | 'wsl_registry' | 'drvfs_writable'


KALI = Pillar("KALI", Path("/root/vermillion_lightfury"), "fs")
UBUNTU = Pillar("UBUNTU", Path("/mnt/wsl/Ubuntu"), "wsl_registry")
W11 = Pillar("W11", Path("/mnt/c/Users/clint/vermillion_lightfury"), "fs")
G_DRIVE = Pillar("G", Path("/mnt/g/vermillion_lightfury"), "drvfs_writable")

ALL_PILLARS: Sequence[Pillar] = (KALI, UBUNTU, W11, G_DRIVE)


# ── Reports ───────────────────────────────────────────────────────────────


@dataclass
class PillarReport:
    name: str
    reachable: bool
    vermillion_present: bool
    severity: str  # 'pass' | 'warn' | 'fail'
    message: str
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Probes ────────────────────────────────────────────────────────────────


def _probe_wsl(timeout: int = 6) -> list[str]:
    """Run `wsl.exe -l -v` (with hard timeout) and return distro names.

    Invokes `wsl.exe` DIRECTLY (not via `cmd.exe`) because cmd.exe inherits
    the WSL UNC current-working-directory (e.g. \\\\wsl.localhost\\\\Kali\\\\...)
    which contaminates nested command resolution and breaks `wsl -l -v` when
    the call originates from inside Kali WSL. Setting `cwd="/mnt/c"` anchors
    the spawn in a clean Windows drvfs path so the WSL service can resolve
    it unambiguously. `encoding="utf-16le"` matches `wsl.exe`'s native
    output; bypassing cmd.exe bypasses the default code-page transcoding,
    so without utf-16 the NULL-byte-rich header breaks the parser's
    "NAME"/"VERSION" string-matching.

    Returns lowercase distro names that appeared after the header.
    Returns [] on timeout / wsl.exe inaccessible / parsing failure.
    """
    try:
        r = subprocess.run(
            ["wsl.exe", "-l", "-v"],
            capture_output=True, text=True, timeout=timeout,
            cwd="/mnt/c", encoding="utf-16le", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    raw = (r.stdout or "") + "\n" + (r.stderr or "")
    names: list[str] = []
    seen_header = False
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if not seen_header:
            if "NAME" in s and "VERSION" in s:
                seen_header = True
                continue
        # Strip leading '*' (default marker) + whitespace
        s = s.lstrip("*").strip()
        tokens = s.split()
        if not tokens:
            continue
        first = tokens[0].lower()
        if first in ("name", "state", "version"):
            continue
        names.append(first)
    return names


def _probe_path(path: Path) -> tuple[bool, bool, str]:
    """Probe `path` for fs-level existence and report vermillion_present when
    `path/worms` exists (or `path` is itself a vermillion tree root).

    Returns (reachable, vermillion_present, message).
    """
    try:
        exists = path.exists()
    except OSError as e:
        return False, False, f"OSError({e.errno}) on {path}: {e.strerror}"
    if not exists:
        return False, False, f"missing: {path}"
    # If `path/worms` exists, treat path as a vermillion tree root.
    try:
        if (path / "worms").is_dir():
            return True, True, f"reachable + worms/ at {path}"
        return True, False, f"reachable but no worms/ subtree at {path}"
    except OSError as e:
        return True, False, f"reachable but probe error on {path}: {e}"


def _probe_ubuntu_bridge() -> bool:
    """True iff /mnt/wsl/Ubuntu auto-bridge is mounted (Ubuntu is RUNNING)."""
    p = Path("/mnt/wsl/Ubuntu")
    try:
        return p.is_dir()
    except OSError:
        return False


def _probe_drvfs_writable(path: Path) -> tuple[bool, bool, str]:
    """Probe a drvfs mount + test writability with a tiny tmp file.

    Returns (reachable, vermillion_present, message).
    """
    try:
        if not path.exists():
            if path.parent.exists():
                return True, False, f"{path.parent} mounted but {path.name}/ missing"
            return False, False, f"missing: {path}"
    except OSError as e:
        return False, False, f"OSError on {path}: {e}"
    # Test writability with a throw-away probe file.
    try:
        probe = path / ".quartet_preflight_probe"
        probe.write_text("ok\n")
        probe.unlink()
    except OSError as e:
        return True, False, f"reachable but NOT writable ({e})"
    return True, True, f"reachable + writable at {path}"


# ── Pillar reporting ─────────────────────────────────────────────────────


def report_pillar(pillar: Pillar, strict: bool) -> PillarReport:
    if pillar.probe_kind == "fs":
        reachable, present, msg = _probe_path(pillar.vermillion_path)
    elif pillar.probe_kind == "wsl_registry":
        names = _probe_wsl()
        has_ubuntu = any("ubuntu" in n for n in names)
        bridge = _probe_ubuntu_bridge()
        if has_ubuntu and bridge:
            reachable, present = True, True
            msg = f"registered ({names}) + /mnt/wsl/Ubuntu mounted"
        elif has_ubuntu and not bridge:
            reachable, present = True, False
            # Auto-detect the actual registered Ubuntu distro name from the
            # `wsl -l -v` output so the hint matches whatever is registered.
            ubuntu_distro = next((n for n in names if "ubuntu" in n), "Ubuntu")
            msg = (
                f"registered ({names}) but STOPPED — "
                f"run `wsl -d {ubuntu_distro}` from Windows PowerShell"
            )
        else:
            reachable, present = False, False
            msg = f"no Ubuntu distro registered (saw {names!r})"
    elif pillar.probe_kind == "drvfs_writable":
        reachable, present, msg = _probe_drvfs_writable(pillar.vermillion_path)
    else:
        reachable, present = False, False
        msg = f"unknown probe {pillar.probe_kind!r}"

    # Severity logic — strict mode elevates UBUNTU-warn to fail
    if not reachable:
        severity = "fail"
    elif not present:
        if pillar.probe_kind == "wsl_registry" and not strict:
            severity = "warn"
        else:
            severity = "fail"
    else:
        severity = "pass"

    return PillarReport(
        name=pillar.name,
        reachable=reachable,
        vermillion_present=present,
        severity=severity,
        message=msg,
    )


# ── Output rendering ─────────────────────────────────────────────────────


def render_table(reports: list[PillarReport]) -> str:
    green = "\033[32m"
    yellow = "\033[33m"
    red = "\033[31m"
    dim = "\033[2m"
    reset = "\033[0m"

    def cell(severity: str, label: str) -> str:
        if severity == "pass":
            return f"{green}{label}{reset}"
        if severity == "warn":
            return f"{yellow}{label}{reset}"
        return f"{red}{label}{reset}"

    lines = [
        f"  {'PILLAR':<10} {'STATE':<6} {'VERMILLION':<11} MESSAGE",
        f"  {'-' * 10} {'-' * 6} {'-' * 11} {'-' * 60}",
    ]
    for r in reports:
        state_label = r.severity.upper()
        vm_label = "yes" if r.vermillion_present else "no"
        lines.append(
            f"  {r.name:<10} "
            f"{cell(r.severity, state_label):<6} "
            f"{cell(r.severity, vm_label):<11} "
            f"{r.message}"
        )
    return "\n".join(lines)


# ── main() ──────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="quartet_preflight.py",
        description="Quartet architecture pre-flight gate.",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    ap.add_argument("--quiet", action="store_true", help="silent except nonzero exit")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="treat UBUNTU stopped state as fail (no warn)",
    )
    args = ap.parse_args(argv)

    reports = [report_pillar(p, args.strict) for p in ALL_PILLARS]

    any_fail = any(r.severity == "fail" for r in reports)
    any_warn = any(r.severity == "warn" for r in reports)

    if args.json:
        out = {
            "all_reachable": all(r.reachable for r in reports),
            "vermillion_present_on_all": all(r.vermillion_present for r in reports),
            "pillars": [r.to_dict() for r in reports],
            "exit_code_hint": 1 if any_fail else 2 if any_warn else 0,
        }
        print(json.dumps(out, indent=2))
    elif not args.quiet:
        print(render_table(reports))
        print()
        if any_fail:
            print(
                "  [FAIL] One or more pillars unreachable. "
                "Refusing to start session.",
                file=sys.stderr,
            )
        elif any_warn:
            print(
                "  [WARN] Some pillars reachable but vermillion reference "
                "missing. Re-run with --strict to fail loud.",
                file=sys.stderr,
            )
        else:
            print("  [PASS] All 4 quartet pillars reachable + vermillion detected.")

    if any_fail:
        return 1
    if any_warn:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
