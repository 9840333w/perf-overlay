@echo off
rem Installs Python libraries into the current interpreter only.
rem No service, no scheduled task, no autostart.
setlocal
cd /d "%~dp0"
set "PY="
if exist "D:\python\python.exe" set "PY=D:\python\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto NOPY
echo Using Python: %PY%
"%PY%" -V
echo.
echo Installing psutil / pythonnet / pywin32 / pillow ...
"%PY%" -m pip install --upgrade psutil pythonnet pywin32 pillow
echo.
"%PY%" -c "import psutil, clr, win32gui, tkinter; print('DEPS_OK')"
if errorlevel 1 goto BAD
echo.
echo DEPS_OK - now double-click the launcher.
echo.
pause
exit /b 0

:BAD
echo.
echo Dependency install failed. Copy the error above and check your pip proxy settings.
echo.
pause
exit /b 1

:NOPY
echo Python not found. Install Python 3.10+ from https://www.python.org first.
pause
exit /b 1
