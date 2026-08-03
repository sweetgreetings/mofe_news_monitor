# Design Ref: 스크랩 초안(preview.html) ↑/↓ — 같은 소제목 "안"의 순서를 기억해두는 저장소.
# group_overrides.json이 소제목 "사이" 이동(어느 소제목으로 갈지)을 기억하는 것과 짝을
# 이룬다. 초안은 저장된 회차가 없어 매번 새로 계산되므로, app.curation.move_article이
# 계산해낸 순서를 어딘가에 저장해두지 않으면 다음 계산(새로고침)에서 그냥 사라진다.
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import PREVIEW_ORDER_FILE


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def load_preview_order(now: Optional[datetime] = None) -> list:
    """오늘 저장된 "사용자가 직접 정렬한 순서"(URL 목록, 앞에 있을수록 우선)를 읽어온다.

    저장된 날짜가 오늘이 아니면(자정이 지났으면) 빈 목록 취급한다 — manual_articles.json과
    같은 패턴으로, 파일을 그 자리에서 지우진 않고 다음 save_preview_order 호출 때 오늘
    날짜로 덮어써진다.
    """
    if not PREVIEW_ORDER_FILE.exists():
        return []
    try:
        data = json.loads(PREVIEW_ORDER_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    if data.get("date") != _today_str(now):
        return []
    return data.get("order", [])


def save_preview_order(order: list, now: Optional[datetime] = None) -> None:
    atomic_write_text(
        PREVIEW_ORDER_FILE,
        json.dumps({"date": _today_str(now), "order": order}, ensure_ascii=False),
    )


def clear_preview_order(now: Optional[datetime] = None) -> None:
    """정식 회차가 저장되며 이 순서를 한 번 반영한 뒤 비운다(app.scraper.collect_run) —
    다음 회차는 새로 시작해야 하므로, 지난 회차의 순서가 다음 회차에 잘못 섞이지 않게 한다."""
    save_preview_order([], now)


def apply_preview_order(articles: list, order: list) -> list:
    """저장된 순서(order, URL 목록)에 맞춰 articles를 재배열한다.

    order에 있는 URL의 기사는 그 순서대로 앞쪽에, order에 없는(그 사이 새로 나온) 기사는
    원래 순서(언론사 우선순위 등 이미 정렬된 입력 순서)를 유지한 채 뒤에 이어붙인다.
    app.classifier.classify_articles가 "그룹 내 기사 순서는 입력 순서를 유지"하므로,
    여기서 정한 순서가 소제목별 화면 순서로 그대로 이어진다.
    """
    if not order:
        return articles
    position = {url: i for i, url in enumerate(order)}
    known = [a for a in articles if a["url"] in position]
    unknown = [a for a in articles if a["url"] not in position]
    known.sort(key=lambda a: position[a["url"]])
    return known + unknown
