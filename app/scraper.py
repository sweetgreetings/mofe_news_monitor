# Design Ref: DESIGN.md §2 데이터 흐름 — 검색 -> 정렬 -> 필터 -> 저장을 한 회차로 이어 실행
import time
from datetime import datetime
from typing import Callable, Optional

import requests

from app.classifier import snapshot_group_names
from app.config import MAX_RETRIES, RETRY_INTERVAL_SEC
from app.curation import load_group_overrides
from app.custom_groups import load_custom_groups
from app.draft_articles import clear_draft_pending_articles
from app.filters import (
    deduplicate_by_title,
    exclude_personnel_articles,
    exclude_photo_articles,
    filter_by_outlet_whitelist,
)
from app.naver_api import kst_today_at, search_articles_by_groups
from app.preview_order import apply_preview_order, clear_preview_order, load_preview_order
from app.settings import active_schedule_times, active_search_groups, all_search_keywords, load_settings
from app.sorter import sort_by_outlet_priority
from app.storage import save_run


def collect_run(
    run_slot: str,
    window_start: Optional[str] = None,
    groups: Optional[list[dict]] = None,
    outlet_order: Optional[list[str]] = None,
    run_at: Optional[str] = None,
) -> dict:
    """
    한 회차의 기사를 수집해 로컬 JSON 파일로 저장하고, 저장된 회차 데이터를 반환한다.

    처리 순서 (DESIGN.md §2):
      1. search_articles_by_groups: 설정된 키워드 그룹으로, 이 회차의 시간창
         (window_start~run_slot)에 게시된 기사만 검색 (그룹간 OR·그룹내 OR/AND,
         PRD 규칙 2 — 회차별 시간창 수집·키워드 그룹)
      2. outlet_order가 있으면(PRD 규칙16) 그 언론사만 남기고(filter_by_outlet_whitelist)
         그 순서로 정렬, 없으면 기본 우선순위(PRD 규칙3)로 정렬
      3. 사진기사 제외(PHOTO_HINT_WORDS, [속보] 예외) -> 인사 발령 기사 제외([인사]) ->
         완전 동일 제목 중복 제거(정렬을 먼저 해야 중복 중 우선순위 높은 언론사 사본이
         남는다). [수정: 2026-07-26] 사진기사·인사 발령 기사 제외는 각각 설정
         (include_photo_in_scrap/include_personnel_in_scrap)이 켜져 있으면 건너뛴다 —
         중복 제거는 이 옵션들과 무관하게 항상 적용한다(PRD 규칙4, 설정으로 끌 수 없는 고정 규칙).
      4. save_run: 언론사·제목·URL·요약을 JSON 파일로 저장

    run_slot: 이 회차의 예정 끝 시각 (예: "09:00"). 헤더 "언론 모니터링 09:00 기준"에 쓰이고,
              시간창의 상한(포함)으로도 쓰인다.
    window_start: 이 회차 시간창의 시작 시각(제외) — 생략하면 설정 화면에 저장된 스케줄에서
                  run_slot과 끝 시각이 같은 회차를 찾아 그 시작 시각을 쓴다. 그래도 못 찾으면
                  (예: 스케줄에 없는 임의의 run_slot으로 직접 호출) 하한 없이 당일 전체를 모은다.
    groups: 키워드 그룹 목록([{"keywords": [...], "mode": "OR"|"AND"}, ...]). 생략하면
            설정 화면에 저장된 그룹 중 "정기 스크랩에도 포함"(include_in_scrap)이
            켜진 그룹만, 그마저도 체크 해제(꺼짐)된 키워드는 뺀 채로 쓴다
            (app.settings.active_search_groups) — [수정: 2026-07-25] 실시간 기사
            현황은 등록된 그룹 전체를 보여주지만, 예정된 회차 스크랩은 이 플래그가
            켜진 그룹으로만 좁힌다. 나머지 그룹(주제어 그룹 등)에서 나온 기사를 이
            회차에 포함하려면, 실시간 기사 현황에서 "→ 스크랩"으로 직접 옮겨야
            한다(app.manual_articles).
    outlet_order: 선택된 언론사(화이트리스트) 및 그 순서. 생략하면 저장된 설정값을 쓰며,
                  빈 리스트면 화이트리스트 없이 기본 우선순위 동작을 그대로 따른다.
    run_at:   실제 실행 시각. 놓친 회차 보충 실행 시 run_slot과 달라질 수 있다.
    """
    # [수정: 2026-07-26] include_photo_in_scrap 플래그를 읽어야 해서, groups·outlet_order·
    # window_start를 전부 명시적으로 넘긴 호출(테스트 등)이라도 설정은 항상 한 번 읽는다.
    settings = load_settings()
    if groups is None:
        groups = active_search_groups(
            settings, [g for g in settings.get("keyword_groups", []) if g.get("include_in_scrap")]
        )
    if outlet_order is None:
        outlet_order = settings.get("outlet_order", [])
    if window_start is None:
        window_start = next(
            (w["start"] for w in active_schedule_times(settings) if w["end"] == run_slot), None
        )
    # 저장 파일과 반환값의 run_at이 어긋나지 않도록 여기서 한 번만 확정한다.
    run_at = run_at or datetime.now().isoformat(timespec="seconds")

    after_dt = kst_today_at(window_start) if window_start else None
    before_dt = kst_today_at(run_slot)
    articles = search_articles_by_groups(groups, after=after_dt, before=before_dt)
    if outlet_order:
        articles = filter_by_outlet_whitelist(articles, outlet_order)
        articles = sort_by_outlet_priority(articles, priority_outlets=outlet_order)
    else:
        articles = sort_by_outlet_priority(articles)
    if not settings.get("include_photo_in_scrap", False):
        articles = exclude_photo_articles(articles)
    if not settings.get("include_personnel_in_scrap", False):
        articles = exclude_personnel_articles(articles)
    articles = deduplicate_by_title(articles)

    # [추가: 2026-07-27] "스크랩 초안"에서 "위로"(예약) 눌러둔 기사를 이번 회차에 합친다
    # (app.draft_articles) — 검색 결과에 자연스럽게 안 걸린 기사를 미리 초안 화면에서
    # 봐두고 예약해둔 것이므로, 다른 필터를 다시 거치지 않고 그대로 추가한다(URL
    # 중복만 피한다). 한 번 합쳐지면 예약 목록은 비워진다.
    pending = clear_draft_pending_articles()
    if pending:
        existing_urls = {a["url"] for a in articles}
        articles = articles + [a for a in pending if a["url"] not in existing_urls]

    # [추가: 2026-07-30] 초안(preview.html)에서 ↑/↓로 같은 소제목 안에 정리해둔 순서가
    # 있으면 정식 회차에도 그대로 이어받는다(app.preview_order) — 안 그러면 정식 회차로
    # 넘어가는 순간 애써 정리한 순서가 다시 언론사 우선순위로 리셋돼버린다. 한 번
    # 반영했으면 이 회차의 "진행 중" 상태는 끝난 것이므로 저장소를 비워, 다음 회차가
    # 지난 회차의 순서를 잘못 이어받지 않게 한다.
    articles = apply_preview_order(articles, load_preview_order())
    clear_preview_order()

    # [추가: 2026-07-30] 저장 시점의 소제목 분류 결과를 각 기사에 "group" 필드로 함께
    # 저장한다 — "지난 기사 더보기"(history.html)가 나중에 재분류 없이 당시 소제목
    # 구성을 그대로 복원할 수 있게 한다(app.history_renderer). 이 회차가 최신 회차로
    # 남아있는 동안 숨기기·이동으로 다시 바뀌면, 그때는 app.settings_server의 해당
    # 핸들러가 이 스냅샷을 다시 갱신한다.
    articles = snapshot_group_names(
        articles, all_search_keywords(settings), load_group_overrides(), load_custom_groups()
    )

    save_run(run_slot, articles, run_at=run_at)
    return {"run_slot": run_slot, "run_at": run_at, "articles": articles}


