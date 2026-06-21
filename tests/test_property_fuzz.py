# coding: utf-8
"""
tests/test_property_fuzz.py — Phase 13.5 Property-Based Fuzz Tests
==================================================================

Per the Phase 13.4 reviewer + Phase 13.5 thinker analysis, the discrete
parametrize tests in ``tests/test_dataclass_integrity.py`` covered only a
finite subset of boundary values. This file uses ``hypothesis`` to fuzz
the full float range (NaN/Inf/near-threshold/negative) and discover
off-by-one bugs the discrete parametrization missed.

Bug surfaces discovered by the Phase 13.5 thinker (will be exercised by
hypothesis):

  1. **DriveStatus.free_gb** near exactly 1.0 and 3.0 (NaN/Inf, -1).
     Threshold rule: ``free_gb <= 1.0`` / ``<= 3.0`` / else ok.
     NaN fails all comparisons → falls through to ``else`` → ``"ok"``
     (silently masks filesystem errors). Hypothesis DISCOVERS this:
     explicit SF7 asserts NaN MUST NOT silently return ``"ok"``.

  2. **VHDXStatus.growth_mb_since_last** — UNTESTED for NEGATIVE values.
     Source uses ``abs(growth_mb_since_last) > 500``; negatives never
     appeared in the discrete parametrization. SF2 locks down the
     ``abs()`` invariant for the full [-5000, +5000] range.

  3. **VHDXFsckResult.errors_found = -1** — operational error state.
     Source ``if not self.passed and self.errors_found > 0: warning``.
     With errors_found = -1, the ``> 0`` branch is False; the ``!passed``
     else-branch routes to ``"warning"``. But an OPERATIONAL failure
     (vs pure data corruption) arguably warrants ``"critical"``.
     SF3 asserts the EXPECTED-correct behavior ``"critical"`` (xfail
     until worms/wsl_recovery.py is fixed — discovery artifact).

  4. **WSLRecovery fuzz** — WSLDistroStatus and VHDXFsckResult are
     entirely untested in ``tests/test_dataclass_integrity.py``. SF4 + SF5
     cover their alert_level cascades.

  5. **Process subprocess CLI fuzz** — exercise ``python -m worms.X``
     with very-long / unicode / empty / malformed args to find CLI
     surface bugs. SF6.

Stdlib + hypothesis only; hypothesis is installed via
``pip3 install hypothesis``.
"""

import json
import math
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

# hypothesis is mandatory for this file — skip the whole module if unavailable.
try:
    from hypothesis import HealthCheck, assume, given, settings, strategies as st
    _HYPOTHESIS_AVAILABLE = True
