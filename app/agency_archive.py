# 「🗄️ 기관 기사 찾기」 — 저장 부분 (화면은 아직 없다).
#
# 왜 따로 쌓나: 정기 회차는 하루를 다 덮지 않는다 — 평일 회차가 00:00~17:00을 연속으로
# 덮고 그 뒤 7시간이 빈다(주말은 16:00 이후 8시간). 다음날 00:00~06:00 회차가 그걸 줍지도
# 않는다(app.naver_api._is_today_kst가 당일 기사만 통과시킨다). 「오늘 전체 기사」는 그
# 시간 기사를 화면엔 보여 주지만 캐시(data/live_cache.json)가 자정에 통째로 버려지고,
# 그나마 담당자가 화면을 열었을 때만 채워진다. 그래서 나중에 다시 찾을 일이 있는 기관
# 기사가 매일 밤 사라진다.
#
# 이건 **책이다, 보고서가 아니다.** 그 성질이 곧 규칙이다:
#   - 읽기 전용 — 숨김·소제목·↑↓가 없다. 되돌리기 스냅샷(app.undo)에도, 회차 끊기의
#     copy_round_records(app.today_cuts)에도 들어가지 않는다. 되돌릴 것도 없고 회차
#     이름으로 저장하지도 않는다.
#   - 자기 완결 — 하루 전체를 제 손으로 모은다. **회차 파일과 합집합을 계산하지 않는다** —
#     회차 파일은 제목 중복 제거(app.curation.deduplicate_by_title)를 거쳐서, 합치면 한
#     화면에서 오전은 대표 기사만·저녁은 전 언론사가 나오고 그 이유를 화면이 설명할 수 없다.
#     그래서 여기선 URL만 중복 제거하고 제목 중복은 그대로 둔다.
#   - 집계에 안 들어간다 — 정책 단어 추이·오늘의 주요 주제의 모수는 회차 파일
#     그대로다. 여기 기사를 섞으면 도입일에 그래프가 계단처럼 뛴다.
#   - 나이로 지우지 않는다(실측 하루 ~300건·170KB, 연 60MB).
#
# 알림 폴링(app.breaking_alert_sender.poll_and_alert_tick)이 같은 키워드를 이미 검색하고
# 있지만 **그 결과를 얻어 쓰지 않는다** — 아끼는 건 하루 240 요청뿐인데, 얹으면 담당자가
# [단독]·[속보] 감시를 끌 때 책이 조용히 멈춘다(CODING_CONVENTIONS §3).
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from app import store
from app.config import (
    AGENCY_ARCHIVE_DIR,
    AGENCY_ARCHIVE_INTERVAL_MIN,
    AGENCY_ARCHIVE_KEYWORDS,
)
from app.naver_api import incremental_search_after, kst_today_at, search_keywords
from app.sorter import sort_by_pub_desc

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# 자정 직전 마감 수집 — 주기가 30분이라 그날 마지막 tick이 23:35쯤에 끝나면 그 뒤 기사를
# 영영 못 줍는다(다음날엔 네이버가 어제 기사를 안 준다). 이 시각을 지났는데 아직 그 뒤로
# 수집한 적이 없으면 주기와 무관하게 한 번 더 돈다.
_LAST_CALL_HHMM = "23:50"


def _archive_dir() -> Path:
    """AGENCY_ARCHIVE_DIR을 호출 시점에 읽는다 — 테스트가 이 모듈의 이름을 임시 폴더로
    바꿔치기하므로(patch.object) 상수를 미리 붙들면 실제 data/를 건드린다
    (app.storage._articles_dir_for_cleanup과 같은 이유)."""
    return AGENCY_ARCHIVE_DIR


def _path(date_str: str) -> Path:
    return _archive_dir() / f"{date_str}.json"


def _empty(date_str: str) -> dict:
    return {"date": date_str, "articles": [], "last_seen": None, "collected_at": None}


def load_day(date_str: str) -> dict:
    """그 날짜의 원장. 없거나 깨졌으면 빈 하루를 돌려준다 — 읽기만 하는 곳도 쓴다."""
    try:
        data = store.read_json(_path(date_str))
    except (FileNotFoundError, ValueError, OSError):
        return _empty(date_str)
    if not isinstance(data, dict):
        return _empty(date_str)
    base = _empty(date_str)
    base.update(data)
    if not isinstance(base.get("articles"), list):
        base["articles"] = []
    return base


def available_dates() -> list:
    """원장에 쌓인 날짜를 최신순으로. 파일을 열지 않고 이름만 본다(화면의 기간 조회용)."""
    directory = _archive_dir()
    if not directory.exists():
        return []
    dates = []
    for path in directory.glob("*.json"):
        name = path.stem
        try:
            datetime.strptime(name, "%Y-%m-%d")
        except ValueError:
            continue  # 임시 파일 등은 건너뛴다
        dates.append(name)
    return sorted(dates, reverse=True)


