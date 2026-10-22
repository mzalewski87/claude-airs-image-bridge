@echo off
rem Starts the Portkey image proxy on http://127.0.0.1:8787 (Ctrl+C to stop).
python -I "%~dp0proxy.py" %*
