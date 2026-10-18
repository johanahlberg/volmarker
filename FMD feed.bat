@echo off
REM Start the Fenics Market Data feed (run on ONE machine only). Adjust DATA below.
REM The password comes from FMD_PASSWORD or fmd.password_file in config.json - never put it in this file.
set DATA=S:\FXO\VolMarker\data
cd /d "%~dp0"
python -m volmarker.fmd feed --data "%DATA%"
pause
