# Design Ref: "스크랩 초안"에서 "직접 추가한 기사"를 위로 올려 예약해두면, 다음 정식
# 회차가 실제로 저장될 때(app.scraper.collect_run) 자동으로 그 회차의 기사 목록에
# 합쳐진다 — 저장되기 전까지는 초안 화면(app.preview_renderer)의 미리보기 계산에도
# 미리 합쳐 보여줘서, 소제목 분류에 바로 묶여 보이도록 한다.
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import DRAFT_PENDING_ARTICLES_FILE


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def load_draft_pending_articles(now: Optional[datetime] = None) -> list:
    """오늘 예약된(다음 회차에 끼워 넣을) 기사 목록을 읽어온다.

    app.manual_articles.load_manual_articles와 같은 패턴 — 저장된 날짜가 오늘이
    아니면(자정이 지났으면) 자동으로 빈 목록 취급한다.
    """
    if not DRAFT_PENDING_ARTICLES_FILE.exists():
        return []
    try:
        data = json.loads(DRAFT_PENDING_ARTICLES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    if data.get("date") != _today_str(now):
        return []
    return data.get("articles", [])


def _write(articles: list, now: Optional[datetime] = None) -> None:
    atomic_write_text(
        DRAFT_PENDING_ARTICLES_FILE,
        json.dumps({"date": _today_str(now), "articles": articles}, ensure_ascii=False, indent=2),
    )


def add_draft_pending_article(article: dict, now: Optional[datetime] = None) -> bool:
    """"스크랩 초안"에서 "위로"(예약) 눌러 기사 하나를 예약 목록 맨 뒤에 추가한다.

    이미 같은 URL이 들어있으면 아무 일도 하지 않고 False를 돌려준다.
    """
    articles = load_draft_pending_articles(now)
    if any(a["url"] == article["url"] for a in articles):
        return False
    articles.append(article)
    _write(articles, now)
    return True


def clear_draft_pending_articles(now: Optional[datetime] = None) -> list:
    """예약 목록을 통째로 비우면서 그 내용을 돌려준다.

    app.scraper.collect_run이 회차를 실제로 저장하는 시점에 호출해, 예약된 기사를
    이번 회차 기사 목록에 합친 뒤 목록을 비운다(한 번 합쳐지면 다시 쓰일 일이 없다).
    """
    articles = load_draft_pending_articles(now)
    _write([], now)
    return articles
