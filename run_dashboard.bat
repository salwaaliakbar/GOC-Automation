@echo off
title GOC Register Data Collector
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python is not installed on this computer.
  echo  Please install it from https://www.python.org/downloads/
  echo  and tick "Add Python to PATH" during installation. Then try again.
  echo.
  pause
  exit /b 1
)

python -c "import fastapi, uvicorn, openpyxl, requests, bs4, lxml, multipart" >nul 2>nul
if errorlevel 1 (
  echo  First start: installing the required components. This takes a minute...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  Installation failed. Please check the internet connection and try again.
    pause
    exit /b 1
  )
)

python -m goc_collector serve
echo.
echo  The collector has closed. You can close this window.
pause
