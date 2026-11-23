# coding: utf-8
"""
Vermillion LightFury — Azazael × Kali Orchestration Engine
===========================================================
A bridge between the Azazael AI worm ecosystem and Kali Linux
penetration testing tools, running across Windows, WSL Kali,
and WSL Ubuntu for GPU-accelerated security automation.

Project:   Vermillion LightFury
Version:    1.0.0
Platform:   Kali Linux (WSL2) + Windows 11
Python:     3.14t free-threaded
Azazael:     Worm ecosystem at /root/azazael (Kali WSL)

Modules:
    worms/nmap_scanner.py   — Wraps 'wsl -d kali nmap', wires to VulnScanner + PAMS
    worms/kali_health.py    — Monitors E: free space, RAM, VHDX growth, breach alerts
    config/shared_dir.py    — /mnt/c/azazael_shared/ setup for cross-platform data exchange
    build/build.bat         — Nuitka standalone .exe build script
"""

__version__ = "1.0.0"
__project__ = "Vermillion LightFury"
__author__ = "Azazael Ecosystem"
