 @echo off
setlocal
title HireMeQuackers Setup and Run

cd /d "%USERPROFILE%\Desktop"

if not exist "HireMeQuackers\.git" (
    echo Cloning repository...
    git clone https://github.com/Owsper/HireMeQuackers.git
    if errorlevel 1 goto error
)

cd /d "%USERPROFILE%\Desktop\HireMeQuackers"
if errorlevel 1 goto error

echo.
echo Creating virtual environment...
if not exist ".venv\Scripts\python.exe" (
    py -m venv .venv
    if errorlevel 1 goto error
)

echo.
echo Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error

echo.
echo Installing project...
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto error

echo.
echo Starting HireMeQuackers...
".venv\Scripts\quackers.exe"
if errorlevel 1 goto error

echo.
echo Program finished.
pause
exit /b 0

:error
echo.
echo An error occurred. Check the messages above.
pause
exit /b 1