@echo off
rem Normal privilege. GPU temp/load, CPU load, memory, FPS all work.
rem CPU temperature needs the admin launcher (read CPU MSRs).
setlocal
cd /d "%~dp0"
set "PYW="
if exist "D:\python\pythonw.exe" set "PYW=D:\python\pythonw.exe"
if not defined PYW for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if defined PYW if not exist "%PYW%" set "PYW="
if defined PYW goto LAUNCH_PYW
where py >nul 2>&1
if not errorlevel 1 goto LAUNCH_PY
goto NOPY

:LAUNCH_PYW
start "" "%PYW%" "%~dp0perf_overlay.pyw"
goto END

:LAUNCH_PY
start "" py -3w "%~dp0perf_overlay.pyw"
goto END

:NOPY
echo.
echo  [ERROR] Python 3 not found.
echo          1. Install Python 3.10 or newer from https://www.python.org
echo             and tick "Add Python to PATH" during setup.
echo          2. Run the batch file named with 4 Chinese characters: AN-ZHUANG-YI-LAI
echo             (it installs psutil / pythonnet / pywin32 / pillow).
echo          3. Then double-click this file again.
echo          Details: see the usage doc in this folder.
echo.
pause
exit /b 1

:END
exit /b 0
