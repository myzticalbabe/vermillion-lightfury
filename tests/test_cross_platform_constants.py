# coding: utf-8
"""
tests/test_cross_platform_constants.py — Phase 13.4 Cross-Platform Constants Tests
==================================================================================

Verifies the platform-detection contract that worms + config rely on. The
``worms.exceptions`` module is the canonical source of cross-platform
constants consumed by every worm's method bodies (Phase 13.1 fix made
them importable as ``from worms.exceptions import _IS_KALI_NATIVE``).

Test matrix:

  C1  worms.exceptions attribute types
        ``_IS_WINDOWS`` -> bool
        ``_IS_KALI_NATIVE`` -> bool
        ``_E_DRIVE`` / ``_C_DRIVE`` / ``_G_DRIVE`` -> str
        ``KaliUnresponsiveError`` -> Exception subclass

  C2  String-format invariants conditional on ``_IS_WINDOWS``
        On Windows:  ``_E_DRIVE`` starts with a drive letter + colon
        On Linux:    ``_E_DRIVE`` starts with ``/``

  C3  Cross-file constant consistency
        ``worms.kali_health.KALI_VHDX_PATH`` matches the canonical form
        More general: every ``KALI_VHDX_PATH`` / ``KALI_*-PATH`` /
        ``*_PATH`` / ``*_DIR`` attribute across worms matches the
        cross-platform rule.

  C4  drive_letter format in DriveStatus (always "E:")
  C5  Anti-false-positive parity on the host platform
  C6  KaliUnresponsiveError raise / catch round-trip
"""

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── C1: attribute types ───────────────────────────────────────────────────

def test_C1_is_windows_is_bool() -> None:
    import worms.exceptions as exc
    assert isinstance(exc._IS_WINDOWS, bool), (
        f"_IS_WINDOWS = {exc._IS_WINDOWS!r} (type={type(exc._IS_WINDOWS).__name__}, "
        f"expected bool)"
    )


def test_C1_is_kali_native_is_bool() -> None:
    import worms.exceptions as exc
    assert isinstance(exc._IS_KALI_NATIVE, bool), (
        f"_IS_KALI_NATIVE = {exc._IS_KALI_NATIVE!r} (type="
        f"{type(exc._IS_KALI_NATIVE).__name__}, expected bool)"
    )


@pytest.mark.parametrize("attr", ["_E_DRIVE", "_C_DRIVE", "_G_DRIVE"])
def test_C1_drive_constant_is_string(attr: str) -> None:
    import worms.exceptions as exc
    val = getattr(exc, attr)
    assert isinstance(val, str), (
        f"{attr} = {val!r} (type={type(val).__name__}, expected str)"
    )
    assert len(val) > 0, f"{attr} is empty string"


def test_C1_kali_unresponsive_error_is_exception_subclass() -> None:
    import worms.exceptions as exc
    assert isinstance(exc.KaliUnresponsiveError, type), (
        "KaliUnresponsiveError is not a class"
    )
    assert issubclass(exc.KaliUnresponsiveError, Exception), (
        "KaliUnresponsiveError is not a subclass of Exception"
    )
    err = exc.KaliUnresponsiveError("test message")
    assert str(err) == "test message"


# ── C2: string-format invariants conditional on _IS_WINDOWS ──────────────

@pytest.mark.parametrize("attr", ["_E_DRIVE", "_C_DRIVE", "_G_DRIVE"])
def test_C2_drive_constant_format_matches_platform(attr: str) -> None:
    """The drive constant's string format must match the platform.

    On Windows: starts with a drive letter + colon (e.g. ``E:\\``, ``C:\\``).
    On Linux:   starts with ``/``.
    """
    import worms.exceptions as exc
    val = getattr(exc, attr)

    if exc._IS_WINDOWS:
        m = re.match(r"^[A-Z]:", val)
        assert m is not None, (
            f"_IS_WINDOWS=True but {attr}={val!r} does not start with "
            f"a drive letter + colon"
        )
    else:
        assert val.startswith("/"), (
            f"_IS_WINDOWS=False but {attr}={val!r} does not start with '/'"
        )


