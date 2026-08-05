# Design Ref: DESIGN.md §1 설정 화면 목업(메뉴+하위 6페이지), §3 "최소 로컬 서버" (PRD 규칙 17)
import html
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs

from app.config import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_ERROR,
    COLOR_HEADER,
    COLOR_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    FONT_STACK,
    HIGHLIGHT_COLORS,
    HISTORY_HTML_PATH,
    LANDING_HTML_PATH,
    LOGO_PATH,
    MAX_HIGHLIGHT_KEYWORDS,
    MAX_KEYWORD_GROUPS,
    MAX_KEYWORDS_PER_GROUP,
    MAX_SCHEDULE_GROUPS,
    MAX_SCHEDULE_TIMES,
    MAX_WORDCLOUD_EXCLUDE_WORDS,
    MIN_SCHEDULE_TIMES,
    OUTPUT_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.classifier import classify_articles, snapshot_group_names
from app.curation import (
    bulk_move_articles,
    bulk_reassign_group,
    display_name_in_use,
    filter_hidden,
    hide_article,
    load_group_overrides,
    load_hidden_records,
    move_article,
    set_group_label,
    set_group_override,
    unhide_article,
)
from app.custom_groups import add_custom_group, load_custom_groups, remove_custom_group
from app.group_order import save_group_order
from app.draft_articles import add_draft_pending_article
from app.history_renderer import generate_history_page
from app.landing_renderer import generate_landing_page
from app.live_renderer import generate_live_page
from app.manual_articles import add_manual_article, pop_manual_article
from app.manual_keyword_note import save_manual_keyword_note
from app.naver_api import OUTLET_CATEGORIES, fetch_full_title_and_summary, outlet_display_label
from app.summary_overrides import set_summary_override
from app.preview_renderer import generate_preview_page, preview_bulk_move_articles, preview_move_article
from app.renderer import apply_line_template, generate_screen
from app.settings import (
    SettingsError,
    add_highlight_keyword,
    all_search_keywords,
    cycle_highlight_color,
    load_settings,
    move_outlet,
    save_article_line_template,
    save_highlight_keywords,
    save_keyword_groups,
    save_outlet_selection,
    save_scrap_page_settings,
    save_schedule_groups,
    save_telegram_settings,
    save_wordcloud_exclude_words,
    toggle_highlight_keyword,
)
from app.telegram_bot import is_configured as telegram_is_configured
from app.telegram_bot import send_text as telegram_send_text
from app.storage import list_all_runs, load_latest_run, update_run_articles

# 페이지마다 공통으로 쓰는 스타일 조각. 아직 .format()으로 값을 채우기 전(중괄호가 전부
# {{ }}로 이중화된 상태)이라, 각 페이지 템플릿에 문자열로 이어붙인 뒤 페이지 전체를
# 한 번에 .format()해야 한다 — 미리 풀어서 이어붙이면 CSS의 실제 중괄호가 남아
# 바깥쪽 .format() 호출과 충돌한다.
_BASE_STYLE = """
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 480px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: 8px;
  }}
  /* [수정: 2026-07-26] 모든 설정 세부 페이지에 상단(뒤로/HOME)·하단(저장) 고정 바를
     적용하면서, 내용이 그 바에 가리지 않도록 컨테이너 위쪽에 여백을 더했다. 하단
     여백(저장 버튼 있는 페이지만 필요)은 페이지마다 필요할 때만 덧붙인다. */
  .container {{ padding-top: 60px; }}
  h1 {{ font-size: 1.3rem; color: {header}; }}
  .hint {{ color: {muted}; font-size: 0.85rem; margin-bottom: 16px; }}
  .caption {{ color: {muted}; font-size: 0.78rem; margin: 0 0 12px; }}
  .error {{ color: {error}; margin: 12px 0; }}
  .topbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 20;
    background: {card}; border-bottom: 1px solid {border}; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  }}
  .topbar-inner {{
    max-width: 480px; margin: 0 auto; padding: 12px 24px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .topbar a {{ color: {accent}; text-decoration: none; font-size: 0.92rem; font-weight: 600; padding: 6px 10px; border-radius: 6px; }}
  .topbar a:hover {{ background: {hover}; }}
  .save-bar {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  }}
  .save-bar-inner {{ max-width: 480px; margin: 0 auto; padding: 12px 24px; }}
  .save-bar button {{ width: 100%; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유 — button은 a와 달리 font-family를
     상속받지 않고, appearance:auto(네이티브 OS 버튼 껍데기)까지 남아있어 폰트만 맞춰선
     완전히 똑같이 안 보인다. 크롬이 button 텍스트만 내부적으로 수직 중앙 정렬해주는
     것까지 발견해 align-items: center를 직접 지정했다. */
  button, a.btn {{
    background: {accent}; color: #ffffff; border: none; border-radius: 4px;
    padding: 8px 16px; font-size: 1rem; font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-flex; align-items: center; justify-content: center;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  button:disabled {{ background: {border}; color: {muted}; cursor: not-allowed; }}
  input[type=text], input[type=number] {{
    background: {bg}; color: {text}; border: 1px solid {border}; border-radius: 4px;
    padding: 6px 10px; font-size: 1rem;
  }}
  .del-btn {{
    background: transparent; color: {muted}; border: 1px solid {border}; border-radius: 4px;
    padding: 4px 10px; font-size: 0.8rem; margin-left: 6px; cursor: pointer;
  }}
  .del-btn:hover {{ background: {hover}; color: {error}; border-color: {error}; }}
"""

# [수정: 2026-07-26] 페이지 맨 아래 있던 settings/home 링크를 상단 고정 바로 옮겼다 —
# 긴 페이지에서 스크롤 없이 바로 나갈 수 있게 하려는 목적(저장 버튼을 하단 고정으로
# 옮긴 것과 같은 이유). [수정: 2026-07-26] HOME을 왼쪽, 설정을 오른쪽으로 —
# 모바일 앱의 "왼쪽=뒤로가기"보다, 다음/네이버 같은 웹사이트의 "왼쪽=홈(로고)"
# 관습이 이 앱(브라우저 화면)에 더 맞는다는 판단. "HOME"은 진입 화면으로,
# "설정"은 설정 메뉴(/)로 이동한다.
_TOP_BAR_HTML = (
    '<div class="topbar"><div class="topbar-inner">'
    '<a href="{index_href}">홈</a>'
    '<a href="/">설정</a>'
    "</div></div>"
)

# [수정: 2026-07-27] 숨긴 기사 관리 화면은 설정으로 갈 일이 없다 — 여기서 되돌리기(↩️)를
# 누른 뒤에는 바로 스크랩 결과 화면으로 돌아가는 게 자연스러워서 오른쪽 링크를
# "스크랩 보기"(index.html)로 바꿨다.
# [수정: 2026-08-05] 왼쪽도 "홈"(home.html)이 아니라 "📝 스크랩 초안"(preview.html)으로
# 바꿨다 — 숨긴 기사 관리에서 되돌리기 후 주로 가는 곳은 초안 아니면 완성본이지, 로고만
# 있는 진입 화면(홈)으로 갈 일은 거의 없다는 피드백.
_HIDDEN_TOP_BAR_HTML = (
    '<div class="topbar"><div class="topbar-inner">'
    '<a href="{preview_href}">📝 스크랩 초안</a>'
    '<a href="{scrap_href}">📗 스크랩 완성본</a>'
    "</div></div>"
)

_MENU_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>설정</title>
<style>"""
    + _BASE_STYLE
    + """
  /* [수정: 2026-08-03] 이모지 아이콘 대신 단색 이니셜 카드로(디자인 시안 B, 사용자 선택) —
     항목마다 색이 다른 이모지가 붙어 화면이 알록달록했던 것을, 남색 톤 하나로 통일한
     이니셜 배지 + 옅은 회색 카드 + 화살표로 바꿨다. */
  .menu a {{
    display: flex; align-items: center; gap: 11px; margin: 6px 0; padding: 12px 12px;
    font-size: 0.95rem; font-weight: 500; color: {text}; text-decoration: none;
    border-radius: 8px; background: {bg};
  }}
  .menu a:hover {{ background: {hover}; }}
  .menu-icon {{
    width: 26px; height: 26px; border-radius: 7px; background: #E9EEF6; color: {header};
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    font-size: 0.85rem; font-weight: 700;
  }}
  .menu-chev {{ margin-left: auto; color: {border}; font-size: 0.9rem; }}
  /* [수정: 2026-07-26] 메뉴를 기능별 소제목으로 재편했다 — 검색 키워드/형광펜 긋기는
     소제목 없이 맨 위(점선 구분선만), 나머지는 "실시간 기사 현황"/"뉴스 스크랩"/"기타"
     소제목 아래로 묶는다. /outlets 페이지의 카테고리 이름과 같은 스타일이되, 굵게는
     하지 않는다(사용자 요청). 숨긴 기사 관리는 스크랩 화면 상단의 🗑️로 바로 갈 수
     있어 메뉴에서는 없앴다. */
  .ungrouped {{ margin-bottom: 22px; padding-bottom: 14px; border-bottom: 1px dashed {border}; }}
  .category {{ margin: 22px 0 0; }}
  .category:first-of-type {{ margin-top: 0; }}
  .category-name {{
    color: {header}; font-size: 0.95rem; margin-bottom: 8px;
    padding-bottom: 4px; border-bottom: 1px solid {border};
  }}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <a href="{index_href}">홈</a>
</div></div>
<div class="container">
  <h1>⚙️ 설정</h1>
  <div class="menu ungrouped">
    <a href="/keywords"><span class="menu-icon">키</span>검색 키워드<span class="menu-chev">›</span></a>
    <a href="/highlight"><span class="menu-icon">형</span>형광펜 긋기<span class="menu-chev">›</span></a>
  </div>
  <div class="category">
    <div class="category-name">뉴스 스크랩</div>
    <div class="menu">
      <a href="/schedule"><span class="menu-icon">시</span>수집 시간<span class="menu-chev">›</span></a>
      <a href="/outlets"><span class="menu-icon">언</span>언론사 선택 · 순서 지정<span class="menu-chev">›</span></a>
      <a href="/scrap-page"><span class="menu-icon">범</span>수집 범위<span class="menu-chev">›</span></a>
      <a href="/format"><span class="menu-icon">형</span>본문 형식<span class="menu-chev">›</span></a>
    </div>
  </div>
  <div class="category">
    <div class="category-name">기타</div>
    <div class="menu">
      <a href="/wordcloud-exclude"><span class="menu-icon">워</span>워드클라우드 제외어<span class="menu-chev">›</span></a>
      <a href="/telegram"><span class="menu-icon">텔</span>텔레그램 전송<span class="menu-chev">›</span></a>
    </div>
  </div>
</div>
</body>
</html>
"""
)

_KEYWORD_GROUPS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>검색 키워드</title>
<style>"""
    + _BASE_STYLE
    + """
  .keyword-row {{ margin: 8px 0; display: flex; align-items: center; gap: 8px; }}
  .keyword-row input[type=text] {{ width: 220px; }}
  .keyword-row:has(input[type=checkbox]:not(:checked)) input[type=text] {{
    color: {muted}; border-style: dashed;
  }}
  .mode {{ margin: 12px 0 2px; }}
  .mode label {{ margin-right: 16px; }}
  .group-block {{ border: 1px solid {border}; border-radius: 6px; padding: 14px; margin: 14px 0; }}
  .group-head {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }}
  .group-name-input {{ font-weight: 600; flex: 1; }}
  .add-row button {{ background: {card}; color: {accent}; border: 1px solid {accent}; }}
  .add-row button:hover {{ background: {hover}; }}
  .add-row button:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
  .add-keyword-row {{ margin: 4px 0 0; }}
  .scrap-toggle {{ margin: 14px 0 2px; padding-top: 10px; border-top: 1px solid {border}; }}
  .scrap-toggle label {{ font-size: 0.85rem; color: {muted}; display: flex; align-items: flex-start; gap: 6px; }}
  /* [수정: 2026-07-26] 슬라이딩 스위치 대신 사각 버튼 — 자바스크립트 없이 체크박스
     하나로 배경색만 바뀐다(움직이는 손잡이 없음). OFF면 회색 배경, ON이면 파란
     배경+흰 글자로 "ON"/"OFF" 글자가 그 안에서 바뀐다. */
  .toggle {{ position: relative; display: inline-flex; align-items: center; flex-shrink: 0; cursor: pointer; }}
  .toggle input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .toggle .track {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 36px; height: 24px; background: {border}; border-radius: 4px; transition: background 0.15s;
  }}
  .toggle input:checked ~ .track {{ background: {accent}; }}
  .toggle-text {{ font-size: 0.68rem; font-weight: 700; }}
  .toggle-text.on {{ display: none; color: #ffffff; }}
  .toggle-text.off {{ display: inline; color: {muted}; }}
  .toggle input:checked ~ .track .toggle-text.on {{ display: inline; }}
  .toggle input:checked ~ .track .toggle-text.off {{ display: none; }}
  /* [수정: 2026-07-26] 기본(비활성) 상태는 del처럼 회색 박스를 두르지 않는다 — 형광펜에
     추가되지 않은 대부분의 키워드에 매번 박스가 보이면 오히려 산만하다. 활성(형광펜에
     추가됨) 상태만 형광펜 팔레트 1번(라임) 배경으로 표시해 눈에 띄게 한다. */
  .icon-btn {{
    flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 26px; border: 1px solid transparent; border-radius: 4px;
    background: transparent; font-size: 0.9rem; cursor: pointer;
  }}
  .icon-btn.active {{ border-color: {highlight_active}; background: {highlight_active}; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>🔎 검색 키워드</h1>
  <p class="caption">* 저장 후 다음 스크랩 회차부터 적용됩니다. (실시간 현황은 즉시 반영)</p>
  <p class="hint">
    키워드 그룹은 최대 {max_groups}개, 그룹당 키워드는 최대 {max_keywords_per_group}개까지
    추가할 수 있습니다. 그룹 중 하나라도 조건을 만족하면 실시간 기사 현황에 나옵니다
    (그룹 간 OR). 그룹은 최소 1개는 있어야 하고, "정기 스크랩에도 포함"으로 켠 그룹만
    예정된 회차에 자동으로 수집됩니다(최소 1개 필요). 키워드 앞 ON/OFF는 지우지 않고
    검색에서만 빼고 싶을 때 쓰고, 🖍️는 그 단어를 형광펜에 추가/제거합니다.
  </p>
  {error_html}
  <form method="POST" action="/save">
    {group_blocks}
    <p class="add-row"><button type="submit" formaction="/keywords/add-group"{add_disabled}>+ 키워드 그룹 추가</button></p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{clear_script}
<script>
function toggleHighlight(btn) {{
  fetch("http://{settings_host}:{settings_port}/keywords/toggle-highlight", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{word: btn.dataset.word}})
  }}).then(function(res) {{
    if (!res.ok) {{ alert("형광펜 변경에 실패했습니다. 다시 시도해주세요."); return; }}
    location.reload();
  }}).catch(function() {{
    alert("형광펜 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
</script>
</body>
</html>
"""
)

