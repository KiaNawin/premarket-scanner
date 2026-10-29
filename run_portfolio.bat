@echo off
REM Daily portfolio analysis -> output\YYYY-MM-DD\ and alerts to Telegram.
REM Scheduled by Windows Task Scheduler (see README). Runs from this file's own folder.
cd /d "%~dp0"
if not exist output mkdir output
echo ===== %date% %time% >> output\portfolio_run.log
python -m portfolio_stage run --telegram >> output\portfolio_run.log 2>&1
