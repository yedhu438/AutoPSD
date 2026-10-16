@echo off
rem The bucket is per PC: b2_bucket.txt (git-ignored) holds its name. Without that file the production bucket is used.
set "B2BUCKET=varsaniautomation"
if exist "C:\gimpTest\b2_bucket.txt" set /p B2BUCKET=<"C:\gimpTest\b2_bucket.txt"
:loop
"C:\rclone\rclone.exe" copy "C:\gimpTest\Output" b2varsany:%B2BUCKET% --log-file="C:\gimpTest\rclone_sync.log" --log-level INFO --timeout 30s --contimeout 10s
timeout /t 5 /nobreak
goto loop
