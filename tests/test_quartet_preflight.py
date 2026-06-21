# coding: utf-8
"""
tests/test_quartet_preflight.py — Phase 14.3 quartet_preflight tests
====================================================================

Coverage of the 4-pillar gate's structural behavior with mocked
subprocess.run + path probes. Each test patches the KALI/UBUNTU/W11/G
pillars to point at tmp dirs so the test run is hermetic — it does NOT
touch the real /root/vermillion_lightfury, /mnt/* paths, or wsl.exe.

Exit-code matrix targeted:
  0   all 4 reachable + vermillion detected
  1   ≥1 pillar unreachable / vermillion missing under --strict
  2   ≥1 pillar reachable but vermillion missing (lenient default)
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

QUARTET = Path(__file__).resolve().parents[1] / "tools" / "quartet_preflight.py"
if not QUARTET.exists():
    pytest.skip(
        f"tools/quartet_preflight.py not found at {QUARTET}",
        allow_module_level=True,
    )

spec = importlib.util.spec_from_file_location("quartet_preflight", str(QUARTET))
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules["quartet_preflight"] = mod
spec.loader.exec_module(mod)


# ── Helper: fake `cmd.exe wsl -l -v` stdout ───────────────────────────────


def fake_wsl_output(state: str = "Stopped", distro: str = "Ubuntu-22.04") -> str:
    """Build a fake `cmd.exe /c wsl -l -v` stdout with one Ubuntu row."""
    return (
        "  NAME            STATE           VERSION\n"
        "* Kali            Running         2\n"
        f"* {distro:<15} {state:<14} 2\n"
        "  docker-desktop  Stopped         2\n"
    )


def _stub_wsl(state: str = "Stopped", distro: str = "Ubuntu-22.04"):
    """Patch `subprocess.run` to return a fake wsl-l-v output."""
    fake = fake_wsl_output(state, distro)
    p = patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fake, stderr="",
        ),
    )
    return p


# ── Probes ────────────────────────────────────────────────────────────────


def test_probe_wsl_calls_wsl_exe_directly():
    """Regression guard: _probe_wsl must invoke `wsl.exe` directly with
    `cwd="/mnt/c"` and `encoding="utf-16le"`, NOT via `cmd.exe` wrapper.
    The cmd.exe path is broken on Kali WSL due to UNC-CWD contamination.
    """
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args[0]
        captured["cwd"] = kwargs.get("cwd")
        captured["encoding"] = kwargs.get("encoding")
        # Empty stdout so the parser yields []
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = mod._probe_wsl(timeout=5)

    assert captured["args"] == ["wsl.exe", "-l", "-v"], (
        f"_probe_wsl must call wsl.exe directly, got {captured['args']!r}"
    )
    assert captured["cwd"] == "/mnt/c", (
        f"_probe_wsl must set cwd=/mnt/c to dodge UNC-CWD contamination, "
        f"got {captured['cwd']!r}"
    )
    assert captured["encoding"] == "utf-16le", (
        f"_probe_wsl must use utf-16le encoding for wsl.exe native output, "
        f"got {captured['encoding']!r}"
    )
    assert result == []


def test_probe_wsl_parses_ubuntu_running():
    with _stub_wsl("Running"):
        names = mod._probe_wsl(timeout=5)
    assert any("ubuntu" in n for n in names)
    assert "kali" in names


def test_probe_wsl_parses_ubuntu_stopped():
    with _stub_wsl("Stopped"):
        names = mod._probe_wsl(timeout=5)
    assert any("ubuntu" in n for n in names)


def test_probe_wsl_handles_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
        names = mod._probe_wsl(timeout=5)
    assert names == []


def test_probe_wsl_handles_no_cmd_exe():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        names = mod._probe_wsl(timeout=5)
    assert names == []


def test_probe_path_present_with_worms(tmp_path):
    fake = tmp_path / "vermillion_lightfury"
    fake.mkdir()
    (fake / "worms").mkdir()
    reachable, present, msg = mod._probe_path(fake)
    assert reachable is True
    assert present is True
    assert "worms/" in msg


def test_probe_path_present_no_worms(tmp_path):
    fake = tmp_path / "not-vermillion"
    fake.mkdir()
    reachable, present, _ = mod._probe_path(fake)
    assert reachable is True
    assert present is False


def test_probe_path_absent(tmp_path):
    p = tmp_path / "nope"
    reachable, present, _ = mod._probe_path(p)
    assert reachable is False
    assert present is False


def test_probe_drvfs_writable_writes(tmp_path):
    fake = tmp_path / "g" / "vermillion_lightfury"
    fake.mkdir(parents=True)
    reachable, present, msg = mod._probe_drvfs_writable(fake)
    assert reachable is True
    assert present is True
    assert "writable" in msg


def test_probe_drvfs_writable_missing_under_mounted(tmp_path):
    parent = tmp_path / "g"
    parent.mkdir()
    fake = parent / "vermillion_lightfury"
    reachable, present, msg = mod._probe_drvfs_writable(fake)
    assert reachable is True
    assert present is False
    assert "missing" in msg


# ── Pillar reporting ──────────────────────────────────────────────────────


def test_ubuntu_running_pass():
    with _stub_wsl("Running"):
        with patch.object(mod, "_probe_ubuntu_bridge", return_value=True):
            r = mod.report_pillar(mod.UBUNTU, strict=False)
    assert r.reachable is True
    assert r.vermillion_present is True
    assert r.severity == "pass"


def test_ubuntu_stopped_lenient_warn():
    with _stub_wsl("Stopped"):
        with patch.object(mod, "_probe_ubuntu_bridge", return_value=False):
            r = mod.report_pillar(mod.UBUNTU, strict=False)
    assert r.reachable is True
    assert r.vermillion_present is False
    assert r.severity == "warn"
    assert "STOPPED" in r.message


def test_ubuntu_stopped_strict_fail():
    with _stub_wsl("Stopped"):
        with patch.object(mod, "_probe_ubuntu_bridge", return_value=False):
            r = mod.report_pillar(mod.UBUNTU, strict=True)
    assert r.vermillion_present is False
    assert r.severity == "fail"


def test_ubuntu_unregistered_fail():
    with _stub_wsl("Running", distro="docker-desktop-data"):
        # pretend only docker-desktop is registered; no Ubuntu at all
        with patch.object(mod, "_probe_ubuntu_bridge", return_value=False):
            r = mod.report_pillar(mod.UBUNTU, strict=False)
    assert r.reachable is False
    assert r.severity == "fail"


# ── main() exit-code matrix ──────────────────────────────────────────────


def _fresh_pillars(tmp_path, *, ubuntu_stopped: bool = False):
    """Build a list of (Pillar-instance, path-or-probe) tuples pointing at
    tmp_path fixtures. Used to construct a custom ALL_PILLARS patched tuple.
    """
    kali_root = tmp_path / "K" / "vermillion_lightfury"
    kali_root.mkdir(parents=True)
    (kali_root / "worms").mkdir()
    w11_root = tmp_path / "W" / "vermillion_lightfury"
    w11_root.mkdir(parents=True)
    (w11_root / "worms").mkdir()
    g_root = tmp_path / "G" / "vermillion_lightfury"
    g_root.mkdir(parents=True)

    return (
        mod.Pillar("KALI", kali_root, "fs"),
        # UBUNTU path is irrelevant under wsl_registry probe; keep /mnt/wsl/Ubuntu
        # as placeholder so report_pillar can read it without OSError.
        mod.Pillar("UBUNTU", mod.Path("/mnt/wsl/Ubuntu"), "wsl_registry"),
        mod.Pillar("W11", w11_root, "fs"),
        mod.Pillar("G", tmp_path / "G", "drvfs_writable"),
    )


def _install_happy_pillars(tmp_path):
    """Replace `mod.ALL_PILLARS` with a tuple of tmp-path fixtures and stub
    the WSL probe functions so all 4 pillars report reachable + vermillion
    present (exit code 0).
    """
    new_pillars = _fresh_pillars(tmp_path)
    return [
        patch.object(mod, "ALL_PILLARS", new_pillars),
        patch.object(mod, "_probe_wsl", return_value=["ubuntu-22.04", "kali"]),
        patch.object(mod, "_probe_ubuntu_bridge", return_value=True),
    ]


def _install_stopped_ubuntu_pillars(tmp_path):
    """Same fixture layout as _install_happy_pillars but with UBUNTU
    bridge=False (stopped) — exit 2 under lenient / 1 under --strict.

    Built explicitly (not by swap-of-list-tail) so this helper is
    order-independent from any future reordering of _install_happy_pillars.
    """
    return [
        patch.object(mod, "ALL_PILLARS", _fresh_pillars(tmp_path)),
        patch.object(mod, "_probe_wsl", return_value=["ubuntu-22.04", "kali"]),
        patch.object(mod, "_probe_ubuntu_bridge", return_value=False),
    ]


def _start_then_run(patches, args):
    """Start all patches, call main(args), stop in finally. Returns rc."""
    for p in patches:
        p.start()
    try:
        return mod.main(args)
    finally:
        for p in reversed(patches):
            p.stop()


def test_main_exits_0_when_all_pass(tmp_path, capsys):
    rc = _start_then_run(_install_happy_pillars(tmp_path), ["--quiet"])
    captured = capsys.readouterr()
    assert rc == 0, f"unexpected stderr: {captured.err!r}"


def test_main_exits_2_when_ubuntu_stopped_lenient(tmp_path, capsys):
    """Lenient: stopped Ubuntu → WARN → exit 2."""
    rc = _start_then_run(_install_stopped_ubuntu_pillars(tmp_path), ["--quiet"])
    assert rc == 2


def test_main_exits_1_when_ubuntu_stopped_strict(tmp_path, capsys):
    rc = _start_then_run(
        _install_stopped_ubuntu_pillars(tmp_path), ["--quiet", "--strict"],
    )
    assert rc == 1


def test_main_exits_1_when_kali_missing(tmp_path, capsys):
    """KALI missing → fail → exit 1 regardless of strict."""
    fake_w11 = tmp_path / "W" / "vermillion_lightfury"
    fake_w11.mkdir(parents=True)
    (fake_w11 / "worms").mkdir()
    fake_g = tmp_path / "G" / "vermillion_lightfury"
    fake_g.mkdir(parents=True)
    # Patch ALL_PILLARS with KALI pointing at a non-existent path.
    missing_kali_pillars = (
        mod.Pillar("KALI", tmp_path / "absent_kali", "fs"),
        mod.Pillar("UBUNTU", mod.Path("/mnt/wsl/Ubuntu"), "wsl_registry"),
        mod.Pillar("W11", fake_w11, "fs"),
        mod.Pillar("G", tmp_path / "G", "drvfs_writable"),
    )
    patches = [
        patch.object(mod, "ALL_PILLARS", missing_kali_pillars),
        patch.object(mod, "_probe_wsl", return_value=["ubuntu-22.04", "kali"]),
        patch.object(mod, "_probe_ubuntu_bridge", return_value=True),
    ]
    rc = _start_then_run(patches, ["--quiet"])
    assert rc == 1


# ── JSON output ───────────────────────────────────────────────────────────


def test_main_json_emits_keys(tmp_path, capsys):
    rc = _start_then_run(_install_happy_pillars(tmp_path), ["--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "pillars" in parsed
    pillar_names = {p["name"] for p in parsed["pillars"]}
    assert pillar_names == {"KALI", "UBUNTU", "W11", "G"}
    assert parsed["exit_code_hint"] == 0


def test_main_json_emits_correct_hint_when_stopped(tmp_path, capsys):
    rc = _start_then_run(_install_stopped_ubuntu_pillars(tmp_path), ["--json"])
    assert rc == 2
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["exit_code_hint"] == 2
    ubu = next(p for p in parsed["pillars"] if p["name"] == "UBUNTU")
    assert ubu["severity"] == "warn"


# ── Table render ──────────────────────────────────────────────────────────


def test_render_table_has_pillar_rows():
    fake_reports = [
        mod.PillarReport("KALI", True, True, "pass", "ok"),
        mod.PillarReport("UBUNTU", True, False, "warn", "STOPPED"),
        mod.PillarReport("W11", True, True, "pass", "ok"),
        mod.PillarReport("G", False, False, "fail", "missing"),
    ]
    rendered = mod.render_table(fake_reports)
    for name in ("KALI", "UBUNTU", "W11", "G"):
        assert name in rendered
    # PASS/WARN/FAIL are emitted (possibly ANSI-colored)
    assert "PASS" in rendered
    assert "WARN" in rendered
    assert "FAIL" in rendered
