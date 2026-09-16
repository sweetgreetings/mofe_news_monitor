# Design Ref: DESIGN.md §2 데이터 흐름 — 검색 -> 정렬 -> 필터 -> 저장을 한 회차로 이어 실행
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

import requests

from app.breaking_alert_sender import detect_and_alert
from app.classifier import classify_for_finalize, forced_group_target_names
from app.llm_classifier import llm_attempt_failed_without_fallback, round_id_for_run
from app.config import COLLECT_LOOKBACK_MIN, MAX_RETRIES, RETRY_INTERVAL_SEC
from app.curation import filter_hidden, load_group_overrides, set_group_override
from app.draft_articles import clear_draft_pending_articles
from app.draft_seen import load_draft_seen, search_condition
from app.filters import (
    deduplicate_by_title,
    exclude_personnel_articles,
    exclude_photo_articles,
    filter_by_outlet_whitelist,
    sort_scoop_first,
)
from app.credentials import naver_is_configured
from app.naver_api import NaverNotConfiguredError, kst_today_at, search_articles_by_groups
from app.preview_order import apply_preview_order, clear_preview_order, load_preview_order
from app.settings import active_schedule_times, active_search_groups, all_search_keywords, group_in_scrap, load_settings
from app.sorter import sort_by_outlet_priority
from app.storage import load_run_file, load_today_runs, save_run

logger = logging.getLogger(__name__)


def _already_published_urls(date_str: str) -> set:
    """오늘 이미 저장된 회차들에 실린 기사 URL 전부.

    [추가: 2026-09-03] 회차 하한을 COLLECT_LOOKBACK_MIN만큼 앞으로 물리면(아래
    collect_run 참고) 앞 회차 구간이 다시 검색 범위에 들어온다 — 그 구간의 기사는
    이미 앞 회차 보고서에 실렸으므로 여기서 통째로 빼야 중복 게재가 0이 된다.
    이 제외가 있어서 하한을 넉넉히 잡는 게 안전한 방향이 된다.

    **URL로만 대조한다.** 제목으로도 빼면 제목만 우연히 같은 별개 기사가 조용히
    사라진다 — 2026-08-27에 숨김 판정에서 제목 대조를 통째로 되돌린 것과 같은 이유다
    (실측: 저장된 고유 제목 2,515건 중 제목 중복 38건의 절반 이상이 별개 기사였다).
    그 대가로 통신사 쌍둥이가 회차를 건너뛰어 한 번 더 실릴 수 있는데, 그건 화면에
    **보이는** 실패라 담당자가 🗑 한 번으로 끝낼 수 있다.
    """
    urls = set()
    for run in load_today_runs(date_str):
        for a in run.get("articles", []):
            url = a.get("url")
            if url:
                urls.add(url)
    return urls


