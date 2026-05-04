@echo off
echo Starting Chrome with remote debugging...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins="*" ^
  --user-data-dir="%USERPROFILE%\fb_chrome_profile" ^
  "https://www.facebook.com"
echo Chrome launched. Log in to Facebook, then run windows_run_poster.bat
pause
