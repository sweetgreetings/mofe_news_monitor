# Design Ref: 스크랩 초안(preview.html) 증분 캐시 — app.live_cache와 같은 원리(지난번에
# 어디까지 봤는지 기억해뒀다가 그 이후 것만 추가로 검색해 병합)를 쓰되, 기준이 "당일
# 전체"가 아니라 "지금 진행 중인 한 회차"라는 점만 다르다. signature에 회차 식별값
# (slot["end"])까지 넣어두면, 회차가 바뀔 때(정식 스크랩이 끝나 다음 회차로 넘어갈 때)
# 자연히 캐시가 무효화되어 그 회차의 시작 시각부터 새로 모은다.
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import PREVIEW_CACHE_FILE


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def load_preview_cache(now: Optional[datetime] = None) -> Optional[dict]:
    """저장된 캐시를 읽어온다.

    파일이 없거나, 깨졌거나, 오늘 날짜가 아니면(자정이 지났으면) None을 돌려준다 —
    app.live_cache와 같은 패턴으로, 별도의 자정 정리 없이 다음 호출이 자연히
    "캐시 없음 = 이 회차 시작부터 새로 검색"으로 처리하게 한다.
    """
    if not PREVIEW_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(PREVIEW_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return None
    if data.get("date") != _today_str(now):
        return None
    return data


def save_preview_cache(signature: dict, last_seen_pub_date: str, articles: list, now: Optional[datetime] = None) -> None:
    """검색 결과와 "여기까지 봤다"는 기준 시각을 저장한다.

    signature: 이 캐시를 만들 때 쓴 키워드 그룹 구성 + 지금 회차의 식별값(slot["end"]) —
    다음 호출 때 지금 상태와 다르면(키워드를 고쳤거나 다음 회차로 넘어갔으면) 캐시를
    신뢰하지 않고 처음부터 다시 검색하기 위한 비교용 값이다(app.preview_renderer).
    """
    atomic_write_text(
        PREVIEW_CACHE_FILE,
        json.dumps(
            {
                "date": _today_str(now),
                "signature": signature,
                "last_seen_pub_date": last_seen_pub_date,
                "articles": articles,
            },
            ensure_ascii=False,
        ),
    )
