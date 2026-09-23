# [속보] 같은 사건 묶기 — 한 사건을 여러 언론사가 동시에 [속보]로 내면 알림이 그 수만큼
# 쏟아진다. 실측(2026-09-23, 저장 회차 34일 203건): 9/22는 19통 중 6통이 「이형일·강신철·
# 홍지선 장관 임명안 재가」 한 사건이었고, 15개 언론사가 8분 사이에 냈다.
#
# 이 모듈은 "두 제목이 같은 사건인가"만 판정하는 순수 함수 모음이고(저장소·네트워크 없음),
# 쓰는 자리는 app.breaking_alert_sender 두 곳이다:
#   (1) 한 통 안에서 — group_by_event로 묶어 대표 1건만 적고 나머지는 언론사 이름 한 줄로
#       접는다. 이건 **메시지 길이**를 줄인다.
#   (2) 통과 통 사이에서 — folded_by_sent로, 오늘 이미 알림이 나간 사건의 후속 기사는 새
#       알림을 만들지 않는다. **울리는 횟수가 줄어드는 건 이쪽이다** — 폴링이 3분마다
#       돌아 한 사건이 여러 통으로 갈라지므로, (1)만 넣으면 통수는 그대로다.
#
# [단독]에는 적용하지 않는다 — 34일 전수에서 [단독]끼리 같은 사건으로 걸리는 쌍이 0건이라
# (한 언론사가 단독으로 내는 기사라 제목이 겹치지 않는다) 효과는 0이고 다른 사건을 잘못
# 묶을 위험만 남는다. 판정 임계·창은 app.config(ALERT_EVENT_*).
#
# 접힌 기사가 어디서도 사라지지는 않는다: 홈 [속보] 목록(app.alerted_urls의 items)에는
# 그대로 남고, 정말 몰린 경우엔 몰림 알림(app.breaking_burst)이 전체 언론사 수를 알린다.
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import ALERT_EVENT_SIMILARITY, ALERT_EVENT_WINDOW_MIN
from app.filters import HEADLINE_TAG_RE

_KST = timezone(timedelta(hours=9))
# 한 글자 토큰은 버린다 — "이", "등" 같은 조각이 겹쳐 유사도를 부풀린다.
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


def pub_dt(raw) -> Optional[datetime]:
    """pub_date를 KST 기준 naive datetime으로. 못 읽으면 None(없는 시각을 지어내지 않는다).

    app.breaking_burst가 같은 판정을 쓴다 — 두 곳이 갈리면 "묶은 사건"과 "몰림으로 센 사건"의
    시각 축이 달라진다."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_KST).replace(tzinfo=None)
    return parsed


def event_tokens(title: str) -> set:
    """제목에서 사건 비교용 토큰을 뽑는다 — 말머리를 떼고, 두 글자 이상 낱말만."""
    stripped = HEADLINE_TAG_RE.sub("", title or "")
    return {word for word in _TOKEN_RE.findall(stripped) if len(word) > 1}


def similarity(a: set, b: set) -> float:
    """자카드 유사도. 한쪽이 비면 0(비교할 근거가 없으면 같은 사건이라고 하지 않는다)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _within_window(a: Optional[datetime], b: Optional[datetime], window_min: int) -> bool:
    """두 게시 시각이 창 안인가. 한쪽이라도 시각을 모르면 False — 모르는 기사는 묶지 않는다
    (게시 시각 줄·몰림 알림과 같은 규칙)."""
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) / 60 <= window_min


def group_by_event(
    articles: list,
    similarity_min: float = ALERT_EVENT_SIMILARITY,
    window_min: int = ALERT_EVENT_WINDOW_MIN,
) -> list:
    """기사 목록을 같은 사건끼리 묶어 [[대표, 후속…], …]으로 돌려준다.

    - 묶음의 대표는 **가장 먼저 게시된 기사**다(먼저 낸 곳이 대표). 시각을 모르는 기사는
      혼자 한 묶음으로 남는다.
    - 묶음들의 순서와 묶음 안 후속 순서는 **들어온 순서 그대로**다 — 알림 본문의 줄 순서가
      까닭 없이 뒤바뀌면 여러 건을 훑을 때 읽기 어렵다.
    - 창은 **대표 기준**이다(묶음이 무한정 번지지 않게). 몰아보내기처럼 하루치가 한꺼번에
      들어오는 경우에도 아침 사건과 저녁 사건이 제목만 닮아 합쳐지지 않는다.
    """
    groups: list = []          # [[기사…]]
    heads: list = []           # [(대표 토큰, 대표 게시시각)]
    for article in articles:
        tokens = event_tokens(article.get("title", ""))
        when = pub_dt(article.get("pub_date"))
        placed = False
        for index, (head_tokens, head_when) in enumerate(heads):
            if similarity(tokens, head_tokens) >= similarity_min and _within_window(when, head_when, window_min):
                groups[index].append(article)
                # 대표보다 먼저 게시된 기사가 뒤늦게 들어오면 대표 자리를 넘긴다.
                if when is not None and head_when is not None and when < head_when:
                    groups[index].insert(0, groups[index].pop())
                    heads[index] = (tokens, when)
                placed = True
                break
        if not placed:
            groups.append([article])
            heads.append((tokens, when))
    return groups


def folded_by_sent(
    article: dict,
    sent_items: list,
    similarity_min: float = ALERT_EVENT_SIMILARITY,
    window_min: int = ALERT_EVENT_WINDOW_MIN,
) -> Optional[dict]:
    """오늘 이미 알림이 나간 기사(sent_items) 중 이 기사와 같은 사건인 것을 돌려준다
    (없으면 None). sent_items는 app.alerted_urls.alerted_items()의 같은 말머리 항목.

    시각을 못 읽는 기사는 접지 않는다 — 창을 확인할 수 없으면 "새 사건"으로 보내는 쪽이
    안전하다(알려야 할 것을 조용히 삼키는 실수보다 한 통 더 가는 게 낫다)."""
    tokens = event_tokens(article.get("title", ""))
    when = pub_dt(article.get("pub_date"))
    if when is None or not tokens:
        return None
    for item in sent_items:
        if item.get("url") == article.get("url"):
            continue
        if similarity(tokens, event_tokens(item.get("title", ""))) < similarity_min:
            continue
        if _within_window(when, pub_dt(item.get("pub_date")), window_min):
            return item
    return None
