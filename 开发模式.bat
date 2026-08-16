@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo    NovelFlow 开发模式（修改代码自动重启）
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有检测到 Python，请先安装 Python 3.9 或更高版本。
    echo 下载地址：https://www.python.org/downloads/
    echo 安装时一定要勾选 Add Python to PATH 再点 Install。
    echo.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo 正在创建虚拟环境...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"

echo 正在安装依赖...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败，请把上面的红色报错截图。
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
)

set "PYTHONPATH=%CD%"

echo.
echo 启动开发模式！请用浏览器打开： http://127.0.0.1:8021
echo 修改 src\ 里的代码并保存后，服务会自动重启。
echo 终端里会实时打印日志和报错，方便调试。
echo 注意：本窗口不要关闭。
echo.
python -m uvicorn src.main:app --host 127.0.0.1 --port 8021 --reload

pause
