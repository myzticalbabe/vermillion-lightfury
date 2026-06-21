# coding: utf-8
"""
build/check_goto_chain.py — Goto-Chain Smoke Check (Pre-Build)

Stateless Python pre-build guard that parses a Windows .bat script and
verifies every ``goto :LABEL`` reference resolves to a ``:LABEL`` declaration.
Fails fast (exit 1) if any goto targets a missing label, before Nuitka is
invoked. Code-reviewer-validated heuristic against build.bat; tags unused
labels (potential fall-through guards) as informational rather than errors.

Usage:
    python build/check_goto_chain.py --script build/build.bat     # default
    python build/check_goto_chain.py --quiet

Exit codes:
    0  — goto chain is clean (all references resolve)
    1  — broken goto(s) found (one or more ``goto :X`` has no matching ``:X``)
    2  — script file not found
    3  — script file unparsable / unreadable

Stdlib only (argparse + re). No third-party deps.
"""

import argparse
import re
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `config.*` is importable when
# this script is invoked from anywhere (not just the project root). Single
# source of truth lives in build/_pathutil.py.
# NB: Use direct sibling import — `_pathutil.py` lives next to this script
# in `build/` and Python auto-adds the script's directory to sys.path[0].
from _pathutil import ensure_project_root_on_syspath
ensure_project_root_on_syspath()

# Windows batch label syntax: starts with ':' followed by an identifier.
# Per CMD: labels must be in column 0 (or after whitespace).
LABEL_RE = re.compile(r"^\s*:([A-Za-z][A-Za-z0-9_]*)\s*$")

# Goto references — match `goto :LABEL` (CMD is case-insensitive on lookup;
# we normalise to lowercase for comparison so authors catch subtle typos).
GOTO_RE = re.compile(r"\bgoto\s+:([A-Za-z][A-Za-z0-9_]*)\b", re.IGNORECASE)

# CMD comment line (legacy `::` form).
COMMENT_LINE_RE = re.compile(r"^\s*::.*$")

# REM comment form (full-line REM). Inline REM is stripped separately.
REM_LINE_RE = re.compile(r"^\s*REM\b", re.IGNORECASE)


def parse_labels_and_gotos(script_text: str) -> tuple[set[str], dict[str, list[int]]]:
    """Return (labels_set_lowercase, gotos dict mapping label_lc -> [line numbers])."""
    labels: set[str] = set()
    gotos: dict[str, list[int]] = {}
    for lineno, raw_line in enumerate(script_text.splitlines(), start=1):
        # Skip blank lines
        if not raw_line.strip():
            continue
        # Skip :: comments
        if COMMENT_LINE_RE.match(raw_line):
            continue
        # Skip REM full-line comments
        if REM_LINE_RE.match(raw_line):
            continue
        # Strip any inline REM portion (anything after REM is comment text).
        # re.split ALWAYS returns ≥1 element list (worst case: [raw_line]),
        # so [_split[0]] is safe — no need for an unreachable fallback.
        _split = re.split(r"\bREM\b", raw_line, maxsplit=1, flags=re.IGNORECASE)
        line_clean = _split[0]
        # Label declarations
        m = LABEL_RE.match(line_clean)
        if m:
            labels.add(m.group(1).lower())
            continue
        # Goto references
        for g in GOTO_RE.finditer(line_clean):
            gotos.setdefault(g.group(1).lower(), []).append(lineno)
    return labels, gotos


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Goto-chain smoke check for a Windows batch script.",
    )
    parser.add_argument(
        "--script", default="build/build.bat",
        help="Path to the .bat script (default: build/build.bat)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress informational output (broken-goto summary still prints).",
    )
    args = parser.parse_args()

    script_path = Path(args.script)
    if not script_path.is_file():
        print(f"[ERROR] Script not found: {script_path}", file=sys.stderr)
        return 2

    try:
        script_text = script_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[ERROR] Could not read {script_path}: {exc}", file=sys.stderr)
        return 3

    labels, gotos = parse_labels_and_gotos(script_text)

    if not args.quiet:
        print(f"=== goto-chain check: {script_path} ===")
        print(f"  labels defined: {len(labels)}  ({', '.join(sorted(labels))})")
        print(f"  goto references: {sum(len(v) for v in gotos.values())}  "
              f"across {len(gotos)} unique labels")

    # Broken references
    broken: dict[str, list[int]] = {
        label: lines for label, lines in gotos.items() if label not in labels
    }
    # Unused labels (informational — labels guard fall-through too)
    unused: set[str] = labels - set(gotos.keys())

    if broken:
        print(f"\n[FAIL] Goto chain broken — {len(broken)} label(s) referenced but not defined:",
              file=sys.stderr)
        for label, lines in sorted(broken.items()):
            line_list = ", ".join(str(n) for n in lines)
            print(f"  goto :{label}  on line(s): {line_list}", file=sys.stderr)
        return 1

    if not args.quiet:
        if unused:
            print(f"\n[INFO] {len(unused)} label(s) defined but never goto-referenced "
                  f"(possibly fall-through guards): {', '.join(sorted(unused))}")
        print(f"[OK] Goto chain clean — all {sum(len(v) for v in gotos.values())} "
              f"references resolve.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
