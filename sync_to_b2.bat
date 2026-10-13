@echo off
:loop
"C:\rclone\rclone.exe" copy "C:\gimpTest\Output" b2varsany:varsaniautomation --log-file="C:\gimpTest\rclone_sync.log" --log-level INFO --timeout 30s --contimeout 10s
timeout /t 5 /nobreak
goto loop
