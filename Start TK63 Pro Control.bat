@echo off
rem Starts TK-63 Pro Control without a console window.
rem Close the official Tezarre Driver first - both can't talk to the keyboard at once.
cd /d "%~dp0"
start "" pythonw app.py
