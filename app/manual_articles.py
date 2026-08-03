# Design Ref: 실시간 기사 현황 "→ 스크랩" — 당일 자정까지만 유지되는 임시 클립보드 성격의 구획
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import MANUAL_ARTICLES_FILE


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def load_manual_articles(now: Optional[datetime] = None) -> list:
    """오늘 "직접 추가한 기사" 목록을 추가한 순서대로 읽어온다.

    저장된 날짜가 오늘이 아니면(자정이 지났으면) 자동으로 빈 목록 취급한다 — 파일을
    그 자리에서 지우진 않고, 다음에 add_manual_article이 호출될 때 오늘 날짜로 덮어써진다.
    """
    if not MANUAL_ARTICLES_FILE.exists():
        return []
    try:
        data = json.loads(MANUAL_ARTICLES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    if data.get("date") != _today_str(now):
        return []
    return data.get("articles", [])


def _write(articles: list, now: Optional[datetime] = None) -> None:
    atomic_write_text(
        MANUAL_ARTICLES_FILE,
        json.dumps({"date": _today_str(now), "articles": articles}, ensure_ascii=False, indent=2),
    )


def add_manual_article(article: dict, now: Optional[datetime] = None) -> bool:
    """실시간 기사 현황에서 고른 기사 하나를 "직접 추가한 기사" 목록 맨 뒤에 추가한다.

    이미 같은 URL이 들어있으면(중복 클릭 등) 아무 일도 하지 않고 False를 돌려준다.
    새로 추가하면 True. 자정이 지난 뒤 첫 추가라면 load_manual_articles가 빈 목록을
    돌려주므로 자연히 오늘치로 새로 시작된다.
    """
    articles = load_manual_articles(now)
    if any(a["url"] == article["url"] for a in articles):
        return False
    articles.append(article)
    _write(articles, now)
    return True


def pop_manual_article(url: str, now: Optional[datetime] = None) -> Optional[dict]:
    """"직접 추가한 기사" 목록에서 기사 하나를 꺼내(제거하고) 그 내용을 돌려준다.

    정식 스크랩으로 "승격"시킬 때 쓴다(app.settings_server의 promote 핸들러) — 여기서
    빼서 호출하는 쪽이 오늘 회차의 실제 기사 목록에 넣는다. 없으면 None.
    """
    articles = load_manual_articles(now)
    for i, article in enumerate(articles):
        if article["url"] == url:
            popped = articles.pop(i)
            _write(articles, now)
            return popped
    return None