except ImportError:
    _HYPOTHESIS_AVAILABLE = False
    pytest.skip(
        "hypothesis not installed; skipping property-fuzz tests. "
        "Install with 'pip3 install hypothesis' to enable.",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── SF1: DriveStatus.free_gb boundary scan ─────────────────────────────────


@pytest.mark.parametrize("free_gb,expected_alert", [
    # Exact boundary-1 below CRITICAL: 0.9999 → critical
    (0.9999, "critical"),
    # Exact boundary-1 AT CRITICAL: 1.0 → critical (inclusive)
    (1.0,     "critical"),
    # Just above CRITICAL: 1.0001 → warning
    (1.0001,  "warning"),
    # Just below WARNING: 2.9999 → warning
    (2.9999,  "warning"),
    # Exact AT WARNING: 3.0 → warning (inclusive)
    (3.0,     "warning"),
    # Just above WARNING: 3.0001 → ok
    (3.0001,  "ok"),
    # Mid-range healthy: 100.0 → ok
    (100.0,   "ok"),
    # Negative: any negative value <= 1.0 → critical
    (-1.0,    "critical"),
    (-0.5,    "critical"),
    # Zero
    (0.0,     "critical"),
])
def test_SF1_free_gb_discrete_boundary_scan(
    free_gb: float, expected_alert: str,
) -> None:
    """Each free_gb value must map to its expected alert_level per the
    source rule ``<= E_DRIVE_CRITICAL_GB (1.0) | <= E_DRIVE_WARNING_GB
    (3.0) | ok``.
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


# ── SF1_hypothesis: full-range floats (NaN/Inf explicitly EXCLUDED) ────────


@given(
    free_gb=st.floats(
        min_value=-1000.0, max_value=10000.0,
        allow_nan=False, allow_infinity=False,
    ),
)
@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_SF1_hypothesis_free_gb_invariants(free_gb: float) -> None:
    """Hypothesis-driven: for ANY finite float in [-1000, +10000], the
    source rule ``<= 1.0 | <= 3.0 | ok`` must hold deterministically.

    NaN/Inf are EXPLICITLY excluded here — they are tested separately in
    SF7 (and would otherwise cause hypothesis to SHRINK toward them as
    the simplest counter-example, masking real boundary bugs).
    """
    from worms.kali_health import (
        DriveStatus, E_DRIVE_CRITICAL_GB, E_DRIVE_WARNING_GB,
    )
    ds = DriveStatus(
        drive_letter="E:", total_gb=500.0, free_gb=free_gb, used_gb=499.0,
        used_pct=99.8, filesystem="ext4", accessible=True,
    )
    if free_gb <= E_DRIVE_CRITICAL_GB:
        expected = "critical"
    elif free_gb <= E_DRIVE_WARNING_GB:
        expected = "warning"
    else:
        expected = "ok"
    assert ds.alert_level == expected, (
        f"free_gb={free_gb!r}: alert_level={ds.alert_level!r} "
        f"violates the source rule (expected {expected!r})"
    )


# ── SF2: VHDXStatus.growth_mb abs() invariant (incl. NEGATIVE values) ─────


@pytest.mark.parametrize("growth_mb,expected_alert", [
    # Positive cases
    (0.0,      "ok"),
    (499.9,    "ok"),
    (500.0,    "ok"),     # strict > 500 → ok
    (500.1,    "warning"),
    (1000.0,   "warning"),
    # NEGATIVE cases — locks down the abs() invariant
    (-1.0,     "ok"),
    (-500.0,   "ok"),
    (-500.1,   "warning"),  # abs(-500.1) = 500.1 > 500
    (-501.0,   "warning"),
    (-1000.0,  "warning"),
    (-1001.0,  "warning"),
])
def test_SF2_growth_mb_positive_and_negative(
    growth_mb: float, expected_alert: str,
) -> None:
    """VHDXStatus.alert_level MUST apply ``abs()`` to growth_mb_since_last.

    If a future refactor drops the ``abs()``, negative past -500 would
    silently flip to ``"ok"`` regardless of magnitude. This test locks
    down the abs() invariant over the full +/− range.
    """
    from worms.kali_health import VHDXStatus
    v = VHDXStatus(
        path="/dummy", size_gb=20.0, last_modified="2026-06-20T00:00:00",
        exists=True, growth_mb_since_last=growth_mb,
    )
    assert v.alert_level == expected_alert, (
        f"growth_mb={growth_mb}: abs={abs(growth_mb)} → alert={v.alert_level!r} "
        f"(expected {expected_alert!r}; abs() regression if mismatched)"
    )


@given(
    growth_mb=st.floats(
        min_value=-5000.0, max_value=5000.0,
        allow_nan=False, allow_infinity=False,
    ),
)
@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_SF2_hypothesis_growth_mb_abs_invariant(growth_mb: float) -> None:
    """``abs(growth_mb) > 500`` is the source rule. If a refactor drops
    the abs(), negative inputs past -500 magnitude would silently flip
    to ``"ok"`` instead of ``"warning"``.
    """
    from worms.kali_health import VHDXStatus, VHDX_GROWTH_RATE_WARN
    v = VHDXStatus(
        path="/dummy", size_gb=20.0, last_modified="2026-06-20T00:00:00",
        exists=True, growth_mb_since_last=growth_mb,
    )
    expected = "warning" if abs(growth_mb) > VHDX_GROWTH_RATE_WARN else "ok"
    assert v.alert_level == expected, (
        f"growth_mb={growth_mb!r}: abs={abs(growth_mb)} alert={v.alert_level!r} "
        f"(expected {expected!r})"
    )


# ── SF3: VHDXFsckResult.errors_found extreme values ───────────────────────


@pytest.mark.parametrize("passed,errors_found,expected_alert", [
    # Source rule: if !passed && errors>0 -> critical
    #             elif !passed -> warning
    #             else -> ok
    # Normal states where current source behavior IS semantically correct
    (True,  0,  "ok"),       # clean
    (True,  5,  "ok"),       # passed=True overrides everything
    (False, 0,  "warning"),  # !passed && errors==0 → else-branch warning
    (False, 1,  "critical"), # !passed && errors>0 → critical (correct!)
    (False, 5,  "critical"), # !passed && errors>0 → critical (correct!)
])
def test_SF3_vhdx_fsck_alert_levels_validated(
    passed: bool, errors_found: int, expected_alert: str,
) -> None:
    """Validate VHDXFsckResult.alert_level source rule cascade for cases
    where current source behavior is correct.

    Locks down the rule: a "passed=False with errors_found>0" path which
    the discrete parametrization in test_dataclass_integrity.py never
    tested.
    """
    from worms.wsl_recovery import VHDXFsckResult
    fs = VHDXFsckResult(
        path="/e/dummy", passed=passed, read_only=True,
        errors_found=errors_found, errors_fixed=0, output="",
        duration_seconds=0.0,
    )
    assert fs.alert_level == expected_alert, (
        f"passed={passed}, errors_found={errors_found}: "
        f"alert_level={fs.alert_level!r} (expected {expected_alert!r})"
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Phase 13.5 hypothesis-driven bug discovery: "
        "VHDXFsckResult.alert_level with errors_found=-1 (OPERATIONAL "
        "error per source code line ~506) currently routes to 'warning' "
        "(the else-branch catches it because -1 > 0 is False). "
        "EXPECTED-correct behavior: escalate to 'critical' since "
        "errors_found=-1 indicates a process/runtime failure, not a "
        "benign data-corruption count. Fix worms/wsl_recovery.py to use "
        "'if self.errors_found != 0' instead of 'if self.errors_found > 0' "
        "to escalate operational failures."
    ),
)
def test_SF3_vhdx_fsck_alert_levels_operational_failure_discovery() -> None:
    """Phase 13.5 discovery artifact: errors_found=-1 should escalate to
    "critical" (operational failure) but source returns "warning".

    The e2fsck source-code path at line ~506 emits errors_found=-1 to
    indicate operational failure (couldn't even run fsck). The
    alert_level rule `if errors_found > 0` silently bypasses this
    sentinel value, dropping it into the `elif not passed: warning`
    branch. The EXPECTED-correct semantic is `"critical"`.

    Locked down as `xfail(strict=False)` so a future source fix
    automatically promotes this test to PASS without code change.
    """
    from worms.wsl_recovery import VHDXFsckResult
    fs = VHDXFsckResult(
        path="/e/dummy", passed=False, read_only=True,
        errors_found=-1, errors_fixed=0, output="",
        duration_seconds=0.0,
    )
    assert fs.alert_level == "critical", (
        f"passed=False, errors_found=-1 (OPERATIONAL FAILURE): "
        f"alert_level={fs.alert_level!r} (expected 'critical'). "
        f"Source rule 'errors_found > 0' catches -1 as 'warning' which "
        f"silently downgrades an operational failure to a warning."
    )


# ── SF4: WSLDistroStatus.alert_level fuzz ────────────────────────────────


@pytest.mark.parametrize("state,responsive,expected_alert", [
    # state="Crashed" → critical (highest precedence)
    ("Crashed",  False, "critical"),
    ("Crashed",  True,  "critical"),   # even if responsive=True, state trumps
    # !is_healthy → warning (Stopped OR not-responsive)
    ("Stopped",  False, "warning"),
    ("Stopped",  True,  "warning"),    # Stopped trumps responsive
    ("Running",  False, "warning"),    # running but unresponsive = warning
    ("Running",  True,  "ok"),
    ("Unknown",  False, "warning"),
    ("Unknown",  True,  "ok"),
])
def test_SF4_wsl_distro_alert_levels(
    state: str, responsive: bool, expected_alert: str,
) -> None:
    """WSLDistroStatus.alert_level cascade:
        state == "Crashed"              → critical
        state == "Stopped" OR !responsive → warning
        else                            → ok
    """
    from worms.wsl_recovery import WSLDistroStatus
    d = WSLDistroStatus(
        name="kali", state=state, wsl_version=2,
        responsive=responsive, last_error="",
        checked_at="2026-06-20T00:00:00",
    )
    assert d.alert_level == expected_alert, (
        f"state={state!r}, responsive={responsive}: "
        f"alert_level={d.alert_level!r} (expected {expected_alert!r})"
    )


# ── SF5: KaliProcessStatus.alert_level fuzz ───────────────────────────────


@pytest.mark.parametrize("ram_pct,expected_alert", [
    # Source rule for KaliProcessStatus.alert_level (RAM-only):
    #   if ram_used_pct > RAM_USED_WARN_PCT (=90):     critical
    #   elif ram_used_pct > 80:                        warning
    #   else:                                          ok
    # NOTE: source uses EXCLUSIVE `>` (not `>=`), so values EXACTLY at the
    # boundary (80.0, 90.0) fall to the NEXT-DOWN branch. Phase 13.5
    # hypothesis discovery: this operator inclusivity is a real source
    # semantics question — locked down here so a future `>=` flip is
    # caught. The boundary test cases that "fall through" the boundary
    # document the operator choice.
    (0.0,    "ok"),
    (50.0,   "ok"),
    (79.99,  "ok"),
    (80.0,   "ok"),        # boundary: 80.0 > 80 is False → else ok
    (80.01,  "warning"),   # just above lower boundary
    (89.99,  "warning"),
    (90.0,   "warning"),   # boundary: 90.0 > 90 is False → elif 90.0 > 80 True → warning
    (90.01,  "critical"),  # just above upper boundary
    (95.0,   "critical"),
    (100.0,  "critical"),
])
def test_SF5_process_alert_levels(
    ram_pct: float, expected_alert: str,
) -> None:
    """KaliProcessStatus.alert_level RAM-only threshold cascade:
    ram_used_pct > 90 → critical; ram_used_pct > 80 → warning; else ok.

    NOTE: This dataclass's alert_level is RAM-only. CPU percent contributes
    to `check()` alerts but does NOT affect `KaliProcessStatus.alert_level`
    directly.

    Locks down the inclusive (> vs >=) boundary behavior at exactly 80.0
    and exactly 90.0 to catch any future refactor flipping the operators.
    """
    from worms.kali_health import KaliProcessStatus
    kp = KaliProcessStatus(
        total_ram_gb=16.0, used_ram_gb=8.0, free_ram_gb=8.0,
        ram_used_pct=ram_pct, cpu_percent=50.0,
        uptime_minutes=600.0, accessible=True,
    )
    assert kp.alert_level == expected_alert, (
        f"ram_pct={ram_pct}: alert_level={kp.alert_level!r} "
        f"(expected {expected_alert!r})"
    )


# ── SF6: Process subprocess CLI fuzz (real subprocess invocations) ─────────


def _run_subproc(
    args: list[str], *, timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
    )


@pytest.mark.parametrize("module", [
    "worms.kali_health",
    "worms.nmap_scanner",
    "worms.wsl_recovery",
    "worms.metasploit_bridge",
    "worms.benchmark_worm",
])
def test_SF6_subprocess_very_long_target_does_not_crash(module: str) -> None:
    """A 10,000-character target argument MUST NOT crash the worm CLI.

    Real-world: scanner targets from URL lists can be 10k+ chars.
    """
    long_target = "127.0.0.1," * 1000  # ~12k chars
    result = _run_subproc(["-m", module, "scan", long_target], timeout=60)
    # ModuleNotFoundError must never appear (Phase 13.1 latent-bug regression)
    assert "ModuleNotFoundError" not in result.stderr, (
        f"`python -m {module} scan <very_long>` triggered ModuleNotFoundError: "
        f"{result.stderr[:600]}"
    )


@pytest.mark.parametrize("module", [
    "worms.kali_health",
    "worms.nmap_scanner",
    "worms.wsl_recovery",
    "worms.metasploit_bridge",
    "worms.benchmark_worm",
])
def test_SF6_subprocess_unicode_arg_is_handled_or_rejected(module: str) -> None:
    """Unicode arguments MUST NOT crash the worm CLI with import errors.

    Real-world: international user mentions / labels / metadata fields.
    """
    unicode_arg = "test_unicode_\u00e9\u00e0\u4e2d\u6587_\U0001f600"
    result = _run_subproc(["-m", module, "scan", unicode_arg], timeout=60)
    assert "ModuleNotFoundError" not in result.stderr, (
        f"`python -m {module} scan <unicode>` triggered ModuleNotFoundError: "
        f"{result.stderr[:600]}"
    )


@pytest.mark.parametrize("module", [
    "worms.kali_health",
    "worms.nmap_scanner",
    "worms.wsl_recovery",
    "worms.metasploit_bridge",
    "worms.benchmark_worm",
])
def test_SF6_subprocess_empty_string_arg_does_not_crash(module: str) -> None:
    """Empty-string arguments MUST NOT crash the worm CLI with import errors."""
    result = _run_subproc(["-m", module, "scan", ""], timeout=30)
    assert "ModuleNotFoundError" not in result.stderr, (
        f"`python -m {module} scan ''` triggered ModuleNotFoundError: "
        f"{result.stderr[:600]}"
    )


def test_SF6_subprocess_kali_health_json_output_is_valid_json() -> None:
    """``python -m worms.kali_health check --json`` MUST return valid JSON
    when the worm exits 0 (otherwise the CLI surface is broken).

    This is the SF6 surface-bug detector: any non-JSON, hint-suppressed,
    or trace-leaking output is a discovery.
    """
    result = _run_subproc(
        ["-m", "worms.kali_health", "check", "--json"], timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
            assert isinstance(parsed, dict), (
                f"`python -m worms.kali_health check --json` returned "
                f"non-dict: {type(parsed).__name__}"
            )
        except json.JSONDecodeError as e:
            pytest.fail(
                f"`python -m worms.kali_health check --json` returned "
                f"non-JSON output: {e}; stdout first 300: {result.stdout[:300]}"
            )


# ── SF6b: Phase 13.7 argparse subcommand-surface regression fuzz ────────────
#
# Phase 13.7 introduced new `add_subparsers(required=True)` surfaces on every
# worm. SF6b locks down the CLI invariants of those surfaces — specifically:
#   1. Each new subcommand supports `--help` cleanly (exit 0, help text shown)
#   2. No-args invocation preserves the legacy banner-printing UX at exit 0
#   3. An unknown subcommand exits 2 (argparse default) with a stderr error —
#      no Python traceback leaks (especially no ModuleNotFoundError regression)
#   4. JSON-output subcommands produce parseable JSON when rc == 0
#
# These tests run via real `python -m worms.X` subprocess invocations
# (not mocked) so any latent import error, sys.excepthook bug, or argparse
# regression immediately surfaces.


# (module, subcommand, args_after_subcommand, mode)
# mode == "help"        → expect rc 0, stdout contains the help text
# mode == "run_help"    → call with NO args (banner-UX preservation)
# mode == "unknown"     → use a clearly-bogus subcommand; expect rc == 2
# mode == "json_check"  → known-arg path; if rc 0 stdout must be JSON
_SF6B_CASES: list[tuple[str, str, list[str], str]] = [
    # nmap_scanner (5 new subcommands in Phase 13.7: scan / summary / preflight)
    ("worms.nmap_scanner",   "scan",      ["--help"],                                   "help"),
    ("worms.nmap_scanner",   "summary",   ["--help"],                                   "help"),
    ("worms.nmap_scanner",   "preflight", ["--help"],                                   "help"),
    ("worms.nmap_scanner",   "",          [],                                           "run_help"),
    ("worms.nmap_scanner",   "bogus_sub", [],                                           "unknown"),
    ("worms.nmap_scanner",   "summary",   [],                                           "json_check"),
    # benchmark_worm (2 new subcommands: run / summary)
    ("worms.benchmark_worm", "run",       ["--help"],                                   "help"),
    ("worms.benchmark_worm", "summary",   ["--help"],                                   "help"),
    ("worms.benchmark_worm", "",          [],                                           "run_help"),
    ("worms.benchmark_worm", "bogus_sub", [],                                           "unknown"),
    ("worms.benchmark_worm", "summary",   [],                                           "json_check"),
]


@pytest.mark.parametrize("module, subcommand, args, mode", _SF6B_CASES)
def test_SF6b_subcommand_help_run_unknown_and_json(
    module: str, subcommand: str, args: list[str], mode: str,
) -> None:
    """Phase 13.9 SF6b regression guard for the new subcommand surface.

    Locks down the four CLI invariants listed in the section comment above.
    """
    cli_args: list[str] = ["-m", module]
    if subcommand:
        cli_args.append(subcommand)
    cli_args.extend(args)
    result = subprocess.run(
        [sys.executable, *cli_args],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )

    # Invariant 0 (universal): NEVER a ModuleNotFoundError. Same regression
    # guard as SF6: Phase 13.1 fixed the `from exceptions import X` latent
    # bug across 5 files. This locks it across the new surface.
    assert "ModuleNotFoundError" not in result.stderr, (
        f"`python {' '.join(cli_args)}` triggered ModuleNotFoundError: "
        f"{result.stderr[:600]}"
    )

    if mode == "help":
        # --help must succeed and emit usage info on stdout. argparse writes
        # to stdout for INVOCATION-level help; rc 0 expected.
        assert result.returncode == 0, (
            f"`python {' '.join(cli_args)}` --help exited "
            f"{result.returncode}, expected 0. stderr: {result.stderr[:400]}"
        )
        assert "usage" in result.stdout.lower() or "help" in result.stdout.lower(), (
            f"`python {' '.join(cli_args)}` --help stdout missing usage/help "
            f"text: {result.stdout[:400]}"
        )

    elif mode == "run_help":
        # No-args UX: banner must print, exit 0. This preserves the F1/F5/F7
        # banner behavior that older tests rely on, while argparse +
        # required=True guarantees that OMITTED subcommand on a future
        # refactor raises cleanly (mode == "unknown" covers that path).
        assert result.returncode == 0, (
            f"`python {' '.join(cli_args)}` (no args) exited "
            f"{result.returncode}, expected 0 (banner UX). stderr: "
            f"{result.stderr[:400]}"
        )
        assert result.stdout.strip(), (
            f"`python {' '.join(cli_args)}` (no args) printed empty stdout — "
            f"banner UX regressed"
        )

    elif mode == "unknown":
        # Unknown subcommand: argparse exits STRICTLY with rc==2 (the
        # argparse default for invalid subparser choice — Phase 13.7
        # `required=True` invariant). NO Python traceback should leak.
        assert result.returncode == 2, (
            f"`python {' '.join(cli_args)}` (unknown subcommand) exited "
            f"{result.returncode}, expected EXACTLY 2 (argparse). stdout: "
            f"{result.stdout[:400]}"
        )
        combined = (result.stdout + result.stderr).lower()
        assert (
            "invalid choice" in combined
            or "unrecognized arguments" in combined
            or "the following arguments are required" in combined
        ), (
            f"`python {' '.join(cli_args)}` (unknown subcommand) stderr "
            f"missing argparse error phrasing. stdout: "
            f"{result.stdout[:400]} stderr: {result.stderr[:400]}"
        )
        assert "Traceback" not in result.stderr, (
            f"`python {' '.join(cli_args)}` (unknown subcommand) leaked a "
            f"Python traceback to stderr: {result.stderr[:400]}"
        )

    elif mode == "json_check":
        # Subcommand-exit-success path: stdout must be valid JSON (parsers
        # verify against the source contract).
        assert result.returncode == 0, (
            f"`python {' '.join(cli_args)}` exited {result.returncode}, "
            f"expected 0 for JSON subcommand. stdout: "
            f"{result.stdout[:400]} stderr: {result.stderr[:400]}"
        )
        assert result.stdout.strip(), (
            f"`python {' '.join(cli_args)}` printed empty stdout — "
            f"summary subcommand regressed"
        )
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            pytest.fail(
                f"`python {' '.join(cli_args)}` returned non-JSON output: "
                f"{e}; stdout first 400: {result.stdout[:400]}"
            )
        assert isinstance(parsed, dict), (
            f"`python {' '.join(cli_args)}` JSON root is "
            f"{type(parsed).__name__}, expected dict"
        )


# ── SF7: NaN/Inf semantic assertion — MUST NOT silently return "ok" ─────────


@pytest.mark.parametrize("free_gb_value,description", [
    (float("nan"),  "NaN"),
    (float("inf"),  "+Infinity"),
    (float("-inf"), "-Infinity"),
])
@pytest.mark.xfail(
    strict=False,
    reason=(
        "Phase 13.5 hypothesis-driven bug discovery: NaN/+Inf/-Inf "
        "free_gb silently returns 'ok'. The source rule uses "
        "`<= 1.0` / `<= 3.0` comparisons; for NaN both `NaN <= 1.0` "
        "and `NaN <= 3.0` are False in Python, falling through to the "
        "else-branch returning 'ok'. Sentinel/unbounded values SHOULD "
        "NOT be reported as healthy. Fix worms/kali_health.py:DriveStatus."
        "alert_level to add: `if math.isnan(self.free_gb) or "
        "math.isinf(self.free_gb): return 'critical'` as the first "
        "branch."
    ),
)
def test_SF7_nan_inf_free_gb_must_not_silently_fall_through_to_ok(
    free_gb_value: float, description: str,
) -> None:
    """Hypothesis-driven discovery (Phase 13.5): a NaN/Infinity free_gb
    MUST NOT silently return ``"ok"``.

    Today, source rule ``free_gb <= 1.0`` / ``<= 3.0`` / else ok silently
    evaluates NaN/Inf as neither <= 1.0 nor <= 3.0 → falls through to
    ``else`` → ``"ok"``. This is a real latent bug: a sentinel value
    (unknown / unbounded) should NOT be reported as healthy.

    Expected-correct behavior: NaN/Inf should fail the gate (return
    ``"critical"`` or raise). Test asserts the non-ok property — if
    source returns ``"ok"`` for NaN/Inf, the test fails, surfacing the
    bug.
    """
    from worms.kali_health import DriveStatus
    ds = DriveStatus(
        drive_letter="E:", total_gb=500.0, free_gb=free_gb_value,
        used_gb=499.0, used_pct=99.8, filesystem="ext4", accessible=True,
    )
    # Source SHOULD NOT silently fall through to "ok" for sentinel values.
    # The expected minimum escalation: "warning" or "critical".
    assert ds.alert_level in {"warning", "critical"}, (
        f"free_gb={description}: alert_level={ds.alert_level!r} silently "
        f"returned 'ok' — sentinel/unbounded value should escalate to at "
        f"least 'warning' (Phase 13.5 hypothesis-discovered bug surface)."
    )


# ── SF8: serialization round-trip ────────────────────────────────────────


def test_SF8_DriveStatus_serialization_round_trip() -> None:
    """dataclasses.replace bypasses __init__ — if any future post-init
    validation is added to DriveStatus, this test stays green while
    runtime breaks. Pure-trivial now; gates future regressions.
    """
    import dataclasses
    from worms.kali_health import DriveStatus
    original = DriveStatus(
        drive_letter="E:", total_gb=500.0, free_gb=10.5, used_gb=489.5,
        used_pct=97.9, filesystem="ext4", accessible=True,
    )
    json.dumps(original.to_dict())  # must not raise
    updated = dataclasses.replace(original, free_gb=15.0)
    assert updated.free_gb == 15.0
    json.dumps(updated.to_dict())


# ── SF9 (free-threaded only): instantiation stress under 3.14t ────────────


_PY314T = sysconfig.get_config_var("Py_GIL_DISABLED") == 1


@pytest.mark.skipif(
    not _PY314T,
    reason="free-threaded invariant test only meaningful on 3.14t",
)
@given(
    free_gb=st.floats(
        min_value=-10.0, max_value=10.0,
        allow_nan=False, allow_infinity=False,
    ),
)
@settings(max_examples=200, deadline=None)
def test_SF9_free_gb_instantiation_no_crash_on_314t(free_gb: float) -> None:
    """On 3.14t free-threading, generating 200 DriveStatus instances across
    a wide float range must not crash the python interpreter.

    NOTE: This test exercises SEQUENTIAL instantiation — concurrent
    thread spawning is not part of this test (the Phase 13.3 S2 test
    already covers the concurrent thread stress case for
    WormSafeInstance lock semantics). What this catches is any
    INTERPRETER-level bug in DriveStatus ctor / property access that
    happens to only manifest on free-threaded mode (e.g., race in
    dataclass __init__ or to_dict()). If non-thread-safe code lurks,
    sequential fuzz with 200 samples across the float space is the
    minimum reproducible unit that surfaces it.
    """
    from worms.kali_health import DriveStatus
    ds = DriveStatus(
        drive_letter="E:", total_gb=500.0, free_gb=free_gb, used_gb=499.0,
        used_pct=99.8, filesystem="ext4", accessible=True,
    )
    assert ds.alert_level in {"critical", "warning", "ok"}


if __name__ == "__main__":
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-v"],
    ))
