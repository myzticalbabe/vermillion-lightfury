# coding: utf-8
"""Vermillion LightFury — Shared Exceptions

Cross-worm exception classes used by all Vermillion LightFury modules.
Centralizing here avoids duplicate definitions drifting apart over time.
"""

import os as _os
import platform as _platform

# ── Platform detection (shared across all VF modules) ──────────────────────
_IS_WINDOWS = _platform.system() == "Windows"
_IS_KALI_NATIVE = _os.name == "posix" and not _IS_WINDOWS  # True when running on Kali Linux

# ── Cross-platform drive paths ─────────────────────────────────────────────
_C_DRIVE = "C:\\" if _IS_WINDOWS else "/mnt/c"
_E_DRIVE = "E:\\" if _IS_WINDOWS else "/mnt/e"
_G_DRIVE = "G:\\" if _IS_WINDOWS else "/mnt/g"


class KaliUnresponsiveError(Exception):
    """Raised when Kali WSL is unresponsive and auto-recovery failed.

    All worms that communicate with Kali WSL (NmapScanner, MetasploitBridge,
    etc.) raise this when Kali cannot be reached even after recovery attempts.
    """
    pass
