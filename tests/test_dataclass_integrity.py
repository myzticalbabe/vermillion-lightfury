# coding: utf-8
"""
tests/test_dataclass_integrity.py — Phase 13.4 Dataclass Integrity Tests
=========================================================================

Introspects EVERY public dataclass in every worm and verifies:

  D1  Class instantiation succeeds with required fields
  D2  ``.to_dict()`` returns a real ``dict`` (not None, not list, etc.)
  D3  ``json.dumps(.to_dict())`` succeeds — every produced dict is JSON-serializable
      (catches accidental insertion of non-serializable values like dataclasses, sets, bytes)
  D4  Computed properties return expected types (e.g. ``alert_level`` returns str)
  D5  Boundary values trigger expected classifications:
      - DriveStatus(free_gb<=1)   -> critical
      - DriveStatus(free_gb<=3)   -> warning
      - DriveStatus(free_gb>3)    -> ok
      - VHDXStatus(|growth|>500)  -> warning
      - KaliProcessStatus(ram>90) -> critical
      - KaliProcessStatus(ram>80) -> warning
  D6  overall_health aggregation: critical trumps warning trumps healthy

Dataclass inventory (14 total across 4 worms):
  worms.kali_health:         DriveStatus, VHDXStatus, KaliProcessStatus, KaliHealthStatus
  worms.nmap_scanner:        NmapPort, NmapHost, NmapScanResult
  worms.wsl_recovery:        WSLDistroStatus, VHDXFsckResult, RecoveryAction, RecoveryStatus
  worms.metasploit_bridge:   ExploitResult, PostModuleResult, SessionInfo
  worms.benchmark_worm:      (no dataclasses)
"""

import dataclasses
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── D1-D3: every to_dict() is dict + JSON-serializable ────────────────────

# Type-aware default factory for dataclass field values.
_TYPE_DEFAULTS: dict[type, Any] = {
    str: "x",
    int: 0,
    float: 0.0,
    bool: False,
    bytes: b"x",
    list: [],
    dict: {},
    tuple: (),
    set: set(),  # but sets aren't JSON-serializable — caller should special-case
}


def _field_default(field: dataclasses.Field) -> Any:
    """Return a type-aware default value for the dataclass field.

    Tries (in order):
      1. field.default if available (not MISSING)
      2. field.default_factory if available
      3. Type-based default from _TYPE_DEFAULTS by inspecting the field's type annotation
    """
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:
        try:
            return field.default_factory()
        except Exception:
            pass
    # Fallback: try type-based default
    if field.type in _TYPE_DEFAULTS:
        return _TYPE_DEFAULTS[field.type]
    return None


def _get_dataclasses_to_test() -> list[tuple[str, type]]:
    """Discover every dataclass in every worm module."""
    cases: list[tuple[str, type]] = []
    sourcery: list[tuple[str, list[str]]] = [
        ("worms.kali_health", ["DriveStatus", "VHDXStatus",
                                "KaliProcessStatus", "KaliHealthStatus"]),
        ("worms.nmap_scanner", ["NmapPort", "NmapHost", "NmapScanResult"]),
        ("worms.wsl_recovery", ["WSLDistroStatus", "VHDXFsckResult",
                                 "RecoveryAction", "RecoveryStatus"]),
        ("worms.metasploit_bridge", ["ExploitResult", "PostModuleResult",
                                       "SessionInfo"]),
    ]
    for mod_name, class_names in sourcery:
        mod = importlib.import_module(mod_name)
        for cls_name in class_names:
            cls = getattr(mod, cls_name)
            if dataclasses.is_dataclass(cls):
                cases.append((f"{mod_name}.{cls_name}", cls))
    return cases


def test_D1_D2_D3_every_to_dict_is_json_serializable() -> None:
    """For every dataclass that successfully instantiates:
        • to_dict()  returns a real dict
        • json.dumps(to_dict()) does not raise
    """
    cases = _get_dataclasses_to_test()
    assert len(cases) > 0, (
        "D1-D3: dataclass discovery found zero cases — test is wrong or "
        "all dataclasses became uninstantiable."
    )
    failures: list[tuple[str, str]] = []
    instantiated: list[tuple[str, type]] = []
    for label, cls in cases:
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            kwargs[f.name] = _field_default(f)
        try:
            inst = cls(**kwargs)
        except (TypeError, ValueError) as e:
            # Skip classes we can't instantiate cleanly — surface in failure list
            failures.append((label, f"instantiation raised {type(e).__name__}: {e}"))
            continue
        instantiated.append((label, cls))

        if not hasattr(inst, "to_dict"):
            continue
        try:
            d = inst.to_dict()
        except Exception as e:
            failures.append((label, f"to_dict raised {type(e).__name__}: {e}"))
            continue
        if not isinstance(d, dict):
            failures.append((label, f"to_dict returned {type(d).__name__} (not dict)"))
            continue
        try:
            json.dumps(d)
        except (TypeError, ValueError) as e:
            failures.append((label, f"json.dumps(to_dict()) raised: {e}"))
            continue
    # Assert at least 70% of discovered dataclasses instantiated (allows some
    # weird cases but catches the systematic pre-Phase-13.4 regression where
    # half were being silently skipped due to field-type fall-through bugs).
    assert len(instantiated) >= int(len(cases) * 0.7), (
        f"D1-D3: only {len(instantiated)}/{len(cases)} dataclasses instantiated "
        f"cleanly. Failures:\n"
        + "\n".join(f"  - {label}: {msg}" for label, msg in failures[:20])
    )
    assert not failures, (
        f"D3: {len(failures)} dataclass(es) failed JSON-serializability:\n"
        + "\n".join(f"  - {label}: {msg}" for label, msg in failures[:20])
    )


