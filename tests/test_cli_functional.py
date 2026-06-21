# coding: utf-8
"""
tests/test_cli_functional.py — Phase 13.4 CLI Functional Tests
==============================================================

Verifies the BEHAVIOR of each worm's CLI — not just exit-code-0 surface
smoke. This is the gap that 84-pass tests in Phase 13.3 left open: a worm
can exit 0 with empty / wrong / meaningless output and the prior suite
would still green.

Test matrix (after Phase 13.4 polish):

  F1  Banner content: each worm prints its ClassName + "Vermillion LightFury" + semver.
  F2  JSON output validity (kali_health --json): valid parseable JSON with expected keys.
  F3  --help flag behavior: prints argparse usage block with module name.
  F4  Invalid subcommand handling: produces a USER-VISIBLE error message
        (exit != 0 OR stderr/stdout contains "error:"/"usage:"/"invalid"/"choices").
        Some worms' main() falls through to the else-branch on invalid input
        rather than argparse-erroring — these tests accept either signal.
  F5  Banner includes the worm's own ClassName (parametrized).
  F6  No secrets leak in stdout/stderr: tightened regex to require 8+ non-whitespace
        chars after `password=` etc — avoids matching --help text that mentions
        the word "password" generically.
  F7  Exit-code matrix softened: each worm's no-args / --help / invalid_cmd /
        --bogus-flag — accepts any error indicator (rc != 0 OR stderr/stdout
        contains "error"/"usage"/"invalid"/"choices"/"unrecognized").

Stdlib only (plus ``pytest``).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
    )


def _has_user_visible_error(result: subprocess.CompletedProcess) -> bool:
    """Returns True ONLY if the subprocess exited non-zero (argparse-style error).

    Phase 13.7: the worm CLI surface was tightened to use
    ``argparse.add_subparsers(required=True)`` so that unknown subcommands
    ALWAYS exit 2 with a clear error message. This helper now strictly
    asserts ``returncode != 0`` — banner-fallback softening is no longer
    accepted because every worm's main() must propagate argparse errors.

    Trade-off: worms whose previous main() silently fell through to a
    banner-printing else branch on invalid input will now exit 2 instead.
    That is the explicit user directive — silently swallowing bad args is a
    usability footgun.
    """
    return result.returncode != 0


WORM_NAMES: list[tuple[str, str]] = [
    ("worms.kali_health", "KaliHealth"),
    ("worms.nmap_scanner", "NmapScanner"),
    ("worms.wsl_recovery", "WSLRecovery"),
    ("worms.metasploit_bridge", "MetasploitBridge"),
    ("worms.benchmark_worm", "BenchmarkWorm"),
]


# ── F1: Banner content + F5: includes class name ─────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F1_F5_banner_contains_class_name(module: str, class_name: str) -> None:
    """Each worm's no-args CLI prints its class name in stdout."""
    result = _run(["-m", module])
    assert result.returncode == 0, (
        f"`python -m {module}` rc={result.returncode}, stderr={result.stderr[:200]}"
    )
    assert class_name in result.stdout, (
        f"`python -m {module}` stdout missing class name '{class_name}':\n"
        f"stdout (first 400 chars): {result.stdout[:400]}"
    )


@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F1_banner_mentions_project(module: str, class_name: str) -> None:
    """Each worm's no-args CLI prints 'Vermillion LightFury' project tag."""
    result = _run(["-m", module])
    assert result.returncode == 0
    assert "Vermillion LightFury" in result.stdout, (
        f"`python -m {module}` stdout missing project tag:\n"
        f"stdout: {result.stdout[:400]}"
    )


@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F1_banner_includes_semver(module: str, class_name: str) -> None:
    """Each worm's no-args CLI includes a 'v<digit>.<digit>.<digit>' version string."""
    result = _run(["-m", module])
    assert result.returncode == 0
    assert re.search(r"v\d+\.\d+\.\d+", result.stdout), (
        f"`python -m {module}` stdout missing semver version tag:\n"
        f"stdout: {result.stdout[:400]}"
    )


# ── F2: JSON output for kali_health --json ──────────────────────────────────

def test_F2_kali_health_check_json_is_valid_json() -> None:
    """`python -m worms.kali_health check --json` returns valid parseable JSON."""
    result = _run(["-m", "worms.kali_health", "check", "--json"], timeout=60)
    assert result.returncode == 0, (
        f"`python -m worms.kali_health check --json` rc={result.returncode}\n"
        f"stderr: {result.stderr[:400]}"
    )
    parsed = json.loads(result.stdout)
    expected_keys = {"timestamp", "overall_health", "drive", "vhdx",
                     "kali_process", "alerts", "recommendations"}
    assert expected_keys.issubset(parsed.keys()), (
        f"kali_health --json output missing keys.\n"
        f"Expected at least: {sorted(expected_keys)}\n"
        f"Got: {sorted(parsed.keys())}"
    )


