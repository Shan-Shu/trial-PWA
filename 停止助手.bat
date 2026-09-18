@echo off
chcp 65001 >nul 2>&1
rem ============================================================================
rem  研究助手 · 停止
rem
rem  双击本文件即可停掉后台运行的服务。
rem  启动脚本用了"隐藏窗口"，服务没有可见的控制台，所以只能从这里停。
rem
rem  本文件是 UTF-8 编码，第 2 行的 chcp 65001 是配套的（原因见「启动助手.bat」）。
rem ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
title 研究助手 · 停止中

rem  与「启动助手.bat」保持一致：从 PORT 起依次往后找，找到哪个就停哪个。
set "PORT=8000"
set "HOST=127.0.0.1"
set "SCAN=10"
rem --------------------------------------------------------------------------

set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "PROBE=%ROOT%scripts\probe_ready.py"

echo.
echo   研究助手 · 正在停止…
echo.

set /a FOUND=0
set /a INDEX=0
:scan
if !INDEX! GEQ %SCAN% goto done
set /a CUR=PORT+INDEX
call :is_our_assistant !CUR!
if not errorlevel 1 (
  set /a FOUND+=1
  echo   端口 !CUR! 上正在运行，正在停止…
  call :kill_port !CUR!
)
set /a INDEX+=1
goto scan

:done
if !FOUND! EQU 0 (
  echo   没有检测到正在运行的服务。
  echo.
  pause
  exit /b 0
)
echo.
echo   [√] 已停止 !FOUND! 个服务。
>nul ping -n 3 127.0.0.1
endlocal
exit /b 0

rem ------------------------------------------------------------------ 子过程
:is_our_assistant
if not exist "%PY%" exit /b 1
if not exist "%PROBE%" exit /b 1
"%PY%" "%PROBE%" %HOST% %~1 >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

rem  找出监听该端口的进程并结束它（连同其子进程）
:kill_port
set "TARGET=%~1"
set "TPID="
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /i "LISTENING" ^| findstr /c:":!TARGET! "') do (
  if not defined TPID set "TPID=%%P"
)
if not defined TPID (
  echo   [!] 端口 !TARGET! 上没有找到监听进程，可能刚好已退出。
  exit /b 0
)
taskkill /PID !TPID! /T /F >nul 2>&1
if errorlevel 1 (
  echo   [!] 结束进程 !TPID! 失败（可能需要管理员权限）。
) else (
  echo       已结束进程 !TPID!。
)
exit /b 0
