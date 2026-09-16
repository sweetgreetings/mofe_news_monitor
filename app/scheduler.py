# Design Ref: PRD.md 기능1 규칙 1·9·20 — 자동 실행(설정한 시각·횟수) + 놓친 회차는 가장 최근 1개만 보충
import logging
import time
from datetime import datetime
from typing import Callable, List, Optional

from app.breaking_alert_sender import poll_and_alert_tick
from app.config import LAST_SLOT_COLLECT_DELAY_MIN, MAX_RETRIES
from app.confirm_send import check_pending_confirm_and_send
from app.renderer import generate_waiting_page
from app.scraper import collect_run_with_retry
from app.settings import active_schedule_times, load_settings
from app.curation import cleanup_group_overrides
from app.storage import delete_expired_runs, run_exists

logger = logging.getLogger(__name__)

# 폴링 간격(초). [수정: 2026-08-07] 60초일 때 "정시 실행"이 최대 60초까지 늦어질 수 있어
# (예: 매 정각 30분 슬롯을 30분 59초에 확인하면 그 tick은 놓치고 다음 tick인 31분 59초에야
# 감지) 텔레그램/이메일 자동 전송이 "31분에 온다"는 사용자 체감으로 이어졌다. 이 확인 자체는
# 설정 파일을 한 번 읽고 시각을 비교하는 가벼운 작업이라(로컬 단일 사용자 앱) 10초로 줄여도
# 비용 증가는 무시할 만하고, 최악 지연을 10초 이내로 낮춘다.
POLL_INTERVAL_SEC = 10


def _resolve_schedule_times(schedule_times: Optional[List[dict]]) -> List[dict]:
    """schedule_times를 생략하면 설정 화면에 저장된 값을 매번 새로 읽는다 (규칙20).

    스케줄 루프가 매 tick마다 이 함수를 거치므로, 앱을 재시작하지 않아도 설정 화면에서
    바꾼 시간대·횟수가 다음 tick(최대 POLL_INTERVAL_SEC 이내)부터 바로 반영된다.

    [추가: 2026-07-29] enabled=False인 시간대는 여기서 걸러낸다 — current_active_slot/
    next_pending_slot/find_slot_to_run 등 이 함수를 거치는 모든 곳이 자동으로 꺼둔
    시간대를 무시하게 된다(평일/휴일처럼 상황에 따라 켜고 끌 시간대를 지우지 않고
    남겨둘 수 있다). enabled 필드가 없는 옛 데이터는 켜진 것으로 간주한다.

    [수정: 2026-07-29] 시간대 자체가 이제 "그룹"(주중/주말 등, 그 중 1개만 활성)으로
    묶여있어, 생략 시 설정에서 지금 활성인 그룹의 시간대만 가져온다(app.settings
    active_schedule_times) — 활성 그룹을 바꾸면 다음 tick부터 바로 반영된다.
    """
    times = schedule_times if schedule_times is not None else active_schedule_times(load_settings())
    return [w for w in times if w.get("enabled", True)]


def current_active_slot(now: datetime, schedule_times: Optional[List[dict]] = None) -> Optional[dict]:
    """지금 시각 기준으로 '오늘 이미 지나온 회차 중 가장 최근 회차'의 시간창을 돌려준다.

    각 회차는 {"start": "HH:MM", "end": "HH:MM"} 형태다 (PRD.md 기능1 규칙 2 — 회차별
    시간창 수집). 예) 지금이 11:00이고 회차 끝 시각이 09:00·10:30이면 10:30 회차를 반환.
    아직 첫 회차 전이면 오늘 지나온 회차가 없으므로 None.
    """
    schedule_times = _resolve_schedule_times(schedule_times)
    now_hm = now.strftime("%H:%M")
    passed = [window for window in schedule_times if window["end"] <= now_hm]
    return max(passed, key=lambda window: window["end"]) if passed else None


