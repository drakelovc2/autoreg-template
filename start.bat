@echo off
chcp 65001 >nul 2>&1
title autoreg-template
setlocal enabledelayedexpansion

REM ============================================================
REM  Windows-лаунчер шаблона автореггера.
REM  Создаёт venv, ставит зависимости, копирует конфиг и запускает панель.
REM  Флаги: --reinstall (пересоздать venv), --skip-install (не ставить deps)
REM ============================================================

cd /d "%~dp0"

set REINSTALL=0
set SKIP_INSTALL=0
for %%a in (%*) do (
    if /I "%%a"=="--reinstall" set REINSTALL=1
    if /I "%%a"=="--skip-install" set SKIP_INSTALL=1
)

echo ================================
echo   autoreg-template
echo ================================
echo.

REM ---------- 1. Python ----------
set PYTHON_BIN=
for %%P in (python python3 py) do (
    if not defined PYTHON_BIN (
        where %%P >nul 2>&1
        if not errorlevel 1 set PYTHON_BIN=%%P
    )
)
if not defined PYTHON_BIN (
    echo [ERROR] Python не найден. Установи Python 3.10+ и добавь его в PATH.
    pause
    exit /b 1
)
echo [*] Python: %PYTHON_BIN%

REM ---------- 2. venv ----------
set VENV_PY=venv\Scripts\python.exe
if "%REINSTALL%"=="1" (
    if exist venv rmdir /s /q venv
)
if not exist "%VENV_PY%" (
    echo [*] Создаю venv...
    %PYTHON_BIN% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Не удалось создать venv.
        pause
        exit /b 1
    )
)

REM ---------- 3. Зависимости ----------
if "%SKIP_INSTALL%"=="1" goto SKIP_DEPS
"%VENV_PY%" -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo [*] Ставлю зависимости...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    if exist requirements.txt "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Установка не удалась. Запусти: start.bat --reinstall
        pause
        exit /b 1
    )
)
:SKIP_DEPS

REM ---------- 4. Конфиг ----------
if not exist config.json (
    if exist config.example.json (
        copy /y config.example.json config.json >nul
        echo [!] Создан config.json — заполни base_url, mail_*, proxies.
    )
)

REM ---------- 5. Панель ----------
echo.
echo [*] Запускаю панель на http://127.0.0.1:9900
echo.
"%VENV_PY%" panel\server.py --port 9900

echo.
echo [*] Панель остановлена.
pause