def _save_day(day: dict) -> None:
    directory = _archive_dir()
    directory.mkdir(parents=True, exist_ok=True)
    store.write_json(_path(day["date"]), day, indent=2)


def _merge(existing: list, found: list) -> tuple[list, int]:
    """URL로만 중복을 제거해 합치고 발행 최신순으로 정렬한다.

    **제목 중복은 제거하지 않는다** — 원장은 "그 시각에 무엇이 나와 있었나"의 기록이라
    같은 제목의 다른 언론사 판본도 그대로 남는다(위 모듈 주석의 「자기 완결」).
    먼저 들어온 기사를 남긴다 — 나중 판본이 제목을 고쳐 보내도 처음 본 모양을 지킨다.
    """
    by_url = {}
    for article in existing:
        url = article.get("url")
        if url:
            by_url[url] = article
    added = 0
    for article in found:
        url = article.get("url")
        if url and url not in by_url:
            by_url[url] = article
            added += 1
    return sort_by_pub_desc(list(by_url.values())), added


def _parse(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _is_due(day: dict, now: datetime) -> bool:
    """이번 tick에 수집할 차례인가 — 주기가 지났거나, 자정 직전 마감 수집이 아직이면 참."""
    collected_at = _parse(day.get("collected_at"))
    if collected_at is None:
        return True
    elapsed_min = (now - collected_at).total_seconds() / 60
    if elapsed_min >= AGENCY_ARCHIVE_INTERVAL_MIN:
        return True
    # 자정 직전 마감 수집: 지금이 23:50을 지났는데 마지막 수집이 그 전이면 한 번 더.
    return now.strftime("%H:%M") >= _LAST_CALL_HHMM > collected_at.strftime("%H:%M")


def collect_tick(now: Optional[datetime] = None) -> int:
    """스케줄러 tick마다 불린다. 차례가 아니면 즉시 돌아온다(비용 0).

    검색 하한은 「지난번에 본 가장 최신 발행시각」이되, 네이버가 발행순으로 색인하지 않아
    app.naver_api.incremental_search_after로 최근 한 시간을 늘 다시 훑는다(되받은 기사는
    URL로 걸러진다). 바닥은 당일 0시다 — 어제 기사는 네이버가 주지 않는다.

    **검색이 하나라도 실패하면 last_seen을 올리지 않는다** — 받아온 기사는 그대로 합치되
    다음 tick이 같은 구간을 다시 훑게 둔다. 실패를 "그 구간엔 기사가 없었다"로 굳히면
    그 기사들은 영영 안 들어온다(app.live_cache의 stored_last_seen과 같은 원칙).

    돌려주는 값은 이번에 새로 들어온 기사 수(테스트·로그용).
    """
    now = now or datetime.now()
    # now의 날짜가 진짜 오늘이 아니면 아무것도 안 한다. 네이버는 당일 기사만 주므로
    # (app.naver_api._is_today_kst) 지금 받아오는 건 언제나 "오늘" 것인데, 그걸 다른
    # 날짜 파일에 적으면 원장에 거짓 날짜가 남는다. 스케줄러 테스트가 가짜 시각을
    # 넣어 돌 때 실제로 그런 파일이 생겼다.
    if now.strftime("%Y-%m-%d") != datetime.now().strftime("%Y-%m-%d"):
        return 0
    keywords = [k for k in AGENCY_ARCHIVE_KEYWORDS if k]
    if not keywords:
        return 0

    with _lock:
        date_str = now.strftime("%Y-%m-%d")
        day = load_day(date_str)
        if not _is_due(day, now):
            return 0

        midnight = kst_today_at("00:00")
        last_seen = _parse(day.get("last_seen")) or midnight
        after = incremental_search_after(last_seen, midnight)

        try:
            results, failed, _capped = search_keywords(keywords, after=after)
        except Exception:
            # 앱은 멈추지 않는다 — 다음 tick에 그대로 다시 시도한다(수집 기록도 안 남긴다).
            logger.exception("기관 기사 원장 수집 실패 — 다음 tick에 다시 시도합니다")
            return 0

        found = [article for articles in results.values() for article in articles]
        merged, added = _merge(day["articles"], found)

        day["articles"] = merged
        day["collected_at"] = now.isoformat(timespec="seconds")
        if not failed:
            newest = max(
                (p for p in (_parse(a.get("pub_date")) for a in found) if p),
                default=None,
            )
            if newest is not None:
                day["last_seen"] = newest.isoformat()
        _save_day(day)

    if added:
        logger.info(
            "기관 기사 원장 — %s에 %d건 추가(누적 %d건, 검색어 %d개%s)",
            date_str,
            added,
            len(merged),
            len(keywords),
            f", 실패 {len(failed)}개" if failed else "",
        )
    return added
