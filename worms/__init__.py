# coding: utf-8
"""
Vermillion LightFury Worms — Kali × Azazael Integration Worms
===============================================================

Public surface (re-exported via ``__all__``):

* 5 worm classes:
    - ``NmapScanner``        — Kali nmap via WSL (security, p=8)
    - ``KaliHealth``         — E: drive / VHDX / RAM watchdog (monitor, p=8)
    - ``MetasploitBridge``   — Kali msfconsole + CheckpointWorm (security, p=9)
    - ``WSLRecovery``        — WSL health, auto-restart, e2fsck (defense, p=8)
    - ``BenchmarkWorm``      — Weekly benchmark runner (telemetry, p=5)

* 1 mixin:
    - ``ThreadSafeWormMixin`` — ``self._lock = threading.Lock()`` via
      ``super().__init__()``; new worms inherit from this BEFORE
      ``BaseWormMixin`` so the lock is allocated during the MRO super-chain
      before any state that reads/writes under it. See ``worms/mixins.py``.

Adding a new worm? Four steps required:

  1. Define the class with
     ``@register_worm(category=..., priority=..., cooldown_seconds=..., description=...)``
  2. Inherit from ``(ThreadSafeWormMixin, BaseWormMixin)`` — mixin FIRST so
     the lock is allocated during ``super().__init__()`` before any
     worm-specific initialisation that depends on it.
  3. Call ``super().__init__()`` as the FIRST line of your worm's ``__init__``.
  4. Add ``from worms.<your_worm> import <YourWorm>`` here AND append
     ``"<YourWorm>"`` to ``__all__``.

Usage::

    >>> from worms import NmapScanner, ThreadSafeWormMixin
    >>> ns = NmapScanner()
    >>> print(ns.get_summary())
"""

# Mixin import comes BEFORE the worm classes so the order of declaration
# in this file is unambiguous (and avoids any subtle MRO surprises if a
# downstream consumer introspects locals()).
from worms.mixins import ThreadSafeWormMixin  # noqa: F401  (re-export)

# Worm classes — alphabetical for readability.
from worms.benchmark_worm import BenchmarkWorm
from worms.kali_health import KaliHealth
from worms.metasploit_bridge import MetasploitBridge
from worms.nmap_scanner import NmapScanner
from worms.wsl_recovery import WSLRecovery

__all__ = [
    # Worm classes (alphabetical).
    "BenchmarkWorm",
    "KaliHealth",
    "MetasploitBridge",
    "NmapScanner",
    "WSLRecovery",
    # Threading mixin.
    "ThreadSafeWormMixin",
]