def next_pending_slot(
    now: datetime,
    schedule_times: Optional[List[dict]] = None,
    already_run: Callable[[str], bool] = run_exists,
) -> Optional[dict]:
    """지금 시각 기준으로 '아직 안 끝난 회차 중 가장 먼저 끝나는 회차'(진행 중인 회차)를 돌려준다.

    회차 미리보기(app.preview_renderer)에서 쓴다 — current_active_slot과 정반대로,
    아직 끝나지 않은 회차 중 가장 임박한 것을 찾는다. 오늘 모든 회차가 이미 끝났으면
    (마지막 회차 끝 시각도 지났으면) None.

    [수정: 2026-08-11] 이미 수집이 끝난 회차는 건너뛴다 — "지금 마감하기"로 종료 시각
    전에 미리 마감하면(예: 9:30 회차를 9:27에 마감) 시계로는 아직 그 회차가 "안 끝난
    회차"라, 방금 확정본으로 넘긴 바로 그 구간을 초안이 또 미리보기로 검색해 같은 기사가
    양쪽에 동시에 뜨는 문제가 있었다. run_exists로 실제 수집 여부를 함께 보고 판단한다.
    """
    schedule_times = _resolve_schedule_times(schedule_times)
    now_hm = now.strftime("%H:%M")
    upcoming = [w for w in schedule_times if w["end"] > now_hm and not already_run(w["end"])]
    return min(upcoming, key=lambda window: window["end"]) if upcoming else None


# [추가: 2026-09-04] 회차 하나를 하루에 몇 번까지 다시 시도할지.
#
# 놓친 회차를 "가장 최근 1개"만 채우던 시절엔 이 상한이 필요 없었다 — 실패한 회차는 다음
# 회차가 도착하는 순간 "가장 최근"이 아니게 되어 후보에서 저절로 빠졌기 때문이다. 보충을
# "오늘 놓친 전부"로 넓히면서 그 자연스러운 제동이 사라졌다: 어떤 회차가 계속 실패하면
# run_exists가 하루 종일 False로 남아 **매 tick(10초)마다** 다시 시도되고, 한 번 시도가
# collect_run_with_retry의 내부 재시도(3회 × 5분)까지 도는 탓에 tick 하나가 10분씩 잡아먹는다.
#
# 값은 MAX_RETRIES(3)를 그대로 쓴다 — 안쪽 재시도와 곱해져 회차당 최대 9번·30분이라
# 일시적 장애를 넘기기엔 충분하고, 영영 못 받는 회차에 하루를 쓰지도 않는다.
_MAX_SLOT_ATTEMPTS = MAX_RETRIES

# {(YYYY-MM-DD, run_slot): 실패 횟수} — 프로세스 메모리에만 둔다(앱을 다시 켜면 초기화).
# 디스크에 남기지 않는 이유: 담당자가 앱을 다시 켰다는 건 "이제 되겠지"라는 뜻이고,
# 그 판단을 지난 실패 기록이 막으면 안 된다.
_slot_failures: dict = {}


def _give_up_on(now: datetime, run_slot: str) -> bool:
    """이 회차를 오늘 더 시도하지 않기로 했는지."""
    return _slot_failures.get((now.date().isoformat(), run_slot), 0) >= _MAX_SLOT_ATTEMPTS


def _record_slot_failure(now: datetime, run_slot: str) -> int:
    """실패를 세고 누적 횟수를 돌려준다. 오늘 것만 남기고 지난 날짜는 버린다."""
    today = now.date().isoformat()
    for key in [k for k in _slot_failures if k[0] != today]:
        del _slot_failures[key]
    count = _slot_failures.get((today, run_slot), 0) + 1
    _slot_failures[(today, run_slot)] = count
    return count


