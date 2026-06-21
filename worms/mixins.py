# coding: utf-8
"""
worms.mixins — Shared threading helpers for Vermillion LightFury worms
======================================================================
Dedup target. Before this module existed, the same boilerplate appeared in
4 worms (nmap_scanner / kali_health / wsl_recovery / metasploit_bridge)::

    import threading
    ...
    def __init__(self):
        ...
        self._lock = threading.Lock()
        # working state that uses self._lock at construct-time

The pattern was repeated and easy to forget when adding a new worm. This
mixin centralises the lock allocation so a new worm only needs to::

    class MyWorm(ThreadSafeWormMixin, BaseWormMixin):
        def __init__(self):
            super().__init__()             # init lock
            self._pams = ...
            # ... other state

``ThreadSafeWormMixin`` MUST appear BEFORE ``BaseWormMixin`` in the
inheritance list so the lock is allocated during super().__init__() chain
before any worm subclass initializes state that reads ``self._lock``.

Public API:
    ThreadSafeWormMixin    — base class providing ``self._lock`` instance attr

Stdlib only (`threading`). No third-party deps.
"""

import threading


__all__ = ["ThreadSafeWormMixin"]


class ThreadSafeWormMixin:
    """Mixin that allocates ``self._lock = threading.Lock()`` in __init__.

    Worm subclasses MUST call ``super().__init__()`` from their own
    ``__init__`` method so the lock is initialised before any state that
    might read or use it.

    The lock design is intentionally minimal — writes are atomic against
    concurrent writes; readers (e.g. ``get_summary()``) remain
    lock-free on a snapshot basis, consistent with MetasploitBridge's
    pre-existing pattern (stale increment on a snapshot read is
    acceptable for summary purposes).
    """

    def __init__(self) -> None:
        # Forward MRO to BaseWormMixin; lock allocated NEXT line — sibling
        # mixins must not read self._lock from their own __init__ (would AttributeError).
        super().__init__()
        self._lock = threading.Lock()
