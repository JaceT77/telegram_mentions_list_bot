@echo off
rem Copies the bot to %LOCALAPPDATA%\MentionsBot and adds it to Windows startup
rem (runs hidden at logon).
rem   install_autostart.bat           - install / update
rem   install_autostart.bat remove    - remove from startup (files are kept)
rem Run main.py once by hand first so the Telegram login session is saved.

setlocal
set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "DEST=%LOCALAPPDATA%\MentionsBot"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LAUNCHER=%STARTUP%\telegram_mentions_bot.vbs"

if /i "%~1"=="remove" (
    del "%LAUNCHER%" 2>nul
    echo Removed from startup. Files remain in %DEST%
    pause
    exit /b 0
)

where uv >nul 2>nul
if errorlevel 1 (
    echo ERROR: uv not found in PATH. Install it: https://docs.astral.sh/uv/
    pause
    exit /b 1
)

echo Copying program to %DEST% ...
robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS /NP ^
    /XD .venv .git .idea .vscode __pycache__ data ^
    /XF install_autostart.bat >nul
if errorlevel 8 (
    echo ERROR: copy failed.
    pause
    exit /b 1
)

rem Copy data (login session, database) only on first install,
rem so an update never overwrites the live database.
if not exist "%DEST%\data" (
    if exist "%SRC%\data" (
        robocopy "%SRC%\data" "%DEST%\data" /E /NFL /NDL /NJH /NJS /NP >nul
    ) else (
        mkdir "%DEST%\data"
    )
)

echo Installing dependencies...
pushd "%DEST%"
uv sync
if errorlevel 1 (
    popd
    echo ERROR: uv sync failed.
    pause
    exit /b 1
)
popd

> "%LAUNCHER%" (
    echo Set sh = CreateObject("WScript.Shell"^)
    echo sh.CurrentDirectory = "%DEST%"
    echo sh.Run "cmd /c uv run main.py >> ""%DEST%\data\autostart.log"" 2>&1", 0, False
)

echo.
echo Installed to: %DEST%
echo Startup entry: %LAUNCHER%
echo Starting the bot now (hidden)...
wscript "%LAUNCHER%"
pause
