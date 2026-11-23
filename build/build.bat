@echo off
REM ============================================================
REM  Vermillion LightFury — Nuitka Standalone .exe Build Script
REM  ============================================================
REM  Builds a standalone .exe for each Vermillion LightFury worm
REM  using Nuitka with Python 3.14t (free-threaded).
REM
REM  Prerequisites:
REM    1. Python 3.14t must be the active Python
REM       Verify: python --version  →  Python 3.14.x
REM       Verify: python -c "import sys; print(hasattr(sys, '_is_gil_enabled') and not sys._is_gil_enabled())"
REM                → True
REM    2. Nuitka 3.14t patches must be applied:
REM       python azazael/docs/scripts/patch_nuitka_314t.py --apply
REM    3. Azazael must be importable:
REM       pip install -e azazael/   (if setup.py exists)
REM       OR add azazael/src/ to PYTHONPATH
REM
REM  Usage:
REM    build.bat              → Build all 5 Vermillion LightFury .exe modules
REM                                  (NmapScanner, KaliHealth, SharedDirManager,
REM                                   MetasploitBridge, WSLRecovery, BenchmarkWorm)
REM    build.bat nmap         → Build only NmapScanner
REM    build.bat health       → Build only KaliHealth
REM    build.bat shared       → Build only SharedDirManager
REM    build.bat metasploit   → Build only MetasploitBridge
REM    build.bat wsl          → Build only WSLRecovery
REM    build.bat benchmark    → Build only BenchmarkWorm
REM    build.bat clean        → Clean build artifacts
REM  ============================================================

setlocal enabledelayedexpansion
set PROJECT_DIR=%~dp0
set ROOT_DIR=%PROJECT_DIR%..
set BUILD_DIR=%PROJECT_DIR%build_output
set AZAZAEL_SRC=%ROOT_DIR%\..\azazael\src

echo ============================================================
echo   Vermillion LightFury — Nuitka Standalone .exe Build
echo   Version: 1.0.0
echo ============================================================
echo.

:: Verify Python 3.14t
echo [1/5] Checking Python 3.14t...
python --version 2>nul | findstr "3.14" >nul
if %ERRORLEVEL% neq 0 (
    echo   ERROR: Python 3.14t is required!
    echo   Current: 
    python --version 2>nul
    exit /b 1
)
echo   OK: Python 3.14t detected

:: Check Nuitka
echo [2/5] Checking Nuitka...
python -m nuitka --version >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo   ERROR: Nuitka not found! Install with: pip install nuitka
    exit /b 1
)
echo   OK: Nuitka available

:: Check Azazael imports
echo [3/6] Checking Azazael imports...
set PYTHONPATH=%AZAZAEL_SRC%;%PYTHONPATH%
python -c "from CORE.worm_registry import register_worm; print('OK')" 2>nul
if %ERRORLEVEL% neq 0 (
    echo   WARNING: Azazael imports may fail. Setting PYTHONPATH=%AZAZAEL_SRC%
)
echo   OK

:: Pre-build guards: goto-chain smoke + F11 anti-pattern regression guard.
:: Fail fast before Nuitka is invoked if a future commit lands something
:: that forgets to terminate a label or accidentally re-introduces the
:: mixed-slash path construction (Finding #11).
echo [4/6] Checking goto chain + F11 anti-pattern...
python "%PROJECT_DIR%check_goto_chain.py" --script "%PROJECT_DIR%build.bat" --quiet >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   ERROR: goto chain broken ^(exit %ERRORLEVEL%^). Fix goto references in build.bat and retry.
    exit /b %ERRORLEVEL%
)
echo   goto-chain: OK
python "%PROJECT_DIR%check_no_mixed_slash.py" --quiet >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   ERROR: F11 anti-pattern matched in worms/ or config/ ^(exit %ERRORLEVEL%^). Replace f-string mixed-slash with os.path.join ^(see AUDIT.md §11^).
    exit /b %ERRORLEVEL%
)
echo   F11 guard:  OK

:: Create build output directory
if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"

