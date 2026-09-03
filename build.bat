@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo   屏幕文字识别监控 - 一键打包脚本
echo   首次运行需要联网安装依赖，请耐心等待
echo ================================================
echo.

echo [1/3] 安装运行依赖 ...
python -m pip install -r requirements.txt || goto :err
echo.

echo [2/3] 安装打包工具 ...
python -m pip install -r requirements-build.txt || goto :err
echo.

echo [3/3] 开始打包（需要几分钟）...
REM 判断安装了哪个 rapidocr 版本，收集其自带的模型文件
python -c "import rapidocr" 2>nul
if %errorlevel%==0 (
    set COLLECT=--collect-all rapidocr
) else (
    set COLLECT=--collect-all rapidocr_onnxruntime
)

pyinstaller --noconfirm --clean --windowed ^
    --name "屏幕文字识别监控" ^
    %COLLECT% ^
    --hidden-import mss ^
    --hidden-import serial ^
    --hidden-import requests ^
    main.py || goto :err

echo.
echo [自检] 运行打包后的程序自检（验证离线 OCR 可用，约需十几秒）...
start /wait "" "dist\屏幕文字识别监控\屏幕文字识别监控.exe" --selftest
if %errorlevel%==0 (
    echo [自检] 通过！离线 OCR 引擎正常。
) else (
    echo [自检] 失败！请检查上方打包输出或程序目录下的 程序错误日志.txt。
)

echo.
echo ================================================
echo   打包完成！
echo   程序在 dist\屏幕文字识别监控\ 文件夹中。
echo   把这个文件夹整体复制到任何 Windows 电脑，
echo   双击里面的 屏幕文字识别监控.exe 即可使用，
echo   不需要安装 Python，也不需要联网。
echo ================================================
pause
exit /b 0

:err
echo.
echo 打包失败！请检查网络连接后重试，或查看上方错误信息。
pause
exit /b 1
