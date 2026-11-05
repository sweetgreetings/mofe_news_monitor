@echo off
chcp 65001 >nul
title 언론 모니터링

rem  앱만 켠다 — 코드를 받지도, 꾸러미를 깔지도 않는다(그건 설치.bat 몫이다).
rem  이미 켜져 있으면 앱이 스스로 알아보고 브라우저만 연다.

set "APP_DIR=%USERPROFILE%\press-monitor"

if not exist "%APP_DIR%\main.py" (
  echo.
  echo   앱이 아직 깔려 있지 않습니다 - 먼저 [설치] 를 눌러 주세요.
  echo.
  pause
  exit /b 1
)

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo   파이썬을 찾지 못했습니다 - [설치] 를 한 번 눌러 주세요.
  echo.
  pause
  exit /b 1
)

echo.
echo   언론 모니터링을 켭니다. **이 창을 닫으면 앱이 꺼집니다.**
echo.
cd /d "%APP_DIR%"
%PY% main.py
pause
