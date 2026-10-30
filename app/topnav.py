"""상단바 — 모든 화면이 이 한 곳의 CSS·마크업을 쓴다.

규칙(CLAUDE.md 「상단바」, 시안 mockups/TOPBAR_NAV_MOCKUP.html · 간격은 mockups/TOPBAR_SPACING_MOCKUP.html B안):
- 정기 화면: 홈 │ 오늘 전체 기사 · 초안 · 확정본 · 보관함
- 수시 화면: 홈 │ 새 수집 · 원본 · 확정본 · 보관함
- 지금 화면은 빼지 않고 흐름 색 칩으로 켜 둔다(누를 수 없음). 그래야 같은 링크가
  모든 화면에서 같은 자리에 온다.
- 갈 곳이 아직 없는 링크(수시 원본·확정본 카드가 없을 때)는 빼지 않고 흐리게 둔다.
- 흐름 밖 화면은 홈 │ ← 돌아갈 곳 하나(돌아갈 곳이 홈뿐이면 홈만).
- 모두 왼쪽 붙임. 상단바엔 가는 곳만 둔다(새로고침 같은 동작은 본문으로).

링크는 모두 설정 서버 절대주소다 — 정기 화면은 file:// 로 열릴 수도 있어서다.
"""

from __future__ import annotations

import html
from typing import Optional

from app.config import (
    PLUS_GLYPH_CSS,
    BANNER_CSS,
    SHAPE_TOKENS_CSS,
    COLOR_ACCENT,
    COLOR_ACCENT_BORDER,
    COLOR_ADHOC_BG,
    COLOR_ADHOC_BORDER_STRONG,
    COLOR_ADHOC_TEXT,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_HOVER,
    COLOR_SCREEN_TAG_WORK_BG,
    COLOR_SCREEN_TAG_WORK_BORDER,
    COLOR_SCREEN_TAG_WORK_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SOFT,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)

_BASE = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}"

HOME_HREF = f"{_BASE}/home.html"

# (키, 라벨, 경로) — 홈 흐름도 정기 줄과 같은 순서
REGULAR_ITEMS = (
    ("live", "오늘 전체 기사", "/live.html"),
    ("preview", "초안", "/preview.html"),
    ("final", "확정본", "/index.html"),
    ("archive", "보관함", "/history.html"),
)


def topnav_style() -> str:
    """상단바 CSS(값이 채워진 문자열). 템플릿에 그대로 끼워 넣는다.

    링크마다 투명 테두리 1px + 같은 안쪽 여백을 줘서, 켜진 칩이 옮겨 다녀도 옆 링크가
    1px도 밀리지 않고 상단바 높이도 예전과 같다(sticky 요소들의 top 값이 이 높이에 맞춰져 있다).

    모든 화면(홈 빼고)이 이 CSS를 한 번씩 끼워 넣으므로, 모양 토큰(`--r-md` 등 CSS 변수,
    app.config SHAPE_TOKENS_CSS)과 「+」 글리프 CSS(PLUS_GLYPH_CSS), 경고·안내 상자 한 벌
    (BANNER_CSS)도 여기서 함께 낸다. 홈은 상단바가 없어 따로 낸다 — 다만 홈엔 경고 상자가
    없어 BANNER_CSS는 안 낸다.
    """
    return f"""
  {SHAPE_TOKENS_CSS}
  {PLUS_GLYPH_CSS}
{BANNER_CSS}
  .topbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 20;
    background: {COLOR_CARD}; border-bottom: 1px solid {COLOR_BORDER}; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  }}
  .topbar-inner {{
    max-width: 800px; margin: 0 auto; padding: 12px 24px;
    display: flex; justify-content: flex-start; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  .topbar-inner > .tn {{ color: {COLOR_ACCENT}; text-decoration: none; font-size: var(--fs-md); font-weight: 600;
    padding: 5px 14px; border: 1px solid transparent; border-radius: var(--r-pill); white-space: nowrap; }}
  .topbar-inner > a.tn:hover {{ background: {COLOR_HOVER}; }}
  .topbar-inner > .tn-sep {{ width: 1px; height: 18px; background: {COLOR_BORDER}; margin: 0 16px; }}
  .topbar-inner > .tn.is-current {{ cursor: default; background: {COLOR_HOVER};
    border-color: {COLOR_ACCENT_BORDER}; color: {COLOR_ACCENT}; }}
  .topbar.adhoc .topbar-inner > .tn.is-current {{ background: {COLOR_ADHOC_BG};
    border-color: {COLOR_ADHOC_BORDER_STRONG}; color: {COLOR_ADHOC_TEXT}; }}
  /* 켜진 칩 배색 = 그 화면의 이름 칩 배색. 초안은 회색으로 켠다 — 초안·확정본은 상단바도
     머리줄도 같은 글자라, 켜진 칩까지 같은 파랑이면 「어느 글자가 켜졌나」를 읽어야만 안다.
     파란 링크 넷 사이에서 혼자 파랗지 않은 칩은 글자를 안 읽어도 걸리고, 머리줄 띠
     (app/renderer.py head-band)와 같은 배색이라 상단바와 머리줄이 같은 말을 두 번 한다.
     시안 mockups/DRAFT_FINAL_DISTINCT_MOCKUP.html. */
  .topbar.work .topbar-inner > .tn.is-current {{ background: {COLOR_SCREEN_TAG_WORK_BG};
    border-color: {COLOR_SCREEN_TAG_WORK_BORDER}; color: {COLOR_SCREEN_TAG_WORK_TEXT}; }}
  .topbar-inner > .tn.is-empty {{ color: {COLOR_TEXT_MUTED}; opacity: 0.55; cursor: not-allowed; }}
  .topbar-inner > a.tn.back {{ color: {COLOR_TEXT_SOFT}; }}
  .topbar-inner > a.tn.back:hover {{ color: {COLOR_ACCENT}; }}
  @media (max-width: 480px) {{
    .topbar-inner {{ padding: 12px 12px; gap: 2px; }}
    .topbar-inner > .tn {{ padding: 5px 8px; }}
    .topbar-inner > .tn-sep {{ margin: 0 8px; }}
  }}
"""


