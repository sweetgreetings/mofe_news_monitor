# Design Ref: CLAUDE.md "스케줄러 — 하루 마지막 회차 보충 훑기"
"""하루 마지막 회차의 「보충 훑기」 — 언제 도는지를 정하는 순수 규칙.

**왜 있나.** 회차 창은 (시작, 마감]이고 다음 회차의 하한이 정확히 이 회차의 마감이라,
마감 순간 네이버 검색이 아직 안 준 기사는 다음 회차가 하한을 COLLECT_LOOKBACK_MIN만큼
물려 주워 담는다(app.scraper.collect_run). **그런데 하루 마지막 회차엔 주워갈 다음 회차가
없다** — 다음날 첫 회차는 전날 기사를 안 본다(app.naver_api._is_today_kst). 그래서 그
기사들은 어느 회차에도 못 들어가고 영영 사라졌다.

**예전 방식(기각).** 마지막 회차만 수집 실행을 30분 늦췄다(옛
`LAST_SLOT_COLLECT_DELAY_MIN`·`_last_slot_is_waiting`). 기사는 많이 담겼지만 담당자가
마감 시각에 확정본을 못 봤다 — 중간 회차는 1분 안에 나오는데 하루의 마지막 보고서만
30~45분을 기다려야 했고, "왜 17시 확정본이 안 나오나"로 되풀이됐다(2026-09-29 실사용).

**지금 방식.** 마지막 회차도 **정시에 수집해 확정본을 낸다**(중간 회차와 똑같이). 그리고
이 모듈이 정한 시각에 **같은 창을 한 번 더 훑어 그 회차에 아직 없는 기사만 덧붙인다**
(app.scraper.top_up_last_run). 확정본은 마감 순간에 보이고, 늦게 들어온 기사는 이미 있는
앰버 배너(app.renderer의 late-banner)가 알려 준다. 덧붙이기라 담당자가 그 사이 다듬어 둔
숨김·이동·이름표·순서는 하나도 안 건드린다.

**자정 규칙.** 보충 시각은 LAST_SLOT_TOPUP_LAST_CALL_HHMM(23:50)에서 잘리고, 마지막
회차의 마감이 그 시각 이후면 보충을 아예 건너뛴다 — 자정이 지나면 네이버가 어제 기사를
안 주고(당일만 준다) 회차 파일의 날짜도 어긋난다. 이 23:50은 새로 정한 값이 아니라
「🗄️ 기관 기사 찾기」 원장이 같은 이유로 이미 쓰는 자정 직전 마감선이다
(app.agency_archive._LAST_CALL_HHMM).
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.config import LAST_SLOT_TOPUP_LAST_CALL_HHMM, LAST_SLOT_TOPUP_MIN
from app.settings import active_schedule_times, load_settings
from app.storage import load_today_runs

logger = logging.getLogger(__name__)


def schedule_times_now(now: Optional[datetime] = None) -> List[dict]:
    """지금 적용 중인 시간대 목록(꺼둔 시간대는 뺀다).

    app.scheduler._resolve_schedule_times와 같은 값을 보되 그 모듈을 import하지 않는다 —
    이 모듈은 app.trend_data처럼 가벼운 곳에서도 부르는 순수 규칙이라, 스케줄러(수집·발송
    ·알림을 다 끌어온다)에 의존하면 import 그래프가 뒤엉킨다. 「✂ 오늘만 끊기」가 끼워
    넣은 회차도 active_schedule_times 한 곳을 거치므로 여기서 자동으로 함께 보인다.
    """
    times = active_schedule_times(load_settings(), now)
    return [w for w in times if w.get("enabled", True)]


def last_window(schedule_times: Optional[List[dict]] = None) -> Optional[dict]:
    """하루 마지막 회차의 시간창. 시간대가 하나도 없으면 None."""
    times = schedule_times if schedule_times is not None else schedule_times_now()
    if not times:
        return None
    return max(times, key=lambda w: w["end"])


def topup_due_at(schedule_times: Optional[List[dict]] = None) -> Optional[str]:
    """마지막 회차의 보충 훑기를 돌 시각("HH:MM"). 보충하지 않는 경우엔 None.

    규칙(위 모듈 설명 참고):
      - 마감 + LAST_SLOT_TOPUP_MIN
      - 그 시각이 LAST_SLOT_TOPUP_LAST_CALL_HHMM을 넘으면 그 시각으로 당긴다
        (23:40 회차 -> 00:10이 아니라 23:50에 10분만 벌어 훑는다)
      - 마감 자체가 LAST_SLOT_TOPUP_LAST_CALL_HHMM 이후면 None — 벌 시간이 없다
        (23:59 회차엔 보충이 없다. 자정을 넘길 수는 없기 때문이다)
    """
    window = last_window(schedule_times)
    if window is None:
        return None
    end = window["end"]
    if end >= LAST_SLOT_TOPUP_LAST_CALL_HHMM:
        return None
    hour, minute = map(int, end.split(":"))
    total = hour * 60 + minute + LAST_SLOT_TOPUP_MIN
    due = f"{total // 60:02d}:{total % 60:02d}" if total < 24 * 60 else "24:00"
    return min(due, LAST_SLOT_TOPUP_LAST_CALL_HHMM)


def find_run(date_str: str, run_slot: str) -> Optional[dict]:
    """그 날짜에 저장된 그 회차. 없으면 None (아직 수집 전이거나 실패했다)."""
    for run in load_today_runs(date_str):
        if run.get("run_slot") == run_slot:
            return run
    return None


def topup_pending(now: Optional[datetime] = None, schedule_times: Optional[List[dict]] = None) -> bool:
    """오늘 마지막 회차의 보충이 **아직 남아 있는가**(할 일이 있다 = True).

    보충이 필요 없는 경우(마지막 회차가 없다·23:50 이후 마감·이미 보충했다)와 **회차가
    아직 저장되지도 않은 경우**는 False다 — 뒤의 것은 "보충이 남았다"가 아니라 "수집이
    남았다"이고, 그건 부르는 쪽이 이미 run_exists로 따로 본다(app.trend_data).
    """
    now = now or datetime.now()
    times = schedule_times if schedule_times is not None else schedule_times_now(now)
    window = last_window(times)
    if window is None or topup_due_at(times) is None:
        return False
    run = find_run(now.strftime("%Y-%m-%d"), window["end"])
    if run is None:
        return False
    return not run.get("topup_at")
