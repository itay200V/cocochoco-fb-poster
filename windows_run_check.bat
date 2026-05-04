@echo off
echo Checking Facebook groups (hair/beauty filter)...
cd /d "%~dp0"
python fb_check_groups.py
pause