def test_C2_drive_constants_are_distinct() -> None:
    """_E_DRIVE / _C_DRIVE / _G_DRIVE are all different strings."""
    import worms.exceptions as exc
    vals = {exc._E_DRIVE, exc._C_DRIVE, exc._G_DRIVE}
    assert len(vals) == 3, (
        f"Drive constants not all distinct! E={exc._E_DRIVE!r} "
        f"C={exc._C_DRIVE!r} G={exc._G_DRIVE!r}"
    )


# ── C3: cross-file KALI_VHDX_PATH + _DIR/_PATH/_DRIVE consistency ────────

def _module_path_constants(modules: list[str]) -> list[tuple[str, str, str]]:
    """Iterate every module's module-level attributes and return
    (module_name, attr_name, value) for each that looks like a path / dir.

    Uses ``vars(mod)`` (the module dict) rather than ``dir(mod)`` so we
    discover ALL module-level assignments including private attrs and
    imported names — not just the public ones ``dir()`` surfaces.
    """
    found: list[tuple[str, str, str]] = []
    # Expanded suffix list: matches _DIR, _PATH, _DRIVE, _URI, _FILE,
    # _LOG, _CONFIG, _SHARED, _SCRIPT, _HISTORY (more permissive).
    path_like_re = re.compile(
        r"(_DIR|_PATH|_DRIVE|_URI|_FILE|_LOG|_CONFIG|_SHARED|_SCRIPT|_HISTORY)$"
    )
    for mod_name in modules:
        try:
            mod = importlib.import_module(mod_name)
        except (ImportError, ModuleNotFoundError):
            continue
        for attr_name, v in vars(mod).items():
            # Skip dunders (built-in module machinery), callables, and
            # other modules themselves (e.g. imported submodules).
            if attr_name.startswith("__"):
                continue
            if not isinstance(v, str):
                continue
            if v == "":
                continue
            if path_like_re.search(attr_name) is None:
                continue
            found.append((mod_name, attr_name, v))
    return found


def test_C3_every_path_constant_matches_platform_format() -> None:
    """For every ``*_DIR`` / ``*_PATH`` / ``*_DRIVE`` module-level string
    attribute across the 5 worm + 1 config module, verify the format
    matches the host platform rule (Windows: drive letter + colon; Linux: ``/``).

    Catches: hardcoded ``C:\\\\`` paths leaking into Linux, missing ``/mnt/``
    prefix on Linux, etc.
    """
    import worms.exceptions as exc
    modules = [
        "worms.kali_health",
        "worms.nmap_scanner",
        "worms.wsl_recovery",
        "worms.metasploit_bridge",
        "worms.benchmark_worm",
        "config.shared_dir",
    ]
    constants = _module_path_constants(modules)
    assert len(constants) > 5, (
        f"C3: expected to find many path constants, found only {len(constants)}; "
        f"the discovery logic may be too narrow"
    )

    mismatches: list[tuple[str, str, str, str, str]] = []  # (mod, attr, val, expected_format_indicator, msg)
    for mod_name, attr_name, val in constants:
        if exc._IS_WINDOWS:
            if not re.match(r"^[A-Z]:", val) and not val.startswith("\\\\"):
                mismatches.append((
                    mod_name, attr_name, val, "Windows: starts with drive letter or UNC",
                    f"_IS_WINDOWS=True but {mod_name}.{attr_name}={val!r} lacks drive letter prefix",
                ))
        else:
            if not val.startswith("/"):
                mismatches.append((
                    mod_name, attr_name, val, "Linux: starts with /",
                    f"_IS_WINDOWS=False but {mod_name}.{attr_name}={val!r} does not start with '/'",
                ))

    assert not mismatches, (
        f"C3: {len(mismatches)} module-level path constant(s) don't match platform format:\n"
        + "\n".join(f"  - {mod}.{attr}={val!r}: {msg}" for mod, attr, val, _, msg in mismatches)
    )


