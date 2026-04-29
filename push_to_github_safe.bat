@echo off
chcp 65001 >nul
cd /d "%~dp0"

set GIT="C:\Program Files\Git\bin\git.exe"
set REPO="%~dp0"
set REMOTE=https://github.com/Haker7992/crashbot.git

echo ========================================
echo   Kanero Bot - Safe Push to GitHub
echo ========================================
echo.

echo [1/4] Проверяю git репозиторий...

%GIT% rev-parse --git-dir >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo     Инициализирую git...
    %GIT% init
    %GIT% branch -M main
)

%GIT% remote get-url origin >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo     Добавляю remote origin...
    %GIT% remote add origin %REMOTE%
) else (
    echo     Обновляю remote origin...
    %GIT% remote set-url origin %REMOTE%
)

echo [1/4] Готово!
echo.
echo [2/4] Проверяю статус...

%GIT% status --short

echo.
echo [3/4] Коммит...

%GIT% add .

set dt=%date:~6,4%-%date:~3,2%-%date:~0,2% %time:~0,2%:%time:~3,2%
%GIT% commit -m "Update %dt%"

if %ERRORLEVEL% neq 0 (
    echo     Нет изменений для коммита.
)

echo [3/4] Готово!
echo.
echo [4/4] Отправляю на GitHub (БЕЗОПАСНО - без --force)...
echo.

%GIT% push -u origin main

if %ERRORLEVEL% == 0 (
    echo.
    echo ========================================
    echo   ✅ Успешно загружено на GitHub!
    echo ========================================
    echo.
    echo   Репозиторий: https://github.com/Haker7992/crashbot
    echo   Ветка: main
    echo.
) else (
    echo.
    echo ========================================
    echo   ❌ Ошибка при загрузке!
    echo ========================================
    echo.
    echo   Возможные причины:
    echo   1. Нет доступа в интернет
    echo   2. Не авторизован в GitHub
    echo   3. Конфликт с удалённой веткой
    echo.
    echo   Решение:
    echo   - Проверь интернет
    echo   - Авторизуйся: git config --global user.name "Имя"
    echo   - Выполни вручную: git push -u origin main
    echo.
)

pause
