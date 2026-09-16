#!/bin/bash
# 언론 모니터링 앱을 macOS 로그인 시 자동으로 띄운다 (launchd LaunchAgent 등록).
#
# 왜 필요한가 — 2026-08-19~09-02 실측: 예정 78회차 중 14회(18%)가 통째로 비었다.
# 전부 "앱을 손으로 안 켠" 날이다(8/28 오후~8/30 주말 10회 연속). 네이버 검색 API에는
# 기간 지정 파라미터가 없고 키워드당 최신 1000건까지만 주므로, 정기 스크랩 키워드
# 중 가장 좁은 '재정경제부'가 실측상 하루치밖에 안 거슬러 간다 — 즉 놓친 회차는
# 하루만 지나면 어떤 방법으로도 못 되살린다. 안 놓치는 것 말고 다른 길이 없다.
#
# 사용법:  bash autostart/install.sh
# 해제:    bash autostart/uninstall.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"
LABEL="local.press-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$PYTHON" ]; then
  echo "❌ 파이썬을 찾을 수 없습니다: $PYTHON"
  echo "   다른 파이썬을 쓰려면:  PYTHON=/경로/python3 bash autostart/install.sh"
  exit 1
fi
if ! "$PYTHON" -c "import anthropic, requests, dotenv, openpyxl" 2>/dev/null; then
  echo "❌ $PYTHON 에 필요한 패키지가 없습니다 — 먼저:  $PYTHON -m pip install -r requirements.txt"
  exit 1
fi

# [추가: 2026-09-04] macOS 보호 폴더(TCC) 사전 경고.
# 데스크탑/서류/다운로드 폴더는 macOS가 보호하는 위치라, launchd가 띄운 백그라운드
# 프로세스는 **아무 프롬프트 없이 조용히 거부당한다**(실측 2026-09-04: 이 앱이
# ~/Desktop/my_app 에 있어서 에이전트가 main.py를 못 읽고 30회 넘게 죽고 있었다 —
# 그런데도 이 스크립트는 "등록 완료"라고만 했다). 미리 알린다.
case "$PROJECT_DIR/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
    echo "⚠️  이 앱이 macOS 보호 폴더 안에 있습니다:"
    echo "    $PROJECT_DIR"
    echo "    이 위치에서는 자동 실행이 **조용히 실패**할 수 있습니다(권한 거부)."
    echo "    설치는 계속 진행하고, 아래에서 실제로 떴는지 확인합니다."
    echo
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/data/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$PROJECT_DIR/main.py</string>
        <string>--no-browser</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <!-- 로그인하면 바로 띄운다. -->
    <key>RunAtLoad</key>
    <true/>

    <!-- 비정상 종료(크래시)일 때만 다시 살린다. 중복 실행 자물쇠에 걸려 정상 종료(0)한
         경우까지 되살리면 무한 재시작이 된다 — main.py의 EADDRINUSE 경로가 그렇다. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/data/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/data/logs/launchd.err.log</string>
</dict>
</plist>
PLISTEOF

# 이미 등록돼 있으면 먼저 내린다(설정을 바꿔 다시 설치하는 경우).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"

# [추가: 2026-09-04] **등록했다 ≠ 돈다.** 예전엔 여기서 바로 "✅ 등록 완료"를 찍고
# 끝냈는데, 정작 에이전트는 권한 거부로 30회 넘게 죽고 있었고 아무도 몰랐다. 등록만으로
# 성공을 선언하지 않는다 — 실제로 떴는지 확인하고, 안 떴으면 원인과 고치는 법을 말한다.
ERR_LOG="$PROJECT_DIR/data/logs/launchd.err.log"
ERR_BEFORE=$(wc -c < "$ERR_LOG" 2>/dev/null || echo 0)
sleep 6

# launchctl list 출력은 "PID  마지막종료코드  라벨". PID가 "-"이고 종료코드가 0이 아니면
# 떴다가 죽은 것이다. (종료코드 0 + PID "-"는 정상 — 앱이 이미 손으로 켜져 있어서
# 중복 실행 자물쇠에 걸려 얌전히 빠진 경우다. main.py의 EADDRINUSE 경로.)
LINE=$(launchctl list 2>/dev/null | grep -F "$LABEL" || true)
PID=$(echo "$LINE" | awk '{print $1}')
CODE=$(echo "$LINE" | awk '{print $2}')
NEW_ERR=$(tail -c "+$((ERR_BEFORE + 1))" "$ERR_LOG" 2>/dev/null || true)

if [ -z "$LINE" ]; then
  echo "❌ 자동 실행 등록에 실패했습니다 — launchctl 목록에 $LABEL 이 없습니다."
  exit 1
fi

if echo "$NEW_ERR" | grep -q "Operation not permitted"; then
  echo "❌ 자동 실행이 **권한 거부로 실패**했습니다."
  echo
  echo "   원인: 이 앱이 macOS 보호 폴더(데스크탑/서류/다운로드) 안에 있습니다."
  echo "         $PROJECT_DIR"
  echo "         파일 권한 문제가 아니라 macOS 보안 정책(TCC)이라, 백그라운드로 뜨는"
  echo "         프로세스는 이 폴더를 아예 못 읽습니다."
  echo
  echo "   고치는 법 (둘 중 하나):"
  echo "     A) 앱 폴더를 보호 폴더 밖으로 옮긴다 (권장 — 권한 설정이 아예 필요 없음)"
  echo "        예:  mv \"$PROJECT_DIR\" ~/press-monitor && cd ~/press-monitor && bash autostart/install.sh"
  echo "     B) 시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근 권한에"
  echo "        $PYTHON 을(를) 추가한다 (암호 필요, macOS 업데이트 때 풀릴 수 있음)"
  echo
  echo "   원본 오류: $(echo "$NEW_ERR" | tail -1)"
  exit 1
fi

if [ "$PID" = "-" ] && [ "${CODE:-0}" != "0" ]; then
  echo "❌ 자동 실행이 떴다가 즉시 종료됐습니다 (마지막 종료 코드: $CODE)."
  echo "   로그를 확인하세요:  tail -20 $ERR_LOG"
  exit 1
fi

echo "✅ 자동 실행 등록 완료 — 로그인할 때마다 앱이 백그라운드로 뜹니다. (실제 실행까지 확인함)"
echo "   화면 열기:  http://127.0.0.1:8765/home.html  (또는 python3 main.py 를 그냥 실행)"
echo "   상태 확인:  launchctl list | grep $LABEL"
echo "   해제:       bash autostart/uninstall.sh"
echo
echo "⚠️  이것만으로는 '컴퓨터가 잠들어 있는' 동안은 못 막습니다 — autostart/README.md 참고."