# ── D4: DriveStatus.alert_level thresholds ────────────────────────────────

@pytest.mark.parametrize("free_gb,expected_alert", [
    (0.0, "critical"),
    (0.5, "critical"),
    (0.9, "critical"),
    (1.0, "critical"),  # exactly at threshold (<=, so critical)
    (1.5, "warning"),
    (2.0, "warning"),
    (3.0, "warning"),   # exactly at threshold (<=, so warning)
    (5.0, "ok"),
    (100.0, "ok"),
])
def test_D4_DriveStatus_alert_level_threshold_logic(
    free_gb: float, expected_alert: str,
) -> None:
    """DriveStatus.alert_level:
        free_gb <= 1.0  -> critical
        free_gb <= 3.0  -> warning
        free_gb >  3.0  -> ok

    Catches regressions in the threshold gating; protects the production
    circuit-breaker logic.
    """
    from worms.kali_health import DriveStatus
    ds = DriveStatus(
        drive_letter="E:", total_gb=500.0, free_gb=free_gb, used_gb=499.0,
        used_pct=99.8, filesystem="ext4", accessible=True,
    )
    assert ds.alert_level == expected_alert, (
        f"free_gb={free_gb}: alert_level={ds.alert_level!r} "
        f"(expected {expected_alert!r})"
    )


# ── D5: VHDXStatus.alert_level threshold ─────────────────────────────────

@pytest.mark.parametrize("growth_mb,expected_alert", [
    (0.0, "ok"),
    (50.0, "ok"),
    (499.9, "ok"),
    (500.0, "ok"),       # at threshold (strict >)
    (500.1, "warning"),
    (1000.0, "warning"),
])
def test_D5_VHDXStatus_alert_level_threshold_logic(
    growth_mb: float, expected_alert: str,
) -> None:
    """VHDXStatus.alert_level:
        abs(growth_mb_since_last) > 500 -> warning
        else -> ok
    """
    from worms.kali_health import VHDXStatus
    v = VHDXStatus(
        path="/dummy", size_gb=20.0, last_modified="2026-06-20T00:00:00",
        exists=True, growth_mb_since_last=growth_mb,
    )
    assert v.alert_level == expected_alert, (
        f"growth_mb={growth_mb}: alert={v.alert_level!r} (expected {expected_alert!r})"
    )


# ── D5b: KaliProcessStatus.alert_level threshold ─────────────────────────

@pytest.mark.parametrize("ram_used_pct,expected_alert", [
    (0.0, "ok"),
    (50.0, "ok"),
    (80.0, "ok"),       # at boundary (<= 80 -> ok)
    (80.5, "warning"),
    (90.0, "warning"),  # at boundary (> 80 -> warning, <= 90 -> warning)
    (90.5, "critical"),
    (100.0, "critical"),
])
def test_D5b_KaliProcessStatus_alert_level_threshold_logic(
    ram_used_pct: float, expected_alert: str,
) -> None:
    """KaliProcessStatus.alert_level:
        ram_used_pct > 90 -> critical
        ram_used_pct > 80 -> warning
        else -> ok
    """
    from worms.kali_health import KaliProcessStatus
    kp = KaliProcessStatus(
        total_ram_gb=1.0, used_ram_gb=ram_used_pct / 100.0, free_ram_gb=0.5,
        ram_used_pct=ram_used_pct, cpu_percent=5.0, uptime_minutes=10.0,
        accessible=True,
    )
    assert kp.alert_level == expected_alert, (
        f"ram_used_pct={ram_used_pct}: alert={kp.alert_level!r} "
        f"(expected {expected_alert!r})"
    )


# ── D6: overall_health aggregation (real test, not private-attr mutation) ──────

