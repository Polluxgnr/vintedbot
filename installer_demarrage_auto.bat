@echo off
rem Double-clique pour que Vinted Hunter demarre tout seul en arriere-plan a chaque ouverture de session.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_autostart.ps1"
pause
