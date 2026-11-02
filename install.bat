@echo off
cd /d "%~dp0"
where python >nul 2>nul || goto nopython
python -c "import sys" >nul 2>nul || goto nopython

echo Installing Slouch Detector. This can take a few minutes the first time...
python -m pip install --user --quiet --disable-pip-version-check mediapipe pystray pillow || goto failed
python posture.py --install || goto failed
start "" pythonw posture.py
echo.
echo Done! Slouch Detector is running. Look for the little level icon near the clock.
timeout /t 8 >nul
exit /b 0

:nopython
echo Python isn't installed yet.
echo 1. Get it from https://www.python.org/downloads/
echo 2. In the installer, tick "Add python.exe to PATH"
echo 3. Then double-click install.bat again.
pause
exit /b 1

:failed
echo Something went wrong while installing. Take a screenshot of this window and send it to whoever shared this with you.
pause
exit /b 1