def collect_run_with_retry(
    run_slot: str,
    window_start: Optional[str] = None,
    groups: Optional[list[dict]] = None,
    outlet_order: Optional[list[str]] = None,
    max_retries: int = MAX_RETRIES,
    retry_interval_sec: int = RETRY_INTERVAL_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """
    collect_run을 감싸, 네트워크 오류로 스크랩이 실패하면 자동 재시도한다 (PRD.md 기능1 규칙 8).

    run_slot·window_start는 순서대로 위치 인자로 받는다 — app.scheduler가 매 tick마다
    scrape(run_slot, window_start) 형태로 그대로 호출하기 때문이다 (PRD 규칙 2).

    최초 1회 시도 후 실패하면 retry_interval_sec(기본 300초=5분) 간격으로 최대
    max_retries회(기본 3회) 재시도한다. 즉 최대 (1 + max_retries)회까지 시도한다.
    재시도 사이 대기는 sleep 함수로 주입할 수 있어 테스트에서 실제 5분을 기다리지 않아도 된다.

    재시도 대상은 네트워크·API 오류(requests.exceptions.RequestException)뿐이다.
    수집 결과가 0건인 것은 실패가 아니라 정상(화면에 "💤")이므로 재시도하지 않는다.
    max_retries회를 모두 소진해도 실패하면 마지막 예외를 그대로 올린다.

    run_at은 각 시도 시점의 실제 시각으로 기록되므로(collect_run이 매번 확정), 재시도로
    성공한 회차의 run_at은 실제 성공 시각을 가리킨다. run_slot은 예정 시각으로 고정된다.

    단, 4xx 클라이언트 오류(잘못된 API 키 401, 잘못된 요청 400 등)는 재시도해도 결과가
    바뀌지 않으므로 즉시 예외를 올린다. 일시적 오류(연결 실패·타임아웃·5xx 서버 오류·429
    rate limit)만 재시도한다.
    """
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return collect_run(run_slot, window_start=window_start, groups=groups, outlet_order=outlet_order)
        except requests.exceptions.RequestException as error:
            if not _is_retryable(error):
                raise
            last_error = error
            if attempt < max_retries:
                sleep(retry_interval_sec)
    raise last_error


def _is_retryable(error: requests.exceptions.RequestException) -> bool:
    """일시적 오류(재시도할 가치가 있는 오류)인지 판단한다.

    4xx 클라이언트 오류는 재시도해도 소용없으므로 False (단 429 rate limit은 예외적으로 재시도).
    연결 실패·타임아웃 등 응답 자체가 없는 오류나 5xx 서버 오류는 True.
    """
    response = getattr(error, "response", None)
    if response is None:
        return True  # 연결 실패·타임아웃 등 — 재시도 대상
    if response.status_code == 429:
        return True  # rate limit — 잠시 후 재시도하면 풀릴 수 있음
    return not (400 <= response.status_code < 500)  # 그 외 4xx는 재시도 안 함
