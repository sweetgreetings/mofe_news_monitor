# Design Ref: 사용자 요청(2026-08-20) — [단독]·[속보] 알림 폴링이 네이버 뉴스 검색 API
# 일일 호출 한도(NAVER_DAILY_CALL_LIMIT)를 넘기지 않도록, 실제 호출 수를 세어 감시한다.
#
# 카운터를 둔다는 것 자체가 "짐작하지 말고 잰다"(CODING_CONVENTIONS.md §1) 원칙의
# 적용이다 — 알림 설정 화면이 보여주는 예상 호출량(키워드 수 × 폴링 횟수)은 어디까지나
# 그 폴링만의 추정치이고, 실제 한도는 정기 스크랩·실시간 현황·수시 모니터링 호출까지
# 전부 같이 나눠 쓴다. 추정이 아니라 실측으로 멈출지 판단하려면 실제 호출 지점
# (app.naver_api._search_one_keyword)에서 세는 수밖에 없다.
import json
import threading
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import API_USAGE_FILE, API_USAGE_WARN_RATIO, NAVER_DAILY_CALL_LIMIT

# [추가: 2026-08-20] 네이버 검색 요청은 최대 8개 스레드가 동시에 날린다
# (app.naver_api._GLOBAL_REQUEST_SEM) — 파일 하나에 여러 스레드가 동시에 "읽고 +1 해서
# 쓰기"를 하면 갱신이 서로를 덮어써 실제보다 적게 세어질 수 있다. 프로세스 전체가
# 공유하는 락 하나로 read-modify-write를 감싼다(app.curation의 hide/unhide 직렬화와 같은 이유).
_LOCK = threading.Lock()


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _load(now: Optional[datetime] = None) -> dict:
    """오늘 날짜가 아니면(자정이 지났으면) 0부터 새로 센다 — app.live_cache와 같은
    "date 필드 불일치 = 리셋" 패턴, 별도의 자정 정리 작업이 필요 없다."""
    if not API_USAGE_FILE.exists():
        return {"date": _today_str(now), "count": 0}
    try:
        data = json.loads(API_USAGE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {"date": _today_str(now), "count": 0}
    if data.get("date") != _today_str(now):
        return {"date": _today_str(now), "count": 0}
    return {"date": data.get("date"), "count": int(data.get("count", 0) or 0)}


def record_api_call(now: Optional[datetime] = None) -> None:
    """네이버 뉴스 검색 API 요청 하나(페이지네이션의 각 페이지)를 셀 때마다 호출한다
    (app.naver_api._search_one_keyword의 유일한 호출 지점). 429 등으로 실패한 요청도
    실제로 네이버에 도달한 호출이므로 그대로 센다."""
    with _LOCK:
        data = _load(now)
        data["count"] += 1
        atomic_write_text(API_USAGE_FILE, json.dumps(data, ensure_ascii=False))


def today_call_count(now: Optional[datetime] = None) -> int:
    return _load(now)["count"]


def usage_ratio(now: Optional[datetime] = None) -> float:
    return today_call_count(now) / NAVER_DAILY_CALL_LIMIT


def should_pause_polling(now: Optional[datetime] = None) -> bool:
    """[단독]·[속보] 알림의 "폴링"만 멈출지 판단한다(app.breaking_alert_sender.
    poll_and_alert_tick) — 정기 스크랩·실시간 현황·수시 모니터링은 이 값을 보지 않는다
    (본업은 절대 안 막는다, 사용자 결정)."""
    return usage_ratio(now) >= API_USAGE_WARN_RATIO
