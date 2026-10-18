@echo off
REM Start VolMarker against the desk's shared folder. Adjust DATA below.
set DATA=S:\FXO\VolMarker\data
cd /d "%~dp0"
python -m volmarker --data "%DATA%"
pause