def _link(label: str, href: Optional[str], cls: str = "", tip: str = "") -> str:
    classes = ("tn " + cls).strip()
    label_html = html.escape(label)
    if cls == "is-current":
        return f'<span class="{classes}" aria-current="page">{label_html}</span>'
    if not href:
        return (f'<span class="{classes} is-empty" aria-disabled="true" '
                f'title="{html.escape(tip)}">{label_html}</span>')
    return f'<a class="{classes}" href="{html.escape(href)}">{label_html}</a>'


def _bar(items: list[str], flow: str = "") -> str:
    cls = f"topbar {flow}".strip()
    inner = "\n  ".join(items)
    return f'<div class="{cls}"><div class="topbar-inner">\n  {inner}\n</div></div>'


_SEP = '<span class="tn-sep" aria-hidden="true"></span>'


def regular_nav(current: Optional[str] = None) -> str:
    """정기 화면 상단바. current는 REGULAR_ITEMS의 키(휴지통처럼 어느 칸도 아니면 None)."""
    items = [_link("홈", HOME_HREF), _SEP]
    for key, label, path in REGULAR_ITEMS:
        items.append(_link(label, _BASE + path, "is-current" if key == current else ""))
    # 다듬기 전 화면(초안)만 켜진 칩이 회색이다 — 그 화면 이름 칩과 같은 배색(위 CSS).
    return _bar(items, "reg work" if current == "preview" else "reg")


def adhoc_nav(current: Optional[str], collect_href: str, bundle_href: str) -> str:
    """수시 화면 상단바. current ∈ {"new", "collect", "bundle", "archive"}.

    [수정: 2026-10-21] 원본·확정본은 **오늘 만든 카드**로 가고, 오늘 것이 없으면 그 종류의
    빈 화면으로 간다(app.adhoc.renderer._latest_href) — 그래서 이 두 칸은 언제나 갈 곳이
    있고 흐린 자리(is-empty)로 남지 않는다. 흐린 칩은 "영영 없다"는 뜻인데 이건 "오늘 것만
    없다"라 할 일이 있고, 흐린 칩은 왜 흐린지 스스로 말하지 못한다.
    """
    entries = (
        ("new", "새 수집", f"{_BASE}/adhoc/new"),
        ("collect", "원본", collect_href),
        ("bundle", "확정본", bundle_href),
        ("archive", "보관함", f"{_BASE}/adhoc"),
    )
    items = [_link("홈", HOME_HREF), _SEP]
    for key, label, href in entries:
        if key == current:
            items.append(_link(label, href, "is-current"))
        else:
            items.append(_link(label, href))
    # 다듬기 전 화면(원본)만 켜진 칩이 회색이다 — 그 화면 이름 칩과 같은 배색(위 .topbar.work).
    # 정기 초안과 **같은 규칙·같은 CSS**이고, 머리줄 띠(app/adhoc/renderer.py .head-band)와
    # 배색이 같아 상단바와 머리줄이 같은 말을 두 번 한다.
    return _bar(items, "adhoc work" if current == "collect" else "adhoc")


def plain_nav(back_label: Optional[str] = None, back_href: Optional[str] = None) -> str:
    """흐름 밖 화면 상단바 — 홈 │ ← 돌아갈 곳. 돌아갈 곳이 없으면 홈만."""
    items = [_link("홈", HOME_HREF)]
    if back_label and back_href:
        items += [_SEP, _link(f"← {back_label}", back_href, "back")]
    return _bar(items)
