# Design Ref: 사용자 요청(2026-08-20) — [단독]·[속보] 알림이 같은 기사를 여러 번 보내지
# 않도록(정기 회차 검사·수시 폴링·밤사이 몰아보내기 세 경로가 같은 기사를 각자 발견할
# 수 있다), 하루 동안 이미 보낸 기사 URL을 기억해둔다.
#
# [수정: 2026-08-26] URL만 남기던 것을 **보낸 기사의 표시 정보까지** 남기도록 넓혔다
# (`items`). 이유: 홈 화면이 "오늘 [단독] N건"을 보여주려 할 때 회차 파일에서 다시
# 세면 **알림이 실제로 나간 것과 숫자가 어긋난다** — 알림 폴링(poll_and_alert_tick)은
# 감시 그룹의 키워드를 평평하게 모아 넓게 훑는 반면(app.breaking_alert_sender.
# _watched_keywords), 정기 회차는 include_in_scrap과 그룹 OR/AND를 따져 좁게 담기
# 때문이다. "알림 갔는데 화면엔 없는" 기사가 생길 수 있다는 뜻이다(사용자 지적).
# 그래서 **화면도 이 기록 하나만 본다** — 발송 기록이 곧 표시 근거라 둘이 어긋날 수 없다.
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import ALERTED_URLS_FILE


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _load(now: Optional[datetime] = None) -> dict:
    """app.live_cache와 같은 패턴 — date가 오늘이 아니면(자정이 지났으면) 빈 목록으로
    본다. 같은 기사가 다음날 다시 검색돼도(네이버가 재노출하는 경우 등) 새 알림
    대상으로 취급한다 — 실무상 하루가 지나면 "새 소식"으로 다시 알려도 무리가 없다."""
    empty = {"date": _today_str(now), "urls": [], "items": []}
    if not ALERTED_URLS_FILE.exists():
        return empty
    try:
        data = json.loads(ALERTED_URLS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return empty
    if data.get("date") != _today_str(now):
        return empty
    # items가 없는 옛 형식도 그대로 읽는다 — 그날 하루는 건수만 알 수 있고(urls),
    # 다음 알림부터 items가 채워진다. 자정이면 어차피 새로 시작한다.
    return {
        "date": data.get("date"),
        "urls": list(data.get("urls", [])),
        "items": list(data.get("items", [])),
    }


def already_alerted_urls(now: Optional[datetime] = None) -> set:
    return set(_load(now)["urls"])


def mark_alerted(urls: list, now: Optional[datetime] = None, items: Optional[list] = None) -> None:
    """방금 보낸 기사 URL들을 오늘치 목록에 더한다. 받는 사람이 없어 실제로는 아무도
    못 받았어도 이미 "확인은 했다"는 뜻으로 마찬가지로 기록한다 — 안 그러면 매 폴링
    tick마다 같은 후보를 다시 걸러내는 헛수고가 반복된다.

    items: [{"url", "kind", "outlet", "title", "pub_date", "at"}] — 화면(홈)이 목록을
    그리는 데 쓰는 표시 정보. 생략하면 URL만 남던 예전과 똑같이 동작한다(중복 방지는
    urls만으로 되므로 알림 기능 자체는 items가 없어도 온전하다).
    """
    if not urls:
        return
    data = _load(now)
    existing = set(data["urls"])
    existing.update(urls)
    data["urls"] = list(existing)
    if items:
        known = {it.get("url") for it in data["items"]}
        for it in items:
            # 넘어온 items 안의 중복도 거른다 — known을 갱신하지 않으면 한 번에 온 같은 URL이
            # 모두 들어간다(2026-09-18 실측: 같은 속보가 기록에 3번).
            if it.get("url") not in known:
                known.add(it.get("url"))
                data["items"].append(it)
    atomic_write_text(ALERTED_URLS_FILE, json.dumps(data, ensure_ascii=False))


def alerted_items(now: Optional[datetime] = None) -> list:
    """오늘 알림이 나간 기사 목록을 발행시각 순으로 돌려준다 (홈 화면용).

    **홈의 "[단독] N건"은 반드시 이 함수만 쓴다** — 회차 파일에서 말머리를 다시 세면
    알림과 숫자가 어긋난다(모듈 맨 위 주석 참고). 옛 형식(items 없음)이면 빈 목록이
    나오는데, 그때는 화면이 건수만(len(already_alerted_urls())) 보여주면 된다.
    """
    # 중복 방어 — 고치기 전(2026-09-18)에 쌓인 기록엔 같은 URL이 여러 번 있다. 먼저 온 것 하나만.
    unique: dict = {}
    for it in _load(now)["items"]:
        unique.setdefault(it.get("url"), it)
    return sorted(unique.values(), key=lambda it: it.get("pub_date") or "")
