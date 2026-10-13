@echo off
rem Restart the VarsanyDaemon Windows service (NSSM). Double-click this file and accept the UAC prompt.
fltmc >nul 2>&1
if errorlevel 1 (
    if "%~1"=="elevated" (
        echo Could not get administrator rights.
        pause
        exit /b 1
    )
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs" || (echo Elevation was cancelled.& pause)
    exit /b
)
echo Restarting VarsanyDaemon...
net stop VarsanyDaemon
net start VarsanyDaemon
echo.
sc query VarsanyDaemon | findstr /i "STATE"
echo.
pause
