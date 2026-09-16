# Design Ref: ADHOC_DESIGN.md §6.1(검색)·§6.2(시간창 검증)·§6.3(자정 잠금)·
# §6.4(재수집은 교체)·§6.4a(검색어 편집)·§6.4b(1000건 상한)·§6.4c(키워드별 캐시)·
# §6.9(수집 실패 처리)·§6.10(API 동시 호출)
#
# 이 파일이 하는 일은 "카드 하나를 실제로 채운다"뿐이다 — 검색은 정기와 같은 계층
# (app.naver_api.search_keywords)을 그대로 쓰고, 저장은 app.adhoc.card에
# 맡긴다. 정기의 전역 큐레이션 파일(hidden_articles.json 등)은 여기서도 건드리지 않는다.
import threading
from datetime import datetime
from typing import Optional

from app.adhoc.card import AdhocCardError, append_collect_log, card_lock, load_card, save_card
from app.filters import deduplicate_by_title, exclude_personnel_articles, exclude_photo_articles, filter_by_outlet_whitelist
from app.naver_api import _MAX_START, kst_today_at, search_keywords
from app.settings import load_settings
from app.sorter import sort_by_outlet_priority


class AdhocCollectError(Exception):
    """수집을 실행할 수 없거나 실행 중 실패했을 때 — 카드 스키마 자체는 멀쩡하므로
    AdhocCardError와 구분한다. 화면은 이 메시지를 그대로 보여주고 [다시 시도]를 제공하면
    된다(ADHOC_DESIGN.md §6.9) — 자동 재시도는 여기서 하지 않는다."""


class AdhocKeywordCapError(AdhocCollectError):
    """꼭 포함할 검색어가 네이버 조회 상한(1,000건)에 걸려 교집합을 정확히 낼 수 없을 때
    (ADHOC_DESIGN.md §6.4b). 이때는 적용을 **거부**한다 — AND는 URL 교집합이라
    (app.naver_api._match_group) 상한에 걸리면 "남아야 할 기사가 잘못 빠지는" 방향으로
    틀리기 때문이다. OR의 잘림("덜 가져옴")과 손해 방향이 정반대다."""


# --- 키워드별 검색 캐시 (ADHOC_DESIGN.md §6.4c) ---------------------------------
# 교체 모델이라 조건을 바꿀 때마다 재검색이 필요한데, 매번 전부 돌리면 조건 하나 고칠
# 때마다 몇 초씩 기다리게 된다. 같은 (날짜, 키워드, 시간창)이면 결과가 같으므로 캐시한다
# — app/live_cache.py가 실시간현황에서 쓰는 것과 같은 발상이다.
#
# **디스크가 아니라 메모리에 둔다.** 키워드 하나의 원시 결과가 최대 1,000건(약 300KB)이라
# 카드마다 파일로 남기면 보관 기간 1년 × 카드 수만큼 쌓인다. 반면 이 캐시가 쓸모 있는
# 구간은 "담당자가 지금 이 카드를 만지는 동안"뿐이다(카드는 자정 잠금이라 어차피 당일에만
# 수집된다) — 앱을 재시작하면 한 번 더 검색할 뿐, 틀린 결과가 나오지는 않는다.
_search_cache: dict[tuple, tuple[list[dict], bool]] = {}
_search_cache_lock = threading.Lock()
_SEARCH_CACHE_MAX = 40  # 키워드 5 × 카드 8개 정도. 넘으면 오래된 것부터 버린다.


def _cache_key(collect_date: str, keyword: str, window: dict) -> tuple:
    return (collect_date, keyword, window["start"], window["end"])


def clear_search_cache() -> None:
    """테스트·수동 초기화용. 평상시에는 부를 일이 없다(캐시가 틀릴 수 있는 경로가 없다 —
    키에 날짜·시간창이 다 들어 있어서 조건이 바뀌면 키 자체가 달라진다)."""
    with _search_cache_lock:
        _search_cache.clear()


