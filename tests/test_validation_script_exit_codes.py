# coding: utf-8
"""
test_validation_script_exit_codes.py — Phase 11.8 Exit-Code Capture Sanity
=========================================================================

Regression test for the shell-level exit-code capture artifact caught during
the Phase 11.7 regression_guard.py validation report.

ROOT CAUSE (Phase 11.7):
    A validator bash script ran:

        python3 regression_guard.py ... > /tmp/rg.txt 2>&1
        echo "exit code: $?"      # <-- captures ECHO's exit (always 0)

    The shell pattern ``python3 foo.py > file; echo $?`` ALWAYS reports 0
    because ``echo`` is the last command in the sequence and ``$?`` reflects
    the previous command's exit status — i.e. ``echo``'s, not python's.

CORRECT PATTERNS:
    (a) Capture into a variable immediately after python:
            python3 foo.py > /tmp/x.txt 2>&1; rc=$?
            echo "exit code: $rc"
    (b) Use PIPESTATUS for pipe-aware capture:
            python3 foo.py | tee /tmp/x.txt
            echo "exit code: ${PIPESTATUS[0]}"
    (c) Python subprocess.run(...) + .returncode — robust, no shell issues.

This test scans the project for shell-style exit-code capture AFTER a
redirect and asserts ALL such occurrences use ``rc=$?`` (variable capture)
or are inside a known-safe wrapper (subprocess.run returncode, os.system).

Phase 11.8 — closed inventory item #5's "future CI gate" followup.
Stdlib only. Module is NOT gated to free-threaded Python — exit-code
handling is ortho-gonal to the GIL; this is a static-analysis guard.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ── Patterns ────────────────────────────────────────────────────────────────

# DANGEROUS: ``python3 ... > ...; echo ... $?`` — captures echo's (always 0).
# Anchored on a newline so we don't flag the middleware ``...; echo ...``
# Python code (which is fine — Python doesn't have this failure mode).
DANGEROUS_ECHO_AFTER_REDIRECT = re.compile(
    # Single-line: ``python3 foo > x; echo $?``  (anti-pattern capped to ``echo``)
    r"(?m)^[^\n]*python3?\s+[^|\n;&]*[>|]+\s*[^|\n;&]*;[^|\n&]*echo[^|\n&]*\$?")

# Multi-line detection pattern: ``PATTERN_PYTHON_REDIRECT`` — captures the
# python+redirect set-up line for the cross-line scan. Companion regex used in
# _find_dangerous_capture() looks at NEXT 5 lines for an ``echo ... $?`` line.
# Safe (``rc=$?`` capture) variant matches immediately.
PATTERN_PYTHON_REDIRECT = re.compile(
    r"(?m)^[^\n]*python3?\s+[^|\n;&]*[>|]+\s*[^|\n;&]*"
)
# Tightened PATTERN_ECHO_DOLLAR_QUESTION: requires the literal ``$?`` to be
# followed by a NON-IDENTIFIER boundary (whitespace, EOL, close-paren, colon,
# quote, or similar) so it does NOT false-flag ``$rc`` or ``$rc_var`` patterns.
# Phase 13.0 second-review finding: the previous ``[^|\n&]*\$?`` regex matched
# ``echo "now showing: $rc"`` because ``\?`` made ``$`` optional with no
# post-anchor.
PATTERN_ECHO_DOLLAR_QUESTION = re.compile(
    r"(?m)^[^\n]*echo\b[^|\n&]*\$\?(?:[\s)\]\"'\;,]|$)"
)
PATTERN_RC_CAPTURE = re.compile(
    r"(?m)^[^\n]*rc\s*=\s*\$?"
)

# Files explicitly excluded from the dangerous-pattern scan:
#   - ``__pycache__/``: compiled bytecode (not source)
#   - ``.git/``: vendored — git hooks use $? legitimately for ``git`` invocations
#   - ``node_modules/``: pre-emptive if any future Node tooling arrives
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "data"}

# Files that legitimately use $? for non-python invocation (e.g. wsl, bash)
# — allowlisted by exact relative path so a future regression can't sneak in.
PATH_ALLOWLIST = {
    # Allow baseline-windows-bash-passthrough comments / docs
    "tests/test_validation_script_exit_codes.py",  # self-reference; the pattern detector itself
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _iter_python_scripts() -> list[Path]:
    """Yield all source files potentially containing shell exit-code patterns.

    Scope: .py + .sh files at the project root (not recursive walks), plus
    CI scripts if present. Excludes tests/ (covered separately) and any
    EXCLUDE_DIRS.
    """
    candidates: list[Path] = []
    for ext in ("*.sh", "*.bash", "*.py"):
        for p in ROOT.rglob(ext):
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if any(p.name == name for name in ("__init__.py",)):
                continue
            candidates.append(p)
    return candidates


def _find_dangerous_capture() -> list[tuple[Path, int, str]]:
    """Return (file, line_no, matched_line) for every dangerous capture pattern.

    Handles BOTH single-line and multi-line anti-patterns:

    Single-line: ``python3 ... > ...; echo ... $?`` — the redirect + echo are on the same line.

    Multi-line (the actual Phase 11.7 offender pattern):
        python3 foo > /tmp/x.txt 2>&1
        echo "exit code: $?"

    For multi-line detection, scan line-by-line:
      - Find a line matching PATTERN_PYTHON_REDIRECT (python + redirect).
      - Look at the next 5 lines for PATTERN_ECHO_DOLLAR_QUESTION (echo ... $?).
      - Confirm PATTERN_RC_CAPTURE does NOT appear in between (would whitelist the
        safe pattern ``python3 ... > ; rc=$?; echo $rc``).

    Safe patterns (whitelisted):
      - Captures ``rc=$?`` BEFORE the echo line (single OR multi-line).
      - Uses subprocess.run(...).returncode (Python-side, immune to shell artifact).
    """
    hits: list[tuple[Path, int, str]] = []
    for p in _iter_python_scripts():
        rel = p.relative_to(ROOT).as_posix()
        if rel in PATH_ALLOWLIST:
            continue
        # Self-test: exclude this very file's dangerous-pattern strings
        if rel == "tests/test_validation_script_exit_codes.py":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()

        # --- Path A: single-line dangerous capture ---
        for line_idx, line in enumerate(lines):
            for match in DANGEROUS_ECHO_AFTER_REDIRECT.finditer(line):
                line_no = line_idx + 1
                # Whitelist: rc=$? on the SAME line OR on the immediately preceding 2 lines.
                nearby_lines = lines[max(0, line_idx - 2): line_idx + 1]
                if any(PATTERN_RC_CAPTURE.search(L) for L in nearby_lines):
                    continue
                hits.append((p, line_no, line.strip()))

        # --- Path B: multi-line dangerous capture ---
        for line_idx, line in enumerate(lines):
            if not PATTERN_PYTHON_REDIRECT.match(line):
                continue
            python_line_no = line_idx + 1
            # Look at next 5 lines for the echo-with-$? capture.
            window_end = min(len(lines), line_idx + 6)
            rc_seen_in_window = False
            echo_match_found = False
            for j in range(line_idx + 1, window_end):
                if PATTERN_RC_CAPTURE.match(lines[j]):
                    rc_seen_in_window = True
                    break
                if PATTERN_ECHO_DOLLAR_QUESTION.match(lines[j]):
                    echo_match_found = True
                    # Don't return the python_line_no; report the echo line so
                    # the diagnostic message tells the operator which echo is the bug.
                    hits.append((p, j + 1, lines[j].strip()))
            # Catch the case where echo-with-$? was found AND no rc=$? in window
            # (the for-loop's break on rc_seen_in_window already handles the safe case)
            _ = (rc_seen_in_window, echo_match_found)  # silence unused-var warnings
    return hits


# ── F-series: Capture-Artifact Guard ────────────────────────────────────────


def test_F1_no_dangerous_echo_after_python_redirect_in_project() -> None:
    """F1 (Phase 11.8): No file in the project uses ``python3 ... > file; echo $?``
    without an intermediate ``rc=$?`` capture. This is the EXACT pattern that
    caused the Phase 11.7 regression — exit code was reported as 0 even
    when python returned 1.
    """
    dangerous = _find_dangerous_capture()
    rel_paths = [str(p.relative_to(ROOT)) for p, _, _ in dangerous]
    assert not dangerous, (
        f"Found {len(dangerous)} shell-level exit-code capture artifact(s) "
        f"(the same Phase 11.7 lesson):\n"
        f"  Files: {rel_paths}\n\n"
        f"FIX OPTIONS (any one is correct):\n"
        f"  (a) python3 foo.py > /tmp/x.txt 2>&1; rc=$?\n"
        f"      echo \"exit: $rc\"\n"
        f"  (b) subprocess.run([sys.executable, 'foo.py']).returncode\n"
        f"  (c) Use PIPESTATUS: echo \"exit: ${{PIPESTATUS[0]}}\"\n\n"
        f"See MILESTONE.md Phase 11.7 for the original false-positive report."
    )


def test_F2_regression_guard_does_not_capture_echo_after_redirect() -> None:
    """F2: Specific regression check against regression_guard.py — the
    canonical Phase 11.7 offender. Failing this means the validator is
    STILL vulnerable to its own bug."""
    rg_path = ROOT / "regression_guard.py"
    if not rg_path.exists():
        pytest.skip("regression_guard.py not present (project layout shift)")
    text = rg_path.read_text(encoding="utf-8", errors="replace")
    # The pattern: a line ending in a redirect, immediately followed by an
    # echo of $? without rc=$?. Direct scan (sum of `:` per line in bash
    # is the trigger).
    for match in DANGEROUS_ECHO_AFTER_REDIRECT.finditer(text):
        # Allowed mitigations: rc=$? on the SAME or prior line, OR use of
        # subprocess.run returncode (Python-side, immune to shell artifact).
        line_no = text[: match.start()].count("\n") + 1
        preceding = text.splitlines()[max(0, line_no - 3): line_no]
        if any(PATTERN_RC_CAPTURE.search(L) for L in preceding):
            continue
        if SAFE_SUBPROCESS_WRAPPER.search(text[max(0, match.start() - 1000): match.end() + 1000]):
            continue
        pytest.fail(
            f"regression_guard.py:{line_no}: shell-level exit-code capture "
            f"artifact pattern detected (this file was the canonical "
            f"Phase 11.7 offender — see MILESTONE Phase 11.7)\n"
            f"  matched line: {text.splitlines()[line_no - 1].strip()!r}"
        )
    # Multi-line variant: scan for the actual Phase 11.7 offender pattern
    # ``python3 foo > /tmp/x.txt`` followed by ``echo "exit code: $?"`` on a
    # subsequent line, without ``rc=$?`` in between.
    lines = text.splitlines()
    for line_idx, line in enumerate(lines):
        if not PATTERN_PYTHON_REDIRECT.match(line):
            continue
        window_end = min(len(lines), line_idx + 6)
        for j in range(line_idx + 1, window_end):
            if PATTERN_RC_CAPTURE.match(lines[j]):
                break  # safe — rc captured before echo
            if PATTERN_ECHO_DOLLAR_QUESTION.match(lines[j]):
                pytest.fail(
                    f"regression_guard.py:{j + 1}: MULTI-LINE shell-level "
                    f"exit-code capture artifact (the actual Phase 11.7 "
                    f"offender pattern)\n"
                    f"  python-line: {line.strip()!r}\n"
                    f"  echo-line:   {lines[j].strip()!r}\n"
                    f"  Fix: rc=$? captured immediately after the python line."
                )


def test_F3_python_helpers_use_subprocess_run() -> None:
    """F3: Project python orchestration code prefers subprocess.run() over
    os.system() / shell=True passthroughs. No new shell-pattern scripts
    were added in Phase 11.8+.
    """
    new_suspect_files = list(ROOT.rglob("*.py"))
    offenders: list[tuple[Path, int]] = []
    for p in new_suspect_files:
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Look for ``os.system(`` — caller-managed shell-out, fragile to
        # exit-code capture issues (Python doesn't surface rc reliably
        # without extra guard).
        if "os.system" in text and "exit code" not in text.lower():
            # Allow if the file documents its limitation in a top comment
            line_no = text.find("os.system")
            offenders.append((p, text[:line_no].count("\n") + 1))
    assert not offenders, (
        f"os.system() in {len(offenders)} file(s) without exit-code handling: "
        f"{[(str(p.relative_to(ROOT)), ln) for p, ln in offenders]}.\n"
        f"Prefer subprocess.run(..., check=False).returncode for robust "
        f"exit-code capture (immune to shell $? artifacts)."
    )


def test_F4_phase11_8_lesson_persists_in_milestone() -> None:
    """F4: The Phase 11.7 exit-code lesson must be discoverable from
    MILESTONE.md (cross-team anchor — future maintainers reading just
    MILESTONE must see the lesson). Uses a single stable anchor phrase to
    avoid brittleness to prose rewrites."""
    milestone = ROOT / "MILESTONE.md"
    if not milestone.exists():
        pytest.skip("MILESTONE.md not present")
    text = milestone.read_text(encoding="utf-8", errors="replace")
    # ONE stable sentinel: the exact phrasing from Phase 11.7. If the lesson
    # is rephrased, the lesson is missing — adjust this single string.
    # Rationale: 4 disjunctive keywords proved fragile to MILESTONE rewrites;
    # one anchor phrase is harder to drift away from accidentally.
    stable_anchor = "shell-level exit-code capture artifact"
    assert stable_anchor in text.lower(), (
        f"MILESTONE.md does not contain the stable Phase 11.7 anchor:\n"
        f"  expected phrase: {stable_anchor!r}\n"
        f"  Status: lesson has drifted or been removed.\n"
        f"  Future validators risk tripping the same regression.\n"
        f"Fix: re-add the lesson text or update this anchor phrase in lockstep "
        f"with MILESTONE.md."
    )


# ── Phase 11.8 Server-side cross-reference ───────────────────────────────────
# Reinforces: the dangerous pattern is detect-only; the test fixture is
# part of the project's CI defense surface (intent: the F-series is fast
# enough to run on every CI gate, even on 3.13 GIL-enabled Python).
