@echo off
rem Compatibility shortcut: environment checks live in the Python launcher.
setlocal
cd /d "%~dp0"
set "MDVA_BOOT="
if defined MDVA_PY if exist "%MDVA_PY%" set "MDVA_BOOT=%MDVA_PY%"
if defined MDVA_BOOT goto launch
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "MDVA_BOOT=%%P"
if defined MDVA_BOOT goto launch
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "MDVA_BOOT=%%P"
if defined MDVA_BOOT goto launch
for %%D in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\anaconda3") do if exist "%%~D\python.exe" set "MDVA_BOOT=%%~D\python.exe"
if defined MDVA_BOOT goto launch
echo Python 3.10+ was not found. Install Python with Tcl/Tk support, then open this launcher again.
pause
exit /b 1
:launch
for %%P in ("%MDVA_BOOT%") do if exist "%%~dpPpythonw.exe" set "MDVA_BOOT=%%~dpPpythonw.exe"
start "" "%MDVA_BOOT%" "%~dp0main.py"
endlocal