# [추가: 2026-07-25] 검색 키워드 화면과 완전히 같은 방식(del/+ 추가, 최대 15개) —
# AND/OR 검색 방식 개념만 없다(그냥 제외할 단어 목록일 뿐이라 의미가 없음).
_WORDCLOUD_EXCLUDE_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>워드클라우드 제외어</title>
<style>"""
    + _BASE_STYLE
    + """
  .keyword-row {{ margin: 8px 0; }}
  .keyword-row input {{ width: 220px; }}
  .add-row button {{ background: {card}; color: {accent}; border: 1px solid {accent}; }}
  .add-row button:hover {{ background: {hover}; }}
  .add-row button:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>☁️ 워드클라우드 제외어 (최대 {max_exclude}개)</h1>
  <p class="hint">
    진입 화면 워드클라우드에서 보고 싶지 않은 단어를 등록하세요. 검색 키워드·형광펜
    단어와는 별개입니다. del 버튼으로 바로 비우거나, 직접 지우고 빈 칸으로 둬도 됩니다.
  </p>
  {error_html}
  <form method="POST" action="/save-wordcloud-exclude">
    {exclude_inputs}
    <p class="add-row"><button type="submit" formaction="/wordcloud-exclude/add-slot"{add_disabled}>+ 제외어 추가</button></p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{clear_script}
</body>
</html>
"""
)

_OUTLETS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>언론사 선택 · 순서 지정</title>
<style>"""
    + _BASE_STYLE
    + """
  /* [수정: 2026-07-26] 아래 "선택한 언론사" 순서 목록이 길어지면 저장 버튼을 찾으려고
     맨 위까지 다시 스크롤해야 하는 문제가 있었다. position:sticky는 뒤에 오는 내용이
     많으면(순서 목록) 스크롤 중 실제로 화면에 붙어있지 않아 의도대로 동작하지 않아,
     화면(뷰포트) 자체에 고정되는 position:fixed 바로 바꿨다. 컨테이너 하단 여백은
     이 바에 가려지지 않도록 .container에 padding-bottom을 더했다. */
  .container {{ padding-bottom: 88px; }}
  .filter-bar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px; }}
  .filter-bar input[type=text] {{ flex: 1; min-width: 140px; }}
  .filter-bar button {{
    padding: 6px 12px; font-size: 0.85rem; background: {card}; color: {accent}; border: 1px solid {accent};
  }}
  .filter-bar button:hover {{ background: {hover}; }}
  .filter-bar label {{
    font-size: 0.85rem; color: {muted}; display: flex; align-items: center; gap: 4px; white-space: nowrap;
  }}
  .category {{ margin: 18px 0; }}
  .category.is-hidden {{ display: none; }}
  .category-name {{
    color: {header}; font-size: 0.95rem; font-weight: 700; margin-bottom: 8px;
    padding-bottom: 4px; border-bottom: 1px solid {border};
  }}
  .category-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 12px; }}
  .category-grid label {{ white-space: nowrap; }}
  .category-grid label.is-hidden {{ display: none; }}
  .order-list {{ margin-top: 16px; }}
  .order-row {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; flex-wrap: wrap; }}
  .order-row .order-num {{ width: 28px; color: {muted}; }}
  .order-row .name {{ width: 140px; }}
  .order-row button {{ padding: 2px 10px; font-size: 0.85rem; white-space: nowrap; }}
  .order-row button:disabled {{ background: {border}; color: {muted}; cursor: not-allowed; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📰 언론사 선택 · 순서 지정</h1>
  {error_html}
  <form method="POST" action="/save-outlets">
    <div class="filter-bar">
      <input type="text" id="outlet-search" placeholder="언론사 검색" oninput="filterOutlets()">
      <button type="button" onclick="selectAllOutlets()">전체 선택</button>
      <button type="button" onclick="deselectAllOutlets()">전체 해제</button>
      <label><input type="checkbox" id="show-selected-only" onchange="filterOutlets()"> 선택된 항목만 보기</label>
    </div>
    {outlet_categories}
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
  {outlet_order_html}
</div>
{outlet_order_script}
{outlet_filter_script}
</body>
</html>
"""
)

# [추가: 2026-07-24] 검색 키워드/스크랩 시간대/형광펜 단어 칸 옆 del 버튼을 누르면 그
# 칸(줄) 자체를 화면에서 없앤다 — 서버에는 아직 저장하지 않고(기존과 같은 "저장" 버튼
# 흐름 그대로) 화면에서만 지운다. .keyword-row는 검색 키워드·형광펜 단어 화면이 공유하는
# 클래스, .time-row는 스크랩 시간대 화면. [수정: 2026-07-24] 처음엔 칸 값만 비웠는데,
# "칸 자체가 안 없어진다"는 피드백으로 row.remove()로 바꿨다.
_CLEAR_FIELD_SCRIPT = """<script>
function removeRow(btn) {
  var row = btn.closest(".keyword-row, .time-row, .word-row");
  if (row) row.remove();
}
function removeGroup(btn) {
  var group = btn.closest(".group-block");
  if (group) group.remove();
}
</script>"""

# [추가: 2026-07-24] ↑/↓ 클릭마다 즉시 서버에 저장하는 건 그대로 두되(안전하게 바로
# 반영), 클릭할 때마다 페이지 전체가 새로고침되며 깜빡이던 걸 없앤다 — index.html의
# 🗑️ 버튼과 같은 방식(fetch로 보내고 화면은 자바스크립트로 그 자리에서만 갱신).
# [수정: 2026-07-26] ⇈/⇊(맨 위로/맨 아래로) 버튼은 없앴다 — 목록에 검색/필터가 생겨
# 한 칸씩 옮길 대상을 좁히기 쉬워졌고, 버튼 4개가 가로 공간을 너무 차지한다는 피드백.
# _OUTLETS_TEMPLATE은 .format()을 거치므로, 중괄호를 이중화하지 않도록 이 스크립트는
# 별도 상수로 두고 {outlet_order_script} 자리에 값으로만 끼워 넣는다.
# [수정: 2026-07-26] "맨 위"/"맨 아래" 텍스트 버튼을 다시 추가했다(이번엔 ⇈/⇊ 아이콘이
# 아니라 텍스트로 — 뜻이 더 분명하다는 피드백) — 목록이 길면 한 칸씩 옮기는 게 여전히
# 비효율적이라는 실사용 피드백.
_OUTLET_ORDER_SCRIPT = """<script>
function moveOutlet(btn) {
  var row = btn.closest(".order-row");
  var list = row.parentElement;
  var outlet = btn.dataset.outlet;
  var direction = btn.dataset.direction;
  fetch("/move-outlet", {
    method: "POST", keepalive: true,
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: new URLSearchParams({outlet: outlet, direction: direction})
  }).then(function(res) {
    if (!res.ok) { alert("순서 변경에 실패했습니다. 다시 시도해주세요."); return; }
    // list 안에는 .order-row 말고도 "선택한 언론사 (n개)" 안내문(<p class="hint">)이 함께
    // 들어있어, list.children/firstElementChild를 그대로 쓰면 그 안내문을 기준으로
    // 삼는 사고가 난다. 반드시 .order-row만 따로 추려 그 안에서의 순서로 옮긴다.
    var rows = Array.prototype.slice.call(list.querySelectorAll(".order-row"));
    var idx = rows.indexOf(row);
    if (direction === "top" && idx > 0) {
      list.insertBefore(row, rows[0]);
    } else if (direction === "up" && idx > 0) {
      list.insertBefore(row, rows[idx - 1]);
    } else if (direction === "down" && idx < rows.length - 1) {
      list.insertBefore(rows[idx + 1], row);
    } else if (direction === "bottom" && idx < rows.length - 1) {
      list.appendChild(row);
    }
    renumberOutlets(list);
  }).catch(function() {
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  });
}
function renumberOutlets(list) {
  var rows = list.querySelectorAll(".order-row");
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    row.querySelector(".order-num").textContent = (i + 1) + ".";
    var atTop = i === 0, atBottom = i === rows.length - 1;
    row.querySelector('[data-direction="top"]').disabled = atTop;
    row.querySelector('[data-direction="up"]').disabled = atTop;
    row.querySelector('[data-direction="down"]').disabled = atBottom;
    row.querySelector('[data-direction="bottom"]').disabled = atBottom;
  }
}
</script>"""

# [추가: 2026-07-26] 언론사 목록 전체선택/전체해제/검색/선택된 항목만 보기 — 서버 통신 없이
# 화면에서만 처리한다(체크 상태는 "저장" 눌러야 실제로 반영되는 기존 흐름 그대로).
# 전체 선택/해제는 검색어로 좁혀진(현재 화면에 보이는) 체크박스에만 적용된다 — 예를 들어
# "경제"로 검색한 뒤 전체 선택을 누르면 경제 계열 언론사만 선택된다.
_OUTLET_FILTER_SCRIPT = """<script>
function filterOutlets() {
  var query = document.getElementById("outlet-search").value.trim().toLowerCase();
  var selectedOnly = document.getElementById("show-selected-only").checked;
  var categories = document.querySelectorAll(".category");
  for (var c = 0; c < categories.length; c++) {
    var category = categories[c];
    var labels = category.querySelectorAll(".category-grid label");
    var visibleCount = 0;
    for (var i = 0; i < labels.length; i++) {
      var label = labels[i];
      var checkbox = label.querySelector("input[type=checkbox]");
      var text = label.textContent.trim().toLowerCase();
      var matches = (!query || text.indexOf(query) !== -1) && (!selectedOnly || checkbox.checked);
      label.classList.toggle("is-hidden", !matches);
      if (matches) visibleCount++;
    }
    category.classList.toggle("is-hidden", visibleCount === 0);
  }
}
function selectAllOutlets() {
  setVisibleOutlets(true);
}
function deselectAllOutlets() {
  setVisibleOutlets(false);
}
function setVisibleOutlets(checked) {
  var labels = document.querySelectorAll(".category-grid label:not(.is-hidden)");
  for (var i = 0; i < labels.length; i++) {
    labels[i].querySelector("input[type=checkbox]").checked = checked;
  }
}
</script>"""