def _search_with_cache(
    collect_date: str, keywords: list[str], window: dict, after, before
) -> tuple[dict[str, list[dict]], list[str], set[str]]:
    """키워드별 검색 결과를 돌려준다 — 캐시에 있는 키워드는 네이버를 부르지 않는다.

    반환값은 `({키워드: 결과}, 실패한 키워드, 조회 상한에 걸렸던 키워드 집합)` —
    앞 두 개는 app.naver_api.search_keywords와 같은 모양이라 호출부가 캐시의 존재를
    몰라도 된다.

    [수정: 2026-08-24] 세 번째 값(capped)을 새로 추가했다 — 캐시가 원래 `{키워드: 결과
    리스트}`만 저장해 "이 결과가 상한에 걸려 잘렸는지"를 기억하지 못했다. 그래서 같은
    키워드를 캐시 히트로 다시 받는 요청(예: 검색어를 뺐다가 그대로 다시 넣는 경우)에서는
    상한 정보가 사라져, §6.4b 거부 판정이 "이번에 네이버를 실제로 불렀는지"에 따라
    달라지는 버그가 될 뻔했다. 이제 캐시 값 자체를 (결과, capped) 튜플로 저장해
    캐시 히트에서도 판정이 새로 조회했을 때와 똑같이 나오게 했다.
    """
    cached: dict[str, list[dict]] = {}
    cached_capped: set[str] = set()
    missing: list[str] = []
    with _search_cache_lock:
        for keyword in keywords:
            hit = _search_cache.get(_cache_key(collect_date, keyword, window))
            if hit is None:
                missing.append(keyword)
            else:
                results, capped = hit
                cached[keyword] = results
                if capped:
                    cached_capped.add(keyword)

    if not missing:
        return cached, [], cached_capped

    fresh, failed, fresh_capped = search_keywords(missing, after=after, before=before)
    with _search_cache_lock:
        for keyword, results in fresh.items():
            if keyword in failed:
                continue  # 실패한 키워드는 캐시하지 않는다 — 다음에 다시 시도해야 한다
            _search_cache[_cache_key(collect_date, keyword, window)] = (results, keyword in fresh_capped)
        while len(_search_cache) > _SEARCH_CACHE_MAX:
            _search_cache.pop(next(iter(_search_cache)))
    cached.update(fresh)
    capped = cached_capped | set(fresh_capped)
    return cached, failed, capped


def josa_eun(word: str) -> str:
    """받침 유무로 은/는을 고른다. 검색어는 담당자 자유 입력이라 문구에 조사를 고정으로
    박으면 '정부은'처럼 반드시 틀린다(한글이 아니면 무난한 '는')."""
    if not word:
        return "는"
    code = ord(word[-1])
    if not (0xAC00 <= code <= 0xD7A3):
        return "는"
    return "은" if (code - 0xAC00) % 28 else "는"


def _validate_collectable(card: dict, now: datetime) -> None:
    """지금 이 카드를 수집해도 되는지 확인한다. 실패하면 AdhocCollectError.

    두 검사 모두 카드 내용(스키마)이 아니라 "지금 몇 시인가"에 달려 있어 app.adhoc.card의
    구조 검증과는 성격이 달라 여기(collector)에 둔다.
    """
    today = now.strftime("%Y-%m-%d")
    if card["collect_date"] != today:
        raise AdhocCollectError(
            f"이 카드는 {card['collect_date']} 기준으로 수집한 카드입니다 (오늘은 {today}). "
            "시간대를 그대로 다시 수집하면 오늘 날짜로 검색됩니다 — 같은 사안 아래 "
            "오늘 회차를 새로 만들어주세요."  # ADHOC_DESIGN.md §6.3 — 기준일 변경 = 새 카드
        )
    window_end = card["window"]["end"]
    now_hhmm = now.strftime("%H:%M")
    if window_end > now_hhmm:
        raise AdhocCollectError(
            f"아직 {window_end}이 안 됐어요 (지금 {now_hhmm}). {now_hhmm}까지만 수집할 수 있어요."
        )


def article_out_of_window(article: dict, window: dict) -> bool:
    """이 기사가 카드의 현재 시간창 밖인지 계산한다(저장하지 않고 매번 다시 계산 —
    시간창이 재수집으로 바뀌면 그 즉시 다시 맞아떨어져야 하므로, live_renderer의
    matched_keywords와 같은 이유로 값을 고정 저장하지 않는다).

    담아둠에서 담당자가 직접 넣은 기사(added_by == "manual")는 검사 대상에서 뺀다 —
    의도적으로 넣은 것이므로 "구간 밖" 경고가 뜨면 오히려 방해된다(ADHOC_DESIGN.md §6.4).
    """
    if article.get("added_by") == "manual":
        return False
    # 모음 카드는 시간창 자체가 없다(§6.13, window=None) — 위 manual 면제로 이미 다
    # 걸러지지만, 판정 함수가 인자만 보고도 안전하도록 한 겹 더 둔다.
    if not window or not window.get("start") or not window.get("end"):
        return False
    pub_hhmm = article["pub_date"][11:16]  # ISO 8601 "...THH:MM:SS+09:00"에서 시:분만
    return not (window["start"] < pub_hhmm <= window["end"])


