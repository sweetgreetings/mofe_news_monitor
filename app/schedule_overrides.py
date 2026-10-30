"""담당자가 직접 끈 일정 추정 (`📅 일정`이 틀렸을 때 그 기사 하나만).

일정 판정(`app.filters.is_schedule_listing`)은 **제목 맨 앞 말머리 하나로만** 보는 표식
판정이라 사진 추정보다 훨씬 덜 틀린다(실측 전수에서 오탐 0건). 그래도 끄는 자리를 두는
이유는 말머리는 정형인데 내용은 일반 기사인 경우가 있어서다 — YTN
「[오늘의 주요 일정] 김승원 후보자 인사청문회...여야, 정면충돌 예고」처럼.

- **`app.photo_overrides`와 같은 성격·같은 모양이다.** 「이 기사가 정형 일정 기사인가」는
  그 URL이 가리키는 **기사에 관한 사실**이지, 어느 보고서에 넣고 뺄지(`hidden_articles`)나
  어느 소제목에 담을지(`group_overrides`) 같은 **편집 결정**이 아니다. 그래서 URL 키
  전역이고, 어느 화면에서 껐든 모든 화면이 함께 본다. 나이로 지우지 않는다.
- **입히는 자리는 `is_schedule_listing()` 맨 앞 한 곳**이다. 그래서 화면 여섯·수시 새 수집
  「일정 기사 제외」·기관 기사 찾기 「일정 제외」·홈 「오늘의 주요 주제」 순위가 저절로
  따라온다.
- **방향은 해제뿐이다.** 「일정으로 표시」는 만들지 않는다 — 말머리가 없는 기사를 일정으로
  만들 길은 두지 않았다(그건 추정 층 몫이고 아직 보류다). 저장 형식만 양방향으로 열어 둔다.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import store
from app.config import SCHEDULE_OVERRIDES_FILE

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

# 한 화면이 기사 수백 건을 훑으며 기사마다 이 값을 묻는다(기관 기사 찾기는 수천 건).
# 파일을 그때마다 열지 않도록 (mtime_ns, size)가 같으면 읽어둔 것을 쓴다 —
# app.photo_overrides와 같은 방식이다.
_lock = threading.Lock()
_cache: Optional[dict] = None
_cache_sig: Optional[tuple] = None


def _signature() -> Optional[tuple]:
    try:
        st = SCHEDULE_OVERRIDES_FILE.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_locked() -> dict:
    global _cache, _cache_sig
    sig = _signature()
    if _cache is not None and sig == _cache_sig:
        return _cache
    data: dict = {}
    if sig is not None:
        try:
            parsed = store.read_json(SCHEDULE_OVERRIDES_FILE)
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            # 깨진 파일 때문에 화면이 멈추면 안 된다 — 비어 있는 것으로 보고 경고만 남긴다.
            logger.warning("일정 추정 해제 기록을 읽지 못했습니다: %s", exc)
    _cache, _cache_sig = data, sig
    return data


def load_schedule_overrides() -> dict:
    """{url: {"schedule": bool, "at": ISO}} 전체를 돌려준다(읽기 전용으로 쓴다)."""
    with _lock:
        return _read_locked()


def schedule_override(url: str) -> Optional[bool]:
    """그 URL에 담당자가 적어 둔 판정. 적은 적이 없으면 None(= 앱의 판정을 그대로 쓴다)."""
    if not url:
        return None
    entry = load_schedule_overrides().get(url)
    if not isinstance(entry, dict):
        return None
    value = entry.get("schedule")
    return value if isinstance(value, bool) else None


def is_schedule_cleared(url: str) -> bool:
    """담당자가 「일정 추정 해제」를 눌러 둔 기사인가 — ⋯ 메뉴의 글자를 고르는 데 쓴다."""
    return schedule_override(url) is False


def set_schedule_override(url: str, schedule: Optional[bool]) -> None:
    """그 기사의 일정 판정을 담당자 값으로 적는다. `schedule=None`이면 항목째 지워
    앱의 판정으로 되돌린다(「일정 추정 되돌리기」)."""
    if not url:
        return
    global _cache, _cache_sig
    with _lock:
        data = dict(_read_locked())
        if schedule is None:
            if url not in data:
                return
            data.pop(url)
        else:
            data[url] = {
                "schedule": schedule,
                "at": datetime.now(_KST).isoformat(timespec="seconds"),
            }
        store.write_json(SCHEDULE_OVERRIDES_FILE, data, indent=2)
        _cache, _cache_sig = data, _signature()
    logger.info("일정 추정 %s: %s", "되돌림" if schedule is None else "해제", url)


def _reset_cache_for_tests() -> None:
    """테스트가 파일을 갈아끼운 뒤 부른다(실행 중인 앱은 서명이 달라져 저절로 다시 읽는다)."""
    global _cache, _cache_sig
    with _lock:
        _cache, _cache_sig = None, None
