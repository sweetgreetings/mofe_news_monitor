# Design Ref: 실시간 기사 현황 증분 캐시 — 새로고침마다 당일 전체를 다시 검색하지 않고
# 지난번에 어디까지 봤는지 기억해뒀다가 그 이후 것만 추가로 검색해 병합한다.
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
    keywords_signature: list, last_seen_pub_date: str, articles: list, now: Optional[datetime] = None
) -> None:
    """검색 결과와 "여기까지 봤다"는 기준 시각을 저장한다.

    keywords_signature: 이 캐시를 만들 때 쓴 키워드 그룹 구성(그룹별 keywords+mode) —
    다음 호출 때 지금 설정과 다르면(그룹을 고쳤으면) 캐시를 신뢰하지 않고 처음부터
    다시 검색하기 위한 비교용 값이다(app.live_renderer.generate_live_page).
    """
    atomic_write_text(
        LIVE_CACHE_FILE,
        json.dumps(
            {
                "date": _today_str(now),
                "keywords_signature": keywords_signature,
                "last_seen_pub_date": last_seen_pub_date,
                "articles": articles,
            },
            ensure_ascii=False,
        ),
    )
