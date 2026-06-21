# coding: utf-8
"""
config.f11_patterns — Shared Finding #11 anti-pattern detector
==============================================================
Dedup target. Before this module existed, the F11 anti-pattern regex
``FSTRING_MIXED_SLASH_RE`` lived only inside ``build/check_no_mixed_slash.py``.
The F11 shape was implicit in ``tests/test_wsl_path_asymmetry.py`` without a
single point of truth.

This module extracts the regex + a convenience helper so:
  * ``build/check_no_mixed_slash.py`` (build-time guard)
  * ``tests/test_wsl_path_asymmetry.py`` (regression test)
  * any future in-process detector

can all import the SAME discriminator. When Finding #11's anti-pattern
shape changes (e.g. someone adds a new instance), updating this file
auto-propagates to every consumer.

Public API:
    FSTRING_MIXED_SLASH_RE     — compiled regex pattern (raw-string aware)
    find_f11_anti_patterns(source: str) -> list[(line_no, line_text)]
                                    — locate every F11-shape line in source
    has_f11_anti_pattern(line: str) -> bool
                                    — single-line detection helper

Signature rules (verified by integration test in
``tests/test_wsl_path_asymmetry.py``):
  1. PLAIN f-string prefix (``f"`` or ``f'``) — NOT a raw string (``r"``,
     ``rf"``) which legitimately carries regex/literal escape content.
  2. Two source-backslash pairs bracketing a path-like identifier token
     of ≥2 chars total. In a raw regex pattern, two source backslashes
     are expressed as 4 characters ``\\\\`` (each ``\\`` matches one
     literal backslash, so ``\\\\`` matches two). This eliminates
     single-char escapes (``\\n``, ``\\t``, ``\\d``, ``\\w``, etc.)
     which never appear twice in a row in legitimate Python source.
  3. The body between the two backslash-pair segments has no backslashes
     (so partial-path fragments like ``f"{var}\\suffix"`` are correctly
     rejected — they only have one backslash pair, not two).

Stdlib only (`re`). No third-party deps.
"""

import re

__all__ = ["FSTRING_MIXED_SLASH_RE", "find_f11_anti_patterns", "has_f11_anti_pattern"]


# Strict two-segment shape detector.
# Anonymous in source, exported under a canonical alias.
#
# Why 4 raw-string chars per segment marker?
#   In raw regex `r'\\\\'`, there are 4 literal chars: \, \, \, \.
#   The re engine interprets this as TWO escapes `\\` + `\\`, each
#   matching ONE ASCII backslash. So `r'\\\\'` matches TWO ASCII
#   backslashes in the target text. With 2 chars `r'\\'`, the pattern
#   would only match ONE backslash — too lenient, false-positives on
#   `f"{var}\n_subscript"` shapes that don't have two backslashes.
FSTRING_MIXED_SLASH_RE = re.compile(
    r'''\bf["']                          # plain f-string prefix (NOT raw)
        [^"\\]*?                         # body before 1st path segment (lazy — no backslashes)
        \\\\                             # TWO source backslashes
        [A-Za-z_][A-Za-z0-9_]{1,}        # path-segment identifier (>=2 chars)
        [^"\\]*?                         # body between segments (lazy — no backslashes)
        \\\\                             # TWO source backslashes (closing segment)
        [^"\\]*                          # trailing body (greedy — no backslashes)
        ["']                             # closing string bound
    ''',
    re.VERBOSE,
)


def has_f11_anti_pattern(line: str) -> bool:
    """Return True if `line` contains an F11-shape mixed-slash f-string.

    Convenience wrapper around ``FSTRING_MIXED_SLASH_RE.search``.
    """
    return bool(FSTRING_MIXED_SLASH_RE.search(line))


def find_f11_anti_patterns(source: str) -> list[tuple[int, str]]:
    """Scan a source string and return ``[(line_no, line_text), ...]`` for
    every line containing an F11-shape anti-pattern.

    Useful for in-process detectors that want richer reporting than a
    boolean per line. Line numbers are 1-indexed (matches `open()` /
    editor conventions).
    """
    hits: list[tuple[int, str]] = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        if FSTRING_MIXED_SLASH_RE.search(raw):
            hits.append((lineno, raw.strip()))
    return hits