:: ── Build functions ──────────────────────────────────────────
set MODE=%~1
if "%MODE%"=="" set MODE=all

echo [5/6] Building Vermillion LightFury modules...

:: Nuitka common flags
set NUITKA_FLAGS=--standalone --onefile --assume-yes-for-downloads --remove-output
set NUITKA_EXCLUDES=--nofollow-import-to=torch --nofollow-import-to=optree --nofollow-import-to=openvino --nofollow-import-to=tensorflow

if "%MODE%"=="clean" goto :clean
if "%MODE%"=="test_all" goto :test_all

:: ── NmapScanner ──────────────────────────────────────────────
if "%MODE%"=="all" goto :build_nmap
if "%MODE%"=="nmap" goto :build_nmap
goto :check_health

:build_nmap
echo.
echo   Building NmapScanner (Vermillion LightFury Nmap Scanner)...
python -m nuitka %NUITKA_FLAGS% %NUITKA_EXCLUDES% ^
    --output-filename=VermillionLightFury-NmapScanner_314t.exe ^
    --output-dir="%BUILD_DIR%" ^
    "%ROOT_DIR%\worms\nmap_scanner.py"
if %ERRORLEVEL% neq 0 (
    echo   ERROR: NmapScanner build failed!
    if "%MODE%"=="nmap" exit /b 1
) else (
    echo   SUCCESS: %BUILD_DIR%\VermillionLightFury-NmapScanner_314t.exe
)
if "%MODE%"=="nmap" goto :test_all

:: ── KaliHealth ───────────────────────────────────────────────
:check_health
if "%MODE%"=="all" goto :build_health
if "%MODE%"=="health" goto :build_health
goto :check_shared

:build_health
echo.
echo   Building KaliHealth (Vermillion LightFury Health Monitor)...
python -m nuitka %NUITKA_FLAGS% %NUITKA_EXCLUDES% ^
    --output-filename=VermillionLightFury-KaliHealth_314t.exe ^
    --output-dir="%BUILD_DIR%" ^
    "%ROOT_DIR%\worms\kali_health.py"
if %ERRORLEVEL% neq 0 (
    echo   ERROR: KaliHealth build failed!
    if "%MODE%"=="health" exit /b 1
) else (
    echo   SUCCESS: %BUILD_DIR%\VermillionLightFury-KaliHealth_314t.exe
)
if "%MODE%"=="health" goto :test_all

:: ── SharedDirManager ─────────────────────────────────────────
:check_shared
if "%MODE%"=="all" goto :build_shared
if "%MODE%"=="shared" goto :build_shared
goto :done

:build_shared
echo.
echo   Building SharedDirManager (Vermillion LightFury Shared Directory Manager)...
python -m nuitka %NUITKA_FLAGS% %NUITKA_EXCLUDES% ^
    --output-filename=VermillionLightFury-SharedDir_314t.exe ^
    --output-dir="%BUILD_DIR%" ^
    "%ROOT_DIR%\config\shared_dir.py"
if %ERRORLEVEL% neq 0 (
    echo   ERROR: SharedDirManager build failed!
    if "%MODE%"=="shared" exit /b 1
) else (
    echo   SUCCESS: %BUILD_DIR%\VermillionLightFury-SharedDir_314t.exe
)
if "%MODE%"=="shared" goto :test_all

:: ── MetasploitBridge ──
:check_metasploit
if "%MODE%"=="all" goto :build_metasploit
if "%MODE%"=="metasploit" goto :build_metasploit
goto :check_wsl

:build_metasploit
echo.
echo   Building MetasploitBridge (Vermillion LightFury Metasploit Bridge)...
python -m nuitka %NUITKA_FLAGS% %NUITKA_EXCLUDES% ^
    --output-filename=VermillionLightFury-MetasploitBridge_314t.exe ^
    --output-dir="%BUILD_DIR%" ^
    "%ROOT_DIR%\worms\metasploit_bridge.py"
