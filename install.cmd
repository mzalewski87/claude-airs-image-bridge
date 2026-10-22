@echo off
rem Installs/updates the PortkeyImageProxy service (asks for UAC) and the tray icon autostart.
rem   install.cmd            install or update
rem   install.cmd uninstall  remove service and tray autostart
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %1
pause
