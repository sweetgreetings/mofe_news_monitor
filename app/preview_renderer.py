# Design Ref: 다음 회차 미리보기 — 정식 회차(app.scraper.collect_run)가 끝나기 전에,
# 지금까지의 시간창(회차 시작~지금)으로 미리 검색·분류해 보여준다. live.html(당일 0시~
# 현재, 전체 언론사, 소제목 분류 없음)과 달리 정식 회차와 똑같은 파이프라인(아웃렛
# 화이트리스트·포토/인사 제외·중복 제거·소제목 분류)을 그대로 따른다는 점이 다르다 —
# "지금 정식 회차가 끝난다면 어떤 모습일지"의 미리보기이기 때문이다.
#
# 저장 버튼이 없다: 여기서 숨기기(🗑️)·소제목 이동(↑/↓)·이름 바꾸기(✏️)를 하면
# hidden_articles.json/group_overrides.json/group_labels.json에 URL·단어 기준으로
# 바로 기록되고(app.curation), 이 파일들은 정식 회차와 공유되는 전역 저장소라 정식
# 회차가 자동으로 돌 때도 그 큐레이션이 그대로 반영된다. 별도의 "저장" 개념이 필요 없다.
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from app.classifier import classify_articles
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
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    FONT_STACK,
    HIGHLIGHT_COLORS,
    PREVIEW_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.curation import (
    bulk_move_articles,
    bulk_reassign_group,
    display_group_name,
    filter_hidden,
    load_group_labels,
    load_group_overrides,
    move_article,
    set_group_override,
)
from app.custom_groups import add_custom_group, load_custom_groups, remove_custom_group
from app.summary_overrides import apply_summary_overrides
from app.manual_keyword_note import load_manual_keyword_note
from app.group_order import apply_group_order
from app.draft_articles import add_draft_pending_article, load_draft_pending_articles
from app.preview_cache import load_preview_cache, save_preview_cache
from app.preview_order import apply_preview_order, load_preview_order, save_preview_order
from app.filters import (
    deduplicate_by_title,
    exclude_personnel_articles,
    exclude_photo_articles,
    filter_by_outlet_whitelist,
)
from app.highlight import highlight_keywords
from app.manual_articles import load_manual_articles
from app.naver_api import kst_today_at, search_articles_by_groups
from app.renderer import (
    _group_option_label,
    _render_manual_section,
    apply_line_template,
    format_slot_time_kr,
    render_article,
)
from app.scheduler import next_pending_slot
from app.settings import active_schedule_times, active_search_groups, all_search_keywords, load_settings
from app.sorter import sort_by_outlet_priority
from app.summarizer import extract_keywords, summarize_groups

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>스크랩 초안</title>
<style>
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: 8px; padding-top: 60px; padding-bottom: 56px;
  }}
  .topbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 20;
    background: {card}; border-bottom: 1px solid {border}; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  }}
  .topbar-inner {{
    max-width: 800px; margin: 0 auto; padding: 12px 24px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .topbar a {{ color: {accent}; text-decoration: none; font-size: 0.92rem; font-weight: 600; padding: 6px 10px; border-radius: 6px; }}
  .topbar a:hover {{ background: {hover}; }}
  .bottombar {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  }}
  .bottombar-inner {{
    max-width: 800px; margin: 0 auto; padding: 10px 24px;
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
  }}
  .bottombar a {{ color: {accent}; text-decoration: none; font-size: 1.1rem; padding: 6px 10px; border-radius: 6px; }}
  .bottombar a:hover {{ background: {hover}; }}
  /* [추가: 2026-08-03] app.renderer와 동일한 이유 — 하단바 🖍️ 형광펜 편집 팝오버. */
  .highlight-wrap {{ position: relative; }}
  .highlight-toggle {{
    background: transparent; border: none; font-size: 1.1rem; cursor: pointer;
    padding: 6px 10px; border-radius: 6px;
  }}
  .highlight-toggle:hover {{ background: {hover}; }}
  .highlight-popover {{
    display: none; position: absolute; bottom: 100%; right: 0; margin-bottom: 8px;
    background: {card}; border: 1px solid {border}; border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12); padding: 12px; width: 220px; z-index: 30;
  }}
  .highlight-popover.is-open {{ display: block; }}
  .highlight-chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
  .highlight-chip {{
    display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 14px;
    font-size: 0.82rem; color: {text}; cursor: pointer;
  }}
  .highlight-chip button {{
    background: transparent; border: none; padding: 0; font-size: 0.8rem; cursor: pointer;
    color: inherit; line-height: 1;
  }}
  .highlight-empty {{ color: {muted}; font-size: 0.8rem; margin: 0 0 8px; }}
  .highlight-add-form {{ display: flex; gap: 6px; }}
  .highlight-add-form input {{ flex: 1; font-size: 0.85rem; padding: 5px 8px; min-width: 0; }}
  .highlight-add-form button {{ font-size: 0.82rem; padding: 5px 10px; white-space: nowrap; }}
  /* [추가: 2026-08-05] app.renderer와 동일한 소제목 미니 목차(펼침형). */
  .toc-toggle-btn {{
    position: fixed; right: 20px; bottom: 20px; width: 46px; height: 46px; border-radius: 50%;
    background: {accent}; color: #ffffff; border: none; font-size: 1.2rem; cursor: pointer;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .toc-toggle-btn:hover {{ background: {header}; }}
  .toc-popover {{
    display: none; position: fixed; right: 20px; bottom: 74px; width: 200px;
    max-height: 320px; overflow-y: auto; background: {card}; border: 1px solid {border};
    border-radius: 8px; padding: 8px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15); z-index: 200;
  }}
  .toc-popover.is-open {{ display: block; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 목차 안에서 소제목 순서까지 바꿀 수 있게
     항목마다 작은 ▲▼를 붙였다(이름 클릭=이동, 화살표 클릭=순서 변경으로 분리). */
  .toc-row {{ display: flex; align-items: center; justify-content: space-between; border-radius: 4px; }}
  .toc-row a {{
    flex: 1; min-width: 0; padding: 6px 8px; font-size: 0.85rem; color: {text};
    text-decoration: none; border-radius: 4px; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis;
  }}
  .toc-row a:hover {{ background: {hover}; }}
  .toc-row-btns {{ display: flex; gap: 1px; flex-shrink: 0; padding-right: 4px; }}
  .toc-order-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.72rem; cursor: pointer;
    padding: 2px 4px; border-radius: 4px;
  }}
  .toc-order-btn:hover {{ background: {hover}; }}
  .toc-order-btn:disabled {{ opacity: 0.3; cursor: default; }}
  .toc-row.toc-row-moved {{ background: #DCFCE7; }}
  .toc-empty {{ color: {muted}; font-size: 0.82rem; padding: 6px 8px; }}
  /* [추가: 2026-07-30] 체크박스로 기사를 선택했을 때만 나타나는 "일괄 이동" 바 —
     장바구니처럼 화면 하단에 붙어있다가, 선택이 하나도 없으면 숨어서 원래 있던
     🗑️(숨긴 기사 관리)만 오른쪽에 그대로 남는다. */
  .bulk-move-bar {{ display: none; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.85rem; }}
  .bulk-move-bar.is-active {{ display: flex; }}
  .bulk-move-bar .bulk-move-count {{ font-weight: 600; color: {header}; white-space: nowrap; }}
  #bulk-move-select {{
    border: 1px solid {accent}; border-radius: 4px; padding: 5px 8px; font-size: 0.85rem;
    color: {text}; background: {card};
  }}
  .bulk-move-bar button {{ padding: 5px 12px; font-size: 0.85rem; }}
  /* [추가: 2026-08-04] app.renderer와 동일 — 일괄 위/아래 이동 버튼. */
  #bulk-move-up, #bulk-move-down {{ padding: 5px 10px; }}
  #bulk-move-up:disabled, #bulk-move-down:disabled {{ opacity: 0.35; cursor: not-allowed; }}
  .bulk-move-bar .clear-btn {{ background: transparent; color: {muted}; }}
  .bulk-move-bar .clear-btn:hover {{ background: {hover}; }}
  .actions .create-group-btn {{
    background: transparent; color: {accent}; border: 1px solid {ghost_border};
    border-radius: 6px; font-weight: 400; margin-left: auto;
  }}
  .actions .create-group-btn:hover {{ background: {hover}; border-color: {ghost_border_hover}; }}
  .article-select {{ margin-top: 3px; flex-shrink: 0; }}
  .group-move-select {{
    flex-shrink: 0; width: 100px; border: 1px solid {border}; border-radius: 4px;
    padding: 2px 4px; font-size: 0.78rem; color: {muted}; background: {card};
  }}
  /* [추가: 2026-08-04] app.renderer와 동일 — 소제목별/시간순/언론사순 보기 전환 select. */
  .view-mode-select {{
    border: 1px solid {accent}; border-radius: 6px; padding: 6px 10px; font-size: 0.85rem;
    color: {text}; background: {card};
  }}
  /* [수정: 2026-08-04] app.renderer와 동일 — 사용자가 직접 만든 소제목은 기사가
     차 있어도 점선 테두리를 유지해 자동 분류 소제목과 구분되게 한다. */
  .subheading-custom {{ border: 2px dashed #7DD3FC; border-radius: 8px; padding: 8px 16px; }}
  .empty-group-hint {{ color: {muted}; font-size: 0.85rem; margin: 6px 0 0; }}
  header h1 {{ font-size: 1.3rem; margin-bottom: 6px; color: {header}; }}
  .preview-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
  .preview-hint {{
    font-size: 0.85rem; color: {muted}; background: {hover}; border-radius: 8px;
    padding: 10px 14px; margin-bottom: 20px; line-height: 1.6;
  }}
  .actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유 — button은 a와 달리 font-family를
     상속받지 않고, appearance:auto(네이티브 OS 버튼 껍데기)까지 남아있어 폰트만 맞춰선
     완전히 똑같이 안 보인다. 크롬이 button 텍스트만 내부적으로 수직 중앙 정렬해주는
     것까지 발견해 align-items: center를 직접 지정했다. */
  button, a.btn {{
    background: {accent}; color: #ffffff; border: none; border-radius: 4px;
    padding: 6px 14px; font-size: 0.9rem; font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-flex; align-items: center; justify-content: center;
    user-select: none; -webkit-user-select: none;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유로 액션 툴바만 소프트 필로(시안 B) */
  .actions button, .actions a.btn {{
    background: {hover}; color: {accent}; font-weight: 600; border-radius: 8px;
  }}
  .actions button:hover, .actions a.btn:hover {{ background: {tonal_hover}; }}
  .subheading {{ margin-top: 28px; }}
  /* [수정: 2026-08-05] app.renderer와 동일 — h2 기본 margin 제거 + 아이콘 오른쪽 정렬. */
  .subheading h2 {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    font-size: 1.1rem; color: {header}; border-bottom: 1px solid {border};
    margin: 0 0 6px; padding-bottom: 6px;
  }}
  .subheading-title {{ display: flex; align-items: center; gap: 4px; min-width: 0; }}
  .subheading-icons {{ display: flex; align-items: center; gap: 2px; flex-shrink: 0; }}
  .rename-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.9rem; cursor: pointer;
    vertical-align: middle; user-select: none; -webkit-user-select: none;
  }}
  /* [추가: 2026-08-03] app.renderer와 동일한 이유 — 소제목 순서 조정 버튼. */
  /* [수정: 2026-08-05] app.renderer와 동일 — 기사 ↑/↓(.move-btn)과 가로폭 통일. */
  .order-btn {{
    background: transparent; border: none; color: {muted}; font-size: 1rem; cursor: pointer;
    vertical-align: middle; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .order-btn:disabled {{ opacity: 0.35; cursor: default; }}
  .article {{ margin: 10px 0; line-height: 1.5; padding: 6px 8px; border-radius: 8px; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 마우스 오버 시 연한 회색 표시. */
  .article:hover {{ background: #F3F4F6; }}
  /* [추가: 2026-08-03] app.renderer와 동일한 이유 — 체크된 기사만 연한 배경으로
     표시한다 (디자인 시안 A). */
  .article:has(.article-select:checked) {{ background: {hover}; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 승격 직후 그 기사를 한 번 표시한다. */
  .article.just-promoted {{ background: #FFF9C4; }}
  /* [수정: 2026-08-05] app.renderer와 동일 — 순서 이동은 노란색(승격·새 기사 도착과 겹침)
     대신 연두색을 쓴다. */
  .article.just-moved {{ background: #DCFCE7; }}
  /* [수정: 2026-07-30] 🗑️를 제목 바로 옆에 붙인다 — space-between이면 창이 넓을 때
     제목이 짧을수록 버튼이 화면 오른쪽 끝까지 멀어져 잘못 누를 위험이 있었다. */
  .article summary {{
    cursor: pointer; display: flex; align-items: baseline; justify-content: flex-start; gap: 8px;
  }}
  .article summary::marker {{ color: {muted}; }}
  .title-line {{ min-width: 0; overflow-wrap: anywhere; }}
  /* [수정: 2026-08-05] app.renderer와 동일 — 액션 묶음만 margin-left: auto로 항상 줄
     오른쪽 끝에 붙인다. */
  .article-actions {{ margin-left: auto; display: flex; align-items: center; gap: 4px; flex-shrink: 0; }}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: 0.95rem; }}
  .article-footer {{ display: flex; align-items: center; gap: 8px; }}
  .article .url {{ flex: 1; color: {muted}; font-size: 0.9rem; word-break: break-all; text-decoration: underline; }}
  /* [수정: 2026-07-30] "이미 확인함"을 영구 기억(localStorage)하지 않고 "지금 펼쳐서
     보고 있는 기사"에만 실시간 적용 — app.renderer와 같은 이유·같은 방식(:has()). */
  .article:has(details[open]) .title-line, .article:has(details[open]) .url {{ color: {seen_purple}; }}
  /* [추가: 2026-07-29] 지난번 이 화면을 봤을 때는 없다가 이번에 새로 들어온 기사 —
     초안은 새로고침할 때마다 다시 검색하므로 뭐가 새로 섞였는지 표시해준다
     (app.preview_renderer._compute_preview_articles가 매번 새로 검색·병합한 결과). */
  .article.is-new-arrival {{ background: {new_arrival_bg}; border-radius: 6px; padding: 8px 10px; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유 — 화면에서 직접 드래그해 복사할 때
     게시 시각이 같이 딸려오지 않도록 이 부분만 선택 자체를 막는다. */
  .pub-time {{
    flex-shrink: 0; color: {muted}; font-size: 0.8rem; white-space: nowrap;
    user-select: none; -webkit-user-select: none;
  }}
  .move-btn, .hide-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .move-btn:disabled {{ color: {border}; cursor: not-allowed; }}
  .hide-btn:hover {{ color: {error}; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 원문 다시 가져오기 버튼, 평소 숨김. */
  .refetch-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
    opacity: 0; transition: opacity 0.15s;
  }}
  .article:hover .refetch-btn {{ opacity: 1; }}
  .refetch-btn:disabled {{ opacity: 0.35 !important; cursor: not-allowed; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — ✏️ 직접 수정 인라인 편집 칸. */
  .edit-summary-form {{
    margin: 8px 0 4px 20px; padding: 10px 12px; border: 1px solid {accent}; border-radius: 8px;
    display: flex; flex-direction: column; gap: 8px;
  }}
  .edit-summary-form input, .edit-summary-form textarea {{
    width: 100%; box-sizing: border-box; padding: 6px 10px; border: 1px solid {border}; border-radius: 6px;
    font-size: 0.88rem; color: {text}; background: {card}; font-family: inherit;
  }}
  .edit-summary-form .edit-summary-actions {{ display: flex; justify-content: flex-end; gap: 8px; }}
  .edit-summary-form button {{ font-size: 0.82rem; padding: 5px 12px; }}
  .empty {{ text-align: center; margin-top: 60px; font-size: 1.1rem; color: {muted}; }}
  .bottom {{ margin-top: 40px; border-top: 1px solid {border}; padding-top: 16px; }}
  .bottom h3 {{ font-size: 1rem; color: {header}; }}
  .manual-divider {{
    display: flex; align-items: center; gap: 10px; margin: 32px 0 4px;
    color: {muted}; font-size: 0.75rem;
  }}
  .manual-divider::before, .manual-divider::after {{ content: ""; flex: 1; border-top: 1px dashed {border}; }}
  .manual-zone {{ border: 1px dashed {border}; border-radius: 8px; padding: 14px 18px; margin-top: 10px; }}
  .manual-zone-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }}
  .manual-zone-title {{ font-weight: 600; color: {text}; font-size: 0.95rem; }}
  .manual-zone-hint {{ font-size: 0.75rem; color: {muted}; }}
  .manual-zone .article:first-child {{ margin-top: 10px; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 직접 키워드 작성 메모 칸. */
  .keyword-note-zone {{
    display: none; align-items: center; gap: 8px; border: 1px dashed {border}; border-radius: 8px;
    padding: 10px 14px; margin: 10px 0 4px; flex-wrap: wrap;
  }}
  .keyword-note-zone.is-open {{ display: flex; }}
  .keyword-note-zone .keyword-note-label {{ font-weight: 600; color: {text}; font-size: 0.85rem; white-space: nowrap; }}
  .keyword-note-zone input {{
    flex: 1; min-width: 220px; padding: 6px 10px; border: 1px solid {border}; border-radius: 6px;
    font-size: 0.88rem; color: {text}; background: {card};
  }}
  .keyword-note-zone button {{ font-size: 0.82rem; padding: 5px 10px; white-space: nowrap; }}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <a href="{home_href}">홈</a>
  <a href="{live_href}">🔴 실시간 현황</a>
  <a href="{scrap_href}">📗 스크랩 완성본</a>
</div></div>
<div class="container">
  <header class="preview-head">
    <h1>📝 스크랩 초안 {run_slot_html}</h1>
  </header>
  <div class="actions" id="preview-actions">{actions_html}</div>
  <div class="keyword-note-zone{note_open_class}" id="keyword-note-zone">
    <span class="keyword-note-label">키워드 작성 :</span>
    <input type="text" id="keyword-note-input" value="{note_value_attr}" placeholder="키워드 a, 키워드 b, 키워드 c...">
    <button type="button" onclick="saveKeywordNote()">저장</button>
    <button class="clear-btn" type="button" onclick="clearKeywordNote()">삭제</button>
  </div>
  <div class="preview-hint" id="preview-hint">{hint}</div>
  <div id="preview-body">{body}</div>
</div>
<button type="button" class="toc-toggle-btn" onclick="toggleTocPopover()" title="소제목 목차">☰</button>
<div class="toc-popover" id="toc-popover"></div>
<div class="bottombar"><div class="bottombar-inner">
  <div class="bulk-move-bar" id="bulk-move-bar">
    <span class="bulk-move-count" id="bulk-move-count"></span>
    <select id="bulk-move-select"></select>
    <button type="button" onclick="bulkMoveSelected()">옮기기</button>
    <button type="button" id="bulk-move-up" onclick="bulkMoveOrder('up')" title="선택한 기사들을 통째로 위로 이동(같은 소제목 안에서만)">↑</button>
    <button type="button" id="bulk-move-down" onclick="bulkMoveOrder('down')" title="선택한 기사들을 통째로 아래로 이동(같은 소제목 안에서만)">↓</button>
    <button type="button" onclick="bulkHideSelected()" title="선택한 기사 전부 숨기기 (되돌리기 가능)">🗑️</button>
    <button class="clear-btn" type="button" onclick="clearSelection()">선택 해제</button>
  </div>
  <div class="highlight-wrap">
    <button type="button" class="highlight-toggle" onclick="toggleHighlightPopover()" title="형광펜 단어 편집">🖍️</button>
    <div class="highlight-popover" id="highlight-popover">
      <div class="highlight-chips" id="highlight-chips"></div>
      <form class="highlight-add-form" onsubmit="return addHighlightWord(event);">
        <input type="text" id="highlight-new-word" placeholder="단어 추가" maxlength="20">
        <button type="submit">추가</button>
      </form>
    </div>
  </div>
  <a href="{hidden_href}" title="숨긴 기사 관리">🗑️</a>
</div></div>
<script>
// [수정: 2026-07-30] previewMoveArticle이 reload 없이 이 값을 새로 대입하므로 const가 아닌 let.
let PLAIN_TEXT = {plain_text_json};
function copyPlainText() {{
  navigator.clipboard.writeText(PLAIN_TEXT)
    .then(() => alert("클립보드에 복사했습니다."))
    .catch(() => alert("복사에 실패했습니다."));
}}
function sendToTelegram(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/telegram-send-draft", {{
    method: "POST",
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: PLAIN_TEXT}})
  }}).then(function(res) {{
    if (res.ok) {{ alert("텔레그램으로 보냈습니다."); }}
    else {{ alert("전송에 실패했습니다 — 설정 화면에서 텔레그램 연결 상태를 확인해주세요."); }}
  }}).catch(function() {{
    alert("전송에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }}).finally(function() {{
    btn.disabled = false;
  }});
}}
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
// [추가: 2026-08-05] app.renderer와 동일한 소제목 미니 목차(펼침형) — #preview-body가
// previewMoveArticle 등으로 통째로 갈아끼워질 수 있어서, 목록을 미리 만들어두지 않고
// 열 때마다 그 시점의 .subheading[data-toc-name]을 다시 읽어 항상 최신 상태로 그린다.
function buildTocPopover() {{
  var pop = document.getElementById("toc-popover");
  var sections = document.querySelectorAll(".subheading[data-toc-name]");
  pop.innerHTML = "";
  if (!sections.length) {{
    pop.innerHTML = '<p class="toc-empty">소제목이 없어요.</p>';
    return;
  }}
  var justMoved = sessionStorage.getItem("tocJustMovedName");
  if (justMoved) {{ sessionStorage.removeItem("tocJustMovedName"); }}
  sections.forEach(function(sec, idx) {{
    var renameBtn = sec.querySelector(".rename-btn[data-current]");
    var rawName = renameBtn ? renameBtn.dataset.name : "";
    var row = document.createElement("div");
    row.className = "toc-row" + (rawName && rawName === justMoved ? " toc-row-moved" : "");
    var a = document.createElement("a");
    a.href = "#";
    a.textContent = sec.dataset.tocName;
    a.onclick = function(e) {{ e.preventDefault(); scrollToSubheading(sec.id); }};
    var btns = document.createElement("span");
    btns.className = "toc-row-btns";
    var up = document.createElement("button");
    up.type = "button"; up.className = "toc-order-btn"; up.textContent = "▲"; up.title = "위로";
    up.disabled = idx === 0;
    up.onclick = function() {{ moveTocOrder(rawName, "up"); }};
    var down = document.createElement("button");
    down.type = "button"; down.className = "toc-order-btn"; down.textContent = "▼"; down.title = "아래로";
    down.disabled = idx === sections.length - 1;
    down.onclick = function() {{ moveTocOrder(rawName, "down"); }};
    btns.appendChild(up);
    btns.appendChild(down);
    row.appendChild(a);
    row.appendChild(btns);
    pop.appendChild(row);
  }});
}}
function toggleTocPopover() {{
  var pop = document.getElementById("toc-popover");
  var opening = !pop.classList.contains("is-open");
  pop.classList.toggle("is-open");
  if (opening) {{ buildTocPopover(); }}
}}
function scrollToSubheading(id) {{
  var el = document.getElementById(id);
  if (el) {{ el.scrollIntoView({{behavior: "smooth", block: "start"}}); }}
  document.getElementById("toc-popover").classList.remove("is-open");
}}
function moveTocOrder(rawName, direction) {{
  var names = Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(b) {{ return b.dataset.name; }});
  var i = names.indexOf(rawName);
  var j = direction === "up" ? i - 1 : i + 1;
  if (i < 0 || j < 0 || j >= names.length) {{ return; }}
  var tmp = names[i]; names[i] = names[j]; names[j] = tmp;
  fetch("http://{settings_host}:{settings_port}/save-group-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{order: JSON.stringify(names)}})
  }}).then(function(res) {{
    if (res.ok) {{
      sessionStorage.setItem("tocJustMovedName", rawName);
      sessionStorage.setItem("tocPopoverReopen", "1");
      location.reload();
    }} else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
document.addEventListener("click", function(e) {{
  var pop = document.getElementById("toc-popover");
  if (pop.classList.contains("is-open") && !e.target.closest(".toc-popover") && !e.target.closest(".toc-toggle-btn")) {{
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
// [추가: 2026-08-04] app.renderer와 동일 — 형광펜 단어 클릭 시 다음 색으로 순환.
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
function hideArticle(btn) {{
  var url = btn.dataset.url;
  var article = btn.closest(".article");
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{
      url: url, outlet: btn.dataset.outlet, title: btn.dataset.title, pubDate: btn.dataset.pubDate
    }})
  }}).then(function(res) {{
    if (res.ok) {{ article.remove(); }}
    else {{ alert("숨기기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] app.renderer와 동일 — 원문에서 다시 가져오기.
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
// [추가: 2026-08-05] app.renderer와 동일 — ✏️ 직접 수정.
function editSummary(btn) {{
  var article = btn.closest(".article");
  if (article.querySelector(".edit-summary-form")) {{ return; }}
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
  var details = article.querySelector("details");
  details.open = true;
  article.querySelector(".article-summary").insertAdjacentElement("afterend", form);
}}
// [추가: 2026-08-04] app.renderer와 동일 — 소제목별/시간순/언론사순 보기 전환. 실제
// 소제목 구성은 그대로 두고, 화면에 기사를 나열하는 순서만 바꾼다(카드 DOM 노드를 옮겼다
// 되돌리는 방식 — 이유는 app.renderer.hideArticle 주변 주석 참고). previewMoveArticle이
// #preview-body를 통째로 갈아끼우는 화면이라, 그때마다 _FLAT_VIEW_HOMES를 비워 다시
// 캡처하게 한다(위 previewMoveArticle 참고).
var _FLAT_VIEW_HOMES = null;
function _restoreSubheadingView() {{
  if (_FLAT_VIEW_HOMES) {{
    // app.renderer._restoreSubheadingView와 동일한 이유 — 뒤에서부터 복원해야
    // insertBefore의 참조 노드(next)가 항상 이미 제자리로 돌아와 있다.
    _FLAT_VIEW_HOMES.slice().reverse().forEach(function(home) {{
      if (!document.body.contains(home.el)) {{ return; }}
      if (home.next && home.next.parentNode === home.parent) {{
        home.parent.insertBefore(home.el, home.next);
      }} else {{
        home.parent.appendChild(home.el);
      }}
    }});
  }}
  document.querySelectorAll(".subheading").forEach(function(s) {{ s.style.display = ""; }});
  var flat = document.getElementById("flat-view");
  if (flat) {{ flat.remove(); }}
}}
function applyViewMode(mode) {{
  _restoreSubheadingView();
  if (mode === "subheading") {{ return; }}
  var firstSub = document.querySelector(".subheading");
  if (!firstSub) {{ return; }}
  if (!_FLAT_VIEW_HOMES) {{
    _FLAT_VIEW_HOMES = Array.prototype.map.call(document.querySelectorAll(".subheading .article"), function(el) {{
      return {{ el: el, parent: el.parentElement, next: el.nextElementSibling }};
    }});
  }}
  // [추가: 2026-08-04] app.renderer와 동일 — 소제목 경계는 유지한 채 그 안 카드만 시간순.
  if (mode === "group-time") {{
    document.querySelectorAll(".subheading").forEach(function(section) {{
      var inGroup = Array.prototype.filter.call(section.children, function(el) {{
        return el.classList.contains("article");
      }});
      inGroup.sort(function(a, b) {{
        var ad = (a.querySelector(".hide-btn").dataset.pubDate || "");
        var bd = (b.querySelector(".hide-btn").dataset.pubDate || "");
        return ad < bd ? 1 : ad > bd ? -1 : 0;
      }});
      inGroup.forEach(function(el) {{ section.appendChild(el); }});
    }});
    return;
  }}
  var articles = Array.prototype.filter.call(document.querySelectorAll(".subheading .article"), function(el) {{
    return document.body.contains(el);
  }});
  if (mode === "time") {{
    articles.sort(function(a, b) {{
      var ad = (a.querySelector(".hide-btn").dataset.pubDate || "");
      var bd = (b.querySelector(".hide-btn").dataset.pubDate || "");
      return ad < bd ? 1 : ad > bd ? -1 : 0;
    }});
  }} else if (mode === "outlet") {{
    articles.sort(function(a, b) {{
      return (Number(a.dataset.outletRank) || 0) - (Number(b.dataset.outletRank) || 0);
    }});
  }}
  document.querySelectorAll(".subheading").forEach(function(s) {{ s.style.display = "none"; }});
  var flat = document.createElement("section");
  flat.id = "flat-view";
  flat.className = "subheading";
  firstSub.parentNode.insertBefore(flat, firstSub);
  articles.forEach(function(el) {{ flat.appendChild(el); }});
}}
// [추가: 2026-07-30] 자동분류가 완벽하지 않아 여러 기사를 한꺼번에 바로잡아야 할 때 쓰는
// 체크박스 다중 선택 + 하단 "장바구니" 바. 소제목 이름은 서버가 다시 내려주는 게 아니라
// 지금 화면에 그려진 <h2> 제목들을 그대로 읽어서 쓴다 — reload 없이 이동한 직후에도
// (previewMoveArticle이 #preview-body를 갈아끼운 뒤) 항상 최신 소제목 목록과 맞는다.
// [수정: 2026-07-30] 소제목 이름표(rename)가 붙어 있으면 화면엔 그 이름표가 보이지만,
// 서버(set_group_override)는 원래 분류 단어(원본 이름) 기준으로 소제목을 찾는다 —
// <h2> 텍스트(이름표)를 그대로 값으로 쓰면 이름표 붙은 소제목으로는 이동 자체가
// 조용히 실패한다. 이미 화면에 원본 이름을 갖고 있는 ✏️ 버튼의 data-name을 대신
// 읽어서, 값(value)엔 원본 이름을, 화면엔 이름표(data-current)를 쓴다.
function getAllGroups() {{
  return Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(btn) {{
    return {{value: btn.dataset.name, label: btn.dataset.current}};
  }});
}}
// [추가: 2026-08-03] app.renderer와 동일한 이유·동작 — 소제목 화면 순서를 바꾼다.
function moveGroupOrder(btn, direction) {{
  var names = Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(b) {{ return b.dataset.name; }});
  var i = names.indexOf(btn.dataset.name);
  var j = direction === "up" ? i - 1 : i + 1;
  if (i < 0 || j < 0 || j >= names.length) {{ return; }}
  var tmp = names[i]; names[i] = names[j]; names[j] = tmp;
  fetch("http://{settings_host}:{settings_port}/save-group-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{order: JSON.stringify(names)}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-04] app.renderer와 동일 — 소제목 헤더 체크박스로 그 소제목 안 기사
// 체크박스를 한 번에 맞춘다.
function toggleGroupSelectAll(checkbox) {{
  var section = checkbox.closest(".subheading");
  if (!section) {{ return; }}
  section.querySelectorAll(".article-select").forEach(function(cb) {{ cb.checked = checkbox.checked; }});
  updateBulkMoveBar();
}}
function updateBulkMoveBar() {{
  var checked = document.querySelectorAll(".article-select:checked");
  var bar = document.getElementById("bulk-move-bar");
  if (checked.length === 0) {{ bar.classList.remove("is-active"); return; }}
  bar.classList.add("is-active");
  document.getElementById("bulk-move-count").textContent = "☑️ " + checked.length + "개 선택됨";
  var select = document.getElementById("bulk-move-select");
  var options = '<option value="" selected disabled>이동할 소제목</option>';
  getAllGroups().forEach(function(g) {{
    var opt = document.createElement("option");
    opt.value = g.value;
    opt.textContent = g.label;
    options += opt.outerHTML;
  }});
  select.innerHTML = options;
  // [추가: 2026-08-04] app.renderer와 동일 — 일괄 위/아래 이동 버튼 활성화 여부.
  var upBtn = document.getElementById("bulk-move-up");
  var downBtn = document.getElementById("bulk-move-down");
  var checkedArticles = Array.prototype.map.call(checked, function(cb) {{ return cb.closest(".article"); }});
  var sections = checkedArticles.map(function(el) {{ return el.closest(".subheading"); }});
  var sameSection = sections.every(function(s) {{ return s === sections[0]; }});
  if (sameSection) {{
    var siblings = Array.prototype.filter.call(sections[0].children, function(el) {{ return el.classList.contains("article"); }});
    var positions = checkedArticles.map(function(el) {{ return siblings.indexOf(el); }}).sort(function(a, b) {{ return a - b; }});
    upBtn.disabled = positions[0] === 0;
    downBtn.disabled = positions[positions.length - 1] === siblings.length - 1;
  }} else {{
    upBtn.disabled = true;
    downBtn.disabled = true;
  }}
}}
function bulkMoveOrder(direction) {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  if (!urls.length) {{ return; }}
  var body = new URLSearchParams();
  urls.forEach(function(u) {{ body.append("urls", u); }});
  body.append("direction", direction);
  fetch("http://{settings_host}:{settings_port}/preview-bulk-move-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("bulkMoveCheckedUrls", JSON.stringify(urls)); location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] app.renderer와 동일 — 선택한 기사 여러 개를 한꺼번에 숨긴다.
function bulkHideSelected() {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  if (!urls.length) {{ return; }}
  if (!confirm(urls.length + "개 기사를 숨길까요? (숨긴 기사 관리에서 하나씩 되돌릴 수 있어요)")) return;
  Promise.all(urls.map(function(url) {{
    return fetch("http://{settings_host}:{settings_port}/hide-article", {{
      method: "POST", keepalive: true,
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: new URLSearchParams({{url: url}})
    }});
  }})).then(function(responses) {{
    if (responses.every(function(res) {{ return res.ok; }})) {{ location.reload(); }}
    else {{ alert("일부 기사를 숨기지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function clearSelection() {{
  document.querySelectorAll(".article-select:checked").forEach(function(cb) {{ cb.checked = false; }});
  updateBulkMoveBar();
}}
function _postGroupMove(urls, target) {{
  var body = new URLSearchParams();
  urls.forEach(function(u) {{ body.append("urls", u); }});
  body.append("target", target);
  return fetch("http://{settings_host}:{settings_port}/bulk-move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }});
}}
function bulkMoveSelected() {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  var target = document.getElementById("bulk-move-select").value;
  if (!urls.length || !target) {{ return; }}
  _postGroupMove(urls, target).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justMovedUrls", JSON.stringify(urls)); location.reload(); }}
    else {{ alert("이동에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function moveArticleToGroup(select) {{
  var url = select.closest(".article").dataset.url;
  var target = select.value;
  if (!target) {{ return; }}
  _postGroupMove([url], target).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justMovedUrls", JSON.stringify([url])); location.reload(); }}
    else {{ alert("소제목 이동에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("소제목 이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function createCustomGroup() {{
  var name = prompt("새 소제목 이름을 입력하세요");
  if (name === null) return;
  name = name.trim();
  if (!name) {{ alert("소제목 이름은 비워둘 수 없습니다."); return; }}
  // [수정: 2026-08-04] app.renderer와 동일 — 지금 화면에 떠 있는 소제목만 중복 검사.
  var body = new URLSearchParams({{name: name}});
  getAllGroups().forEach(function(g) {{ body.append("active_names", g.value); }});
  fetch("http://{settings_host}:{settings_port}/add-custom-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else if (res.status === 409) {{ alert("중복된 소제목이 있습니다 — 다른 이름을 써주세요."); }}
    else {{ alert("소제목 만들기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("소제목 만들기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] app.renderer와 동일 — 직접 키워드 작성 메모.
function toggleKeywordNote() {{
  document.getElementById("keyword-note-zone").classList.toggle("is-open");
}}
function saveKeywordNote() {{
  var text = document.getElementById("keyword-note-input").value;
  fetch("http://{settings_host}:{settings_port}/save-manual-keyword-note", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: text}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("저장하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("저장하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function clearKeywordNote() {{
  document.getElementById("keyword-note-input").value = "";
  saveKeywordNote();
}}
function removeCustomGroup(btn) {{
  var name = btn.dataset.name;
  if (!confirm('"' + name + '" 소제목을 삭제할까요?')) return;
  fetch("http://{settings_host}:{settings_port}/remove-custom-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{name: name}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("삭제에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("삭제에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [수정: 2026-07-30] 예전엔 성공하면 location.reload()로 페이지 전체를 새로 불러왔는데,
// 그 reload가 서버에서 전체 그룹 재검색을 한 번 더 유발해 클릭 한 번에 네이버 검색이
// 2번(이동 계산 1번 + reload로 다시 그리기 1번) 돌았다. 이제 서버가 이동 계산에 쓴
// 결과를 그대로 화면 조각(JSON)으로 돌려주고, 여기서 DOM만 갈아끼운다 — 검색은
// 이동 계산 1번으로 끝난다(그마저도 app.preview_cache 덕에 증분 검색이라 가볍다).
function previewMoveArticle(btn) {{
  var url = btn.dataset.url;
  var direction = btn.dataset.direction;
  fetch("http://{settings_host}:{settings_port}/preview-move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, direction: direction}})
  }}).then(function(res) {{
    if (!res.ok) {{ alert("소제목 이동에 실패했습니다. 다시 시도해주세요."); return null; }}
    if (res.status === 204) {{ return null; }}  // 더 옮길 곳이 없거나 검색 실패 — 조용히 무시
    return res.json();
  }}).then(function(data) {{
    if (!data) return;
    document.getElementById("preview-body").innerHTML = data.body;
    document.getElementById("preview-actions").innerHTML = data.actions_html;
    document.getElementById("preview-hint").innerHTML = data.hint;
    PLAIN_TEXT = data.plain_text;
    // [추가: 2026-08-04] #preview-body를 통째로 갈아끼웠으니 시간순/언론사순 보기가
    // 기억해둔 기사 노드 위치(_FLAT_VIEW_HOMES)는 전부 무효 — 다시 캡처하게 비운다.
    _FLAT_VIEW_HOMES = null;
    renderRelativeTimes();
    // [추가: 2026-08-05] 이 함수는 새로고침 없이 DOM만 바로 갈아끼우므로, 다른 이동
    // 함수처럼 sessionStorage를 거칠 필요 없이 방금 옮긴 기사를 바로 표시할 수 있다.
    var movedEl = document.querySelector('.article[data-url="' + CSS.escape(url) + '"]');
    if (movedEl) {{ movedEl.classList.add("just-moved"); }}
  }}).catch(function() {{
    alert("소제목 이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function promoteManualArticle(btn) {{
  var url = btn.dataset.url;
  fetch("http://{settings_host}:{settings_port}/promote-manual-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justPromotedUrl", url); location.reload(); }}
    else {{ alert("정식 스크랩으로 승격하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("정식 스크랩으로 승격하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function promoteManualArticleToDraft(btn) {{
  var url = btn.dataset.url;
  fetch("http://{settings_host}:{settings_port}/promote-manual-article-to-draft", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justPromotedUrl", url); location.reload(); }}
    else {{ alert("다음 회차 초안에 포함하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("다음 회차 초안에 포함하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function renameGroup(btn) {{
  var name = btn.dataset.name;
  var current = btn.dataset.current;
  var newLabel = prompt("소제목 이름을 입력하세요", current);
  if (newLabel === null) return;
  newLabel = newLabel.trim();
  if (!newLabel) {{ alert("소제목 이름은 비워둘 수 없습니다."); return; }}
  // [수정: 2026-08-04] app.renderer와 동일 — 지금 화면에 떠 있는 소제목만 중복 검사.
  var body = new URLSearchParams({{name: name, label: newLabel}});
  getAllGroups().forEach(function(g) {{ body.append("active_names", g.value); }});
  fetch("http://{settings_host}:{settings_port}/rename-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else if (res.status === 409) {{ alert("중복된 소제목이 있습니다 — 다른 이름을 써주세요."); }}
    else {{ alert("이름 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("이름 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function hideGroup(btn) {{
  var section = btn.closest(".subheading");
  if (!confirm("이 소제목의 기사를 전부 숨길까요? (숨긴 기사 관리에서 하나씩 되돌릴 수 있어요)")) return;
  var urls = Array.prototype.map.call(section.querySelectorAll(".hide-btn[data-url]"), function (el) {{
    return el.dataset.url;
  }});
  Promise.all(urls.map(function (url) {{
    return fetch("http://{settings_host}:{settings_port}/hide-article", {{
      method: "POST", keepalive: true,
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: new URLSearchParams({{url: url}})
    }});
  }})).then(function (responses) {{
    if (responses.every(function (res) {{ return res.ok; }})) {{ location.reload(); }}
    else {{ alert("일부 기사를 숨기지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function () {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
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
// [추가: 2026-07-29] 지난번 이 화면을 봤을 때 있던 기사 URL 목록을 저장해뒀다가, 이번
// 로드에서 그 목록에 없는 것만 "새로 들어온 기사"로 표시한다(is-seen과는 다른 개념 —
// "내가 클릭해봤나"가 아니라 "지난 로드 이후 새로 생겼나"). previewKnownUrls가 아예
// 없으면(이 브라우저에서 초안 화면 첫 방문) 비교 기준이 없으므로 아무것도 표시하지
// 않고 이번 목록을 기준선으로만 저장한다.
(function () {{
  var currentUrls = Array.prototype.map.call(
    document.querySelectorAll(".article[data-url]"),
    function (el) {{ return el.dataset.url; }}
  );
  var known = null;
  try {{
    var raw = localStorage.getItem("previewKnownUrls");
    known = raw === null ? null : JSON.parse(raw);
  }} catch (e) {{ known = null; }}
  if (known !== null) {{
    document.querySelectorAll(".article[data-url]").forEach(function (el) {{
      if (known.indexOf(el.dataset.url) === -1) {{ el.classList.add("is-new-arrival"); }}
    }});
  }}
  localStorage.setItem("previewKnownUrls", JSON.stringify(currentUrls));
}})();
// [추가: 2026-08-05] app.renderer와 동일 — 방금 승격한 기사를 한 번 표시하고 지운다.
(function() {{
  var justPromotedUrl = sessionStorage.getItem("justPromotedUrl");
  if (!justPromotedUrl) {{ return; }}
  sessionStorage.removeItem("justPromotedUrl");
  var el = document.querySelector('.article[data-url="' + CSS.escape(justPromotedUrl) + '"]');
  if (el) {{ el.classList.add("just-promoted"); }}
}})();
// [추가: 2026-08-05] app.renderer와 동일 — 일괄 위/아래 이동 직전 체크했던 URL들을 다시 체크.
(function() {{
  var raw = sessionStorage.getItem("bulkMoveCheckedUrls");
  if (!raw) {{ return; }}
  sessionStorage.removeItem("bulkMoveCheckedUrls");
  var urls = JSON.parse(raw);
  urls.forEach(function(u) {{
    var cb = document.querySelector('.article-select[data-url="' + CSS.escape(u) + '"]');
    if (cb) {{ cb.checked = true; }}
    var el = document.querySelector('.article[data-url="' + CSS.escape(u) + '"]');
    if (el) {{ el.classList.add("just-moved"); }}
  }});
  updateBulkMoveBar();
}})();
// [추가: 2026-08-05] app.renderer와 동일 — moveArticleToGroup/bulkMoveSelected가 남겨둔
// "방금 옮긴 기사들"을 한 번 표시한다.
(function() {{
  var raw = sessionStorage.getItem("justMovedUrls");
  if (!raw) {{ return; }}
  sessionStorage.removeItem("justMovedUrls");
  JSON.parse(raw).forEach(function(u) {{
    var el = document.querySelector('.article[data-url="' + CSS.escape(u) + '"]');
    if (el) {{ el.classList.add("just-moved"); }}
  }});
}})();
// [추가: 2026-08-05] app.renderer와 동일 — moveTocOrder가 남겨둔 신호가 있으면 목차
// 팝오버를 다시 열어둔다.
(function() {{
  if (!sessionStorage.getItem("tocPopoverReopen")) {{ return; }}
  sessionStorage.removeItem("tocPopoverReopen");
  document.getElementById("toc-popover").classList.add("is-open");
  buildTocPopover();
}})();
</script>
</body>
</html>
"""


def _theme() -> dict:
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
        # [추가: 2026-07-28] "이미 확인한 기사" 표시 전용 색 — app.renderer와 통일.
        "seen_purple": "#A855F7",
        # [추가: 2026-07-29] "지난번 봤을 때는 없었는데 이번에 새로 들어온 기사" 표시 전용
        # 색 — 초안은 열 때마다 다시 검색해 새 기사가 계속 섞여 들어오므로, 뭐가 방금
        # 추가됐는지 눈에 띄게 한다. 옅은 노랑(경고색 아님, 그냥 참고 정보) — 진한 톤은
        # 이 화면의 옅은 파스텔 톤과 안 어울려서 일부러 연하게 뒀다.
        "new_arrival_bg": "#FFF9C4",
        # [추가: 2026-08-03] 액션 툴바 버튼 리디자인(시안 B/A) 전용 색 — app.renderer와 통일.
        "tonal_hover": "#DCEAFE",
        "ghost_border": "#C7D9F7",
        "ghost_border_hover": "#A9C6F5",
    }


def _home_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/home.html"


def _scrap_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/index.html"


def _live_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/live.html"


def _hidden_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden"


def _preview_keywords_signature(groups: list, slot: dict) -> dict:
    """캐시 유효성 비교용 값 — 키워드 그룹 구성 + 지금 회차의 식별값(slot["end"]).

    사용자가 검색 키워드를 고치거나(그룹 추가/삭제, 키워드 on-off, OR/AND 변경) 회차가
    바뀌면(정식 스크랩이 끝나 다음 회차로 넘어가면) 이 값이 달라져 캐시가 자동으로
    무효화된다(_load_preview_search_base 참고).
    """
    return {
        "keywords": [{"keywords": g["keywords"], "mode": g.get("mode", "OR")} for g in groups],
        "slot_end": slot["end"],
    }


def _load_preview_search_base(groups: list, slot: dict) -> tuple:
    """캐시가 유효하면(오늘 날짜 + 키워드 구성 + 회차 동일) 캐시된 기사와 "마지막으로
    본 시각"을, 아니면(캐시 없음/날짜 바뀜/키워드 바뀜/회차 바뀜) 빈 목록과 이 회차의
    시작 시각을 돌려준다. app.live_renderer._load_live_search_base와 같은 패턴이다.
    """
    signature = _preview_keywords_signature(groups, slot)
    cache = load_preview_cache()
    if cache is not None and cache.get("signature") == signature:
        last_seen = cache.get("last_seen_pub_date")
        if last_seen:
            return cache.get("articles", []), datetime.fromisoformat(last_seen)
    return [], kst_today_at(slot["start"])


def _compute_preview_articles(settings: dict, slot: dict) -> list:
    """slot 시작~지금까지를, 정식 회차(app.scraper.collect_run)와 똑같은 순서로 미리
    검색·정렬·필터링한다(검색 → 아웃렛 화이트리스트/우선순위 정렬 → 포토/인사 기사 제외
    → 제목 중복 제거 → 예약된 기사 합치기 → 숨긴 기사 제외 → 사용자가 직접 정리한 순서
    반영) — 저장은 하지 않는다.

    [수정: 2026-07-30] 매번 slot 시작부터 전체를 다시 검색하던 것을, app.live_cache와
    같은 증분 캐시(app.preview_cache)로 바꿨다 — 초안은 열 때마다, 특히 ↑/↓로 순서를
    옮길 때마다 다시 계산되는데(preview_move_article), 그때마다 등록된 키워드 그룹
    전체를 처음부터 다시 검색하면 느렸다. 이제 "마지막으로 본 시각 이후" 것만 추가로
    검색해 캐시에 병합한다. 검색 자체가 실패해도 캐시가 있으면 그 결과로 계속
    진행하고(완전히 빈 오류로 막지 않음), 캐시조차 없으면 예외를 그대로 올려 호출하는
    쪽(generate_preview_page/preview_move_article)이 안내 화면으로 대체하게 한다.

    [추가: 2026-07-30] 마지막에 app.preview_order.apply_preview_order를 거친다 — 검색
    결과는 매번 새로 계산되지만(저장된 회차 없음), 사용자가 ↑/↓로 같은 소제목 안에서
    정리해둔 순서(preview_move_article이 저장)는 여기서 다시 입혀진다. 그 사이 새로
    나온 기사는 원래 순서(언론사 우선순위) 그대로 뒤에 붙는다.
    """
    groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if g.get("include_in_scrap")]
    )
    outlet_order = settings.get("outlet_order", [])
    signature = _preview_keywords_signature(groups, slot)
    cached_articles, after_dt = _load_preview_search_base(groups, slot)
    before_dt = kst_today_at(datetime.now().strftime("%H:%M"))
    try:
        new_articles = search_articles_by_groups(groups, after=after_dt, before=before_dt)
    except requests.exceptions.RequestException:
        if not cached_articles:
            raise
        new_articles = []
    existing_cache_urls = {a["url"] for a in cached_articles}
    articles = cached_articles + [a for a in new_articles if a["url"] not in existing_cache_urls]
    pub_dates = [datetime.fromisoformat(a["pub_date"]) for a in articles if a.get("pub_date")]
    newest = max(pub_dates) if pub_dates else after_dt
    save_preview_cache(signature, newest.isoformat(), articles)

    if outlet_order:
        articles = filter_by_outlet_whitelist(articles, outlet_order)
        articles = sort_by_outlet_priority(articles, priority_outlets=outlet_order)
    else:
        articles = sort_by_outlet_priority(articles)
    if not settings.get("include_photo_in_scrap", False):
        articles = exclude_photo_articles(articles)
    if not settings.get("include_personnel_in_scrap", False):
        articles = exclude_personnel_articles(articles)
    articles = deduplicate_by_title(articles)
    # [추가: 2026-07-27] "위로"(예약) 눌러둔 기사를 검색 결과에 합쳐서, 이 회차가
    # 실제로 저장되기 전에도 초안 화면 소제목 분류에 바로 묶여 보이게 한다
    # (app.scraper.collect_run이 저장 시점에 똑같이 합치므로 나중에도 그대로 유지된다).
    pending = load_draft_pending_articles()
    if pending:
        existing_urls = {a["url"] for a in articles}
        articles = articles + [a for a in pending if a["url"] not in existing_urls]
    articles = apply_summary_overrides(filter_hidden(articles))
    return apply_preview_order(articles, load_preview_order())


def _group_select_html(current_name: str, all_names: list, labels: dict) -> str:
    """"다른 소제목으로" 드롭다운 — 지금 속한 소제목은 옵션에서 빼고 나머지를 보여준다.

    [추가: 2026-07-30] 체크박스 하단 바와 별개로, 기사 하나만 바로 다른 소제목에
    옮기고 싶을 때 쓴다(경계를 여러 번 넘나들며 ↑/↓를 반복할 필요 없이 한 번에 이동).
    고르는 즉시 반영되고(onchange), 옮길 곳이 아예 없으면(소제목이 이거 하나뿐)
    빈 문자열을 돌려줘 드롭다운 자체를 숨긴다.
    """
    others = [n for n in all_names if n != current_name]
    if not others:
        return ""
    options = "".join(
        f'<option value="{html.escape(n)}">{html.escape(_group_option_label(n, labels))}</option>'
        for n in others
    )
    return (
        '<select class="group-move-select" onclick="event.stopPropagation();" '
        'onchange="event.stopPropagation(); moveArticleToGroup(this);">'
        '<option value="" selected disabled>다른 소제목</option>'
        f"{options}"
        "</select>"
    )


def _render_preview_groups(
    groups: list, highlight_words: list, line_template: str, labels: dict, rank_by_url: dict, custom_names: set
) -> str:
    """소제목별 화면을 렌더링한다 (app.renderer._render_groups와 구성은 같지만, ↑/↓ 버튼이
    호출하는 JS만 previewMoveArticle로 다르다 — 미리보기는 저장된 회차가 없어
    app.curation.move_article이 기대하는 "현재 회차의 저장된 기사 목록 갱신"을 쓸 수
    없고, 소제목 경계를 넘는 이동(group_overrides.json)만 반영한다).

    [수정: 2026-07-30] 기사마다 체크박스(다중 선택)와 "다른 소제목으로" 드롭다운을
    함께 그린다 — 자동분류가 완벽하지 않아 여러 개를 한꺼번에 바로잡아야 하는 경우가
    있어서다(하단 선택 바가 여러 개를, 이 드롭다운이 하나씩 바로 옮기는 걸 담당).

    groups 중 기사가 0개인 소제목(app.custom_groups로 사용자가 미리 만들어둔 빈
    소제목)은 기사 목록 대신 안내 문구를 보여주고, 헤더의 🗑️도 "기사 숨기기"가 아니라
    "이 빈 소제목 자체를 삭제"로 동작한다 — 지울 기사가 없으니 의미가 다르다.
    """
    all_names = [g["name"] for g in groups]
    sections = []
    last_group_index = len(groups) - 1
    for group_index, group in enumerate(groups):
        original_name = html.escape(group["name"])
        display_name = html.escape(display_group_name(group["name"], labels))
        if not group["articles"]:
            body = (
                '<p class="empty-group-hint">아직 기사가 없어요 — 체크박스로 선택하거나 '
                "드롭다운으로 기사를 여기로 옮겨보세요.</p>"
            )
            delete_btn = (
                f'<button class="rename-btn" type="button" data-name="{original_name}" '
                'onclick="removeCustomGroup(this)" title="이 빈 소제목 삭제">🗑️</button>'
            )
        else:
            last_index = len(group["articles"]) - 1
            group_select = _group_select_html(group["name"], all_names, labels)
            body = "\n".join(
                render_article(
                    a,
                    highlight_words,
                    line_template,
                    move={
                        "disable_up": i == 0 and group_index == 0,
                        "disable_down": i == last_index and group_index == last_group_index,
                    },
                    move_handler="previewMoveArticle",
                    checkbox=True,
                    group_select_html=group_select,
                    outlet_rank=rank_by_url.get(a["url"], 0),
                )
                for i, a in enumerate(group["articles"])
            )
            delete_btn = (
                f'<button class="rename-btn" type="button" onclick="hideGroup(this)" '
                f'title="이 소제목 통째로 숨기기(기사 각각 숨긴 것과 동일 — 되돌리기 가능)">🗑️</button>'
            )
        # [추가: 2026-08-03] app.renderer._render_groups와 동일한 이유 — 소제목 자체의
        # 화면 순서를 ↑/↓로 바꾼다.
        order_up_disabled = " disabled" if group_index == 0 else ""
        order_down_disabled = " disabled" if group_index == last_group_index else ""
        order_html = (
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'up\');"{order_up_disabled} title="소제목 위로 이동">↑</button>'
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'down\');"{order_down_disabled} title="소제목 아래로 이동">↓</button>'
        )
        # [추가: 2026-08-04] app.renderer._render_groups와 동일 — 소제목 안 기사 전체 선택.
        select_all_html = (
            '<input type="checkbox" class="group-select-all" '
            'onclick="event.stopPropagation();" onchange="toggleGroupSelectAll(this);" '
            'title="이 소제목 기사 전체 선택"> '
            if group["articles"]
            else ""
        )
        heading = (
            f'<h2><span class="subheading-title">{select_all_html}&lt;{display_name}&gt;</span>'
            f'<span class="subheading-icons">'
            f'<button class="rename-btn" type="button" data-name="{original_name}" '
            f'data-current="{display_name}" onclick="renameGroup(this)" title="소제목 이름 바꾸기">✏️</button> '
            f"{delete_btn}{order_html}</span></h2>"
        )
        section_class = "subheading subheading-custom" if group["name"] in custom_names else "subheading"
        sections.append(
            f'<section class="{section_class}" id="subheading-{group_index}" data-toc-name="{display_name}">'
            f"{heading}{body}</section>"
        )
    return "\n".join(sections)


def _render_preview_bottom(articles: list, groups: list, keywords: list, highlight_words: list, labels: dict) -> str:
    keyword_tags = ", ".join(html.escape(k) for k in extract_keywords(articles, keywords))
    summary_lines = "\n".join(
        f'<p><strong>&lt;{html.escape(display_group_name(s["name"], labels))}&gt;</strong> '
        f'{highlight_keywords(s["summary"], highlight_words)}</p>'
        for s in summarize_groups(groups)
    )
    return (
        '<div class="bottom">'
        "<h3>🤖 AI가 추출한 주요 키워드</h3>"
        f"<p>{keyword_tags}</p>"
        "<h3>💬 AI가 읽은 소제목별 주요 요약</h3>"
        f"{summary_lines}"
        "</div>"
    )


def _manual_section_html(highlight_words: list, line_template: str) -> str:
    """"직접 추가한 기사"(app.manual_articles) 구획 — 미리보기 대상 회차가 14:00→17:00처럼
    넘어가도(app.scheduler.next_pending_slot) 이 구획은 영향받지 않는다. manual_articles.json은
    당일 자정에만 비워지는 완전히 별개 저장소라, 어느 회차를 미리보고 있는지와 무관하게
    항상 같은 내용을 보여준다(app.renderer.render_page가 index.html에서 하는 것과 동일).

    [수정: 2026-07-28] "📝 초안에 포함" 버튼을 누르면 아직 저장된 회차가 없어서 즉시
    끼워 넣을 곳이 없으므로, app.draft_articles의 예약 목록에 담아뒀다가 다음 회차가
    실제로 저장될 때(app.scraper.collect_run) 합쳐지고, 그 전까지도 이 초안 화면의
    소제목 분류에는 바로 반영된다(_compute_preview_articles). "📗 완성본에 포함"
    버튼은 완성 화면과 똑같이 이미 저장된 최신 회차에 바로 끼워 넣는다 — 렌더링
    자체는 app.renderer._render_manual_section을 그대로 재사용한다(두 버튼 다
    화면과 무관하게 동일하게 동작하므로 이 화면만의 커스터마이즈가 필요 없다).
    """
    manual_articles = filter_hidden(load_manual_articles())
    return _render_manual_section(manual_articles, highlight_words, line_template)


def _build_preview_plain_text(slot: dict, groups: list, line_template: str, labels: dict) -> str:
    """초안 화면의 copy/export용 텍스트를 만든다 (app.renderer._build_plain_text와 형식은
    같지만 헤더에 "[초안]"이 붙는다는 것만 다르다).

    [추가: 2026-07-27] 헤더 형식은 완성본과 똑같이 "언론 모니터링 {end} 기준"으로 두고
    맨 앞에 "[초안]"만 덧붙인다 — 처음엔 "지금 OO:OO 현재 기준"까지 같이 넣었는데,
    완성본과 긁는(추출하는) 방식 자체를 통일해달라는 요청에 따라 뺐다. "[초안]" 표시
    하나로도 정식 완성본과는 구분되고, 형식은 완성본과 동일해야 나중에 이 텍스트를
    완성본에 이어 붙이거나 비교할 때도 헷갈리지 않는다.
    """
    lines = [f"[초안] 언론 모니터링 {format_slot_time_kr(slot['end'])} 기준"]
    # [추가: 2026-08-05] app.renderer._build_plain_text와 동일 — 직접 작성한 메모가
    # 있으면 헤더 바로 아래 한 줄로 끼워 넣는다.
    note = load_manual_keyword_note()
    if note:
        lines.append(f"- {note}")
    lines.append("")
    if not groups:
        lines.append("💤")
    else:
        for group in groups:
            lines.append(f"<{display_group_name(group['name'], labels)}>")
            for article in group["articles"]:
                lines.append(apply_line_template(line_template, article["outlet"], article["title"]))
                lines.append(article["url"])
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _highlight_words_json(highlight_words: list) -> str:
    """하단바 🖍️ 팝오버에 실어 보낼 형광펜 단어 목록 — app.renderer와 동일한 형식
    ({"word":..., "color_hex":...})으로, _PAGE_TEMPLATE을 쓰는 모든 곳(정상 렌더링·
    끝남/오류 안내 화면·↑/↓ 이동 후 부분 갱신)이 공유한다."""
    return json.dumps(
        [
            {"word": item["word"], "color_hex": HIGHLIGHT_COLORS[item.get("color", 0) % len(HIGHLIGHT_COLORS)]}
            for item in highlight_words
        ],
        ensure_ascii=False,
    ).replace("</", "<\\/")


def _actions_html(plain_text: str, slot_end: str, generated_at: str) -> str:
    export_filename = f"스크랩초안_{slot_end.replace(':', '-')}_{generated_at.replace(':', '-')}.txt"
    return (
        '<button onclick="copyPlainText()">복사</button>'
        f'<a class="btn" href="data:text/plain;charset=utf-8,{quote(plain_text)}" '
        f'download="{html.escape(export_filename)}">txt로 저장</a>'
        '<button onclick="sendToTelegram(this)">📤 Telegram 전송</button>'
        '<select class="view-mode-select" onchange="applyViewMode(this.value)" '
        'title="소제목 구성은 그대로 두고 화면에 나열하는 순서만 바꿉니다">'
        '<option value="subheading" selected>소제목별</option>'
        '<option value="group-time">소제목 내 시간순</option>'
        '<option value="time">시간순</option>'
        '<option value="outlet">언론사순</option>'
        "</select>"
        '<button class="create-group-btn" type="button" onclick="createCustomGroup()">+ 새 소제목 만들기</button>'
        '<button class="create-group-btn" type="button" onclick="toggleKeywordNote()">+ 직접 키워드 작성하기</button>'
    )


def _compute_preview_content(
    slot: dict, articles: list, keywords: list, highlight_words: list, line_template: str, generated_at: str
) -> dict:
    """render_preview_page와 preview_move_article이 공유하는 실제 렌더링 계산 —
    소제목 분류·본문 HTML·안내문구·복사용 텍스트·상단 액션 버튼을 만든다.

    [추가: 2026-07-30] preview_move_article이 ↑/↓ 이동 직후 이 결과를 그대로
    JSON으로 돌려줄 수 있도록 render_preview_page에서 분리했다 — 페이지 전체를
    다시 감싸는 부분(_PAGE_TEMPLATE.format)만 render_preview_page에 남기고,
    실제로 매번 다시 계산해야 하는 내용은 여기 한 곳에만 있다.
    """
    labels = load_group_labels()
    custom_names = load_custom_groups()
    groups = (
        classify_articles(articles, keywords, forced_groups=load_group_overrides(), custom_group_names=custom_names)
        if articles
        else []
    )

    # [추가: 2026-07-30] "+ 새 소제목 만들기"로 미리 만들어둔, 아직 기사가 하나도 없는
    # 소제목도 화면에 같이 보여준다 — 실제 분류 결과(groups)와 합쳐서 한 번에 렌더링해야
    # "다른 소제목으로" 드롭다운·하단 선택 바가 이 빈 소제목도 이동 대상으로 인식한다.
    # 복사/내보내기 텍스트(_build_preview_plain_text)·AI 요약(_render_preview_bottom)에는
    # 원래의 groups만 넘겨 빈 placeholder가 섞이지 않게 한다.
    existing_names = {g["name"] for g in groups}
    empty_custom_groups = [{"name": n, "articles": []} for n in custom_names if n not in existing_names]
    # [추가: 2026-08-03] app.renderer.render_page와 동일한 이유 — 사용자가 ↑/↓로 정한
    # 소제목 순서를 얹고, 요약·복사 텍스트용 groups도 이 순서를 그대로 반영한다.
    render_groups = apply_group_order(groups + empty_custom_groups)
    groups = [g for g in render_groups if g["articles"]]
    # [추가: 2026-08-04] app.renderer.render_page와 동일 — "언론사순" 보기용 순위.
    rank_by_url = {a["url"]: i for i, a in enumerate(articles)}

    if not render_groups:
        body = '<div class="empty">💤<br>아직 모인 기사가 없어요</div>'
    else:
        body = _render_preview_groups(render_groups, highlight_words, line_template, labels, rank_by_url, set(custom_names))
        if groups:
            body += _render_preview_bottom(articles, groups, keywords, highlight_words, labels)
    body += _manual_section_html(highlight_words, line_template)
    hint = (
        f"⏰ 아직 정식 회차가 아니에요 — {html.escape(format_slot_time_kr(slot['end']))}에 자동으로 정식 수집됩니다. "
        "지금 숨기거나(🗑️) 소제목을 옮겨두면(↑/↓) 정식 회차에도 그대로 반영돼요."
    )
    plain_text = _build_preview_plain_text(slot, groups, line_template, labels)
    return {
        "body": body,
        "hint": hint,
        "plain_text": plain_text,
        "actions_html": _actions_html(plain_text, slot["end"], generated_at),
    }


def render_preview_page(slot: dict, articles: list, keywords: list, highlight_words: list, line_template: str, generated_at: str) -> str:
    content = _compute_preview_content(slot, articles, keywords, highlight_words, line_template, generated_at)
    manual_keyword_note = load_manual_keyword_note()
    return _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html=(
            f"({html.escape(format_slot_time_kr(slot['end']))} 예정 · "
            f"지금 {html.escape(format_slot_time_kr(generated_at))} 기준)"
        ),
        hint=content["hint"],
        body=content["body"],
        actions_html=content["actions_html"],
        note_open_class=" is-open" if manual_keyword_note else "",
        note_value_attr=html.escape(manual_keyword_note),
        plain_text_json=json.dumps(content["plain_text"], ensure_ascii=False).replace("</", "<\\/"),
        highlight_words_json=_highlight_words_json(highlight_words),
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )


def render_preview_done_page() -> str:
    settings = load_settings()
    body = '<div class="empty">☕️ 수고하셨습니다.</div>'
    body += _manual_section_html(settings.get("highlight_keywords", []), DEFAULT_ARTICLE_LINE_TEMPLATE)
    manual_keyword_note = load_manual_keyword_note()
    return _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html="",
        hint="⏰ 오늘 예정된 회차가 모두 끝났어요.",
        body=body,
        actions_html="",
        note_open_class=" is-open" if manual_keyword_note else "",
        note_value_attr=html.escape(manual_keyword_note),
        plain_text_json='""',
        highlight_words_json=_highlight_words_json(settings.get("highlight_keywords", [])),
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )


def render_preview_error_page(slot: dict) -> str:
    settings = load_settings()
    body = '<div class="empty">🔌 지금은 조회할 수 없어요.<br>잠시 후 새로고침해주세요.</div>'
    body += _manual_section_html(settings.get("highlight_keywords", []), DEFAULT_ARTICLE_LINE_TEMPLATE)
    manual_keyword_note = load_manual_keyword_note()
    return _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html=f"({html.escape(format_slot_time_kr(slot['end']))} 예정)",
        hint="⏰ 네이버 검색 중 오류가 발생했습니다.",
        body=body,
        actions_html="",
        note_open_class=" is-open" if manual_keyword_note else "",
        note_value_attr=html.escape(manual_keyword_note),
        plain_text_json='""',
        highlight_words_json=_highlight_words_json(settings.get("highlight_keywords", [])),
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )


def generate_preview_page() -> Path:
    """요청이 올 때마다(새로고침 포함) 다시 계산해 preview.html을 새로 만든다 — live.html과
    같은 "매번 새로 생성, 저장 없음" 패턴. 오늘 남은 회차가 없으면 안내 화면으로 대체한다.
    """
    settings = load_settings()
    now = datetime.now()
    slot = next_pending_slot(now, active_schedule_times(settings))
    if slot is None:
        html_text = render_preview_done_page()
        atomic_write_text(PREVIEW_HTML_PATH, html_text)
        return PREVIEW_HTML_PATH

    try:
        articles = _compute_preview_articles(settings, slot)
    except requests.exceptions.RequestException:
        html_text = render_preview_error_page(slot)
        atomic_write_text(PREVIEW_HTML_PATH, html_text)
        return PREVIEW_HTML_PATH

    keywords = all_search_keywords(settings)
    html_text = render_preview_page(
        slot,
        articles,
        keywords,
        settings.get("highlight_keywords", []),
        settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE),
        now.strftime("%H:%M"),
    )
    atomic_write_text(PREVIEW_HTML_PATH, html_text)
    return PREVIEW_HTML_PATH


def preview_move_article(url: str, direction: str) -> Optional[dict]:
    """미리보기 화면의 ↑/↓ 버튼이 호출한다(app.settings_server._handle_preview_move_article).

    저장된 회차가 없으므로 미리보기 목록을 다시 계산해 그 기준으로 옮긴다 — 소제목
    경계를 넘는 이동은 app.curation.set_group_override(URL 기준 전역 저장)로, 같은
    소제목 "안"에서의 순서 변경은 app.preview_order.save_preview_order로 각각 저장한다.

    [수정: 2026-07-30] 예전엔 같은 소제목 내 순서 변경은 저장할 곳이 없어 새로고침하면
    사라졌다 — move_article이 계산해낸 새 순서(new_articles)를 그냥 버리고 있었기
    때문. 이제 그 결과를 preview_order.json에 저장해 다음 계산(_compute_preview_articles)
    에도 그대로 반영되고, 정식 회차가 저장될 때(app.scraper.collect_run)도 한 번 더
    이어받는다.

    [수정: 2026-07-30] 예전엔 이 함수가 저장만 하고 None을 돌려주면, 브라우저가
    location.reload()로 페이지를 통째로 다시 불러와 서버가 검색을 한 번 더 하게
    만들었다(클릭 한 번에 전체 그룹 재검색 2번). 이제 이동 계산에 쓴 결과로 화면
    조각(본문·상단 액션·안내문구·복사용 텍스트)까지 바로 만들어 돌려준다 — 호출하는
    쪽(app.settings_server)이 이걸 JSON으로 그대로 응답하면 브라우저는 reload 없이
    DOM만 갈아끼우면 된다. preview.html 파일 자체도 같이 갱신해둬서, 다른 탭에서
    새로고침해도 같은 결과가 보인다.

    더 옮길 곳이 없거나(url을 못 찾음) 검색 실패로 계산 자체를 할 수 없으면(캐시도
    없어 예외가 남) None을 돌려주고 조용히 무시한다 — 호출하는 쪽이 204만 응답한다.
    """
    settings = load_settings()
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot is None:
        return None
    try:
        articles = _compute_preview_articles(settings, slot)
    except requests.exceptions.RequestException:
        return None
    keywords = all_search_keywords(settings)
    overrides = load_group_overrides()
    new_articles, new_override = move_article(articles, keywords, url, direction, overrides)
    if new_override is not None:
        set_group_override(*new_override)
        final_articles = articles
    else:
        save_preview_order([a["url"] for a in new_articles])
        final_articles = new_articles

    highlight_words = settings.get("highlight_keywords", [])
    line_template = settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)
    generated_at = datetime.now().strftime("%H:%M")
    content = _compute_preview_content(slot, final_articles, keywords, highlight_words, line_template, generated_at)
    manual_keyword_note = load_manual_keyword_note()

    full_html = _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html=(
            f"({html.escape(format_slot_time_kr(slot['end']))} 예정 · "
            f"지금 {html.escape(format_slot_time_kr(generated_at))} 기준)"
        ),
        hint=content["hint"],
        body=content["body"],
        actions_html=content["actions_html"],
        note_open_class=" is-open" if manual_keyword_note else "",
        note_value_attr=html.escape(manual_keyword_note),
        plain_text_json=json.dumps(content["plain_text"], ensure_ascii=False).replace("</", "<\\/"),
        highlight_words_json=_highlight_words_json(highlight_words),
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )
    atomic_write_text(PREVIEW_HTML_PATH, full_html)
    return content


def preview_bulk_move_articles(urls: list, direction: str) -> bool:
    """미리보기 화면 체크박스 하단 "일괄 이동" 바의 ↑/↓가 fetch로 호출한다
    (app.settings_server._handle_preview_bulk_move_order, JS bulkMoveOrder).

    같은 소제목 안에서 선택한 기사 여러 개를 통째로 한 칸 위/아래로 옮긴다 — 실제
    순서 계산은 app.curation.bulk_move_articles가 맡고(소제목 경계를 넘는 조합이면
    아무 것도 안 바꾸고 그대로 돌려준다), 여기서는 그 결과를 preview_order.json에
    저장할 뿐이다(소제목 경계를 넘는 케이스 자체가 없으므로 set_group_override를 쓸
    일도 없다 — preview_move_article과 다른 점).

    [수정: 2026-08-04] preview_move_article과 달리 화면 조각을 JSON으로 바로 안
    돌려주고 단순 새로고침 방식을 쓴다 — "다른 소제목으로" 일괄 이동(bulkMoveSelected)도
    이미 같은 방식이라 하단 바 안에서 일관성을 맞췄다.
    """
    settings = load_settings()
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot is None:
        return False
    try:
        articles = _compute_preview_articles(settings, slot)
    except requests.exceptions.RequestException:
        return False
    keywords = all_search_keywords(settings)
    overrides = load_group_overrides()
    new_articles = bulk_move_articles(articles, keywords, urls, direction, overrides)
    if new_articles is articles:
        return False
    save_preview_order([a["url"] for a in new_articles])
    generate_preview_page()
    return True
