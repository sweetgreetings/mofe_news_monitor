# Design Ref: app.breaking_alert(사용자가 편집하는 설정 파일)과 분리한 실행 상태
# 저장소 — 마지막 폴링 시각, 호출 한도 경고를 이미 알렸는지만 담는다. 같은 파일이면
# 설정 화면에서 "저장"을 누를 때마다 폴링 시각까지 덮어써 간격 계산이 매번 리셋된다.
import json

from app.atomic_write import atomic_write_text
from app.config import BREAKING_ALERT_STATE_FILE


def load_state() -> dict:
    if not BREAKING_ALERT_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(BREAKING_ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    atomic_write_text(BREAKING_ALERT_STATE_FILE, json.dumps(state, ensure_ascii=False))
