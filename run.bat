@echo off
rem 一键启动：按顺序找一个装好依赖的 Python，然后开窗口。
rem 想指定解释器就设环境变量 MDVA_PY，例如
rem   set MDVA_PY=D:\some\path\python.exe
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not defined MDVA_PY goto try_conda
"%MDVA_PY%" -c "import numpy, PIL" >nul 2>nul
if not errorlevel 1 goto run_mdva

:try_conda
where conda >nul 2>nul
if errorlevel 1 goto try_path
conda run -n mdva python -c "import numpy, PIL" >nul 2>nul
if not errorlevel 1 goto run_conda

:try_path
python -c "import numpy, PIL" >nul 2>nul
if not errorlevel 1 goto run_path
py -3 -c "import numpy, PIL" >nul 2>nul
if not errorlevel 1 goto run_py

echo.
echo  没找到已经装好依赖的 Python。
echo  先双击 install.bat 装依赖，装完再双击本文件。
echo.
pause
exit /b 1

:run_mdva
echo  使用 %MDVA_PY%
"%MDVA_PY%" main.py
goto done

:run_conda
echo  使用 conda 环境 mdva
conda run -n mdva --no-capture-output python main.py
goto done

:run_path
echo  使用 PATH 里的 python
python main.py
goto done

:run_py
echo  使用 py -3
py -3 main.py

:done
endlocal
