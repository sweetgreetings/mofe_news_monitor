# Design Ref: 사용자 요청(2026-08-20) — [단독]·[속보] 기사 알림(/breaking-alert) 설정.
# app.settings(data/settings.json)와 분리한 이유는 app.telegram_recipients.json과 같다
# (CLAUDE.md "[단독]·[속보] 기사 알림" 참고) — 이 기능 하나만의 값이라 전역 설정을
# 매번 통째로 읽고 쓰는 경합에 얹지 않는다.
import json
import re
from typing import Optional

from app import store
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
        data = store.read_json(BREAKING_ALERT_FILE)
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
    start: str,
    end: str,
    interval_min,
    catch_up_enabled: bool,
    burst_enabled: bool = True,
    burst_window_min=DEFAULT_BURST_WINDOW_MIN,
    burst_min_outlets=DEFAULT_BURST_MIN_OUTLETS,
) -> dict:
    """설정 화면(/breaking-alert)의 "저장" — 시간대·주기·몰아보내기·몰림 알림만 맡는다.

    [수정] 감시 **대상**(어느 검색어 그룹을 볼까)은 여기서 빠졌다. 검색어 화면의 그룹
    스위치(include_in_alert)가 그 자리다 — 예전엔 그룹 이름 문자열 목록이라 그룹명을
    바꾸면 감시가 조용히 풀렸고, 무엇보다 회차 수집의 「바닥」 경로가 이 설정을 통째로
    무시해서 담당자가 알림을 끌 수가 없었다(app.alert_groups).
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
        "start": start,
        "end": end,
        "interval_min": interval,
        "catch_up_enabled": bool(catch_up_enabled),
        "burst_enabled": bool(burst_enabled),
        "burst_window_min": burst_window,
        "burst_min_outlets": burst_outlets,
    }
    store.write_json(BREAKING_ALERT_FILE, data, indent=2)
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
