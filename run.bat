@echo off
chcp 65001 >nul
echo ========================================
echo        fliphtml5 Downloader
echo ========================================
"%~dp0.venv\Scripts\python.exe" "%~dp0fliphtml5Downloader.py"
pause
