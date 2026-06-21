# coding: utf-8
"""
build/check_no_mixed_slash.py — F11 Anti-Pattern Regression Guard (Pre-Build)

Catches the literal mixed-slash path bug from Finding #11 — the f-string
construction that emits Python-rejected paths on Linux hosts. The original
anti-pattern shape::

    win_path = f"{_SHARED_DIR}\\\\metasploit_logs\\\\{filename}"

On a Linux host, ``\\\\metasploit_logs\\\\`` is parsed as ordinary filename
characters (NOT path separators) by the kernel — yielding `[Errno 13]`
Permission denied when ``open()`` collides with the real shared directory.

Detection rule lives in ``config.f11_patterns.FSTRING_MIXED_SLASH_RE``
(single source of truth shared with ``tests/test_f11_regex.py`` and
``tests/test_wsl_path_asymmetry.py``). The strict shape requires TWO
source-backslash pairs framing a path-like identifier — eliminates raw-
string regex literals (``r"\\n"`` etc.) which never have two escapes in
a row. Regression-protected by ``tests/test_f11_regex.py`` (12-case
discriminative invariant test).

Code that already uses ``os.path.join(...)`` is exempt (the dev is doing
the right thing; allowlist line-wide for the join pattern).

Usage:
    python build/check_no_mixed_slash.py                    # scans worms/ config/
    python build/check_no_mixed_slash.py --roots worms config
    python build/check_no_mixed_slash.py --quiet
    python build/check_no_mixed_slash.py --include-tests

Exit codes:
    0  — no matches (anti-pattern is gone)
    1  — one or more matches (build should NOT proceed)
    2  — root directory missing
    3  — file read error (permissions etc.)

Stdlib only (argparse + pathlib + re). No third-party deps.
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

# Shared F11 anti-pattern detector (single source of truth — see
# config/f11_patterns.py for the regex signature + rationale).
from config.f11_patterns import FSTRING_MIXED_SLASH_RE, has_f11_anti_pattern

# Allowlist: any line that already uses os.path.join is considered safe
# (the dev is constructing the path correctly; don't false-positive on
# legitimate ``f"{os.path.join(a, b)}\\foo"`` extensions).
SAFE_JOIN_RE = re.compile(r"\bos\.path\.join\s*\(")


def iter_python_files(roots: list[Path]) -> list[Path]:
    """Collect all .py files under the given roots (recursive)."""
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            print(f"[WARN] skipping non-directory root: {root}", file=sys.stderr)
            continue
        for p in root.rglob("*.py"):
            parts = set(p.relative_to(root).parts)
            # Skip __pycache__ entirely
            if "__pycache__" in parts:
                continue
            out.append(p)
    return sorted(out)


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (line_no, line_text) hits."""
    hits: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[ERROR] {path}: {exc}", file=sys.stderr)
        return hits
    for lineno, raw in enumerate(text.splitlines(), start=1):
        # Skip comments outright (whole-line)
        if raw.lstrip().startswith("#"):
            continue
        # Allowlist path: any line that uses os.path.join is exempt.
        if SAFE_JOIN_RE.search(raw):
            continue
        # F11 detector (delegated to config.f11_patterns).
        if has_f11_anti_pattern(raw):
            hits.append((lineno, raw.strip()))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description="F11 anti-pattern regression guard (mixed-slash f-string in path construction).",
    )
    parser.add_argument(
        "--roots", nargs="+", default=["worms", "config"],
        help="Directories to scan for .py files (default: worms config)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress informational output (hits still print).",
    )
    parser.add_argument(
        "--include-tests", action="store_true",
        help="Include tests/ directory in the scan (default OFF — tests document "
             "the bug shape rather than containing production instances).",
    )
    args = parser.parse_args()

    roots = [Path(r) for r in args.roots]
    if args.include_tests:
        roots.append(Path("tests"))

    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        print(f"[ERROR] root(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    files = iter_python_files(roots)
    if not args.quiet:
        print(f"=== F11 anti-pattern guard ===")
        print(f"  roots: {', '.join(str(r) for r in roots if r.exists())}")
        print(f"  files scanned: {len(files)}")

    per_file_count: dict[Path, list[tuple[int, str]]] = {}
    total_hits = 0
    for f in files:
        hits = scan_file(f)
        if hits:
            per_file_count[f] = hits
            total_hits += len(hits)

    if total_hits:
        print(f"\n[FAIL] F11 anti-pattern detected in {len(per_file_count)} file(s), "
              f"{total_hits} match(es):", file=sys.stderr)
        for f, hits in per_file_count.items():
            for lineno, line in hits:
                snippet = line if len(line) <= 120 else line[:117] + "..."
                print(f"  {f}:{lineno}  {snippet}", file=sys.stderr)
        print(f"\nFix: replace f-string mixed-slash construction with os.path.join(). "
              f"See AUDIT.md §11 for the canonical fix shape.", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"[OK] No mixed-slash f-string anti-pattern detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
