#!/bin/bash
# 자동 실행 해제. 앱 자체는 지우지 않고 launchd 등록만 뺀다.
set -euo pipefail
LABEL="local.press-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✅ 자동 실행 해제 완료 — 이제 python3 main.py 로 직접 켜야 합니다."
