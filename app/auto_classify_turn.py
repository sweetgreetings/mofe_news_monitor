# Design Ref: CLAUDE.md "Cost control — when it's called" — 스크랩 초안(preview.html)은
# 회차당 딱 한 번만 자동으로 소제목을 분류하고, 그 뒤로는 담당자가 버튼을 눌러야 다시
# 분류한다. 그 "1회 소진" 기록을 여기서 보관한다.
#
# [추가: 2026-09-03] 예전엔 app.preview_renderer의 모듈 전역 set(_auto_classified_slots)에만
# 있었다. 그래서 앱을 껐다 켜면 같은 회차인데 기회가 다시 열렸고, 초안을 여는 순간
# **전체 재분류**가 한 번 더 돌았다 — 소제목 이름·구성·순서가 통째로 새로 지어지고,
# 이름을 키로 저장하는 이름표(group_labels)·순서(group_order)·배정(group_overrides)이
# 전부 무효가 된다. 배정 버튼(assignUnclassified)이 끝나고 location.reload()를 하므로,
# 재시작 직후 그 버튼을 누르면 "미분류만 눌렀는데 전체가 다시 분류되는" 것으로 보였다
# (담당자 제보, 2026-09-03). 실측: 2026-09-02 하루에 재시작 7회, 10:57:31 재시작 →
# 10:57:44 호출로 11:00 회차가 이름이 다른 두 벌로 남았고 14:00 회차는 세 벌이었다.
#
# 저장 형식은 custom_groups.json과 같은 {date, slots} 자정 초기화 패턴이다 — 회차 식별에
# 날짜가 이미 들어가므로 어제 기록을 들고 있을 이유가 없다.
import json
import threading
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import AUTO_CLASSIFY_TURNS_FILE

_lock = threading.Lock()


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _load_slots(now: Optional[datetime] = None) -> list:
    """오늘 이미 자동 분류를 쓴 회차(slot end) 목록. 읽을 수 없으면 빈 목록."""
    if not AUTO_CLASSIFY_TURNS_FILE.exists():
        return []
    try:
        data = json.loads(AUTO_CLASSIFY_TURNS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, OSError):
        return []
    if not isinstance(data, dict) or data.get("date") != _today_str(now):
        return []
    slots = data.get("slots", [])
    return slots if isinstance(slots, list) else []


def take_turn(slot_end: str, now: Optional[datetime] = None) -> bool:
    """이 회차의 "자동 분류 1회" 기회가 남아 있으면 **소진하고** True를 돌려준다.

    파일을 못 쓰는 상황(권한·디스크)에서는 기회를 소진한 것으로 치고 False를 돌려준다 —
    틀리는 방향이 "자동 분류를 한 번 걸렀다"(담당자가 버튼으로 만회 가능)여야지
    "매번 전체 재분류"(되돌릴 수 없는 손실)이면 안 된다.
    """
    with _lock:
        slots = _load_slots(now)
        if slot_end in slots:
            return False
        slots = slots + [slot_end]
        try:
            atomic_write_text(
                AUTO_CLASSIFY_TURNS_FILE,
                json.dumps({"date": _today_str(now), "slots": slots}, ensure_ascii=False),
            )
        except OSError:
            return False
        return True
