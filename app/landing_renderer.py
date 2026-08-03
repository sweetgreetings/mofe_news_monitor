# Design Ref: DESIGN.md §0 진입 화면 (home.html — 앱이 브라우저로 맨 처음 여는 화면), PRD.md 기능3
import html
from pathlib import Path
from typing import Optional

from app.config import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_ERROR,
    COLOR_HEADER,
    COLOR_HOVER,
    COLOR_LIVE_BG,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    CUTE_FONT_BASE64,
    CUTE_FONT_NAME,
    FONT_STACK,
    LANDING_HTML_PATH,
    LANDING_KEYWORD_COUNT,
    LOGO_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
    WORDCLOUD_FONT_BASE64,
    WORDCLOUD_FONT_NAME,
)
from app.atomic_write import atomic_write_text
from app.curation import filter_hidden
from app.settings import all_search_keywords, load_settings
from app.storage import load_today_runs
from app.summarizer import extract_keyword_frequencies

# [추가: 2026-07-26] "구름이 잠잠" 빈 상태 문구용 손글씨체 — app.renderer와 동일 규칙(오프라인
# 대비 base64 내장). 파일별로 독립적인 이 앱의 렌더러 구조를 따라 여기서도 따로 정의한다.
_CUTE_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{CUTE_FONT_NAME}'; "
    f"src: url(data:font/woff2;base64,{CUTE_FONT_BASE64}) format('woff2'); font-display: swap; }}"
)

# [추가: 2026-07-26] 워드클라우드 전용 서체(Jua) — 그날그날 임의의 뉴스 단어가 나오므로
# CUTE_FONT처럼 몇 글자만 골라낸 서브셋이 아니라 한글 전체 글리프가 필요하다
# (app.config.WORDCLOUD_FONT_BASE64 문서 참고).
_WORDCLOUD_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{WORDCLOUD_FONT_NAME}'; "
    f"src: url(data:font/woff2;base64,{WORDCLOUD_FONT_BASE64}) format('woff2'); font-display: swap; }}"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>재정경제부 언론 모니터링</title>
<style>
  {cute_font_face}
  {wordcloud_font_face}
  body {{
    margin: 0; background: {bg}; color: {text}; font-family: {font_stack};
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
  }}
  .container {{
    max-width: 600px; margin: 24px auto; padding: 40px 24px; text-align: center;
    background: {card}; border: 1px solid {border}; border-radius: 8px;
  }}
  .logo {{ max-width: 160px; max-height: 160px; margin-bottom: 16px; border-radius: 50%; }}
  h1 {{ font-size: 1.3rem; font-weight: 600; margin: 0 0 18px; line-height: 1.5; color: {header}; }}
  h2 {{ font-size: 1rem; color: {text_muted}; font-weight: 500; margin-bottom: 12px; }}
  /* [수정: 2026-08-03] 진입 화면 리디자인(처음엔 시안 C였다가 시안 A로 변경) —
     실시간·초안·완성본이 워드클라우드 위/아래로 나뉘어 있어 "하나의 흐름"으로 안 읽히던
     것을, 화살표로 이은 가로 파이프라인 한 줄로 바꿨다. 설정은 그 아래 작은 텍스트
     링크로 낮춰서 나머지 셋과 뎁스가 같아 보이지 않게 했다. */
  .flow-a {{ display: flex; align-items: center; justify-content: center; gap: 0; margin: 0 auto 32px; flex-wrap: wrap; }}
  .flow-a a {{
    text-decoration: none; display: flex; flex-direction: column; align-items: center; gap: 4px;
    padding: 12px 18px; border-radius: 10px; min-width: 108px;
  }}
  .flow-a .icon {{ font-size: 1.05rem; line-height: 1; }}
  .flow-a .label {{ font-size: 0.86rem; font-weight: 700; }}
  .flow-a .sub {{ font-size: 0.68rem; color: {text_muted}; }}
  .flow-a a.live {{ background: {live_bg}; }}
  .flow-a a.live .label {{ color: {error}; }}
  .flow-a a.draft {{ background: {hover}; }}
  .flow-a a.draft .label {{ color: {accent}; }}
  .flow-a a.done {{ background: {tonal_hover}; }}
  .flow-a a.done .label {{ color: {header}; }}
  .flow-a .arrow {{ color: {border}; font-size: 1.3rem; padding: 0 4px; }}
  /* [수정: 2026-08-03] 설정 링크를 화면 우측 하단(워드클라우드~문의 이메일 사이)으로 옮김 */
  .settings-link {{ display: flex; justify-content: flex-end; margin: 4px 0 22px; }}
  .settings-link a {{ font-size: 0.95rem; color: {text_muted}; text-decoration: none; }}
  .settings-link a:hover {{ color: {accent}; }}
  .wordcloud {{
    display: flex; flex-wrap: wrap; justify-content: center; align-items: baseline;
    gap: 2px 7px; margin: 0 auto; max-width: 100%;
  }}
  /* [수정: 2026-08-03] 단어마다 주던 고정 회전(--rot)을 없앴다 — "굳이 비뚤빼뚤할 필요
     없다"는 사용자 판단(2026-07-26에 넣었던 걸 되돌림). 호버 시 살짝 흔들리는
     wc-jitter 효과는 그대로 두되, 이제 0도를 중심으로만 흔들린다. */
  .wc-word {{
    white-space: nowrap; line-height: 1.3; font-family: '{wordcloud_font_name}', sans-serif;
    display: inline-block;
  }}
  .wc-word:hover {{ animation: wc-jitter 0.35s ease-in-out; }}
  @media (prefers-reduced-motion: reduce) {{
    .wc-word:hover {{ animation: none; }}
  }}
  @keyframes wc-jitter {{
    0%   {{ transform: translate(0, 0) rotate(0deg); }}
    20%  {{ transform: translate(-1px, 0.5px) rotate(-2deg); }}
    40%  {{ transform: translate(1px, -0.5px) rotate(2deg); }}
    60%  {{ transform: translate(-1px, 0.5px) rotate(-1.5deg); }}
    80%  {{ transform: translate(1px, 0) rotate(1deg); }}
    100% {{ transform: translate(0, 0) rotate(0deg); }}
  }}
  .wc-caption {{ color: {text_muted}; font-size: 0.7rem; margin: 4px 0 36px; }}
  .empty {{ color: {text_muted}; font-size: 0.9rem; margin-bottom: 36px; }}
  .cute-caption-sm {{ font-family: '{cute_font_name}', sans-serif; font-size: 0.85rem; color: {text_muted}; margin-top: 4px; }}
  .contact {{ color: {text_muted}; font-size: 0.85rem; }}
  .contact a {{ color: {accent}; }}