_HIGHLIGHT_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>형광펜 단어</title>
<style>"""
    + _BASE_STYLE
    + """
  /* [추가: 2026-07-26] 검색 키워드에 없는 단어도 직접 등록할 수 있는 입력창 —
     아래 단어 목록(색 변경·del)과는 별개 <form>이다(즉시 저장되므로 "저장" 버튼을
     기다릴 필요가 없다). */
  .add-word-form {{ display: flex; gap: 8px; margin-bottom: 18px; }}
  .add-word-form input[type=text] {{ flex: 1; }}
  .add-word-form button {{ background: {card}; color: {accent}; border: 1px solid {accent}; white-space: nowrap; }}
  .add-word-form button:hover {{ background: {hover}; }}
  .word-row {{ display: flex; align-items: center; gap: 10px; padding: 9px 0; border-top: 1px solid {border}; }}
  .word-row:first-of-type {{ border-top: none; }}
  .word-text {{ flex: 1; font-size: 0.92rem; }}
  .color-picker {{ position: relative; flex-shrink: 0; }}
  .color-picker summary {{
    list-style: none; width: 20px; height: 20px; border-radius: 4px; cursor: pointer;
    border: 1px solid {border};
  }}
  .color-picker summary::-webkit-details-marker {{ display: none; }}
  .palette-popover {{
    position: absolute; top: 26px; right: 0; z-index: 5;
    display: flex; gap: 5px; padding: 7px; background: {card};
    border: 1px solid {border}; border-radius: 6px;
  }}
  .palette-popover .swatch {{
    width: 18px; height: 18px; border-radius: 4px; cursor: pointer;
    border: 2px solid transparent; display: inline-block;
  }}
  .palette-popover .swatch.selected {{ border-color: {text}; }}
  .empty-hint {{ text-align: center; color: {muted}; font-size: 0.85rem; padding: 24px 0; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>💡 형광펜 단어</h1>
  <p class="caption">최대 {max_highlight}개까지 추가할 수 있습니다. 검색 키워드에 없는 단어도 직접 추가할 수 있습니다.</p>
  {error_html}
  <form method="POST" action="/highlight/add-word" class="add-word-form">
    <input type="text" name="new_word" value="{new_word_value}" placeholder="추가할 단어 입력">
    <button type="submit">추가</button>
  </form>
  <form method="POST" action="/save-highlight">
    {word_rows}
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{clear_script}
<script>
function pickHighlightColor(el, colorIndex) {{
  var picker = el.closest(".color-picker");
  picker.querySelectorAll(".swatch").forEach(function (s) {{ s.classList.remove("selected"); }});
  el.classList.add("selected");
  picker.querySelector("summary").style.background = el.style.background;
  picker.querySelector('input[type=hidden][name$="_color"]').value = colorIndex;
  picker.removeAttribute("open");
}}
</script>
</body>
</html>
"""
)


_FORMAT_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>본문 형식</title>
<style>"""
    + _BASE_STYLE
    + """
  input[type=text] {{ width: 100%; max-width: 360px; box-sizing: border-box; }}
  .preview {{ margin-top: 12px; color: {muted}; }}
  code {{ background: {bg}; border: 1px solid {border}; padding: 2px 6px; border-radius: 4px; color: {text}; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📃 본문 형식</h1>
  <p class="hint">
    기사 목록 첫 줄의 형식을 바꿀 수 있습니다.<br>
    <code>{{outlet}}</code>은 언론사명, <code>{{title}}</code>은 기사 제목으로 바뀝니다.<br>
    ※ 두 자리표시자를 각각 정확히 1번씩 포함해야 합니다. (URL 줄·게시일자는 이 설정과 무관합니다)
  </p>
  {error_html}
  <form method="POST" action="/save-format">
    <input type="text" name="line_template" value="{template_value}">
    <p class="preview">미리보기: <code>{preview}</code></p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
</body>
</html>
"""
)


_SCHEDULE_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>스크랩 시간 및 횟수</title>
<style>"""
    + _BASE_STYLE
    + """
  .time-row {{ margin: 8px 0; display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }}
  .time-row input {{ width: 46px; text-align: center; }}
  .time-pair {{ white-space: nowrap; }}
  /* [추가: 2026-07-29] 꺼둔 시간대는 흐리게 — 지운 게 아니라 잠깐 안 쓰는 것뿐이라는 걸
     시각적으로 드러낸다(평일/휴일처럼 상황별로 켜고 끄는 용도). 체크박스 상태만으로
     즉시 반응하도록 :has()를 쓴다(자바스크립트 불필요, 키워드 ON/OFF 행과 같은 방식). */
  .time-row:has(.enabled-toggle input:not(:checked)) {{ opacity: 0.5; }}
  .enabled-toggle {{ display: flex; align-items: center; cursor: pointer; }}
  .enabled-toggle input {{ width: auto; }}
  /* [추가: 2026-07-30] 스크랩 시간대 "그룹"(주중/주말 등) — 검색 키워드 그룹 화면과
     같은 생김새(그룹 카드 안에 여러 줄)를 쓴다. group-block류는 그 화면에선 공용
     _BASE_STYLE이 아니라 그 화면 로컬 스타일이라 여기 다시 정의한다. */
  .group-block {{ border: 1px solid {border}; border-radius: 6px; padding: 14px; margin: 14px 0; }}
  .group-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }}
  .group-name-input {{ font-weight: 600; flex: 1; }}
  .active-radio {{
    flex-shrink: 0; display: flex; align-items: center; gap: 4px; font-size: 0.85rem;
    color: {accent}; font-weight: 600; white-space: nowrap; cursor: pointer;
  }}
  .add-row button {{ background: {card}; color: {accent}; border: 1px solid {accent}; }}
  .add-row button:hover {{ background: {hover}; }}
  .add-row button:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
  .add-time-row {{ margin: 4px 0 0; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>⏰ 스크랩 시간 및 횟수</h1>
  <p class="hint">
    스크랩할 회차마다 시작~종료 시간대를 24시간제 "HH:MM" 형식으로 등록하세요
    (예: 07:30 ~ 09:30). 그 시간대에 게시된 기사만 그 회차에 모입니다.
    그룹당 등록한 개수만큼 하루에 자동 실행됩니다 (그룹당 최소 {min_times}개, 최대
    {max_times}개). del 버튼으로 바로 비우거나, 시작·종료 둘 다 빈 칸으로 둬도 그
    자리는 삭제됩니다.<br>
    시간대 왼쪽 체크박스는 개별 on/off, 그룹 왼쪽 "적용" 라디오는 그룹째 켜고 끕니다
    — "주중"/"주말"처럼 그룹을 나눠두면 상황에 따라 그룹만 바꿔가며 매번 시간대를
    다시 입력할 필요가 없습니다. 그룹은 최대 {max_groups}개까지 만들 수 있고, 그 중
    정확히 1개만 "적용" 상태로 실제 스케줄에 쓰입니다.<br>
    화면 상단 "언론 모니터링 [종료시각] 기준" 제목도 지금 적용 중인 그룹의 종료 시각을
    그대로 따라갑니다.
  </p>
  {error_html}
  <form method="POST" action="/save-schedule">
    {group_blocks}
    <p class="add-row"><button type="submit" formaction="/schedule/add-group"{add_group_disabled}>+ 그룹 추가</button></p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{clear_script}
</body>
</html>
"""
)


# [추가: 2026-07-26] 정기 스크랩(예정된 회차)에 포토/현장 기사를 포함할지 여부 —
# 기본은 현행 유지(제외)이고, 켜면 사진기사도 그대로 수집돼 소제목 분류·AI 요약까지
# 거친다(휴지통으로 직접 골라 지우는 걸 감수하는 옵션).
_SCRAP_PAGE_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>스크랩 페이지</title>
<style>"""
    + _BASE_STYLE
    + """
  .checkbox-row {{ margin: 16px 0; display: flex; align-items: center; gap: 8px; }}
  .checkbox-row label {{ font-size: 1rem; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📄 스크랩 페이지</h1>
  <p class="hint">
    정기 스크랩(예정된 회차) 화면의 기사 범위를 설정합니다. 켜면 해당 기사도 그대로
    수집돼 소제목 분류·AI 요약까지 거치므로, 화면에서 🗑️로 직접 골라 지워야 할 수
    있습니다. 기본은 둘 다 꺼짐(현행 유지 — 항상 제외).
  </p>
  <form method="POST" action="/save-scrap-page">
    <div class="checkbox-row">
      <input type="checkbox" id="include_photo" name="include_photo_in_scrap" value="1"{photo_checked}>
      <label for="include_photo">정기 스크랩에도 포토/현장 기사 포함</label>
    </div>
    <div class="checkbox-row">
      <input type="checkbox" id="include_personnel" name="include_personnel_in_scrap" value="1"{personnel_checked}>
      <label for="include_personnel">정기 스크랩에도 인사 기사 포함</label>
    </div>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
</body>
</html>
"""
)


# [추가: 2026-08-03] 정기 회차 스크랩 완료 시 텔레그램으로도 자동 전송할지 설정하는 화면.
# .env에 토큰/챗아이디가 없으면 켜도 조용히 건너뛰므로(app.telegram_bot.send_text),
# 그 상태를 status_html로 미리 알려줘 "켰는데 왜 안 오지"를 방지한다.
_TELEGRAM_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>텔레그램 전송</title>
<style>"""
    + _BASE_STYLE
    + """
  .checkbox-row {{ margin: 16px 0; display: flex; align-items: center; gap: 8px; }}
  .checkbox-row label {{ font-size: 1rem; }}
  .status {{ margin: 4px 0 20px; padding: 10px 14px; border-radius: 6px; font-size: 0.88rem; }}
  .status-ok {{ background: #EFF6FF; color: {accent}; }}
  .status-warn {{ background: #FEF2F2; color: {error}; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📤 텔레그램 전송</h1>
  <p class="hint">
    정기 스크랩(예정된 회차)이 끝날 때마다 완성본을 텔레그램으로도 보냅니다. 초안 화면의
    "텔레로 보내기" 버튼은 이 설정과 무관하게 항상 켜져 있습니다.
  </p>
  <div class="status {status_class}">{status_text}</div>
  <form method="POST" action="/save-telegram">
    <div class="checkbox-row">
      <input type="checkbox" id="telegram_auto_send" name="telegram_auto_send" value="1"{auto_send_checked}>
      <label for="telegram_auto_send">정기 스크랩 완료 시 텔레그램으로 자동 전송</label>
    </div>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
</body>
</html>
"""
)


_HIDDEN_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>숨긴 기사 관리</title>
<style>"""
    + _BASE_STYLE
    + """
  .hidden-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 10px 0; }}
  .hidden-row .name {{ flex: 1; font-size: 0.95rem; }}
  /* [수정: 2026-07-26] 별도 flex 칸(오른쪽 고정폭)이었던 걸 제목 옆 인라인으로 옮겼다 —
     제목이 두 줄로 넘어가면 가운데 정렬된 시각이 줄바꿈된 글자 위에 겹쳐 보이는
     문제가 있었다. 제목 텍스트 흐름 안에 작게 끼워 넣으면 그럴 일이 없다. */
  .hidden-row .pub-time {{ color: {muted}; font-size: 0.78rem; white-space: nowrap; margin-left: 6px; }}
  .hidden-row form {{ display: inline; flex-shrink: 0; }}
  .hidden-row .undo-btn {{
    font-size: 1.1rem; padding: 4px 10px; background: transparent; border: 1px solid {border};
  }}
  .hidden-row .undo-btn:hover {{ background: {hover}; }}
</style>
</head>
<body>
"""
    + _HIDDEN_TOP_BAR_HTML
    + """
<div class="container">
  <h1>🗑️ 숨긴 기사 관리</h1>
  <p class="caption">🧹 다음 날 0시가 지나면 이 목록은 자동으로 비워집니다.</p>
  <p class="hint">
    화면의 🗑️ 버튼으로 숨긴 기사 목록입니다. 최근에 숨긴 기사가 맨 위에 오고, 원본
    데이터는 지워지지 않으며, ↩️를 누르면 숨긴 기사가 다시 화면에 보이게 됩니다.
  </p>
  {rows_html}
</div>
</body>
</html>
"""
)


def _render_keyword_group_block(
    index: int, group: dict, slots: Optional[int] = None, highlight_words: Optional[set] = None
) -> str:
    """키워드 그룹 하나를 렌더링한다.

    [수정: 2026-07-29] "기관 정보" 그룹만 삭제 버튼·OR/AND 선택 없이 고정 문구로 보여주던
    특수 취급을 없앴다 — 이제 모든 그룹이 완전히 동등하게 삭제 버튼과 OR/AND 선택지를
    가진다("그룹이 하나도 안 남으면 에러" 규칙이 최소 1개는 계속 보장한다).

    slots: 지금 보여줄 키워드 입력칸 개수. 생략하면 "저장된 개수, 최소 1개"로 보여준다
    (그룹을 새로 추가했을 때 칸을 5~7개씩 한꺼번에 보여주면 어지럽다는 피드백으로,
    그룹 안에서도 "+ 키워드 추가" 버튼으로 한 칸씩 늘리는 방식을 쓴다).
    """
    name = html.escape(group.get("name", ""))
    keywords = group.get("keywords", [])
    mode = group.get("mode", "OR")
    disabled_keywords = set(group.get("disabled_keywords", []))
    if slots is None:
        # [수정: 2026-07-25] slots를 명시적으로 넘겨받은 경우엔 그 값을 그대로 믿는다.
        # _parse_group으로 만든 group["keywords"]는 항상 MAX_KEYWORDS_PER_GROUP개짜리
        # (빈 칸은 "") 목록이라, 여기서 len(keywords)를 다시 하한으로 쓰면 슬롯 개수가
        # 항상 최대치로 튀어버리는 버그가 있었다 — "+그룹 추가" 직후 새 그룹까지 7칸이
        # 다 보이던 원인.
        slots = max(len(keywords), 1)
    slots = min(max(slots, 1), MAX_KEYWORDS_PER_GROUP)
    highlight_words = highlight_words or set()
    rows = []
    for k in range(slots):
        value = html.escape(keywords[k]) if k < len(keywords) else ""
        is_on = k >= len(keywords) or keywords[k] not in disabled_keywords
        on_checked = " checked" if is_on else ""
        is_highlighted = bool(value) and keywords[k].lower() in highlight_words if k < len(keywords) else False
        highlight_class = " active" if is_highlighted else ""
        highlight_title = "형광펜에서 빼기" if is_highlighted else "형광펜에 추가"
        highlight_btn = (
            f'<span class="icon-btn{highlight_class}" onclick="toggleHighlight(this)" '
            f'data-word="{value}" title="{highlight_title}">🖍️</span>'
            if value
            else '<span class="icon-btn" style="visibility:hidden;">🖍️</span>'
        )
        rows.append(
            f'<div class="keyword-row">'
            '<label class="toggle" title="검색에 포함할지 켜고 끕니다(삭제 아님)">'
            f'<input type="checkbox" name="group{index}_keyword{k + 1}_on" value="1"{on_checked}>'
            '<span class="track">'
            '<span class="toggle-text on">ON</span>'
            '<span class="toggle-text off">OFF</span>'
            "</span>"
            "</label>"
            f'<input type="text" name="group{index}_keyword{k + 1}" value="{value}" placeholder="키워드 {k + 1}">'
            f"{highlight_btn}"
            f'<button type="button" class="del-btn" onclick="removeRow(this)" title="이 칸 지우기">del</button>'
            f"</div>"
        )
    add_keyword_disabled = " disabled" if slots >= MAX_KEYWORDS_PER_GROUP else ""
    scrap_checked = " checked" if group.get("include_in_scrap") else ""
    or_checked = "checked" if mode == "OR" else ""
    and_checked = "checked" if mode == "AND" else ""
    mode_html = (
        '<div class="mode">검색 방식: '
        f'<label><input type="radio" name="group{index}_mode" value="OR" {or_checked}> 그룹 내 1개라도 포함</label>'
        f'<label><input type="radio" name="group{index}_mode" value="AND" {and_checked}> 그룹 내 모두 포함</label>'
        "</div>"
    )

    return (
        '<div class="group-block">'
        '<div class="group-head">'
        f'<input type="text" class="group-name-input" name="group{index}_name" value="{name}" placeholder="그룹명">'
        f'<button type="button" class="del-btn" onclick="removeGroup(this)" title="이 그룹 지우기">그룹 삭제</button>'
        "</div>"
        + "\n".join(rows)
        + '<p class="add-row add-keyword-row">'
        f'<button type="submit" formaction="/keywords/add-keyword-slot" name="group_index" '
        f'value="{index}"{add_keyword_disabled}>+ 키워드 추가</button>'
        "</p>"
        f"{mode_html}"
        '<div class="scrap-toggle">'
        f'<label><input type="checkbox" name="group{index}_include_in_scrap" value="1"{scrap_checked}> '
        "정기 스크랩(예정된 회차)에도 포함 — 꺼두면 실시간 기사 현황에만 나오고, "
        '"→ 스크랩"으로 직접 옮겨야 정식 반영됩니다.</label>'
        "</div>"
        "</div>"
    )


def _render_group_blocks(
    groups: list, slots_by_group: Optional[dict] = None, highlight_words: Optional[set] = None
) -> str:
    slots_by_group = slots_by_group or {}
    return "\n".join(
        _render_keyword_group_block(i + 1, group, slots_by_group.get(i + 1), highlight_words)
        for i, group in enumerate(groups)
    )


_DEFAULT_VISIBLE_SCHEDULE_SLOTS = 4  # PRD 규칙20 — 처음엔 4개만 보이고, 그 이상은 "+"로 늘린다
# [추가: 2026-07-25] 키워드 그룹 — 저장된 그룹이 없으면 처음엔 그룹 블록을 하나도 안 보여주고
# "+ 키워드 그룹 추가"만 보여준다 (그룹당 키워드 5개는 슬롯을 늘릴 필요 없이 처음부터 다 보여준다).
_DEFAULT_VISIBLE_GROUP_SLOTS = 0


def _split_hhmm(value: str) -> tuple:
    """"HH:MM" 문자열을 (시, 분) 숫자 문자열 쌍으로 나눈다. 형식이 안 맞으면 ("", "")."""
    parts = value.split(":")
    if len(parts) != 2:
        return "", ""
    try:
        return str(int(parts[0])), str(int(parts[1]))
    except ValueError:
        return "", ""


def _render_time_inputs(group_index: int, schedule_times: list, slots: int) -> str:
    # [수정: 2026-07-24] ":"까지 직접 타이핑해야 해서 불편하다는 피드백으로, 시/분을
    # 숫자 입력칸 2개로 나누고 ":"는 화면에 고정 문구로만 보여준다 — 이용자는 숫자만
    # 입력하면 된다. 종료 시각이 화면 헤더·저장 파일명의 기준(run_slot)이고, 시작
    # 시각은 이 회차가 모을 시간창의 하한이다 (PRD 규칙 2 — 회차별 시간창 수집).
    # [수정: 2026-07-30] 시간대 그룹 도입 — 입력칸 이름을 group{group_index}_...로
    # 묶어서, 같은 화면 안에 여러 그룹의 시간대 칸이 섞여도 폼에서 서로 안 겹친다.
    rows = []
    for i in range(slots):
        window = schedule_times[i] if i < len(schedule_times) else {}
        start_h, start_m = _split_hhmm(window.get("start", ""))
        end_h, end_m = _split_hhmm(window.get("end", ""))
        # [추가: 2026-07-29] 시간대 개별 on/off — 평일/휴일처럼 상황에 따라 켜고 끌 시간대를
        # 지우지 않고 남겨둘 수 있다(다시 켤 때 시작~종료를 다시 입력할 필요가 없음).
        # 새로 만드는 빈 칸(window가 {})은 기본 켜짐으로 보여준다.
        checked = "" if window.get("enabled", True) is False else " checked"
        rows.append(
            f'<div class="time-row">'
            f'<label class="enabled-toggle" title="이 시간대 켜기/끄기">'
            f'<input type="checkbox" name="group{group_index}_enabled{i + 1}"{checked}></label>'
            f'<span class="time-pair">'
            f'<input type="number" min="0" max="23" placeholder="07" name="group{group_index}_start{i + 1}_h" value="{start_h}">'
            f':<input type="number" min="0" max="59" placeholder="30" name="group{group_index}_start{i + 1}_m" value="{start_m}">'
            f'</span> ~ '
            f'<span class="time-pair">'
            f'<input type="number" min="0" max="23" placeholder="09" name="group{group_index}_end{i + 1}_h" value="{end_h}">'
            f':<input type="number" min="0" max="59" placeholder="30" name="group{group_index}_end{i + 1}_m" value="{end_m}">'
            f'</span>'
            f'<button type="button" class="del-btn" onclick="removeRow(this)" title="이 시간대 지우기">del</button>'
            f"</div>"
        )
    return "\n".join(rows)


def _render_schedule_group_block(group_index: int, group: dict, slots: Optional[int] = None) -> str:
    """스크랩 시간대 그룹 하나(예: "주중"/"주말")를 렌더링한다.

    [추가: 2026-07-30] 검색 키워드 그룹 화면과 같은 생김새(그룹명 입력 + "그룹 삭제" +
    그 안의 여러 줄)를 쓰되, 맨 앞에 라디오 버튼이 하나 더 있다 — 이 그룹을 "지금
    적용 중"으로 켤지 정하는 용도로, name="active_group"이 전체 그룹에서 공유되므로
    브라우저가 알아서 한 번에 1개만 선택되게 해준다(자바스크립트로 서로 배타적으로
    만들 필요가 없음).
    """
    name = html.escape(group.get("name", ""))
    times = group.get("times", [])
    if slots is None:
        slots = max(len(times), 1)
    slots = min(max(slots, 1), MAX_SCHEDULE_TIMES)
    active_checked = " checked" if group.get("active") else ""
    add_time_disabled = " disabled" if slots >= MAX_SCHEDULE_TIMES else ""
    return (
        '<div class="group-block">'
        '<div class="group-head">'
        f'<label class="active-radio" title="이 그룹을 지금 적용">'
        f'<input type="radio" name="active_group" value="{group_index}"{active_checked}> 적용</label>'
        f'<input type="text" class="group-name-input" name="group{group_index}_name" value="{name}" placeholder="그룹명 (예: 주중)">'
        f'<button type="button" class="del-btn" onclick="removeGroup(this)" title="이 그룹 지우기">그룹 삭제</button>'
        "</div>"
        + _render_time_inputs(group_index, times, slots)
        + '<p class="add-row add-time-row">'
        f'<button type="submit" formaction="/schedule/add-slot" name="group_index" '
        f'value="{group_index}"{add_time_disabled}>+ 시간대 추가</button>'
        "</p>"
        "</div>"
    )


def _render_highlight_row(index: int, item: dict) -> str:
    """형광펜 단어 한 줄 — 단어는 읽기 전용(수정은 검색 키워드 화면에서), 색은 접이식
    팔레트(<details>/<summary>, 자바스크립트 없이도 펼침/접힘)로 고르고, del로 뺀다."""
    word = html.escape(item["word"])
    current_color = item.get("color", 0)
    swatches = []
    for color_index, hex_value in enumerate(HIGHLIGHT_COLORS):
        selected = " selected" if color_index == current_color else ""
        swatches.append(
            f'<span class="swatch{selected}" style="background:{hex_value};" '
            f'onclick="pickHighlightColor(this, {color_index})"></span>'
        )
    return (
        '<div class="word-row">'
        f'<input type="hidden" name="highlight{index}_word" value="{word}">'
        f'<span class="word-text">{word}</span>'
        '<details class="color-picker">'
        f'<summary style="background:{HIGHLIGHT_COLORS[current_color]};"></summary>'
        f'<div class="palette-popover">{"".join(swatches)}</div>'
        f'<input type="hidden" name="highlight{index}_color" value="{current_color}">'
        "</details>"
        f'<button type="button" class="del-btn" onclick="removeRow(this)" title="형광펜에서 빼기">del</button>'
        "</div>"
    )


def _render_highlight_rows(items: list) -> str:
    if not items:
        return '<p class="empty-hint">아직 등록된 형광펜 단어가 없습니다. 검색 키워드 화면에서 🖍️로 추가해보세요.</p>'
    # 저장하면 같은 색끼리 모여 보이도록 색 인덱스 기준으로 정렬한다(리스트 자체의
    # 저장 순서는 안 바꾼다 — 표시할 때만 이 순서로 보여준다).
    ordered = sorted(items, key=lambda item: item.get("color", 0))
    return "\n".join(_render_highlight_row(i + 1, item) for i, item in enumerate(ordered))


def _render_wordcloud_exclude_inputs(words: list, slots: int) -> str:
    rows = []
    for i in range(slots):
        value = html.escape(words[i]) if i < len(words) else ""
        rows.append(
            f'<div class="keyword-row">'
            f'<input type="text" name="exclude{i + 1}" value="{value}" placeholder="제외어 {i + 1}">'
            f'<button type="button" class="del-btn" onclick="removeRow(this)" title="이 칸 지우기">del</button>'
            f"</div>"
        )
    return "\n".join(rows)


def _render_outlet_categories(selected: list) -> str:
    """카테고리별 언론사 체크박스를 렌더링한다 (PRD.md 기능1 규칙 16).

    [수정: 2026-07-26] 카테고리 이름은 <꺾쇠> 대신 소제목 스타일로, 체크박스는 flex
    줄바꿈 대신 2열 그리드로 통일해 훑어보기 쉽게 했다.
    """
    selected_set = set(selected)
    blocks = []
    for category, names in OUTLET_CATEGORIES.items():
        checkboxes = "\n".join(
            f'<label><input type="checkbox" name="outlet" value="{html.escape(name)}"'
            f'{" checked" if name in selected_set else ""}> {html.escape(name)}</label>'
            for name in names
        )
        blocks.append(
            f'<div class="category"><div class="category-name">{html.escape(category)}</div>'
            f'<div class="category-grid">{checkboxes}</div></div>'
        )
    return "\n".join(blocks)


def _render_outlet_order(order: list) -> str:
    """현재 선택된 언론사를 순서대로, ↑/↓·맨 위/맨 아래 버튼과 함께 보여준다 (드래그
    앤 드롭 대신 가벼운 방식 — 드래그 정렬은 검토 후 폐기).

    [수정: 2026-07-24] 버튼을 <form> 제출이 아니라 자바스크립트(moveOutlet, 같은 파일의
    _OUTLET_ORDER_SCRIPT)로 처리한다 — 클릭마다 서버에는 그대로 즉시 저장되지만(안전),
    페이지 전체가 새로고침되며 깜빡이던 걸 없애고 그 줄만 화면에서 바로 옮긴다.
    [수정: 2026-07-26] 맨 위/맨 아래 버튼을 없앴다가(가로 공간 절약), 목록이 길면 한
    칸씩 옮기는 게 여전히 번거롭다는 피드백으로 다시 추가했다 — 이번엔 ⇈/⇊ 아이콘이
    아니라 "맨 위"/"맨 아래" 텍스트로(뜻이 더 분명함). "현재 선택 순서" 안내문은
    "선택한 언론사 (n개)"로 바꿔 현재 개수가 바로 보이게 했다.
    """
    if not order:
        return ""
    rows = []
    last_index = len(order) - 1
    for i, name in enumerate(order):
        escaped = html.escape(name)
        at_top = i == 0
        at_bottom = i == last_index

        def _move_button(direction: str, label: str, title: str, disabled: bool) -> str:
            return (
                f'<button type="button" class="order-btn" data-outlet="{escaped}" '
                f'data-direction="{direction}" onclick="moveOutlet(this)" title="{title}"'
                f'{" disabled" if disabled else ""}>{label}</button>'
            )

        buttons = (
            _move_button("top", "맨 위", "맨 위로", at_top)
            + _move_button("up", "↑", "한 칸 위로", at_top)
            + _move_button("down", "↓", "한 칸 아래로", at_bottom)
            + _move_button("bottom", "맨 아래", "맨 아래로", at_bottom)
        )
        rows.append(
            f'<div class="order-row"><span class="order-num">{i + 1}.</span>'
            f'<span class="name">{escaped}</span>{buttons}</div>'
        )
    return f'<div class="order-list"><p class="hint">선택한 언론사 ({len(order)}개)</p>{"".join(rows)}</div>'


def _theme() -> dict:
    """모든 설정 페이지 템플릿이 공유하는 색상·폰트 값. `.format(**_theme(), ...)`로 채운다."""
    return {
        "font_stack": FONT_STACK,
        "bg": COLOR_BG,
        "card": COLOR_CARD,
        "header": COLOR_HEADER,
        "accent": COLOR_ACCENT,
        "text": COLOR_TEXT,
        "muted": COLOR_TEXT_MUTED,
        "border": COLOR_BORDER,
        "hover": COLOR_HOVER,
        "error": COLOR_ERROR,
        # [추가: 2026-07-26] 🖍️ 형광펜 추가 버튼의 활성 상태 음영 — 파란 accent 대신
        # 형광펜 팔레트 1번(라임)을 그대로 써서 "이 버튼 = 형광펜에 들어감"이 색으로
        # 바로 연결되게 한다.
        "highlight_active": HIGHLIGHT_COLORS[0],
    }


# [수정: 2026-07-24] "메인 화면으로" 링크가 예전엔 file:// 경로를 가리켰는데, 최신
# 브라우저(사파리 포함)는 보안상 http 페이지에서 file 경로로의 이동을 막아 버튼이
# 먹통이었다. 그래서 설정 서버가 정적 화면 파일도 같은 127.0.0.1 출처로 함께 서빙하고,
# 이 링크도 file:// 대신 그 http 주소를 가리키도록 바꿨다.
_STATIC_HTML_ROUTES = {
    "/index.html": lambda: OUTPUT_HTML_PATH,
    "/history.html": lambda: HISTORY_HTML_PATH,
    # home.html은 다른 둘과 달리, 열 때마다 당일 누적 워드클라우드를 새로 집계해 보여준다
    # (사용자 요청) — 접속 시점에 매번 generate_landing_page()를 다시 실행한 뒤 그 결과를 서빙.
    "/home.html": generate_landing_page,
    # [추가: 2026-07-25] 실시간 기사 현황 — 열 때마다(새로고침 포함) 당일 0시~현재를
    # 라이브로 다시 검색한다. 자동 새로고침은 없음 — 사용자가 "새로고침" 버튼을 눌러야만
    # 다시 검색한다(app.live_renderer.generate_live_page 문서 참고).
    "/live.html": generate_live_page,
    # [추가: 2026-07-27] 다음 회차 미리보기 — 열 때마다(새로고침 포함) 진행 중인 회차의
    # 시작~현재 시각으로 다시 검색·분류한다. 저장은 하지 않는다(app.preview_renderer 참고).
    "/preview.html": generate_preview_page,
}


def _home_href() -> str:
    # [수정: 2026-07-24] 설정 화면의 "메인 화면으로"는 스크랩 결과 화면(index.html)이
    # 아니라 로고가 있는 진입 화면(home.html)으로 가도록 변경 — 사용자 요청.
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/home.html"


def _scrap_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/index.html"


def _preview_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/preview.html"


def render_menu_page() -> str:
    """설정 메뉴 화면을 렌더링한다 (PRD.md 기능1 규칙 17)."""
    return _MENU_TEMPLATE.format(**_theme(), index_href=_home_href())


def render_keywords_page(
    settings: dict,
    error: str = "",
    groups: Optional[list] = None,
    slots_by_group: Optional[dict] = None,
) -> str:
    """검색 키워드(그룹) 설정 화면을 렌더링한다 (PRD.md 기능1 규칙 15 갱신 — 키워드 그룹).

    groups를 생략하면 저장된 그룹을 그대로 보여준다. "+ 키워드 그룹 추가"/"+ 키워드 추가"·
    저장 실패 시에는 호출자가 지금까지 입력하던(아직 저장 전인) groups·slots_by_group을
    직접 넘긴다.
    """
    display_groups = groups if groups is not None else settings.get("keyword_groups", [])
    highlight_words = {item["word"].lower() for item in settings.get("highlight_keywords", [])}
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _KEYWORD_GROUPS_TEMPLATE.format(
        **_theme(),
        max_groups=MAX_KEYWORD_GROUPS,
        max_keywords_per_group=MAX_KEYWORDS_PER_GROUP,
        error_html=error_html,
        group_blocks=_render_group_blocks(display_groups, slots_by_group, highlight_words),
        add_disabled=" disabled" if len(display_groups) >= MAX_KEYWORD_GROUPS else "",
        clear_script=_CLEAR_FIELD_SCRIPT,
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        index_href=_home_href(),
    )


def render_outlets_page(settings: dict, error: str = "") -> str:
    """언론사 선택 화면을 렌더링한다 (PRD.md 기능1 규칙 16)."""
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    outlet_order = settings.get("outlet_order", [])
    return _OUTLETS_TEMPLATE.format(
        **_theme(),
        error_html=error_html,
        outlet_categories=_render_outlet_categories(outlet_order),
        outlet_order_html=_render_outlet_order(outlet_order),
        outlet_order_script=_OUTLET_ORDER_SCRIPT,
        outlet_filter_script=_OUTLET_FILTER_SCRIPT,
        index_href=_home_href(),
    )


def render_highlight_page(
    settings: dict, error: str = "", items: Optional[list] = None, new_word: str = ""
) -> str:
    """형광펜 단어 설정 화면을 렌더링한다 (PRD.md 기능1 규칙 6).

    [수정: 2026-07-25] 단어 목록의 색 변경·제거는 이 화면에서, 새 단어는 검색 키워드
    화면의 🖍️로만 들어왔었다. [수정: 2026-07-26] 검색 키워드에 없는 단어도 형광펜만
    치고 싶은 경우가 있어, 이 화면 상단에 직접 추가 입력창을 다시 추가했다(다만 이번엔
    "+ 추가" 슬롯 방식이 아니라 즉시 저장되는 별도 폼 — add_highlight_keyword 참고).
    items를 생략하면 저장된 값을 그대로 보여준다(저장 실패 시에만 호출자가 지금 입력하던
    값을 넘긴다). new_word는 추가 입력창에서 에러가 났을 때 방금 입력했던 값을 그대로
    보여주기 위한 것이다.
    """
    display_items = items if items is not None else settings.get("highlight_keywords", [])
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _HIGHLIGHT_TEMPLATE.format(
        **_theme(),
        max_highlight=MAX_HIGHLIGHT_KEYWORDS,
        error_html=error_html,
        new_word_value=html.escape(new_word),
        word_rows=_render_highlight_rows(display_items),
        clear_script=_CLEAR_FIELD_SCRIPT,
        index_href=_home_href(),
    )


def _render_wordcloud_exclude_preview(words: list, slots: int) -> str:
    """"+ 제외어 추가" 직후의 미리보기를 렌더링한다."""
    return _WORDCLOUD_EXCLUDE_TEMPLATE.format(
        **_theme(),
        max_exclude=MAX_WORDCLOUD_EXCLUDE_WORDS,
        error_html="",
        exclude_inputs=_render_wordcloud_exclude_inputs(words, slots),
        add_disabled=" disabled" if slots >= MAX_WORDCLOUD_EXCLUDE_WORDS else "",
        clear_script=_CLEAR_FIELD_SCRIPT,
        index_href=_home_href(),
    )


def render_wordcloud_exclude_page(settings: dict, error: str = "", slots: Optional[int] = None) -> str:
    """워드클라우드 제외어 설정 화면을 렌더링한다.

    [수정: 2026-07-26] slots를 생략하면 "저장된 개수만큼만"(하나도 없으면 빈 칸 1개)
    입력칸을 보여준다 — 예전엔 최소 5개를 항상 보여줘서, 단어가 2개뿐이어도 빈 칸
    3개가 항상 따라다녔다(검색 키워드 그룹의 점진적 표시 방식과도 안 맞았음).
    "+ 제외어 추가" 버튼을 누르면 slots를 1 늘려 다시 렌더링한다.
    """
    words = settings.get("wordcloud_exclude_words", [])
    if slots is None:
        slots = max(len(words), 1)
    slots = min(max(slots, len(words)), MAX_WORDCLOUD_EXCLUDE_WORDS)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _WORDCLOUD_EXCLUDE_TEMPLATE.format(
        **_theme(),
        max_exclude=MAX_WORDCLOUD_EXCLUDE_WORDS,
        error_html=error_html,
        exclude_inputs=_render_wordcloud_exclude_inputs(words, slots),
        add_disabled=" disabled" if slots >= MAX_WORDCLOUD_EXCLUDE_WORDS else "",
        clear_script=_CLEAR_FIELD_SCRIPT,
        index_href=_home_href(),
    )


def _render_schedule_group_blocks(groups: list, slots_by_group: Optional[dict] = None) -> str:
    slots_by_group = slots_by_group or {}
    return "\n".join(
        _render_schedule_group_block(i + 1, group, slots_by_group.get(i + 1))
        for i, group in enumerate(groups)
    )


def render_schedule_page(
    settings: dict, error: str = "", groups: Optional[list] = None, slots_by_group: Optional[dict] = None
) -> str:
    """스크랩 시간대(그룹) 설정 화면을 렌더링한다 (PRD.md 기능1 규칙 20).

    groups를 생략하면 저장된 그룹을 그대로 보여준다. "+ 그룹 추가"/"+ 시간대 추가"·
    저장 실패 시에는 호출하는 쪽이 지금까지 입력하던(아직 저장 전인) groups·
    slots_by_group을 직접 넘긴다(검색 키워드 그룹 화면과 같은 패턴).

    [수정: 2026-07-30] 시간대 그룹(주중/주말 등, 그 중 1개만 활성) 도입 — 그룹마다
    처음엔 _DEFAULT_VISIBLE_SCHEDULE_SLOTS개만큼만 시간대 칸을 보여준다.
    """
    display_groups = groups if groups is not None else settings.get("schedule_groups", [])
    if slots_by_group is None:
        slots_by_group = {
            i + 1: max(len(g.get("times", [])), _DEFAULT_VISIBLE_SCHEDULE_SLOTS)
            for i, g in enumerate(display_groups)
        }
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _SCHEDULE_TEMPLATE.format(
        **_theme(),
        min_times=MIN_SCHEDULE_TIMES,
        max_times=MAX_SCHEDULE_TIMES,
        max_groups=MAX_SCHEDULE_GROUPS,
        error_html=error_html,
        group_blocks=_render_schedule_group_blocks(display_groups, slots_by_group),
        add_group_disabled=" disabled" if len(display_groups) >= MAX_SCHEDULE_GROUPS else "",
        clear_script=_CLEAR_FIELD_SCRIPT,
        index_href=_home_href(),
    )


def render_format_page(settings: dict, error: str = "") -> str:
    """출력 형식 설정 화면을 렌더링한다 (PRD.md 기능1 규칙 5)."""
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    template = settings.get("article_line_template", "")
    # 미리보기는 실제로 저장하기 전에 결과를 보여주는 용도라, 저장 여부와 무관하게
    # 항상 예시 언론사/제목으로 렌더링해본다 (템플릿이 잘못돼 있어도 여기선 안전한
    # apply_line_template의 단순 치환이라 오류가 날 일이 없다).
    preview = apply_line_template(html.escape(template), "KBS", "예시 기사 제목입니다")
    return _FORMAT_TEMPLATE.format(
        **_theme(),
        error_html=error_html,
        template_value=html.escape(template),
        preview=preview,
        index_href=_home_href(),
    )


def render_scrap_page_settings(settings: dict) -> str:
    """스크랩 페이지 설정 화면을 렌더링한다 — 정기 스크랩의 포토/현장·인사 기사 포함 여부를 다룬다."""
    photo_checked = " checked" if settings.get("include_photo_in_scrap", False) else ""
    personnel_checked = " checked" if settings.get("include_personnel_in_scrap", False) else ""
    return _SCRAP_PAGE_SETTINGS_TEMPLATE.format(
        **_theme(), photo_checked=photo_checked, personnel_checked=personnel_checked, index_href=_home_href()
    )


def render_telegram_settings(settings: dict) -> str:
    """텔레그램 자동 전송 설정 화면을 렌더링한다."""
    auto_send_checked = " checked" if settings.get("telegram_auto_send", False) else ""
    if telegram_is_configured():
        status_class, status_text = "status-ok", "✅ .env에 봇 토큰·챗아이디가 설정돼 있습니다."
    else:
        status_class, status_text = (
            "status-warn",
            "⚠️ .env에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다 — 켜도 전송되지 않습니다.",
        )
    return _TELEGRAM_SETTINGS_TEMPLATE.format(
        **_theme(),
        auto_send_checked=auto_send_checked,
        status_class=status_class,
        status_text=status_text,
        index_href=_home_href(),
    )


def _known_articles_by_url() -> dict:
    """숨긴 기사의 언론사·제목을 보여주려고, 최근 7일 회차에서 URL로 원본 정보를 찾는다
    (숨긴 목록 자체는 URL만 저장하므로)."""
    lookup = {}
    for run in list_all_runs():
        for article in run["articles"]:
            lookup.setdefault(article["url"], article)
    return lookup


def _format_pub_hhmm(pub_date: Optional[str]) -> str:
    """게시 시각을 "HH:MM"만 돌려준다(라벨 없이) — 숨긴 기사 관리 화면 전용.

    [추가: 2026-07-26] 자동 스크랩은 그날(00시~24시) 게시된 기사만 대상이라 연·월·일은
    의미가 없고, 숨긴 시각은 어차피 최근 순 정렬이 대신하므로(요청에 따라) 따로
    보여주지 않는다 — 게시 시각 하나만 짧게 보여주는 게 충분하다는 판단.
    """
    if not pub_date:
        return ""
    try:
        return datetime.fromisoformat(pub_date).strftime("%H:%M")
    except ValueError:
        return ""


def render_hidden_page() -> str:
    """숨긴 기사 목록과 되돌리기 버튼을 렌더링한다 (PRD.md 기능1 규칙 19).

    [수정: 2026-07-26] 가장 최근에 숨긴 기사가 맨 위에 오도록 정렬한다
    (app.curation.load_hidden_records가 이미 hidden_at 내림차순으로 정렬해 돌려준다).
    숨긴 시각은 화면에 따로 안 보여준다 — 최근 순 정렬 자체가 "새로 숨긴 것"을 이미
    알려주고, 자동 스크랩이 당일 발행 기사만 대상이라 "언제 숨겼는지"보다 "언제
    게시됐는지"가 더 의미 있다는 판단(놓친 기사는 어차피 담당자가 수작업으로 챙김).
    되돌리기 버튼은 글자 대신 ↩️ 아이콘만 써서 자리를 줄이고, 게시 시각은 제목 옆에
    작게 인라인으로 붙인다(별도 칸으로 두면 제목이 두 줄로 넘어갈 때 겹쳐 보였다).
    [추가: 2026-07-26] 이 목록 자체가 익일 0시가 지나면 자동으로 비워진다
    (app.curation._load_records 참고) — 숨김은 "오늘 화면 정리용"이라는 판단.
    """
    records = load_hidden_records()
    if not records:
        rows_html = '<p class="hint">숨긴 기사가 없습니다.</p>'
    else:
        lookup = _known_articles_by_url()
        rows = []
        for record in records:
            url = record["url"]
            # [수정: 2026-07-27] 숨긴 기록 자체에 outlet/title/pub_date가 있으면(숨긴 시점에
            # 화면에서 그대로 실어 보낸 것) 그걸 우선 쓴다 — 정식 회차 역조회(lookup)로는
            # "다음 회차 초안"에서 숨긴 기사(아직 정식 회차로 저장된 적 없음)를 못 찾아
            # URL만 보이는 문제가 있었다. 기록에 없으면(예전 방식으로 숨긴 기록) 기존대로
            # 정식 회차 역조회로 대체한다.
            info = record if record.get("outlet") else lookup.get(url)
            escaped_url = html.escape(url)
            # [수정: 2026-07-30] outlet_display_label이 이미 이스케이프된 HTML(매일경제는
            # 빨간 글자 span)을 돌려주므로 여기서 다시 html.escape()하지 않는다.
            label = (
                f'({outlet_display_label(info["outlet"])}) {html.escape(info["title"])}'
                if info
                else escaped_url
            )
            pub_hhmm = _format_pub_hhmm(info.get("pub_date")) if info else ""
            pub_html = f'<span class="pub-time">{pub_hhmm}</span>' if pub_hhmm else ""
            rows.append(
                '<div class="hidden-row">'
                f"<span class=\"name\">{label}{pub_html}</span>"
                '<form method="POST" action="/unhide-article">'
                f'<input type="hidden" name="url" value="{escaped_url}">'
                '<button type="submit" class="undo-btn" title="되돌리기">↩️</button>'
                "</form>"
                "</div>"
            )
        rows_html = "\n".join(rows)
    return _HIDDEN_TEMPLATE.format(
        **_theme(), rows_html=rows_html, preview_href=_preview_href(), scrap_href=_scrap_href()
    )


class _SettingsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in _STATIC_HTML_ROUTES:
            self._respond_static_html(_STATIC_HTML_ROUTES[self.path]())
        elif LOGO_PATH.exists() and self.path == f"/{LOGO_PATH.name}":
            self._respond_static_binary(LOGO_PATH, "image/svg+xml")
        elif self.path == "/":
            self._respond(render_menu_page())
        elif self.path == "/keywords":
            self._respond(render_keywords_page(load_settings()))
        elif self.path == "/outlets":
            self._respond(render_outlets_page(load_settings()))
        elif self.path == "/highlight":
            self._respond(render_highlight_page(load_settings()))
        elif self.path == "/format":
            self._respond(render_format_page(load_settings()))
        elif self.path == "/schedule":
            self._respond(render_schedule_page(load_settings()))
        elif self.path == "/hidden":
            self._respond(render_hidden_page())
        elif self.path == "/wordcloud-exclude":
            self._respond(render_wordcloud_exclude_page(load_settings()))
        elif self.path == "/scrap-page":
            self._respond(render_scrap_page_settings(load_settings()))
        elif self.path == "/telegram":
            self._respond(render_telegram_settings(load_settings()))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        # keep_blank_values=True — 기본값(False)이면 빈 문자열 값을 가진 필드를 통째로
        # 빼버려서, "이 칸이 실제로 화면에서 지워졌는지"와 "칸은 있는데 아직 안 채웠는지"를
        # 구분할 수 없게 된다(예: "+ 추가" 핸들러들의 `f"...{i}..." in form` 존재 확인이
        # 빈 칸을 del로 지운 것처럼 잘못 판단함).
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

        if self.path == "/save":
            self._handle_save_keywords(form)
        elif self.path == "/keywords/add-group":
            self._handle_add_keyword_group(form)
        elif self.path == "/keywords/add-keyword-slot":
            self._handle_add_keyword_slot(form)
        elif self.path == "/save-outlets":
            self._handle_save_outlets(form)
        elif self.path == "/move-outlet":
            self._handle_move_outlet(form)
        elif self.path == "/save-highlight":
            self._handle_save_highlight(form)
        elif self.path == "/highlight/add-word":
            self._handle_add_highlight_word(form)
        elif self.path == "/keywords/toggle-highlight":
            self._handle_toggle_highlight(form)
        elif self.path == "/keywords/cycle-highlight-color":
            self._handle_cycle_highlight_color(form)
        elif self.path == "/save-format":
            self._handle_save_format(form)
        elif self.path == "/save-schedule":
            self._handle_save_schedule(form)
        elif self.path == "/schedule/add-group":
            self._handle_add_schedule_group(form)
        elif self.path == "/schedule/add-slot":
            self._handle_add_schedule_slot(form)
        elif self.path == "/move-article":
            self._handle_move_article(form)
        elif self.path == "/rename-group":
            self._handle_rename_group(form)
        elif self.path == "/hide-article":
            self._handle_hide_article(form)
        elif self.path == "/unhide-article":
            self._handle_unhide_article(form)
        elif self.path == "/save-wordcloud-exclude":
            self._handle_save_wordcloud_exclude(form)
        elif self.path == "/wordcloud-exclude/add-slot":
            self._handle_add_wordcloud_exclude_slot(form)
        elif self.path == "/save-scrap-page":
            self._handle_save_scrap_page_settings(form)
        elif self.path == "/add-manual-article":
            self._handle_add_manual_article(form)
        elif self.path == "/unpin-manual-article":
            self._handle_unpin_manual_article(form)
        elif self.path == "/promote-manual-article":
            self._handle_promote_manual_article(form)
        elif self.path == "/promote-manual-article-to-draft":
            self._handle_promote_manual_article_to_draft(form)
        elif self.path == "/preview-move-article":
            self._handle_preview_move_article(form)
        elif self.path == "/bulk-move-article":
            self._handle_bulk_move_article(form)
        elif self.path == "/bulk-move-order":
            self._handle_bulk_move_order(form)
        elif self.path == "/preview-bulk-move-order":
            self._handle_preview_bulk_move_order(form)
        elif self.path == "/refetch-summary":
            self._handle_refetch_summary(form)
        elif self.path == "/edit-summary":
            self._handle_edit_summary(form)
        elif self.path == "/save-manual-keyword-note":
            self._handle_save_manual_keyword_note(form)
        elif self.path == "/add-custom-group":
            self._handle_add_custom_group(form)
        elif self.path == "/remove-custom-group":
            self._handle_remove_custom_group(form)
        elif self.path == "/save-telegram":
            self._handle_save_telegram_settings(form)
        elif self.path == "/telegram-send-draft":
            self._handle_telegram_send_draft(form)
        elif self.path == "/telegram-send-scrap":
            self._handle_telegram_send_scrap(form)
        elif self.path == "/save-group-order":
            self._handle_save_group_order(form)
        else:
            self.send_response(404)
            self.end_headers()

    @staticmethod
    def _parse_group(form: dict, index: int) -> dict:
        keywords = [form.get(f"group{index}_keyword{k + 1}", [""])[0] for k in range(MAX_KEYWORDS_PER_GROUP)]
        # 체크(ON) 해제된 칸의 단어만 disabled_keywords로 옮긴다 — del로 지워진(폼에 아예
        # 없는) 칸은 keywords[k]가 빈 문자열이라 자연히 걸러진다.
        disabled_keywords = [
            keywords[k]
            for k in range(MAX_KEYWORDS_PER_GROUP)
            if keywords[k].strip() and f"group{index}_keyword{k + 1}_on" not in form
        ]
        mode = form.get(f"group{index}_mode", ["OR"])[0]
        return {
            "name": form.get(f"group{index}_name", [""])[0],
            "keywords": keywords,
            "mode": mode,
            "include_in_scrap": f"group{index}_include_in_scrap" in form,
            "disabled_keywords": disabled_keywords,
        }

    @staticmethod
    def _group_slot_count(form: dict, index: int) -> int:
        """지금 화면에 실제로 떠 있던(del로 지워지지 않은) 그 그룹의 키워드 입력칸 개수.
        값 자체(_parse_group)와 달리, 칸이 몇 개 "보이고 있었는지"만 알아야 하는 재렌더링
        용도라 존재 여부만 센다."""
        return sum(1 for k in range(MAX_KEYWORDS_PER_GROUP) if f"group{index}_keyword{k + 1}" in form)

    def _handle_save_keywords(self, form: dict) -> None:
        groups = [self._parse_group(form, g) for g in range(1, MAX_KEYWORD_GROUPS + 1)]
        try:
            save_keyword_groups(groups)
        except SettingsError as error:
            # 검증 실패 시 저장하지 않고, 입력했던 값을 그대로 보여주며 오류 메시지만 추가한다.
            attempted = [g for g in groups if g["name"].strip() or any(k.strip() for k in g["keywords"])]
            self._respond(render_keywords_page(load_settings(), error=str(error), groups=attempted))
            return
        self._redirect("/keywords")

    def _handle_add_keyword_group(self, form: dict) -> None:
        """"+ 키워드 그룹 추가" 버튼 — 저장하지 않고 그룹 블록을 하나 더 보여준다. del 버튼으로
        중간 그룹이 지워졌을 수 있으니, 순서대로 멈추지 않고 MAX_KEYWORD_GROUPS까지 전부
        훑어 실제 존재하는(그룹명 입력칸이 폼에 남아있는) 블록만 모은다."""
        groups = [
            self._parse_group(form, g)
            for g in range(1, MAX_KEYWORD_GROUPS + 1)
            if f"group{g}_name" in form
        ]
        # 기존 그룹들은 지금까지 보여주던 키워드 칸 개수를 그대로 유지한다 — 안 그러면
        # 그룹 하나 더 추가할 때마다 다른 그룹들의 칸이 전부 최대 개수로 늘어나 버린다.
        slots_by_group = {i + 1: self._group_slot_count(form, i + 1) for i in range(len(groups))}
        if len(groups) < MAX_KEYWORD_GROUPS:
            groups.append(
                {
                    "name": "",
                    "keywords": [],
                    "mode": "OR",
                    "include_in_scrap": False,
                    "disabled_keywords": [],
                }
            )
            slots_by_group[len(groups)] = 1
        self._respond(render_keywords_page(load_settings(), groups=groups, slots_by_group=slots_by_group))

    def _handle_add_keyword_slot(self, form: dict) -> None:
        """그룹 안 "+ 키워드 추가" 버튼 — 그 그룹에만 입력칸을 하나 더 보여준다(다른
        그룹·값은 그대로 유지). group_index는 클릭된 버튼 자신의 name=value로 실려온다."""
        try:
            group_index = int(form.get("group_index", ["0"])[0] or 0)
        except ValueError:
            group_index = 0
        groups = [
            self._parse_group(form, g)
            for g in range(1, MAX_KEYWORD_GROUPS + 1)
            if f"group{g}_name" in form
        ]
        slots_by_group = {i + 1: self._group_slot_count(form, i + 1) for i in range(len(groups))}
        if group_index in slots_by_group:
            slots_by_group[group_index] = min(slots_by_group[group_index] + 1, MAX_KEYWORDS_PER_GROUP)
        self._respond(render_keywords_page(load_settings(), groups=groups, slots_by_group=slots_by_group))

    def _handle_save_outlets(self, form: dict) -> None:
        selected = form.get("outlet", [])
        try:
            save_outlet_selection(selected)
        except SettingsError as error:
            attempted = {**load_settings(), "outlet_order": selected}
            self._respond(render_outlets_page(attempted, error=str(error)))
            return
        self._redirect("/outlets")

    def _handle_move_outlet(self, form: dict) -> None:
        """/outlets 화면의 ⇈/↑/↓/⇊ 버튼이 fetch로 호출한다(moveOutlet, _OUTLET_ORDER_SCRIPT).

        [수정: 2026-07-24] 예전엔 폼 제출 + 전체 페이지 리다이렉트였는데, 클릭마다 화면이
        깜빡이는 게 피로하다는 피드백으로 fetch 방식으로 바꿨다. 성공하면 204만 돌려주고
        (화면 갱신은 자바스크립트가 그 자리에서 처리), 실패하면 fetch의 res.ok가 false가
        되도록 400을 돌려준다 — 버튼이 이미 유효한 항목에만, 맨 위/아래에서는 비활성으로
        렌더링되므로 이 실패 경로는 사실상 방어용이다.
        """
        outlet = form.get("outlet", [""])[0]
        direction = form.get("direction", [""])[0]
        try:
            move_outlet(outlet, direction)
        except SettingsError:
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()

    def _handle_save_highlight(self, form: dict) -> None:
        """형광펜 화면의 "저장" — del로 지워진(폼에 없는) 단어는 자연히 빠지고, 남은
        단어들의 색만 그때 고른 값으로 반영한다. 단어 자체는 이 폼에서 새로 만들 수
        없다(검색 키워드 화면의 🖍️로만 추가됨)."""
        items = [
            {
                "word": form.get(f"highlight{i}_word", [""])[0],
                "color": form.get(f"highlight{i}_color", ["0"])[0],
            }
            for i in range(1, MAX_HIGHLIGHT_KEYWORDS + 1)
            if f"highlight{i}_word" in form
        ]
        try:
            save_highlight_keywords(items)
        except SettingsError as error:
            self._respond(render_highlight_page(load_settings(), error=str(error), items=items))
            return
        self._redirect("/highlight")

    def _handle_add_highlight_word(self, form: dict) -> None:
        """형광펜 화면 상단 "추가" — 검색 키워드에 없는 단어도 바로 등록한다
        (app.settings.add_highlight_keyword). 이미 있으면(검색 키워드의 🖍️로 이미
        들어와 있던 경우 포함) 조용히 무시하지 않고 에러 메시지를 보여준다."""
        new_word = form.get("new_word", [""])[0]
        try:
            add_highlight_keyword(new_word)
        except SettingsError as error:
            self._respond(render_highlight_page(load_settings(), error=str(error), new_word=new_word))
            return
        self._redirect("/highlight")

    def _handle_toggle_highlight(self, form: dict) -> None:
        """검색 키워드 화면의 🖍️ 버튼과, 완성본·초안·실시간 화면 하단의 🖍️ 형광펜 팝오버가
        fetch로 호출한다 — 이미 형광펜에 있으면 빼고, 없으면 다음 순번 색으로 추가한다
        (app.settings.toggle_highlight_keyword).

        [수정: 2026-08-03] 버그 수정 — 최대 개수 초과로 실패해도 항상 204(성공)를 돌려줘서,
        호출하는 쪽(toggleHighlight의 !res.ok 분기)이 실패 안내를 절대 보여줄 수 없었다.
        이제 실패하면 400을 돌려줘 호출하는 쪽이 정상적으로 실패를 알 수 있다.
        """
        word = form.get("word", [""])[0]
        if not word.strip():
            self.send_response(400)
        else:
            try:
                toggle_highlight_keyword(word)
                self.send_response(204)
            except SettingsError:
                self.send_response(400)  # 최대 개수 초과
        self.end_headers()

    def _handle_cycle_highlight_color(self, form: dict) -> None:
        """"🖍️ 형광펜 단어 편집" 팝오버의 칩을 클릭하면 fetch로 호출한다
        (cycleChipColor) — 그 단어의 색을 팔레트 다음 순번으로 바꾸고
        (app.settings.cycle_highlight_color), 새 색을 JSON으로 돌려줘 호출하는 쪽이
        새로고침 없이 같은 단어가 나온 모든 자리를 즉시 갱신할 수 있게 한다. 등록되지
        않은 단어(word가 빈 문자열 반환)면 404로 알린다 — 이론상 화면에 있는 단어는
        항상 등록돼 있어야 하지만, 팝오버로 그 사이에 삭제됐을 수도 있다.
        """
        word = form.get("word", [""])[0]
        color_hex = cycle_highlight_color(word) if word.strip() else ""
        if not color_hex:
            self.send_response(404)
            self.end_headers()
            return
        self._regenerate_screens()
        self._respond_json({"color_hex": color_hex})

    def _handle_save_wordcloud_exclude(self, form: dict) -> None:
        words = [form.get(f"exclude{i + 1}", [""])[0] for i in range(MAX_WORDCLOUD_EXCLUDE_WORDS)]
        try:
            save_wordcloud_exclude_words(words)
        except SettingsError as error:
            attempted = {**load_settings(), "wordcloud_exclude_words": [w for w in words if w.strip()]}
            self._respond(render_wordcloud_exclude_page(attempted, error=str(error)))
            return
        self._redirect("/wordcloud-exclude")

    def _handle_add_wordcloud_exclude_slot(self, form: dict) -> None:
        """"+ 제외어 추가" 버튼 — 저장하지 않고 입력칸을 하나 더 보여준다."""
        words = [
            form.get(f"exclude{i + 1}", [""])[0]
            for i in range(MAX_WORDCLOUD_EXCLUDE_WORDS)
            if f"exclude{i + 1}" in form
        ]
        slots = min(len(words) + 1, MAX_WORDCLOUD_EXCLUDE_WORDS)
        self._respond(_render_wordcloud_exclude_preview(words, slots))

    def _handle_save_scrap_page_settings(self, form: dict) -> None:
        """스크랩 페이지의 "정기 스크랩에도 포토/현장·인사 기사 포함" 체크박스를 저장한다."""
        save_scrap_page_settings("include_photo_in_scrap" in form, "include_personnel_in_scrap" in form)
        self._redirect("/scrap-page")

    def _handle_save_telegram_settings(self, form: dict) -> None:
        """"정기 스크랩 완료 시 텔레그램 자동 전송" 체크박스를 저장한다."""
        save_telegram_settings("telegram_auto_send" in form)
        self._redirect("/telegram")

    def _handle_telegram_send_draft(self, form: dict) -> None:
        """초안 화면(preview.html)의 "텔레로 보내기" 버튼이 fetch로 호출한다.

        초안 상태를 서버가 다시 계산하지 않고, 화면이 이미 갖고 있던 PLAIN_TEXT를 그대로
        받아 전송한다 — 재정렬·숨김 직후처럼 화면과 저장된 상태 사이에 미묘한 시차가
        있을 수 있는 상황에서도 "지금 화면에 보이는 그대로"가 항상 보장된다.
        preview.html은 이 서버와 같은 출처(127.0.0.1:{port})라 CORS 헤더가 필요 없다.
        """
        text = form.get("text", [""])[0]
        if text and telegram_send_text(text):
            self.send_response(204)
        else:
            self.send_response(502)
        self.end_headers()

    def _handle_telegram_send_scrap(self, form: dict) -> None:
        """스크랩 완성본(index.html)의 "텔레로 보내기" 버튼이 fetch로 호출한다.

        초안과 동일하게, 서버가 최신 회차를 다시 계산하지 않고 화면의 PLAIN_TEXT를
        그대로 받아 전송한다(_handle_telegram_send_draft와 같은 이유).
        """
        text = form.get("text", [""])[0]
        if text and telegram_send_text(text):
            self.send_response(204)
        else:
            self.send_response(502)
        self.end_headers()

    def _handle_save_group_order(self, form: dict) -> None:
        """소제목 헤더의 ↑/↓ 버튼이 fetch로 호출한다.

        화면(자바스크립트)이 이미 "지금 보이는 소제목 순서에서 인접한 두 개를 맞바꾼"
        전체 순서를 JSON 배열로 계산해 보내주므로, 서버는 그대로 저장하기만 한다
        (app.group_order.save_group_order). 완성본·초안 어느 화면에서 눌렀든 다음 번
        정적 화면 생성에도 반영되도록 _regenerate_screens를 호출한다.
        """
        raw_order = form.get("order", [""])[0]
        try:
            order = json.loads(raw_order)
        except json.JSONDecodeError:
            order = None
        if isinstance(order, list):
            save_group_order(order)
            self._regenerate_screens()
            self.send_response(204)
        else:
            self.send_response(400)
        self.end_headers()

    def _handle_save_format(self, form: dict) -> None:
        template = form.get("line_template", [""])[0]
        try:
            save_article_line_template(template)
        except SettingsError as error:
            attempted = {**load_settings(), "article_line_template": template}
            self._respond(render_format_page(attempted, error=str(error)))
            return
        self._redirect("/format")

    @staticmethod
    def _combine_hhmm(form: dict, prefix: str) -> str:
        """시/분으로 나뉜 입력칸({prefix}_h, {prefix}_m)을 "HH:MM" 문자열로 합친다.

        둘 다 비어 있으면 그 회차는 삭제하려는 것이므로 그대로 빈 문자열을 돌려준다.
        하나만 비어 있거나 숫자가 아니면, 검증 단계(validate_schedule_windows)가 형식
        오류로 자연스럽게 잡아낼 수 있도록 그 값 그대로(빈 칸 포함) 이어 붙여 넘긴다.
        """
        h = form.get(f"{prefix}_h", [""])[0].strip()
        m = form.get(f"{prefix}_m", [""])[0].strip()
        if not h and not m:
            return ""
        try:
            return f"{int(h):02d}:{int(m):02d}"
        except ValueError:
            return f"{h}:{m}"

    def _parse_schedule_group(self, form: dict, group_index: int) -> dict:
        """스크랩 시간대 그룹 하나를 폼에서 읽어온다.

        [추가: 2026-07-30] active_group은 라디오 버튼이라 폼 전체에서 값이 딱 하나만
        실려온다 — 그 값이 이 그룹의 인덱스와 같은지로 활성 여부를 판단한다.
        """
        times = [
            {
                "start": self._combine_hhmm(form, f"group{group_index}_start{i + 1}"),
                "end": self._combine_hhmm(form, f"group{group_index}_end{i + 1}"),
                "enabled": f"group{group_index}_enabled{i + 1}" in form,
            }
            for i in range(MAX_SCHEDULE_TIMES)
        ]
        return {
            "name": form.get(f"group{group_index}_name", [""])[0],
            "times": times,
            "active": form.get("active_group", [""])[0] == str(group_index),
        }

    @staticmethod
    def _schedule_group_slot_count(form: dict, group_index: int) -> int:
        """지금 화면에 실제로 떠 있던(del로 지워지지 않은) 그 그룹의 시간대 입력칸 개수."""
        return sum(1 for i in range(MAX_SCHEDULE_TIMES) if f"group{group_index}_end{i + 1}_h" in form)

    def _handle_save_schedule(self, form: dict) -> None:
        groups = [self._parse_schedule_group(form, g) for g in range(1, MAX_SCHEDULE_GROUPS + 1)]
        try:
            save_schedule_groups(groups)
        except SettingsError as error:
            attempted = [
                g
                for g in groups
                if g["name"].strip() or any(w["start"].strip() or w["end"].strip() for w in g["times"])
            ]
            self._respond(render_schedule_page(load_settings(), error=str(error), groups=attempted))
            return
        self._redirect("/schedule")

    def _handle_add_schedule_group(self, form: dict) -> None:
        """"+ 그룹 추가" 버튼 — 저장하지 않고 그룹 블록을 하나 더 보여준다(검색 키워드
        그룹 화면의 "+ 키워드 그룹 추가"와 같은 패턴). 기존 그룹들은 지금까지 보여주던
        시간대 칸 개수·활성 선택을 그대로 유지한다."""
        groups = [
            self._parse_schedule_group(form, g)
            for g in range(1, MAX_SCHEDULE_GROUPS + 1)
            if f"group{g}_name" in form
        ]
        slots_by_group = {i + 1: self._schedule_group_slot_count(form, i + 1) for i in range(len(groups))}
        if len(groups) < MAX_SCHEDULE_GROUPS:
            groups.append({"name": "", "times": [], "active": False})
            slots_by_group[len(groups)] = 1
        self._respond(render_schedule_page(load_settings(), groups=groups, slots_by_group=slots_by_group))

    def _handle_add_schedule_slot(self, form: dict) -> None:
        """그룹 안 "+ 시간대 추가" 버튼 — 그 그룹에만 입력칸을 하나 더 보여준다(다른
        그룹·값은 그대로 유지). group_index는 클릭된 버튼 자신의 name=value로 실려온다.

        [수정: 2026-07-24] del 버튼으로 중간 칸을 통째로 지울 수 있게 되면서, "1번칸부터
        순서대로 몇 번째까지 채워져 있나"만 세던 예전 방식은 중간에 지워진 칸에서 멈춰
        그 뒤 칸들을 잃어버렸다. 이제 MAX_SCHEDULE_TIMES까지 전부 훑어 실제 존재하는
        칸만 모으므로, 어느 칸이 지워졌든 나머지 칸을 그대로 유지한다.
        """
        try:
            group_index = int(form.get("group_index", ["0"])[0] or 0)
        except ValueError:
            group_index = 0
        groups = [
            self._parse_schedule_group(form, g)
            for g in range(1, MAX_SCHEDULE_GROUPS + 1)
            if f"group{g}_name" in form
        ]
        slots_by_group = {i + 1: self._schedule_group_slot_count(form, i + 1) for i in range(len(groups))}
        if 1 <= group_index <= len(groups):
            slots_by_group[group_index] = min(slots_by_group.get(group_index, 1) + 1, MAX_SCHEDULE_TIMES)
        self._respond(render_schedule_page(load_settings(), groups=groups, slots_by_group=slots_by_group))

    def _handle_move_article(self, form: dict) -> None:
        """index.html의 ↑/↓ 버튼이 fetch로 호출한다 (PRD.md 기능1 규칙 21).

        현재 화면(최신 회차)에 보이는 기사만 대상으로 한다 — 지난 기사는 재분류를
        하지 않아 "그룹 내 순서"라는 개념이 없으므로 버튼 자체를 보여주지 않는다.

        [수정: 2026-07-30] 저장할 때 app.classifier.snapshot_group_names로 각 기사에
        "group" 필드를 다시 붙여서 저장한다 — 이 회차가 나중에 "지난 기사 더보기"로
        넘어갔을 때도(더 이상 최신 회차가 아니게 됐을 때) 지금 이 이동 결과가 반영된
        소제목 구성 그대로 보이게 하기 위해서다. 소제목 경계를 넘는 이동(new_override)은
        기사 순서 자체는 그대로라도 소속 그룹이 바뀐 것이므로, 이 경우에도 스냅샷을
        다시 저장해야 한다(안 그러면 history에서 옛 소제목으로 보임).
        """
        url = form.get("url", [""])[0]
        direction = form.get("direction", [""])[0]
        if url and direction in ("up", "down"):
            run = load_latest_run()
            if run is not None:
                keywords = all_search_keywords(load_settings())
                overrides = load_group_overrides()
                new_articles, new_override = move_article(run["articles"], keywords, url, direction, overrides)
                changed = False
                if new_override is not None:
                    set_group_override(*new_override)
                    overrides = load_group_overrides()
                    changed = True
                if new_articles is not run["articles"] or new_override is not None:
                    snapshot = snapshot_group_names(new_articles, keywords, overrides, load_custom_groups())
                    if update_run_articles(run, snapshot):
                        changed = True
                if changed:
                    self._regenerate_screens()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _handle_rename_group(self, form: dict) -> None:
        """소제목 옆 ✏️ 버튼이 fetch로 호출한다 (PRD.md 기능2 규칙 8).

        name(원래 소제목 단어)은 그대로 두고 표시 이름만 바꾼다 — 분류 로직에는
        영향이 없다.

        [추가: 2026-08-04] 원래 단어가 다른 소제목끼리 이름표만 같아지면 화면엔 완전히
        똑같은 소제목이 두 개로 보인다("기타"·"비판 의견"이 둘 다 "우리부 관련 및
        기타"로 붙어 실제로 발생했던 문제) — display_name_in_use로 막고 409로 알린다.
        [수정: 2026-08-04] 검사 범위를 "이 화면에 지금 같이 떠 있는 소제목"으로 좁혔다 —
        피드백: 회차 안에서 중복이면 안 되지만 회차끼리는(예: 완성본 vs 지난 회차)
        같은 이름이어도 상관없다. active_names는 클라이언트가 지금 그려진 소제목
        원래 단어를 모아 보낸 목록(getAllGroups() JS)이다.
        """
        name = form.get("name", [""])[0]
        label = form.get("label", [""])[0].strip()
        active_names = set(form.get("active_names", []))
        if not name or not label:
            self.send_response(204)
        elif display_name_in_use(label, active_names, exclude_name=name):
            self.send_response(409)
        else:
            set_group_label(name, label)
            self._regenerate_screens()
            self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _handle_hide_article(self, form: dict) -> None:
        """index.html/history.html(file://로 열림)의 🗑️ 버튼이 fetch로 호출한다.
        file:// 오리진은 CORS상 "null"로 취급되므로, 성공/실패를 JS가 읽을 수 있도록
        Access-Control-Allow-Origin을 열어준다 (로컬 단일 사용자 도구라 위험 없음)."""
        url = form.get("url", [""])[0]
        if url:
            outlet = form.get("outlet", [""])[0] or None
            title = form.get("title", [""])[0] or None
            pub_date = form.get("pubDate", [""])[0] or None
            hide_article(url, outlet=outlet, title=title, pub_date=pub_date)
            self._regenerate_screens()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _handle_unhide_article(self, form: dict) -> None:
        url = form.get("url", [""])[0]
        if url:
            unhide_article(url)
            self._regenerate_screens()
        self._redirect("/hidden")

    def _handle_add_manual_article(self, form: dict) -> None:
        """live.html의 "→ 스크랩" 버튼이 fetch로 호출한다 (실시간 기사 현황 → 스크랩).

        live.html은 http://127.0.0.1:{port}/live.html로(같은 출처) 서빙되므로, index.html/
        history.html의 hide-article과 달리 CORS 헤더가 필요 없다.
        """
        outlet = form.get("outlet", [""])[0]
        title = form.get("title", [""])[0]
        url = form.get("url", [""])[0]
        summary = form.get("summary", [""])[0]
        pub_date = form.get("pubDate", [""])[0]
        if outlet and title and url:
            article = {"outlet": outlet, "title": title, "url": url, "summary": summary}
            if pub_date:
                article["pub_date"] = pub_date
            add_manual_article(article)
            self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_unpin_manual_article(self, form: dict) -> None:
        """live.html의 "📌 담아둠" 버튼 재클릭(unpinFromLive)이 fetch로 호출한다.

        promote와 달리 정식 스크랩·초안 예약 어디에도 옮기지 않고 그냥 임시보드
        (manual_articles.json)에서만 뺀다 — "담아두기 자체를 취소"하는 액션이라
        숨기기(hide_article)와 달리 전역 영향이 없다.
        """
        url = form.get("url", [""])[0]
        if url:
            pop_manual_article(url)
            self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_promote_manual_article(self, form: dict) -> None:
        """"직접 추가한 기사" 맨 위 기사의 ↑ 버튼이 fetch로 호출한다(promoteManualArticle).

        manual_articles.json에서 그 기사를 빼서 오늘 최신 회차의 실제 기사 목록에
        더한다 — 이제부터 정식 스크랩 결과(화면·복사/내보내기)에 포함된다. 이미 소제목이
        하나라도 있으면(빈 회차가 아니면) 그 중 마지막 소제목으로 강제 배정해(소제목
        경계를 넘는 다른 기사 이동, app.curation.move_article과 같은 방식) 어디로
        들어갔는지 예측 가능하게 하고, 아직 소제목이 없으면(오늘 첫 기사가 이것뿐이면)
        자동 분류에 맡긴다. 오늘 회차 자체가 없으면(이 화면이 뜰 수 없는 상황) 되돌린다.
        """
        url = form.get("url", [""])[0]
        if url:
            run = load_latest_run()
            if run is None:
                self.send_response(409)
                self.end_headers()
                return
            article = pop_manual_article(url)
            if article is not None:
                keywords = all_search_keywords(load_settings())
                overrides = load_group_overrides()
                custom_names = load_custom_groups()
                # [수정: 2026-07-27] filter_hidden 적용 — app.curation.move_article과 같은 이유로,
                # 숨긴 기사가 여전히 분류에 끼어들면 "화면에 보이는 마지막 소제목"과 실제로 다른
                # 이름으로 승격될 수 있다.
                existing_groups = classify_articles(
                    filter_hidden(run["articles"]), keywords, forced_groups=overrides, custom_group_names=custom_names
                )
                if existing_groups:
                    set_group_override(url, existing_groups[-1]["name"])
                    overrides = load_group_overrides()
                # [수정: 2026-07-30] snapshot_group_names로 저장해, 이 회차가 나중에 "지난 기사
                # 더보기"로 넘어가도 방금 승격된 기사가 제 소제목에 들어간 채로 보이게 한다.
                new_articles = snapshot_group_names(run["articles"] + [article], keywords, overrides, custom_names)
                update_run_articles(run, new_articles)
                self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_promote_manual_article_to_draft(self, form: dict) -> None:
        """"스크랩 초안" 화면의 "직접 추가한 기사" 맨 위 기사 ↑ 버튼이 fetch로 호출한다
        (promoteManualArticleToDraft).

        아직 저장된 회차가 없어(다음 회차가 진행 중일 뿐) _handle_promote_manual_article처럼
        바로 끼워 넣을 곳이 없다 — 그래서 manual_articles.json에서 빼서 예약 목록
        (app.draft_articles)에 옮겨 담아두고, 다음 정식 회차가 실제로 저장될 때
        (app.scraper.collect_run) 자동으로 그 회차 기사 목록에 합쳐지도록 한다. 저장되기
        전에도 초안 화면 자체의 소제목 분류에는 바로 반영된다(app.preview_renderer
        ._compute_preview_articles가 매번 예약 목록을 같이 읽어온다) — 그래서 여기서는
        완성 화면과 달리 정식 회차 파일을 직접 건드리지 않는다.
        """
        url = form.get("url", [""])[0]
        if url:
            article = pop_manual_article(url)
            if article is not None:
                add_draft_pending_article(article)
                self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_preview_move_article(self, form: dict) -> None:
        """미리보기 화면(preview.html)의 ↑/↓ 버튼이 fetch로 호출한다(previewMoveArticle).

        정식 회차처럼 저장된 run이 없으므로 app.preview_renderer.preview_move_article이
        미리보기 목록을 다시 계산해 소제목 경계를 넘는 이동은 group_overrides.json에,
        같은 소제목 내 순서는 preview_order.json에 기록한다.

        [수정: 2026-07-30] 예전엔 204만 응답하고 브라우저가 location.reload()로 다시
        불러오게 했는데, 그 reload가 서버 쪽 전체 재검색을 한 번 더 유발했다(클릭
        한 번에 검색 2번). 이제 preview_move_article이 이동 계산에 쓴 결과로 만든
        화면 조각을 그대로 JSON으로 응답한다 — 브라우저는 reload 없이 DOM만
        갈아끼운다. 더 옮길 곳이 없거나 검색 실패로 계산 자체를 못했으면(None) 예전과
        똑같이 204만 응답해 조용히 무시한다.
        """
        url = form.get("url", [""])[0]
        direction = form.get("direction", [""])[0]
        if url and direction in ("up", "down"):
            content = preview_move_article(url, direction)
            if content is not None:
                self._respond_json(content)
                return
        self.send_response(204)
        self.end_headers()

    def _handle_bulk_move_article(self, form: dict) -> None:
        """스크랩 초안·완성본 양쪽의 체크박스 하단 "일괄 이동" 바 + 기사별 "다른
        소제목으로" 드롭다운이 fetch로 호출한다(bulkMoveSelected/moveArticleToGroup).

        move_article(↑/↓)과 달리 이웃과 스왑하는 게 아니라 그냥 지정한 소제목으로
        바로 배정하는 것뿐이라(app.curation.bulk_reassign_group), 순서 계산은 필요 없다.

        [수정: 2026-07-30] 완성본(index.html)에서도 쓰게 되면서, 최신 회차가 있으면
        그 회차에 저장해둔 소제목 스냅샷(app.classifier.snapshot_group_names)도 같이
        갱신해야 한다 — app.settings_server._handle_move_article과 같은 이유로,
        안 그러면 이 회차가 나중에 "지난 기사 더보기"로 넘어갔을 때 방금 옮긴 결과가
        아니라 옛 소제목으로 보인다. 완성본은 요청마다 새로 계산되는 preview.html과
        달리 정적 파일이라 _regenerate_screens()로 즉시 다시 그려줘야 새로고침 없이도
        (다음 접속에) 반영된다.
        """
        urls = [u for u in form.get("urls", []) if u]
        target = form.get("target", [""])[0].strip()
        if urls and target:
            bulk_reassign_group(urls, target)
            run = load_latest_run()
            if run is not None:
                keywords = all_search_keywords(load_settings())
                overrides = load_group_overrides()
                custom_names = load_custom_groups()
                snapshot = snapshot_group_names(run["articles"], keywords, overrides, custom_names)
                update_run_articles(run, snapshot)
            self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_bulk_move_order(self, form: dict) -> None:
        """완성본(index.html) 체크박스 하단 "일괄 이동" 바의 새 ↑/↓가 fetch로
        호출한다(bulkMoveOrder, 2026-08-04 추가) — 여러 기사를 3~4개씩 체크해 같은
        소제목 안에서 통째로 한 칸 옮기는 용도. app.curation.bulk_move_articles가
        소제목 경계를 넘는 조합이면 아무 것도 안 바꾸므로, 여기서는 결과가 실제로
        달라졌을 때만 저장한다. app.settings_server._handle_move_article과 같은
        이유로 snapshot_group_names를 다시 붙여 저장한다.
        """
        urls = [u for u in form.get("urls", []) if u]
        direction = form.get("direction", [""])[0]
        if urls and direction in ("up", "down"):
            run = load_latest_run()
            if run is not None:
                keywords = all_search_keywords(load_settings())
                overrides = load_group_overrides()
                new_articles = bulk_move_articles(run["articles"], keywords, urls, direction, overrides)
                if new_articles is not run["articles"]:
                    snapshot = snapshot_group_names(new_articles, keywords, overrides, load_custom_groups())
                    if update_run_articles(run, snapshot):
                        self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_preview_bulk_move_order(self, form: dict) -> None:
        """스크랩 초안(preview.html) 쪽의 같은 기능 — app.preview_renderer
        .preview_bulk_move_articles가 실제 계산·저장을 맡는다(완성본과 달리 저장된
        회차가 없어 preview_order.json에 저장한다).
        """
        urls = [u for u in form.get("urls", []) if u]
        direction = form.get("direction", [""])[0]
        if urls and direction in ("up", "down"):
            preview_bulk_move_articles(urls, direction)
        self.send_response(204)
        self.end_headers()

    def _handle_refetch_summary(self, form: dict) -> None:
        """기사 카드에 마우스를 올리면 나타나는 "🔄 원문에서 다시 가져오기" 버튼이
        fetch로 호출한다(refetchSummary, 완성본·초안·실시간 현황 공통) — 네이버 API의
        제목/요약이 사진 설명이나 문장 중간처럼 이상한 지점에서 잘려 있을 때, 그 기사
        원문 페이지의 og:title/og:description으로 그 기사 하나만 다시 가져온다
        (app.naver_api.fetch_full_title_and_summary). 찾은 값이 있으면
        app.summary_overrides에 저장해 어느 화면에서 봐도 계속 반영되게 한다.

        원문에서 아무것도 못 찾았거나(태그 없음) 접속 자체가 실패하면 404로 알린다 —
        자동이 아니라 사용자가 직접 누른 동작이라, 실패를 조용히 무시하지 않고 알려준다.
        """
        url = form.get("url", [""])[0]
        if not url:
            self.send_response(400)
            self.end_headers()
            return
        result = fetch_full_title_and_summary(url)
        if not result or not (result.get("title") or result.get("summary")):
            self.send_response(404)
            self.end_headers()
            return
        set_summary_override(url, result.get("title"), result.get("summary"))
        self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_edit_summary(self, form: dict) -> None:
        """✏️ 직접 수정 버튼이 fetch로 호출한다(editSummary) — 🔄가 원문에서도 못 찾는
        경우의 최후 수단으로, 사용자가 직접 타이핑한 제목·요약을 그대로 저장한다.
        저장 형식·적용 범위는 🔄와 완전히 같다(app.summary_overrides, 부분 저장 —
        비워둔 칸은 기존 값을 그대로 둔다).
        """
        url = form.get("url", [""])[0]
        title = form.get("title", [""])[0].strip()
        summary = form.get("summary", [""])[0].strip()
        if not url or not (title or summary):
            self.send_response(400)
            self.end_headers()
            return
        set_summary_override(url, title or None, summary or None)
        self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_save_manual_keyword_note(self, form: dict) -> None:
        """"+ 직접 키워드 작성하기"의 저장/삭제 버튼이 fetch로 호출한다(saveKeywordNote/
        clearKeywordNote) — AI가 추출한 하단 키워드 블록과 무관한, 이용자가 자유
        서식으로 적어두는 메모 한 줄을 저장한다(app.manual_keyword_note). 완성본은
        정적 파일이라 즉시 다시 그려야 다음 접속에도 바로 보인다(초안·실시간 현황은
        요청마다 새로 계산되므로 별도 처리가 필요 없다).
        """
        text = form.get("text", [""])[0]
        save_manual_keyword_note(text)
        self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_add_custom_group(self, form: dict) -> None:
        """"+ 새 소제목 만들기" 버튼이 fetch로 호출한다(createCustomGroup) — 자동
        분류로는 나오지 않는 이름을 미리 만들어, 기사가 없어도 화면에 띄워둔다.
        초안·완성본 둘 다에서 보여야 하므로(전역 목록) 완성본도 즉시 다시 그린다.

        [추가: 2026-08-04] 새로 만들 이름이 지금 이 화면에 이미 떠 있는 다른 소제목의
        표시 이름과 같으면(app.curation.display_name_in_use) 화면에 똑같은 소제목이
        두 개로 보이니 409로 막는다. [수정: 2026-08-04] 검사 범위는 app.settings_server
        ._handle_rename_group과 같은 이유로 "이 화면에 지금 같이 떠 있는 소제목"으로
        좁혔다 — 회차끼리는 같은 이름이어도 상관없다.
        """
        name = form.get("name", [""])[0].strip()
        active_names = set(form.get("active_names", []))
        if name and display_name_in_use(name, active_names):
            self.send_response(409)
        else:
            add_custom_group(name)
            self._regenerate_screens()
            self.send_response(204)
        self.end_headers()

    def _handle_remove_custom_group(self, form: dict) -> None:
        """빈 소제목 헤더의 🗑️(삭제)가 fetch로 호출한다(removeCustomGroup)."""
        name = form.get("name", [""])[0].strip()
        if name:
            remove_custom_group(name)
            self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _regenerate_screens(self) -> None:
        """숨김 상태가 바뀌면 정적 화면 3개를 즉시 다시 만든다 (새로고침해도 최신 상태가 보이도록).

        아직 저장된 회차가 하나도 없으면 generate_screen만 RuntimeError를 내므로,
        메인 화면 생성 실패와 무관하게 지난 기사·진입 화면은 항상 다시 만든다.
        """
        try:
            generate_screen()
        except RuntimeError:
            pass
        generate_history_page()
        generate_landing_page()

    def _redirect(self, path: str) -> None:
        # Post/Redirect/Get 패턴: 새로고침해도 저장이 중복 제출되지 않도록 리다이렉트한다.
        # 메뉴로 보내지 않고 자기 페이지로 되돌려, 이어서 같은 항목을 계속 손보기 편하게 한다.
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _respond(self, html_text: str, status: int = 200) -> None:
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, data: dict, status: int = 200) -> None:
        """fetch가 reload 없이 화면을 갱신할 수 있게 JSON으로 응답한다
        (app.preview_renderer.preview_move_article 등, JS가 파싱해서 DOM 조각을 갈아끼움)."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_static_html(self, path) -> None:
        """index.html/history.html/home.html을 같은 127.0.0.1 출처로 그대로 서빙한다.

        파일이 아직 없으면(예: 오늘 첫 스크랩 전) 404 대신 안내 문구를 보여준다 —
        설정 화면에서 "메인 화면으로"를 눌렀는데 그냥 죽은 링크처럼 보이지 않도록.
        """
        if not path.exists():
            self._respond('<p style="font-family:sans-serif;padding:24px;">아직 생성된 화면이 없습니다.</p>', status=404)
            return
        self._respond(path.read_text(encoding="utf-8"))

    def _respond_static_binary(self, path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 로컬 단일 사용자 도구라 매 요청을 콘솔에 찍지 않는다.


def run_settings_server(port: int = SETTINGS_SERVER_PORT) -> None:
    """설정 저장용 최소 로컬 서버를 실행한다 (블로킹). 기본은 127.0.0.1에서만 연다.

    [수정: 2026-07-25] 단일 스레드 HTTPServer 대신 ThreadingHTTPServer를 쓴다 — 실시간
    기사 현황(live.html)이 요청마다 여러 키워드를 순차적으로 라이브 검색해 수십 초씩
    걸릴 수 있는데, 단일 스레드였다면 그동안 설정 화면 등 다른 모든 요청이 함께 멈춘다.

    [수정: 2026-07-31] app.config.SETTINGS_SERVER_HOST(.env의 SERVER_HOST)가 기본값
    "127.0.0.1"이 아니면(다른 컴퓨터에서도 접속하도록 의도적으로 설정한 경우) "0.0.0.0"
    으로 리스닝해 실제로 외부 요청을 받아들인다. 기본값 그대로면 예전처럼 127.0.0.1에만
    묶여 이 컴퓨터 자신만 접속 가능한 상태를 그대로 유지한다 — 설정을 안 건드리면
    동작이 하나도 안 바뀌는 게 이 변경의 핵심 전제다.
    """
    bind_host = "0.0.0.0" if SETTINGS_SERVER_HOST != "127.0.0.1" else "127.0.0.1"
    ThreadingHTTPServer((bind_host, port), _SettingsHandler).serve_forever()
