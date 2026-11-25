@echo off
call "E:\ANACONDA\condabin\conda.bat" activate mdva311
if errorlevel 1 exit /b 1
cd /d "%~dp0"
echo MDVA model environment: %CONDA_PREFIX%
echo Check GPU: python -m mdva.training.environment_check
echo No training starts automatically.
cmd /k
