@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Генератор презентаций

rem ===================== НАСТРОЙКИ ИИ =====================
rem Впишите ключ и название модели между знаком = и закрывающей кавычкой.
rem Если не нужен ИИ, оставьте пустыми: сайт будет работать без ИИ.
set "LLM_API_KEY="
set "LLM_MODEL="
set "LLM_BASE_URL=https://openrouter.ai/api/v1"
rem ========================================================

set "PYTHONIOENCODING=utf-8"
set "OPEN_BROWSER=1"

rem --- 1. Ищем Python ---
set "PY="
python --version >nul 2>nul && set "PY=python"
if not defined PY (
  py --version >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo Python не найден.
  echo Установите его с сайта python.org и при установке поставьте галочку Add Python to PATH.
  echo После установки запустите этот файл снова.
  pause
  exit /b 1
)

rem --- 2. Проверяем библиотеки, при необходимости ставим ---
%PY% -c "import flask, requests, pptx" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаю нужные библиотеки. Это нужно один раз, подождите...
  %PY% -m pip install flask requests python-pptx
  if errorlevel 1 (
    echo Не удалось установить библиотеки. Проверьте интернет и запустите файл снова.
    echo Если не помогло, откройте командную строку и выполните: pip install flask requests python-pptx
    pause
    exit /b 1
  )
)

rem --- 3. Предупреждаем, если ИИ не настроен ---
set "AI_OK=1"
if "%LLM_API_KEY%"=="" set "AI_OK="
if "%LLM_MODEL%"=="" set "AI_OK="
if not defined AI_OK echo ИИ не настроен: впишите ключ и модель в начале файла start.bat. Пока работает режим без ИИ.

echo.
echo Запускаю сайт. Браузер откроется сам.
echo Чтобы остановить сайт, закройте это окно.
echo.
%PY% app.py
echo.
echo Сервер остановлен. Если это произошло из-за ошибки, текст ошибки виден выше.
pause
