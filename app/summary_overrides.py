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


def set_summary_override(
    url: str,
    title: Optional[str],
    summary: Optional[str],
    outlet: Optional[str] = None,
) -> None:
    """원문 페이지에서 다시 가져왔거나 담당자가 직접 친 값을 저장한다. title·summary·outlet
    중 실제로 값이 있는 쪽만 덮어쓰고(하나도 없으면 애초에 호출하는 쪽이 부르지 않는다),
    빈 칸은 기존 저장값(있다면)이나 원래 표시값을 그대로 둔다 — 예: og:title은 없고
    og:description만 있는 페이지면 요약만 고쳐지고 제목은 그대로다.

    outlet(언론사명)은 「기사 정보 직접 수정」 창에서만 들어온다 — 「원문 다시 불러오기」는
    제목·요약만 읽는다. 네이버 미러가 아닌 언론사 자체 도메인 기사는 oid가 없어
    resolve_outlet이 이름을 못 찾고 도메인 문자열(aienews.co.kr)을 그대로 돌려주는데,
    그 값이 보고서 한 줄(`ㅇ (언론사) 제목`)로 나가기 때문에 담당자가 고칠 길이 있어야 한다.
    제목·요약과 같은 칸에 두는 이유는 「이 기사의 언론사가 무엇인가」가 제목과 같은
    **기사에 관한 사실**이어서다(어느 보고서에 넣고 빼는 결정이 아니다).
    """
    overrides = load_summary_overrides()
    entry = overrides.get(url, {})
    if title:
        entry["title"] = title
    if summary:
        entry["summary"] = summary
    if outlet:
        entry["outlet"] = outlet
    overrides[url] = entry
    _write(overrides)


def apply_summary_overrides(articles: list) -> list:
    """기사 목록에 저장된 오버라이드를 입혀 돌려준다 — 저장된 키(title/summary/outlet)만
    바꾸고 나머지(url, pub_date 등)는 그대로 둔다. 완성본·초안·오늘 전체 기사·지난 기사
    전부 기사 목록을 확정하는 지점에서 한 번씩 이 함수를 거쳐야, 어느 화면에서 봐도
    같은 제목·요약·언론사가 보인다(전역 저장이므로).

    **입히는 자리가 여기 한 곳이라** 화면·복사·txt·발송·엑셀 「언론사명」이 코드를 더
    안 고쳐도 따라온다 — 저장된 키를 통째로 병합하기 때문이다.
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