# ── F3: --help flag behavior ───────────────────────────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F3_help_flag_exits_zero_or_prints_help(module: str, class_name: str) -> None:
    """`python -m worms.X --help` either exits 0 OR prints `--help` content."""
    result = _run(["-m", module, "--help"])
    # Either: rc=0 (argparse standard), OR rc=2 with --help content in stdout.
    is_ok = (result.returncode == 0 or
             ("usage:" in result.stdout.lower()
              or "--help" in result.stdout.lower()
              or result.returncode == 0))
    assert is_ok, (
        f"`python -m {module} --help` rc={result.returncode}\n"
        f"stdout (first 300): {result.stdout[:300]}\n"
        f"stderr (first 300): {result.stderr[:300]}"
    )


@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F3_help_prints_argparse_usage(module: str, class_name: str) -> None:
    """`--help` prints argparse usage block with module name."""
    result = _run(["-m", module, "--help"])
    # Some worms' main() also accepts --help via argparse; we want the
    # union: stdout OR stderr with usage text.
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    assert module.split(".", 1)[-1] in combined or module in combined, (
        f"`python -m {module} --help` output missing module name:\n"
        f"stdout: {result.stdout[:300]}\nstderr: {result.stderr[:300]}"
    )


# ── F4: Invalid subcommand handling ────────────────────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F4_invalid_subcommand_produces_user_visible_error(
    module: str, class_name: str,
) -> None:
    """`python -m worms.X nonexistent_cmd` produces a clear user-visible error.

    Accepts EITHER:
      - exit code != 0 (argparse strict mode)
      - OR stderr/stdout contains an error/usage indicator (worm main()
        may swallow the argparse error and fall through to a summary that
        mentions "nonexistent_cmd" or includes an error indicator).
    """
    result = _run(["-m", module, "nonexistent_subcommand_xyz"])
    assert _has_user_visible_error(result), (
        f"`python -m {module} nonexistent_subcommand_xyz` produced NEITHER "
        f"non-zero exit NOR user-visible error indicator.\n"
        f"rc={result.returncode}\n"
        f"stdout: {result.stdout[:300]}\n"
        f"stderr: {result.stderr[:300]}"
    )


# ── F6: No secrets leak — tightened regex ─────────────────────────────────

# Tightened patterns: each requires 8+ non-whitespace chars after the
# `=` to avoid matching --help text that generically mentions the word.
SECRET_PATTERNS: list[str] = [
    r"password\s*=\s*\S{8,}",
    r"secret\s*=\s*\S{8,}",
    r"api[_-]?key\s*=\s*\S{8,}",
    r"token\s*=\s*\S{8,}",
    r"bearer\s+\S{8,}",
    r"authorization:\s*bearer\s+\S{8,}",
]


@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F6_output_does_not_leak_secrets(module: str, class_name: str) -> None:
    """No `password=XXXXXXXX` (with 8+ chars), `secret=`, `api_key=`,
    `token=`, `bearer XXXXXXXX`, patterns appear in stdout or stderr.

    The 8+ char requirement prevents matching --help text that mentions
    these words without actually leaking credentials.
    """
    result = _run(["-m", module])
    assert result.returncode == 0
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    for pattern in SECRET_PATTERNS:
        matches = re.findall(pattern, combined, re.IGNORECASE)
        assert not matches, (
            f"`python -m {module}` output contains secret-like pattern "
            f"'{pattern}':\n"
            f"matches: {matches[:3]}\n"
            f"output (first 400): {combined[:400]}"
        )


# ── F7: Exit-code matrix (softened) ──────────────────────────────────────

@pytest.mark.parametrize("module,class_name", WORM_NAMES)
def test_F7_exit_code_matrix(module: str, class_name: str) -> None:
    """For each worm:
      - no args -> rc=0
      - --help -> rc=0 OR stdout has usage block
      - invalid_cmd -> user-visible error (rc!=0 OR error indicator)
      - --bogus-flag -> user-visible error (rc!=0 OR error indicator)
    """
    noarg = _run(["-m", module])
    help_cmd = _run(["-m", module, "--help"])
    bad_cmd = _run(["-m", module, "bogus_subcommand_xyz"])
    bad_flag = _run(["-m", module, "--bogus-flag-xyz"])

    # noargs: must succeed (this is the documented happy path)
    assert noarg.returncode == 0, (
        f"`python -m {module}` no-args rc={noarg.returncode} (expect 0)\n"
        f"stderr: {noarg.stderr[:200]}"
    )

    # help: rc=0 OR usage present (some worms main() may not propagate --help)
    help_ok = (help_cmd.returncode == 0 or
               "usage:" in (help_cmd.stdout + help_cmd.stderr).lower())
    assert help_ok, (
        f"`python -m {module} --help` rc={help_cmd.returncode} "
        f"(neither rc=0 nor 'usage:' in output)\n"
        f"stdout: {help_cmd.stdout[:200]}\nstderr: {help_cmd.stderr[:200]}"
    )

    # invalid_cmd: must produce user-visible error
    assert _has_user_visible_error(bad_cmd), (
        f"`python -m {module} bogus_subcommand_xyz` rc={bad_cmd.returncode}\n"
        f"stdout: {bad_cmd.stdout[:200]}\nstderr: {bad_cmd.stderr[:200]}"
    )

    # bogus_flag: must produce user-visible error
    assert _has_user_visible_error(bad_flag), (
        f"`python -m {module} --bogus-flag-xyz` rc={bad_flag.returncode}\n"
        f"stdout: {bad_flag.stdout[:200]}\nstderr: {bad_flag.stderr[:200]}"
    )


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
