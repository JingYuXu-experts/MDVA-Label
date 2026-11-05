@echo off
rem 一键装依赖。有 conda 就建/用 mdva 环境，没有就直接装进当前 python。
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "DEPS=-r requirements.txt -r requirements/science.txt -r requirements/dev.txt"

where conda >nul 2>nul
if errorlevel 1 goto pip_current

conda env list | findstr /r /c:"^mdva " >nul
if not errorlevel 1 goto have_env

echo  正在创建 conda 环境 mdva（第一次会久一点）…
conda env create -f environment.yml
if not errorlevel 1 goto done
echo  环境创建没成功，改用当前 python 安装依赖。

:have_env
echo  往 conda 环境 mdva 里补依赖…
conda run -n mdva --no-capture-output python -m pip install %DEPS%
goto done

:pip_current
echo  往当前 python 里装依赖…
python -m pip install %DEPS%

:done
echo.
echo  完成。以后双击 run.bat，或者命令行输 python main.py 就能打开。
pause
endlocal