</style>
</head>
<body>
<div class="container">
  {logo_html}
  <h1>온라인 기사 모아보기</h1>
  <div class="flow-a">
    <a class="live" href="http://{settings_host}:{settings_port}/live.html">
      <span class="icon">🔴</span><span class="label">실시간 현황</span><span class="sub">지금 들어오는 기사</span>
    </a>
    <span class="arrow">→</span>
    <a class="draft" href="http://{settings_host}:{settings_port}/preview.html">
      <span class="icon">📝</span><span class="label">스크랩 초안</span><span class="sub">다음 회차 미리보기</span>
    </a>
    <span class="arrow">→</span>
    <a class="done" href="index.html">
      <span class="icon">📗</span><span class="label">스크랩 완성본</span><span class="sub">가장 최근 확정본</span>
    </a>
  </div>
  {wordcloud_html}
  <p class="wc-caption">*0시 이후 현재까지 주요 언급어</p>
  <div class="settings-link"><a href="http://{settings_host}:{settings_port}/">⚙️ 설정</a></div>
  <p class="contact">문의 📧 <a href="mailto:sweetgreetings@naver.com">sweetgreetings@naver.com</a></p>
</div>
</body>
</html>
"""

# [수정: 2026-07-26] 실제 언급 횟수(값) 기준 연속 보간 대신 등수(rank) 기준 5단계
# 티어로 바꿨다 — 그날 언급 횟수 분포가 한쪽에 몰리면(예: 대부분 2~3회, 소수만 7회)
# 값 기준 보간은 크기가 양극단으로만 쏠려 "중간이 없어 보이는" 문제가 있었다.
# 등수로 5등분하면 단어 수가 몇 개든 각 티어에 고르게 배정되어 중간 단계가 항상 존재한다.
_TIER_SIZES_REM = (1.9, 1.55, 1.25, 1.0, 0.82)
_TIER_COLORS = (COLOR_HEADER, COLOR_ACCENT, "#3B7DDB", "#7AA5E8", "#A9C3EF")
# [추가: 2026-07-25] 검색 키워드(예: 등록해둔 인물명)도 워드클라우드 집계에 포함하되
# (app.summarizer.extract_keyword_frequencies), "이건 검색어라 나온 거구나"를 한눈에
# 구분할 수 있도록 muted 회색으로 눈에 띄지 않게 표시한다.
_KEYWORD_ORIGIN_COLOR = COLOR_TEXT_MUTED


def render_word_cloud(freqs: list, search_keywords: Optional[list] = None) -> str:
    """(단어, 빈도) 목록을 등수 기준 5단계 크기·색 티어로 태그 클라우드를 렌더링한다
    (PRD.md 기능3 규칙 3).

    통계적으로 정확할 필요는 없는 눈요기용이다. freqs는 이미 빈도 내림차순이므로,
    등수를 5등분해 티어를 매긴다(1티어=가장 진하고 큼 ~ 5티어=가장 옅고 작음).
    검색 키워드 자체는(등록해둔 인물명 등) 티어와 무관하게 muted 회색으로 표시해,
    언급량이 많아서 뜬 실제 화제어와 구분되게 한다.

    마우스를 올리면(title 속성, 자바스크립트 불필요) 실제 언급 횟수를 볼 수 있다 —
    티어 색·크기만으로는 정확한 횟수나 다른 단어와의 차이를 알 수 없기 때문이다.
    """
    if not freqs:
        return '<div class="empty">💤<div class="cute-caption-sm">구름이 잠잠</div></div>'

    keyword_set = {k.lower() for k in (search_keywords or [])}
    total = len(freqs)

    spans = []
    for rank, (word, freq) in enumerate(freqs):
        tier = min(4, rank * 5 // total)
        size = _TIER_SIZES_REM[tier]
        if word.lower() in keyword_set:
            color, weight = _KEYWORD_ORIGIN_COLOR, 500
        else:
            color = _TIER_COLORS[tier]
            weight = 700 if tier == 0 else 500
        spans.append(
            f'<span class="wc-word" style="font-size:{size:.2f}rem; color:{color}; '
            f'font-weight:{weight};" title="{freq}회 언급">{html.escape(word)}</span>'
        )
    return f'<div class="wordcloud">{"".join(spans)}</div>'


def _render_logo() -> str:
    """LOGO_PATH 파일이 있으면 보여주고, 없으면 자리를 아예 만들지 않는다 (깨진 이미지 아이콘 방지)."""
    if LOGO_PATH.exists():
        return f'<img class="logo" src="{LOGO_PATH.name}" alt="로고">'
    return ""


def render_landing_page(freqs: list, search_keywords: Optional[list] = None) -> str:
    """진입 화면 HTML을 렌더링한다 (PRD.md 기능3)."""
    return _PAGE_TEMPLATE.format(
        font_stack=FONT_STACK,
        cute_font_face=_CUTE_FONT_FACE_CSS,
        cute_font_name=CUTE_FONT_NAME,
        wordcloud_font_face=_WORDCLOUD_FONT_FACE_CSS,
        wordcloud_font_name=WORDCLOUD_FONT_NAME,
        logo_html=_render_logo(),
        wordcloud_html=render_word_cloud(freqs, search_keywords),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        bg=COLOR_BG,
        card=COLOR_CARD,
        header=COLOR_HEADER,
        accent=COLOR_ACCENT,
        text=COLOR_TEXT,
        text_muted=COLOR_TEXT_MUTED,
        border=COLOR_BORDER,
        error=COLOR_ERROR,
        live_bg=COLOR_LIVE_BG,
        hover=COLOR_HOVER,
        # [추가: 2026-08-03] 세그먼트 위젯 "완성본" 칸 배경 — 버튼 리디자인(시안 B)과 톤 통일
        # 위해 같은 값을 쓰되, 여기서만 쓰는 값이라 공용 COLOR_* 팔레트에는 넣지 않는다.
        tonal_hover="#DCEAFE",
    )


def generate_landing_page(keywords: Optional[list] = None) -> Path:
    """오늘 저장된 모든 회차를 합쳐, 당일 누적 주요 키워드로 home.html을 렌더링한다.

    기사 스크랩(수집)과는 별개 파이프라인이다 — 이미 저장된 회차 데이터를 읽어
    집계만 할 뿐, 새로 수집하지 않는다. generate_screen과 달리, 아직 저장된 회차가
    없어도 예외를 내지 않고 빈 화면을 보여준다 — 진입 화면은 앱이 가장 먼저 여는
    화면이라, 오늘 첫 스크랩 전에도 떠 있어야 한다.

    회차마다 같은 기사가 다시 잡힐 수 있어(회차별 시간창 수집이 적용되기 전에는
    특히), url 기준으로 중복 제거한 뒤 집계한다.
    """
    settings = load_settings()
    keywords = keywords if keywords is not None else all_search_keywords(settings)
    exclude_words = settings.get("wordcloud_exclude_words", [])
    seen_urls: set = set()
    articles: list = []
    for run in load_today_runs():
        for article in filter_hidden(run["articles"]):
            if article["url"] not in seen_urls:
                seen_urls.add(article["url"])
                articles.append(article)
    freqs = extract_keyword_frequencies(articles, keywords, top_n=LANDING_KEYWORD_COUNT, exclude_words=exclude_words)
    html_text = render_landing_page(freqs, keywords)
    atomic_write_text(LANDING_HTML_PATH, html_text)
    return LANDING_HTML_PATH
