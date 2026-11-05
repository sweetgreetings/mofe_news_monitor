@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title 언론 모니터링 - 설치

rem ───────────────────────────────────────────────────────────────
rem  언론 모니터링 설치 스크립트 (윈도우)
rem
rem  더블클릭 한 번으로: 파이썬·깃 확인 → 코드 받기 → 꾸러미 → DATABASE_URL →
rem  앱 켜기. **두 번째부터는 「최신 코드 받고 켜기」가 된다** — 베타 동안
rem  코드가 자주 바뀌므로 다시 누르는 것이 곧 업데이트다.
rem
rem  exe로 만들지 않는 이유: 코드가 자주 바뀌어 그때마다 다시 받아야 하고,
rem  서명이 없어 윈도우가 경고창을 띄운다(MULTIUSER_ROLLOUT_CHECKLIST.md A-2).
rem ───────────────────────────────────────────────────────────────

set "APP_DIR=%USERPROFILE%\press-monitor"
set "REPO=https://github.com/sweetgreetings/mofe_news_monitor.git"
set "BRANCH=multiuser"

echo.
echo ==========================================
echo   재정경제부 AI 뉴스 모니터링 - 설치
echo ==========================================
echo.
echo  설치할 곳: %APP_DIR%
echo.

rem ── 1. 파이썬 ────────────────────────────────────────────────
rem  py 런처를 먼저 본다 — python 은 마이크로소프트 스토어를 여는 가짜일 수 있다.
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo [1/5] 파이썬이 없습니다 - 지금 설치합니다^(2~3분^).
  winget --version >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   이 컴퓨터에는 winget 이 없어 자동 설치를 못 합니다.
    echo   https://www.python.org/downloads/ 에서 파이썬을 받아 설치할 때
    echo   첫 화면의 [Add python.exe to PATH] 를 꼭 체크해 주세요.
    echo.
    pause
    exit /b 1
  )
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo   파이썬을 깔았습니다. **이 창을 닫고 설치.bat 을 한 번 더 눌러 주세요.**
  echo   ^(새로 깐 프로그램은 창을 다시 열어야 보입니다^)
  echo.
  pause
  exit /b 0
)
echo [1/5] 파이썬 확인 - !PY!

rem  3.9 이상인지 본다(앱이 쓰는 문법의 하한이다)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>nul
if errorlevel 1 (
  echo.
  echo   파이썬이 너무 오래됐습니다 - 3.9 이상이 필요합니다.
  echo   https://www.python.org/downloads/ 에서 최신판을 설치해 주세요.
  echo.
  pause
  exit /b 1
)

rem ── 2. 깃 ────────────────────────────────────────────────────
git --version >nul 2>nul
if errorlevel 1 (
  echo [2/5] 코드를 받는 도구^(Git^)가 없습니다 - 지금 설치합니다.
  winget --version >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   이 컴퓨터에는 winget 이 없어 자동 설치를 못 합니다.
    echo   https://git-scm.com/download/win 에서 Git 을 받아 설치해 주세요.
    echo.
    pause
    exit /b 1
  )
  winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
  echo.
  echo   Git 을 깔았습니다. **이 창을 닫고 설치.bat 을 한 번 더 눌러 주세요.**
  echo.
  pause
  exit /b 0
)
echo [2/5] Git 확인

rem ── 3. 코드 받기 / 최신으로 ──────────────────────────────────
if exist "%APP_DIR%\.git" (
  echo [3/5] 최신 코드로 맞추는 중...
  git -C "%APP_DIR%" pull --ff-only origin %BRANCH%
  if errorlevel 1 (
    echo.
    echo   최신 코드를 받지 못했습니다 - 인터넷을 확인하고 다시 눌러 주세요.
    echo   ^(그래도 안 되면 %APP_DIR% 폴더를 통째로 지우고 다시 눌러 주세요^)
    echo.
    pause
    exit /b 1
  )
) else (
  echo [3/5] 코드를 받는 중...
  git clone -b %BRANCH% "%REPO%" "%APP_DIR%"
  if errorlevel 1 (
    echo.
    echo   코드를 받지 못했습니다 - 인터넷을 확인하고 다시 눌러 주세요.
    echo.
    pause
    exit /b 1
  )
)

rem ── 4. 파이썬 꾸러미 ─────────────────────────────────────────
echo [4/5] 필요한 꾸러미를 까는 중^(1~2분^)...
%PY% -m pip install --quiet --upgrade pip
%PY% -m pip install --quiet -r "%APP_DIR%\requirements.txt" -r "%APP_DIR%\requirements-postgres.txt"
if errorlevel 1 (
  echo.
  echo   꾸러미를 깔지 못했습니다 - 인터넷을 확인하고 다시 눌러 주세요.
  echo.
  pause
  exit /b 1
)

rem ── 5. 연결 주소(DATABASE_URL) ───────────────────────────────
rem  **이미 적혀 있으면 묻지 않는다** - 두 번째부터는 그냥 지나간다.
set "ENV_FILE=%APP_DIR%\.env"
set "HAS_URL="
if exist "%ENV_FILE%" (
  find /i "DATABASE_URL=" "%ENV_FILE%" >nul 2>nul && set "HAS_URL=1"
)

if not defined HAS_URL (
  echo.
  echo [5/5] 마지막입니다 - 담당자에게 받은 **연결 주소**를 붙여넣어 주세요.
  echo       ^(postgresql:// 로 시작하는 한 줄입니다. 마우스 오른쪽 클릭이 붙여넣기입니다^)
  echo.
  set /p "DB_URL=연결 주소: "
  if "!DB_URL!"=="" (
    echo.
    echo   아무것도 입력하지 않았습니다 - 주소를 받은 뒤 다시 눌러 주세요.
    echo.
    pause
    exit /b 1
  )
  >>"%ENV_FILE%" echo DATABASE_URL=!DB_URL!
  echo.
  echo   적었습니다: %ENV_FILE%
  echo   ^(주소가 잘못 적혔다면 그 파일을 메모장으로 열어 한 줄을 직접 고치면 됩니다^)
) else (
  echo [5/5] 연결 주소는 이미 적혀 있습니다 - 그대로 씁니다.
)

rem ── 붙는지 먼저 본다 ─────────────────────────────────────────
echo.
echo  DB에 붙어 보는 중...
cd /d "%APP_DIR%"
%PY% supabase_setup.py check
if errorlevel 1 (
  echo.
  echo   붙지 못했습니다. 연결 주소가 맞는지 담당자와 확인해 주세요.
  echo   ^(고칠 곳: %ENV_FILE% ^)
  echo.
  pause
  exit /b 1
)

rem ── 켜기 ─────────────────────────────────────────────────────
echo.
echo ==========================================
echo   다 됐습니다. 앱을 켭니다.
echo.
echo   * 브라우저가 저절로 열립니다.
echo   * **이 검은 창을 닫으면 앱이 꺼집니다** - 쓰는 동안은 그대로 두세요.
echo   * 다음부터는 [앱 열기] 를 누르면 됩니다.
echo   * 코드가 바뀌었다는 연락을 받으면 이 [설치] 를 다시 누르세요.
echo ==========================================
echo.
%PY% main.py
pause
