# coding: utf-8
"""
tests/conftest.py — Phase 13.3 Sessionstart Import Sanity Audit
================================================================

Promotes the AST-based import sanity check from ``tests/test_import_sanity.py``
to a pytest sessionstart autouse fixture.

WHY THIS EXISTS (per Phase 13.2 reviewer feedback):

The load-bearing regression guard is the AST walker that catches bare
``from exceptions import X`` / ``import exceptions`` (missing ``worms.``
prefix) — the Phase 13.1 latent bug. If future code reverts the fix, the
audit must fire BEFORE any test that depends on the worms importing cleanly.

If the audit lives only as ``test_no_bare_exceptions_imports_in_source``
(in ``tests/test_import_sanity.py``) and a *different* test in that same
file (``test_all_worm_modules_import_cleanly`` or
``test_worms_exceptions_module_canonical``) fails first, pytest may halt
the session before the AST audit test runs. Promoting it to a ``session``
-scoped ``autouse`` fixture guarantees it always runs first.

WHY ``pytest.exit`` INSTEAD OF ``assert`` (per Phase 13.3 reviewer feedback):

A bare ``assert`` failure on a sessionstart fixture marks the fixture as
failed but does NOT abort the pytest run — other tests still execute,
and their output potentially obscures the regression message at the
bottom of the log. CI pipelines will still see exit code != 0, but the
operator inspecting the log may miss the regression if downstream test
noise buries it.

``pytest.exit(msg, returncode=1)`` terminates pytest immediately,
prioritizing the regression message at the very top of pytest output.
Older pytest versions (< 6.2) don't have ``exit`` and fall back to
``raise AssertionError`` for the same session-failure semantics.

Stdlib only (plus ``pytest``).
"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _audit_no_bare_exceptions_imports() -> str | None:
    """Walk every .py file in worms/, config/, tests/ via ``ast.parse()`` +
    ``ast.walk``. Return ``None`` if clean, else a multi-line failure
    string describing each bad import with file + line.

    Robust against:
      - Multiline imports: ``from exceptions import (\n    X,\n   Y,\n)``
      - Aliased imports:  ``from exceptions import X as Y``
      - Whole-module:     ``import exceptions``
      - String/comment references (AST ignores non-code)
    """
    bad = []
    src_dirs = [ROOT / "worms", ROOT / "config", ROOT / "tests"]
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
                    # `from exceptions import X` (any shape) - top-level or
                    # method-body; ``level == 0`` excludes relative imports
                    # (``from .exceptions`` etc).
                    if node.module == "exceptions" and node.level == 0:
                        try:
                            snippet = ast.unparse(node)
                        except Exception:
                            snippet = "from exceptions import ..."
                        bad.append((py_file, node.lineno, snippet))
                elif isinstance(node, ast.Import):
                    # `import exceptions` (whole-module variant)
                    for alias in node.names:
                        if alias.name == "exceptions":
                            try:
                                snippet = ast.unparse(node)
                            except Exception:
                                snippet = "import exceptions"
                            bad.append((py_file, node.lineno, snippet))
    if not bad:
        return None
    return (
        f"Found {len(bad)} bare `exceptions` import statement(s) (missing "
        f"`worms.` prefix - Phase 13.1 latent-bug regression):\n"
        + "\n".join(
            f"  {f.relative_to(ROOT)}:{ln}: {line}"
            for f, ln, line in bad[:20]
        )
        + ("\n  ... (truncated, more remain)" if len(bad) > 20 else "")
        + "\n\nFIX: replace `from exceptions import X` (or "
        "`import exceptions`) with `from worms.exceptions import X` (or "
        "`import worms.exceptions`)."
    )


@pytest.fixture(scope="session", autouse=True)
def _audit_imports_sessionstart() -> None:
    """Sessionstart autouse fixture - runs ONCE at pytest session start.

    Phase 13.1 latent-bug regression guard promoted from a regular test
    (per Phase 13.2 reviewer feedback - ensures the audit always fires
    first, even if other tests later halt with ``-x``).

    Failure semantics (Phase 13.3 update): if the AST audit finds any
    bare exceptions import, the fixture calls ``pytest.exit(msg,
    returncode=1)`` which TERMINATES pytest immediately. This
    prioritizes the regression over all other tests (CI will gate on
    exit code != 0 and the failure message will be at the very top of
    pytest output).

    Requires ``pytest >= 6.2`` (Feb 2021 release — ``pytest.exit`` has
    been stable for 5+ years). If running on older pytest, this fixture
    will raise an ``AttributeError`` and the regression message will
    reach operators via the traceback rather than pytest's exit summary.
    """
    msg = _audit_no_bare_exceptions_imports()
    if msg is not None:
        pytest.exit(
            f"[Phase 13.3 SESSIONSTART] {msg}",
            returncode=1,
        )