def _apply_options(articles: list[dict], options: dict) -> list[dict]:
    """app/scraper.py collect_run과 동일한 순서(화이트리스트→정렬→사진/인사 제외→중복
    제거)를 그대로 따른다 — 정기와 다른 파이프라인처럼 보이면 담당자가 결과를 신뢰하기
    어려워진다. 동일 제목 중복 제거만 옵션과 무관하게 항상 적용(ADHOC_DESIGN.md §6.1)."""
    settings = load_settings()
    outlet_order = settings.get("outlet_order", []) if options.get("use_outlet_whitelist") else []
    if outlet_order:
        articles = filter_by_outlet_whitelist(articles, outlet_order)
        articles = sort_by_outlet_priority(articles, priority_outlets=outlet_order)
    else:
        articles = sort_by_outlet_priority(articles)
    if options.get("exclude_photo"):
        articles = exclude_photo_articles(articles)
    if options.get("exclude_personnel"):
        articles = exclude_personnel_articles(articles)
    return deduplicate_by_title(articles)


def article_in_condition(article: dict, card: dict) -> bool:
    """이 기사가 **지금 카드의 조건**으로 불러올 수 있는 기사인지 판정한다
    (ADHOC_DESIGN.md §6.4 — 카드는 조건의 스냅샷이 아니라 조건 그 자체다).

    저장하지 않고 매번 다시 계산한다 — article_out_of_window와 같은 이유로, 조건을
    고치는 그 즉시 화면이 맞아떨어져야 하기 때문이다. 판정 근거인 matched_keywords는
    run_collect가 매 수집마다 **현재 검색어 기준으로 다시 써준다**(아래 참고).

    - 검색어(OR): 하나라도 걸리면 통과
    - 꼭 포함할 검색어(AND): 전부 걸려야 통과
    """
    # [추가: 2026-09-02] 담당자가 직접 넣은 기사는 조건 판정에서 면제한다
    # (ADHOC_DESIGN.md §6.13 규칙 3). 모음 카드는 keywords가 비어 있어, 이 줄이 없으면
    # 보내는 즉시 전부 "검색어에 안 걸림"으로 조건 밖에 떨어져 기능이 통째로 안 돈다.
    # article_out_of_window가 같은 값을 이미 면제하고 있던 것과 짝을 맞춘 것이기도 하다.
    if article.get("added_by") == "manual":
        return True
    matched = article.get("matched_keywords", [])
    if not any(k in matched for k in card["keywords"]):
        return False
    return all(k in matched for k in card.get("must_keywords", []))


def articles_in_condition(card: dict) -> list[dict]:
    """지금 조건으로 불러올 수 있는 기사만 (순서 유지). 화면·복사·엑셀·소제목 배정이
    전부 이 함수 하나를 통과해야 서로 어긋나지 않는다 — 조건 밖 기사가 어느 한 곳에만
    남으면 "화면엔 없는데 보고서엔 있는" 상태가 된다."""
    return [a for a in card["articles"] if article_in_condition(a, card)]


def out_of_condition_reason(article: dict, card: dict) -> Optional[str]:
    """조건에서 빠진 이유를 짧게 돌려준다(숨김함 표시용). 조건 안이면 None."""
    if article.get("added_by") == "manual":
        return None
    matched = article.get("matched_keywords", [])
    if not any(k in matched for k in card["keywords"]):
        return "검색어에 안 걸림"
    for keyword in card.get("must_keywords", []):
        if keyword not in matched:
            # 곧은 따옴표(')는 html.escape가 &#x27;로 바꿔 화면에 그대로 보이므로
            # 둥근 따옴표를 쓴다(이스케이프 대상이 아니다).
            return f"\u2018{keyword}\u2019 없음"
    return None


