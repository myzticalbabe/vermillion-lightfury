# coding: utf-8
"""
build._pathutil — Shared path/sys helpers for build/* scripts
=============================================================
Dedup target. Before this module existed, the same 5-line ``_PROJECT_ROOT
= Path(__file__).resolve().parent.parent; if str(_PROJECT_ROOT) not in
sys.path: sys.path.insert(0, str(_PROJECT_ROOT))`` block was copy-pasted
into both ``build/check_no_mixed_slash.py`` AND ``build/check_goto_chain.py``,
plus required the same ``import sys / from pathlib import Path`` header.
Any future build/* script that needs to import ``config.*`` can call
``ensure_project_root_on_syspath()`` once at module top instead of pasting
the sys.path manipulation block inline.

Public API:
    ensure_project_root_on_syspath()  — idempotent (no-op if already present)

Stdlib only. No third-party deps.
"""

import sys
from pathlib import Path


def ensure_project_root_on_syspath() -> str:
    """Add this build script's project root to ``sys.path`` if not already present.

    Resolves ``Path(__file__).resolve().parent.parent`` — i.e. the parent
    of the ``build/`` directory hosting the caller script — and inserts it
    at ``sys.path[0]`` so that ``config.*`` and other project-root modules
    are importable regardless of the invocation cwd.

    Idempotent: safe to call multiple times (no-op when already on path).

    Returns:
        The absolute project-root path as a string (useful for diagnostics).
    """
    project_root = Path(__file__).resolve().parent.parent
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)
    return project_root_str


__all__ = ["ensure_project_root_on_syspath"]
