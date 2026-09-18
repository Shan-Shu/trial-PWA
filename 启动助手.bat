@echo off
chcp 65001 >nul 2>&1
rem ============================================================================
rem  研究助手 · 一键启动
rem
rem  双击本文件即可：后台启动服务（完全无窗口）→ 等它就绪 → 自动打开浏览器。
rem
rem  · 服务在后台独立运行，关掉浏览器标签页不会停掉它；
rem  · 要停止请双击「停止助手.bat」；
rem  · 重复双击本文件不会起第二个进程，只会把浏览器再打开一次。
rem
rem  本文件是 UTF-8 编码，第 2 行的 chcp 65001 是配套的：它必须紧跟在
rem  @echo off 之后、且在此之前不出现任何中文。否则 cmd 会按系统默认代码页
rem  解析本文件，中文会变成乱码（实测：GBK 文件配 65001 控制台就是这样）。
rem ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
title 研究助手 · 启动中

rem ---------------------------------------------------------------- 可调参数
rem  想固定用哪个数据库，改下面这一行（相对本脚本所在目录）。
rem  也可以启动后在界面右上角的下拉框里临时切换。
set "DB=data\v031_fresh.db"
set "PORT=8000"
set "HOST=127.0.0.1"
set "WAIT_SECONDS=120"
rem --------------------------------------------------------------------------

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PY=%ROOT%.venv\Scripts\python.exe"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
set "SERVE=%ROOT%scripts\serve.py"
set "PROBE=%ROOT%scripts\probe_ready.py"
set "SERVELOG=%ROOT%data\logs\serve.log"

echo.
echo   研究助手 · 正在启动…
echo.

rem ------------------------------------------------------------ 1. 环境自检
if not exist "%PYW%" goto no_venv
if not exist "%PY%" goto no_venv
if not exist "%SERVE%" (
  echo   [×] 找不到启动入口：%SERVE%
  echo       请确认本脚本与 scripts\serve.py 在同一个项目目录下。
  echo.
  pause
  exit /b 1
)
if not exist "%PROBE%" (
  echo   [×] 找不到就绪探测脚本：%PROBE%
  echo.
  pause
  exit /b 1
)
if not exist "%ROOT%data" mkdir "%ROOT%data" >nul 2>&1
if not exist "%ROOT%data\logs" mkdir "%ROOT%data\logs" >nul 2>&1

rem ------------------------------------------- 2. 挑一个没被占用的端口
rem  8000 被别的程序占用时自动往后找，避免"点了没反应"。
set /a TRIES=0
:find_port
call :is_our_assistant %PORT%
if not errorlevel 1 goto already_running
call :is_port_busy %PORT%
if not errorlevel 1 (
  set /a TRIES+=1
  if !TRIES! GEQ 10 (
    echo   [×] 从 %PORT% 起连续 10 个端口都被别的程序占用，无法启动。
    echo       请关掉占用端口的程序，或修改本脚本顶部的 PORT。
    echo.
    pause
    exit /b 1
  )
  echo   端口 %PORT% 被别的程序占用，改用下一个…
  set /a PORT+=1
  goto find_port
)

rem --------------------------------------- 3. 隐藏启动（无任何窗口）
rem  做法：写一个 2 行 VBS 到临时目录，用 cscript 以"窗口样式 0"拉起
rem  pythonw.exe。pythonw 本身没有控制台，再叠加隐藏样式，
rem  后台服务完全不可见，也不会闪一下黑框。
set "VBS=%TEMP%\ra-launch-%RANDOM%%RANDOM%.vbs"
> "%VBS%" echo Set sh = CreateObject("WScript.Shell")
>>"%VBS%" echo sh.Run """%PYW%"" ""%SERVE%"" --host %HOST% --port %PORT% --db ""%DB%""", 0, False
cscript //nologo "%VBS%" >nul 2>&1
del "%VBS%" >nul 2>&1

echo   服务已在后台启动（端口 %PORT%），正在等它就绪…

rem ------------------------------------------------------ 4. 等它真的能响应
set /a WAITED=0
:wait_ready
call :is_our_assistant %PORT%
if not errorlevel 1 goto ready
set /a WAITED+=1
if !WAITED! GEQ %WAIT_SECONDS% (
  echo.
  echo   [×] 等了 %WAIT_SECONDS% 秒仍未就绪。
  echo       启动日志里有具体原因：
  echo       %SERVELOG%
  echo.
  echo   常见原因：依赖没装（在项目目录执行 uv sync）、
  echo             数据库文件不存在、API Key 没配（.env）。
  echo.
  pause
  exit /b 1
)
rem  用 ping 代替 timeout：不依赖输入重定向，也不会有额外窗口
>nul ping -n 2 127.0.0.1
set /a DOTS=WAITED %% 5
if !DOTS! EQU 0 echo   仍在启动…（!WAITED! 秒）
goto wait_ready

:ready
set "URL=http://%HOST%:%PORT%/#writing"
echo   [√] 已就绪，正在打开浏览器：%URL%
start "" "%URL%"
>nul ping -n 3 127.0.0.1
endlocal
exit /b 0

:already_running
rem  已经在跑就直接开浏览器——重复双击不该起第二个进程。
set "URL=http://%HOST%:%PORT%/#writing"
echo   [√] 服务已在运行（端口 %PORT%），直接打开浏览器。
start "" "%URL%"
>nul ping -n 2 127.0.0.1
endlocal
exit /b 0

:no_venv
echo   [×] 找不到 Python 运行环境：
echo       %PYW%
echo.
echo   解决办法：在本目录打开一次终端，执行
echo       uv sync
echo   装好依赖后再双击本脚本。
echo.
pause
exit /b 1

rem ------------------------------------------------------------------ 子过程
rem  :is_our_assistant ^<port^>  —— 返回 0 表示"本助手已在运行"
:is_our_assistant
"%PY%" "%PROBE%" %HOST% %~1 >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

rem  :is_port_busy ^<port^>  —— 返回 0 表示端口已被占用
:is_port_busy
netstat -ano -p tcp | findstr /i "LISTENING" | findstr /c:":%~1 " >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0