def _last_slot_is_waiting(
    now: datetime, window: dict, schedule_times: Optional[List[dict]] = None
) -> bool:
    """하루 마지막 회차인데 아직 LAST_SLOT_COLLECT_DELAY_MIN이 안 지났으면 True(= 더 기다린다).

    [추가: 2026-09-03] 마감 순간 네이버 검색이 아직 안 준 기사는 다음 회차가 하한을
    물려 주워오는데(app.scraper.collect_run), **마지막 회차만은 주워갈 다음 회차가
    없다** — 다음날 첫 회차는 전날 기사를 안 본다. 그래서 마지막 회차만 수집 실행을
    이 분 수만큼 늦춘다. 시간창(start/end)도 회차 이름(run_slot)도 그대로이고 수집하는
    시각만 뒤로 미는 것이라, 저장 파일·헤더·보고서 텍스트에는 아무 티가 안 난다.

    늦추는 건 "아직 이르다"일 때뿐이라 보충 실행(앱이 꺼져 있다 켜진 경우)은 그대로
    즉시 돈다 — 그때는 이미 지연 시각을 한참 지났기 때문이다.

    지연 시각이 자정을 넘기면(예: 마지막 회차가 23:50) 그날 안에 그 시각이 오지 않아
    회차가 통째로 누락된다 — 그 경우엔 늦추지 않는다(늦추는 건 부가 기능이고, 회차를
    거르는 건 본업 실패다).
    """
    schedule_times = _resolve_schedule_times(schedule_times)
    if not schedule_times:
        return False
    if window["end"] != max(w["end"] for w in schedule_times):
        return False  # 마지막 회차가 아니면 예전 그대로 정시 실행
    hour, minute = map(int, window["end"].split(":"))
    total = hour * 60 + minute + LAST_SLOT_COLLECT_DELAY_MIN
    if total >= 24 * 60:
        return False  # 자정을 넘기면 늦추지 않는다
    due_hm = f"{total // 60:02d}:{total % 60:02d}"
    return now.strftime("%H:%M") < due_hm


def find_slots_to_run(
    now: datetime,
    schedule_times: Optional[List[dict]] = None,
    already_run: Callable[[str], bool] = run_exists,
) -> List[dict]:
    """지금 실행해야 할 회차의 시간창들을 **오래된 것부터** 돌려준다. 없으면 빈 목록.

    [수정: 2026-09-04] 예전엔 "가장 최근에 놓친 회차 1개만"이었다(PRD 기능1 규칙9).
    실측(2026-08-19~09-02)에서 예정 78회차 중 14회가 통째로 비었는데, 그중 여러 회차를
    한 번에 놓친 날(8/28 4회·8/31 2회)은 앱을 켜도 마지막 하나만 채워졌다. 놓친 회차는
    되살릴 방법이 없다 — 네이버 검색 API에 기간 파라미터가 없고 키워드당 최신 1000건까지만
    주는데, 정기 스크랩 키워드 중 가장 좁은 것이 하루치밖에 안 거슬러 간다(실측). 그래서
    **오늘 안에서 되살릴 수 있는 건 전부 되살린다.**

    **오래된 것부터**가 이 함수의 계약이다 — app.scraper.collect_run이
    `_already_published_urls`(오늘 이미 저장된 회차들의 기사 URL)로 앞 회차 기사를
    걸러내므로, 최신부터 돌리면 그 방어가 거꾸로 작동해 앞 회차가 자기 기사를 뺏긴다.

    어제 것까지 거슬러 올라가지 않는 이유: 회차 시간창은 언제나 "오늘"로 해석되고
    (app.naver_api.kst_today_at·_is_today_kst) 위 1000건 한계상 그 이상은 어차피 못 가져온다.
    """
    schedule_times = _resolve_schedule_times(schedule_times)
    now_hm = now.strftime("%H:%M")
    passed = [w for w in schedule_times if w["end"] <= now_hm and not already_run(w["end"])]
    # 하루 마지막 회차만은 아직 지연 시각 전이면 남겨둔다(_last_slot_is_waiting) — 그 판단은
    # "마지막 회차인가"까지 함수 안에서 하므로 여기서 따로 가려낼 필요가 없다.
    passed = [w for w in passed if not _last_slot_is_waiting(now, w, schedule_times)]
    # 오늘 이미 여러 번 실패한 회차는 더 붙들지 않는다(_MAX_SLOT_ATTEMPTS 주석 참고).
    passed = [w for w in passed if not _give_up_on(now, w["end"])]
    return sorted(passed, key=lambda window: window["end"])


def find_slot_to_run(
    now: datetime,
    schedule_times: Optional[List[dict]] = None,
    already_run: Callable[[str], bool] = run_exists,
) -> Optional[dict]:
    """실행해야 할 회차 중 **가장 최근** 것 하나. 없으면 None.

    find_slots_to_run의 마지막 원소일 뿐이라 판단 로직이 두 벌로 갈리지 않는다.
    """
    slots = find_slots_to_run(now, schedule_times, already_run)
    return slots[-1] if slots else None


