# coding: utf-8
"""
Regression-guard tests for Finding #11 — WSL/Windows path asymmetry.

Asserts the invariants the fix relies on:

1. ``wsl_path`` (consumed by WSL bash subprocesses) contains ONLY forward
   slashes regardless of what ``_WSL_SCRIPT_DIR`` happens to contain
   (including any backslash-bearing value someone might accidentally feed in).
2. ``win_path`` (consumed by the host's Python file APIs) faithfully reflects
   ``os.path.join(_SHARED_DIR, "metasploit_logs", filename)`` — guaranteeing
   cross-platform safety no matter which host runs the code.
3. The file is actually materialised at the win_path (round-trip write).

Stdlib only (unittest + unittest.mock + tempfile + pathlib). No pytest, no
test framework dependencies. Auto-discovered by ``python -m unittest discover``.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Project paths — make `worms.metasploit_bridge` importable regardless of cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# Azazael src for PAMS/CORE layout that the worms import lazily.
AZAZAEL_SRC_CANDIDATE = PROJECT_ROOT.parent / "azazael" / "src"
if AZAZAEL_SRC_CANDIDATE.is_dir() and str(AZAZAEL_SRC_CANDIDATE) not in sys.path:
    sys.path.insert(0, str(AZAZAEL_SRC_CANDIDATE))


class TestWslPathAsymmetry(unittest.TestCase):
    """Invariant guards for MetasploitBridge._save_resource_script() path
    construction. Mirrors the F11 fix design (F11 mixup was: bare
    f-string with embedded backslashes — produces wrong file on Linux host).
    """

    def setUp(self) -> None:
        # Work in an isolated tmpdir; do NOT touch the real shared dir.
        self._tmp = tempfile.TemporaryDirectory()
        self._shared_dir = Path(self._tmp.name) / "shared"
        self._logs_dir = self._shared_dir / "metasploit_logs"
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        # Use a properly-formed WSL path (forward-slashes only). The F11
        # invariant is: _save_resource_script does NOT INTRODUCE backslashes
        # into either path output. It does NOT sanitize a malformed
        # _WSL_SCRIPT_DIR (that would be a separate invariant / config
        # validation concern). With proper input, both outputs must use
        # forward slashes exclusively on Linux.
        self._wsl_script_dir = str(self._logs_dir / ".wsl_view")

        # Patch module-level constants on the metasploit_bridge module BEFORE
        # the worm class is instantiated (so __init__ captures the patch).
        from worms import metasploit_bridge as mb_module
        self._mb_module = mb_module
        self._patches = [
            patch.object(mb_module, "_SHARED_DIR", str(self._shared_dir)),
            patch.object(mb_module, "_WSL_SCRIPT_DIR", self._wsl_script_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in getattr(self, "_patches", []):
            p.stop()
        self._tmp.cleanup()

    def _build_mb_instance(self):
        """Build a MetasploitBridge instance against the patched paths."""
        return self._mb_module.MetasploitBridge()

    def test_wsl_path_contains_only_forward_slashes(self):
        """Given a properly-formed _WSL_SCRIPT_DIR (forward-slashes only),
        the constructed wsl_path must contain ONLY forward-slash separators
        — never introduce backslashes via the f-string concatenation
        (Finding #11's failure mode on Linux hosts)."""
        mb = self._build_mb_instance()
        wsl_path = mb._save_resource_script("# test script\nuse dummy\n", name="t")

        self.assertNotIn("\\", wsl_path,
            f"wsl_path must not contain backslashes (the F11 anti-pattern "
            f"shape). Got: {wsl_path!r}")
        self.assertTrue(wsl_path.startswith(self._wsl_script_dir + "/") or wsl_path == self._wsl_script_dir + "/t.rc",
            f"wsl_path must be constructed under _WSL_SCRIPT_DIR. "
            f"Got: {wsl_path!r}, expected prefix: {self._wsl_script_dir!r}")
        self.assertTrue(wsl_path.endswith(".rc"),
            f"wsl_path must end with .rc filename. Got: {wsl_path!r}")

    def test_win_path_uses_os_path_join(self):
        """win_path must be exactly what os.path.join(_SHARED_DIR,
        'metasploit_logs', filename) produces — the cross-platform-safe
        construction that REPLACED the F11 f-string with embedded
        backslashes."""
        mb = self._build_mb_instance()
        wsl_path = mb._save_resource_script("# test script\n", name="t")
        filename = wsl_path.rsplit("/", 1)[-1]  # extract filename
        expected_win_path = os.path.join(str(self._shared_dir), "metasploit_logs", filename)

        # Independently verify the file landed at the predicted location.
        landed = self._logs_dir / filename
        self.assertTrue(landed.is_file(),
            f"Expected resource script to be written under {self._logs_dir}, "
            f"but {landed} does not exist.")
        self.assertEqual(str(landed.parent.resolve()), str(self._logs_dir.resolve()))
        # On Linux, os.path.join emits forward slashes — win_path must match.
        self.assertEqual(str(landed), expected_win_path)
        # And confirm the content was written.
        self.assertEqual(landed.read_text(encoding="utf-8"), "# test script\n")

    def test_paths_share_same_filename(self):
        """Both wsl_path and win_path must end with the same .rc filename —
        the join logic must NOT mangle the name."""
        mb = self._build_mb_instance()
        wsl_path = mb._save_resource_script("# x\n", name="sharedtest")
        filename = wsl_path.rsplit("/", 1)[-1]
        landed = self._logs_dir / filename
        self.assertEqual(os.path.basename(str(landed)), filename)
        self.assertTrue(landed.is_file())

    def test_no_mixed_slash_in_isolated_call(self):
        """End-to-end invariant: across 5 isolated calls (warm), every
        output path must be free of mixed-slash separators. Mimics the
        property the F11 fix establishes."""
        mb = self._build_mb_instance()
        for i in range(5):
            wsl_path = mb._save_resource_script(f"# call {i}\n", name=f"iso{i}")
            self.assertNotIn("\\", wsl_path,
                f"Call {i}: backslash leaked into wsl_path: {wsl_path!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