def _slot_end_for_pub_date(pub_date: str, times: list[dict]) -> str:
    """그 기사의 발행시각이 원래 속했어야 할 회차의 끝 시각("HH:MM")을 돌려준다.

    [추가: 2026-09-03] 뒤늦게 회수된 기사가 "어느 회차 기사였는지"를 화면 배너
    ("11:00 회차 기사 3건")에 쓰기 위한 값이다. 못 찾으면 빈 문자열 — 없는 값을
    지어내지 않는다(배너는 그런 기사를 회차 이름 없이 건수로만 센다).
    """
    if not pub_date:
        return ""
    hhmm = pub_date[11:16]
    if len(hhmm) != 5:
        return ""
    for w in times:
        # 창은 (start, end] — 경계 기사가 두 창에 겹쳐 들어가지 않게 하한은 제외한다.
        if w["start"] < hhmm <= w["end"]:
            return w["end"]
    return ""


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
         PRD 규칙 2 — 회차별 시간창 수집·키워드 그룹). 키워드 하나가 재시도까지
         다 쓰고도 끝내 실패하면(failed_keywords) 이 함수가 예외를 올려
         collect_run_with_retry의 전체 회차 재시도(5분 간격 최대 3회)로 넘긴다 —
         일부 키워드가 조용히 빠진 채 저장되는 것보다 낫다.
      2. outlet_order가 있으면(PRD 규칙16) 그 언론사만 남기고(filter_by_outlet_whitelist)
         그 순서로 정렬, 없으면 기본 우선순위(PRD 규칙3)로 정렬
      3. 사진기사 제외(looks_like_photo_caption — [포토] 말머리 + 표식 없는 기사 추정,
         [속보] 예외) -> 인사 발령 기사 제외([인사]) ->
         완전 동일 제목 중복 제거(정렬을 먼저 해야 중복 중 우선순위 높은 언론사 사본이
         남는다). [수정: 2026-08-11] 사진기사·인사 발령 기사 제외는 각각 설정
         (exclude_photo_in_scrap/exclude_personnel_in_scrap)을 켜둔 경우에만 적용한다 —
         중복 제거는 이 옵션들과 무관하게 항상 적용한다(PRD 규칙4, 설정으로 끌 수 없는 고정 규칙).
      4. save_run: 언론사·제목·URL·요약을 JSON 파일로 저장

    run_slot: 이 회차의 예정 끝 시각 (예: "09:00"). 헤더 "언론 모니터링 09:00 기준"에 쓰이고,
              시간창의 상한(포함)으로도 쓰인다.
    window_start: 이 회차 시간창의 시작 시각(제외) — 생략하면 설정 화면에 저장된 스케줄에서
                  run_slot과 끝 시각이 같은 회차를 찾아 그 시작 시각을 쓴다. 그래도 못 찾으면
                  (예: 스케줄에 없는 임의의 run_slot으로 직접 호출) 하한 없이 당일 전체를 모은다.
    groups: 키워드 그룹 목록([{"keywords": [...], "mode": "OR"|"AND"}, ...]). 생략하면
            설정 화면에 저장된 그룹 중 「초안·확정본」 스위치(include_in_scrap)가
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
    # [추가: 2026-08-20] 키가 아예 없으면 검색을 시도하지도 않고 바로 실패시킨다.
    # search_articles_by_groups를 그대로 불렀다면 실패가 failed_keywords를 거쳐 일반
    # ConnectionError(response 없음 -> _is_retryable가 True)로 바뀌어, 5분 간격 재시도
    # 3회(collect_run_with_retry, 최대 15분)를 키가 등록될 때까지 매번 헛돌게 된다.
    # 여기서 바로 NaverNotConfiguredError(response.status_code=401)를 올리면
    # _is_retryable가 즉시 포기로 판단해 그 낭비를 막는다 — app.naver_api.
    # NaverNotConfiguredError 참고.
    if not naver_is_configured():
        raise NaverNotConfiguredError()
    # [수정: 2026-07-26] exclude_photo_in_scrap 플래그를 읽어야 해서, groups·outlet_order·
    # window_start를 전부 명시적으로 넘긴 호출(테스트 등)이라도 설정은 항상 한 번 읽는다.
    settings = load_settings()
    if groups is None:
        groups = active_search_groups(
            settings, [g for g in settings.get("keyword_groups", []) if group_in_scrap(g)]
        )
    if outlet_order is None:
        outlet_order = settings.get("outlet_order", [])
    if window_start is None:
        window_start = next(
            (w["start"] for w in active_schedule_times(settings) if w["end"] == run_slot), None
        )
    # 저장 파일과 반환값의 run_at이 어긋나지 않도록 여기서 한 번만 확정한다.
    run_at = run_at or datetime.now().isoformat(timespec="seconds")

    # [수정: 2026-09-03] 하한을 이 회차 시작보다 COLLECT_LOOKBACK_MIN만큼 더 앞으로
    # 물린다 — 앞 회차 마감 순간 네이버 검색이 아직 안 주던 기사를 여기서 주워 담기
    # 위해서다(그렇게 안 하면 그 기사는 어느 회차에도 못 들어가고 영영 사라진다 —
    # 다음 회차의 하한이 정확히 앞 회차의 마감이라 pub_date <= after로 걸러지기 때문).
    # 당일 0시보다 더 내려가지는 않는다(첫 회차는 물릴 자리가 없다 — 전날 기사는
    # app.naver_api._search_one_keyword의 _is_today_kst가 어차피 막는다).
    window_start_dt = kst_today_at(window_start) if window_start else None
    after_dt = window_start_dt
    if window_start_dt is not None:
        after_dt = max(
            window_start_dt - timedelta(minutes=COLLECT_LOOKBACK_MIN),
            window_start_dt.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    before_dt = kst_today_at(run_slot)
    # [추가: 2026-08-21] track_keyword_matches=True — 이 회차 기사가 어떤 검색어로
    # 걸렸는지를 기사마다 matched_keywords로 받아 회차 파일에 그대로 저장한다.
    # 네이버 추가 호출은 0회다(이미 손에 든 키워드별 검색 결과에서 뽑는 값). 확정본은
    # 실시간현황과 달리 키워드별 원시 결과를 갖고 있지 않아 나중에 다시 계산할 방법이
    # 없으므로, 저장해두지 않으면 영영 못 보여준다. 저장된 값은 그 회차 수집 당시의
    # 검색어 기준이라, 나중에 검색어를 바꿔도 안 따라 바뀐다 — "이 회차가 왜 이렇게
    # 모였나"의 기록이므로 그게 맞는 동작이다.
    articles, failed_keywords = search_articles_by_groups(
        groups, after=after_dt, before=before_dt, track_keyword_matches=True
    )
    if failed_keywords:
        # [추가: 2026-08-13] app.naver_api가 키워드별 재시도까지 다 쓰고도 실패를
        # 보고하면, 그 키워드의 기사는 이 회차에서 통째로 빠진 채 저장될 수 있다 —
        # 조용히 넘기지 않고 이미 있는 전체 회차 재시도(collect_run_with_retry,
        # 5분 간격 최대 3회, PRD 규칙 8)에 그대로 태운다. RequestException을 올리면
        # response가 없어(response=None) _is_retryable가 재시도 대상으로 판단한다.
        raise requests.exceptions.ConnectionError(
            f"키워드 검색 실패(재시도 소진): {failed_keywords}"
        )
    # [추가: 2026-09-11] 이 회차 초안에 한 번이라도 보였던 기사(app.draft_seen)를 되돌려
    # 넣는다 — 마감 검색에 안 잡혔어도(네이버에서 사라짐·번호 바뀜 등) 담당자가 초안에서
    # 본 기사는 이번 회차 보고서에 들어가야 한다(사용자 결정, 2026-09-11). 초안
    # (app.preview_renderer._compute_preview_articles)과 **같은 자리**에서 합쳐, 뒤의 필터
    # (이미 실림·선택 언론사·사진/인사 제외·숨김)가 똑같이 걸린다. 검색 조건이 초안 때와
    # 다르면(검색어를 바꿈) load_draft_seen이 빈 목록을 준다. 창 밖 기사는 넣지 않는다 —
    # 회차 스케줄을 중간에 바꾼 경우에도 확정본의 창 규칙은 그대로다.
    seen = load_draft_seen((run_at[:10], run_slot), search_condition(groups))
    fresh_urls = {a["url"] for a in articles}
    kept_back = []
    for url, seen_article in seen["articles"].items():
        pub = seen_article.get("pub_date")
        if url in fresh_urls or not pub:
            continue
        try:
            pub_dt = datetime.fromisoformat(pub)
        except (TypeError, ValueError):
            continue
        if (after_dt is not None and pub_dt <= after_dt) or pub_dt > before_dt:
            continue
        kept_back.append(dict(seen_article))
    if kept_back:
        articles = articles + kept_back
        logger.info(
            "초안에 보였던 기사 %d건을 확정본에 되돌려 넣음 — 마감 검색에 안 잡힘 (회차=%s)",
            len(kept_back), run_slot,
        )

    # [추가: 2026-09-03] 넓힌 하한 때문에 다시 딸려온 "앞 회차에 이미 실린 기사"를 뺀다.
    # **모든 필터보다 먼저** 한다 — 이미 보고서에 나간 기사는 이 회차에 아예 없는 것으로
    # 쳐야 deduplicate_by_title의 대표 자리를 차지해 새 기사를 밀어내는 일이 없다.
    if window_start_dt is not None:
        published = _already_published_urls(run_at[:10])
        if published:
            articles = [a for a in articles if a["url"] not in published]
        # 남은 것 중 이 회차 창(window_start 초과)보다 앞선 기사가 곧 "뒤늦게 회수된
        # 기사"다. 화면 배너·칩이 쓰는 표식을 기사에 직접 달아 회차 파일에 그대로
        # 저장한다(별도 필드를 save_run에 추가하지 않아도 되고, 확정본이 나중에 다시
        # 계산할 필요도 없다 — matched_keywords를 저장하는 것과 같은 방식).
        window_start_iso = window_start_dt.isoformat()
        schedule_times = active_schedule_times(settings)
        late_count = 0
        for a in articles:
            pub = a.get("pub_date")
            if pub and pub <= window_start_iso:
                a["late_pickup"] = True
                a["late_pickup_slot"] = _slot_end_for_pub_date(pub, schedule_times)
                late_count += 1
        if late_count:
            # [추가: 2026-09-03] 재발 감지용 기록 — 이 문제가 1년 가까이 아무도 모르게
            # 새고 있었던 게 진짜 사고였다(HISTORY.md 같은 항목). 숫자가 남으면 다음엔
            # 하루 만에 안다.
            logger.info(
                "앞 회차 기사 회수 — %d건 (회차=%s, 하한을 %d분 물려 다시 훑음)",
                late_count, run_slot, COLLECT_LOOKBACK_MIN,
            )

    if outlet_order:
        articles = filter_by_outlet_whitelist(articles, outlet_order)
        articles = sort_by_outlet_priority(articles, priority_outlets=outlet_order)
    else:
        articles = sort_by_outlet_priority(articles)
    if settings.get("exclude_photo_in_scrap", False):
        articles = exclude_photo_articles(articles)
    if settings.get("exclude_personnel_in_scrap", False):
        articles = exclude_personnel_articles(articles)
    # [수정: 2026-09-11] prefer — 같은 제목이 여럿이면 초안에 먼저 보인 쪽이 대표를 지킨다
    # (app.draft_seen 규칙 ①). 초안과 같은 값을 넘겨야 두 화면의 대표가 같다.
    articles = deduplicate_by_title(articles, prefer=seen["seen_order"])
    # [추가: 2026-08-20] [단독] 기사를 소제목 내 최상단으로 올리기 위한 첫 단계 —
    # app.filters.sort_scoop_first 참고. apply_preview_order(담당자가 직접 옮긴
    # 순서)보다 반드시 먼저 호출해야 담당자 조작이 이 자동 정렬을 항상 이긴다.
    articles = sort_scoop_first(articles)

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

    # [추가: 2026-08-20] [단독]·[속보] 기사 알림 "바닥" 경로 — 이 회차가 어차피 검색한
    # 결과를 그대로 검사한다(추가 네이버 호출 0회). /breaking-alert 설정을 전부 꺼도
    # 이 경로만은 항상 동작한다(CLAUDE.md "[단독]·[속보] 기사 알림" 참고) — 담당자가
    # 수시 감시를 켜지 않았어도 정기 회차 안에서는 놓치지 않게 하기 위해서다. 텔레그램
    # 발송 실패가 회차 저장을 막으면 안 된다(app.telegram_bot.send_text의 기존
    # fail-open 원칙과 동일)는 이유로 예외를 여기서 삼킨다.
    try:
        detect_and_alert(articles)
    except Exception:
        logger.exception("[단독]·[속보] 알림(정기 회차 경로) 실패 — 회차 저장은 정상 진행됩니다")

    # [추가: 2026-07-30] 저장 시점의 소제목 분류 결과를 각 기사에 "group" 필드로 함께
    # 저장한다 — "지난 기사 더보기"(history.html)가 나중에 재분류 없이 당시 소제목
    # 구성을 그대로 복원할 수 있게 한다(app.history_renderer). 이 회차가 최신 회차로
    # 남아있는 동안 숨기기·이동으로 다시 바뀌면, 그때는 app.settings_server의 해당
    # 핸들러가 이 스냅샷을 다시 갱신한다.
    #
    # [수정: 2026-08-13] 숨긴 기사는 분류 입력에서 아예 뺀다 — 예전엔 숨긴 기사까지
    # 그대로 넣어 분류하는 바람에, 숨긴 기사로만 이뤄진 소제목("유령 폴더")이 확정본에
    # 떴다(2026-08-13 17시 회차 사고, HISTORY.md 참고). 숨긴 기사는 filter_hidden으로
    # 어차피 모든 화면에서 걸러지므로 분류에 넣어봐야 화면엔 안 보이고 토큰만 쓴다.
    # classify_for_finalize(app.classifier)를 써서, 초안에서 이미 분류·정리해둔 이름을
    # 그대로 물려받고(재분류로 이름이 바뀌지 않는다) 새로 들어온 기사만 기존 소제목에
    # 끼워 넣는다 — 자세한 이유는 그 함수의 docstring 참고.
    visible_articles = filter_hidden(articles)

    overrides = load_group_overrides()
    # [수정: 2026-08-26] load_custom_groups()만 넘기면, 초안의 "🤖 미분류 배정"이 그 사이
    # 새로 지어낸 소제목(아직 이 회차 LLM 캐시엔 없는 이름)으로의 override가 여기서도
    # 조용히 무시돼 마감 순간 다시 미분류로 돌아갔다 — forced_group_target_names()로
    # app.assigned_groups(오늘 배정 중 새로 생긴 이름)도 함께 넘긴다.
    custom_names = forced_group_target_names()
    # [주의] 여기 이름은 subheading_groups다 — 위쪽 검색 키워드 그룹 매개변수 groups와는
    # 전혀 다른 개념이라 같은 이름을 쓰면 헷갈린다(이 시점엔 검색 groups는 이미 다 썼다).
    # [추가: 2026-08-14] round_id_for_run — 직전 초안(round_id_for_slot(slot))과 같은 슬롯이면
    # 같은 식별자가 나오므로, 그 초안이 만들어둔 캐시를 정확히 찾아 재사용한다("직전 초안"이
    # 아닌 다른 회차의 캐시는 URL이 겹쳐도 후보에서 제외된다).
    subheading_groups, new_forced, new_subheading_names = classify_for_finalize(
        visible_articles,
        all_search_keywords(settings),
        forced_groups=overrides,
        custom_group_names=custom_names,
        round_id=round_id_for_run({"run_at": run_at, "run_slot": run_slot}),
    )
    for url, group_name in new_forced.items():
        set_group_override(url, group_name)

    # [추가: 2026-08-12] 이 회차의 첫 분류가 "정말 실패해서 규칙 기반으로 떨어졌는지"를
    # 회차 파일에 같이 남긴다 — classify_for_finalize 안에서 classify_with_llm이 방금
    # 결정한 값이라 바로 뒤에서 읽어야 한다(다음 호출 전까지만 유효한 값).
    classification_degraded = llm_attempt_failed_without_fallback()

    # [추가: 2026-08-20] new_forced의 키(URL)는 "초안 마감 시점에 캐시에 없어 방금
    # 자동으로 소제목에 배정된 기사"다 — 담당자가 초안에서 못 본 기사일 수 있으므로
    # "finalize_auto_assigned" 플래그로 기사마다 영구히 남겨, 확정본이 음영·배지로
    # 알려준다(app.renderer). 한 번 찍히면 이후 큐레이션(숨기기/이동/이름변경)이
    # snapshot_group_names로 재스냅샷해도 "**a" 스프레드로 그대로 이어진다 —
    # 일부러 지우는 로직을 두지 않는다(사용자 확인: 음영은 계속 남아야 한다).
    finalize_added_urls = set(new_forced.keys())

    group_by_url = {
        a["url"]: (g["name"], g.get("summary", "")) for g in subheading_groups for a in g["articles"]
    }
    snapshotted = []
    for a in articles:
        name, summary = group_by_url.get(a["url"], (a.get("group", "기타"), a.get("group_summary", "")))
        snapshotted.append(
            {
                **a,
                "group": name,
                "group_summary": summary,
                "finalize_auto_assigned": a["url"] in finalize_added_urls,
            }
        )
    articles = snapshotted

    path = save_run(
        run_slot,
        articles,
        run_at=run_at,
        classification_degraded=classification_degraded,
        finalize_new_subheadings=new_subheading_names,
    )
    # [수정: 2026-08-27] 방금 저장한 파일을 그대로 다시 읽어 돌려준다 — 예전엔
    # {"run_slot", "run_at", "articles"} 세 개만 손으로 지어 돌려줬는데, 이 회차를 받은
    # app.confirm_send.confirm_and_promote가 곧바로 app.storage.confirm_run을 부르고
    # 그 함수가 `{**run, "confirmed": True, ...}`로 **파일을 통째로 덮어쓴다**. 결과적으로
    # save_run이 기껏 적어둔 classification_degraded·finalize_new_subheadings·send_count가
    # 저장 직후 몇 밀리초 만에 사라졌다(실측: 저장된 81개 회차 파일 전부에 이 필드들이 없음).
    # 그 탓에 "AI 분류가 실패한 회차"라는 사실이 어디에도 안 남아,
    #   - 확정본의 "⚠️ AI 분류 실패" 배너 (app.renderer.render_page)
    #   - 분류 실패 시 자동발송 차단 (app.confirm_send.check_pending_confirm_and_send)
    #   - "마감 후 자동 배정" 배지 (finalize_new_subheadings)
    # 셋 다 도입 이후 한 번도 동작한 적이 없다(2026-08-27 06:00 회차가 규칙 기반 폴백
    # 상태로 조용히 자동발송된 사고 — HISTORY.md 참고).
    #
    # 필드를 손으로 나열해 채우지 않고 파일을 되읽는 이유: 그러면 save_run에 필드가
    # 하나 더 늘 때마다 여기도 같이 고쳐야 하고, 안 고치면 똑같은 사고가 조용히 재발한다.
    # 되읽으면 "collect_run이 돌려주는 값 = 디스크에 저장된 값"이 구조적으로 보장된다.
    # 회차당 파일 1개(하루 6번)라 비용은 무시할 수준이다.
    stored = load_run_file(path)
    if stored is not None:
        return stored
    # 방금 쓴 파일을 못 읽는 건 사실상 없는 일이지만, 그 경우에도 회차 수집 자체를
    # 실패시키지는 않는다 — 예전과 같은 최소 형태로 돌려준다(화면은 뜨고, 위 세 기능만
    # 예전처럼 동작하지 않는다).
    logger.warning("방금 저장한 회차 파일을 다시 읽지 못했습니다 (%s) — 최소 정보만 돌려줍니다", path)
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
