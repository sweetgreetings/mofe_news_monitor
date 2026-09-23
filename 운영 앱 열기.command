#!/bin/bash
# 운영 앱 화면을 연다. 앱은 자동 실행(LaunchAgent)이 돌리므로, 꺼져 있으면 자동 실행으로 다시 켠다.
# (터미널에서 직접 띄우면 창을 닫을 때 앱이 같이 꺼지므로 그렇게 하지 않는다.)
URL="http://127.0.0.1:8765/home.html"
if ! curl -s -o /dev/null --max-time 2 "$URL"; then
  echo "운영 앱이 꺼져 있어서 켜는 중입니다…"
  launchctl kickstart "gui/$(id -u)/local.press-monitor" 2>/dev/null
  for i in $(seq 1 30); do
    curl -s -o /dev/null --max-time 1 "$URL" && break
    sleep 1
  done
fi
if curl -s -o /dev/null --max-time 2 "$URL"; then
  open "$URL"
  echo "운영 앱 화면을 열었습니다. 이 창은 닫아도 됩니다(앱은 계속 켜져 있어요)."
else
  echo "운영 앱을 켜지 못했습니다. 자동 실행 등록이 풀렸을 수 있어요 — autostart/install.sh 를 다시 실행해 주세요."
fi
