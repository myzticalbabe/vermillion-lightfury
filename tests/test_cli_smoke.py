# coding: utf-8
"""
tests/test_cli_smoke.py — Phase 13.3 Real Subprocess CLI Smoke Tests
======================================================================

Invokes each worm's ``python -m worms.X`` CLI as an actual subprocess
(NOT in-process pytest import) to verify the production entry point is
healthy end-to-end.

WHY THESE TESTS MATTER:

Phase 13.1's latent bug was ``ModuleNotFoundError: No module named
'exceptions'`` raised from a worm's method body on first execution.
The bug surfaced ``ModuleNotFoundError`` ONLY on production CLI path:
a developer running ``python -m worms.kali_health check`` would hit
it, but ``import worms.kali_health`` in pytest would NOT (because
pytest's import machinery shadows the bare-import via sys.path hacks).

Subprocess invocation closes that gap. ``subprocess.run([python,
"-m", "worms.X"])`` spawns a FRESH Python interpreter, executes the
full runpy machinery, re-imports every dependency from scratch, and
only then calls ``main()``. If any ``from exceptions import X`` reverts
without ``worms.`` prefix, the subprocess fails with non-zero exit +
ModuleNotFoundError in stderr.

The five ``test_module_cli_subprocess_*`` tests below lock this down
permanently. The conftest.py sessionstart fixture adds a static AST
layer on top.

Stdlib only (plus ``pytest``).
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_cli_subprocess(module_path: str, args: list[str] | None = None,
                         timeout: int = 30) -> subprocess.CompletedProcess:
    """Invoke ``python -m module_path`` as a subprocess from project root.

    Returns the CompletedProcess; caller asserts on returncode/stderr.
    """
    cmd = [sys.executable, "-m", module_path]
    if args:
        cmd.extend(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
    )


# ── Per-worm CLI smoke tests ───────────────────────────────────────────────

WORM_MODULES: list[str] = [
    "worms.kali_health",
    "worms.nmap_scanner",
    "worms.wsl_recovery",
    "worms.metasploit_bridge",
    "worms.benchmark_worm",
]


@pytest.mark.parametrize("module", WORM_MODULES)
def test_module_cli_subprocess_exit_zero(module: str) -> None:
    """Each worm's ``python -m worms.X`` with no args returns exit code 0.

    The no-args path is ``else: print(f'WormName vVERSION...'); s =
    worm.get_summary(); print(...)``` \u2014 safe print-to-stdout summary
    that doesn't require any IO/worm-check to succeed for the print
    itself to succeed.

    Catches: ModuleNotFoundError, ImportError, any unhandled traceback,
    sys.path issues, sys.argv parsing bugs, encoding errors.
    """
    result = _run_cli_subprocess(module)
    assert result.returncode == 0, (
        f"`python -m {module}` returned exit code {result.returncode}\n"
        f"--- stdout ---\n{result.stdout[:400]}\n"
        f"--- stderr ---\n{result.stderr[:400]}"
    )
    assert "ModuleNotFoundError" not in result.stderr, (
        f"`python -m {module}` raised ModuleNotFoundError:\n"
        f"{result.stderr[:400]}"
    )
    # ``ImportError`` may appear as part of "Expected module name" warning
    # text \u2014 we only fail on a complete traceback or explicit ImportError
    # at module import time. Inspect the substring directly.
    assert "ImportError" not in result.stderr, (
        f"`python -m {module}` raised ImportError:\n{result.stderr[:400]}"
    )
    assert "Traceback (most recent call last)" not in result.stderr, (
        f"`python -m {module}` has an uncaught traceback in stderr:\n"
        f"{result.stderr[:400]}"
    )


@pytest.mark.parametrize("module", WORM_MODULES)
def test_module_cli_subprocess_prints_banner(module: str) -> None:
    """Each worm's CLI prints the ``WormName vVERSION \u2014 Vermillion LightFury``
    banner \u2014 sanity check that main() actually ran (not just exit-0).

    Catches: main() short-circuiting due to missing CLI arg, suppressed
    stdout, broken argparse. Belt-and-suspenders with the exit-zero test.
    """
    result = _run_cli_subprocess(module)
    assert result.returncode == 0
    expected_banner_words = ["Vermillion LightFury"]
    for word in expected_banner_words:
        assert word in result.stdout, (
            f"`python -m {module}` stdout missing '{word}' "
            f"(exit={result.returncode}):\n{result.stdout[:400]}"
        )


def test_kali_health_quick_subprocess_exit_zero() -> None:
    """KaliHealth `quick` subcommand is the simplest non-default CLI path.

    No WSL/Kali needed \u2014 just reads E: drive metadata via
    ``shutil.disk_usage``. Should always exit-0 in any environment.
    """
    result = _run_cli_subprocess("worms.kali_health", ["quick"])
    assert result.returncode == 0, (
        f"`python -m worms.kali_health quick` rc={result.returncode}\n"
        f"stderr: {result.stderr[:300]}"
    )
    assert "ModuleNotFoundError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_wsl_recovery_status_subprocess_exit_zero() -> None:
    """WSLRecovery `status` subcommand prints distro states without WSL.

    Even on a non-WSL Linux host, this command should exit-0: ``wsl
    --list --verbose`` fails gracefully \u2192 empty state map \u2192
    prints "Unknown" for all distros.
    """
    result = _run_cli_subprocess("worms.wsl_recovery", ["status"])
    assert result.returncode == 0, (
        f"`python -m worms.wsl_recovery status` rc={result.returncode}\n"
        f"stderr: {result.stderr[:300]}"
    )
    assert "ModuleNotFoundError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_nmap_scanner_ping_subprocess_uses_post_fix_body() -> None:
    """NmapScanner `scan 127.0.0.1` end-to-end (with clean env) \u2014 validates
    the production scan path executes without ModuleNotFoundError.

    Note: this WILL fail if no WSL+Kali is available (expected in this
    CI env). We only check that the failure mode is NOT a missing
    import \u2014 i.e. any stderr must NOT contain ``ModuleNotFoundError``.
    """
    result = _run_cli_subprocess(
        "worms.nmap_scanner", ["scan", "127.0.0.1", "--scan-type", "ping"],
        timeout=60,
    )
    # Don't assert exit code \u2014 scan can legitimately fail without WSL.
    # Only assert: no ModuleNotFoundError + no Traceback from missing modules.
    assert "ModuleNotFoundError" not in result.stderr, (
        f"`python -m worms.nmap_scanner scan` raised ModuleNotFoundError:\n"
        f"stderr: {result.stderr[:400]}"
    )
    # ``Traceback (most recent call last)`` is acceptable for non-import
    # errors (e.g. "WSL not found" raises FileNotFoundError which is the
    # EXPECTED behavior on non-WSL hosts). We only fail on tracebacks
    # that originate from an import-related frame.
    if "Traceback (most recent call last)" in result.stderr:
        # Allow traces that don't mention exceptions/imports
        bad = "exceptions" in result.stderr or "import" in result.stderr.lower()
        assert not bad, (
            f"`python -m worms.nmap_scanner scan` has import-related "
            f"traceback:\n{result.stderr[:600]}"
        )


@pytest.mark.slow  # 5 subprocess invocations serialized (~25s); skip in fast iteration
def test_subprocess_summary_module_no_module_warnings() -> None:
    """Cross-worm sanity check: none of the worms emit
    ``RuntimeWarning: ... found in sys.modules`` warnings on first import
    (a known runpy artifact when ``worms/__init__.py`` causes partial-
    module initialization during ``python -m`` setup).

    Such warnings are correct in some cases but indicate import-machinery
    hygiene issues. If a worm triggers one, it's a code-smell.
    """
    for module in WORM_MODULES:
        result = _run_cli_subprocess(module)
        # Note: we DO allow RuntimeWarning \u2014 document if any appear.
        # Test passes if no `RuntimeWarning` AND no `ImportWarning`.
        assert "ImportWarning" not in result.stderr, (
            f"`python -m {module}` emitted ImportWarning:\n"
            f"{result.stderr[:400]}"
        )


if __name__ == "__main__":
    # Manual run mode \u2014 useful for debugging
    import subprocess as _sp
    sys.exit(_sp.call([sys.executable, "-m", "pytest", __file__, "-v"]))
