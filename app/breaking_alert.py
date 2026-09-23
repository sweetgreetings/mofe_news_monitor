# Design Ref: 사용자 요청(2026-08-20) — [단독]·[속보] 기사 알림(/breaking-alert) 설정.
# app.settings(data/settings.json)와 분리한 이유는 app.telegram_recipients.json과 같다
# (CLAUDE.md "[단독]·[속보] 기사 알림" 참고) — 이 기능 하나만의 값이라 전역 설정을
# 매번 통째로 읽고 쓰는 경합에 얹지 않는다.
import json
import re
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import (
    BREAKING_ALERT_FILE,
    BURST_MIN_OUTLETS_CHOICES,
    BURST_WINDOW_CHOICES,
    DEFAULT_BURST_MIN_OUTLETS,
    DEFAULT_BURST_WINDOW_MIN,
    DEFAULT_BREAKING_ALERT_END,
    DEFAULT_BREAKING_ALERT_INTERVAL_MIN,
    DEFAULT_BREAKING_ALERT_START,
    MAX_BREAKING_ALERT_INTERVAL_MIN,
    MIN_BREAKING_ALERT_INTERVAL_MIN,
)

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class BreakingAlertSettingsError(ValueError):
    """[단독]·[속보] 알림 설정값이 잘못됐을 때(시간 형식, 주기 범위)."""


def _default() -> dict:
    return {
        "enabled": True,
        "group_names": [],
        "start": DEFAULT_BREAKING_ALERT_START,
        "end": DEFAULT_BREAKING_ALERT_END,
        "interval_min": DEFAULT_BREAKING_ALERT_INTERVAL_MIN,
        "catch_up_enabled": True,
        # [속보] 몰림 알림(app.breaking_burst). 키가 없는 옛 설정 파일도 이 기본값으로 켜진다.
        "burst_enabled": True,
        "burst_window_min": DEFAULT_BURST_WINDOW_MIN,
        "burst_min_outlets": DEFAULT_BURST_MIN_OUTLETS,
    }


def load_breaking_alert_settings() -> dict:
    if not BREAKING_ALERT_FILE.exists():
        return _default()
    try:
        data = json.loads(BREAKING_ALERT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return _default()
    if not isinstance(data, dict):
        return _default()
    return {**_default(), **data}


def _validate_time(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _TIME_PATTERN.match(value):
        raise BreakingAlertSettingsError(f"{label} 형식이 올바르지 않습니다 (HH:MM).")
    return value


def _choice(value, choices: tuple, label: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise BreakingAlertSettingsError(f"{label} 값이 올바르지 않습니다.")
    if number not in choices:
        raise BreakingAlertSettingsError(f"{label} 값이 올바르지 않습니다.")
    return number


def save_breaking_alert_settings(
    enabled: bool,
    group_names: list,
    start: str,
    end: str,
    interval_min,
    catch_up_enabled: bool,
    burst_enabled: bool = True,
    burst_window_min=DEFAULT_BURST_WINDOW_MIN,
    burst_min_outlets=DEFAULT_BURST_MIN_OUTLETS,
) -> dict:
    """설정 화면(/breaking-alert)의 "저장". 감시 대상 그룹은 이름 문자열로 저장한다 —
    이 프로젝트의 기존 저장소들(data/group_order.json, data/custom_groups.json 등)과
    같은 관례로, 그룹에 별도 ID가 없다. 그룹 이름을 나중에 바꾸면 감시 대상에서
    조용히 빠진다는 뜻인데, 소제목 강제배정(group_overrides)도 같은 한계를 갖고 있고
    그룹 이름은 자주 바뀌는 값이 아니라 감수한다.
    """
    start = _validate_time(start, "감시 시작 시각")
    end = _validate_time(end, "감시 종료 시각")
    try:
        interval = int(str(interval_min).strip())
    except (TypeError, ValueError):
        raise BreakingAlertSettingsError("확인 주기는 숫자로 입력해주세요.")
    if not MIN_BREAKING_ALERT_INTERVAL_MIN <= interval <= MAX_BREAKING_ALERT_INTERVAL_MIN:
        raise BreakingAlertSettingsError(
            f"확인 주기는 {MIN_BREAKING_ALERT_INTERVAL_MIN}~{MAX_BREAKING_ALERT_INTERVAL_MIN}분 사이로 입력해주세요."
        )
    burst_window = _choice(burst_window_min, BURST_WINDOW_CHOICES, "몰림 알림의 시간")
    burst_outlets = _choice(burst_min_outlets, BURST_MIN_OUTLETS_CHOICES, "몰림 알림의 언론사 수")
    data = {
        "enabled": bool(enabled),
        "group_names": [str(name) for name in group_names if str(name).strip()],
        "start": start,
        "end": end,
        "interval_min": interval,
        "catch_up_enabled": bool(catch_up_enabled),
        "burst_enabled": bool(burst_enabled),
        "burst_window_min": burst_window,
        "burst_min_outlets": burst_outlets,
    }
    atomic_write_text(BREAKING_ALERT_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def is_within_window(now_hm: str, start: str, end: str) -> bool:
    """"HH:MM" 문자열 셋으로 지금이 감시 시간대 안인지 판단한다.

    start < end면 보통의 낮 구간(예: 09:00~20:00) — start<=now<end.
    start == end면 "하루 종일"로 취급한다(자정을 걸치는 구간을 시:분 두 값만으로
    표현하는 가장 단순한 방법 — 24시간 내내 감시하고 싶을 때 start=end=아무 값이나
    넣으면 된다는 뜻).
    start > end면 자정을 걸친 구간(예: 22:00~06:00)으로 본다 — 담당자가 밤 시간을
    감시하고 싶어질 경우까지 막지 않는다.
    """
    if start == end:
        return True
    if start < end:
        return start <= now_hm < end
    return now_hm >= start or now_hm < end
