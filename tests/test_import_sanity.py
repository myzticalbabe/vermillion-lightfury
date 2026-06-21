# coding: utf-8
"""
test_import_sanity.py — Phase 13.2 Import Sanity Audit
========================================================

Programmatic audit that locks in Phase 13.1 worm-source fix:

  1. Every worm + config module imports cleanly (no ModuleNotFoundError /
     ImportError at module-load time).
  2. ``worms.exceptions`` is the canonical exports module — all required
     attributes (``_IS_WINDOWS``, ``_IS_KALI_NATIVE``, ``_E_DRIVE``,
     ``_C_DRIVE``, ``_G_DRIVE``, ``KaliUnresponsiveError``) are present.
  3. AST-based source audit: NO bare ``from exceptions import`` or
     ``import exceptions`` import statements remain in source code. The
     Phase 13.1 bug was that ``exceptions.py`` is at ``worms/exceptions.py``
     but worms' method bodies imported it as bare ``from exceptions import X``
     (no ``worms.`` prefix), raising ``ModuleNotFoundError`` in production.
     Uses ``ast.parse`` + ``ast.walk`` so it catches multiline imports,
     aliased imports, and whole-module variants — robustly ignoring
     matching substrings inside docstrings/comment strings.
  4. All 6 main classes instantiate cleanly with a 31-char ``_session_id``
     (Phase 12.1 invariant is preserved post-Phase 13.1).
  5. Every worm + shared_dir module has a non-trivial docstring (catches
     accidentally emptied files that would silently lose functionality).

The Phase 13.0 execution-barrier tests exercised method BODIES, but with
many sub-method mocks that masked the latent import bug. The Phase 13.1.5
test cleanup removed most sub-method mocks so the bug surface area is now
small. This sanity test catches the bug at REGISTRATION TIME (Python's
import machinery) — earlier than any other test can fire. AST parses
catch the bug statically before any runtime import attempt succeeds.

Stdlib only (plus ``pytest``).
"""

import ast
import sys
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WORM_MODULES: list[str] = [
    "worms.nmap_scanner",
    "worms.kali_health",
    "worms.metasploit_bridge",
    "worms.wsl_recovery",
    "worms.benchmark_worm",
    "worms.exceptions",
    "config.shared_dir",
]


# ── Test 1: every worm + config module imports cleanly ─────────────────────

def test_all_worm_modules_import_cleanly() -> None:
    """Every worm + config module imports WITHOUT ModuleNotFoundError or ImportError.

    If this fails: a ``from X import Y`` line in some module references a
    missing module. Most common cause (post-Phase 13.1): someone added a
    bare ``from exceptions import X`` line to worm source — exceptions.py
    lives at worms/exceptions.py, so the bare import raises
    ``ModuleNotFoundError: No module named 'exceptions'``.
    """
    for mod_name in WORM_MODULES:
        try:
            importlib.import_module(mod_name)
        except (ModuleNotFoundError, ImportError) as e:
            pytest.fail(
                f"Import failed for {mod_name!r}: {type(e).__name__}: {e} — "
                f"this likely means a `from X import Y` line references a "
                f"missing module (Phase 13.1 latent-bug pattern: `from "
                f"exceptions import X` instead of `from worms.exceptions "
                f"import X`)"
            )


# ── Test 2: worms.exceptions is canonical and exports required constants ───

def test_worms_exceptions_module_canonical() -> None:
    """The canonical exceptions module is ``worms.exceptions`` and exports
    the platform-detected constants used by all worms' method bodies.

    Required exports (consumed by ``worms/_wsl_exec``, ``_check_drive``,
    ``_windows_exec``, ``_check_distro``, ``config.shared_dir._wsl_exec``,
    and the cross-worm ``verify_kali_responsive`` helper):
      - ``_IS_WINDOWS`` (bool)
      - ``_IS_KALI_NATIVE`` (bool)
      - ``_E_DRIVE`` (str — drive mount path)
      - ``_C_DRIVE`` (str)
      - ``_G_DRIVE`` (str)
      - ``KaliUnresponsiveError`` (Exception class)
    """
    mod = importlib.import_module("worms.exceptions")
    required: list[str] = [
        "_IS_WINDOWS", "_IS_KALI_NATIVE", "_E_DRIVE",
        "_C_DRIVE", "_G_DRIVE", "KaliUnresponsiveError",
    ]
    for attr in required:
        assert hasattr(mod, attr), (
            f"worms.exceptions missing required attribute {attr!r} — "
            f"check worms/exceptions.py was not accidentally emptied"
        )
    # _IS_WINDOWS / _IS_KALI_NATIVE must be bools, not None / unparsed.
    assert isinstance(mod._IS_WINDOWS, bool), (
        f"worms.exceptions._IS_WINDOWS = {mod._IS_WINDOWS!r} (expected bool) — "
        f"platform-detection logic regressed"
    )
    assert isinstance(mod._IS_KALI_NATIVE, bool), (
        f"worms.exceptions._IS_KALI_NATIVE = {mod._IS_KALI_NATIVE!r} "
        f"(expected bool) — platform-detection logic regressed"
    )