@pytest.mark.parametrize("drive_alert,vhdx_alert,kp_alert,expected_overall", [
    ("ok", "ok", "ok", "healthy"),
    ("warning", "ok", "ok", "warning"),
    ("ok", "warning", "ok", "warning"),
    ("ok", "ok", "warning", "warning"),
    ("critical", "ok", "ok", "critical"),         # drive critical trumps all
    ("critical", "warning", "warning", "critical"),
    ("warning", "warning", "critical", "critical"),     # kp critical trumps warning
    ("ok", "warning", "critical", "critical"),
    ("ok", "ok", "warning", "warning"),            # kp warning-only
    # Critical-trumps invariants (per Phase 13.4 D6-review finding):
    ("ok", "ok", "critical", "critical"),          # kp critical alone (no warning anywhere, but critical still wins)
    ("ok", "warning", "critical", "critical"),    # kp critical + vhdx warning (critical wins over warning)
    ("warning", "ok", "critical", "critical"),    # kp critical + drive warning (critical wins over warning)
])
def test_D6_KaliHealthStatus_overall_health_aggregation(
    drive_alert: str, vhdx_alert: str, kp_alert: str, expected_overall: str,
) -> None:
    """KaliHealthStatus.overall_health:
        any critical -> critical; any warning -> warning; else healthy.
    Uses fresh instances per case (not private-attr mutation).
    """
    from worms.kali_health import (
        DriveStatus, VHDXStatus, KaliProcessStatus, KaliHealthStatus,
    )

    # Construct components whose alert_levels match the parametrized inputs
    if drive_alert == "critical":
        drive = DriveStatus(drive_letter="E:", total_gb=500.0, free_gb=0.0,
                            used_gb=500.0, used_pct=100.0, filesystem="ext4",
                            accessible=False)
    elif drive_alert == "warning":
        drive = DriveStatus(drive_letter="E:", total_gb=500.0, free_gb=2.0,
                            used_gb=498.0, used_pct=99.6, filesystem="ext4",
                            accessible=True)
    else:
        drive = DriveStatus(drive_letter="E:", total_gb=500.0, free_gb=10.0,
                            used_gb=490.0, used_pct=98.0, filesystem="ext4",
                            accessible=True)

    if vhdx_alert == "critical":
        vhdx = VHDXStatus(path="/d", size_gb=20.0, last_modified="t",
                          exists=False, growth_mb_since_last=0.0)
    elif vhdx_alert == "warning":
        vhdx = VHDXStatus(path="/d", size_gb=20.0, last_modified="t",
                          exists=True, growth_mb_since_last=600.0)
    else:
        vhdx = VHDXStatus(path="/d", size_gb=20.0, last_modified="t",
                          exists=True, growth_mb_since_last=100.0)

    if kp_alert == "critical":
        kp = KaliProcessStatus(total_ram_gb=1.0, used_ram_gb=0.95,
                               free_ram_gb=0.05, ram_used_pct=95.0,
                               cpu_percent=5.0, uptime_minutes=10.0,
                               accessible=True)
    elif kp_alert == "warning":
        kp = KaliProcessStatus(total_ram_gb=1.0, used_ram_gb=0.85,
                               free_ram_gb=0.15, ram_used_pct=85.0,
                               cpu_percent=5.0, uptime_minutes=10.0,
                               accessible=True)
    else:
        kp = KaliProcessStatus(total_ram_gb=1.0, used_ram_gb=0.4,
                               free_ram_gb=0.6, ram_used_pct=40.0,
                               cpu_percent=5.0, uptime_minutes=10.0,
                               accessible=True)

    # Sanity: components have the expected alert_level
    assert drive.alert_level == drive_alert
    assert vhdx.alert_level == vhdx_alert
    assert kp.alert_level == kp_alert

    status = KaliHealthStatus(
        timestamp="t", drive=drive, vhdx=vhdx, kali_process=kp,
        alerts=[], recommendations=[],
    )
    assert status.overall_health == expected_overall, (
        f"drive={drive_alert}, vhdx={vhdx_alert}, kp={kp_alert}: "
        f"overall_health={status.overall_health!r} "
        f"(expected {expected_overall!r})"
    )


# ── D6b: NmapHost.open_port_count correct ────────────────────────────────

@pytest.mark.parametrize("port_states,expected_open_count", [
    ([("open", "ssh"), ("filtered", "http"), ("closed", "ftp")], 1),
    ([], 0),
    ([("open", "a"), ("open", "b"), ("open", "c"), ("closed", "d")], 3),
    ([("filtered", "a"), ("filtered", "b")], 0),
])
def test_D6b_NmapHost_open_port_count(
    port_states: list[tuple[str, str]],
    expected_open_count: int,
) -> None:
    """NmapHost.open_port_count returns the count of ports with state='open'."""
    from worms.nmap_scanner import NmapHost, NmapPort
    host = NmapHost(ip="127.0.0.1", state="up")
    for state, svc in port_states:
        host.ports.append(NmapPort(port=80, protocol="tcp", state=state, service=svc))
    assert host.open_port_count == expected_open_count, (
        f"port_states={port_states}: open_port_count={host.open_port_count} "
        f"(expected {expected_open_count})"
    )


if __name__ == "__main__":
    sys.exit(__import__("subprocess").call(
        [sys.executable, "-m", "pytest", __file__, "-v"]
    ))
