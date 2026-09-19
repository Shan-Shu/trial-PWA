@echo off
chcp 65001 >nul 2>&1
rem ============================================================================
rem  research-desk · 停止
rem
rem  双击本文件即可停掉后台运行的界面。
rem  启动脚本用了"隐藏窗口"，进程没有可见的控制台，所以只能从这里停。
rem
rem  本文件是 UTF-8 编码，第 2 行的 chcp 65001 是配套的
rem  （原因见「启动助手.bat」的注释）。
rem ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
title research-desk · 停止中

rem  与「启动助手.bat」保持一致：从 PORT 起依次往后找，找到哪个就停哪个。
set "PORT=8501"
set "HOST=127.0.0.1"
set "SCAN=10"
rem --------------------------------------------------------------------------

set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "PROBE=%ROOT%scripts\probe_ready.py"

echo.
echo   research-desk · 正在停止…
echo.

set /a FOUND=0
set /a INDEX=0
:scan
if !INDEX! GEQ %SCAN% goto done
set /a CUR=PORT+INDEX
call :is_our_app !CUR!
if not errorlevel 1 (
  set /a FOUND+=1
  echo   端口 !CUR! 上正在运行，正在停止…
  call :kill_port !CUR!
)
set /a INDEX+=1
goto scan

:done
echo.
if !FOUND! EQU 0 (
  echo   端口扫描未发现正在监听的界面。
) else (
  echo   [√] 已停止 !FOUND! 个监听进程。
)
echo.
rem --------------------------------------------------------------------------
rem  兜底清杀：端口扫描只会杀「正在监听」的那个进程，两种情况会漏：
rem    1) 实例卡死、健康探测超时 → 根本进不了 FOUND；
rem    2) 启动器是「父进程 serve_desk.py → 子进程(真正监听)」两层，
rem       只杀子进程可能留下父进程。
rem  漏掉的实例仍在后台跑检索与模型调用——实测漏过一个（仍占着 8501），它持续
rem  把「补检」结果写进错误的库、下载 PDF、白烧配额，而界面上完全看不出来。
rem  这里按**命令行特征**再兜一遍（与端口无关），卡死的实例同样杀得掉。
rem
rem  为什么不用 for /f 取它的计数：cmd 的 for /f 在命令以引号开头时会吃掉引号
rem  （实测 `for /f ... in (`"a" "b"`)` 直接报「文件名语法不正确」），
rem  而本脚本的路径必须带引号才稳。交给脚本自己报告，反而更简单可靠。
"%PY%" "%ROOT%scripts\kill_strays.py"
echo.
pause
endlocal
exit /b 0

rem ------------------------------------------------------------------ 子过程
:is_our_app
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
