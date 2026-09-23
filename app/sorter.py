# Design Ref: PRD.md 기능1 규칙 3·16 — 언론사 우선순위(기본값, 또는 설정 화면의 사용자 지정 순서) 정렬
from datetime import datetime
from typing import Optional

from app.naver_api import PRIORITY_OUTLETS


def _priority_rank(outlet: str, priority_outlets: tuple) -> int:
    try:
        return priority_outlets.index(outlet)
    except ValueError:
        return len(priority_outlets)  # 목록에 없는 언론사는 맨 뒤 순위


def _pub_desc_key(article: dict) -> float:
    """같은 언론사 안에서 "발행시각 최신 먼저"로 세우기 위한 값 — 최신일수록 작다.

    pub_date가 없거나(실시간 현황이 허용하는 파싱 실패 기사) 형식이 깨졌으면 맨 뒤로
    보낸다(inf) — 없는 시각을 지어내 최신인 척 올리지 않는다.
    """
    raw = article.get("pub_date")
    if not raw:
        return float("inf")
    try:
        return -datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def sort_by_outlet_priority(articles: list[dict], priority_outlets: Optional[list] = None) -> list[dict]:
    """
    언론사 우선순위 순으로 정렬한다 — 같은 언론사 안에서는 발행시각 최신 먼저, 그래도
    같으면 URL 순.

    priority_outlets를 생략하면 기본 우선순위(방송사 -> 주요 언론사 -> 그 외, PRD 규칙3)를 쓴다.
    설정 화면에서 "언론사 선택"을 지정하면(PRD 규칙16) 그 순서를 여기로 넘겨 대신 쓸 수 있다 —
    이 경우 목록에 없는 언론사도 맨 뒤로 밀릴 뿐 정렬 자체는 동일하게 동작한다(제외는 이 함수의
    책임이 아니라 app.filters.filter_by_outlet_whitelist가 별도로 담당).

    목록에 없는 언론사는 제외하지 않고 맨 뒤로 보낸다.

    [수정: 2026-09-02] 예전엔 **언론사 순위 하나만 보는 안정 정렬**이었다 — 같은 언론사
    안에서의 순서가 정의돼 있지 않아, 입력 목록이 어떤 순서로 들어오느냐에 따라 결과가
    매번 달라졌다. 그게 초안에서 숨긴 기사가 확정본에서 되살아나는 사고의 원인이었다:
    초안은 증분 캐시에 쌓인 순서로, 확정본은 마감 시점에 회차 시간창을 한 번에 재검색한
    순서로 이 함수에 들어오는데, 통신사가 같은 기사를 기사 ID만 다르게 여러 건 올리면
    (연합뉴스 001/0016285932 ↔ 001/0016285931 — 제목·발행시각까지 동일)
    app.filters.deduplicate_by_title이 "먼저 나온 1건"으로 남기는 **대표가 두 화면에서
    뒤바뀐다**. 담당자는 초안의 대표를 숨겼는데 확정본엔 쌍둥이 쪽이 대표로 올라오고,
    숨김 대조는 URL 하나뿐이라(app.curation.filter_hidden, 2026-08-27 결정) 숨긴 적 없는
    URL이 그대로 보고서에 실렸다(2026-09-02 17:00 회차, 9쌍 동시 발생).

    그래서 **완전 정렬**로 바꿨다 — 정렬 키만 결정론적이면 두 화면이 같은 대표를 고르므로,
    "담당자가 안 누른 기사를 앱이 대신 치우는" 제목 대조를 되살리지 않고도 부활이 막힌다.
    2차 키를 발행시각 최신순으로 잡은 건 사용자 결정이다(URL 순도 재현되기는 하지만
    순서에 뜻이 없다 — 같은 언론사 안에서 최신 기사가 위로 오는 편이 읽기에도 낫다).

    주의: 완전 동일 제목 중복 제거(app.filters.deduplicate_by_title)는
    이 정렬 이후에 호출해야 한다 — 그래야 우선순위 높은 언론사의 사본이
    남는다 (archive/DESIGN.md 데이터 흐름 참고).

    주의: 이 함수는 어디까지나 **수집 시점의 출발 순서**다 — 담당자가 ↑/↓로 정리한 순서
    (app.preview_order.apply_preview_order)와 [단독] 최상단 규칙(app.filters.
    sort_scoop_first)은 이 뒤에 적용돼 항상 이 정렬을 이긴다.
    """
    return sorted(articles, key=lambda a: outlet_sort_key(a, priority_outlets))


def outlet_sort_key(article: dict, priority_outlets: Optional[list] = None) -> tuple:
    """sort_by_outlet_priority가 쓰는 정렬 키 그대로 — 목록 전체를 다시 세우지 않고
    **한 건을 기존 목록의 어느 자리에 끼울지**만 고를 때 쓴다(수시 확정본이 새로 받은
    기사를 언론사순 자리에 넣는 경우). 정렬과 삽입이 각자 키를 만들면 한쪽만 고쳤을 때
    조용히 어긋나므로 함수를 하나로 둔다."""
    priority = tuple(priority_outlets) if priority_outlets is not None else PRIORITY_OUTLETS
    return (_priority_rank(article["outlet"], priority), _pub_desc_key(article), article.get("url") or "")


def sort_by_pub_desc(articles: list[dict]) -> list[dict]:
    """발행시각 최신 먼저, 같으면 URL 순 — 언론사 순위를 아예 안 보는 정렬.

    수시 **수집 원본** 화면이 쓴다(그 화면의 규칙이 "시간순"이다). 언론사순과 마찬가지로
    완전 정렬이라 같은 입력이면 항상 같은 결과가 나온다 — pub_date가 없거나 깨진 기사는
    없는 시각을 지어내지 않고 맨 뒤로 보낸다(_pub_desc_key)."""
    return sorted(articles, key=lambda a: (_pub_desc_key(a), a.get("url") or ""))
