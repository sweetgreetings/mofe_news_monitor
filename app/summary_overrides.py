# Design Ref: 사용자 요청(2026-08-05) — 네이버 API의 제목·요약이 이상한 지점에서 잘려 있을 때,
# 기사 줄에 마우스를 올리면 나오는 "🔄 원문에서 다시 가져오기" 버튼으로 그 기사 하나만 고친다.
import json
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import SUMMARY_OVERRIDES_FILE


def load_summary_overrides() -> dict:
    """URL별로 사용자가 원문에서 다시 가져온 제목·요약을 읽어온다. {url: {"title": ..., "summary": ...}}."""
    if not SUMMARY_OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(SUMMARY_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(overrides: dict) -> None:
    atomic_write_text(SUMMARY_OVERRIDES_FILE, json.dumps(overrides, ensure_ascii=False, indent=2))


def set_summary_override(url: str, title: Optional[str], summary: Optional[str]) -> None:
    """원문 페이지에서 다시 가져온 값을 저장한다. title·summary 중 실제로 값을 찾은 쪽만
    덮어쓰고(둘 다 못 찾았으면 애초에 호출하는 쪽이 부르지 않는다), 못 찾은 쪽은 기존
    저장값(있다면)이나 원래 표시값을 그대로 둔다 — 예: og:title은 없고 og:description만
    있는 페이지면 요약만 고쳐지고 제목은 그대로다.
    """
    overrides = load_summary_overrides()
    entry = overrides.get(url, {})
    if title:
        entry["title"] = title
    if summary:
        entry["summary"] = summary
    overrides[url] = entry
    _write(overrides)


def apply_summary_overrides(articles: list) -> list:
    """기사 목록에 저장된 오버라이드를 입혀 돌려준다 — title/summary 필드만 있으면 바꾸고
    나머지(outlet, url, pub_date 등)는 그대로 둔다. 완성본·초안·실시간 현황·지난 기사
    전부 기사 목록을 확정하는 지점에서 한 번씩 이 함수를 거쳐야, 어느 화면에서 봐도
    같은 개선된 제목·요약이 보인다(전역 저장이므로).
    """
    overrides = load_summary_overrides()
    if not overrides:
        return articles
    result = []
    for article in articles:
        override = overrides.get(article["url"])
        if override:
            article = {**article, **{k: v for k, v in override.items() if v}}
        result.append(article)
    return result