def _search_card(card: dict, now: datetime) -> dict[str, list[dict]]:
    """이 카드의 검색어 + 꼭 포함할 검색어를 전부 검색해 {키워드: 결과}로 돌려준다.

    naver_api 쪽이 이미 키워드 단위 재시도(_KEYWORD_RETRY_ATTEMPTS)를 거친 뒤에도 실패한
    키워드를 사실대로 보고한다 — 그 이상 여기서 다시 돌리지 않는다(§6.9 "자동 재시도
    0회"는 이 계층 얘기). 하나라도 실패했으면 이번 시도 전체를 실패로 취급해, 부분
    반영으로 생기는 애매한 상태("어디까지 반영됐는지 모르겠다")를 만들지 않는다.
    """
    window = card["window"]
    keywords = list(dict.fromkeys(card["keywords"] + card.get("must_keywords", [])))
    results, failed, capped = _search_with_cache(
        card["collect_date"],
        keywords,
        window,
        after=kst_today_at(window["start"]),
        before=kst_today_at(window["end"]),
    )
    if failed:
        names = ", ".join(f"'{k}'" for k in failed)
        raise AdhocCollectError(f"수집에 실패했어요 — {names} 검색 중 오류가 발생했습니다.")

    # §6.4b — 꼭 포함할 검색어가 상한에 걸리면 교집합이 "남아야 할 기사를 빼는" 방향으로
    # 틀린다. 검색어(OR)는 잘려도 "덜 가져올" 뿐이라 여기서 막지 않는다.
    #
    # [수정: 2026-08-24] 예전엔 `hit_search_cap(results)`가 `len(results) >= _MAX_START`로
    # 다시 추정했는데, `_search_one_keyword`가 시간창 밖 기사나 pubDate 파싱 실패 기사를
    # 건너뛰면서도 `start`는 그대로 올리는 경우를 놓쳐 실제로 잘렸는데도 결과가 1,000건
    # 밑으로 떨어져 "안 걸렸다"고 오판했다(§6.4b가 막으려던 상황이 그대로 샜다 — 창을
    # 과거로 좁힌 카드에서 실측 재현: '정부' must-키워드가 0건 수집되고도 통과했었다).
    # 이제 app.naver_api가 이미 정확히 알고 있는 판정(capped 집합)을 그대로 받아 쓴다.
    for keyword in card.get("must_keywords", []):
        if keyword in capped:
            raise AdhocKeywordCapError(
                f"'{keyword}'{josa_eun(keyword)} 오늘 기사가 {_MAX_START:,}건을 넘어 "
                "정확히 거를 수 없어요. "
                "수집 시간대를 좁히거나 더 구체적인 말을 써주세요."
            )
    return results


