# Design Ref: 실시간 기사 현황 증분 캐시 — 새로고침마다 등록된 키워드 전체를 다시
# 검색하지 않고, 키워드별로 "지난번에 어디까지 봤는지"를 따로 기억해뒀다가 그 이후
# 것만 추가로 검색해 병합한다.
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import LIVE_CACHE_FILE


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def load_live_cache(now: Optional[datetime] = None) -> Optional[dict]:
    """저장된 캐시를 읽어온다.

    파일이 없거나, 깨졌거나, 오늘 날짜가 아니면(자정이 지났으면) None을 돌려준다 —
    manual_articles.json과 같은 패턴으로, 별도의 자정 정리 작업 없이 다음 호출이
    자연히 "캐시 없음 = 당일 0시부터 새로 검색"으로 처리하게 한다.

    [수정: 2026-08-13] 캐시 단위를 "그룹 구성 전체"에서 "키워드 하나하나"로 바꿨다
    (app.live_renderer.generate_live_page) — 예전엔 그룹을 새로 만들거나 키워드
    하나만 추가해도 지문(keywords_signature)이 달라져 캐시를 통째로 버리고 당일
    0시부터 전부 다시 검색했다. 이제 키워드마다 "마지막으로 본 시각"과 그 키워드의
    원시 검색 결과를 따로 들고 있어서, 새로 추가된 키워드만 처음부터 검색하고 나머지는
    그대로 재사용한다. 그룹 이름·모드·소속이 바뀌어도 재검색이 필요 없다 — 최종 기사
    목록은 매번 app.naver_api.match_articles_to_groups로 "현재" 그룹 설정 기준으로
    다시 매칭하기 때문에(원시 검색 결과 자체는 그룹과 무관하다), 캐시된 원시 결과만
    있으면 재분류는 공짜다.

    필드:
      keyword_last_seen: {키워드: ISO 시각} — 그 키워드를 마지막으로 어디까지 검색했는지.
      keyword_articles: {키워드: [기사, ...]} — 그 키워드의 원시 검색 결과 누적(당일 전체).
    """
    if not LIVE_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(LIVE_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return None
    if data.get("date") != _today_str(now):
        return None
    return data


def save_live_cache(
    keyword_last_seen: dict, keyword_articles: dict, now: Optional[datetime] = None
) -> None:
    """키워드별 "여기까지 봤다" 시각과 원시 검색 결과를 저장한다.

    keyword_last_seen: {키워드: ISO 시각} — 성공적으로 검색을 마친 키워드만 담는다.
    실패한 키워드는 호출부(app.live_renderer)가 이전 값을 그대로 넘겨 재시도 대상으로
    남긴다(app.naver_api.search_keywords의 failed_keywords 참고).
    keyword_articles: {키워드: [기사, ...]} — 이번에 새로 찾은 것과 기존 캐시를 합친
    "그 키워드의 당일 전체 원시 결과"를 호출부가 미리 병합해 넘긴다.
    """
    atomic_write_text(
        LIVE_CACHE_FILE,
        json.dumps(
            {
                "date": _today_str(now),
                "keyword_last_seen": keyword_last_seen,
                "keyword_articles": keyword_articles,
            },
            ensure_ascii=False,
        ),
    )