def run_due_slot(
    now: Optional[datetime] = None,
    schedule_times: Optional[List[dict]] = None,
    scrape: Callable[[str, str], dict] = collect_run_with_retry,
    already_run: Callable[[str], bool] = run_exists,
) -> Optional[str]:
    """한 번의 확인(tick)을 수행한다. 실행할 회차가 있으면 (끝 시각을 run_slot으로,
    시작 시각을 window_start로) **오래된 것부터 차례로** 수집하고 마지막으로 성공한
    회차명(끝 시각)을, 하나도 안 돌았으면 None을 반환한다.

    [수정: 2026-09-04] 놓친 회차를 1개만 채우던 것을 "오늘 놓친 것 전부"로 넓혔다 —
    근거는 find_slots_to_run 참고. 여러 회차를 한꺼번에 채워도 **자동발송은 마지막
    회차에 대해서만** 일어난다(app.confirm_send.check_pending_confirm_and_send가
    load_latest_run() 하나만 본다) — 몇 시간 지난 회차를 지금 와서 보내는 건 담당자가
    원하는 일이 아니고, 그 회차들은 확정본·정기 보관함에 남아 언제든 직접 보낼 수 있다."""
    now = now or datetime.now()
    last_done = None
    for window in find_slots_to_run(now, schedule_times, already_run):
        try:
            scrape(window["end"], window["start"])
        except Exception:
            # 회차 하나가 실패해도 나머지는 계속 간다 — 한 회차의 일시적 실패가 그날
            # 되살릴 수 있는 나머지 회차까지 통째로 막으면 안 된다. 실패한 회차는 파일이
            # 저장되지 않아 run_exists가 False로 남으므로 다음 tick에 자연히 다시 시도되고,
            # _MAX_SLOT_ATTEMPTS번 실패하면 오늘은 더 붙들지 않는다.
            count = _record_slot_failure(now, window["end"])
            if count >= _MAX_SLOT_ATTEMPTS:
                logger.exception(
                    "%s 회차 수집이 %d번 실패해 오늘은 더 시도하지 않습니다 — 이 회차는 비게 됩니다",
                    window["end"], count,
                )
            else:
                logger.exception(
                    "%s 회차 수집 실패(%d/%d) — 다음 tick에서 다시 시도합니다",
                    window["end"], count, _MAX_SLOT_ATTEMPTS,
                )
            continue
        last_done = window["end"]
    return last_done


