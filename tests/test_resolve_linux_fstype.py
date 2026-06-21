# coding: utf-8
"""
Regression-guard tests for KaliHealth._resolve_linux_fstype().

Longest-prefix matching for /proc/mounts + the root-mount special case
(when only `/` is mounted, paths under a 'missing' prefix inherit root's
fstype) + the false-positive guard (a /mnt2 mount must NOT match /mnt
queries) — all the failure modes mentioned in the F5 fix note.

Tests mock ``builtins.open`` so the suite is deterministic regardless of
the host system's actual /proc/mounts state.

Stdlib only (unittest + unittest.mock). No pytest. Auto-discoverable via
``python -m unittest discover tests``.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# Helper: format a /proc/mounts line (post-3.10 path field may contain spaces).
def _mount_line(dev: str, mp: str, fstype: str, opts: str = "rw,relatime") -> str:
    return f"{dev} {mp} {fstype} {opts} 0 0\n"


class TestResolveLinuxFstype(unittest.TestCase):
    """Covers longest-prefix + root + boundary cases for the
    _resolve_linux_fstype() static method on KaliHealth.
    """

    def _resolve_with_mounts(self, mounts_text: str, path: str) -> str:
        """Drive _resolve_linux_fstype() with a synthetic /proc/mounts."""
        from worms.kali_health import KaliHealth
        m_open = mock_open(read_data=mounts_text)
        with patch("builtins.open", m_open):
            return KaliHealth._resolve_linux_fstype(path)

    def test_exact_mountpoint_match(self):
        """A query that matches a mount point exactly returns its fstype."""
        mounts = _mount_line("/dev/sdd", "/", "ext4") + \
                 _mount_line("/dev/sda1", "/mnt/e", "ntfs")
        self.assertEqual(self._resolve_with_mounts(mounts, "/mnt/e"), "ntfs")
        self.assertEqual(self._resolve_with_mounts(mounts, "/"), "ext4")

    def test_nested_path_under_specific_mount(self):
        """A path that is a descendant of a mount point returns that mount's
        fstype — NOT the outer mount's fstype."""
        mounts = _mount_line("/dev/sdd", "/", "ext4") + \
                 _mount_line("/dev/sda1", "/mnt/e", "ntfs")
        # /mnt/e/foo/bar.txt → ntfs (descendant of /mnt/e), not ext4
        self.assertEqual(self._resolve_with_mounts(mounts, "/mnt/e/Kali/ext4.vhdx"), "ntfs")

    def test_longest_prefix_wins(self):
        """When two mountpoints could match, the LONGER match must win."""
        mounts = _mount_line("overlay", "/mnt", "overlay") + \
                 _mount_line("/dev/sda1", "/mnt/c", "9p")
        # /mnt/c/foo is under both /mnt (overlay) and /mnt/c (9p).
        # /mnt/c wins because it is the longer prefix.
        self.assertEqual(self._resolve_with_mounts(mounts, "/mnt/c/foo"), "9p")

    def test_root_special_case_when_only_root_mounted(self):
        """When only '/' is in /proc/mounts, any descendant path inherits
        root's fstype. Falling back to "unknown" here would break the
        F5 fix's purpose: it should always find SOMETHING when root is
        mounted."""
        mounts = _mount_line("/dev/sdd", "/", "ext4")
        # /mnt/c/azazael_shared has no exact /mnt/c mount but IS a
        # descendant of '/'. The root special case returns ext4.
        self.assertEqual(self._resolve_with_mounts(mounts, "/mnt/c/azazael_shared"), "ext4")

    def test_no_match_returns_unknown(self):
        """A path that is not under any mount and '/' is also missing
        returns 'unknown' as the explicit fallback."""
        # Empty /proc/mounts — no root, no other mounts.
        self.assertEqual(self._resolve_with_mounts("", "/mnt/e/Kali"), "unknown")

    def test_false_positive_mount2_does_not_match_mnt(self):
        """The rstrip('/') normalisation prevents /mnt2 from matching /mnt.
        Critical guard against partial-prefix false positives."""
        mounts = _mount_line("/dev/sdc1", "/mnt2", "ext4") + \
                 _mount_line("/dev/sdd", "/", "ext4")
        # /mnt2 has its own mount; querying /mnt2 does NOT match /mnt2
        # as if it were under /mnt. The query /mnt2 matches /mnt2 itself.
        self.assertEqual(self._resolve_with_mounts(mounts, "/mnt2/foo"), "ext4")

    def test_unreadable_proc_mounts_returns_unknown(self):
        """PermissionError / FileNotFoundError path: returns 'unknown'
        rather than raising."""
        from worms.kali_health import KaliHealth
        with patch("builtins.open", side_effect=PermissionError("denied")):
            self.assertEqual(KaliHealth._resolve_linux_fstype("/mnt/e"), "unknown")
        with patch("builtins.open", side_effect=FileNotFoundError("/proc/mounts missing")):
            self.assertEqual(KaliHealth._resolve_linux_fstype("/mnt/e"), "unknown")

    def test_handles_malformed_mounts_lines(self):
        """Lines with fewer than 3 fields are skipped (don't crash the
        overall parse)."""
        # Mutated mix: one valid root mount, one malformed 2-field line.
        mounts = "garbage line without enough fields\n" + \
                 _mount_line("/dev/sdd", "/", "ext4")
        self.assertEqual(self._resolve_with_mounts(mounts, "/mnt/c"), "ext4")
        self.assertEqual(self._resolve_with_mounts(mounts, "/"), "ext4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
