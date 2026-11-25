@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "E:\ANACONDA\envs\mdva311\python.exe" (
  echo 没有找到已约定的 Conda mdva311 环境。请检查 Anaconda 安装位置。
  pause
  exit /b 1
)
call "E:\ANACONDA\Scripts\activate.bat" "E:\ANACONDA\envs\mdva311"
:menu
echo.
echo MDVA 模型实验管理
echo 1. 查看真实进度
echo 2. 继续识别实验队列（按冻结配置顺序运行）
echo 3. 请求安全暂停（当前 epoch 结束保存）
echo 4. 继续参考目录预测研究
echo 5. 生成已有结果报告
echo 0. 退出
set "mdva_training_choice="
set /p "mdva_training_choice=请选择："
if "%mdva_training_choice%"=="0" exit /b 0
if "%mdva_training_choice%"=="1" python -m mdva.training status
if "%mdva_training_choice%"=="2" (
  python -m mdva.training unpause
  python -m mdva.training continue
)
if "%mdva_training_choice%"=="3" python -m mdva.training pause
if "%mdva_training_choice%"=="4" python -m mdva.training.forecast_revision
if "%mdva_training_choice%"=="5" python -m mdva.training report
goto menu