def run_scheduler(
    poll_interval_sec: int = POLL_INTERVAL_SEC,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    schedule_times: Optional[List[dict]] = None,
    scrape: Callable[[str, str], dict] = collect_run_with_retry,
    cleanup: Callable[[], list] = delete_expired_runs,
    cleanup_overrides: Callable[[], int] = cleanup_group_overrides,
    # [추가: 2026-08-18] 기본값을 아무 일도 안 하는 람다로 둔다 — app/scheduler.py는
    # "정기" 코드라 app.adhoc를 import하지 않는다(ADHOC_DESIGN.md §4의 단방향 의존 규칙).
    # 실제 함수(app.adhoc.card.delete_expired_cards)는 main.py가 이 자리에 주입한다.
    cleanup_adhoc_cards: Callable[[], list] = lambda: [],
    render_waiting: Callable[[], None] = generate_waiting_page,
    check_confirm_send: Callable[[datetime], None] = check_pending_confirm_and_send,
    # [추가: 2026-08-20] [단독]·[속보] 알림의 "폴링" 경로 — app.breaking_alert_sender.
    # poll_and_alert_tick 자체가 감시 켜짐 여부·시간대·주기를 다 확인하고 그 외엔
    # 즉시 반환하므로(비용 0), cleanup_adhoc_cards처럼 기본값을 빈 람다로 둘 필요 없이
    # 매 tick 그대로 불러도 된다.
    check_breaking_alert: Callable[[datetime], None] = poll_and_alert_tick,
    should_continue: Callable[[], bool] = lambda: True,
) -> None:
    """앱이 켜져 있는 동안 poll_interval_sec마다 실행할 회차가 있는지 확인하고 수집한다.

    앱 시작 직후 첫 tick에서 놓친 회차를 바로 보충 실행하고, 이후 회차 시각마다 정시 실행한다.
    잠자기 모드 중에는 이 루프도 함께 멈추므로 정시 실행이 보장되지 않지만, 컴퓨터가 깨어나면
    다음 tick에서 '가장 최근 놓친 회차'를 감지해 즉시 보충 실행한다.

    [추가: 2026-07-26] 오늘 아직 끝난 회차가 하나도 없으면(current_active_slot이 None —
    자정 직후이거나 첫 회차 전) 매 tick마다 index.html을 안내 화면(🐰⏱️)으로 갱신한다.
    이게 없으면 자정을 넘겨도 어제 마지막 회차 화면이 그대로 남아있게 된다. 이 판단은
    시:분만 비교하므로(현재 시각의 날짜와 무관) 자정마다 저절로 다시 None이 되어, 날짜를
    따로 추적할 필요가 없다.

    매 tick마다 보관 기간(RETENTION_DAYS)이 지난 회차 삭제(cleanup, PRD.md 7절)도 함께 실행해 별도 배치 없이
    "자동" 삭제가 되도록 한다. 파일 수가 적어(최대 하루 MAX_SCHEDULE_TIMES개) 매번 스캔해도 비용이 작다.

    [추가: 2026-08-14] group_overrides.json(소제목 강제 배정 기록)도 같은 자리에서 정리한다
    (cleanup_overrides) — 이 파일은 URL이 계속 쌓이기만 해서 만료가 필요하다.
    [수정: 2026-08-21] 만료 기준이 "가리키는 기사가 아직 살아있는가"에서 "배정한 지
    RETENTION_DAYS가 지났는가"로 바뀌었다 — 앞의 기준은 담당자가 방금 초안에서 옮긴
    배정을 조용히 지워버렸다(app.curation.cleanup_group_overrides 참고).

    [추가: 2026-08-18] cleanup_adhoc_cards — 수시 모니터링 카드도 정기와 같은 보관 정책
    (ADHOC_RETENTION_DAYS, PRD 기능9 규칙9)으로 정리한다. app/adhoc/card.py의
    delete_expired_cards를 여기서 직접 import하지 않는 이유는 app/scheduler.py가 "정기"
    코드라서다 — app/adhoc/*는 app/*를 가져다 쓰기만 하고 그 반대는 없어야, app/adhoc/·
    data/adhoc/ 두 폴더만 지워도 정기 쪽에 흔적이 안 남는다(CLAUDE.md "수시 모니터링" 절).
    그래서 기본값은 아무 일도 안 하는 람다이고, main.py가 실제 함수를 주입한다.

    now_fn/sleep/scrape/cleanup/render_waiting/check_confirm_send/check_breaking_alert/
    should_continue는 테스트에서 주입할 수 있다.

    [추가: 2026-08-10] 매 tick마다 확정·전송 유예 시간 경과도 함께 확인한다
    (app.confirm_send.check_pending_confirm_and_send) — 담당자가 (확정)/(전송)을 직접
    누르지 않아도, 이 tick이 유예 시간이 지난 회차를 찾아 자동으로 다음 단계로 넘긴다.

    한 회차 수집이나 정리 작업이 실패해 예외가 나도 루프는 멈추지 않는다. 실패한 회차는
    파일이 저장되지 않아 run_exists가 False로 남으므로, 다음 tick(최대 poll_interval_sec
    이내)에 자연히 다시 시도된다. 이 방어가 없으면 일시적 오류 하나로 이후 모든 자동 실행이
    조용히 중단된다.
    """
    while should_continue():
        try:
            now = now_fn()
            if current_active_slot(now, schedule_times) is None:
                render_waiting()
            run_due_slot(now=now, schedule_times=schedule_times, scrape=scrape)
            check_confirm_send(now)
            check_breaking_alert(now)
            cleanup()
            cleanup_overrides()
            cleanup_adhoc_cards()
        except Exception:
            logger.exception("스크랩 회차 실행 중 오류 — 다음 회차에 다시 시도합니다")
        sleep(poll_interval_sec)