if %ERRORLEVEL% neq 0 (
    echo   ERROR: MetasploitBridge build failed!
    if "%MODE%"=="metasploit" exit /b 1
) else (
    echo   SUCCESS: %BUILD_DIR%\VermillionLightFury-MetasploitBridge_314t.exe
)
if "%MODE%"=="metasploit" goto :test_all

:: ── WSLRecovery ──
:check_wsl
if "%MODE%"=="all" goto :build_wsl
if "%MODE%"=="wsl" goto :build_wsl
goto :check_benchmark

:build_wsl
echo.
echo   Building WSLRecovery (Vermillion LightFury WSL Health & Recovery)...
python -m nuitka %NUITKA_FLAGS% %NUITKA_EXCLUDES% ^
    --output-filename=VermillionLightFury-WSLRecovery_314t.exe ^
    --output-dir="%BUILD_DIR%" ^
    "%ROOT_DIR%\worms\wsl_recovery.py"
if %ERRORLEVEL% neq 0 (
    echo   ERROR: WSLRecovery build failed!
    if "%MODE%"=="wsl" exit /b 1
) else (
    echo   SUCCESS: %BUILD_DIR%\VermillionLightFury-WSLRecovery_314t.exe
)
if "%MODE%"=="wsl" goto :test_all

:: ── BenchmarkWorm ──
:check_benchmark
if "%MODE%"=="all" goto :build_benchmark
if "%MODE%"=="benchmark" goto :build_benchmark
goto :done

:build_benchmark
echo.
echo   Building BenchmarkWorm (Vermillion LightFury Weekly Benchmark)...
python -m nuitka %NUITKA_FLAGS% %NUITKA_EXCLUDES% ^
    --output-filename=VermillionLightFury-BenchmarkWorm_314t.exe ^
    --output-dir="%BUILD_DIR%" ^
    "%ROOT_DIR%\worms\benchmark_worm.py"
if %ERRORLEVEL% neq 0 (
    echo   ERROR: BenchmarkWorm build failed!
    if "%MODE%"=="benchmark" exit /b 1
) else (
    echo   SUCCESS: %BUILD_DIR%\VermillionLightFury-BenchmarkWorm_314t.exe
)
if "%MODE%"=="benchmark" goto :test_all

:: ── Done ────────────────────────────────────────────────────
goto :test_all

:test_all
echo.
echo [Testing] Running executable boot verification...
if not exist "%BUILD_DIR%\*.exe" (
    echo   No .exe files found in %BUILD_DIR% to test.
    goto :done
)

for %%F in ("%BUILD_DIR%\*.exe") do (
    set "TESTARG="
    echo "%%~nF" | findstr /i /c:"KaliHealth" >nul && set "TESTARG=quick"
    echo "%%~nF" | findstr /i /c:"SharedDir"  >nul && set "TESTARG=setup"
    echo "%%~nF" | findstr /i /c:"Metasploit" >nul && set "TESTARG=exploits"
    echo "%%~nF" | findstr /i /c:"WSLRecovery" >nul && set "TESTARG=check"

    echo   Testing: %%~nxF !TESTARG!
    "%%F" !TESTARG! >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        echo   FAIL: %%~nxF exited with !ERRORLEVEL!
        exit /b !ERRORLEVEL!
    ) else (
        echo   PASS: %%~nxF
    )
)
goto :done

:clean
echo.
echo   Cleaning build artifacts...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
echo   Cleaned.
goto :done

:done
echo.
echo [6/6] Build complete!
echo.
echo   Output: %BUILD_DIR%\
echo   Files:
dir /b "%BUILD_DIR%\*.exe" 2>nul
echo.
echo   Run: .\VermillionLightFury-NmapScanner_314t.exe
echo        .\VermillionLightFury-KaliHealth_314t.exe check
echo        .\VermillionLightFury-SharedDir_314t.exe setup
echo        .\VermillionLightFury-MetasploitBridge_314t.exe exploits
echo        .\VermillionLightFury-WSLRecovery_314t.exe check
echo        .\VermillionLightFury-BenchmarkWorm_314t.exe
echo.
exit /b 0