# ── Test 3: AST-based source scan — no bare ``exceptions`` imports ─────────

def test_no_bare_exceptions_imports_in_source() -> None:
    """AST-based static guard: NO bare ``from exceptions import`` or
    ``import exceptions`` import statements exist in source code.

    Catches BOTH variants the Phase 13.1 latent bug could regress into:
      - ``from exceptions import X`` (within method bodies or top-level)
      - ``import exceptions`` (whole-module import)
      - ``from exceptions import X as Y`` (aliased)
      - multiline: ``from exceptions import (\\n    X,\\n    Y,\\n)``

    Robustness advantage over regex: AST parsing ignores string literals,
    docstrings, and comments — no false positives from textual references
    in test docstrings/assertion messages. No need to skip this test file
    itself (unlike the earlier regex-based version).
    """
    bad: list[tuple[Path, int, str]] = []
    src_dirs: list[Path] = [ROOT / "worms", ROOT / "config", ROOT / "tests"]
    for src_dir in src_dirs:
        if not src_dir.exists():
            continue
        for py_file in sorted(src_dir.rglob("*.py")):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    # `from exceptions import X` (any shape):
                    #   ImportFrom.module == "exceptions" AND level == 0
                    if node.module == "exceptions" and node.level == 0:
                        try:
                            snippet = ast.unparse(node)
                        except Exception:
                            snippet = f"from exceptions import ..."
                        bad.append((py_file, node.lineno, snippet))
                elif isinstance(node, ast.Import):
                    # `import exceptions` (whole-module):
                    for alias in node.names:
                        if alias.name == "exceptions":
                            try:
                                snippet = ast.unparse(node)
                            except Exception:
                                snippet = "import exceptions"
                            bad.append((py_file, node.lineno, snippet))

    assert not bad, (
        f"Found {len(bad)} bare `exceptions` import statements (missing "
        f"`worms.` prefix — Phase 13.1 latent-bug regression):\n"
        + "\n".join(
            f"  {f.relative_to(ROOT)}:{ln}: {line}"
            for f, ln, line in bad[:20]
        )
        + ("\n  ... (truncated, more remain)" if len(bad) > 20 else "")
        + "\n\nREGRESSION: Phase 13.1 fix has been reverted. Replace "
        "`from exceptions import X` (or `import exceptions`) with "
        "`from worms.exceptions import X` (or `import worms.exceptions`)."
    )


# ── Test 4: all 6 main classes instantiate cleanly with len==31 session_id ─

def test_all_classes_instantiate_with_31_char_session_id() -> None:
    """After worms import cleanly, all 6 main classes instantiate successfully
    and produce a 31-char ``_session_id`` (Phase 12.1 invariant post-Phase 13.1).

    Failure modes:
      - ``__init__`` raises ``ModuleNotFoundError`` (Phase 13.1 latent bug
        came back via a new module).
      - ``_session_id`` format regressed (Phase 12.1 invariant).
    """
    cls_map: list[tuple[str, str]] = [
        ("worms.nmap_scanner", "NmapScanner"),
        ("worms.kali_health", "KaliHealth"),
        ("worms.metasploit_bridge", "MetasploitBridge"),
        ("worms.wsl_recovery", "WSLRecovery"),
        ("worms.benchmark_worm", "BenchmarkWorm"),
        ("config.shared_dir", "SharedDirManager"),
    ]
    for mod_name, cls_name in cls_map:
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name)
        try:
            instance = cls()
        except (ModuleNotFoundError, ImportError) as e:
            pytest.fail(
                f"{cls_name}() raised {type(e).__name__}: {e} — "
                f"__init__ triggered a missing-module import. Phase 13.1 "
                f"latent bug may have regressed."
            )
        assert hasattr(instance, "_session_id"), (
            f"{cls_name} missing _session_id attribute"
        )
        sid = instance._session_id
        assert len(sid) == 31, (
            f"{cls_name}._session_id length {len(sid)} != 31 "
            f"(got {sid!r}) — Phase 12.1 invariant regressed"
        )


# ── Test 5: every worm + shared_dir module has a non-trivial docstring ─────

def test_summary_module_docstring_present() -> None:
    """Sanity: each worm + shared_dir module has a non-trivial docstring
    (catches accidentally emptied files that could regress import sanity).
    """
    for mod_name in WORM_MODULES:
        mod = importlib.import_module(mod_name)
        assert mod.__doc__ is not None and len(mod.__doc__.strip()) > 10, (
            f"{mod_name} has empty or missing module docstring — "
            f"file may have been accidentally emptied."
        )


if __name__ == "__main__":
    # Manual run mode for quick CLI audit
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
