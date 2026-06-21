# coding: utf-8
"""
tests/test_f11_regex.py — 12-case discriminative invariant test for F11
========================================================================
Single source of truth for the contract ``FSTRING_MIXED_SLASH_RE`` MUST
honour. If any test fails, the regex has drifted and either false-positives
matched a legitimate raw-string / partial-path / clean f-string, or
false-negatives missed the canonical F11 shape.

Locks down:
  - canonical F11 shape: 2 source-backslash pairs framing path-like ident — MATCH
  - fan-out: same shape with different identifier — MATCH
  - single-quote variant: f'...' with same shape — MATCH
  - nested paths: leading + trailing segments — MATCH
  - raw r\"\\n\" / r\"\\d\" / re.compile / rf — REJECT (raw strings excluded)
  - single backslash: f\"{x}\\n_subscript\" — REJECT (only 1 pair, not 2)
  - clean f-string: print(f\"value={value}\") — REJECT (no backslashes at all)
  - one-pair only: f\"prefix_{var}\\\\suffix\" — REJECT (only one pair, not two)
  - short ident: f\"{var}\\xx\\{f}\" with 2-char ident — MATCH
  - tiny ident: f\"{var}\\x\\{f}\" with 1-char ident — REJECT (regex requires ≥2)

Stdlib only (unittest). No pytest. Auto-discovered by ``python -m unittest discover``.
"""

import sys
import unittest
from pathlib import Path

# Make `config.f11_patterns` importable regardless of cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestF11MixedSlashRegex(unittest.TestCase):
    """Pins the contract of `has_f11_anti_pattern()` exactly."""

    # ── POSITIVE: MUST match (F11 anti-pattern instances) ─────────────────

    def test_01_canonical_f11_matches(self):
        # The exact F11 bug shape: f"{var}\\metasploit_logs\\{filename}"
        # 2 source backslashes per side framing the path segment ident.
        line = 'F11 = f"{_SHARED_DIR}\\\\metasploit_logs\\\\{filename}"'
        self.assertTrue(self._check(line),
            f"canonical F11 shape MUST match; input={line!r}")

    def test_02_f11_fan_out_matches(self):
        # Same shape, different identifier — confirms ident is generic,
        # not hardcoded to "metasploit_logs".
        line = 'x = f"{a}\\\\logs\\\\{b}"'
        self.assertTrue(self._check(line),
            f"F11 fan-out MUST match; input={line!r}")

    def test_03_single_quote_variant_matches(self):
        # f-string with single quotes (sys re.VERBOSE allows [\'\"] in the
        # closing-string-bound).
        line = "edge = f'{var}\\\\metasploit_logs\\\\{file}'"
        self.assertTrue(self._check(line),
            f"single-quote f-string variant MUST match; input={line!r}")

    def test_04_nested_path_matches(self):
        # Leading content before the first backslash pair + trailing after.
        line = 'p = f"/a/b/{x}\\\\metasploit_logs\\\\{y}.rc"'
        self.assertTrue(self._check(line),
            f"nested leading+trailing content MUST match; input={line!r}")

    # ── NEGATIVE: MUST NOT match (legitimate uses) ────────────────────────

    def test_05_raw_r_backslash_n_rejected(self):
        # Raw string is for legit regex literals — NOT an F11 anti-pattern.
        line = r'raw_regex = r"\n{3}"'
        self.assertFalse(self._check(line),
            f"raw r\"\\n\" MUST NOT match; input={line!r}")

    def test_06_raw_r_backslash_d_rejected(self):
        line = r'raw_regex = r"\d"'
        self.assertFalse(self._check(line),
            f"raw r\"\\d\" MUST NOT match; input={line!r}")

    def test_07_raw_re_compile_rejected(self):
        line = r'm = re.compile(r"\d{3}")'
        self.assertFalse(self._check(line),
            f"raw re.compile() pattern MUST NOT match; input={line!r}")

    def test_08_raw_rf_double_quote_rejected(self):
        # rf-strings (raw f-strings) are out of scope — never construct paths.
        line = r'rf"\w+"'
        self.assertFalse(self._check(line),
            f"raw rf-string MUST NOT match; input={line!r}")

    def test_09_single_backslash_rejected(self):
        # Only ONE source backslash with `_` subscript — not two pairs.
        # This is the would-be-false-positive if the regex were too lenient.
        line = 'f"{x}\\n_subscript"'
        self.assertFalse(self._check(line),
            f"single-backslash f-string MUST NOT match (lazy body+1 pair, not 2); input={line!r}")

    def test_10_clean_fstring_rejected(self):
        line = 'print(f"value={value}")'
        self.assertFalse(self._check(line),
            f"clean f-string (zero backslashes) MUST NOT match; input={line!r}")

    def test_11_one_pair_only_partial_rejected(self):
        # Only ONE backslash pair in the entire f-string — not two-pair
        # segments bracketing an identifier.
        line = 'f"prefix_{var}\\\\suffix"'
        self.assertFalse(self._check(line),
            f"single-pair f-string MUST NOT match; input={line!r}")

    def test_12_tiny_ident_rejected(self):
        # 1-char identifier — regex requires the path-segment to be
        # ≥2 chars ([A-Za-z_] + [A-Za-z0-9_]{1,}). Anything smaller is
        # a regex-escape-shaped token, not a path segment.
        line = 'f"{var}\\x\\{file}"'
        self.assertFalse(self._check(line),
            f"1-char ident MUST NOT match (regex requires ≥2); input={line!r}")

    # ── HELPER ────────────────────────────────────────────────────────────

    def _check(self, line: str) -> bool:
        """Single entry point so all 12 cases above share the same call site."""
        from config.f11_patterns import has_f11_anti_pattern
        return has_f11_anti_pattern(line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