def test_C3_kali_health_kali_vhdx_path_specific() -> None:
    """worms.kali_health.KALI_VHDX_PATH matches the canonical cross-platform form.

    Direct test of the most-critical cross-file constant.
    """
    import worms.exceptions as exc
    kh = importlib.import_module("worms.kali_health")
    if exc._IS_WINDOWS:
        expected = f"{exc._E_DRIVE}\\Kali\\ext4.vhdx"
    else:
        expected = f"{exc._E_DRIVE}/Kali/ext4.vhdx"
    assert kh.KALI_VHDX_PATH == expected, (
        f"worms.kali_health.KALI_VHDX_PATH = {kh.KALI_VHDX_PATH!r} "
        f"(expected {expected!r}) - cross-file inconsistency!"
    )


def test_C3_wsl_recovery_kali_vhdx_path_specific() -> None:
    """worms.wsl_recovery.KALI_VHDX_PATH matches the canonical cross-platform form."""
    import worms.exceptions as exc
    wr = importlib.import_module("worms.wsl_recovery")
    if exc._IS_WINDOWS:
        expected = f"{exc._E_DRIVE}\\Kali\\ext4.vhdx"
    else:
        expected = f"{exc._E_DRIVE}/Kali/ext4.vhdx"
    assert wr.KALI_VHDX_PATH == expected, (
        f"worms.wsl_recovery.KALI_VHDX_PATH = {wr.KALI_VHDX_PATH!r} "
        f"(expected {expected!r}) - cross-file inconsistency!"
    )


# ── C4: DriveStatus.drive_letter format ──────────────────────────────────

def test_C4_drive_status_drive_letter_is_e_with_colon() -> None:
    """DriveStatus.drive_letter is always ``"E:"`` (symbolic, NOT the
    resolved mount path)."""
    from worms.kali_health import DriveStatus
    ds = DriveStatus(
        drive_letter="E:", total_gb=500.0, free_gb=100.0, used_gb=400.0,
        used_pct=80.0, filesystem="ext4", accessible=True,
    )
    assert ds.drive_letter == "E:", (
        f"DriveStatus.drive_letter={ds.drive_letter!r}, expected 'E:' "
        f"(symbolic label, NOT the mount path)"
    )


# ── C5: host platform parity ────────────────────────────────────────────

def test_C5_on_linux_ci_environment() -> None:
    """On a Linux CI host, ``_IS_WINDOWS`` is False."""
    import platform
    import worms.exceptions as exc
    host_os = platform.system().lower()
    if host_os == "linux":
        assert exc._IS_WINDOWS is False, (
            f"Host is Linux but _IS_WINDOWS={exc._IS_WINDOWS!r} (expected False)"
        )
    elif host_os == "windows":
        assert exc._IS_WINDOWS is True, (
            f"Host is Windows but _IS_WINDOWS={exc._IS_WINDOWS!r} (expected True)"
        )


# ── C6: KaliUnresponsiveError raise / catch round-trip ──────────────────

def test_C6_kali_unresponsive_error_raise_catch() -> None:
    import worms.exceptions as exc
    msg = "Kali is unresponsive after 30s timeout"
    raised = False
    try:
        raise exc.KaliUnresponsiveError(msg)
    except exc.KaliUnresponsiveError as e:
        raised = True
        assert str(e) == msg
        assert isinstance(e, Exception)
    assert raised, "KaliUnresponsiveError didn't reach the except clause"


def test_C6_kali_unresponsive_error_caught_as_exception() -> None:
    import worms.exceptions as exc
    caught = False
    try:
        raise exc.KaliUnresponsiveError("test")
    except Exception:
        caught = True
    assert caught


if __name__ == "__main__":
    sys.exit(__import__("subprocess").call(
        [sys.executable, "-m", "pytest", __file__, "-v"]
    ))