def run_collect(card_id: str, now: Optional[datetime] = None, log: bool = True) -> dict:
    """카드의 검색어·시간창으로 네이버를 검색해, 그 카드의 목록을 **지금 조건 기준으로
    다시 만든다**.

    **[수정: 2026-08-20] 병합이 아니라 교체다** (ADHOC_DESIGN.md §6.4 — 원래는 정반대
    규칙이었다). 화면의 목록은 언제나 "지금 이 카드의 조건으로 불러올 수 있는 기사
    전부"여야 한다 — 조건을 고쳤는데 옛 조건의 기사가 남아 있으면 담당자는 자기 화면이
    무슨 조건의 결과인지 말할 수 없게 된다.

    **교체이지 삭제가 아니다.** 조건에서 벗어난 기사도 card["articles"]에서 지우지 않고
    그대로 둔다 — 화면이 article_in_condition()으로 매번 걸러 보여줄 뿐이라, 조건을
    되돌리면 소제목 배정·순서까지 그대로 돌아온다(CODING_CONVENTIONS.md §3).

    matched_keywords는 **매 수집마다 현재 검색어 기준으로 다시 쓴다.** 예전에는 수집
    시점의 값을 그대로 뒀는데("카드가 검색어 확정본이라 바뀔 일이 없다"는 전제였다),
    검색어 편집(§6.4a)이 생기면서 그 전제가 깨졌다 — 새 검색어를 추가해도 기존 기사의
    matched_keywords가 그대로면 상단 메타 칩의 검색어별 건수가 실제보다 적게 나온다.

    수집 실패(네이버 쪽 오류)는 예외를 그대로 올린다 — 자동 재시도 없음(§6.9). 카드를
    저장하는 지점은 검색이 전부 성공한 뒤뿐이므로, 실패하면 카드는 이전 상태 그대로다.
    """
    now = now or datetime.now()

    with card_lock(card_id):
        card = load_card(card_id)
        if card is None:
            raise AdhocCardError(f"카드를 찾을 수 없습니다: {card_id}")
        _validate_collectable(card, now)

        results = _search_card(card, now)

        # URL별로 "지금 어느 검색어에 걸렸는가" — 조건 판정과 검색어별 건수의 근거가 된다.
        url_keywords: dict[str, list[str]] = {}
        by_url: dict[str, dict] = {}
        for keyword, items in results.items():
            for item in items:
                url_keywords.setdefault(item["url"], []).append(keyword)
                by_url.setdefault(item["url"], item)

        # 이미 카드에 있는 기사도 현재 검색어 기준으로 다시 매긴다(위 docstring 참고).
        # 이번 검색 결과에 아예 없는 기사는 빈 목록이 되어 자연히 "조건 밖"이 된다 —
        # 지워지지는 않으므로 조건을 되돌리면 다시 걸린다.
        for article in card["articles"]:
            article["matched_keywords"] = url_keywords.get(article["url"], [])

        # 새로 추가할 후보 = 조건을 통과한 기사 중 카드에 아직 없는 것.
        # _apply_options(화이트리스트→정렬→사진/인사 제외→중복제거)는 추가 시점에만
        # 적용한다 — 이미 카드에 있는 기사를 옵션 변경으로 나중에 몰아내지 않기 위해서다.
        existing_urls = {a["url"] for a in card["articles"]}
        candidates = [
            by_url[url]
            for url, keywords_hit in url_keywords.items()
            if url not in existing_urls
            and any(k in keywords_hit for k in card["keywords"])
            and all(k in keywords_hit for k in card.get("must_keywords", []))
        ]
        candidates = _apply_options(candidates, card["options"])

        # [수정: 2026-08-24] _apply_options의 deduplicate_by_title은 "이번에 새로 찾은
        # 후보"끼리만 서로 비교한다 — 카드에 **이미 들어있는** 기사의 제목은 안 본다.
        # 그래서 검색어를 고칠 때마다(add-keyword/remove-keyword/recollect 전부 이
        # run_collect 하나를 거친다) 네이버가 URL이 다른 동일 제목 기사를 다시 돌려주면
        # (사진 캡션류에서 특히 흔함 — 실측: 같은 카드를 세 번 편집하니 "의원 질의에
        # 답하는 구윤철 부총리" 같은 제목이 5중복까지 쌓였다) 매번 새 URL로 다시 붙었다.
        # 조건을 되돌리면 옛 기사가 다시 보이는 §6.4 원칙(교체이지 삭제가 아니다)은
        # card["articles"]에서 아무것도 안 지우면 이미 지켜지므로, 여기서는 "새로
        # 추가하려는" 후보만 기존 제목과 대조해 걸러도 그 원칙과 충돌하지 않는다.
        existing_titles = {a["title"] for a in card["articles"]}
        candidates = [a for a in candidates if a["title"] not in existing_titles]

        for article in candidates:
            card["articles"].append(
                {
                    "outlet": article["outlet"],
                    "title": article["title"],
                    "url": article["url"],
                    "summary": article["summary"],
                    "pub_date": article["pub_date"],
                    "matched_keywords": url_keywords.get(article["url"], []),
                    "group": None,  # 미분류 — 소제목 배정은 별도 화면/버튼의 몫
                    "order": None,  # 사용자가 손대기 전까지는 화면이 언론사 우선순위로 정렬
                    "hidden": False,
                    "added_at": now.isoformat(timespec="seconds"),
                    "added_by": "collect",
                }
            )
        save_card(card)
        in_condition = sum(1 for a in card["articles"] if article_in_condition(a, card))

    # log=False — 검색어를 고쳐서 목록을 다시 맞춘 것은 "수집"이 아니다. 여기서도
    # 기록하면 상단 📥 칩의 수집 이력이 편집할 때마다 "+0건"으로 한 줄씩 늘어나,
    # 정작 알고 싶은 "언제 몇 건을 모았나"가 묻힌다.
    if not log:
        return {"found": in_condition, "added": len(candidates)}

    # collect_log는 append_collect_log가 자기 락으로 다시 감싸므로 위 with 블록 밖에서
    # 호출한다(같은 스레드가 같은 락을 두 번 잡는 걸 피한다 — threading.Lock은 재진입 불가).
    return append_collect_log(
        card_id,
        window_end=card["window"]["end"],
        found=in_condition,
        added=len(candidates),
        now=now,
    )


def recompute_condition(card_id: str, now: Optional[datetime] = None) -> dict:
    """검색어를 고친 뒤 목록을 지금 조건에 맞춘다 — run_collect의 얇은 별칭.

    편집 핸들러(app/adhoc/routes.py)가 "무슨 일을 하는지" 이름으로 읽히게 하려고 따로
    둔다. 실제로 하는 일은 같다: 캐시에 있는 키워드는 검색하지 않으므로(§6.4c) 검색어를
    지우기만 한 경우에는 네이버 호출이 0회다.
    """
    return run_collect(card_id, now=now, log=False)
