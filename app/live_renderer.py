# Design Ref: 실시간 기사 현황 — 예정된 회차와 별개로 요청 시점마다 당일 0시~현재를 라이브로 재검색
import html
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from app.topnav import regular_nav, topnav_style
from app.config import (
    COLOR_ACCENT,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    FONT_STACK,
    HIGHLIGHT_COLORS,
    LIVE_HEAVY_KEYWORD_MIN_COUNT,
    LIVE_HEAVY_KEYWORD_RATIO,
    LIVE_HTML_PATH,
    PALETTE,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.credentials import naver_is_configured
from app.curation import is_hidden as _article_is_hidden, load_hidden_urls
from app.summary_overrides import apply_summary_overrides
from app.filters import (
    headline_kind as _headline_kind,
    HEADLINE_TAG_RE as _HEADLINE_TAG_RE,
    is_editorial as _is_editorial,
    photo_badge_tip,
    looks_like_photo_caption,
)
from app.highlight import highlight_keywords
from app.icons import icon
from app.live_cache import load_live_cache, save_live_cache
from app.manual_articles import load_manual_articles
from app.naver_api import (
    incremental_search_after,
    kst_today_at,
    match_articles_to_groups,
    outlet_display_label,
    search_keywords,
)
from app.renderer import apply_line_template
from app.scheduler import next_pending_slot
from app.settings import active_schedule_times, active_search_groups, group_in_scrap, load_settings
from app.storage import load_latest_run, load_today_runs

logger = logging.getLogger(__name__)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>전체 기사</title>
{extra_head_html}
<style>
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: var(--r-lg);
  }}
  /* [수정: 2026-07-27] 상단 고정 바 — HOME + 다른 두 화면(초안/완성본)으로 바로 이동.
     하단 고정 바는 🗑️(숨긴 기사 관리) 전용 — 상단이 3개 내비게이션으로 꽉 차서 자리를
     따로 뺐다. */
  /* [수정: 2026-09-16] 카드 위 여백 60→44px — 60px은 상단 고정바(54px)를 피하려는 값인데 글자 위로
     30px이 비었다. 44px이면 16px 남는다. 확정본·초안·실시간·정기 보관함·설정·수시·정책 단어 추이
     일곱 화면이 같은 값을 써야 화면을 오갈 때 제목이 들썩이지 않는다(수시·추이는 84→68px 형태).
     시안 SUBHEAD_SPACING_MOCKUP.html B안. */
  .container {{ padding-top: 44px; padding-bottom: 56px; }}
{topnav_style}
  .bottombar {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  }}
  .bottombar-inner {{
    max-width: 800px; margin: 0 auto; padding: 10px 24px;
    display: flex; justify-content: flex-end;
  }}
  .bottombar a {{ color: {accent}; text-decoration: none; font-size: 1.1rem; padding: 6px 10px; border-radius: var(--r-md); }}
  .bottombar a:hover {{ background: {live_bg}; }}
  /* [추가: 2026-08-03, 정리: 2026-08-21] 하단바 🖍️ 형광펜 편집 팝오버 — 다른 화면과
     같은 파란 hover(COLOR_HOVER)를 쓴다. 이 화면 고유의 붉은 톤은 COLOR_LIVE_BG로
     이름이 따로 있으므로, 예전처럼 색 값을 직접 적을 이유가 없다. */
  .highlight-wrap {{ position: relative; }}
  .highlight-toggle {{
    background: transparent; border: none; color: {muted}; font-size: 1.1rem; cursor: pointer;
    padding: 6px 10px; border-radius: var(--r-md);
  }}
  .highlight-toggle:hover {{ background: {hover}; }}
  .highlight-popover {{
    display: none; position: absolute; bottom: 100%; right: 0; margin-bottom: 8px;
    background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    box-shadow: var(--sh-pop); padding: 12px; width: 220px; z-index: 30;
  }}
  .highlight-popover.is-open {{ display: block; }}
  .highlight-chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
  .highlight-chip {{
    display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: var(--r-pill);
    font-size: var(--fs-sm); color: {text}; cursor: pointer;
  }}
  .highlight-chip button {{
    background: transparent; border: none; padding: 0; font-size: var(--fs-sm); cursor: pointer;
    color: inherit; line-height: 1;
  }}
  .highlight-empty {{ color: {muted}; font-size: var(--fs-sm); margin: 0 0 8px; }}
  .highlight-add-form {{ display: flex; gap: 6px; }}
  .highlight-add-form input {{ flex: 1; font-size: var(--fs-md); padding: 5px 8px; min-width: 0; }}
  .highlight-add-form button {{ font-size: var(--fs-sm); padding: 5px 10px; white-space: nowrap; }}
  .live-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
  .live-badge {{ display: inline-flex; align-items: center; gap: 6px; color: {error}; font-size: var(--fs-xl); font-weight: 600; }}
  .pulse {{ width: 8px; height: 8px; border-radius: var(--r-circle); background: {error}; }}
  .refresh-btn {{
    background: {card}; color: {accent}; border: 1px solid {accent}; border-radius: var(--r-md);
    padding: 7px 14px; font-size: var(--fs-md); font-weight: 500; text-decoration: none; white-space: nowrap;
  }}
  .refresh-btn:hover {{ background: {live_bg}; }}
  /* [추가: 2026-08-13] 최초 검색(콜드 캐시) 중엔 새로고침을 <a href>가 아니라 <span>으로
     그려 아예 눌리지 않게 한다 — app.live_renderer.render_live_loading_page 참고.
     문구로 "누르지 마세요"라고 안내하는 대신 버튼 자체를 못 누르게 만드는 쪽을
     택했다(안내는 무시될 수 있지만 비활성 버튼은 무시될 수 없다). */
  .refresh-btn.is-disabled {{
    background: {card}; color: {muted}; border: 1px solid {muted}; cursor: default;
  }}
  .loading-spin {{ animation: live-spin 1s linear infinite; }}
  @keyframes live-spin {{ to {{ transform: rotate(360deg); }} }}
  /* [추가: 2026-07-26] '스크랩 언론사만 보기' — 서버 재검색 없이 이미 불러온 기사
     중에서 /outlets에 등록된 언론사만 화면에서 걸러 보여준다(노이즈 완화 목적).
     실시간 현황 자체의 "전체 언론사 라이브 미러" 성격은 그대로 유지된다. */
  /* [수정: 2026-09-16] 체크박스 줄(.filter-row)과 검색·그룹 칩 줄(.live-filter-bar)이 각자 테두리를
     두른 두 상자였다(41 + 93px + 사이 여백 35px) — 둘 다 "목록을 좁히는" 같은 일이라 한 상자
     두 줄로 합쳤다. 1행 = 제목 검색 + 체크박스, 2행 = 그룹 칩 + 키워드별 건수. 등록된 검색어
     그룹이 없으면 예전처럼 제목 검색·2행은 안 그리고 체크박스만 남는다. 시안
     LIVE_TOP_COMPACT_MOCKUP.html B안. */
  .filter-box {{
    margin: 14px 0 4px; padding: 10px 14px; background: {bg}; border: 1px solid {border};
    border-radius: var(--r-lg); display: flex; flex-direction: column; gap: 8px;
  }}
  .fb-row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .fb-row label {{ font-size: var(--fs-md); display: flex; align-items: center; gap: 8px; cursor: pointer; white-space: nowrap; }}
  .fb-row label input[type=checkbox] {{ width: var(--chk-sm); height: var(--chk-sm); margin: 0;
    flex: none; accent-color: {accent}; cursor: pointer; }}
  .fb-row label:has(input:disabled) {{ color: {muted}; cursor: not-allowed; }}
  .live-row.is-hidden-by-filter {{ display: none; }}
  /* [추가: 2026-08-13] 실시간 현황 그룹 필터 바 — 제목 검색, 그룹 칩(OR), "아직 안
     담은 것만", 키워드별 건수 펼침. 전부 이미 렌더링된 목록을 클라이언트에서만
     거른다(서버 재검색 없음, applyLiveFilters). */
  .filter-search-input {{ flex: 1 1 190px; min-width: 150px; box-sizing: border-box; font-size: var(--fs-md); padding: 7px 10px; }}
  .chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
  .group-chip {{
    display: inline-flex; align-items: center; gap: 5px; max-width: 160px;
    background: {card}; border: 1px solid {border}; border-radius: var(--r-pill);
    padding: 5px 11px; font-size: var(--fs-sm); color: {text}; cursor: pointer;
  }}
  /* 그룹명이 길어도 칩 폭은 고정 — 자르는 건 화면 표시뿐, 저장된 이름은 그대로다. */
  .group-chip .chip-label {{
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 110px;
  }}
  .group-chip .chip-count {{ color: {muted}; }}
  .group-chip.is-active {{ background: {live_bg}; border-color: {accent}; color: {accent}; }}
  .group-chip.is-active .chip-count {{ color: {accent}; }}
  .group-chip.status-chip {{ font-style: italic; }}
  .kw-breakdown-toggle {{
    background: transparent; border: none; color: {muted}; font-size: var(--fs-sm);
    cursor: pointer; padding: 5px 4px; margin-left: auto;
  }}
  .kw-breakdown-toggle:hover {{ color: {accent}; }}
  .keyword-breakdown {{
    display: none; flex-direction: column; gap: 3px; padding: 8px 10px;
    background: {card}; border: 1px solid {border}; border-radius: var(--r-lg); font-size: var(--fs-sm);
  }}
  .keyword-breakdown.is-open {{ display: flex; }}
  .kw-row {{ display: flex; justify-content: space-between; color: {text}; }}
  .kw-row span:last-child {{ color: {muted}; }}
  /* 기사 행 안의 그룹 태그(칩보다 작음) — 언론사 옆에 나란히 붙는다. */
  .row-group-tag {{
    display: inline-block; margin-left: 6px; max-width: 90px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; vertical-align: -2px;
    padding: 1px 7px; border-radius: var(--r-pill); font-size: var(--fs-xs);
    color: {accent}; background: {live_bg}; border: 1px solid {border};
  }}
  /* [추가: 2026-08-13] app.renderer와 동일한 "⋯ 더보기" 메뉴 — 🔄/✏️를 접어 그룹 칩에
     자리를 내준다(사용자 요청, 이유는 _render_row 주석 참고). */
  .more-wrap {{ position: relative; display: inline-flex; flex-shrink: 0; }}
  .more-btn {{
    background: transparent; border: none; color: {muted}; font-size: 1.05rem; line-height: 1;
    cursor: pointer; padding: 3px 7px; border-radius: var(--r-sm);
  }}
  .more-btn:hover {{ background: {hover}; color: {accent}; }}
  .more-menu {{
    display: none; position: absolute; right: 0; top: 100%; margin-top: 4px; z-index: 60;
    min-width: 172px; background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    padding: 4px; box-shadow: var(--sh-pop);
  }}
  .more-wrap.is-open .more-menu {{ display: block; }}
  .more-menu button {{
    display: block; width: 100%; text-align: left; background: transparent; border: none;
    padding: 8px 10px; border-radius: var(--r-sm); font-size: var(--fs-md); color: {text};
    cursor: pointer; white-space: nowrap;
  }}
  .more-menu button:hover {{ background: {live_bg}; }}
  .live-warning {{
    background: {error_bg}; border: 1px solid {error}; border-radius: var(--r-lg); padding: 10px 14px;
    margin: 12px 0; font-size: var(--fs-md); color: {error}; line-height: 1.5;
  }}
  /* [추가: 2026-08-24] 오류가 아니라 "참고해 두면 좋을 안내"라 .live-warning(빨강)이
     아니라 앰버(주의) 계열을 쓴다 — CLAUDE.md 색 규칙: 빨강은 오류, 앰버는 주의. */
  .live-heavy-kw-warning {{
    background: {warn_bg}; border: 1px solid {warn_border}; border-radius: var(--r-lg); padding: 10px 14px;
    margin: 12px 0; font-size: var(--fs-md); color: {warn_text}; line-height: 1.5;
  }}
  /* [수정: 2026-09-16] 건수 카드(.stat-card, 59px + 여백 20px)를 없애고 그 문장을 「● 실시간」
     바로 옆에 붙였다 — 숫자 하나와 한 줄 설명에 카드 한 장을 쓰던 자리. 문장·id(stat-count·
     stat-scope)는 그대로라 applyLiveFilters가 예전처럼 건수를 고쳐 쓴다. */
  .head-stat {{ font-size: var(--fs-sm); font-weight: 400; color: {muted}; margin-left: 4px; }}
  .head-stat #stat-count {{ color: {header}; font-weight: 600; }}
  .live-row {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 12px 12px 12px 14px; border-top: 1px solid {border}; border-left: 3px solid transparent;
  }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 마우스 오버 시 연한 회색 표시. 아래 상태별
     배경색(스크랩됨/숨김/담아둠 등) 규칙보다 먼저 둬서, 상태가 있는 줄은 마우스를
     올려도 그 상태 색이 그대로 우선한다(평범한 줄만 회색이 보인다). */
  .live-row:hover {{ background: {row_hover}; }}
  /* [수정: 2026-07-27] 오늘 이미 스크랩된(자동+수동) 기사는 왼쪽 색띠 + 옅은 배경으로
     "이미 잘 담겼다"는 완료 느낌을 준다 — 회색 음영만으로는 존재감이 약했다. 색띠는
     형광펜(제목 안쪽 텍스트 배경색)과 자리가 겹치지 않아 서로 안 부딪친다.
     [수정: 2026-08-21] 초록→파랑 — "스크랩됨"은 담당자가 확정한 결과라, 초록을
     발송 하나에만 남기기로 하면서 다른 "담당자가 확정한 것"과 같은 파랑으로 옮겼다. */
  .live-row.is-scrapped {{
    border-left-color: {accent}; background: {hover}; opacity: 0.86;
  }}
  /* [추가: 2026-07-27] 스크랩 초안/완성본에 있다가 🗑️로 숨겨진 기사는 "이미 스크랩됨"과는
     다른 상태다(살아있는 초안이 아니라 휴지통에 있음) — 취소선으로 "제외됨"을 조용히
     알려준다. 스크랩됨처럼 색띠까지는 필요 없다는 판단으로 회색 음영만 유지한다. */
  /* 흐림·취소선은 기사 글자에만 — 행 전체에 걸면 「숨김」 버튼까지 흐려져 누를 수 있는
     버튼으로 안 보였다(되살리는 버튼인데 끝난 상태 표시처럼 읽힘). */
  .live-row.is-hidden .live-row-text {{ opacity: 0.5; text-decoration: line-through; }}
  /* 숨긴 행의 버튼: 평소엔 「🗑 숨김」(상태), 마우스를 올리면 「↩ 되살리기」(누르면 일어날 일).
     두 라벨을 다 넣고 CSS로만 바꾼다 — 폭은 .add-btn min-width 그대로라 옆 버튼이 안 흔들린다. */
  .unhide-btn .lab-hover {{ display: none; }}
  .unhide-btn:hover {{ background: {hover}; }}
  .unhide-btn:hover .lab-rest {{ display: none; }}
  .unhide-btn:hover .lab-hover {{ display: inline; }}
  /* [수정: 2026-07-27] "직접 추가한 기사"(임시보드)에만 담겨 있고 아직 정식 스크랩에
     안 들어간 기사는 회색 계열 색띠로 보여준다 — 처음엔 호박색을 썼는데, "아직 임시로
     담아둔 것"이 "이미 확정된 스크랩됨"(초록)보다 더 튀어 보이는 게 어색하다는 피드백에
     따라 차분한 회색(= "아직 결정 안 됨")으로 바꿨다. */
  .live-row.is-pinned {{
    border-left-color: {pin_gray}; background: {pin_gray_bg}; opacity: 0.86;
  }}
  /* [추가: 2026-07-29] 컴퓨터가 알아서 [초안]에 담았을 것으로 추정되는 기사 — 사용자가
     직접 담아둔 게 아니라 시스템이 검색 조건에 맞춰 자동으로 골랐다는 뜻이라 담아둠(회색)과
     다른 색으로 구분한다. [수정: 2026-08-21] 청록→연보라 — 이건 정확히 "AI가 하는 일"이라,
     앱의 연보라(🤖 재분류 버튼과 같은 축)를 그대로 쓴다. 새 색을 안 만든다. */
  .live-row.is-auto-drafted {{
    border-left-color: {ai_text}; background: {ai_bg}; opacity: 0.86;
  }}
  /* [추가: 2026-08-14] pubDate 파싱 실패 기사(하단 별도 구역) — "검증 안 된 덤"이라는
     뜻으로 사진 추정 배지와 같은 채도 낮은 호박색 계열을 왼쪽 색띠에 쓴다. */
  .live-row.is-undated {{ border-left-color: {undated_bar}; }}
  .pub-time-unknown {{ color: {photo_badge_text}; }}
  /* [추가] 시간대별 묶기 — 하루치가 200건 가까이 쌓이면 한 줄 목록으로는 훑을 수 없다.
     서버는 지금처럼 전부 내려주고(필터 없음 원칙), 묶기·접기는 화면에서만 한다. 머리줄은
     스크롤해도 상단바(54px) 아래에 붙어 지금 몇 시대를 보는지 놓치지 않게 한다. */
  .hg-bar {{
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    margin: 0 0 10px; font-size: var(--fs-md); color: {muted};
  }}
  .hg-bar .hg-toggle {{
    display: inline-flex; align-items: center; gap: 6px;
    border: 1px solid {border}; background: {card}; color: {text};
    border-radius: var(--r-pill); padding: 5px 12px; font-size: var(--fs-md); cursor: pointer;
  }}
  .hg-bar .hg-toggle.is-on {{ background: {hover}; border-color: {accent}; color: {accent}; }}
  .hg-bar .hg-link {{
    background: none; border: 0; padding: 0; color: {muted}; font-size: var(--fs-md); cursor: pointer;
  }}
  .hg-bar .hg-link:hover {{ color: {accent}; text-decoration: underline; }}
  .hg-bar .hg-sep {{ color: {border}; }}
  .hour-group {{ margin: 0 0 8px; }}
  .hour-group > summary {{
    list-style: none; cursor: pointer; display: flex; align-items: center; gap: 8px;
    padding: 7px 10px; border-radius: var(--r-lg); background: {card}; border: 1px solid {border};
    position: sticky; top: 54px; z-index: 3;
  }}
  .hour-group > summary::-webkit-details-marker {{ display: none; }}
  .hour-group > summary:hover {{ background: {hover}; }}
  .hour-group .hg-caret {{ width: 14px; height: 14px; color: {muted}; transition: transform .15s; flex-shrink: 0; }}
  .hour-group[open] > summary .hg-caret {{ transform: rotate(90deg); }}
  .hour-group .hg-time {{ font-weight: 600; color: {header}; font-size: var(--fs-base); }}
  .hour-group .hg-range {{ color: {muted}; font-size: var(--fs-sm); }}
  .hour-group .hg-count {{ margin-left: auto; color: {muted}; font-size: var(--fs-md); font-variant-numeric: tabular-nums; }}
  .hour-group[open] > summary {{ border-bottom-left-radius: 0; border-bottom-right-radius: 0; }}
  .hour-group .hg-body {{ border: 1px solid {border}; border-top: 0; border-radius: 0 0 var(--r-lg) var(--r-lg); background: {card}; }}
  /* 묶음 첫 행의 윗줄은 머리줄 테두리와 겹치므로 뗀다(두 줄로 보인다). */
  .hour-group .hg-body > .live-row:first-child {{ border-top: 0; }}
  /* 필터를 걸어 한 건도 안 남은 시간대는 머리줄째 감춘다(빈 줄만 남으면 세는 데 방해된다). */
  .hour-group.is-empty {{ display: none; }}
  /* 새 기사가 들어오는 가장 최근 시간대만 파란 테두리로 가볍게 표시. */
  .hour-group.hg-latest > summary {{ border-color: {accent_border}; }}
  .hour-group.hg-latest .hg-time {{ color: {accent}; }}
  /* 묶기를 끄면 예전 한 줄 최신순 그대로. */
  .live-list.is-ungrouped .hour-group > summary {{ display: none; }}
  .live-list.is-ungrouped .hour-group .hg-body {{ border: 0; }}
  .live-undated-section {{ margin-top: 22px; padding-top: 14px; border-top: 1px solid {border}; }}
  .live-undated-heading {{ font-size: var(--fs-base); color: {header}; margin: 0 0 4px; }}
  .live-undated-desc {{ font-size: var(--fs-sm); color: {muted}; line-height: 1.6; margin: 0 0 8px; }}
  .live-row-text {{ min-width: 0; }}
  /* [수정: 2026-07-29] 버그 수정 — 📌 버튼 옆에 🗑️ 아이콘을 추가하면서 .live-row의
     자식이 2개(텍스트+버튼)에서 3개(텍스트+버튼+아이콘)로 늘었는데, justify-content:
     space-between이 자식 "사이사이"에 남는 공간을 나눠 넣다 보니 버튼과 아이콘 사이가
     텅 비면서 핀이 화면 한가운데로 떠밀려 보였다. 두 버튼을 이 래퍼 하나로 묶어 다시
     "텍스트 vs 버튼 묶음" 2개짜리 레이아웃으로 되돌린다. */
  .live-row-actions {{ display: flex; align-items: center; gap: 6px; flex-shrink: 0; }}
  .outlet-tag {{ color: {muted}; font-size: var(--fs-sm); }}
  /* [추가: 2026-08-11] 표식 없는 사진기사 추정 배지 — 기존 상태색(노랑=새 기사, 초록=이동,
     파랑=선택)과 겹치지 않게, 채도 낮은 모래색 테두리 알약으로 "행 강조"가 아니라 "작은
     라벨"로 읽히게 했다. */
  .photo-badge {{
    display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: var(--r-pill);
    font-size: var(--fs-xs); color: {photo_badge_text}; background: {photo_badge_bg}; border: 1px solid {photo_badge_border};
    white-space: nowrap; vertical-align: 1px;
  }}
  .pub-time {{ color: {muted}; font-size: var(--fs-sm); margin-left: 6px; }}
  /* [추가: 2026-08-25] 🔍 검색어 — 확정본·초안(app.renderer의 .kw-inline)과 같은 모양·같은
     뜻이다. 그룹 칩(.row-group-tag)이 "어느 그룹"이라면 이건 "그 안의 어느 검색어"라, 칩과
     달리 테두리를 두르지 않고 회색 글자로만 둔다(참고용 메타 정보). 간격은 이 화면 관례대로
     구분점(·) 대신 margin-left: 6px. */
  .kw-inline {{
    display: inline-flex; align-items: center; gap: 4px; margin-left: 6px;
    font-size: var(--fs-sm); color: {muted}; vertical-align: -1px;
  }}
  .kw-inline .kw-ic {{ width: 0.75em; height: 0.75em; color: {text_faint_alt}; flex-shrink: 0; }}
  /* [수정: 2026-08-25] 그룹 칩(.row-group-tag)과 같은 말줄임 패턴 — 실측(431건 전수, 렌더
     폭 기준) 210px에서 4.6%(20건)만 잘린다. 그룹명(90px)보다 넓게 잡은 이유는 검색어가
     쉼표로 여러 개 이어붙는 값이라 그룹명보다 원래 더 길기 때문. 안 잘리면 지금처럼 다음
     줄로 접혔는데, 사용자 실측(6개 표본)에서 줄바꿈이 카드 높이를 키우고 담아두기 버튼을
     줄여도(대안 검토함) 그 폭을 못 만회해 — 잘라서 한 줄에 고정하는 쪽을 택했다. */
  .kw-inline .kw-v {{
    display: inline-block; max-width: 210px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; vertical-align: -3px; color: {text_soft};
  }}
  .live-title {{ font-size: var(--fs-base); margin: 2px 0 2px; overflow-wrap: anywhere; color: {text}; }}
  .live-title summary {{ cursor: pointer; -webkit-tap-highlight-color: transparent; color: {text}; }}
  .live-title summary::marker {{ color: {muted}; }}
  /* [추가: 2026-08-19] 제목 맨 앞 말머리([단독]/[속보])는 배지로 감싸지 않고 글자색만
     바꾼다 — 제목 문자열 자체는 그대로 두므로 복사/내보내기 텍스트에 영향이 없다
     (CODING_CONVENTIONS.md §4, 화면 전용 표시가 보고서로 새면 안 된다).
     [수정: 2026-08-20] 확정본·초안 화면과 같은 팔레트로 통일 — [단독]이 담당자가
     "가장 중요하다"고 지목한 쪽이라 굵기(800)까지 더 준다. 예전엔 [단독]=분홍
     (#9D174D)/[속보]=빨강(error)이었는데, 확정본·초안에 [단독]=빨강 카드 강조를
     새로 넣으면서 화면마다 말머리 색이 다른 뜻을 가리키게 되는 걸 막기 위해
     세 화면 전부 이 값으로 맞췄다. */
  .t-scoop {{ color: {scoop_text}; font-weight: 800; }}
  .t-flash {{ color: {flash_text}; font-weight: 600; }}
  .live-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: var(--fs-md); }}
  .live-url {{ font-size: var(--fs-sm); overflow-wrap: anywhere; }}
  .live-url a {{ color: {accent}; text-decoration: underline; }}
  /* [수정: 2026-07-29] 상태별 라벨 길이가 다 달라서(📌/📌 담아둠/✓ 스크랩됨/🤖 자동 선별/
     🗑️ 숨김) 버튼 너비가 들쭉날쭉했고, 그 옆에 붙는 휴지통 아이콘 위치까지 같이
     흔들려 지저분해 보였다 — min-width+가운데 정렬로 버튼 너비를 통일해 휴지통이
     항상 같은 자리에 오게 한다.
     [수정: 2026-09-01] 오른쪽에 "복사" 버튼이 새로 붙으면서 자리를 내주려고 폭을
     92 -> 84px, 좌우 여백을 12 -> 9px로 줄였다(라벨 글자는 그대로 둔다 — "담기"로
     줄이면 다른 화면의 📌 담아두기와 말이 갈린다). **84px은 가장 긴 라벨("자동 선별")의
     실제 렌더 폭이라 다섯 상태가 여전히 같은 폭으로 맞는다** — 그보다 더 줄이면(74px로
     재봤을 때 74~84px) 위 "휴지통이 항상 같은 자리에" 규칙이 그대로 깨진다. */
  .add-btn {{
    flex-shrink: 0; background: {card}; color: {text}; border: 1px solid {border};
    border-radius: var(--r-md); padding: 6px 9px; font-size: var(--fs-sm); cursor: pointer; white-space: nowrap;
    min-width: 84px; text-align: center;
  }}
  .add-btn:disabled {{ color: {muted}; cursor: not-allowed; }}
  .add-btn.added {{ color: {accent}; border-color: {accent}; }}
  .add-btn.is-scrapped-btn {{ color: {accent}; border-color: {accent}; background: {hover}; }}
  .add-btn.is-pinned-btn {{ color: {pin_gray}; border-color: {pin_gray}; background: {pin_gray_bg}; }}
  .add-btn.is-auto-drafted-btn {{ color: {ai_text}; border-color: {ai_text}; background: {ai_bg}; }}
  /* [추가: 2026-09-01] 복사 — "⋯ 더보기" 메뉴 안에 있던 "복사하기"를 담아두기 바로
     오른쪽으로 꺼냈다(사용자 요청). 아이콘만 두지 않고 글자 라벨을 붙이는 건, 이
     화면의 오른쪽 묶음에서 글자 버튼은 상태(담아두기/담아둠/…)를 말하는 자리이고
     아이콘은 ⋯·🗑처럼 "가끔 한 번"인 자리라, 자주 쓰는 복사를 아이콘 쪽에 두면
     무게가 실제 사용 빈도와 어긋나기 때문이다. 누르면 확정본·초안과 같은 규칙으로
     아이콘만 1초간 check로 바뀐다(copyArticleIcon, .is-copied) — 라벨은 그대로 둬
     버튼 폭이 흔들리지 않는다. */
  .copy-btn {{
    flex-shrink: 0; display: inline-flex; align-items: center; gap: 3px;
    background: {card}; color: {text}; border: 1px solid {border};
    border-radius: var(--r-md); padding: 6px 10px; font-size: var(--fs-sm); cursor: pointer; white-space: nowrap;
  }}
  .copy-btn:hover {{ color: {accent}; border-color: {accent}; background: {hover}; }}
  .copy-btn .icon-done {{ display: none; }}
  .copy-btn.is-copied .icon-default {{ display: none; }}
  .copy-btn.is-copied .icon-done {{ display: inline-flex; }}
  /* [추가: 2026-07-29] "📌 담아둠" 옆의 작은 숨기기 아이콘 — 초안까지 안 가고 바로
     실시간 현황에서 필요없는 기사를 숨길 수 있게 한다(사용자 요청). 숨기면 스크랩
     화면(index/history)에서도 똑같이 안 보인다(app.curation.hide_article, 전역
     숨김) — live.html 전용 임시 숨김이 아니다(의도된 동작, 사용자 확인됨).*/
  .hide-from-live-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: var(--fs-base);
    cursor: pointer; padding: 4px 6px;
  }}
  .hide-from-live-btn:hover {{ color: {error}; }}
  /* [추가: 2026-08-05] 원문 다시 가져오기 버튼 — app.renderer.render_article과 동일한
     이유·동작(평소 숨김, 그 행에 마우스를 올리면 나타남). */
  .refetch-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: var(--fs-base);
    cursor: pointer; padding: 4px 6px; opacity: 0; transition: opacity 0.15s;
  }}
  .live-row:hover .refetch-btn {{ opacity: 1; }}
  .refetch-btn:disabled {{ opacity: 0.35 !important; cursor: not-allowed; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — ✏️ 직접 수정 인라인 편집 칸. */
  .edit-summary-form {{
    margin: 8px 0 4px 20px; padding: 10px 12px; border: 1px solid {accent}; border-radius: var(--r-lg);
    display: flex; flex-direction: column; gap: 8px;
  }}
  .edit-summary-form input, .edit-summary-form textarea {{
    width: 100%; box-sizing: border-box; padding: 6px 10px; border: 1px solid {border}; border-radius: var(--r-md);
    font-size: var(--fs-md); color: {text}; background: {card}; font-family: inherit;
  }}
  .edit-summary-form .edit-summary-actions {{ display: flex; justify-content: flex-end; gap: 8px; }}
  .edit-summary-form button {{ font-size: var(--fs-sm); padding: 5px 12px; }}
  .empty {{ text-align: center; margin: 60px 0; font-size: var(--fs-lg); color: {muted}; }}
  .error-box {{ text-align: center; margin: 60px 0; color: {muted}; font-size: var(--fs-base); line-height: 1.7; }}
  /* [추가: 2026-08-13] 콜드 캐시 대기 화면(render_live_loading_page) — 스피너·안내
     문구·안심 문구 3단 구성. .loading-spin-icon은 위 .refresh-btn과 별개로 이 화면
     한가운데에 크게 도는 아이콘용(같은 회전 애니메이션을 재사용). */
  .loading-box {{ text-align: center; padding: 28px 16px 32px; }}
  .loading-spin-icon {{ font-size: 2.1rem; color: {accent}; margin-bottom: 18px; }}
  .loading-title {{ margin: 0 0 10px; font-size: var(--fs-base); font-weight: 600; color: {header}; }}
  .loading-desc {{ margin: 0 0 22px; font-size: var(--fs-md); color: {muted}; line-height: 1.65; }}
  /* [추가: 2026-08-13, 정리: 2026-08-21] 안심시키는 문구라 붉은 톤(COLOR_LIVE_BG)이
     아니라 다른 화면과 같은 차분한 파란색(COLOR_HOVER)을 쓴다. */
  .loading-reassure {{
    display: inline-flex; align-items: center; gap: 7px; background: {hover};
    border-radius: var(--r-md); padding: 9px 15px; font-size: var(--fs-sm); color: {header};
  }}
  /* [추가: 2026-08-13] app.renderer와 동일 — 단색 SVG 아이콘(app.icons) 공통 크기·색. */
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
</style>
</head>
<body>
{topnav_html}
<div class="container">
  <div class="live-head">
    <span class="live-badge"><span class="pulse"></span> 실시간{head_stat_html}</span>
    {refresh_button_html}
  </div>
  {body}
</div>
<div class="bottombar"><div class="bottombar-inner">
  <div class="highlight-wrap">
    <button type="button" class="highlight-toggle" onclick="toggleHighlightPopover()" title="형광펜 단어 편집"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3.2 20.8 9l-8.2 8.2H6.8V11.4Z"/><path d="m11.4 6.8 5.8 5.8"/><path d="M3.5 21h17"/></svg></button>
    <div class="highlight-popover" id="highlight-popover">
      <div class="highlight-chips" id="highlight-chips"></div>
      <form class="highlight-add-form" onsubmit="return addHighlightWord(event);">
        <input type="text" id="highlight-new-word" placeholder="단어 추가" maxlength="20">
        <button type="submit">추가</button>
      </form>
    </div>
  </div>
  <a href="{hidden_href}" title="휴지통"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg></a>
</div></div>
<script>
// [추가: 2026-08-03] app.renderer와 동일한 이유·동작 — 하단바 🖍️ 형광펜 팝오버.
const HIGHLIGHT_WORDS = {highlight_words_json};
function renderHighlightChips() {{
  var wrap = document.getElementById("highlight-chips");
  if (!HIGHLIGHT_WORDS.length) {{
    wrap.innerHTML = '<p class="highlight-empty">등록된 형광펜 단어가 없어요.</p>';
    return;
  }}
  wrap.innerHTML = HIGHLIGHT_WORDS.map(function(item) {{
    var word = item.word.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    var attr = item.word.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    // [수정: 2026-08-05] app.renderer와 동일 — 칩 클릭으로 색 순환.
    return '<span class="highlight-chip" data-word="' + attr + '" style="background:' + item.color_hex +
      '" onclick="cycleChipColor(this)" title="클릭하면 색이 바뀝니다">' + word +
      '<button type="button" data-word="' + attr + '" onclick="event.stopPropagation(); removeHighlightWord(this)" title="제거">×</button></span>';
  }}).join("");
}}
function toggleHighlightPopover() {{
  var pop = document.getElementById("highlight-popover");
  var opening = !pop.classList.contains("is-open");
  pop.classList.toggle("is-open");
  if (opening) {{ renderHighlightChips(); }}
}}
// [추가: 2026-08-05] app.renderer와 동일 — 팝오버 바깥 클릭 시 닫힘.
document.addEventListener("click", function(e) {{
  var pop = document.getElementById("highlight-popover");
  if (pop.classList.contains("is-open") && !e.target.closest(".highlight-wrap")) {{
    pop.classList.remove("is-open");
  }}
}});
function _postToggleHighlight(word) {{
  return fetch("http://{settings_host}:{settings_port}/keywords/toggle-highlight", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{word: word}})
  }});
}}
function removeHighlightWord(btn) {{
  _postToggleHighlight(btn.dataset.word).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("삭제에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("삭제에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function addHighlightWord(evt) {{
  evt.preventDefault();
  var input = document.getElementById("highlight-new-word");
  var word = input.value.trim();
  if (!word) {{ return false; }}
  _postToggleHighlight(word).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("추가에 실패했습니다 — 형광펜 단어는 최대 개수까지 등록돼 있을 수 있어요."); }}
  }}).catch(function() {{
    alert("추가에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
  return false;
}}
// [수정: 2026-08-05] app.renderer와 동일 — 팝오버 칩 클릭으로 색 순환(본문 클릭 방식은 삭제).
function cycleChipColor(chip) {{
  var word = chip.dataset.word;
  fetch("http://{settings_host}:{settings_port}/keywords/cycle-highlight-color", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{word: word}})
  }}).then(function(res) {{
    if (!res.ok) {{ alert("색 변경에 실패했습니다. 다시 시도해주세요."); return null; }}
    return res.json();
  }}).then(function(data) {{
    if (!data) return;
    chip.style.background = data.color_hex;
    document.querySelectorAll('.hl-word[data-word="' + CSS.escape(word) + '"]').forEach(function(span) {{
      span.style.backgroundColor = data.color_hex;
    }});
  }}).catch(function() {{
    alert("색 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function addToScrap(btn) {{
  var params = new URLSearchParams({{
    outlet: btn.dataset.outlet, title: btn.dataset.title,
    url: btn.dataset.url, summary: btn.dataset.summary, pubDate: btn.dataset.pubDate
  }});
  fetch("http://{settings_host}:{settings_port}/add-manual-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      // [수정: 2026-07-29] 버그 수정 — disabled로 막아버리면 담아둠 재클릭으로
      // 취소하는 토글이 새로고침 전까진 안 먹혔다. 담아둠 상태의 서버 렌더링과
      // 똑같이 계속 눌러서 취소할 수 있게 유지한다(unpinFromLive와 동일한 최종 상태).
      btn.classList.add("added", "is-pinned-btn");
      btn.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17v5"/><path d="M9 2h6l-1 6 3 3v2H7v-2l3-3-1-6Z"/></svg> 담아둠';
      btn.title = "담아둔 기사(임시보드)에 있어요 — 눌러서 담아두기를 취소할 수 있어요";
      btn.onclick = function() {{ unpinFromLive(btn); }};
      var row = btn.closest(".live-row");
      if (row) {{ row.classList.add("is-pinned"); }}
    }} else {{
      alert("담아두지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("담아두지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function hideFromLive(btn) {{
  var params = new URLSearchParams({{
    outlet: btn.dataset.outlet, title: btn.dataset.title,
    url: btn.dataset.url, pubDate: btn.dataset.pubDate
  }});
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      var row = btn.closest(".live-row");
      row.classList.remove("is-pinned", "is-scrapped", "is-auto-drafted");
      row.classList.add("is-hidden");
      var addBtn = row.querySelector(".add-btn");
      // [수정: 2026-07-29] 숨김 상태 버튼은 재클릭으로 되돌릴 수 있어야 하므로 비활성화하지
      // 않는다 — onclick도 unhideFromLive로 바꿔줘야 재클릭이 실제로 되돌리기를 호출한다.
      addBtn.disabled = false;
      addBtn.className = "add-btn added unhide-btn";
      addBtn.innerHTML = {unhide_label_js};
      addBtn.title = {unhide_title_js};
      addBtn.onclick = function() {{ unhideFromLive(addBtn); }};
      btn.remove();
    }} else {{
      alert("숨기지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("숨기지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] app.renderer.render_article과 동일 — 원문에서 다시 가져오기.
// 실시간 현황은 평소 hideFromLive처럼 DOM만 갈아끼우는 화면이지만, 여기서는 새로고침을
// 쓴다 — 하이라이트 적용된 HTML을 자바스크립트로 다시 만드는 것보다 훨씬 간단하고,
// 어차피 자주 누를 버튼이 아니라 새로고침 한 번(=실시간 재검색 한 번)의 비용이 크지 않다.
function refetchSummary(btn) {{
  var url = btn.dataset.url;
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/refetch-summary", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else if (res.status === 404) {{ alert("원문에서 더 나은 제목·요약을 찾지 못했어요."); btn.disabled = false; }}
    else {{ alert("다시 가져오기에 실패했습니다. 다시 시도해주세요."); btn.disabled = false; }}
  }}).catch(function() {{
    alert("다시 가져오기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
    btn.disabled = false;
  }});
}}
// [추가: 2026-08-05] app.renderer와 동일 — ✏️ 직접 수정. live.html은 카드 구조가
// 달라서(.article/.article-summary가 아니라 .live-row/.live-summary) 그 부분만 다르다.
function editSummary(btn) {{
  var row = btn.closest(".live-row");
  if (row.querySelector(".edit-summary-form")) {{ return; }}
  var url = btn.dataset.url;
  var form = document.createElement("div");
  form.className = "edit-summary-form";
  var titleInput = document.createElement("input");
  titleInput.type = "text";
  titleInput.value = btn.dataset.title;
  var summaryInput = document.createElement("textarea");
  summaryInput.rows = 2;
  summaryInput.value = btn.dataset.summary;
  var actions = document.createElement("div");
  actions.className = "edit-summary-actions";
  var cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "clear-btn";
  cancelBtn.textContent = "취소";
  cancelBtn.onclick = function(e) {{ e.stopPropagation(); form.remove(); }};
  var saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.textContent = "저장";
  saveBtn.onclick = function(e) {{
    e.stopPropagation();
    fetch("http://{settings_host}:{settings_port}/edit-summary", {{
      method: "POST", keepalive: true,
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: new URLSearchParams({{url: url, title: titleInput.value, summary: summaryInput.value}})
    }}).then(function(res) {{
      if (res.ok) {{ location.reload(); }}
      else {{ alert("저장하지 못했습니다. 다시 시도해주세요."); }}
    }}).catch(function() {{
      alert("저장하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
    }});
  }};
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);
  form.appendChild(titleInput);
  form.appendChild(summaryInput);
  form.appendChild(actions);
  var details = row.querySelector(".live-title");
  details.open = true;
  row.querySelector(".live-summary").insertAdjacentElement("afterend", form);
}}
function unpinFromLive(btn) {{
  var params = new URLSearchParams({{url: btn.dataset.url}});
  fetch("http://{settings_host}:{settings_port}/unpin-manual-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      var row = btn.closest(".live-row");
      row.classList.remove("is-pinned");
      // [수정: 2026-07-29] 버그 수정 — 🗑️ 아이콘은 이제 담아둠 상태만이 아니라 평범한
      // 상태에도 나오므로(숨김 상태만 빼고 항상 표시), 담아두기를 취소해도 지우면 안 된다.
      btn.classList.remove("added", "is-pinned-btn");
      btn.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17v5"/><path d="M9 2h6l-1 6 3 3v2H7v-2l3-3-1-6Z"/></svg> 담아두기';
      btn.title = "담아둔 기사(임시보드)에 담아둬요";
      btn.onclick = function() {{ addToScrap(btn); }};
    }} else {{
      alert("담아두기를 취소하지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("담아두기를 취소하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function unhideFromLive(btn) {{
  var params = new URLSearchParams({{url: btn.dataset.url}});
  fetch("http://{settings_host}:{settings_port}/unhide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      // 되돌린 뒤 실제 상태(담아둠 or 평범)는 서버가 다시 정확히 판단하므로 새로고침한다.
      location.reload();
    }} else {{
      alert("숨기기를 취소하지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("숨기기를 취소하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
var SCRAP_OUTLETS = {outlet_order_json};
// [수정: 2026-08-13] 언론사 체크박스 하나만 보던 applyOutletFilter를, 그룹 칩·제목
// 검색·"아직 처리 안 한 기사만"까지 전부 함께 고려하는 applyLiveFilters로 합쳤다 — 필터가
// 여러 개면 전부 AND로 겹쳐야(언론사 필터를 켠 채로 그룹 칩도 고르는 게 자연스러운
// 사용 흐름) 하는데, 각자 따로 있으면 나중 필터가 앞 필터를 덮어써버리기 쉽다.
// 그룹 칩끼리는 OR(여러 개 켜면 그 중 하나라도 걸리면 보임) — 체크박스 여러 개를
// 켜는 관행적 의미가 OR이라 그렇게 맞췄다(사용자와 합의, AND는 검색을 과하게 좁힌다).
// [추가: 2026-08-19] 위쪽 특수조건 줄(언론사·[단독]·[속보]·아직 처리 안 한 기사만)과 아래쪽 그룹 칩
// 줄은 뜻이 다르다 — 그룹 칩끼리만 OR이고, 나머지는 전부 AND([단독]·[속보] 둘만 예외
// 로 서로 OR). 이 체크박스가 원래 그룹 칩 줄에 섞여 있던 건 실수였다(그 칩만 혼자
// AND로 동작해 OR인 나머지 칩들과 논리가 달랐다) — 위 줄로 옮겨 바로잡았다.
function activeGroupFilters() {{
  var active = [];
  document.querySelectorAll(".group-chip.is-active").forEach(function (chip) {{
    if (chip.dataset.group) active.push(chip.dataset.group);
  }});
  return active;
}}
function applyLiveFilters() {{
  var outletCheckbox = document.getElementById("scrap-outlet-only");
  var onlyScrap = outletCheckbox ? outletCheckbox.checked : false;
  // [추가: 2026-08-19] 말머리([단독]/[속보]) 필터 — 이 둘 사이만 OR다(AND면 한 제목이
  // 단독이면서 속보일 수 없어 둘 다 켜면 항상 0건이 된다). 나머지 조건과는 AND.
  // [추가: 2026-09-16] `[사설]`도 같은 이유로 이 OR 묶음에 들어간다 — 사설이면서
  // 단독인 제목은 실측 0건이라, AND로 두면 둘을 같이 켤 때 항상 0건이 된다.
  var scoopCheckbox = document.getElementById("headline-scoop-only");
  var flashCheckbox = document.getElementById("headline-flash-only");
  var editorialCheckbox = document.getElementById("headline-editorial-only");
  var wantScoop = scoopCheckbox ? scoopCheckbox.checked : false;
  var wantFlash = flashCheckbox ? flashCheckbox.checked : false;
  var wantEditorial = editorialCheckbox ? editorialCheckbox.checked : false;
  var unclaimedCheckbox = document.getElementById("unclaimed-only");
  var onlyUnclaimed = unclaimedCheckbox ? unclaimedCheckbox.checked : false;
  var searchInput = document.getElementById("live-title-search");
  var query = searchInput ? searchInput.value.trim().toLowerCase() : "";
  var activeGroups = activeGroupFilters();
  var rows = document.querySelectorAll(".live-row");
  var visible = 0;
  rows.forEach(function (row) {{
    var isScrapOutlet = SCRAP_OUTLETS.indexOf(row.dataset.outlet) !== -1;
    var hide = onlyScrap && !isScrapOutlet;
    if (!hide && (wantScoop || wantFlash || wantEditorial)) {{
      var headline = row.dataset.headline || "";
      var headlineOk = (wantScoop && headline === "단독") || (wantFlash && headline === "속보")
        || (wantEditorial && row.dataset.editorial === "1");
      if (!headlineOk) hide = true;
    }}
    if (!hide && onlyUnclaimed && row.dataset.status !== "normal") hide = true;
    if (!hide && query) {{
      var titleEl = row.querySelector(".live-title summary");
      var titleText = titleEl ? titleEl.textContent.toLowerCase() : "";
      if (titleText.indexOf(query) === -1) hide = true;
    }}
    if (!hide && activeGroups.length) {{
      var rowGroups = (row.dataset.groups || "").split("|");
      var matches = activeGroups.some(function (g) {{ return rowGroups.indexOf(g) !== -1; }});
      if (!matches) hide = true;
    }}
    row.classList.toggle("is-hidden-by-filter", hide);
    // [추가: 2026-08-14] 발행시각 불명 기사(is-undated)는 필터는 그대로 적용받지만
    // (숨김/표시는 위에서 이미 처리됨), 상단 "당일 0시 이후 누적" 건수엔 안 센다 —
    // 그 정의를 만족하는지 확인이 안 된 기사라 숫자를 섞으면 부정확해진다.
    if (!hide && !row.classList.contains("is-undated")) visible++;
  }});
  var statCount = document.getElementById("stat-count");
  if (statCount) statCount.textContent = visible + "건";
  var statScope = document.getElementById("stat-scope");
  if (statScope) statScope.textContent = onlyScrap ? "스크랩 언론사만" : "전체 언론사";
  syncHourGroups();
  saveLiveFilterState();
}}
// [추가] 시간대 묶음 — 필터가 돌 때마다 머리줄 건수를 보이는 만큼으로 고쳐 쓰고, 0건이
// 된 시간대는 통째로 감춘다(상단 누적 건수와 같은 축: 필터와 AND). 제목을 검색하는
// 동안에는 걸린 시간대를 자동으로 펼친다 — 접힌 채로 "N건"만 보이면 찾은 걸 못 본다.
function syncHourGroups() {{
  var search = document.getElementById("live-title-search");
  var searching = !!(search && search.value.trim());
  document.querySelectorAll(".hour-group").forEach(function (group) {{
    var rows = group.querySelectorAll(".live-row");
    var visible = 0;
    rows.forEach(function (row) {{ if (!row.classList.contains("is-hidden-by-filter")) visible++; }});
    var count = group.querySelector(".hg-count");
    if (count) count.textContent = visible + "건";
    group.classList.toggle("is-empty", visible === 0);
    if (searching && visible > 0) group.open = true;
  }});
}}
function openAllHourGroups() {{
  document.querySelectorAll(".hour-group").forEach(function (g) {{ g.open = true; }});
}}
function closeAllHourGroups() {{
  document.querySelectorAll(".hour-group").forEach(function (g) {{ g.open = false; }});
}}
// 묶기 끄기 = 예전 한 줄 최신순으로 되돌리기. 머리줄만 감추고 전부 펼쳐두므로 행 순서는
// 그대로다(다시 켜면 접힘 상태도 그대로 살아난다).
function toggleHourGrouping(btn) {{
  var list = document.getElementById("live-list");
  if (!list) return;
  var grouped = !list.classList.toggle("is-ungrouped");
  btn.classList.toggle("is-on", grouped);
  btn.setAttribute("aria-pressed", grouped ? "true" : "false");
  document.querySelectorAll(".hour-group").forEach(function (g) {{
    if (!grouped) {{ g.dataset.wasOpen = g.open ? "1" : "0"; g.open = true; }}
    else if (g.dataset.wasOpen === "0") {{ g.open = false; }}
  }});
}}
function toggleGroupChip(btn) {{
  btn.classList.toggle("is-active");
  applyLiveFilters();
}}
function toggleKeywordBreakdown() {{
  var panel = document.getElementById("keyword-breakdown");
  if (panel) panel.classList.toggle("is-open");
}}
// [추가: 2026-08-13] app.renderer와 동일한 "⋯ 더보기" 메뉴 열기/닫기.
function closeArticleMenus() {{
  document.querySelectorAll(".more-wrap.is-open").forEach(function (w) {{ w.classList.remove("is-open"); }});
}}
function toggleArticleMenu(btn) {{
  var wrap = btn.closest(".more-wrap");
  var wasOpen = wrap.classList.contains("is-open");
  closeArticleMenus();
  if (!wasOpen) wrap.classList.add("is-open");
}}
document.addEventListener("click", function (e) {{
  if (!e.target.closest(".more-wrap")) closeArticleMenus();
}});
// [추가: 2026-08-13] 기사 한 건만 복사 — 버튼에 이미 복사할 한 줄(data-copy-text)이
// 들어 있어서 서버 호출이 필요 없다.
// [수정: 2026-09-01] "⋯ 더보기" 메뉴에서 담아두기 옆으로 나오면서, 확정본·초안과 같은
// 함수 이름·같은 피드백(아이콘만 1초간 check)을 쓴다 — 하는 일이 완전히 같은데 이름만
// 다른 쌍둥이가 있으면 한쪽만 고쳤을 때 화면끼리 피드백이 어긋난다. 메뉴 안에서 쓰던
// 글자 치환("복사했습니다")은 라벨을 바꿔 버튼 폭이 흔들리므로 안 쓴다.
function copyArticleIcon(btn) {{
  navigator.clipboard.writeText(btn.dataset.copyText).then(function () {{
    btn.classList.add("is-copied");
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(function () {{ btn.classList.remove("is-copied"); }}, 1000);
  }}).catch(function () {{
    alert("복사에 실패했습니다.");
  }});
}}
function renderRelativeTimes() {{
  var now = Date.now();
  document.querySelectorAll(".pub-time-relative[data-pub-date]").forEach(function (el) {{
    var pub = new Date(el.dataset.pubDate).getTime();
    if (isNaN(pub)) return;
    var minutes = Math.floor((now - pub) / 60000);
    var text;
    if (minutes < 1) text = "방금 전";
    else if (minutes < 60) text = minutes + "분 전";
    else if (minutes < 1440) text = Math.floor(minutes / 60) + "시간 전";
    else text = Math.floor(minutes / 1440) + "일 전";
    el.textContent = text;
  }});
}}
renderRelativeTimes();
// [추가: 2026-08-25] 🔍 검색어 말줄임(.kw-v, 위 CSS) — 실제로 잘린 행에만 title에 전체
// 검색어를 채워 넣는다. 안 잘린 행(대부분, 실측 95%)은 값이 이미 다 보이므로 원래
// title("이 기사가 걸린 검색어" 설명)을 그대로 둔다 — 멀쩡히 보이는 값을 또 툴팁으로
// 띄울 이유가 없다. scrollWidth > clientWidth로 실제 렌더 결과를 보고 판단하므로
// 글자수 추정이 아니라 이 브라우저·이 폰트 기준 정확한 잘림 여부다.
function markTruncatedKeywords() {{
  document.querySelectorAll(".kw-inline").forEach(function (kw) {{
    var v = kw.querySelector(".kw-v");
    if (v && v.scrollWidth > v.clientWidth + 1) {{ kw.title = v.textContent; }}
  }});
}}
markTruncatedKeywords();
// [추가] 필터 체크박스 다섯 개는 이 브라우저에 기억한다 — 담당자가 「선택 언론사만」을 한 번
// 켜두면 다음에 열 때도 그대로다. 설정 화면에 기본값 항목을 따로 만들지 않는 이유: 사용자가
// 한 명이라 설정값이 주는 이득이 없는데 "설정에 적힌 기본값"과 "지금 화면 상태"라는 두 개의
// 진실만 생긴다. 제목 검색어·그룹 칩은 그때그때 찾는 값이라 기억하지 않는다(매번 빈 상태).
var LIVE_FILTER_KEY = "liveFilters";
var LIVE_FILTER_IDS = [
  "scrap-outlet-only", "headline-scoop-only", "headline-flash-only",
  "headline-editorial-only", "unclaimed-only"
];
function saveLiveFilterState() {{
  try {{
    var state = {{}};
    LIVE_FILTER_IDS.forEach(function (id) {{
      var el = document.getElementById(id);
      if (el) state[id] = !!el.checked;
    }});
    localStorage.setItem(LIVE_FILTER_KEY, JSON.stringify(state));
  }} catch (e) {{ /* 시크릿 창 등에서 저장이 막혀도 화면은 그대로 돌아야 한다 */ }}
}}
function restoreLiveFilterState() {{
  var state = null;
  try {{ state = JSON.parse(localStorage.getItem(LIVE_FILTER_KEY) || "null"); }} catch (e) {{ state = null; }}
  if (!state) return;
  var restored = false;
  LIVE_FILTER_IDS.forEach(function (id) {{
    var el = document.getElementById(id);
    // 언론사 목록이 비어 체크박스가 잠겨 있으면 되살리지 않는다 — 켜면 한 건도 안 남는다.
    if (el && !el.disabled && state[id]) {{ el.checked = true; restored = true; }}
  }});
  if (restored) applyLiveFilters();
}}
restoreLiveFilterState();
</script>
</body>
</html>
"""


_REFRESH_BUTTON_ENABLED = (
    '<a class="refresh-btn" href="live.html">'
    f'{icon("refresh")} 새로고침</a>'
)
# [추가: 2026-08-13] <a href> 대신 <span>으로 그린다 — href가 없으니 클릭해도 이동할
# 곳 자체가 없다(disabled 속성이 없는 <a>에 CSS로 회색만 입히는 것과 달리, 실수로라도
# 눌리는 경로 자체가 존재하지 않는다).
_REFRESH_BUTTON_DISABLED = (
    '<span class="refresh-btn is-disabled" aria-disabled="true" title="첫 검색이 끝나면 눌러주세요">'
    f'{icon("refresh")} 새로고침</span>'
)


def _theme() -> dict:
    return {
        # [수정: 2026-08-21] 색 값은 전부 app.config.PALETTE에서 온다(scrap_green·
        # pin_gray·auto_teal 포함 — 예전엔 여기 직접 적혀 있었다). 이 화면 고유의 붉은
        # 톤은 live_bg, 다른 화면과 같은 파란 hover는 hover로 자리표시자 이름이 갈려
        # 있다 — 예전엔 hover를 붉은 톤으로 덮어써서, 파란 hover가 필요한 자리마다 색
        # 값을 직접 적어야 했다.
        **PALETTE,
        "font_stack": FONT_STACK,
        "topnav_style": topnav_style(),
        "topnav_html": regular_nav("live"),
        # 숨긴 행 버튼 — 서버 렌더링(_render_row)과 hideFromLive가 같은 마크업을 쓴다.
        "unhide_label_js": json.dumps(unhide_btn_label(), ensure_ascii=False),
        "unhide_title_js": json.dumps(UNHIDE_BTN_TITLE, ensure_ascii=False),
    }


UNHIDE_BTN_TITLE = "숨김을 풀어 원래 자리로 되돌려요 (초안·확정본에도 다시 보여요)"


def unhide_btn_label() -> str:
    """숨긴 행 버튼 라벨 — 평소 「🗑 숨김」, 마우스를 올리면 「↩ 되살리기」(CSS .unhide-btn)."""
    return (
        f'<span class="lab-rest">{icon("trash")} 숨김</span>'
        f'<span class="lab-hover">{icon("undo")} 되살리기</span>'
    )


def _highlight_words_json(highlight_words: list) -> str:
    """하단바 🖍️ 팝오버에 실어 보낼 형광펜 단어 목록 — app.renderer/app.preview_renderer와
    동일한 형식({"word":..., "color_hex":...})."""
    return json.dumps(
        [
            {"word": item["word"], "color_hex": HIGHLIGHT_COLORS[item.get("color", 0) % len(HIGHLIGHT_COLORS)]}
            for item in highlight_words
        ],
        ensure_ascii=False,
    ).replace("</", "<\\/")


def _home_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/home.html"


def _scrap_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/index.html"


def _preview_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/preview.html"


def _hidden_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden"


def _scrapped_urls() -> set:
    """오늘 이미 정식 회차(자동 정기 스크랩)에 실제로 들어간 기사 URL을 모은다.

    [수정: 2026-07-27] 예전엔 "직접 추가한 기사"(manual_articles.json)까지 여기 합쳐서
    똑같이 "✓ 스크랩됨"(초록)으로 표시했는데, 그러면 "임시보드에 담아두기만 한 것"과
    "정식 스크랩 결과에 실제로 포함된 것"이 화면에서 구분이 안 됐다 — "스크랩됨"이라는
    말의 의미가 두 가지로 겹쳐 헷갈린다는 피드백에 따라 분리했다(_pinned_urls 참고).
    """
    return {a["url"] for run in load_today_runs() for a in run["articles"]}


def _pinned_urls() -> set:
    """오늘 "📌"으로 "직접 추가한 기사"(임시보드, app.manual_articles)에 담아둔 기사 URL을 모은다.

    아직 정식 스크랩 결과에 들어간 게 아니라(승격 전) 별도의 임시 목록에 있을 뿐이므로
    _scrapped_urls()와는 다른 상태로 표시한다(app.live_renderer._render_row).
    """
    return {a["url"] for a in load_manual_articles()}


def _auto_drafted_urls(articles: list, settings: dict) -> set:
    """지금 [초안]을 열면 컴퓨터가 자동으로 담았을 것으로 보이는 기사 URL을 근사치로 추정한다.

    정확히 확인하려면 app.preview_renderer._compute_preview_articles와 똑같이 네이버를
    다시 검색해야 하는데, 실시간 현황을 열 때마다 그 검색을 한 번 더 돌리면 네이버 API
    호출이 두 배로 늘어난다(느려짐). 대신 실시간 현황이 이미 받아온 데이터(제목·요약·
    언론사·게시시각)와 설정값만으로 초안 파이프라인의 필터 규칙을 그대로 흉내낸다.

    근사치라 정확히 일치하지 않을 수 있는 부분: 그룹의 AND 모드(그룹 안 키워드를 전부
    포함해야 함)는 재현하지 않고 "스크랩 포함 그룹의 키워드 중 하나라도 제목·요약에
    있으면 매치"로 단순화했고, 완전 동일 제목 기준 중복 제거(deduplicate_by_title)도
    다른 기사와의 비교가 필요해 생략했다 — 실제 초안 화면과 100% 같지 않을 수 있다는
    전제로, "컴퓨터가 이미 선별해서 가져갔겠구나"를 미리 가늠하는 용도로만 쓴다.
    """
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot is None:
        return set()
    slot_start = kst_today_at(slot["start"])

    scrap_groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if group_in_scrap(g)]
    )
    scrap_keywords = []
    seen_keywords = set()
    for group in scrap_groups:
        for keyword in group["keywords"]:
            lowered = keyword.lower()
            if lowered not in seen_keywords:
                seen_keywords.add(lowered)
                scrap_keywords.append(lowered)
    if not scrap_keywords:
        return set()

    outlet_order = settings.get("outlet_order", [])

    result = set()
    for article in articles:
        pub_date = article.get("pub_date")
        if not pub_date:
            continue
        try:
            parsed = datetime.fromisoformat(pub_date)
        except ValueError:
            continue
        if parsed <= slot_start:
            continue
        if outlet_order and article["outlet"] not in outlet_order:
            continue
        haystack = (article["title"] + " " + article.get("summary", "")).lower()
        if not any(keyword in haystack for keyword in scrap_keywords):
            continue
        result.add(article["url"])
    return result


# [추가: 2026-08-19] 실시간 현황 "[단독]·[속보] 필터" — 저장된 고유 제목 1,339건
# (정기 40회차 + 수시 카드) 전수조사: 괄호형 말머리(15+2건)는 전부 제목 맨 앞에만
# 나왔고 정탐이었다. 부분일치로 찾으면 "…장기 거주땐 단독명의" 같은 오탐이 걸린다
# (실측 1건) — 그래서 부분일치가 아니라 "제목 맨 앞" 위치로만 판정한다.
# [수정: 2026-08-20] 정규식·판정 함수를 app.filters로 옮겼다 — 확정본/초안 화면도
# 같은 판정으로 글자색·[단독] 최상단 정렬을 적용하게 되면서 세 화면이 공유하는
# 로직이 됐다(app.filters.headline_kind 참고).


def _render_row(
    article: dict,
    can_add: bool,
    highlight_words: list,
    scrapped_urls: set,
    hidden_urls: set,
    pinned_urls: set,
    auto_drafted_urls: set,
    matched_groups: Optional[list] = None,
    line_template: str = DEFAULT_ARTICLE_LINE_TEMPLATE,
) -> str:
    """기사 한 줄을 렌더링한다.

    [추가: 2026-07-26] 제목을 <details>/<summary>로 감싸 클릭하면 요약이 펼쳐지도록
    했다(자바스크립트 불필요, 스크랩 화면 app.renderer.render_article과 같은 방식).
    요약 텍스트는 애초에 "→ 스크랩" 버튼에 넘기려고 이미 읽어오던 값이라(data-summary),
    추가 네이버 API 호출 없이 그대로 화면에 보여주기만 하면 된다. 제목·요약 둘 다
    형광펜 단어 하이라이트를 적용해 스크랩 화면과 동일한 시각 언어를 유지한다.

    [추가: 2026-07-27] 이미 스크랩된(자동이든 수동이든) 기사는 회색으로 흐리게
    표시하고 버튼도 눌러도 소용없는 "✓ 스크랩됨" 상태로 보여준다 — 페이지를
    새로고침해도 유지된다(수동 추가 직후의 "✓ 추가됨" 표시는 이 클릭의 그 순간에만
    보이던 임시 상태였는데, 이제 저장된 데이터를 기준으로 매번 다시 판단하므로
    새로고침에도 그대로 유지된다).

    [추가: 2026-07-27] 스크랩(초안·완성본 어느 쪽이든)에 있다가 🗑️로 숨겨진 기사는
    "✓ 스크랩됨"과 구분해 "🗑️ 숨김"으로 보여준다 — 원본은 실제로 지워지지 않고
    hidden_articles.json에 URL만 별도로 기록될 뿐이라(app.curation.hide_article),
    실시간 현황에서도 "삭제된 게 아니라 숨김 처리됐을 뿐"이라는 걸 그대로 드러낸다.
    같은 기사가 scrapped_urls에도 있을 수 있는데(숨기기 전엔 스크랩돼 있었을 것이므로),
    이 경우 "숨김" 쪽이 더 최신 상태라 우선한다.

    [추가: 2026-07-27] "📌 담아둔 기사"(임시보드)에만 담겨 있고 아직 정식 스크랩에는
    안 들어간 기사는 "✓ 스크랩됨"(초록)이 아니라 "📌 담아둠"(노란색 계열)으로 따로
    보여준다 — "스크랩됨"이라는 말이 "임시보드에 담아만 둠"과 "정식 결과에 실제로
    포함됨" 두 가지 뜻으로 겹쳐 보인다는 피드백에 따른 구분이다.

    [추가: 2026-07-29] 담아둔 적도 없고 정식 스크랩에도 없지만, 지금 [초안]을 열면
    컴퓨터가 검색 조건에 맞춰 자동으로 담았을 것으로 보이는 기사는(_auto_drafted_urls,
    근사치) 기본 "📌" 버튼 대신 "🤖 자동 선별"로 보여주고 눌러도 소용없게 막는다 —
    "내가 직접 안 담아도 컴퓨터가 이미 가져갔구나"를 실시간 현황에서 바로 알 수 있게
    하기 위해서다. 우선순위는 숨김 > 정식 스크랩됨 > 임시보드에 담아둠(사용자가 직접
    한 행동이라 더 우선) > 자동 선별(추정) > 평범.
    """
    is_hidden = _article_is_hidden(article, hidden_urls)
    is_scrapped = article["url"] in scrapped_urls
    is_pinned = article["url"] in pinned_urls
    is_auto_drafted = article["url"] in auto_drafted_urls
    outlet = html.escape(article["outlet"])
    # [추가: 2026-07-29] 화면에 보이는 이름(outlet_display)만 outlet_display_label을 거친다
    # (매일경제만 빨간 글자로) — outlet(위)은 원본 그대로 둬서 언론사 필터 JS(SCRAP_OUTLETS
    # 비교)와 "→ 스크랩" 버튼 파라미터가 그대로 정확히 매칭되게 한다.
    # [수정: 2026-07-30] outlet_display_label이 이미 이스케이프된 HTML을 돌려주므로
    # 여기서 다시 html.escape()하지 않는다.
    # [수정: 2026-08-14] 기사 URL을 함께 넘겨 oid로 확정된 매일경제 기사에는 표식을 안 붙인다.
    outlet_display = outlet_display_label(article["outlet"], article.get("url"))
    raw_title = article["title"]
    headline_kind = _headline_kind(raw_title)
    if headline_kind:
        # 말머리는 배지로 감싸지 않고 글자색만 준다 — 제목 문자열은 그대로 두고 색만
        # 입히므로, highlight_keywords가 나머지 제목에 적용하는 형광펜 하이라이트와
        # 겹칠 일이 없도록 말머리 구간과 나머지 구간을 나눠 각자 하이라이트를 태운다.
        prefix_len = _HEADLINE_TAG_RE.match(raw_title).end()
        headline_cls = "t-scoop" if headline_kind == "단독" else "t-flash"
        title_html = (
            f'<span class="{headline_cls}">{highlight_keywords(raw_title[:prefix_len], highlight_words)}</span>'
            f"{highlight_keywords(raw_title[prefix_len:], highlight_words)}"
        )
    else:
        title_html = highlight_keywords(raw_title, highlight_words)
    title_attr_escaped = html.escape(raw_title)
    url = html.escape(article["url"])
    raw_summary = article.get("summary", "")
    summary_html = highlight_keywords(raw_summary, highlight_words)
    summary_attr = html.escape(raw_summary)
    # [추가: 2026-07-29] 담아둠/숨김 상태의 버튼은 눌러도 소용없게 막는(disabled) 대신,
    # 각자 자기가 한 일만 되돌리는 토글로 만든다 — 담아둠 재클릭 -> 담아두기 취소(평범
    # 상태로), 숨김 재클릭 -> 숨기기 취소(unhide만, pin 여부는 안 건드림). 두 상태가 서로
    # 독립적이라 "숨김이었다가 되돌리면" 원래 담아둠 상태였으면 담아둠으로, 아니었으면
    # 평범 상태로 돌아간다(둘 다 새로고침으로 서버가 다시 정확히 판단하게 한다 — JS에서
    # 우선순위 로직을 따로 재현하지 않기 위해).
    btn_onclick = "addToScrap(this)"
    # [추가: 2026-08-13] data-status — "아직 처리 안 한 기사만" 필터(applyLiveFilters)가
    # 참고하는 값. "처리 안 한 것"은 담당자·컴퓨터 둘 다 아직 손대지 않은 상태(normal)뿐이다.
    # [수정: 2026-09-01] 그 체크박스의 옛 이름은 "미선정 기사"였다 — 무엇에 선정되지
    # 않았다는 건지 화면 어디에도 없어 이름만 바꿨다(로직·data-status 값은 그대로).
    if is_hidden:
        row_class = "live-row is-hidden"
        data_status = "hidden"
        disabled = ""
        title_attr = UNHIDE_BTN_TITLE
        btn_class = "add-btn added unhide-btn"
        btn_label = unhide_btn_label()
        btn_onclick = "unhideFromLive(this)"
    elif is_scrapped:
        row_class = "live-row is-scrapped"
        data_status = "scrapped"
        disabled = " disabled"
        title_attr = "이미 스크랩된 기사입니다"
        btn_class = "add-btn added is-scrapped-btn"
        btn_label = f'{icon("check")} 스크랩됨'
    elif is_pinned:
        row_class = "live-row is-pinned"
        data_status = "pinned"
        disabled = ""
        title_attr = "담아둔 기사(임시보드)에 있어요 — 눌러서 담아두기를 취소할 수 있어요"
        btn_class = "add-btn added is-pinned-btn"
        btn_label = f'{icon("pin")} 담아둠'
        btn_onclick = "unpinFromLive(this)"
    elif is_auto_drafted:
        row_class = "live-row is-auto-drafted"
        data_status = "auto-drafted"
        disabled = " disabled"
        title_attr = "컴퓨터가 검색 조건에 맞춰 [초안]에 이미 담았을 것으로 보여요 (근사치 추정)"
        btn_class = "add-btn added is-auto-drafted-btn"
        btn_label = f'{icon("bot")} 자동 선별'
    else:
        row_class = "live-row"
        data_status = "normal"
        disabled = "" if can_add else " disabled"
        title_attr = "담아둔 기사(임시보드)에 담아둬요" if can_add else "아직 오늘 첫 회차가 없어요"
        btn_class = "add-btn"
        btn_label = f'{icon("pin")} 담아두기'
    pub_date = article.get("pub_date")
    pub_time_html = ""
    if pub_date:
        try:
            parsed = datetime.fromisoformat(pub_date)
        except ValueError:
            parsed = None
        if parsed:
            escaped_pub_date = html.escape(pub_date)
            pub_time_html = (
                f'<span class="pub-time">{parsed.strftime("%H:%M")} · '
                f'<span class="pub-time-relative" data-pub-date="{escaped_pub_date}"></span></span>'
            )
    else:
        # [추가: 2026-08-14] pubDate 파싱 실패 기사(app.naver_api._search_one_keyword,
        # include_unparsed_dates=True) — 시각을 몰라 "N분 전" 계산도 불가능하므로, 담당자가
        # 원문을 직접 열어 확인하도록 그 사실을 그대로 알린다("바로 앞 기사 시각" 같은
        # 추정치는 오늘 기사라는 보증처럼 오해될 수 있어 일부러 안 보여준다 — 사용자 결정).
        pub_time_html = '<span class="pub-time pub-time-unknown">발행시각 불명</span>'
        row_class += " is-undated"
    # [추가: 2026-08-25] 🔍 검색어 — 이 기사가 어떤 검색어로 걸렸는지를 확정본·초안과
    # 같은 형태(돋보기 + 회색 글자)로 보여준다. 위 그룹 칩이 "어느 그룹에 걸렸나"라면
    # 이건 한 단계 좁힌 "그 그룹의 어느 검색어에 걸렸나"이고, 상단 "키워드별 건수"·헤비
    # 키워드 경고가 가리키는 그 키워드를 행에서 바로 확인할 수 있게 한다.
    # 값은 이미 손에 있다 — _matched_groups_for가 읽는 article["matched_keywords"] 그대로라
    # 네이버 추가 호출도 저장도 없다(이 화면은 매 렌더링마다 캐시된 키워드별 원시 결과에서
    # 다시 계산한다). 실측(2026-08-25, 캐시된 576건): 77%가 키워드 1개, 중앙값 5자,
    # 95%가 20자 이내, 최대 8개 53자 — 메타 줄을 밀어낼 길이가 아니라 확정본과 같이
    # 접기(+N) 없이 전부 보여준다. 제목 문자열에 섞지 않은 별도 <span>이라 복사·.txt
    # 텍스트에는 안 나간다(CODING_CONVENTIONS.md §4).
    matched_keywords = [k for k in (article.get("matched_keywords") or []) if k]
    kw_inline_html = (
        '<span class="kw-inline" title="이 기사가 걸린 검색어">'
        f'{icon("search", "ic kw-ic")}'
        f'<span class="kw-v">{html.escape(", ".join(matched_keywords))}</span></span>'
        if matched_keywords
        else ""
    )
    # [추가: 2026-08-11] 제목에 [포토] 표식이 없는 통신사 사진기사 추정 배지 — 이 화면은
    # "날것 그대로"가 원칙이라 걸러내지 않고 표시만 한다(정기 스크랩·초안에서는 설정이
    # 꺼져 있으면 실제로 제외된다). 언론사·게시시각 줄에 붙여 제목 줄은 건드리지 않는다.
    # [수정: 2026-09-03] 배지가 두 층을 겸하게 되면서(제목 [포토] 표식 / 표식 없는 추정)
    # 툴팁만 갈라 놓는다 — 옛 문구는 [포토]가 붙은 기사에는 거짓말이다.
    photo_badge_html = (
        f'<span class="photo-badge" title="{html.escape(photo_badge_tip(article.get("title", ""), article.get("url", "")))}">'
        f'{icon("camera")} 사진 추정</span>'
        if looks_like_photo_caption(article)
        else ""
    )
    # [수정: 2026-07-29] 이미 숨겨진 행(is_hidden)만 빼고, 나머지 모든 상태(평범·담아둠·
    # 스크랩됨·자동 선별)에 전부 작은 숨기기 아이콘을 보여준다 — 처음엔 담아둠 상태에만
    # 뒀는데, 애초에 담아두지도 않은(평범 상태) 기사도 초안까지 안 가고 바로 숨기고
    # 싶다는 요청(원래 이 기능을 요청한 이유 자체가 "초안까지 안 넘어가도 되게"였다).
    hide_icon_html = ""
    if not is_hidden:
        hide_icon_html = (
            f'<button class="hide-from-live-btn" type="button" data-outlet="{outlet}" '
            f'data-title="{title_attr_escaped}" data-url="{url}" '
            f'data-pub-date="{html.escape(pub_date or "")}" '
            f'onclick="hideFromLive(this)" title="이 기사 숨기기 (되돌리기 가능)">{icon("trash")}</button>'
        )
    # [수정: 2026-08-13] 🔄 원문 다시 불러오기 / ✏️ 직접 수정을 app.renderer와 동일한
    # "⋯ 더보기" 메뉴로 옮겼다 — 그룹 칩(아래)이 새로 생기면서 아이콘 4개(📌·🔄·✏️·🗑️)가
    # 한 줄에 다 있으면 칩과 자리를 다툰다는 지적(사용자) — 📌(자주·바로 눌러야 함)와
    # 🗑️(마찬가지)는 밖에 남기고, 상대적으로 덜 급한 둘만 메뉴로 접었다. URL 줄은
    # 그대로 유지한다("원문 보기는 접근이 빨라야 한다"는 사용자 요청 — 메뉴 뒤로
    # 숨기지 않고 항상 보이는 링크로 남긴다).
    # [추가: 2026-08-13] 이 화면에도 "복사하기"를 넣었다(사용자 요청) — 실시간 현황에서
    # 기사 한 건만 바로 복사해야 하는 경우가 있는데, 여기만 빠져 있었다.
    # [수정: 2026-09-01] 그 자리가 메뉴 안에서 담아두기 옆으로 나왔다(아래 copy_btn_html). 복사 본문은
    # 확정본·초안·지난 기사와 완전히 같은 줄 형식(기사 제목 형식 설정 + URL 줄)이라
    # 어느 화면에서 복사했든 붙여넣은 결과가 같다. 언론사는 화면 표시용 이름
    # (outlet_display_label의 "매일경제**")이 아니라 원본 값을 쓴다 — 화면 전용 표식이
    # 보고서 텍스트로 새면 안 되기 때문(CODING_CONVENTIONS.md §4).
    copy_line = apply_line_template(line_template, article["outlet"], raw_title)
    copy_text_attr = html.escape(f"{copy_line}\n{article['url']}")
    # [수정: 2026-09-01] "복사하기"를 메뉴에서 꺼내 담아두기 바로 오른쪽에 상시 노출한다
    # (사용자 요청) — 실시간 현황에서 기사 한 건을 그때그때 집어 붙여넣는 일이 잦은데
    # 매번 ⋯를 한 번 더 눌러야 했다. 메뉴에는 원문 다시 불러오기·제목·요약 직접 수정
    # 둘만 남는다(둘 다 "가끔 한 번"이라 메뉴 뒤가 맞는 무게).
    copy_btn_html = (
        f'<button class="copy-btn" type="button" data-copy-text="{copy_text_attr}" '
        'onclick="event.stopPropagation(); copyArticleIcon(this);" '
        'title="복사하기" aria-label="복사하기">'
        f'<span class="icon-default">{icon("copy")}</span>'
        f'<span class="icon-done">{icon("check")}</span>'
        "복사</button>"
    )
    more_menu_html = (
        '<span class="more-wrap">'
        '<button class="more-btn" type="button" '
        'onclick="event.stopPropagation(); toggleArticleMenu(this);" '
        f'title="더보기" aria-label="더보기">{icon("dots")}</button>'
        '<span class="more-menu">'
        f'<button type="button" data-url="{url}" '
        'onclick="event.stopPropagation(); closeArticleMenus(); refetchSummary(this);">원문 다시 불러오기</button>'
        f'<button type="button" data-url="{url}" '
        f'data-title="{title_attr_escaped}" data-summary="{summary_attr}" '
        'onclick="event.stopPropagation(); closeArticleMenus(); editSummary(this);">제목·요약 직접 수정</button>'
        "</span>"
        "</span>"
    )
    # [추가: 2026-08-13] 그룹 필터 칩 — 이 기사가 실제로 걸린 키워드가 속한 그룹들을
    # 작은 태그로 보여준다. 이름이 길면(칩 자체가 필터 바에서도 잘리므로) 여기서도
    # CSS로만 말줄임(ellipsis)하고 값은 title 툴팁으로 전체를 보여준다 — 저장값은
    # 그대로 두고 "보여주는 자리"에서만 자른다(사용자 결정).
    matched_groups = matched_groups or []
    group_tags_html = "".join(
        f'<span class="row-group-tag" title="{html.escape(g)}">{html.escape(g)}</span>' for g in matched_groups
    )
    data_groups_attr = html.escape("|".join(matched_groups))
    data_headline_attr = html.escape(headline_kind or "")
    # [추가: 2026-09-16] `[사설]` 체크박스용 표식 — data-headline에 섞지 않고 속성을 따로
    # 둔다. 그 값은 app.filters.headline_kind(맨 앞 [단독]/[속보])가 만든 것이고 제목
    # 글자색도 같은 값을 보는데, 사설은 판정 규칙(위치 무관)도 쓰임새(필터 하나)도
    # 달라서다. 실측상 둘이 한 제목에 같이 붙는 경우는 0건이지만, 그 우연에 기대면
    # 언젠가 한쪽이 다른 쪽을 덮어쓴다.
    data_editorial_attr = "1" if _is_editorial(raw_title) else ""
    return (
        f'<div class="{row_class}" data-outlet="{outlet}" data-status="{data_status}" '
        f'data-groups="{data_groups_attr}" data-headline="{data_headline_attr}" '
        f'data-editorial="{data_editorial_attr}">'
        f'<div class="live-row-text"><span class="outlet-tag">{outlet_display}</span>{group_tags_html}{pub_time_html}{kw_inline_html}{photo_badge_html}'
        '<details class="live-title">'
        f"<summary>{title_html}</summary>"
        f'<p class="live-summary">{summary_html}</p>'
        "</details>"
        f'<div class="live-url"><a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a></div></div>'
        '<div class="live-row-actions">'
        f'<button class="{btn_class}" type="button" data-outlet="{outlet}" data-title="{title_attr_escaped}" '
        f'data-url="{url}" data-summary="{summary_attr}" data-pub-date="{html.escape(pub_date or "")}" '
        f'onclick="{btn_onclick}"{disabled} title="{title_attr}">{btn_label}</button>'
        f"{copy_btn_html}{more_menu_html}{hide_icon_html}"
        "</div>"
        "</div>"
    )


# [추가] 처음 열었을 때 펼쳐둘 시간대 수 — 최근 두 시간대. 새로 들어온 기사는 바로
# 보이면서, 그 아래로는 열 줄 남짓으로 접혀 하루치가 한눈에 들어온다.
HOUR_GROUPS_OPEN = 2


def _pub_hour(article: dict):
    """기사의 발행 시각(시 단위). 못 읽으면 None — 그런 기사는 애초에 이 목록에 안 온다."""
    raw = article.get("pub_date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).hour
    except (ValueError, TypeError):
        return None


def _hour_groups_html(articles: list, row_fn) -> str:
    """발행시각 목록을 시간대별 <details>로 묶는다 (최신순 입력 순서를 그대로 유지).

    묶는 축은 발행 시각의 "시"뿐이다 — 정기 회차 창으로 묶지 않는다. 회차는 하루를 다
    덮지 않고(마지막 회차 뒤에 나온 기사는 어느 회차에도 안 속한다) 요일 그룹에 따라
    칸 이름·개수까지 바뀌어서, 그날그날 모양이 달라지는 서랍이 된다.

    건수는 서버가 그린 값이고, 필터가 걸리면 화면의 syncHourGroups가 보이는 만큼으로
    고쳐 쓴다(상단 누적 건수와 같은 축).
    """
    groups: list = []
    for article in articles:
        hour = _pub_hour(article)
        if not groups or groups[-1]["hour"] != hour:
            groups.append({"hour": hour, "articles": []})
        groups[-1]["articles"].append(article)

    parts = []
    for index, group in enumerate(groups):
        hour = group["hour"]
        label = "시각 미상" if hour is None else f"{hour}시대"
        range_label = "" if hour is None else f"{hour:02d}:00 ~ {hour:02d}:59"
        classes = "hour-group hg-latest" if index == 0 else "hour-group"
        open_attr = " open" if index < HOUR_GROUPS_OPEN else ""
        rows = "".join(row_fn(a) for a in group["articles"])
        parts.append(
            f'<details class="{classes}"{open_attr}>'
            f'<summary>{icon("flow_next", "ic hg-caret")}'
            f'<span class="hg-time">{label}</span>'
            f'<span class="hg-range">{range_label}</span>'
            f'<span class="hg-count">{len(group["articles"])}건</span></summary>'
            f'<div class="hg-body">{rows}</div>'
            "</details>"
        )
    return "".join(parts)


def _hour_group_bar_html() -> str:
    """시간대 묶음 위의 한 줄 — 묶기 끄기(예전 한 줄 목록)와 모두 펼치기/접기."""
    return (
        '<div class="hg-bar">'
        '<button type="button" class="hg-toggle is-on" aria-pressed="true" '
        'onclick="toggleHourGrouping(this)">시간대별 묶기</button>'
        '<button type="button" class="hg-link" onclick="openAllHourGroups()">모두 펼치기</button>'
        '<span class="hg-sep">·</span>'
        '<button type="button" class="hg-link" onclick="closeAllHourGroups()">모두 접기</button>'
        "</div>"
    )


def _matched_groups_for(article: dict, groups: list) -> list:
    """이 기사가 실제로 걸린 키워드가 속한 그룹 이름들을 groups 등록 순서대로 돌려준다.

    한 그룹의 키워드 중 하나라도 이 기사의 matched_keywords에 있으면 그 그룹에 속한다 —
    이 화면은 그룹의 OR/AND 검색 모드와 무관하게 "이 그룹 키워드가 하나라도 있었다"는
    사실 그대로를 보여준다(검색 자체의 AND 판정과는 별개의, 사후 라벨링일 뿐이다).
    """
    matched = set(article.get("matched_keywords", []))
    if not matched:
        return []
    return [g["name"] for g in groups if matched & set(g["keywords"])]


def _heavy_keyword_warning(articles: list) -> str:
    """검색어 하나가 전체 결과의 대부분을 차지하고 있으면 상단에 안내한다.

    [추가: 2026-08-24] 앱 버그가 아니라 담당자의 검색어 선택 문제다(HISTORY.md 같은
    섹션 참고 — 실측: 등록 키워드 21개 중 '코스피' 하나가 그날 결과 1,968건 중
    33.3%(656건)를 차지해 화면 무게·새로고침 대기시간의 상당 부분을 만들고 있었다,
    2위와도 10%p 넘게 벌어진 뚜렷한 이상치였다). 그래서 코드가 강제로 걸러내지 않고,
    담당자가 스스로 알아채고(설정 > 키워드 화면에서 더 좁은 말로 바꾸거나 그룹을
    '모두 포함'으로 묶는 등) 조치할 수 있도록 안내만 한다 — 문턱은
    app.config.LIVE_HEAVY_KEYWORD_RATIO/LIVE_HEAVY_KEYWORD_MIN_COUNT.

    matched_keywords 집계는 _filter_bar_html의 "키워드별 건수"와 같은 방식(기사가
    여러 키워드에 걸리면 각각 센다)이라, 배지에 적힌 건수·비율이 그 disclosure를
    펼쳤을 때 보이는 숫자와 항상 일치한다.
    """
    if not articles:
        return ""
    keyword_counts: dict = {}
    for a in articles:
        for kw in a.get("matched_keywords", []):
            keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
    if not keyword_counts:
        return ""
    heaviest_kw, heaviest_count = max(keyword_counts.items(), key=lambda kv: kv[1])
    if heaviest_count < LIVE_HEAVY_KEYWORD_MIN_COUNT:
        return ""
    ratio = heaviest_count / len(articles)
    if ratio < LIVE_HEAVY_KEYWORD_RATIO:
        return ""
    return (
        f'<div class="live-heavy-kw-warning">{icon("alert")} 검색어 \'{html.escape(heaviest_kw)}\'가 '
        f"전체 결과의 {round(ratio * 100)}%({heaviest_count}/{len(articles)}건)를 차지하고 "
        "있습니다 — 너무 넓은 검색어일 수 있어요. 설정 > 키워드 화면에서 더 구체적인 "
        "말로 바꾸거나, 그룹의 검색 방식을 '모두 포함'으로 바꿔 다른 검색어와 함께 좁히는 "
        "걸 고려해보세요.</div>"
    )


def _filter_bar_html(articles: list, groups: list) -> str:
    """검색어 필터 바 — 제목 검색, 그룹 칩(펼치면 키워드별 건수).

    [추가: 2026-08-13] 키워드가 20~30개로 늘어도 이 화면은 담당자 전용이라 그룹
    단위로 묶어 보여주는 게 낫다는 판단(사용자) — 그룹 칩은 OR로 겹쳐 켤 수 있다
    (체크박스 여러 개 = 관행적으로 "이거 아니면 저거"로 읽히므로 AND가 아니라 OR).
    좁히고 싶으면 제목 검색을 같이 쓰면 된다. 전부 클라이언트 JS로 처리한다 —
    이미 서버가 다 검색해 렌더링해둔 기사 목록을 걸러 보여주기만 하면 되므로
    서버 왕복이 필요 없다(applyLiveFilters, 아래 스크립트).

    [수정: 2026-08-19] "아직 처리 안 한 기사만"은 여기 있었으나 위쪽 특수조건 줄(선택 언론사만·
    [단독]·[속보]와 같은 자리)로 옮겼다 — 이 칩만 그룹 칩들과 달리 혼자 AND로 동작해
    OR인 나머지 칩들과 논리가 어긋나 있었다(render_live_page 쪽 body 참고).
    """
    if not groups:
        return ""
    group_counts: dict = {}
    keyword_counts: dict = {}
    keyword_group: dict = {}
    for g in groups:
        for kw in g["keywords"]:
            keyword_group.setdefault(kw, g["name"])
    for a in articles:
        for name in _matched_groups_for(a, groups):
            group_counts[name] = group_counts.get(name, 0) + 1
        for kw in a.get("matched_keywords", []):
            keyword_counts[kw] = keyword_counts.get(kw, 0) + 1

    chips_html = "".join(
        f'<button type="button" class="group-chip" data-group="{html.escape(name)}" '
        f'title="{html.escape(name)}" onclick="toggleGroupChip(this)">'
        f'<span class="chip-label">{html.escape(name)}</span> '
        f'<span class="chip-count">{group_counts.get(name, 0)}</span></button>'
        for name in (g["name"] for g in groups)
        if group_counts.get(name)
    )
    breakdown_rows = "".join(
        f'<div class="kw-row"><span>{html.escape(keyword_group.get(kw, ""))} · {html.escape(kw)}</span>'
        f"<span>{count}</span></div>"
        for kw, count in sorted(keyword_counts.items(), key=lambda kv: kv[1], reverse=True)
    )
    # [수정: 2026-09-16] 필터 한 상자(.filter-box)의 2행 — 제목 검색은 1행(체크박스 줄)으로
    # 옮겼다(_title_search_html). 그룹이 없으면 예전처럼 아무것도 안 그린다.
    return (
        f'<div class="fb-row chip-row">{chips_html}'
        '<button type="button" class="kw-breakdown-toggle" onclick="toggleKeywordBreakdown()">'
        "키워드별 건수 ▾</button>"
        "</div>"
        f'<div class="keyword-breakdown" id="keyword-breakdown">{breakdown_rows}</div>'
    )


def _title_search_html(groups: list) -> str:
    """필터 상자 1행 맨 앞의 제목 검색칸.

    [추가: 2026-09-16] 예전엔 그룹 칩 상자(_filter_bar_html) 안에 있었다 — 두 상자를 하나로
    합치며 체크박스 줄 앞으로 옮겼다. 그 상자와 같은 조건(등록된 검색어 그룹이 있을 때만)을
    그대로 지켜, 그룹이 0개인 화면은 예전과 똑같이 검색칸이 없다.
    """
    if not groups:
        return ""
    return (
        '<input type="text" id="live-title-search" class="filter-search-input" '
        'placeholder="제목 검색" oninput="applyLiveFilters()">'
    )


def render_live_page(
    articles: list,
    can_add: bool,
    generated_at: str,
    highlight_words: Optional[list] = None,
    outlet_order: Optional[list] = None,
    groups: Optional[list] = None,
    line_template: Optional[str] = None,
    failed_keywords: Optional[list] = None,
) -> str:
    """실시간 기사 현황 화면을 렌더링한다. 소제목 분류는 없지만(예정된 회차만의 기능),
    [수정: 2026-07-26] 제목 클릭 시 요약이 펼쳐지고 형광펜 하이라이트가 적용되는 건
    스크랩 화면과 동일하다 — 큐레이션(숨기기/순서변경)은 여전히 없다("스크랩"으로
    옮긴 뒤에만 가능).

    [추가: 2026-07-26] "☑️ 스크랩 언론사만 보기" 체크박스 — 언론사가 너무 다양하게 섞여
    노이즈가 심하다는 피드백으로 추가했다. 실제 검색 범위(전체 언론사 라이브 미러)는
    그대로 두고, 이미 불러온 결과를 화면에서만 /outlets에 등록된 언론사 기준으로
    걸러 보여준다(서버 재검색 없음, 저장 불필요, 새로고침하면 꺼진 상태로 리셋) —
    정기 스크랩처럼 수집 자체를 좁히면 두 화면의 기능이 겹쳐버리기 때문에 화면
    필터로만 구현한다.

    [수정: 2026-07-28] 제목 중복 제외·포토/현장 기사 제외 옵션은 없앴다 — 이 화면은
    "네이버에 지금 실제로 떠 있는 걸 날것 그대로 보여주는" 실시간 미러가 목적이라,
    그런 선별/가공은 스크랩 초안·완성본 단계에서 하는 게 맞다는 판단(사용자 피드백:
    "실시간은 실시간이어야 되거든"). generate_live_page도 더 이상 이 두 필터를
    적용하지 않는다.

    [추가: 2026-07-29] "🤖 자동 선별" 표시 — 컴퓨터가 검색 조건에 맞춰 지금 [초안]에
    이미 담았을 것으로 보이는 기사를 근사치로 추정해 표시한다(_auto_drafted_urls).

    [추가: 2026-08-13] groups(app.settings.active_search_groups 형태, 이름 포함)를
    주면 기사마다 어떤 그룹 키워드에 걸렸는지 칩으로 보여주고, 그 위에 그룹 필터
    바(제목 검색·그룹 칩·"아직 처리 안 한 기사만"·키워드별 건수)를 그린다. 생략하면(옛
    호출부 host 호환) 필터 바 없이 예전과 동일하게 그린다.

    [추가: 2026-08-13] failed_keywords — 이번 검색에서 재시도까지 다 썼는데도 실패한
    키워드 이름 목록(app.naver_api.search_articles_by_groups). 있으면 상단에 어떤
    키워드가 실패했는지 이름으로 알리는 경고 배너를 보여준다(§3: 실패는 숫자로
    알린다) — 안 그러면 "그 키워드는 오늘 기사가 없다"와 "네트워크 오류로 못 봤다"가
    화면에서 구분되지 않는다.

    [추가: 2026-08-14] articles 중 pub_date가 None인 기사(pubDate 파싱 실패,
    app.naver_api._search_one_keyword include_unparsed_dates=True)는 위 시간순 목록·
    상단 건수 집계에서 빼서 하단 "발행시각을 못 읽은 기사" 별도 구역에 모은다 — 0건일
    땐 배너도 구역도 아예 안 그린다. 그 구역의 행도 같은 .live-row 클래스라 제목
    검색·언론사 필터·그룹 칩에 똑같이 걸린다(applyLiveFilters는 상단 건수에서만
    is-undated 행을 제외한다).
    """
    highlight_words = highlight_words if highlight_words is not None else []
    outlet_order = outlet_order if outlet_order is not None else []
    groups = groups if groups is not None else []
    failed_keywords = failed_keywords if failed_keywords is not None else []
    # [추가: 2026-08-13] "복사하기"가 쓰는 기사 줄 형식 — 다른 화면과 같은 설정값을 쓴다.
    line_template = line_template if line_template is not None else DEFAULT_ARTICLE_LINE_TEMPLATE
    dated_articles: list = []
    undated_articles: list = []
    undated_html = ""
    if not articles:
        rows_html = '<p class="empty">💤</p>'
    else:
        # [추가: 2026-08-14] pubDate 파싱 실패 기사(pub_date=None)는 시간순 목록·상단
        # 건수 집계에서 뺀다 — "오늘 기사"인지조차 확인 안 됐으므로 당일 누적 건수에
        # 섞으면 그 숫자 자체가 부정확해진다. 대신 버리지 않고 하단 별도 구역에 모아
        # 보여준다(아래 undated_html) — 검색·필터는 그대로 걸리도록 같은 .live-row
        # 클래스를 쓴다(_render_row가 is-undated를 덧붙인다).
        dated_articles = [a for a in articles if a.get("pub_date")]
        undated_articles = [a for a in articles if not a.get("pub_date")]
        scrapped_urls = _scrapped_urls()
        hidden_urls = load_hidden_urls()
        pinned_urls = _pinned_urls()
        auto_drafted_urls = _auto_drafted_urls(articles, load_settings())

        def _row(a: dict) -> str:
            return _render_row(
                a, can_add, highlight_words, scrapped_urls, hidden_urls, pinned_urls, auto_drafted_urls,
                matched_groups=_matched_groups_for(a, groups),
                line_template=line_template,
            )

        # [수정] 시간대별로 묶어 접어 둔다 — 하루치가 200건 가까이 쌓여 한 줄 목록으로는
        # 훑기 어렵다(사용자 요청). 서버가 내려주는 기사는 그대로이고 묶기·접기만 화면이다.
        rows_html = (
            _hour_group_bar_html()
            + '<div class="live-list" id="live-list">'
            + _hour_groups_html(dated_articles, _row)
            + "</div>"
        ) if dated_articles else '<p class="empty">💤</p>'
        if undated_articles:
            undated_html = (
                '<div class="live-undated-section" id="live-undated-section">'
                f'<h2 class="live-undated-heading">{icon("alert")} 발행시각을 못 읽은 기사 '
                f'({len(undated_articles)}건)</h2>'
                '<p class="live-undated-desc">네이버가 보낸 발행시각을 읽지 못해 위 시간순 '
                '목록에는 넣지 못했습니다. 오늘 기사인지도 확인되지 않았으니 원문을 열어 '
                "직접 확인하세요.</p>"
                f'{"".join(_row(a) for a in undated_articles)}'
                "</div>"
            )

    filter_disabled = "" if outlet_order else " disabled"
    filter_title_attr = (
        "" if outlet_order else ' title="아직 스크랩 언론사가 지정되지 않았어요 (설정에서 선택하면 활성화됩니다)"'
    )
    warning_html = ""
    if failed_keywords:
        failed_list = ", ".join(html.escape(kw) for kw in failed_keywords)
        warning_html = (
            f'<div class="live-warning">{icon("alert")} 키워드 검색 실패({len(failed_keywords)}개): '
            f"{failed_list} — 잠시 후 새로고침하면 다시 시도합니다.</div>"
        )
    # [추가: 2026-08-14] 평소(파싱 실패 0건)엔 아예 안 뜬다 — §3(실패는 조용히 사라지면
    # 안 된다)이지만 0건까지 매번 보여주면 그 자체가 노이즈다.
    if undated_articles:
        warning_html += (
            f'<div class="live-warning">{icon("alert")} 발행시각을 못 읽은 기사 '
            f'{len(undated_articles)}건 — <a href="#live-undated-section">맨 아래 구역에서 '
            "확인하세요</a></div>"
        )
    # [추가: 2026-08-24] articles(전체, undated 포함)를 넘긴다 — 아래 _filter_bar_html의
    # "키워드별 건수"도 같은 집합을 세므로, 이 배지에 적힌 건수·비율이 그 disclosure를
    # 펼쳤을 때 보이는 숫자와 항상 맞아떨어진다.
    warning_html += _heavy_keyword_warning(articles)
    body = (
        f"{warning_html}"
        '<div class="filter-box">'
        f'<div class="fb-row">{_title_search_html(groups)}'
        f'<label{filter_title_attr}>'
        f'<input type="checkbox" id="scrap-outlet-only" onchange="applyLiveFilters()"{filter_disabled}>'
        "선택 언론사만</label>"
        '<label><input type="checkbox" id="headline-scoop-only" onchange="applyLiveFilters()">[단독]</label>'
        '<label><input type="checkbox" id="headline-flash-only" onchange="applyLiveFilters()">[속보]</label>'
        '<label><input type="checkbox" id="headline-editorial-only" onchange="applyLiveFilters()">[사설]</label>'
        '<label title="숨김·스크랩됨·담아둠·자동 선별 — 담당자나 앱이 이미 손댄 기사를 '
        '모두 보이지 않게 합니다">'
        '<input type="checkbox" id="unclaimed-only" onchange="applyLiveFilters()">아직 처리 안 한 기사만</label>'
        "</div>"
        f"{_filter_bar_html(articles, groups)}"
        "</div>"
        f"{rows_html}"
        f"{undated_html}"
    )
    return _PAGE_TEMPLATE.format(
        **_theme(),
        body=body,
        refresh_button_html=_REFRESH_BUTTON_ENABLED,
        head_stat_html=(
            f'<span class="head-stat"><span id="stat-count">{len(dated_articles)}건</span>'
            f' · 0시~{generated_at} 누적 · <span id="stat-scope">전체 언론사</span></span>'
        ),
        extra_head_html="",
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        preview_href=_preview_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        outlet_order_json=json.dumps(outlet_order, ensure_ascii=False),
        highlight_words_json=_highlight_words_json(highlight_words),
    )


def render_live_error_page() -> str:
    body = (
        '<div class="error-box">🔌 지금은 조회할 수 없어요.<br>'
        "잠시 후 새로고침 버튼을 다시 눌러주세요.</div>"
    )
    return _PAGE_TEMPLATE.format(
        **_theme(),
        body=body,
        refresh_button_html=_REFRESH_BUTTON_ENABLED,
        head_stat_html="",
        extra_head_html="",
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        preview_href=_preview_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        outlet_order_json="[]",
        highlight_words_json=_highlight_words_json(load_settings().get("highlight_keywords", [])),
    )


def render_live_not_configured_page() -> str:
    """네이버 키가 없을 때 — [추가: 2026-08-20] app.config가 더 이상 키 없음을
    RuntimeError로 막지 않으므로, 이 화면이 그 상태를 만날 수 있게 됐다. 검색을
    아예 시도하지 않고 바로 이 화면을 돌려준다 — 그냥 render_live_error_page를
    재사용하면 "지금은 조회할 수 없다"는 일시적 오류처럼 읽혀 새로고침을 부추기는데,
    실제로는 새로고침 백 번 해도 안 바뀌는 상태(키를 등록해야 풀림)라 오해를 막으려
    원인과 등록 링크를 담은 전용 화면을 둔다."""
    naver_href = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/naver"
    body = (
        '<div class="error-box">🔑 네이버 검색 API 키가 없어 기사를 가져올 수 없어요.<br>'
        f'<a href="{naver_href}" style="color:{COLOR_ACCENT};font-weight:600;">'
        "설정 &gt; 연동에서 키를 등록해주세요 →</a></div>"
    )
    return _PAGE_TEMPLATE.format(
        **_theme(),
        body=body,
        refresh_button_html=_REFRESH_BUTTON_ENABLED,
        head_stat_html="",
        extra_head_html="",
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        preview_href=_preview_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        outlet_order_json="[]",
        highlight_words_json=_highlight_words_json(load_settings().get("highlight_keywords", [])),
    )


def render_live_loading_page(keyword_count: int) -> str:
    """콜드 캐시(오늘 처음 보는 키워드가 있음) 상태에서 즉시 돌려주는 대기 화면.

    [추가: 2026-08-13] 실제 검색(수십~백여 초)은 별도 스레드에서 계속 진행 중이고
    (_ensure_live_generation_running), 이 화면은 그동안 사용자에게 보여줄 뿐이다 —
    3초마다 자동 새로고침하다가, 백그라운드 검색이 끝나 캐시가 데워지면 다음
    새로고침이 자연히 진짜 페이지로 바뀐다(별도의 "완료 신호"를 안 만들어도 됨 —
    generate_live_page_or_wait이 그때는 콜드가 아니므로 빠른 경로로 빠진다).

    새로고침 버튼은 disabled 문구가 아니라 아예 <span>(_REFRESH_BUTTON_DISABLED)으로
    바꿔 눌리지 않게 한다 — 자동 새로고침 경로와 별개로 사람이 수동으로 눌러도
    똑같이 막아야 중복 검색(429 위험 구간 재진입)을 완전히 막을 수 있다.
    """
    body = (
        '<div class="loading-box">'
        f'<div class="loading-spin loading-spin-icon">{icon("refresh")}</div>'
        '<p class="loading-title">기사를 불러오고 있습니다</p>'
        f'<p class="loading-desc">등록된 키워드 {keyword_count}개를 검색하는 중입니다.<br>'
        "오늘 처음 여실 때만 시간이 걸리고, 이후에는 바로 열립니다.</p>"
        f'<div class="loading-reassure">{icon("check")} '
        "준비되면 자동으로 화면이 바뀝니다 — 새로고침하지 않으셔도 됩니다.</div>"
        "</div>"
    )
    return _PAGE_TEMPLATE.format(
        **_theme(),
        body=body,
        refresh_button_html=_REFRESH_BUTTON_DISABLED,
        head_stat_html="",
        extra_head_html='<meta http-equiv="refresh" content="3">',
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        preview_href=_preview_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        outlet_order_json="[]",
        highlight_words_json=_highlight_words_json(load_settings().get("highlight_keywords", [])),
    )


def _unique_keywords(groups: list) -> list:
    """그룹 목록에서 중복 없는 키워드 목록을 등록 순서대로 뽑는다(그룹 사이 중복 제거)."""
    seen = set()
    result = []
    for group in groups:
        for keyword in group["keywords"]:
            if keyword not in seen:
                seen.add(keyword)
                result.append(keyword)
    return result


def _live_search_plan(unique_keywords: list) -> tuple:
    """캐시에서 키워드별 "어디부터 검색할지"와 "이미 알고 있는 원시 결과"를 꺼낸다.

    [수정: 2026-08-13] 캐시 단위를 그룹 전체에서 키워드 하나하나로 바꿨다
    (app.live_cache) — 키워드를 하나 추가해도 나머지 키워드는 재검색하지 않기
    위해서다(사용자 피드백: "바로바로 키워드 추가하고 바로 반영하려고 만든 기능인데
    키워드가 많아질수록 버거워진다"). 그룹 이름·모드·소속이 바뀌어도 이 함수는 영향받지
    않는다 — 그룹 매칭은 항상 app.naver_api.match_articles_to_groups가 "지금" 설정으로
    새로 계산하므로, 여기선 "이 키워드를 전에 검색해본 적 있는가"만 안다.

    [수정: 2026-08-21] 하한을 캐시의 last_seen 그대로 쓰지 않고 app.naver_api.
    incremental_search_after에 통과시킨다 — 네이버가 발행시각 순서대로 색인하지 않아,
    그대로 쓰면 뒤늦게 색인된 (발행시각은 과거인) 기사가 이 화면에서 하루 종일 안 보였다
    (그쪽 docstring 참고). 캐시에 **저장**하는 값은 예전 그대로 "가장 최신 pub_date"이고,
    물러서는 건 읽는 이 시점뿐이다 — 그래서 stored_last_seen도 같이 돌려준다(검색이
    실패한 키워드는 물러선 값이 아니라 이 원래 값을 그대로 다시 저장해야 한다).

    반환: (after_map, known_results_by_keyword, stored_last_seen)
      after_map: {키워드: datetime|None} — app.naver_api.search_keywords에 그대로
        넘긴다. None(처음 보는 키워드)이면 당일 0시부터 검색한다.
      known_results_by_keyword: {키워드: [기사, ...]} — 캐시에 있던 원시 결과
        (처음 보는 키워드는 빈 목록).
      stored_last_seen: {키워드: ISO 문자열} — 캐시에 저장돼 있던 값 그대로.
    """
    cache = load_live_cache() or {}
    keyword_last_seen = cache.get("keyword_last_seen", {})
    keyword_articles = cache.get("keyword_articles", {})
    midnight = kst_today_at("00:00")
    after_map = {
        keyword: (
            incremental_search_after(datetime.fromisoformat(keyword_last_seen[keyword]), midnight)
            if keyword in keyword_last_seen
            else None
        )
        for keyword in unique_keywords
    }
    known_results_by_keyword = {keyword: keyword_articles.get(keyword, []) for keyword in unique_keywords}
    stored_last_seen = {k: v for k, v in keyword_last_seen.items() if k in set(unique_keywords)}
    return after_map, known_results_by_keyword, stored_last_seen


def generate_live_page() -> Path:
    """요청이 올 때마다 등록된 키워드 그룹으로 라이브 재검색해 live.html을 새로 만든다.
    예정된 회차 수집(app.scraper.collect_run)과는 완전히 별개 파이프라인 — 화면
    렌더링 결과 외엔 저장하지 않는다(다만 검색 결과 자체는 app.live_cache가 증분
    캐시로 남긴다, 아래 참고).

    - 언론사 화이트리스트는 무시한다(전체 언론사) — "지금 뭐가 떠 있는지" 파악이
      목적이라, 공식 리포트용 큐레이션(설정의 언론사 선택)과는 무관하다.
    - [수정: 2026-07-27] 숨긴 기사를 목록에서 걸러내지 않는다 — 예전엔(filter_hidden)
      스크랩 초안·완성본에서 🗑️로 숨긴 기사가 실시간 현황에서도 통째로 사라졌는데,
      "실시간 현황은 원본을 그대로 비추는 화면이라 삭제되면 안 된다"는 요청에 따라
      바꿨다. 대신 _render_row가 hidden_urls로 "🗑️ 숨김" 상태를 표시만 한다 —
      실제로는 지워진 적이 없다는 사실이 화면에도 그대로 드러난다.
    - [수정: 2026-07-28] 완전 동일 제목 중복 제거·포토/현장 기사 제외도 더 이상
      적용하지 않는다 — 예전엔 설정 화면에서 독립적으로 켜고 끌 수 있었는데(기본은
      꺼짐), "실시간 현황은 날것 그대로 보여주는 게 맞고, 선별은 초안·완성본 단계의
      일"이라는 판단으로 아예 없앴다. 지금은 네이버가 돌려주는 결과를 그대로 보여준다.
    - 체크 해제(꺼짐)로 표시해둔 키워드는 여기서도 제외한다(app.settings.active_search_groups).
    - [추가: 2026-08-14] pubDate 파싱 실패 기사도 include_unparsed_dates=True로 검색해
      `pub_date: None`으로 그대로 담아온다(app.naver_api._search_one_keyword) — 정기
      스크랩과 달리 이 화면은 시각을 몰라도 기사 자체를 버리지 않는다. render_live_page가
      이 기사들을 하단 별도 구역으로 분리해 보여준다(위쪽 시간순 목록·상단 건수 집계에는
      안 들어간다 — "오늘 기사"인지조차 확인 안 됐으므로).

    [추가: 2026-07-29] 증분 캐시 — 새로고침마다 당일 0시부터 매번 다시 검색하면 하루가
    지날수록(모을 기사가 늘수록) 점점 느려지고, 어차피 직전 새로고침 때 이미 받아온
    기사를 거의 그대로 또 받아오는 낭비였다.

    [수정: 2026-08-13] 캐시 단위를 그룹 전체에서 키워드 하나하나로 바꿨다(app.live_cache,
    _live_search_plan) — 예전엔 그룹 구성이 조금만 바뀌어도(키워드 추가, 그룹 이름
    변경, OR/AND 토글 등) 지문이 달라져 캐시를 통째로 버리고 등록된 키워드 전부를
    당일 0시부터 다시 검색했다. 이제 키워드마다 "마지막으로 본 시각"과 원시 검색
    결과를 따로 캐시해뒀다가, 처음 보는 키워드만 당일 0시부터 검색하고 나머지는 그
    이후분만 검색해 이어붙인다. 최종 기사 목록은 원시 결과를 캐시한 뒤 매번
    app.naver_api.match_articles_to_groups로 "지금" 그룹 설정 기준으로 새로 매칭하므로,
    그룹 재편성(이름·모드·소속 변경)은 재검색 없이 즉시 반영된다.

    app.naver_api.search_keywords가 (results_by_keyword, failed_keywords) 튜플을
    돌려준다 — 키워드별로 재시도까지 다 써도 실패한 게 있으면 failed_keywords에
    담겨온다. 실시간 현황은 5분 뒤 재시도(정기 스크랩의 방식)를 기다릴 수 없는
    화면이라(사용자가 새로고침을 누르고 바로 결과를 봄), 실패한 키워드는 캐시의
    "마지막으로 본 시각"을 전진시키지 않고 그대로 둔다 — 다음 새로고침이 실패했던
    구간을 포함해 그 키워드만 다시 검색한다(성공한 다른 키워드는 영향받지 않는다,
    예전엔 하나만 실패해도 캐시 전체를 갱신 안 했다). 성공한 키워드가 찾아온 기사는
    이번 화면에 그대로 보여주되, 화면 상단에 실패한 키워드를 이름으로 알린다
    (§3: 실패는 숫자로 알린다) — 조용히 빈 결과로 넘어가면 "그 키워드엔 오늘 기사가
    없다"와 구분이 안 된다.
    """
    settings = load_settings()
    groups = active_search_groups(settings)
    unique_keywords = _unique_keywords(groups)
    after_map, known_results_by_keyword, stored_last_seen = _live_search_plan(unique_keywords)
    failed_keywords: list = []

    try:
        new_results_by_keyword, failed_keywords, _capped_keywords = search_keywords(
            unique_keywords, after=after_map, before=None, include_unparsed_dates=True
        )
    except requests.exceptions.RequestException:
        if not any(known_results_by_keyword.values()):
            html_text = render_live_error_page()
            atomic_write_text(LIVE_HTML_PATH, html_text)
            return LIVE_HTML_PATH
        results_by_keyword = known_results_by_keyword
    else:
        results_by_keyword = {}
        keyword_last_seen_to_save = {}
        for keyword in unique_keywords:
            known = known_results_by_keyword[keyword]
            if keyword in failed_keywords:
                # 실패 — 이번 새 결과는 버리고 기존 캐시만 쓴다. last_seen도 건드리지
                # 않는다(캐시에 없던 값이면 계속 "처음 보는 키워드"로 남아 다음
                # 새로고침에서 당일 0시부터 다시 시도한다 — 안전한 기본값).
                # [수정: 2026-08-21] after_map이 아니라 stored_last_seen(캐시에 있던 원래
                # 값)을 다시 쓴다 — after_map은 이제 60분 물러선 값이라, 그걸 저장하면
                # 검색이 한 번 실패할 때마다 기준선이 뒤로 밀려 남는다.
                results_by_keyword[keyword] = known
                if keyword in stored_last_seen:
                    keyword_last_seen_to_save[keyword] = stored_last_seen[keyword]
                continue
            # [수정: 2026-08-21] URL로 중복을 걸러 합친다 — 위 60분 되돌아보기 때문에
            # 이미 캐시에 있는 기사가 매번 다시 딸려온다. 그대로 이어붙이면 캐시 파일이
            # 계속 부풀고, match_articles_to_groups(track_keyword_matches=True)가 세는
            # matched_keywords·키워드별 건수가 같은 기사를 여러 번 세게 된다.
            known_urls = {a["url"] for a in known}
            merged = known + [a for a in new_results_by_keyword.get(keyword, []) if a["url"] not in known_urls]
            results_by_keyword[keyword] = merged
            pub_dates = [datetime.fromisoformat(a["pub_date"]) for a in merged if a.get("pub_date")]
            if pub_dates:
                newest = max(pub_dates)
            elif keyword in stored_last_seen:
                # [수정: 2026-08-21] 오늘 기사가 한 건도 없는 키워드 — 여기서 after_map
                # (60분 물러선 값)을 저장하면 새로고침마다 기준선이 조금씩 뒤로 밀린다.
                # 저장하는 값은 어디까지나 "지금까지 본 가장 최신 pub_date"여야 한다.
                newest = datetime.fromisoformat(stored_last_seen[keyword])
            else:
                newest = kst_today_at("00:00")
            keyword_last_seen_to_save[keyword] = newest.isoformat()
        save_live_cache(keyword_last_seen_to_save, results_by_keyword)

    articles = match_articles_to_groups(groups, results_by_keyword, track_keyword_matches=True)

    # [수정: 2026-07-27] filter_hidden 제거 — 위 docstring 참고. 숨긴 기사는 이제
    # 목록에서 안 빠지고 _render_row가 "🗑️ 숨김"으로 표시만 한다.
    # [수정: 2026-07-25] 언론사 우선순위 대신 최신순(네이버 뉴스처럼) — pub_date는
    # app.naver_api._search_one_keyword가 KST로 통일해 isoformat() 문자열로 채워주므로,
    # 그대로 문자열 비교만 해도 시간 순서와 일치한다(굳이 다시 파싱할 필요 없음).
    articles = sorted(articles, key=lambda a: a.get("pub_date") or "", reverse=True)
    articles = apply_summary_overrides(articles)

    can_add = load_latest_run() is not None
    html_text = render_live_page(
        articles,
        can_add,
        datetime.now().strftime("%H:%M"),
        settings.get("highlight_keywords", []),
        settings.get("outlet_order", []),
        groups,
        settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE),
        failed_keywords,
    )
    atomic_write_text(LIVE_HTML_PATH, html_text)
    return LIVE_HTML_PATH


_live_generation_lock = threading.Lock()
_live_generation_thread: Optional[threading.Thread] = None


def _ensure_live_generation_running() -> None:
    """콜드 캐시일 때 generate_live_page()를 백그라운드 스레드에서 딱 한 번만 돌린다.

    [추가: 2026-08-13] 이미 돌고 있는 스레드가 있으면(자동 새로고침이든 사람이 수동으로
    또 열었든) 아무것도 하지 않는다 — 중복으로 돌리면 네이버 쪽 순간 동시 요청이
    두 배가 되어 실측으로 확인한 429 위험 구간에 다시 들어갈 수 있다(app.naver_api의
    동시성 실측 기록 참고: 8은 0%였지만 그 이상은 위험했다). 락은 "스레드를 새로
    시작할지" 결정에만 걸고, 검색 자체가 끝날 때까지 들고 있지 않는다(그러면 락을
    쥔 채 수십~백여 초를 기다리게 되어 다른 요청까지 막힌다).
    """
    global _live_generation_thread
    with _live_generation_lock:
        if _live_generation_thread is not None and _live_generation_thread.is_alive():
            return

        def _run() -> None:
            try:
                generate_live_page()
            except Exception:
                logger.exception("백그라운드 실시간 현황 생성 실패")

        _live_generation_thread = threading.Thread(target=_run, daemon=True)
        _live_generation_thread.start()


def generate_live_page_or_wait() -> Path:
    """실시간 현황 라우트(/live.html)가 실제로 호출하는 진입점.

    [추가: 2026-08-13] 오늘 처음 보는(캐시에 last_seen이 없는) 키워드가 하나라도
    있으면 — 즉 이번 요청이 콜드 캐시라 수십~백여 초가 걸릴 상황이면 — 그 검색을
    이 요청 스레드에서 기다리지 않는다. 대신 별도 스레드에서 검색을 시작시켜두고
    (_ensure_live_generation_running), 3초마다 자동 새로고침하는 대기 화면
    (render_live_loading_page)을 즉시 돌려준다. 새로고침 버튼도 눌리지 않게
    막는다 — 자동 새로고침 경로 말고 사람이 수동으로 눌러도 중복 검색이
    시작되지 않게 하려면 버튼과 백그라운드 가드 둘 다 필요하다.

    등록된 키워드가 전부 캐시에 있으면(오늘 이미 한 번 이상 데워짐) 콜드가 아니므로
    generate_live_page()를 그대로 동기 호출한다 — 이 경우는 이미 빠르다(증분
    캐시, 보통 1초 안쪽).
    """
    # [추가: 2026-08-20] 키가 아예 없으면 콜드 판정·백그라운드 검색 스레드 자체를
    # 시작하지 않는다 — 검색을 시도해도 매번 즉시 실패해(app.naver_api.
    # NaverNotConfiguredError, 재시도 안 함) last_seen이 끝내 안 채워지므로, 이 가드가
    # 없으면 새로고침(3초 자동)마다 실패할 게 뻔한 백그라운드 스레드를 계속 새로 띄우는
    # 무한 "불러오는 중" 상태에 빠진다.
    if not naver_is_configured():
        html_text = render_live_not_configured_page()
        atomic_write_text(LIVE_HTML_PATH, html_text)
        return LIVE_HTML_PATH

    settings = load_settings()
    groups = active_search_groups(settings)
    unique_keywords = _unique_keywords(groups)
    cache = load_live_cache() or {}
    keyword_last_seen = cache.get("keyword_last_seen", {})
    is_cold = any(keyword not in keyword_last_seen for keyword in unique_keywords)

    if not is_cold:
        return generate_live_page()

    _ensure_live_generation_running()
    html_text = render_live_loading_page(len(unique_keywords))
    atomic_write_text(LIVE_HTML_PATH, html_text)
    return LIVE_HTML_PATH
