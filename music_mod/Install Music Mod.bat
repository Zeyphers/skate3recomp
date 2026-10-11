@echo off
rem Converts the songs in "My Music" (next to your game folder) and puts them in Skate 3's playlist.
cd /d "%~dp0"
call :run install %*
pause
exit /b

:run
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 skate3_music.py %*
  exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
  python skate3_music.py %*
  exit /b
)
echo Python 3 is not installed. Install it with:
echo     winget install -e --id Python.Python.3.12
echo then double-click this file again.
exit /b
