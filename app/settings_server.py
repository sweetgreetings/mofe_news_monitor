# Design Ref: archive/DESIGN.md §1 설정 화면 목업(메뉴+하위 6페이지), §3 "최소 로컬 서버" (PRD 규칙 17)
import html
import json
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

from app.config import (
    BURST_MIN_OUTLETS_CHOICES,
    BURST_WINDOW_CHOICES,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_AUTO_SEND_GRACE_MIN,
    DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    FONT_STACK,
    HIDDEN_VIEW_DAYS,
    HIGHLIGHT_COLORS,
    HISTORY_HTML_PATH,
    LANDING_HTML_PATH,
    LOGO_PATH,
    MAX_AUTO_SEND_GRACE_MIN,
    LANDING_KEYWORD_COUNT,
    EMAIL_SERVICES,
    MAX_EMAIL_RECIPIENTS,
    MAX_KEYWORDS_PER_GROUP,
    MAX_KEYWORD_GROUPS,
    MAX_SCHEDULE_GROUPS,
    MAX_SCHEDULE_TIMES,
    MAX_SUBHEADINGS,
    MAX_TELEGRAM_RECIPIENTS,
    MAX_WORDCLOUD_EXCLUDE_WORDS,
    MIN_SCHEDULE_TIMES,
    NAVER_DAILY_CALL_LIMIT,
    OUTLET_ORDER_JUMP_STEP,
    OUTPUT_HTML_PATH,
    PALETTE,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
    WEEKDAY_KEYS,
    WEEKDAY_LABELS_KO,
)
from app.breaking_burst import burst_history
from app.breaking_alert import (
    BreakingAlertSettingsError,
    load_breaking_alert_settings,
    save_breaking_alert_settings,
)
from app.adhoc import routes as adhoc_routes
from app.assigned_groups import add_assigned_group
from app.classifier import (
    classify_articles,
    forced_group_target_names,
    groups_for_confirmed_run,
    snapshot_group_names,
)
from app.llm_classifier import (
    ETC_GROUP_NAME,
    UNCLASSIFIED_GROUP_NAME,
    assign_to_existing,
    build_assign_candidates,
    last_assign_failure_reason,
    last_split_failure_reason,
    round_id_for_run,
    round_id_for_slot,
    split_group_articles,
)
from app.group_split import MIN_SPLIT_ARTICLES, order_with_new_names, plan_split
from app.llm_classifier import last_classification_was_rule_based as llm_last_classification_was_rule_based
from app.reclassify_attempts import record_attempt as record_reclassify_attempt
from app.confirm_send import send_confirmed_run
from app.icons import icon
from app import send_log
from app.config import SEND_LOG_RETENTION_DAYS
from app.topnav import plain_nav, regular_nav, topnav_style
from app.curation import (
    active_hidden_dates,
    bulk_move_articles,
    bulk_reassign_group,
    clear_group_overrides_for,
    display_group_name,
    display_name_in_use,
    filter_hidden,
    hide_articles,
    is_hidden,
    load_group_labels,
    load_group_overrides,
    load_hidden_batches,
    load_hidden_urls,
    move_article,
    set_group_label,
    set_group_override,
    unhide_articles,
)
from app.custom_groups import add_custom_group, load_custom_groups, remove_custom_group
from app.email_recipients import active_recipient_emails, load_email_recipients, save_email_recipients
from app.email_sender import is_configured as email_is_configured
from app.email_sender import send_text as email_send_text
from app.email_sender import check_sender_address as check_email_sender_address, send_test_mail
from app.group_order import apply_group_order, is_order_locked, save_group_order
from app.draft_articles import add_draft_pending_article
from app.excel_export import build_workbook_bytes, format_pub_datetime, sanitize_filename_part
from app.history_renderer import (
    build_excel_rows_for_dates,
    excel_filename_for_dates,
    generate_history_page,
    render_deleted_slot_fragment,
    render_history_day_fragment,
    render_slot_fragment,
)
from app.home_trend import add_trend_word, remove_trend_word
from app.trend_renderer import render_trend_page
from app.trend_export import build_trend_workbook_bytes
from app.landing_renderer import generate_landing_page, wordcloud_candidates
from app.live_renderer import generate_live_page_or_wait
from app.manual_articles import add_manual_article, load_manual_articles, pop_manual_article
from app.manual_keyword_note import save_manual_keyword_note
from app.naver_api import (
    OUTLET_CATEGORIES,
    fetch_article_by_url,
    is_non_news_url,
    normalize_article_url,
    original_url_keys,
    fetch_full_title_and_summary,
    outlet_display_label,
)
from app.naver_api import test_naver_credentials
from app.llm_classifier import test_llm_credentials
from app.credentials import (
    delete_email_credentials,
    delete_llm_credentials,
    email_saved_at,
    email_saved_password,
    email_sender_address,
    email_sender_name,
    email_sender_password,
    email_service,
    email_source,
    save_email_credentials,
    delete_naver_credentials,
    llm_api_key,
    llm_model,
    llm_saved_api_key,
    llm_saved_at,
    llm_source,
    mask as mask_credential,
    naver_client_id,
    naver_client_secret,
    naver_saved_at,
    naver_saved_client_secret,
    naver_source,
    save_llm_credentials,
    delete_telegram_token,
    save_telegram_token,
    telegram_bot_token,
    telegram_saved_at,
    telegram_saved_token,
    telegram_source,
    save_naver_credentials,
)
from app.summary_overrides import load_summary_overrides, set_summary_override
from app.scheduler import next_pending_slot
from app.preview_renderer import (
    _compute_preview_articles,
    current_draft_urls,
    generate_preview_page,
    preview_bulk_move_articles,
    preview_move_article,
)
from app.renderer import (
    range_select_script,
    apply_line_template,
    apply_subheading_format,
    build_group_copy_texts,
    generate_screen,
    known_label_chips_html,
)
from app.settings import (
    SettingsError,
    active_search_groups,
    all_search_keywords,
    cycle_highlight_color,
    group_in_live,
    group_in_scrap,
    load_settings,
    move_outlet,
    pick_active_group_index,
    save_article_line_template,
    save_subheading_format_template,
    save_keyword_groups,
    save_outlet_selection,
    save_adhoc_article_line_template,
    save_adhoc_subheading_format_template,
    save_schedule_groups,
    save_auto_send_settings,
    is_auto_send_enabled,
    save_wordcloud_exclude_words,
    toggle_highlight_keyword,
)
from app.telegram_bot import is_configured as telegram_is_configured
from app.telegram_bot import send_text as telegram_send_text
from app.telegram_bot import check_bot_token
from app.telegram_bot_name import (
    MAX_BOT_NAME_LEN,
    apply_default_bot_name_once,
    fetch_bot_name,
    normalize_bot_name,
    set_bot_name,
)
from app.config import DEFAULT_TELEGRAM_BOT_NAME
from app.telegram_recipients import (
    LEGACY_NOTIFY,
    NEW_RECIPIENT_NOTIFY,
    RECEIVE_FIELDS,
    active_recipient_chat_ids as telegram_active_recipient_chat_ids,
    load_telegram_recipients,
    normalize_notify,
    notify_label,
    notify_sentence,
    save_telegram_recipients,
)
from app.scraper import _already_published_urls
from app.storage import list_run_meta, load_latest_run, update_run_articles
from app.storage import load_run_file
from app.storage import RunLockedError, delete_run, restore_run
from app.undo import peek_label as undo_peek_label
from app.undo import push as undo_push
from app.undo import undo as undo_restore
from app.labels import (
    LabelCollisionError,
    articles_for_labels_and,
    attach_label,
    delete_label,
    detach_label,
    label_stats,
    merge_labels,
    rename_label,
    snapshot_source,
)
from app.label_undo import peek_label as label_undo_peek_label
from app.label_undo import push as label_undo_push
from app.label_undo import undo as label_undo_restore

# 페이지마다 공통으로 쓰는 스타일 조각. 아직 .format()으로 값을 채우기 전(중괄호가 전부
# {{ }}로 이중화된 상태)이라, 각 페이지 템플릿에 문자열로 이어붙인 뒤 페이지 전체를
# 한 번에 .format()해야 한다 — 미리 풀어서 이어붙이면 CSS의 실제 중괄호가 남아
# 바깥쪽 .format() 호출과 충돌한다.
_BASE_STYLE = """
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  /* [수정: 2026-09-11] 폭은 확정본·초안과 같은 800px 하나다 — 본문 카드·상단바·하단
     저장 바 세 자리가 같은 값이어야 한다(하나만 넓히면 상단 링크·저장 버튼이 본문과
     어긋난다). 예전엔 설정 폼용 480px이 기본이고 기사 목록 화면 셋만 body.wide로
     800px을 썼는데, 목록이 길어지는 설정 화면(검색어·언론사 등)까지 좁아져 스크롤만
     길어졌다. 폭만 넓히면 한 줄에 하나씩 쌓는 화면은 오른쪽만 비므로, 긴 화면 여섯은
     각자 템플릿에서 여러 칸 배치를 함께 쓴다(각 템플릿의 "[추가: 2026-09-11]" 참고). */
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: var(--r-lg);
  }}
  /* [수정: 2026-07-26] 모든 설정 세부 페이지에 상단(뒤로/HOME)·하단(저장) 고정 바를
     적용하면서, 내용이 그 바에 가리지 않도록 컨테이너 위쪽에 여백을 더했다. 하단
     여백(저장 버튼 있는 페이지만 필요)은 페이지마다 필요할 때만 덧붙인다. */
  /* [수정: 2026-09-16] 카드 위 여백 60→44px — 60px은 상단 고정바(54px)를 피하려는 값인데 글자 위로
     30px이 비었다. 44px이면 16px 남는다. 확정본·초안·실시간·정기 보관함·설정·수시·정책 단어 추이
     일곱 화면이 같은 값을 써야 화면을 오갈 때 제목이 들썩이지 않는다(수시·추이는 84→68px 형태).
     시안 SUBHEAD_SPACING_MOCKUP.html B안. */
  .container {{ padding-top: 44px; }}
  h1 {{ font-size: var(--fs-xl); color: {header}; }}
  .hint {{ color: {muted}; font-size: var(--fs-md); margin-bottom: 16px; }}
  .caption {{ color: {muted}; font-size: var(--fs-sm); margin: 0 0 12px; }}
  .error {{ color: {error}; margin: 12px 0; }}
{topnav_style}
  .save-bar {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  }}
  .save-bar-inner {{ max-width: 800px; margin: 0 auto; padding: 12px 24px; }}
  .save-bar button {{ width: 100%; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유 — button은 a와 달리 font-family를
     상속받지 않고, appearance:auto(네이티브 OS 버튼 껍데기)까지 남아있어 폰트만 맞춰선
     완전히 똑같이 안 보인다. 크롬이 button 텍스트만 내부적으로 수직 중앙 정렬해주는
     것까지 발견해 align-items: center를 직접 지정했다. */
  button, a.btn {{
    background: {accent}; color: {on_fill}; border: none; border-radius: var(--r-md);
    padding: 8px 16px; font-size: var(--fs-base); font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-flex; align-items: center; justify-content: center;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  button:disabled {{ background: {border}; color: {muted}; cursor: not-allowed; }}
  input[type=text], input[type=number] {{
    background: {bg}; color: {text}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 6px 10px; font-size: var(--fs-base);
  }}
  .del-btn {{
    background: transparent; color: {muted}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 4px 10px; font-size: var(--fs-sm); margin-left: 6px; cursor: pointer;
  }}
  .del-btn:hover {{ background: {hover}; color: {error}; border-color: {error}; }}
  /* [추가: 2026-08-13] 이모지 대신 쓰는 단색 SVG 아이콘(app.icons) 공통 크기·색 —
     currentColor라 부모의 글자색(회색/파랑 호버/헤더 남색 등)을 그대로 물려받는다. */
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
"""

# 상단바는 app/topnav.py 한 곳에서 만든다(시안 mockups/TOPBAR_NAV_MOCKUP.html).
# 설정 하위 화면: 홈 │ ← 설정. 휴지통: 정기 묶음(켜진 칸 없음 — 초안·확정본에서 들어오는 곳).
_TOP_BAR_HTML = plain_nav("설정", f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/")
_LABELS_TOP_BAR_HTML = plain_nav()
_LABEL_MANAGE_TOP_BAR_HTML = plain_nav("라벨 보관함", f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/labels")
_HIDDEN_TOP_BAR_HTML = regular_nav(None)

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
  /* [수정: 2026-08-10] 항목별 이니셜 배지(키/형/시/언/범/형/워/텔/메)를 완전히 없앴다 —
     "형"이 형광펜 긋기·본문 형식 두 곳에 겹치는 등 항목이 늘수록 이니셜이 헷갈린다는
     피드백. 라벨 자체가 이미 충분히 설명적이라(예: "언론사 선택 · 순서 지정") 배지 없이
     텍스트 + 화살표만으로 충분하다고 판단해 아예 뺐다(색깔 이모지로 되돌리면 이전에
     "알록달록해서" 이니셜 배지로 바꿨던 문제가 재발할 수 있어 그 방향은 배제). */
  .menu a {{
    display: flex; align-items: center; gap: 11px; margin: 6px 0; padding: 12px 12px;
    font-size: var(--fs-base); font-weight: 500; color: {text}; text-decoration: none;
    border-radius: var(--r-md); background: {bg};
  }}
  .menu a:hover {{ background: {hover}; }}
  .menu-chev {{ margin-left: auto; color: {border}; font-size: var(--fs-md); }}
  .category {{ margin: 22px 0 0; }}
  .category:first-of-type {{ margin-top: 0; }}
  .category-name {{
    color: {header}; font-size: var(--fs-base); margin-bottom: 8px;
    padding-bottom: 4px; border-bottom: 1px solid {border};
  }}
  /* [추가: 2026-08-20] "연동"(네이버·AI 키) 항목의 상태 배지 — 미등록은 눈에 띄어야
     하고(경고, 노란 톤), .env는 "쓰이고는 있지만 이 화면엔 아직 안 들어왔다"는 중립
     정보라 회색으로 낮춘다. 제대로 저장돼 있을 때는 배지를 아예 안 그린다(설정
     화면 전체가 "고칠 게 있을 때만 표시가 붙는다"는 관례를 따른다). */
  .badge-warn, .badge-env {{
    margin-left: auto; font-size: var(--fs-xs); font-weight: 700; border-radius: var(--r-pill); padding: 2px 8px;
  }}
  .badge-warn {{ color: {warn_text}; background: {warn_chip_bg}; }}
  .badge-env {{ color: {muted}; background: {bg}; border: 1px solid {border}; }}
  .badge-warn + .menu-chev, .badge-env + .menu-chev {{ margin-left: 8px; }}
  /* [추가: 2026-09-18] 정기·수시 묶음 이름 — 홈 흐름도 줄 이름표와 같은 왼쪽 3px 띠. */
  .flow .category-name {{ border-left: 3px solid; padding-left: 8px; font-weight: 700; }}
  .flow.reg .category-name {{ border-left-color: {flow_row_reg_bar}; }}
  .flow.adhoc .category-name {{ border-left-color: {flow_row_adhoc_bar}; }}
  /* 한 묶음 안의 작은 제목(텍스트 표출 형식 · 발송 · 알림) */
  .menu-sub {{ color: {muted}; font-size: var(--fs-sm); margin: 14px 4px 2px; }}
  .menu-sub.first {{ margin-top: 2px; }}
  /* 항목 이름 옆 회색 한정어(이메일 = 정기 확정본만) */
  .menu-note {{ color: {muted}; font-size: var(--fs-sm); font-weight: 400; }}
  /* [추가: 2026-09-11] 폭이 800px이 되면서 메뉴 묶음을 두 단으로 흘린다(CSS 다단 —
     위에서 아래로 읽는 순서가 그대로 유지된다). 묶음 하나가 두 단에 걸쳐 잘리지 않게
     break-inside를 막고, 단 맨 위에서 묶음의 위 여백이 튀지 않게 여백을 아래로 옮긴다.
     좁은 화면에선 한 단으로 돌아간다. */
  .container {{ column-count: 2; column-gap: 32px; }}
  .container > h1 {{ column-span: all; }}
  .container > .menu, .container > .category {{ break-inside: avoid; }}
  .container > .category {{ margin: 0 0 22px; }}
  @media (max-width: 640px) {{ .container {{ column-count: 1; }} }}
</style>
</head>
<body>
{topnav_html}
<div class="container">
  <h1><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.1"/><path d="M19.1 14.4a1.5 1.5 0 0 0 .3 1.7l.1.1a1.9 1.9 0 1 1-2.6 2.6l-.1-.1a1.5 1.5 0 0 0-1.7-.3 1.5 1.5 0 0 0-.9 1.4v.2a1.9 1.9 0 1 1-3.8 0v-.1a1.5 1.5 0 0 0-1-1.4 1.5 1.5 0 0 0-1.7.3l-.1.1a1.9 1.9 0 1 1-2.6-2.6l.1-.1a1.5 1.5 0 0 0 .3-1.7 1.5 1.5 0 0 0-1.4-.9h-.2a1.9 1.9 0 1 1 0-3.8h.1a1.5 1.5 0 0 0 1.4-1 1.5 1.5 0 0 0-.3-1.7l-.1-.1a1.9 1.9 0 1 1 2.6-2.6l.1.1a1.5 1.5 0 0 0 1.7.3h.1a1.5 1.5 0 0 0 .9-1.4v-.2a1.9 1.9 0 1 1 3.8 0v.1a1.5 1.5 0 0 0 .9 1.4 1.5 1.5 0 0 0 1.7-.3l.1-.1a1.9 1.9 0 1 1 2.6 2.6l-.1.1a1.5 1.5 0 0 0-.3 1.7v.1a1.5 1.5 0 0 0 1.4.9h.2a1.9 1.9 0 1 1 0 3.8h-.1a1.5 1.5 0 0 0-1.4.9Z"/></svg> 설정</h1>
  <!-- [수정: 2026-09-18] 정기 / 수시 / 공통 · 수집 / 받는 사람 / 홈 / 연동으로 다시 묶었다.
       정기·수시가 한 화면에 나란히 서므로 두 묶음엔 흐름 이름과 홈 흐름도와 같은 색 띠를 붙인다.
       시안 mockups/SETTINGS_MENU_REORG_MOCKUP.html. -->
  <div class="category flow reg">
    <div class="category-name">정기</div>
    <div class="menu">
      <a href="/keywords">검색어<span class="menu-chev">›</span></a>
      <a href="/schedule">수집시간대<span class="menu-chev">›</span></a>
      <div class="menu-sub">텍스트 표출 형식</div>
      <a href="/format">기사 제목<span class="menu-chev">›</span></a>
      <a href="/subheading-format">소제목<span class="menu-chev">›</span></a>
      <div class="menu-sub">발송 · 알림</div>
      <a href="/auto-send">자동발송 대기시간 및 ON/OFF<span class="menu-chev">›</span></a>
      <a href="/breaking-alert">[단독]·[속보] 기사 알림<span class="menu-chev">›</span></a>
    </div>
  </div>
  <div class="category flow adhoc">
    <div class="category-name">수시</div>
    <div class="menu">
      <div class="menu-sub first">텍스트 표출 형식</div>
      <a href="/format-adhoc">기사 제목<span class="menu-chev">›</span></a>
      <a href="/subheading-format-adhoc">소제목<span class="menu-chev">›</span></a>
    </div>
  </div>
  <div class="category">
    <div class="category-name">정기, 수시 공통</div>
    <div class="menu">
      <a href="/outlets">언론사 선택 및 순서 지정<span class="menu-chev">›</span></a>
    </div>
  </div>
  <div class="category">
    <div class="category-name">받는 사람 지정</div>
    <div class="menu">
      <a href="/telegram">텔레그램<span class="menu-chev">›</span></a>
      <a href="/email">이메일<span class="menu-note">정기 확정본만</span><span class="menu-chev">›</span></a>
      <a href="/send-log">발송 기록<span class="menu-note">누가 받았는지</span><span class="menu-chev">›</span></a>
    </div>
  </div>
  <div class="category">
    <div class="category-name">홈</div>
    <div class="menu">
      <a href="/wordcloud-exclude">워드클라우드 제외어<span class="menu-chev">›</span></a>
    </div>
  </div>
  <!-- [추가: 2026-08-20] "연동"은 일부러 맨 아래에 둔다(사용자 지정 위치) — 검색어
       바로 아래(맨 위)는 이 화면을 처음 여는 사람 눈에 제일 먼저 들어오는 자리라
       배포본을 나눠 받은 사람마다 매번 마주치게 되는데, 키 등록은 설치 시 한 번
       해두면 그 뒤로는 다시 열 일이 거의 없는 화면이다. 자주 여닫는 검색어·형광펜·
       수집 시간 같은 항목들 아래, 가장 손이 안 가는 자리가 맞다. -->
  <div class="category">
    <div class="category-name">연동</div>
    <div class="menu">
      <a href="/naver">네이버 뉴스 API 설정{naver_badge}<span class="menu-chev">›</span></a>
      <a href="/llm">LLM(AI) 연동{llm_badge}<span class="menu-chev">›</span></a>
      <a href="/telegram-sender">텔레그램 발송 계정{telegram_badge}<span class="menu-chev">›</span></a>
      <a href="/email-sender">이메일 발송 계정{email_badge}<span class="menu-chev">›</span></a>
    </div>
  </div>
</div>
</body>
</html>
"""
)

# [수정: 2026-08-10] 이 폼도 시간대 그룹 화면과 같은 이유로 <form>에 onkeydown 가드를
# 단다 — "+ 키워드 추가"/"+ 키워드 그룹 추가"가 "저장"보다 DOM에서 먼저 나오는 submit
# 버튼이라, 그룹명·키워드 입력칸에서 Enter를 치면 저장이 아니라 그 버튼이 암묵적으로
# 눌린다(_SCHEDULE_TEMPLATE 앞 주석에 상세 설명).
# [추가: 2026-08-19] 검색어 화면 전면 개편 — 키워드 추가/삭제/ON-OFF/편집이
# 전부 이 화면 안 메모리(state)에서만 벌어지고 서버 왕복이 없다("+ 키워드 추가"가
# 예전엔 폼 전체를 POST해 페이지를 통째로 다시 그렸다 — 그때마다 스크롤이 맨 위로
# 튀고 화면이 깜빡였다. 게다가 이 화면은 저장 성공 시 아무 확인 메시지도 없었던
# 터라 "깜빡 + 맨 위로"가 이용자가 배운 유일한 저장 신호였고, 저장하지 않는 버튼이
# 그 신호를 그대로 흉내내고 있었다). 이제 화면이 새로 그려지는 건 실제 저장(POST
# /save, 진짜 <form> + 303 리다이렉트 — 이 앱의 다른 저장 화면과 같은 방식)뿐이고,
# 그 전까지의 "저장 안 함" 상태는 하단 저장 바 안내가 계속 알려준다. 이 자바스크립트는
# .format()을 거치는 큰 템플릿 문자열 밖에 별도 상수로 둔다 — JS 자체가 중괄호를
# 수없이 쓰므로 .format() 안에 넣으면 전부 이중 이스케이프({{ }})해야 해서 실수하기
# 쉽다(_CLEAR_FIELD_SCRIPT가 이미 쓰는 방식과 같다). r"""로 감싸 \n 등 JS 자체의
# 이스케이프 시퀀스가 파이썬에 의해 실제 줄바꿈으로 바뀌지 않도록 한다(그러면 JS
# 문자열 리터럴 안에 이스케이프 안 된 줄바꿈이 생겨 문법 오류가 난다).
_KEYWORD_GROUPS_SCRIPT = "<script>\n" + r"""(function () {
  "use strict";
  var INIT = window.__KW_INIT__ || {};
  var MAX_KW = INIT.maxKeywordsPerGroup || 10;
  var MAX_GROUPS = INIT.maxGroups || 8;
  var TRASH_SVG = INIT.trashSvg || "";

  // 서버 저장 형태({keywords, disabled_keywords, ...})를 화면 편집용 형태
  // ({kws:[{w,on}], ...})로 바꾼다. 저장 시 다시 서버 형태로 되돌린다(submitState).
  function toChipGroup(g) {
    var disabled = {};
    (g.disabled_keywords || []).forEach(function (w) { disabled[w] = true; });
    return {
      name: g.name || "",
      live: g.include_in_live !== false,   // 「실시간 현황」
      scrap: !!g.include_in_scrap,          // 「초안·확정본」
      mode: g.mode === "AND" ? "AND" : "OR",
      kws: (g.keywords || []).map(function (w) { return { w: w, on: !disabled[w] }; })
    };
  }

  var state = (INIT.groups || []).map(toChipGroup);
  var dirty = !!INIT.error;
  var lastServerError = INIT.error || "";
  var submitting = false;

  // ====== 저장을 막아야 하는 이유 — app.settings.validate_keyword_groups와 문구를
  // 그대로 맞춘다(서버가 실제로 저장 시점에 검사하는 규칙과 다르면 화면이 거짓말을
  // 하게 된다). 완전히 빈 그룹(이름도 키워드도 없음)은 서버도 조용히 건너뛰므로
  // 여기서도 "시도한 그룹"에서 제외한다.
  function attemptedGroups() {
    return state.filter(function (g) { return g.name.trim() || g.kws.length; });
  }

  function blockingReason() {
    var attempted = attemptedGroups();
    if (!attempted.length) return "키워드 그룹은 최소 1개는 있어야 합니다.";
    for (var i = 0; i < attempted.length; i++) {
      var g = attempted[i];
      if (!g.name.trim()) return "그룹명을 입력해야 합니다.";
      if (!g.kws.length) return '"' + g.name.trim() + '" 그룹에는 키워드를 최소 1개 입력해야 합니다.';
    }
    if (!attempted.some(function (g) { return g.scrap; }))
      return '"초안·확정본"을 켠 그룹이 최소 1개는 있어야 합니다.';
    return "";
  }

  // 다른 그룹과 겹치는 단어들 — 그룹 경계를 넘는 중복만 표시한다(같은 그룹 안
  // 중복은 애초에 addChip에서 못 들어가게 막으므로 여기 나타날 일이 없다).
  function crossGroupDups() {
    var seen = {}, dups = {};
    state.forEach(function (g, gi) {
      var inThis = {};
      g.kws.forEach(function (k) {
        if (inThis[k.w]) return;
        inThis[k.w] = true;
        if (seen[k.w] !== undefined && seen[k.w] !== gi) dups[k.w] = true;
        else if (seen[k.w] === undefined) seen[k.w] = gi;
      });
    });
    return dups;
  }

  function markDirty() { dirty = true; lastServerError = ""; }

  // ====== 저장 바 — 화면에서 저장 신호를 만드는 곳은 여기 하나뿐이다 ======
  function refreshSaveBar() {
    var msg = document.getElementById("kwSaveMsg");
    var txt = document.getElementById("kwSaveMsgText");
    var btn = document.getElementById("kwSaveBtn");
    msg.className = "save-msg";
    btn.className = "";

    var block = blockingReason() || lastServerError;
    if (dirty && block) {
      msg.classList.add("blocked", "show");
      txt.innerHTML = "<b>저장할 수 없습니다.</b> " + block;
      btn.classList.add("blocked");
      return;
    }
    if (dirty) {
      var dups = Object.keys(crossGroupDups());
      msg.classList.add("dirty", "show");
      if (dups.length) {
        txt.innerHTML = "<b>아직 저장하지 않았습니다.</b> 다른 그룹과 겹치는 검색어가 있습니다 — " +
          "노란 테두리가 쳐진 " + dups.map(function (w) { return "<b>" + escapeHtml(w) + "</b>"; }).join(", ") +
          "를 확인해주세요. (겹쳐도 저장은 됩니다)";
      } else {
        // 예전 맨 위 캡션("저장하면 실시간 기사 현황에 바로 반영…")을 여기로 옮겼다 —
        // 저장할 게 있을 때만 보이고, 읽는 자리와 누르는 자리가 같다.
        // [수정: 2026-09-15] "정기 스크랩엔 다음 회차부터"는 초안까지 뭉뚱그렸다 — 초안은
        // 요청마다 다시 그려져 새 검색어로 곧바로 다시 모으고(_preview_keywords_signature),
        // 확정본만 이번 회차 마감 때 모인다.
        txt.innerHTML = "<b>아직 저장하지 않았습니다.</b> 저장하면 전체 기사·초안엔 바로, 확정본엔 이번 회차 마감부터 반영됩니다.";
      }
      return;
    }
    msg.classList.remove("show");
    btn.classList.add("idle");
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ====== 렌더 ======
  function render() {
    var root = document.getElementById("kwGroups");
    root.innerHTML = "";
    var dups = crossGroupDups();
    state.forEach(function (g, gi) { root.appendChild(groupEl(g, gi, dups)); });
    root.querySelectorAll(".group-name-input").forEach(fitNameWidth);
    var addBtn = document.getElementById("kwAddGroupBtn");
    addBtn.disabled = state.length >= MAX_GROUPS;
    refreshSaveBar();
  }

  // [수정: 2026-09-17] 그룹명 칸의 최소 폭 = 글자 폭. 예전엔 5em에서 멈춰 스위치 묶음이
  // 같은 줄에 남고 「공공기관 통폐합」의 끝 글자가 잘렸다. 이름이 다 안 들어가면
  // 스위치 묶음이 아랫줄로 내려간다(.group-head flex-wrap). 입력칸은 글자에 맞춰
  // 스스로 늘지 않으므로 캔버스로 재서 min-width에 넣는다(카드 폭이 상한).
  var _nameCanvas = document.createElement("canvas").getContext("2d");
  function fitNameWidth(input) {
    var cs = getComputedStyle(input);
    _nameCanvas.font = cs.fontWeight + " " + cs.fontSize + " " + cs.fontFamily;
    var text = input.value || input.placeholder;
    var extra = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) +
      parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth) + 4;
    var px = Math.ceil(_nameCanvas.measureText(text).width + extra);
    input.style.minWidth = "min(100%, max(5em, " + px + "px))";
  }

  function groupBlocks() { return document.querySelectorAll(".group-block"); }

  function groupEl(g, gi, dups) {
    var box = document.createElement("div");
    box.className = "group-block" + (inUse(g) ? "" : " off");

    var head = document.createElement("div");
    head.className = "group-head";

    // [수정: 2026-09-15] 그룹 스위치 하나 + 아랫줄 정기 체크 → 머리줄 목적지 스위치 두 개
    // (KEYWORDS_TWO_SWITCH_MOCKUP.html 시안 A). 흐름 순서(실시간 → 초안·확정본)대로 둔다.
    // 둘 다 끄면 그룹 꺼짐 — 따로 끄는 스위치가 없다.
    var dest = document.createElement("span");
    dest.className = "dest-pair";
    dest.appendChild(destSwitch(g, "live", "전체 기사", box,
      "전체 기사 화면에서 이 그룹을 검색합니다.\n언론사 선택·중복 제거 없이 걸린 기사를 전부 보여줍니다."));
    dest.appendChild(destSwitch(g, "scrap", "초안·확정본", box,
      "초안과 확정본(정기 회차)에 이 그룹을 넣습니다.\n저장하면 초안엔 바로, 확정본엔 이번 회차 마감부터 반영됩니다."));

    var nameIn = document.createElement("input");
    nameIn.type = "text";
    nameIn.className = "group-name-input" + (!g.name.trim() && g.kws.length ? " missing" : "");
    nameIn.value = g.name; nameIn.placeholder = "그룹명을 입력하세요";
    nameIn.oninput = function () {
      g.name = nameIn.value; markDirty();
      fitNameWidth(nameIn);
      nameIn.classList.toggle("missing", !g.name.trim() && g.kws.length > 0);
      refreshSaveBar();
    };

    var offTag = document.createElement("span");
    offTag.className = "group-off-tag"; offTag.textContent = "꺼짐";

    var trash = document.createElement("button");
    trash.type = "button";
    trash.className = "group-trash";
    trash.innerHTML = TRASH_SVG;
    trash.title = "이 그룹 지우기";
    trash.setAttribute("aria-label", "이 그룹 지우기");
    trash.onclick = function () {
      var label = g.name.trim() || "이름 없는 그룹";
      if (g.kws.length && !window.confirm(
        '"' + label + '" 그룹을 지웁니다.\n키워드 ' + g.kws.length + '개가 함께 사라집니다.\n\n' +
        '저장을 누르기 전까지는 실제로 지워지지 않습니다.'
      )) return;
      var idx = state.indexOf(g);
      if (idx >= 0) state.splice(idx, 1);
      markDirty(); render();
    };

    // [수정: 2026-09-15] 키워드 수는 머리줄에 목적지 스위치 두 개가 들어오며 아랫줄
    // 오른쪽(검색 방식 맞은편)으로 내려갔다 — 한도(10개)는 이 숫자가 말하므로 화면 위
    // 안내 문단에서 "최대 N개"를 뺀 규칙(2026-09-11)은 그대로다.
    var cnt = document.createElement("span");
    cnt.className = "kw-count" + (g.kws.length >= MAX_KW ? " full" : "");
    var offN = g.kws.filter(function (k) { return !k.on; }).length;
    cnt.textContent = g.kws.length + "/" + MAX_KW + (offN ? " · " + offN + "개 뺌" : "");
    cnt.title = "키워드 " + g.kws.length + "개 (최대 " + MAX_KW + "개)" +
      (offN ? " · 그중 " + offN + "개는 검색에서 잠시 뺐습니다" : "");

    // 스위치 둘과 🗑를 한 묶음으로 — 좁은 화면에서 이름 아래로 내려갈 때 🗑만 따로 떨어지지 않게
    var ctrl = document.createElement("span");
    ctrl.className = "head-ctrl";
    ctrl.appendChild(dest); ctrl.appendChild(trash);
    head.appendChild(nameIn); head.appendChild(offTag); head.appendChild(ctrl);
    box.appendChild(head);

    var body = document.createElement("div");
    body.className = "group-body";

    if (!g.kws.length && g.name.trim()) {
      var warn = document.createElement("div");
      warn.className = "empty-kw";
      warn.textContent = "키워드가 없습니다. 최소 1개는 넣어야 저장됩니다.";
      body.appendChild(warn);
    }

    var chips = document.createElement("div");
    chips.className = "chips";
    g.kws.forEach(function (k, ki) { chips.appendChild(chipEl(g, k, ki, chips, dups)); });
    if (g.kws.length < MAX_KW) chips.appendChild(addChipEl(g, chips));
    body.appendChild(chips);

    box.appendChild(body);

    // 카드 아랫줄 — 검색 방식(작은 두 칸 버튼, "검색 방식" 글자는 툴팁) ↔ 키워드 수.
    // [수정: 2026-09-15] 오른쪽에 있던 「☑ 정기 스크랩」은 머리줄 「초안·확정본」 스위치가
    // 됐다. 그룹이 꺼지면(두 스위치 다 끔) 검색 방식·개수만 CSS가 흐리게 잠근다.
    var foot = document.createElement("div");
    foot.className = "kw-foot";

    var mode = document.createElement("div");
    mode.className = "kw-mode";
    mode.setAttribute("role", "group");
    mode.setAttribute("aria-label", "검색 방식");
    mode.title = "검색 방식 — 키워드 중 1개라도 들어간 기사 / 모두 들어간 기사";
    [["OR", "1개라도 포함"], ["AND", "모두 포함"]].forEach(function (pair) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = pair[1];
      b.dataset.mode = pair[0];
      var on = g.mode === pair[0];
      b.className = on ? "on" : "";
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.onclick = function () {
        if (g.mode === pair[0]) return;
        g.mode = pair[0]; markDirty();
        // 전체를 다시 그리지 않고 이 두 칸만 바꾼다 — 키보드 포커스가 그대로 남는다
        mode.querySelectorAll("button").forEach(function (x) {
          var sel = x.dataset.mode === g.mode;
          x.className = sel ? "on" : "";
          x.setAttribute("aria-pressed", sel ? "true" : "false");
        });
        refreshSaveBar();
      };
      mode.appendChild(b);
    });
    foot.appendChild(mode);
    foot.appendChild(cnt);
    box.appendChild(foot);

    return box;
  }

  function inUse(g) { return g.live || g.scrap; }

  // 목적지 스위치 하나 — 켜진 쪽 이름표가 진해져 카드를 훑기만 해도 어느 그룹이 어디로
  // 가는지 스위치 색과 글자 무게 두 겹으로 보인다. 누를 때 전체를 다시 그리지 않고 이
  // 카드의 표시만 바꾼다 — 키보드 포커스가 스위치에 그대로 남는다(검색 방식 버튼과 같다).
  function destSwitch(g, key, label, box, tip) {
    var sw = document.createElement("label");
    sw.className = "dest-sw" + (g[key] ? " on" : "");
    sw.title = tip;
    sw.appendChild(document.createTextNode(label));
    var cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = g[key];
    cb.onchange = function () {
      g[key] = cb.checked; markDirty();
      sw.classList.toggle("on", g[key]);
      box.classList.toggle("off", !inUse(g));
      refreshSaveBar();
    };
    var pill = document.createElement("span");
    pill.className = "pill";
    sw.appendChild(cb); sw.appendChild(pill);
    return sw;
  }

  function chipEl(g, k, ki, chips, dups) {
    var chip = document.createElement("span");
    chip.className = "chip " + (k.on ? "on" : "off") + (dups[k.w] ? " dup" : "");
    chip.dataset.word = k.w;

    // 버튼 요소를 써야 탭 이동·스크린리더가 인식한다(예전 span+onclick 패턴은 이
    // 앱 다른 곳(라벨 칩 × 등)에도 남아있지만, 새로 쓰는 코드에서 굳이 그대로
    // 반복할 이유가 없다 — 이 화면은 키보드로도 켜고 끌 수 있어야 한다).
    var bodyBtn = document.createElement("button");
    bodyBtn.type = "button";
    bodyBtn.className = "chip-body";
    bodyBtn.textContent = k.w;
    bodyBtn.title = (k.on ? "누르면 검색에서 잠시 뺍니다" : "누르면 다시 검색에 넣습니다") +
      " (두 번 누르면 글자 수정)" + (dups[k.w] ? "\n⚠ 다른 그룹에도 같은 검색어가 있습니다" : "");

    var timer = null;
    bodyBtn.onclick = function () {
      if (timer) return;
      timer = setTimeout(function () { timer = null; k.on = !k.on; markDirty(); render(); }, 220);
    };
    bodyBtn.ondblclick = function () { clearTimeout(timer); timer = null; startEdit(g, k, chip); };

    var x = document.createElement("button");
    x.type = "button";
    x.className = "chip-x"; x.textContent = "×";
    x.title = "이 키워드를 지웁니다";
    x.onclick = function (e) {
      e.stopPropagation();
      var idx = g.kws.indexOf(k);
      if (idx >= 0) g.kws.splice(idx, 1);
      markDirty(); render();
    };

    chip.appendChild(bodyBtn); chip.appendChild(x);
    return chip;
  }

  function startEdit(g, k, chip) {
    var input = document.createElement("input");
    input.className = "chip-edit"; input.value = k.w;
    chip.replaceWith(input);
    input.focus(); input.select();
    function commit() {
      var v = input.value.trim();
      if (v && v !== k.w) { k.w = v; markDirty(); }
      render();
    }
    input.onblur = commit;
    input.onkeydown = function (e) {
      if (e.isComposing || e.keyCode === 229) return;  // 한글 조합 중 Enter는 무시
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") { render(); }
    };
  }

  function addChipEl(g, chips) {
    var wrap = document.createElement("span");
    wrap.className = "chip-add";
    var plus = document.createElement("span");
    plus.className = "plus"; plus.textContent = "+";
    var input = document.createElement("input");
    input.placeholder = "키워드";

    input.onkeydown = function (e) {
      // 한글 IME — 조합 중(받침 치는 중)의 Enter는 조합을 끝내는 Enter라 무시한다.
      // 이 가드가 없으면 Enter 한 번에 핸들러가 두 번 돌거나 조합 중인 글자가 잘린
      // 채로 들어간다. 한글 입력이 기본인 화면이라 필수.
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        var v = input.value.trim().replace(/,$/, "");
        if (!v) return;
        // 같은 그룹 안 중복은 아예 안 넣는다 — 순수한 실수라 저장까지 갈 이유가
        // 없다(app.settings.validate_keyword_groups도 결국 조용히 합치지만,
        // 넣는 시점에 바로 알려주는 게 더 이르다). 기존 칩만 반짝인다.
        var dupInGroup = g.kws.some(function (k) { return k.w === v; });
        if (dupInGroup) { input.value = ""; flashChip(chips, v); return; }
        if (g.kws.length >= MAX_KW) return;
        g.kws.push({ w: v, on: true });
        markDirty(); render();
        var target = groupBlocks()[state.indexOf(g)];
        if (target) {
          var newChip = target.querySelectorAll(".chip")[g.kws.length - 1];
          if (newChip) newChip.classList.add("just-added");
          var nextInput = target.querySelector(".chip-add input");
          if (nextInput) nextInput.focus();
        }
      }
      if (e.key === "Backspace" && input.value === "" && g.kws.length) {
        e.preventDefault();
        g.kws.pop(); markDirty(); render();
        var t2 = groupBlocks()[state.indexOf(g)];
        if (t2) { var ni = t2.querySelector(".chip-add input"); if (ni) ni.focus(); }
      }
    };
    wrap.appendChild(plus); wrap.appendChild(input);
    return wrap;
  }

  function flashChip(chips, word) {
    var found = null;
    chips.querySelectorAll(".chip").forEach(function (c) { if (c.dataset.word === word) found = c; });
    if (!found) return;
    found.classList.remove("just-added");
    void found.offsetWidth;
    found.classList.add("just-added");
  }

  // ====== 그룹 추가 ======
  document.getElementById("kwAddGroupBtn").addEventListener("click", function () {
    if (state.length >= MAX_GROUPS) return;
    state.push({ name: "", live: true, scrap: false, mode: "OR", kws: [] });
    markDirty(); render();
    var blocks = groupBlocks();
    var last = blocks[blocks.length - 1];
    if (last) {
      last.querySelector(".group-name-input").focus();
      last.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  });

  // ====== 저장 — 실제 서버 저장은 여전히 진짜 <form> POST + 303 리다이렉트다
  // (이 앱의 다른 저장 화면들과 같은 방식). 그 전까지의 모든 편집(추가/삭제/토글)은
  // 이 파일 안 메모리(state)에서만 벌어지고 서버 왕복이 없다 — "화면이 새로 그려지는
  // 건 저장뿐"이라는 약속이 이 구분에서 나온다. ======
  function addHidden(form, name, value) {
    var input = document.createElement("input");
    input.type = "hidden"; input.name = name; input.value = value;
    form.appendChild(input);
  }

  function submitState() {
    var form = document.createElement("form");
    form.method = "POST"; form.action = "/save"; form.style.display = "none";
    state.forEach(function (g, i) {
      var gi = i + 1;
      addHidden(form, "group" + gi + "_name", g.name);
      g.kws.forEach(function (k, ki) {
        addHidden(form, "group" + gi + "_keyword" + (ki + 1), k.w);
        if (k.on) addHidden(form, "group" + gi + "_keyword" + (ki + 1) + "_on", "1");
      });
      addHidden(form, "group" + gi + "_mode", g.mode);
      if (g.live) addHidden(form, "group" + gi + "_include_in_live", "1");
      if (g.scrap) addHidden(form, "group" + gi + "_include_in_scrap", "1");
    });
    submitting = true;  // 우리가 지금 막 만든 이 이동은 beforeunload가 막으면 안 된다
    document.body.appendChild(form);
    form.submit();
  }

  document.getElementById("kwSaveBtn").addEventListener("click", function () {
    if (!dirty || blockingReason()) return;
    submitState();
  });

  // 저장 안 한 변경이 있는 채로 나가면(탭 닫기·HOME/설정 링크로 이동 등) 확인창을
  // 띄운다 — 저장 바 안내가 "왜"를 설명하고, 이건 그 순간 실제로 붙잡는 마지막
  // 안전망이다(CODING_CONVENTIONS.md §3, 담당자 작업은 조용히 사라지지 않는다).
  window.addEventListener("beforeunload", function (e) {
    if (dirty && !submitting) { e.preventDefault(); e.returnValue = ""; }
  });

  render();
})();
""" + "\n</script>"

_KEYWORD_GROUPS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>검색어</title>
<style>"""
    + _BASE_STYLE
    + """
  /* [추가: 2026-08-19] 그룹 블록 — 칩 기반 재설계. 예전엔 키워드 한 줄(ON 토글 36px +
     입력칸 220px + 🖍️ 30px + del 46px)이 420px 컨테이너의 대부분을 차지해, 단어 하나
     보여주려고 위젯 4개가 한 줄을 다 잡아먹었다(여백을 아무리 줄여도 20% 이상은 못
     줄임). 칩으로 바꾸면 같은 7개가 세로 350px에서 100px로, 약 3.5배 압축된다. */
  /* [수정: 2026-09-11] 글자 크기에 비해 위아래 여백이 많다는 지적으로 카드 전체를
     촘촘하게 — 카드 안쪽 14·16 → 10·12px, 칩 높이 32 → 25px, 그룹명 칸 34 → 27px,
     카드 아래 세 줄 → 한 줄(.kw-foot). 실측(그룹 6개): 화면 높이 1,228 → 662px.
     시안은 KEYWORDS_COMPACT_MOCKUP.html 시안 A. */
  .group-block {{
    border: 1px solid {border}; border-radius: var(--r-lg); padding: 10px 12px; margin: 14px 0;
    background: {card};
  }}
  .group-block.off {{ background: {surface_off}; }}
  .group-block.off .group-body,
  .group-block.off .kw-mode,
  .group-block.off .kw-count {{ opacity: .5; pointer-events: none; }}
  .group-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin-bottom: 8px; }}
  /* [수정: 2026-09-11] 앞에 .group-head를 붙여 명시도를 올렸다 — 예전엔 _BASE_STYLE의
     공용 input[type=text](회색 배경·테두리, 명시도 0,1,1)가 이 규칙(0,1,0)을 이겨서
     "평소엔 투명, 올리면 테두리"라는 원래 의도 대신 늘 회색 입력 상자로 보였다.
     이제 이름이 입력칸이 아니라 카드 제목처럼 읽힌다. */
  .group-head .group-name-input {{
    font-weight: 700; flex: 1 1 5em; min-width: 5em; background: transparent; color: {header};
    border: 1px solid transparent; border-radius: var(--r-md); padding: 3px 6px; font-size: var(--fs-base);
    font-family: inherit;
  }}
  .group-head .group-name-input:hover {{ border-color: {border}; }}
  .group-head .group-name-input:focus {{ outline: none; border-color: {accent}; background: {bg}; }}
  .group-head .group-name-input.missing {{ border-color: {error}; background: {error_bg}; }}
  .group-head .group-name-input.missing::placeholder {{ color: {error_placeholder}; }}
  /* [수정: 2026-09-11] 맨 위 네 덩어리(캡션 · 계층 상자 · 한도 안내 문단)를 줄였다.
     "최대 N개·최소 1개"는 카드의 "6/10"·잠기는 추가 버튼·저장 바가 말하고, "저장하면
     실시간엔 바로…" 캡션은 저장 바 안내로 옮겼다.
     [수정: 2026-09-15] 남아 있던 첫 줄("켜 둔 그룹은 모두 실시간…그중 ☑정기 스크랩을
     체크한 그룹만…")도 뺐다 — 검색어 계층(실시간 ⊇ 정기)을 풀어 쓰려던 문장인데, 카드
     스위치 이름이 곧 목적지(실시간 / 초안·확정본)가 되면서 풀어 쓸 계층 자체가 없어졌다. */
  .kw-lead {{ color: {muted}; font-size: var(--fs-md); line-height: 1.7; margin: 0 0 14px; }}
  .group-off-tag {{
    display: none; font-size: var(--fs-xs); color: {muted}; background: {bg};
    border-radius: var(--r-pill); padding: 2px 9px;
  }}
  .group-block.off .group-off-tag {{ display: inline-block; }}
  .group-body {{ margin-top: 0; }}

  /* [수정: 2026-08-19] 그룹 스위치가 알약 모양으로 안 그려지던 원인 — .group-enabled-
     toggle .track(48x28 알약)과 .toggle .track(36x24 사각)의 명시도가 같아서 뒤에
     선언된 사각형이 이겼다(손잡이 좌표는 알약 기준이라 9px 밖으로 삐져나갔다). 순서만
     바꾸면 나중에 규칙 하나만 더 붙어도 같은 사고가 재발하므로, .track과 완전히 다른
     이름(.pill)으로 뺀다 — 이름이 안 겹치면 덮일 수가 없다. */
  /* [수정: 2026-09-15] 그룹 스위치(38x22) 하나 → 이름표 달린 목적지 스위치(30x18) 두 개.
     카드 두 장씩 놓는 폭(약 360px)에 이름 · 스위치 둘 · 🗑가 한 줄에 들어가도록 줄였다. */
  .head-ctrl {{ display: inline-flex; align-items: center; gap: 8px; flex-shrink: 0; margin-left: auto; }}
  .dest-pair {{ display: inline-flex; align-items: center; gap: 11px; flex-shrink: 0; }}
  .dest-sw {{ position: relative; display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0;
    cursor: pointer; white-space: nowrap; font-size: var(--fs-sm); color: {muted}; }}
  .dest-sw.on {{ color: {header}; font-weight: 600; }}
  .dest-sw input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .pill {{ position: relative; width: 30px; height: 18px; border-radius: var(--r-pill); background: {toggle_off}; transition: background .16s; flex-shrink: 0; }}
  .pill::after {{
    content: ""; position: absolute; top: 3px; left: 3px; width: 12px; height: 12px;
    border-radius: var(--r-circle); background: {on_fill}; box-shadow: 0 1px 3px rgba(0,0,0,.25); transition: transform .16s;
  }}
  .dest-sw input:checked ~ .pill {{ background: {accent}; }}
  .dest-sw input:checked ~ .pill::after {{ transform: translateX(12px); }}
  .dest-sw input:focus-visible ~ .pill {{ outline: 2px solid {accent}; outline-offset: 2px; }}

  /* 그룹 삭제 = 휴지통, 키워드 삭제 = ×. 크기·무게가 다르면 모양도 다르다(소제목엔
     ▲▼, 기사엔 ↑↓를 쓰는 이 앱의 기존 관례와 같은 논리). */
  .group-trash {{
    background: transparent; border: 1px solid transparent; border-radius: var(--r-md);
    width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center;
    color: {muted}; cursor: pointer; flex-shrink: 0; padding: 0;
  }}
  .group-trash:hover {{ background: {error_bg}; color: {error}; border-color: {error_border}; }}
  .group-trash:focus-visible {{ outline: 2px solid {accent}; outline-offset: 1px; }}

  /* 키워드 칩 — 색은 검색 ON/OFF 딱 하나만 뜻한다(형광펜은 이 화면에서 아예 뺐다).
     OFF는 배경(회색)·테두리(점선) 두 채널로 구분하고 글자를 흐리지 않는다 — "지금
     안 쓰는 키워드"도 목록에서 또렷이 읽혀야 하는 값이기 때문이다. 폭이 ON/OFF
     양쪽에서 같아야 토글할 때 뒤 칩들이 줄을 넘나들며 재배치되지 않는다(글자 라벨
     대신 머리줄 개수 옆 "N개 뺌" 집계로 상태를 알린다). */
  .chips {{ display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }}
  .chip {{ display: inline-flex; align-items: center; border-radius: var(--r-pill); font-size: var(--fs-md); line-height: 1; max-width: 100%; position: relative; }}
  /* .chip-body/.chip-x는 이제 <button>이다(탭 이동·스크린리더가 인식하도록) — UA
     기본 버튼 껍데기(appearance)와 가운데 정렬 텍스트를 지워 예전 <span> 모양 그대로
     보이게 한다(app.renderer의 button 리셋과 같은 이유, _BASE_STYLE 참고). */
  .chip-body {{
    border: none; background: transparent; color: inherit; font: inherit;
    padding: 4px 2px 4px 10px; cursor: pointer; border-radius: var(--r-pill) 0 0 var(--r-pill);
    max-width: 190px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    text-align: left; appearance: none; -webkit-appearance: none;
  }}
  .chip-x {{
    border: none; background: transparent; color: inherit; font-size: .95rem; line-height: 1;
    padding: 4px 8px 4px 4px; cursor: pointer; opacity: .55; border-radius: 0 var(--r-pill) var(--r-pill) 0;
    appearance: none; -webkit-appearance: none;
  }}
  .chip-x:hover {{ opacity: 1; color: {error}; background: rgba(220,38,38,.08); }}
  .chip.on {{ background: {accent_chip_bg}; border: 1px solid {accent}; color: {header}; font-weight: 600; }}
  .chip.on .chip-body:hover {{ background: {accent_chip_hover}; }}
  .chip.off {{ background: {bg}; border: 1px dashed {chip_off_border}; color: {text}; font-weight: 400; }}
  .chip.off .chip-body:hover {{ background: {hover}; }}
  /* 다른 그룹과 겹치는 키워드 — outline은 배경(border)과 다른 층이라 ON/OFF 색을
     그대로 두고 위에 덧씌워진다(막지 않고 알려만 준다 — 그룹마다 OR/AND가 달라 같은
     단어가 의도적으로 여러 그룹에 있을 수 있고, app.naver_api가 이미 키워드·URL
     단위로 중복을 걷어내 검색 비용도 늘지 않는다). */
  .chip.dup {{ outline: 2px solid {warn_dot}; outline-offset: 1px; }}
  .chip.just-added {{ animation: kwpop .55s ease-out; }}
  @keyframes kwpop {{ 0% {{ transform: scale(.85); background: {kwpop_flash}; }} 100% {{ transform: scale(1); }} }}
  .chip-edit {{ border: 1px solid {accent}; background: {card}; border-radius: var(--r-pill); padding: 3px 10px; font: inherit; font-size: var(--fs-md); width: 130px; outline: none; }}
  .chip-add {{ display: inline-flex; align-items: center; gap: 4px; border: 1px dashed {dash_border}; background: {card}; border-radius: var(--r-pill); padding: 3px 10px; color: {muted}; }}
  .chip-add:focus-within {{ border-color: {accent}; border-style: solid; background: {hover}; }}
  .chip-add input {{ border: none; outline: none; background: transparent; font: inherit; font-size: var(--fs-md); width: 72px; color: {text}; padding: 0; }}
  .chip-add input::placeholder {{ color: {placeholder}; }}
  .chip-add .plus {{ font-size: .95rem; color: {accent}; font-weight: 700; }}

  .empty-kw {{ font-size: var(--fs-sm); color: {error}; background: {error_bg}; border: 1px dashed {error_border}; border-radius: var(--r-lg); padding: 8px 12px; margin-bottom: 8px; }}

  .kw-count {{ font-size: var(--fs-sm); color: {muted}; font-variant-numeric: tabular-nums; white-space: nowrap; flex-shrink: 0; }}
  .kw-count.full {{ color: {error}; font-weight: 600; }}

  .kw-foot {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap;
    margin-top: 10px; padding-top: 8px; border-top: 1px solid {border}; }}
  /* 검색 방식 — 라디오 두 개 대신 작은 두 칸 버튼. _BASE_STYLE의 파란 button을 덮는다 */
  .kw-mode {{ display: inline-flex; border: 1px solid {border}; border-radius: var(--r-md); overflow: hidden; }}
  .kw-mode button {{ background: {card}; color: {muted}; border: 0; border-radius: 0;
    font-size: var(--fs-sm); padding: 3px 9px; line-height: 1.4; }}
  .kw-mode button + button {{ border-left: 1px solid {border}; }}
  .kw-mode button:hover {{ background: {hover}; color: {header}; }}
  .kw-mode button.on {{ background: {accent_tonal}; color: {header}; font-weight: 600; }}
  .kw-mode button:focus-visible {{ outline: 2px solid {accent}; outline-offset: -2px; }}

  .add-group-btn {{
    width: 100%; background: {card}; color: {accent}; border: 1px dashed {accent};
    border-radius: var(--r-lg); padding: 9px; font-size: var(--fs-md); font-weight: 600; cursor: pointer;
    font-family: inherit; margin-top: 6px;
  }}
  .add-group-btn:hover {{ background: {hover}; }}
  .add-group-btn:disabled {{ border-color: {border}; color: {muted}; cursor: not-allowed; background: {bg}; }}

  /* [추가: 2026-08-19] 저장 바 안내 — 저장 신호를 만드는 곳을 이 화면에서 하나로
     좁힌다("깜빡 + 맨 위로"가 저장이든 아니든 똑같이 보이던 문제의 해법). 안내가
     저장 버튼 바로 위에 있어 읽는 자리와 누르는 자리가 같다. 3단계: 저장할 수
     없음(빨강, 버튼 잠김) / 저장 안 함(노랑) / 저장됨(초록). */
  .save-msg {{ display: none; align-items: flex-start; gap: 8px; font-size: var(--fs-sm); line-height: 1.5; border-radius: var(--r-md); padding: 9px 11px; margin-bottom: 10px; text-align: left; }}
  .save-msg.show {{ display: flex; }}
  .save-msg .dot {{ width: 8px; height: 8px; border-radius: var(--r-circle); flex-shrink: 0; margin-top: 5px; }}
  .save-msg.dirty {{ background: {warn_bg}; border: 1px solid {warn_border}; color: {warn_text}; }}
  .save-msg.dirty .dot {{ background: {warn_dot}; }}
  .save-msg.blocked {{ background: {error_bg}; border: 1px solid {error_border}; color: {error}; }}
  .save-msg.blocked .dot {{ background: {error}; }}
  .save-msg.saved {{ background: {saved_bg}; border: 1px solid {saved_border}; color: {saved_text}; }}
  .save-msg.saved .dot {{ background: {saved_dot}; }}
  .save-bar button.idle {{ background: {toggle_off}; color: {toggle_off_text}; cursor: default; }}
  .save-bar button.blocked {{ background: {error_disabled}; cursor: not-allowed; }}
  .save-bar button.blocked:hover {{ background: {error_disabled}; }}

  .container {{ padding-bottom: 96px; }}
  /* [추가: 2026-09-11] 그룹 카드를 한 줄에 두 장씩 — 800px 폭에서 카드 한 장이 한 줄을
     다 쓰면 칩이 한 줄에 다 들어가 오른쪽만 비고 스크롤은 그대로였다. 순서는 왼쪽 →
     오른쪽 → 다음 줄. 좁은 화면에선 한 장씩으로 돌아간다. */
  #kwGroups {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: start; }}
  #kwGroups > .group-block {{ margin: 0; }}
  #kwAddGroupBtn {{ margin-top: 12px; }}
  /* [수정: 2026-09-15] 한 장씩으로 돌아가는 폭 640 → 780px — 머리줄에 이름표 달린
     스위치 두 개(약 180px)가 들어오며, 640~780px에선 두 장씩 놓으면 그룹명이 두세
     글자로 잘렸다(실측 648px: 「재정경…」→「재정경」). 그보다 좁아 이름 칸이 5em
     밑으로 떨어지면 스위치 묶음이 이름 아래 줄로 내려간다(.group-head flex-wrap). */
  @media (max-width: 780px) {{ #kwGroups {{ grid-template-columns: minmax(0, 1fr); }} }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg> 검색어</h1>
  <p class="kw-lead">키워드를 누르면 지우지 않고 잠시 빼고, 두 번 누르면 글자를 고칩니다.</p>
  <div id="kwGroups"></div>
  <button type="button" class="add-group-btn" id="kwAddGroupBtn"><span class="plus-glyph">+</span>키워드 그룹 추가</button>
</div>

<div class="save-bar">
  <div class="save-bar-inner">
    <div class="save-msg" id="kwSaveMsg"><span class="dot"></span><span id="kwSaveMsgText"></span></div>
    <button type="button" id="kwSaveBtn">저장</button>
  </div>
</div>

<script>
window.__KW_INIT__ = {{
  maxGroups: {max_groups},
  maxKeywordsPerGroup: {max_keywords_per_group},
  groups: {initial_groups_json},
  error: {initial_error_json},
  trashSvg: {trash_svg_json}
}};
</script>
{keywords_script}
</body>
</html>
"""
)

def _group_to_chip_dict(group: dict) -> dict:
    """저장 형태(name, keywords, disabled_keywords, mode, include_in_live, include_in_scrap)를
    검색어 화면 클라이언트 스크립트(_KEYWORD_GROUPS_SCRIPT)가 바로 읽을 수 있는 편집용
    형태(kws: [{w, on}, ...])로 바꾼다. html.escape를 하지 않는다 — 화면이 이 값을 DOM
    textContent/input.value로만 꽂아 넣고(innerHTML 아님) 저장도 실제 <form> hidden
    input을 거치므로, 이스케이프는 브라우저가 알아서 한다."""
    keywords = group.get("keywords", [])
    disabled = set(group.get("disabled_keywords", []))
    return {
        "name": group.get("name", ""),
        # 옮기기 전 형식이 섞여 와도 옛 규칙으로 읽게 app.settings 헬퍼를 거친다
        "include_in_live": group_in_live(group),
        "include_in_scrap": group_in_scrap(group),
        "mode": "AND" if group.get("mode") == "AND" else "OR",
        "keywords": list(keywords),
        "disabled_keywords": [k for k in keywords if k in disabled],
    }




def _split_hhmm(value: str) -> tuple:
    """"HH:MM" 문자열을 (시, 분) 숫자 문자열 쌍으로 나눈다. 형식이 안 맞으면 ("", "")."""
    parts = value.split(":")
    if len(parts) != 2:
        return "", ""
    try:
        return str(int(parts[0])), str(int(parts[1]))
    except ValueError:
        return "", ""


def _hm_options(value_str: str, values: list) -> str:
    """시·분 <select> 옵션을 만든다. value_str은 _split_hhmm이 돌려주는 "9"/"30" 같은
    비-zero-padded 문자열이거나 빈 문자열(새 그룹의 빈 칸)이다.

    맨 앞에 값이 빈 옵션("--")을 둔다 — 아무 옵션도 selected가 아니면 브라우저가 첫
    옵션을 제출해 버려서, 빈 칸이 "0:00"으로 저장된다. 빈 옵션이 있으면 시·분 둘 다
    빈 문자열이 제출되고 `_combine_hhmm`이 "지우려는 칸"으로 읽는다.
    분은 00·30 둘뿐이다 — 회차 사이가 30분은 돼야 자동발송 유예(최대 30분)와 안 겹친다.
    """
    try:
        selected = int(value_str) if value_str else None
    except ValueError:
        selected = None
    options = [f'<option value=""{" selected" if selected is None else ""}>--</option>']
    options += [
        f'<option value="{v}"{" selected" if selected == int(v) else ""}>{label}</option>' for v, label in values
    ]
    return "".join(options)


_HOUR_VALUES = [(str(h), str(h)) for h in range(24)]
_MINUTE_VALUES = [("00", "00"), ("30", "30")]


def _hm_pair_html(name_prefix: str, value: str) -> str:
    """시 ▾ : 분 ▾ 한 쌍. 이름은 {name_prefix}_h / {name_prefix}_m(서버 `_combine_hhmm`)."""
    h, m = _split_hhmm(value)
    return (
        f'<span class="time-pair">'
        f'<select class="hm-h" name="{name_prefix}_h" aria-label="시">{_hm_options(h, _HOUR_VALUES)}</select>'
        f'<span class="colon">:</span>'
        f'<select class="hm-m" name="{name_prefix}_m" aria-label="분">{_hm_options(m, _MINUTE_VALUES)}</select>'
        f"</span>"
    )


def _render_time_inputs(group_index: int, schedule_times: list, slots: int) -> str:
    """그룹 안 시간대 줄들 — `번호 [시 ▾ : 분 ▾] ~ [시 ▾ : 분 ▾] ×`.

    종료 시각이 회차의 정체성(파일명·머리줄)이고 시작 시각은 수집 하한이다. 시간대마다
    켜고 끄는 체크박스는 없다 — 안 쓰는 시간대는 ×로 지운다(저장 시 enabled는 늘 참).
    """
    rows = []
    for i in range(slots):
        window = schedule_times[i] if i < len(schedule_times) else {}
        prefix = f"group{group_index}"
        rows.append(
            f'<div class="time-row">'
            f'<span class="slot-num">{i + 1}</span>'
            f'{_hm_pair_html(f"{prefix}_start{i + 1}", window.get("start", ""))}'
            f'<span class="tilde">~</span>'
            f'{_hm_pair_html(f"{prefix}_end{i + 1}", window.get("end", ""))}'
            f'<button type="button" class="slot-x" onclick="removeTimeRow(this)" '
            f'title="이 시간대 지우기" aria-label="이 시간대 지우기">{icon("x")}</button>'
            f"</div>"
        )
    return f'<div class="time-rows">{"".join(rows)}</div>'


def _render_schedule_day_checkboxes(group_index: int, days: list) -> str:
    """그룹 하나의 요일 칩 줄(켜고 끄는 알약) — 속은 체크박스라 같은 name이 반복돼
    체크된 값들이 폼에서 리스트로 온다(`form.get(..., [])`)."""
    day_set = set(days or [])
    chips = []
    for key in WEEKDAY_KEYS:
        checked = " checked" if key in day_set else ""
        label = WEEKDAY_LABELS_KO[key]
        chips.append(
            f'<label class="day-chip"><input type="checkbox" name="group{group_index}_days" '
            f'value="{key}"{checked}><span>{label}</span></label>'
        )
    return f'<div class="day-chips">{"".join(chips)}</div>'


def _render_schedule_group_block(
    group_index: int, group: dict, slots: Optional[int] = None, is_today_active: bool = False
) -> str:
    """스크랩 시간대 그룹 하나(예: "주중"/"주말")를 렌더링한다.

    [추가: 2026-07-30, 수정: 2026-08-10] 검색 키워드 그룹 화면과 같은 생김새(그룹명
    입력 + "그룹 삭제" + 그 안의 여러 줄)를 쓴다. 그룹을 "지금 적용 중"으로 켤지는
    더 이상 그룹 전체에서 1개만 고르는 라디오가 아니라, 그룹마다 독립적인 "사용"
    체크박스 + 요일 체크박스 조합으로 정해진다(app.settings.pick_active_group_index) —
    사용 중인 그룹이 1개뿐이면 요일 무관하게 그 그룹이 적용되고(연휴 등 수동
    오버라이드), 2개 이상이면 오늘 요일이 체크된 그룹이 적용된다. is_today_active는
    지금 이 그룹이 그렇게 골라진 상태인지를 나타내며, 배지로만 보여준다(저장 동작에는
    영향 없음).
    """
    name = html.escape(group.get("name", ""))
    times = group.get("times", [])
    if slots is None:
        slots = max(len(times), 1)
    slots = min(max(slots, 1), MAX_SCHEDULE_TIMES)
    enabled_checked = " checked" if group.get("enabled", True) else ""
    add_time_disabled = " disabled" if slots >= MAX_SCHEDULE_TIMES else ""
    today_badge = '<span class="today-active-badge">오늘 적용 중</span>' if is_today_active else ""
    return (
        '<div class="group-block">'
        '<div class="group-head">'
        '<label class="toggle" title="이 그룹 사용">'
        f'<input type="checkbox" name="group{group_index}_enabled"{enabled_checked}>'
        '<span class="track"><span class="toggle-text on">ON</span><span class="toggle-text off">OFF</span></span>'
        "</label>"
        f'<input type="text" class="group-name-input" name="group{group_index}_name" value="{name}" placeholder="그룹명 (예: 주중)">'
        f"{today_badge}"
        f'<button type="button" class="del-btn" onclick="removeGroup(this)" title="이 그룹 지우기">그룹 삭제</button>'
        "</div>"
        + _render_schedule_day_checkboxes(group_index, group.get("days", []))
        + _render_time_inputs(group_index, times, slots)
        + '<p class="add-row add-time-row">'
        f'<button type="submit" formaction="/schedule/add-slot" name="group_index" '
        f'value="{group_index}"{add_time_disabled}><span class="plus-glyph">+</span>시간대 추가</button>'
        "</p>"
        "</div>"
    )


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
    """현재 선택된 언론사를 순서대로, ↑/↓·5↑/5↓ 버튼(SVG 화살표)과 함께 보여준다 (드래그
    앤 드롭 대신 가벼운 방식 — 드래그 정렬은 검토 후 폐기).

    [수정: 2026-07-24] 버튼을 <form> 제출이 아니라 자바스크립트(moveOutlet, 같은 파일의
    _OUTLET_ORDER_SCRIPT)로 처리한다 — 클릭마다 서버에는 그대로 즉시 저장되지만(안전),
    페이지 전체가 새로고침되며 깜빡이던 걸 없애고 그 줄만 화면에서 바로 옮긴다.
    [수정: 2026-07-26] 맨 위/맨 아래 버튼을 없앴다가(가로 공간 절약), 목록이 길면 한
    칸씩 옮기는 게 여전히 번거롭다는 피드백으로 다시 추가했다.
    [수정: 2026-08-13] "맨 위"/"맨 아래"(순간이동)를 "5개 ↑"/"5개 ↓"(app.settings.
    move_outlet의 OUTLET_ORDER_JUMP_STEP)로 바꿨다 — 실측(선택 언론사 41개) 맨 아래→
    맨 위 이동이 1칸(40회)에서 8회로 줄면서도, ↑/↓와 "같은 종류 동작"이라 버튼을
    원색으로 경고할 필요가 없어졌다(사용자 판단). "현재 선택 순서" 안내문은 "선택한
    언론사 (n개)"로 바꿔 현재 개수가 바로 보이게 했다.
    """
    if not order:
        return ""
    rows = []
    last_index = len(order) - 1
    for i, name in enumerate(order):
        escaped = html.escape(name)
        at_top = i == 0
        at_bottom = i == last_index

        def _move_button(direction: str, label: str, title: str, disabled: bool, cls: str = "") -> str:
            return (
                f'<button type="button" class="order-btn{cls}" data-outlet="{escaped}" '
                f'data-direction="{direction}" onclick="moveOutlet(this)" title="{title}" '
                f'aria-label="{escaped} {title}"{" disabled" if disabled else ""}>{label}</button>'
            )

        step = OUTLET_ORDER_JUMP_STEP
        buttons = (
            _move_button("jumpup", f"{step}{icon('up')}", f"{step}개 위로", at_top, " jump jump-up")
            + _move_button("up", icon("up"), "한 칸 위로", at_top)
            + _move_button("down", icon("down"), "한 칸 아래로", at_bottom)
            + _move_button("jumpdown", f"{step}{icon('down')}", f"{step}개 아래로", at_bottom, " jump jump-down")
        )
        rows.append(
            f'<div class="order-row"><span class="order-num">{i + 1}.</span>'
            f'<span class="name">{escaped}</span>{buttons}</div>'
        )
    return f'<div class="order-list"><p class="hint">선택한 언론사 ({len(order)}개)</p>{"".join(rows)}</div>'


# [복구: 2026-08-20] 아래 세 스크립트 상수와 이어지는 설정 화면 템플릿들이 편집 도중
# 통째로 사라져 있었다(참조만 남고 정의가 없어 /outlets·/schedule·/highlight·/format·
# /subheading-format·/scrap-page·/auto-send·/telegram·/email·/wordcloud-exclude 열 화면이
# 전부 NameError로 죽는 상태였다). 커밋 시점 정의를 되살리되, 그 뒤 바뀐 동작
# (언론사 5개 점프 버튼·시간대 그룹 요일·자동발송 통합)에 맞춰 고쳤다.

# [추가: 2026-07-24] 검색 키워드/스크랩 시간대/형광펜 단어 칸 옆 del 버튼을 누르면 그
# 칸(줄) 자체를 화면에서 없앤다 — 서버에는 아직 저장하지 않고(기존과 같은 "저장" 버튼
# 흐름 그대로) 화면에서만 지운다. .keyword-row는 검색 키워드·형광펜 단어 화면이 공유하는
# 클래스, .time-row는 스크랩 시간대 화면.
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

# [추가: 2026-07-24] ↑/↓ 클릭마다 서버에는 즉시 저장하되(안전하게 바로 반영), 페이지
# 전체가 새로고침되며 깜빡이던 걸 없애고 그 줄만 화면에서 옮긴다.
# [수정: 2026-08-13] "맨 위/맨 아래"(순간이동)가 "5개 ↑/5개 ↓"(OUTLET_ORDER_JUMP_STEP)로
# 바뀌면서 방향 값도 top/bottom -> jumpup/jumpdown이 됐다. 목록 끝에서 남은 칸이 5개보다
# 적으면 서버(app.settings.move_outlet)가 끝으로 붙이므로, 화면 쪽도 같은 규칙으로
# 잘라낸다(clamp) — 서버와 화면이 다른 자리를 가리키면 새로고침 때 순서가 튄다.
# _OUTLETS_TEMPLATE은 .format()을 거치므로 이 스크립트는 별도 상수로 두고
# {outlet_order_script} 자리에 값으로만 끼워 넣는다(중괄호 이중화 불필요).
_OUTLET_ORDER_SCRIPT = """<script>
var OUTLET_JUMP_STEP = __JUMP_STEP__;
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
    // 들어있어, list.children를 그대로 쓰면 그 안내문을 기준으로 삼는 사고가 난다.
    var rows = Array.prototype.slice.call(list.querySelectorAll(".order-row"));
    var idx = rows.indexOf(row);
    var step = direction.indexOf("jump") === 0 ? OUTLET_JUMP_STEP : 1;
    var up = direction === "up" || direction === "jumpup";
    var target = up ? idx - step : idx + step;
    if (target < 0) { target = 0; }
    if (target > rows.length - 1) { target = rows.length - 1; }
    if (target === idx) { return; }
    if (target < idx) {
      list.insertBefore(row, rows[target]);
    } else {
      list.insertBefore(row, rows[target].nextSibling);
    }
    renumberOutlets(list);
    // 옮긴 줄을 잠깐 음영 — 기사 순서 변경(.just-moved)과 같은 색·같은 시간.
    row.classList.add("just-moved");
    setTimeout(function () { row.classList.remove("just-moved"); }, 1200);
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
    row.querySelector('[data-direction="jumpup"]').disabled = atTop;
    row.querySelector('[data-direction="up"]').disabled = atTop;
    row.querySelector('[data-direction="down"]').disabled = atBottom;
    row.querySelector('[data-direction="jumpdown"]').disabled = atBottom;
  }
}
</script>""".replace("__JUMP_STEP__", str(OUTLET_ORDER_JUMP_STEP))

# [추가: 2026-07-26] 언론사 목록 전체선택/전체해제/검색/선택된 항목만 보기 — 서버 통신 없이
# 화면에서만 처리한다(체크 상태는 "저장" 눌러야 실제로 반영되는 기존 흐름 그대로).
# 전체 선택/해제는 검색어로 좁혀진(현재 화면에 보이는) 체크박스에만 적용된다.
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


def _theme() -> dict:
    """모든 설정 페이지 템플릿이 공유하는 색상·폰트 값. `.format(**_theme(), ...)`로 채운다."""
    # [수정: 2026-08-21] 색 값은 전부 app.config.PALETTE 하나에서 온다 —
    # ghost_border처럼 다른 화면과 같은 값을 여기 또 적어두던 자리를 없앴다.
    return {
        **PALETTE,
        "font_stack": FONT_STACK,
        # 🖍️ 형광펜 추가 버튼의 활성 상태 음영 — 파란 accent 대신 형광펜 팔레트 1번을
        # 그대로 써서 "이 버튼 = 형광펜에 들어감"이 색으로 바로 연결되게 한다.
        "highlight_active": HIGHLIGHT_COLORS[0],
        "topnav_style": topnav_style(),
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
    # [수정: 2026-08-13] 콜드 캐시(오늘 처음 보는 키워드가 있음)일 땐 이 요청 스레드가
    # 수십~백여 초씩 블로킹되는 대신, generate_live_page_or_wait이 즉시 대기 화면을
    # 돌려주고 실제 검색은 백그라운드 스레드에서 진행한다(app.live_renderer 참고).
    "/live.html": generate_live_page_or_wait,
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


def _credential_badge(source: str) -> str:
    """"연동" 메뉴 항목 옆 상태 배지 — source는 app.credentials.naver_source()/
    llm_source()가 돌려주는 "saved"/"env"/"none" 중 하나. saved(제대로 등록됨)는
    고칠 게 없으므로 배지를 아예 안 그린다. "키가 있지만 인증에 실패하는 중"이라는
    세 번째 상태는 이 메뉴 화면에서는 안 보여준다 — 매번 열 때마다 실제로 API를
    호출해 확인하는 비용을 치를 만한 화면이 아니고(설정 메뉴는 즉시 떠야 한다),
    이미 각 연동 화면 안의 [연결 테스트]와 실시간 현황/초안 상단 배너가 그 신호를
    담당한다."""
    if source == "none":
        return '<span class="badge-warn">미등록</span>'
    if source == "env":
        return '<span class="badge-env">.env</span>'
    return ""


def render_menu_page() -> str:
    """설정 메뉴 화면을 렌더링한다 (PRD.md 기능1 규칙 17)."""
    return _MENU_TEMPLATE.format(
        **_theme(),
        topnav_html=plain_nav(),
        naver_badge=_credential_badge(naver_source()),
        llm_badge=_credential_badge(llm_source()),
        telegram_badge=_credential_badge(telegram_source()),
        email_badge=_credential_badge(email_source()),
    )


def render_keywords_page(settings: dict, error: str = "", groups: Optional[list] = None) -> str:
    """검색 키워드(그룹) 설정 화면을 렌더링한다 (PRD.md 기능1 규칙 15 갱신 — 키워드 그룹).

    [수정: 2026-08-19] 화면을 칩 기반으로 전면 개편하면서, 그룹/키워드 추가·삭제·ON-OFF·
    편집은 전부 클라이언트(_KEYWORD_GROUPS_SCRIPT)가 메모리에서 처리하고 서버 왕복이
    없어졌다 — 그래서 예전에 재렌더링용으로 쓰던 slots_by_group 인자가 사라졌다(더는
    "지금 몇 칸이 보이고 있었는지"를 서버가 알 필요가 없다). groups를 생략하면 저장된
    그룹을 그대로 보여준다. 저장 실패(app.settings.SettingsError) 시에는 호출자가
    지금까지 입력하던(아직 저장 전인) groups를 직접 넘긴다 — 화면은 이걸 "아직 저장
    안 함" 상태로 초기화해(저장 바가 곧바로 실패 사유를 보여준다) 입력을 잃지 않는다.
    """
    display_groups = groups if groups is not None else settings.get("keyword_groups", [])
    initial_groups = [_group_to_chip_dict(g) for g in display_groups]
    trash_svg = icon("trash").replace("</", "<\\/")
    return _KEYWORD_GROUPS_TEMPLATE.format(
        **_theme(),
        max_groups=MAX_KEYWORD_GROUPS,
        max_keywords_per_group=MAX_KEYWORDS_PER_GROUP,
        initial_groups_json=json.dumps(initial_groups, ensure_ascii=False).replace("</", "<\\/"),
        initial_error_json=json.dumps(error, ensure_ascii=False).replace("</", "<\\/"),
        trash_svg_json=json.dumps(trash_svg, ensure_ascii=False),
        keywords_script=_KEYWORD_GROUPS_SCRIPT,
        index_href=_home_href(),
    )


_OUTLETS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>언론사 선택 및 순서 지정</title>
<style>"""
    + _BASE_STYLE
    + """
  /* [수정: 2026-07-26] 아래 "선택한 언론사" 순서 목록이 길어지면 저장 버튼을 찾으려고
     맨 위까지 다시 스크롤해야 하는 문제가 있었다. position:sticky는 뒤에 오는 내용이
     많으면(순서 목록) 스크롤 중 실제로 화면에 붙어있지 않아 의도대로 동작하지 않아,
     화면(뷰포트) 자체에 고정되는 position:fixed 바로 바꿨다. */
  .container {{ padding-bottom: 88px; }}
  .filter-bar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px; }}
  .filter-bar input[type=text] {{ flex: 1; min-width: 140px; }}
  .filter-bar button {{
    padding: 6px 12px; font-size: var(--fs-md); background: {card}; color: {accent}; border: 1px solid {accent};
  }}
  .filter-bar button:hover {{ background: {hover}; }}
  .filter-bar label {{
    font-size: var(--fs-md); color: {muted}; display: flex; align-items: center; gap: 4px; white-space: nowrap;
  }}
  .category {{ margin: 18px 0; }}
  .category.is-hidden {{ display: none; }}
  .category-name {{
    color: {header}; font-size: var(--fs-base); font-weight: 700; margin-bottom: 8px;
    padding-bottom: 4px; border-bottom: 1px solid {border};
  }}
  .category-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 12px; }}
  .category-grid label {{ white-space: nowrap; }}
  .category-grid label.is-hidden {{ display: none; }}
  .order-list {{ margin-top: 16px; }}
  .order-row {{ display: flex; align-items: center; gap: 6px; margin: 4px 0; flex-wrap: wrap; }}
  .order-row .order-num {{ width: 28px; color: {muted}; }}
  .order-row .name {{ width: 140px; }}
  /* ↑↓와 5↑5↓는 걸음 폭만 다른 같은 종류의 동작이라 색·모양을 나누지 않는다. 박스 없이
     SVG 화살표(app.icons up/down)만 두고, 5칸 쪽은 앞의 숫자로 가른다. 두 쌍 사이만 조금 띄운다.
     시안 mockups/OUTLET_ORDER_ARROWS_MOCKUP.html A안. */
  .order-row {{ gap: 2px; }}
  .order-row .order-btn {{
    height: 22px; min-width: 26px; padding: 0 5px; gap: 1px;
    background: transparent; color: {text_soft}; border: none; border-radius: var(--r-md);
    font-size: 0.8rem; font-weight: 600; font-variant-numeric: tabular-nums;
  }}
  .order-row .order-btn .ic {{ width: 17px; height: 17px; stroke-width: 2.2; vertical-align: 0; }}
  .order-row .order-btn:hover {{ background: {hover}; color: {header}; }}
  .order-row .order-btn:disabled {{ background: transparent; color: {order_btn_disabled}; cursor: not-allowed; }}
  .order-row .order-btn.jump-up {{ margin-right: 4px; }}
  .order-row .order-btn.jump-down {{ margin-left: 4px; }}
  /* 옮긴 줄 음영 — 기사 순서 변경(.just-moved)과 같은 색. */
  .order-row.just-moved {{ background: {row_moved}; border-radius: var(--r-sm); }}
  /* [추가: 2026-09-11] 800px 폭에 맞춘 배치 — 체크박스는 두 칸 → 네 칸, 아래 순서
     목록은 두 단(CSS 다단이라 1번부터 위→아래로 읽고, 왼쪽 단이 끝나면 오른쪽 단 맨
     위로 이어진다). 좁은 화면에선 예전 두 칸·한 단으로 돌아간다. */
  .category-grid {{ grid-template-columns: repeat(4, 1fr); }}
  .order-list {{ column-count: 2; column-gap: 32px; }}
  .order-list > .hint {{ column-span: all; }}
  .order-row {{ break-inside: avoid; }}
  @media (max-width: 640px) {{
    .category-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .order-list {{ column-count: 1; }}
  }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📰 언론사 선택 및 순서 지정</h1>
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


# [수정: 2026-09-11] 입력칸 + del 버튼 줄 목록 → 칩 한 상자 + 「오늘 워드클라우드에 뜬
# 단어」에서 눌러 빼기(사용자 결정, 시안 WORDCLOUD_EXCLUDE_MOCKUP.html의 B안). 칩 모양·×·
# 점선 입력칩은 검색어 화면(`/keywords`)과 같고 색만 중립 회색이다 — 그 화면 칩의 파랑은
# "검색 ON"이라는 뜻인데 제외어엔 ON/OFF가 없다. 편집은 전부 화면 메모리에서만 벌어지고
# 저장(POST /save-wordcloud-exclude, 진짜 <form> + 303)을 눌러야 반영된다 — 그 전까지는
# 저장 바 안내가 "아직 저장하지 않았습니다"를 알린다(검색어 화면과 같은 규칙). 예전
# "+ 제외어 추가"(칸을 하나 늘리려고 폼 전체를 POST해 다시 그리던 /wordcloud-exclude/
# add-slot)는 칩 입력으로 대체돼 없앴다.
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
  .container {{ padding-bottom: 120px; }}
  .wc-box {{ border: 1px solid {border}; border-radius: var(--r-lg); padding: 14px; background: {card}; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
  .chip {{
    display: inline-flex; align-items: center; border-radius: var(--r-pill); font-size: var(--fs-md); line-height: 1;
    max-width: 100%; background: {bg}; border: 1px solid {wc_exclude_chip_border}; color: {text};
  }}
  .chip-body {{ padding: 7px 3px 7px 12px; max-width: 190px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  /* _BASE_STYLE의 button 기본값(파란 배경·패딩)을 지운다 — 검색어 화면 .chip-x와 같은 값. */
  .chip-x {{
    border: none; background: transparent; color: inherit; font-size: 1rem; line-height: 1;
    padding: 7px 10px 7px 6px; cursor: pointer; opacity: .55; border-radius: 0 var(--r-pill) var(--r-pill) 0;
  }}
  .chip-x:hover {{ opacity: 1; color: {error}; background: rgba(220,38,38,.08); }}
  .chip.just-added {{ animation: kwpop .55s ease-out; }}
  .chip.bump {{ animation: kwbump .6s ease-out; }}
  @keyframes kwpop {{ 0% {{ transform: scale(.85); background: {kwpop_flash}; }} 100% {{ transform: scale(1); }} }}
  @keyframes kwbump {{
    0%, 100% {{ transform: translateX(0); }}
    20% {{ transform: translateX(-3px); background: {kwpop_flash}; }}
    60% {{ transform: translateX(3px); background: {kwpop_flash}; }}
  }}
  .chip-add {{ display: inline-flex; align-items: center; gap: 4px; border: 1px dashed {dash_border}; background: {card}; border-radius: var(--r-pill); padding: 5px 12px; color: {muted}; }}
  .chip-add:focus-within {{ border-color: {accent}; border-style: solid; background: {hover}; }}
  .chip-add input {{ border: none; outline: none; background: transparent; font: inherit; font-size: var(--fs-md); width: 110px; color: {text}; padding: 0; }}
  .chip-add input::placeholder {{ color: {placeholder}; }}
  .chip-add .plus {{ font-size: .95rem; color: {accent}; font-weight: 700; }}
  .wc-meta {{ display: flex; justify-content: space-between; gap: 10px; margin-top: 12px; font-size: var(--fs-sm); color: {muted}; }}
  .wc-count {{ font-variant-numeric: tabular-nums; }}
  .wc-count.full {{ color: {error}; font-weight: 600; }}

  .wc-sect {{ font-size: var(--fs-sm); font-weight: 700; color: {muted}; margin: 22px 0 9px; }}
  .wc-sect span {{ font-weight: 500; }}
  .sugglist {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .sugg {{
    background: {card}; color: {text}; border: 1px solid {border}; border-radius: var(--r-pill);
    padding: 5px 11px; font-size: var(--fs-sm);
  }}
  .sugg:hover {{ background: {error_bg}; border-color: {error_border}; color: {error}; }}
  .sugg:hover::before {{ content: "− "; }}
  .sugg.kw {{ color: {muted}; }}
  .sugg:disabled {{ opacity: .4; cursor: not-allowed; background: {card}; border-color: {border}; color: {text}; }}
  .sugg:disabled:hover::before {{ content: ""; }}

  .save-msg {{ display: none; align-items: flex-start; gap: 8px; font-size: var(--fs-sm); line-height: 1.5; border-radius: var(--r-md); padding: 9px 11px; margin-bottom: 10px; text-align: left; }}
  .save-msg.show {{ display: flex; }}
  .save-msg .dot {{ width: 8px; height: 8px; border-radius: var(--r-circle); flex-shrink: 0; margin-top: 5px; }}
  .save-msg.dirty {{ background: {warn_bg}; border: 1px solid {warn_border}; color: {warn_text}; }}
  .save-msg.dirty .dot {{ background: {warn_dot}; }}
  .save-bar button.idle {{ background: {toggle_off}; color: {toggle_off_text}; cursor: default; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>☁️ 워드클라우드 제외어</h1>
  <p class="hint">진입 화면 워드클라우드에서 빼고 싶은 단어를 등록하세요. 검색어·형광펜 단어와는 별개입니다.</p>
  {error_html}
  <div class="wc-box">
    <div class="chips" id="wcChips"></div>
    <div class="wc-meta"><span>Enter 또는 쉼표로 추가 · ×로 빼기</span><span class="wc-count" id="wcCount"></span></div>
  </div>
  {suggest_html}
</div>

<form method="POST" action="/save-wordcloud-exclude" id="wcForm">
  <div id="wcHidden"></div>
  <div class="save-bar">
    <div class="save-bar-inner">
      <div class="save-msg" id="wcSaveMsg"><span class="dot"></span><span id="wcSaveMsgText"></span></div>
      <button type="submit" id="wcSaveBtn">저장</button>
    </div>
  </div>
</form>

<script>
window.__WC_INIT__ = {{
  max: {max_exclude},
  show: {show_count},
  words: {words_json},
  saved: {saved_json},
  candidates: {candidates_json}
}};
</script>
{wc_script}
</body>
</html>
"""
)

# 제외어 화면 JS — .format()을 거치는 템플릿 밖에 두는 이유는 _KEYWORD_GROUPS_SCRIPT와 같다.
_WORDCLOUD_EXCLUDE_SCRIPT = "<script>\n" + r"""(function () {
  "use strict";
  var INIT = window.__WC_INIT__ || {};
  var MAX = INIT.max || 15;
  var SHOW = INIT.show || 20;
  var words = (INIT.words || []).slice();
  var savedSnap = (INIT.saved || []).join("\n");
  var CANDS = INIT.candidates || [];   // [단어, 횟수, 검색어인가] — 제외어 없이 뽑은 오늘 순위

  var chipsEl = document.getElementById("wcChips");
  var countEl = document.getElementById("wcCount");
  var suggEl = document.getElementById("wcSugg");

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function has(w) {
    var lw = w.toLowerCase();
    return words.some(function (v) { return v.toLowerCase() === lw; });
  }

  // 홈이 실제로 거르는 규칙(app.tokenizer.tokenize)을 그대로 따른다 — 제외어와 같은 단어,
  // 그리고 "제외어+가" 잔재. 그래서 이 목록이 곧 저장 뒤 홈 워드클라우드다.
  function excludedByChips(w) {
    var set = {};
    words.forEach(function (v) { set[v.toLowerCase()] = true; });
    var lw = w.toLowerCase();
    if (set[lw]) return true;
    return lw.length > 2 && lw.charAt(lw.length - 1) === "가" && set[lw.slice(0, -1)] === true;
  }

  function render(focusInput, addedWord) {
    var h = words.map(function (w) {
      return '<span class="chip' + (w === addedWord ? " just-added" : "") + '" data-word="' + esc(w) + '">' +
        '<span class="chip-body" title="' + esc(w) + '">' + esc(w) + '</span>' +
        '<button type="button" class="chip-x" title="제외어에서 빼기" aria-label="' + esc(w) + ' 빼기">×</button></span>';
    }).join("");
    if (words.length < MAX) {
      h += '<span class="chip-add"><span class="plus">+</span>' +
        '<input type="text" id="wcInput" placeholder="단어 입력" maxlength="30" autocomplete="off"></span>';
    }
    chipsEl.innerHTML = h;
    countEl.textContent = words.length + " / " + MAX + (words.length >= MAX ? " · 가득 참" : "");
    countEl.className = "wc-count" + (words.length >= MAX ? " full" : "");

    chipsEl.querySelectorAll(".chip").forEach(function (chip) {
      chip.querySelector(".chip-x").onclick = function () {
        var w = chip.getAttribute("data-word");
        words = words.filter(function (v) { return v !== w; });
        render(false);
      };
    });
    var inp = document.getElementById("wcInput");
    if (inp) {
      // 한글 조합 중 Enter는 조합이 끝난 뒤에 읽는다 — 안 그러면 Enter를 두 번 눌러야
      // 하거나 마지막 글자가 새 입력칸에 딸려 들어간다. 두 번 불려도 첫 번째가 칸을
      // 비우므로 두 번째는 빈 값이라 아무 일도 안 한다(그래서 늘 "지금" 입력칸을 읽는다).
      inp.onkeydown = function (e) {
        if (e.key !== "Enter" && e.keyCode !== 13) return;
        e.preventDefault();   // 폼 제출(저장) 막기 — Enter는 칩 추가다
        var composing = e.isComposing || e.keyCode === 229;
        setTimeout(function () {
          var cur = document.getElementById("wcInput");
          if (cur && cur.value.trim()) add(cur.value, true);
        }, composing ? 30 : 0);
      };
      inp.oninput = function () { if (inp.value.indexOf(",") >= 0) add(inp.value, true); };
      if (focusInput) inp.focus();
    }
    renderSuggestions();
    refreshSaveBar();
  }

  function renderSuggestions() {
    if (!suggEl) return;
    var full = words.length >= MAX;
    var shown = CANDS.filter(function (c) { return !excludedByChips(c[0]); }).slice(0, SHOW);
    suggEl.innerHTML = shown.map(function (c) {
      return '<button type="button" class="sugg' + (c[2] ? " kw" : "") + '" data-word="' + esc(c[0]) + '"' +
        (full ? " disabled" : "") + (c[2] ? ' title="등록 검색어"' : "") + '>' +
        esc(c[0]) + '</button>';
    }).join("");
    suggEl.querySelectorAll(".sugg").forEach(function (b) {
      b.onclick = function () { add(b.getAttribute("data-word"), false); };
    });
  }

  function add(raw, fromInput) {
    var last = null;
    raw.split(",").map(function (s) { return s.trim(); }).filter(Boolean).forEach(function (w) {
      if (has(w)) { bump(w); return; }   // 같은 단어는 새로 안 만들고 있던 칩을 흔든다
      if (words.length >= MAX) return;
      words.push(w); last = w;
    });
    render(fromInput, last);
  }

  function bump(w) {
    setTimeout(function () {
      var lw = w.toLowerCase();
      chipsEl.querySelectorAll(".chip").forEach(function (c) {
        if (c.getAttribute("data-word").toLowerCase() === lw) {
          c.classList.remove("bump"); void c.offsetWidth; c.classList.add("bump");
        }
      });
    }, 0);
  }

  function refreshSaveBar() {
    var msg = document.getElementById("wcSaveMsg");
    var txt = document.getElementById("wcSaveMsgText");
    var btn = document.getElementById("wcSaveBtn");
    var dirty = words.join("\n") !== savedSnap;
    msg.className = "save-msg" + (dirty ? " dirty show" : "");
    btn.className = dirty ? "" : "idle";
    if (dirty) txt.innerHTML = "<b>아직 저장하지 않았습니다.</b> 아래 저장을 눌러야 홈 워드클라우드에 반영됩니다.";
  }

  document.getElementById("wcForm").addEventListener("submit", function (e) {
    // 입력칸에 쳐두고 Enter를 안 누른 단어도 같이 저장한다 — 치다 만 글자가 저장을
    // 눌렀는데 조용히 사라지면 안 된다.
    var inp = document.getElementById("wcInput");
    if (inp && inp.value.trim()) {
      inp.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean).forEach(function (w) {
        if (!has(w) && words.length < MAX) words.push(w);
      });
    }
    if (words.join("\n") === savedSnap) { e.preventDefault(); return; }
    var box = document.getElementById("wcHidden");
    box.innerHTML = "";
    words.forEach(function (w, i) {
      var h = document.createElement("input");
      h.type = "hidden"; h.name = "exclude" + (i + 1); h.value = w;
      box.appendChild(h);
    });
  });

  render(false);
})();
</script>"""


def _render_wordcloud_suggest_html(candidates: list) -> str:
    """「오늘 워드클라우드에 뜬 단어」 자리. 오늘 회차가 아직 없어 뽑을 게 없으면 통째로 안 그린다."""
    if not candidates:
        return ""
    return (
        '<div class="wc-sect">오늘 워드클라우드에 뜬 단어 '
        "<span>— 누르면 위로 올라가요</span></div>"
        '<div class="sugglist" id="wcSugg"></div>'
    )


def render_wordcloud_exclude_page(settings: dict, error: str = "") -> str:
    """워드클라우드 제외어 설정 화면을 렌더링한다.

    error가 있으면(저장 실패) settings의 wordcloud_exclude_words는 담당자가 방금 저장하려던
    값이다 — 그 값을 칩으로 되살려 두고, "저장된 값"은 파일에서 다시 읽어 저장 바가
    "아직 저장하지 않았습니다"를 계속 띄우게 한다.
    """
    words = settings.get("wordcloud_exclude_words", [])
    saved = load_settings().get("wordcloud_exclude_words", []) if error else words
    candidates = wordcloud_candidates(settings, extra=2 * MAX_WORDCLOUD_EXCLUDE_WORDS)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""

    def _js(value) -> str:
        return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")

    return _WORDCLOUD_EXCLUDE_TEMPLATE.format(
        **_theme(),
        max_exclude=MAX_WORDCLOUD_EXCLUDE_WORDS,
        show_count=LANDING_KEYWORD_COUNT,
        error_html=error_html,
        suggest_html=_render_wordcloud_suggest_html(candidates),
        words_json=_js(words),
        saved_json=_js(saved),
        candidates_json=_js([list(c) for c in candidates]),
        wc_script=_WORDCLOUD_EXCLUDE_SCRIPT,
        index_href=_home_href(),
    )


_SCHEDULE_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>수집시간대</title>
<style>"""
    + _BASE_STYLE
    + """
  /* 시간대 줄 = 번호 [시 ▾ : 분 ▾] ~ [시 ▾ : 분 ▾] ×. 드롭다운은 기본 껍데기를 벗기고
     화살표를 배경으로 그려 사파리에서도 높이(36px)가 맞게 한다. */
  .time-rows {{ display: flex; flex-direction: column; gap: 6px; }}
  .time-row {{ display: flex; align-items: center; gap: 6px; }}
  .slot-num {{ width: 18px; flex: none; text-align: right; font-size: var(--fs-sm); color: {text_faint};
    font-variant-numeric: tabular-nums; }}
  .time-pair {{ display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }}
  .time-pair .colon {{ color: {muted}; margin: 0 -2px; }}
  .time-row .tilde {{ color: {text_faint}; }}
  .time-row select {{
    appearance: none; -webkit-appearance: none; width: 64px; height: var(--h-lg);
    padding: 0 24px 0 12px; font-size: var(--fs-base); font-family: inherit; font-variant-numeric: tabular-nums;
    color: {text}; background-color: {bg}; border: 1px solid {border}; border-radius: var(--r-md); cursor: pointer;
    background-image: {select_chevron}; background-repeat: no-repeat; background-position: right 8px center;
    background-size: 14px;
  }}
  .time-row select:hover {{ border-color: {accent_border}; }}
  .time-row select:focus {{ outline: none; border-color: {accent}; box-shadow: 0 0 0 3px {accent_border}; }}
  .slot-x {{
    margin-left: auto; width: 28px; height: 28px; padding: 0; flex: none;
    background: transparent; color: {text_faint}; border: none; border-radius: var(--r-sm); font-size: var(--fs-base);
  }}
  .slot-x:hover {{ background: {error_bg}; color: {error}; }}
  /* 줄이 하나뿐이면 지울 수 없다(그룹당 최소 1개) — 자리는 지킨다. */
  .time-rows:has(> .time-row:only-child) .slot-x {{ visibility: hidden; }}
  @media (max-width: 400px) {{ .time-row select {{ width: 58px; padding-left: 9px; }} }}
  /* [추가: 2026-07-30] 스크랩 시간대 "그룹"(주중/주말 등) — 검색어 그룹 화면과 같은
     생김새(그룹 카드 안에 여러 줄). group-block류는 그 화면의 로컬 스타일이라 여기
     다시 정의한다. */
  .group-block {{ border: 1px solid {border}; border-radius: var(--r-md); padding: 14px; margin: 14px 0; }}
  .group-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }}
  .group-name-input {{ font-weight: 600; flex: 1; min-width: 70px; }}
  .group-head .del-btn {{ flex: none; white-space: nowrap; }}
  /* [추가: 2026-08-10] 그룹을 켜고 끄는 ON/OFF 토글 — 텔레그램·이메일 받는 사람 줄과
     같은 모양(같은 뜻: 지우지 않고 잠깐 빼둔다). */
  .toggle {{ position: relative; display: inline-flex; align-items: center; flex-shrink: 0; cursor: pointer; }}
  .toggle input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .toggle .track {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 36px; height: 24px; background: {border}; border-radius: var(--r-sm); transition: background 0.15s;
  }}
  .toggle input:checked ~ .track {{ background: {accent}; }}
  .toggle-text {{ font-size: 0.68rem; font-weight: 700; }}
  .toggle-text.on {{ display: none; color: {on_fill}; }}
  .toggle-text.off {{ display: inline; color: {muted}; }}
  .toggle input:checked ~ .track .toggle-text.on {{ display: inline; }}
  .toggle input:checked ~ .track .toggle-text.off {{ display: none; }}
  /* 요일 = 켜고 끄는 알약 칩(속은 체크박스). 켜짐 파란 실선, 꺼짐 회색 점선. 어느 그룹이
     오늘 쓰이는지는 app.settings.pick_active_group_index가 정하고 배지로만 알린다. */
  .day-chips {{ display: flex; flex-wrap: wrap; gap: 5px; margin: 0 0 12px; }}
  .day-chip {{ position: relative; cursor: pointer; }}
  .day-chip input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .day-chip span {{
    display: inline-flex; align-items: center; justify-content: center; width: 34px; height: var(--h-md);
    border: 1px dashed {chip_off_border}; border-radius: var(--r-pill); background: {card}; color: {text_faint};
    font-size: var(--fs-md); font-weight: 600; user-select: none;
  }}
  .day-chip input:checked + span {{ border: 1px solid {accent}; background: {hover}; color: {accent}; }}
  .day-chip input:focus-visible + span {{ outline: 2px solid {accent_border}; outline-offset: 1px; }}
  .today-active-badge {{
    flex-shrink: 0; font-size: var(--fs-xs); font-weight: 700; color: {accent};
    background: {hover}; border-radius: var(--r-pill); padding: 3px 9px; white-space: nowrap;
  }}
  .add-row button {{ background: {card}; color: {accent}; border: 1px solid {accent}; }}
  .add-row button:hover {{ background: {hover}; }}
  .add-row button:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
  .add-time-row {{ margin: 10px 0 0; }}
  .container {{ padding-bottom: 88px; }}
  /* [추가: 2026-09-11] 800px 폭에 맞춰 "주중"/"주말" 같은 그룹 카드를 나란히 두 장씩
     — 한 장이 한 줄을 다 쓰면 시간대 줄 오른쪽이 통째로 비었다. "+ 그룹 추가"는 두 칸을
     다 쓴다. 좁은 화면에선 한 장씩으로 돌아간다. */
  /* 1fr이 아니라 minmax(0, 1fr)인 이유: 1fr의 최소값은 내용물의 최소 폭이라, 좁은
     화면에서 그룹 카드가 칸을 밀어내 화면이 옆으로 스크롤됐다(375px에서 실측). */
  .schedule-form {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 16px; align-items: start; }}
  .schedule-form > .add-row {{ grid-column: 1 / -1; }}
  @media (max-width: 640px) {{ .schedule-form {{ grid-template-columns: minmax(0, 1fr); }} }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>⏰ 수집시간대</h1>
  <p class="hint">
    회차마다 시작~종료 시각을 고르세요. 그 사이에 게시된 기사가 그 회차에 모이고, 종료
    시각이 확정본 머리줄 「언론 모니터링 N시 기준」이 됩니다. 그룹당 {min_times}~{max_times}개,
    안 쓰는 시간대는 ×로 지웁니다.<br>
    요일을 켜 둔 그룹이 그날 쓰이고(그룹은 최대 {max_groups}개), 켜 둔 그룹이 하나뿐이면
    요일과 상관없이 그 그룹이 쓰입니다 — 연휴처럼 예외인 날 그룹 하나만 켜 두는 용도입니다.
  </p>
  {error_html}
  <form method="POST" action="/save-schedule" class="schedule-form">
    {group_blocks}
    <p class="add-row"><button type="submit" formaction="/schedule/add-group"{add_group_disabled}><span class="plus-glyph">+</span>그룹 추가</button></p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{clear_script}
{schedule_script}
</body>
</html>
"""
)


def _render_schedule_group_blocks(groups: list, slots_by_group: Optional[dict] = None) -> str:
    slots_by_group = slots_by_group or {}
    today_active_index = pick_active_group_index(groups)
    return "\n".join(
        _render_schedule_group_block(i + 1, group, slots_by_group.get(i + 1), i == today_active_index)
        for i, group in enumerate(groups)
    )


def render_schedule_page(
    settings: dict, error: str = "", groups: Optional[list] = None, slots_by_group: Optional[dict] = None
) -> str:
    """스크랩 시간대(그룹) 설정 화면을 렌더링한다 (PRD.md 기능1 규칙 20).

    groups를 생략하면 저장된 그룹을 그대로 보여준다. "+ 그룹 추가"/"+ 시간대 추가"·
    저장 실패 시에는 호출하는 쪽이 지금까지 입력하던(아직 저장 전인) groups·
    slots_by_group을 직접 넘긴다(검색 키워드 그룹 화면과 같은 패턴).

    그룹마다 저장된 시간대 수만큼만 줄을 그린다(빈 줄을 미리 깔아 두지 않는다).
    """
    display_groups = groups if groups is not None else settings.get("schedule_groups", [])
    if slots_by_group is None:
        slots_by_group = {
            i + 1: max(len(g.get("times", [])), 1)
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
        schedule_script=_SCHEDULE_SCRIPT,
        select_chevron=_select_chevron_css(),
        index_href=_home_href(),
    )


def _select_chevron_css() -> str:
    """드롭다운 오른쪽 ▾를 배경 그림으로(색은 PALETTE의 muted)."""
    color = PALETTE["muted"].replace("#", "%23")
    svg = (
        "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
        f"stroke='{color}' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E"
        "%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E"
    )
    return f'url("data:image/svg+xml,{svg}")'


# 시간대 줄 × — 줄을 지우고 남은 줄 번호를 1부터 다시 매긴다(저장은 「저장」에서만).
_SCHEDULE_SCRIPT = """<script>
function removeTimeRow(btn) {
  var list = btn.closest(".time-rows");
  var row = btn.closest(".time-row");
  if (!list || !row || list.children.length <= 1) return;
  row.remove();
  list.querySelectorAll(".slot-num").forEach(function (el, i) { el.textContent = i + 1; });
}
</script>"""


_FORMAT_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{flow_name} 기사 제목 형식</title>
<style>"""
    + _BASE_STYLE
    + """
  input[type=text] {{ width: 100%; max-width: 360px; box-sizing: border-box; }}
  .preview {{ margin-top: 12px; color: {muted}; }}
  .preview code {{ display: inline-block; margin: 2px 0; }}
  code {{ background: {bg}; border: 1px solid {border}; padding: 2px 6px; border-radius: var(--r-sm); color: {text}; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📃 {flow_name} · 기사 제목 형식</h1>
  <p class="hint">
    {flow_scope}의 복사·txt·발송 텍스트에서 기사 첫 줄의 형식을 바꿀 수 있습니다.<br>
    <code>{{outlet}}</code>은 언론사명, <code>{{title}}</code>은 기사 제목으로 바뀝니다.<br>
    ※ 두 자리표시자를 각각 정확히 1번씩 포함해야 합니다. 아래 URL 줄과 "발행일 미표시"는
    이 설정과 무관하게 항상 고정입니다.
  </p>
  {error_html}
  <form method="POST" action="{save_path}">
    <input type="text" name="line_template" value="{template_value}">
    <p class="preview">미리보기:<br><code>{preview}</code><br><code>{preview_url}</code></p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
</body>
</html>
"""
)


# [추가: 2026-09-18] 보고서 형식(기사 제목·소제목)은 정기·수시가 따로 가진다. 두 화면은
# 같은 템플릿을 쓰고 저장 키·주소·안내 문구만 이 표로 가른다.
_FORMAT_FLOWS = {
    "regular": {
        "name": "정기", "scope": "정기 초안·확정본·보관함",
        "sub_where": "화면·복사·내보내기",
        "line_key": "article_line_template", "sub_key": "subheading_format_template",
        "line_path": "/format", "sub_path": "/subheading-format",
    },
    "adhoc": {
        "name": "수시", "scope": "수시 확정본·보관함",
        "sub_where": "복사·txt·발송 텍스트",
        "line_key": "adhoc_article_line_template", "sub_key": "adhoc_subheading_format_template",
        "line_path": "/format-adhoc", "sub_path": "/subheading-format-adhoc",
    },
}


def render_format_page(settings: dict, error: str = "", flow: str = "regular") -> str:
    """기사 제목 형식 설정 화면을 렌더링한다 (PRD.md 기능1 규칙 5). flow는 "regular"/"adhoc"."""
    spec = _FORMAT_FLOWS[flow]
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    template = settings.get(spec["line_key"], "")
    # 미리보기는 실제로 저장하기 전에 결과를 보여주는 용도라, 저장 여부와 무관하게
    # 항상 예시 언론사/제목으로 렌더링해본다 (템플릿이 잘못돼 있어도 여기선 안전한
    # apply_line_template의 단순 치환이라 오류가 날 일이 없다).
    preview = apply_line_template(html.escape(template), "KBS", "예시 기사 제목입니다")
    # [추가: 2026-08-13] URL 줄은 이 템플릿과 무관하게 항상 고정이라 apply_line_template을
    # 거치지 않는다 — 실제 (복사)·(다운로드) 결과와 똑같이 예시 URL을 그대로 보여준다.
    preview_url = "https://n.news.naver.com/mnews/article/000/0000000000"
    return _FORMAT_TEMPLATE.format(
        **_theme(),
        flow_name=spec["name"],
        flow_scope=spec["scope"],
        save_path="/save-" + spec["line_path"].lstrip("/"),
        error_html=error_html,
        template_value=html.escape(template),
        preview=preview,
        preview_url=preview_url,
        index_href=_home_href(),
    )


# [복구/신규: 2026-08-20] 소제목 형식 화면 — 기사 제목 형식(_FORMAT_TEMPLATE)과 같은
# 모양·같은 흐름이고, 자리표시자만 {{section}} 하나다. 커밋 전 정의가 없어(이 화면은
# 커밋 이후 추가된 기능) 같은 패턴으로 새로 썼다.
_SUBHEADING_FORMAT_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{flow_name} 소제목 형식</title>
<style>"""
    + _BASE_STYLE
    + """
  input[type=text] {{ width: 100%; max-width: 360px; box-sizing: border-box; }}
  .preview {{ margin-top: 12px; color: {muted}; }}
  code {{ background: {bg}; border: 1px solid {border}; padding: 2px 6px; border-radius: var(--r-sm); color: {text}; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📃 {flow_name} · 소제목 형식</h1>
  <p class="hint">
    {flow_scope}의 소제목을 {sub_where}에 보여줄 형식을 바꿀 수 있습니다.<br>
    <code>{{section}}</code>이 소제목 이름으로 바뀝니다 (기본값 <code>&lt;{{section}}&gt;</code>).<br>
    ※ <code>{{section}}</code>을 정확히 1번 포함해야 합니다. 직접 고친 소제목 이름에는
    앱이 괄호를 덧붙이지 않고 쓴 그대로를 씁니다.
  </p>
  {error_html}
  <form method="POST" action="{save_path}">
    <input type="text" name="subheading_format" value="{template_value}">
    <p class="preview">미리보기: <code>{preview}</code></p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
</body>
</html>
"""
)


def render_subheading_format_page(settings: dict, error: str = "", flow: str = "regular") -> str:
    """소제목 형식 설정 화면을 렌더링한다 (보고서 형식 두 번째 항목, render_format_page와 동일 방식)."""
    spec = _FORMAT_FLOWS[flow]
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    template = settings.get(spec["sub_key"], "")
    preview = apply_subheading_format(html.escape(template), "부동산 세제 개편 관련")
    return _SUBHEADING_FORMAT_TEMPLATE.format(
        **_theme(),
        flow_name=spec["name"],
        flow_scope=spec["scope"],
        sub_where=spec["sub_where"],
        save_path="/save-" + spec["sub_path"].lstrip("/"),
        error_html=error_html,
        template_value=html.escape(template),
        preview=preview,
        index_href=_home_href(),
    )


# [복구/신규: 2026-08-20] 자동발송 설정 화면 — 채널별(/telegram·/email)로 흩어져 있던
# "자동 전송" 체크박스를 여기 하나로 통합한 뒤 만들어진 화면인데(커밋 이후) 정의가
# 없어 새로 썼다. 유예 시간 상한은 app.config.MAX_AUTO_SEND_GRACE_MIN — 회차 간격보다
# 길면 이전 회차가 조용히 묻힌다(app.settings.save_auto_send_settings 주석 참고).
_AUTO_SEND_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>자동발송 대기시간 및 ON/OFF</title>
<style>"""
    + _BASE_STYLE
    + """
  .checkbox-row {{ margin: 16px 0; display: flex; align-items: center; gap: 8px; }}
  .checkbox-row label {{ font-size: var(--fs-base); }}
  .grace-row {{ margin: 16px 0 6px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .grace-row input[type=number] {{ width: 70px; text-align: center; }}
  .grace-row label {{ font-size: var(--fs-base); }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>⏱️ 자동발송 대기시간 및 ON/OFF</h1>
  <p class="hint">
    회차 수집이 끝난 뒤 아래 시간 안에 확정본에서 <b>(발송)</b>을 누르지 않으면, 앱이
    대신 발송합니다(텔레그램·이메일 중 설정된 채널로). 탭을 닫아둬도 실행됩니다.<br>
    끄면 카운트다운도 안 뜨고 아무것도 자동으로 나가지 않습니다 — 발송은 (발송) 버튼을
    누를 때만 일어납니다. AI 분류가 실패한 회차는 이 설정과 별개로 자동발송을 건너뛰고
    직접 확인하도록 안내합니다.
  </p>
  {error_html}
  <form method="POST" action="/save-auto-send">
    <div class="checkbox-row">
      <input type="checkbox" id="auto_send_enabled" name="auto_send_enabled" value="1"{enabled_checked}>
      <label for="auto_send_enabled">자동발송 사용</label>
    </div>
    <p class="grace-row">
      <label for="auto_send_grace_min">기다리는 시간</label>
      <input type="number" id="auto_send_grace_min" name="auto_send_grace_min" min="1" max="{max_grace}" value="{grace_min}">
      <span>분 (1~{max_grace}분)</span>
    </p>
    <p class="caption">회차 사이 간격보다 짧게 두세요 — 길면 앞 회차가 발송되기 전에 다음 회차가 시작됩니다.</p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
</body>
</html>
"""
)


def render_auto_send_settings(settings: dict, error: str = "", grace_min: Optional[str] = None) -> str:
    """자동발송 설정 화면 — 사용 여부와 유예 시간(분)을 한 곳에서 다룬다.

    [추가: 2026-08-11] 예전엔 `/telegram`·`/email`에 채널별 "정기 스크랩 완료 시 자동 전송"
    체크박스가 따로 있었는데, 발송 경로가 그 값을 읽지 않아 **꺼놔도 그냥 나가는** 고아
    설정이었다. 확정본의 개별 Telegram/Email 버튼을 (발송) 하나로 합친 것과 같은 이유로
    자동발송도 채널별로 쪼개지 않고 여기 하나로 통합했다.
    """
    # 검증 실패 시엔 저장된 값이 아니라 담당자가 방금 입력한 값을 그대로 다시 보여준다
    # (다른 설정 화면과 같은 방식) — 안 그러면 무엇을 잘못 썼는지 화면에서 사라진다.
    shown_grace = grace_min if grace_min is not None else settings.get(
        "auto_send_grace_min", DEFAULT_AUTO_SEND_GRACE_MIN
    )
    return _AUTO_SEND_SETTINGS_TEMPLATE.format(
        **_theme(),
        error_html=f'<p class="error">{html.escape(error)}</p>' if error else "",
        enabled_checked=" checked" if is_auto_send_enabled(settings) else "",
        grace_min=html.escape(str(shown_grace)),
        max_grace=MAX_AUTO_SEND_GRACE_MIN,
        index_href=_home_href(),
    )


# [추가: 2026-08-03] 텔레그램 전송 설정 화면. .env에 봇 토큰이 없으면 켜도 조용히
# 건너뛰므로(app.telegram_bot.send_text), 그 상태를 status로 미리 알려줘 "보냈는데 왜
# 안 오지"를 막는다.
# [수정: 2026-09-17] 받는 사람 한 줄 = 이름 · chat id · [단독] · [속보] · 정기(기사|요약) ·
# 수시(기사|요약) · 알림(울리는 시간). 꺼짐은 점선, 켜짐은 실선 칩이고 누르면 바로 바뀐다.
# 시안 mockups/TELEGRAM_RECIPIENTS_MOCKUP.html.

# 화면 설명글 — 머리글·칸 말풍선과 알림 창 안 문구. 문구를 고칠 땐 여기만 고친다.
_TELEGRAM_TIPS = {
    "h_alert": (
        "[단독]·[속보] 즉시 알림",
        "제목에 말머리가 붙은 기사를 감지되는 대로 바로 보냅니다. 회차와 상관없이 해당 기사 1~3건만 갑니다.",
        "감시 시간대·주기·검색어 그룹은 「[단독]·[속보] 기사 알림」에서 정합니다.",
    ),
    "h_reg": (
        "정기 보고서",
        "회차가 마감되면 만들어지는 정기 확정본입니다. 소제목별로 묶인 그 회차 전체가 나갑니다.",
        "직접 (발송)을 누르지 않아도 유예 시간이 지나면 자동으로 나갑니다 — 시간은 「자동발송 대기시간 및 ON/OFF」에서.",
    ),
    "h_adh": (
        "수시 보고서",
        "수시 확정본에서 (발송)을 누르면 나갑니다. 사안 하나를 따로 모아 만든 보고서입니다.",
        "수시엔 자동발송이 없습니다. 누를 때 이 명단이 확인창에 뜨고, 그 한 번만 뺄 수 있습니다.",
    ),
    "h_notify": (
        "알림이 울리는 시간",
        "고른 시간에만 소리·진동으로 알립니다. 그 밖의 시간엔 메시지가 알림 없이 조용히 옵니다 — 내용은 그대로 받습니다.",
        "[단독]·[속보]·정기·수시 모두에 적용됩니다. 텔레그램에만 해당합니다.",
    ),
    "c_scoop": ("[단독] 알림 받기", "제목이 [단독]으로 시작하는 기사를 이 사람에게 즉시 보냅니다.", ""),
    "c_flash": ("[속보] 알림 받기", "제목이 [속보]로 시작하는 기사를 이 사람에게 즉시 보냅니다.", ""),
    "reg_a": ("정기 — 기사 목록", "그 회차의 기사 목록 전체를 보냅니다. 소제목·언론사·제목·링크가 화면 그대로 들어갑니다.", ""),
    "reg_s": (
        "정기 — 요약",
        "소제목마다 3문장 이내로 AI가 쓴 요약만 보냅니다. 기사 목록은 들어가지 않습니다.",
        "「기사」와 같이 켜면 한 통에 기사 목록 + 그 아래 요약이 갑니다. AI 분류가 실패한 회차는 기사 목록도 같이 갑니다.",
    ),
    "adh_a": ("수시 — 기사 목록", "그 수시 확정본의 기사 목록 전체를 보냅니다. 사안명이 보고서 첫 줄에 들어갑니다.", ""),
    "adh_s": (
        "수시 — 요약",
        "소제목마다 3문장 이내로 AI가 쓴 요약만 보냅니다. 기사 목록은 들어가지 않습니다.",
        "「기사」와 같이 켜면 한 통에 기사 목록 + 그 아래 요약이 갑니다. 요약이 없는 확정본은 기사 목록이 갑니다.",
    ),
    "c_notify_foot": "그 밖의 시간엔 조용히 옵니다. 누르면 바꿀 수 있습니다.",
    # 알림 창 안
    "p_title": "{name} — 알림 받는 시간",
    "p_sub": "고른 시간에만 알림이 울리고, 나머지 시간엔 조용히 옵니다.",
    "p_always": "하루 종일",
    "p_always_sub": "언제 보내든 알림이 울립니다.",
    "p_work": "업무 시간",
    "p_work_sub": "월~금 09:00 ~ 18:00",
    "p_custom": "직접 설정",
    "p_custom_sub": "요일과 시간을 고릅니다.",
    "p_daily": "매일",
    "p_weekday": "월~금",
    "p_foot": "평일인 공휴일에도 울립니다.",
    "p_apply": "적용",
}


def _tip_html(key: str, right: bool = False, body: Optional[str] = None) -> str:
    """말풍선 한 개. body를 주면 그 글자로 본문을 갈아 끼운다(알림 칸처럼 값에 따라 바뀌는 곳)."""
    title, default_body, foot = _TELEGRAM_TIPS[key]
    return (
        f'<span class="tip{" r" if right else ""}"><b>{html.escape(title)}</b>'
        f'{html.escape(default_body if body is None else body)}'
        f'{f"<em>{html.escape(foot)}</em>" if foot else ""}</span>'
    )


def _notify_tip_html(notify: dict) -> str:
    foot = "" if notify.get("mode") == "always" else _TELEGRAM_TIPS["c_notify_foot"]
    return (
        f'<span class="tip r"><b>알림이 울리는 시간</b>{html.escape(notify_sentence(notify))}'
        f'{f"<em>{html.escape(foot)}</em>" if foot else ""}</span>'
    )


_TELEGRAM_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>텔레그램 받는 사람</title>
<style>"""
    + _BASE_STYLE
    + """
  .status {{ margin: 4px 0 20px; padding: 10px 14px; border-radius: var(--r-md); font-size: var(--fs-md); }}
  .status-ok {{ background: {hover}; color: {accent}; }}
  .status-warn {{ background: {error_bg}; color: {error}; }}
  .help-box {{ background: {hover}; border-radius: var(--r-md); padding: 10px 14px; margin: 0 0 20px; font-size: var(--fs-md); color: {text}; }}
  .help-box p {{ margin: 0 0 6px; font-weight: 600; }}
  .help-box ol {{ margin: 0; padding-left: 18px; }}
  .help-box li {{ margin: 4px 0; }}
  .help-box code {{ background: {card}; padding: 1px 5px; border-radius: var(--r-sm); font-size: var(--fs-sm); word-break: break-all; }}
  .container {{ padding-bottom: 120px; }}
  .add-row {{ margin: 10px 0 0; }}
  .add-row button {{ background: {card}; color: {accent}; border: 1px solid {accent}; }}
  .add-row button:hover {{ background: {hover}; }}
  .add-row button:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
  .note {{
    background: {bg}; border-left: 3px solid {accent}; border-radius: 0 var(--r-md) var(--r-md) 0;
    padding: 10px 12px; font-size: var(--fs-sm); color: {text}; line-height: 1.65; margin: 14px 0 4px;
  }}

  /* 한 줄 격자 — 머리글과 칸이 같은 열을 쓴다(예전 flex는 머리글과 입력칸 폭이 어긋났다). */
  .rcp-head, .rcp-row {{
    display: grid; align-items: center; column-gap: 7px;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr) 52px 52px 96px 96px 92px 24px;
  }}
  .rcp-head {{ font-size: var(--fs-xs); font-weight: 700; color: {muted}; padding: 0 0 5px; border-bottom: 1px solid {border}; margin: 0 0 3px; }}
  .rcp-head > span {{ text-align: center; }}
  .rcp-head > .l {{ text-align: left; }}
  .rcp-head .span2 {{ grid-column: span 2; }}
  .rcp-row {{ padding: 4px 0; }}
  .rcp-row:hover {{ position: relative; z-index: 3; }}
  .rcp-row input[type=text] {{ width: 100%; min-width: 0; box-sizing: border-box; }}
  .rcp-del {{ background: none; border: 0; padding: 4px; color: {muted}; cursor: pointer; width: auto; display: flex; justify-content: center; }}
  .rcp-del:hover {{ background: none; color: {error}; }}
  .rcp-del svg {{ width: 16px; height: 16px; }}

  /* 말풍선 — 홈 흐름도 말풍선(.tip)과 같은 모양 */
  .tw {{ position: relative; }}
  .rcp-head .tw {{ display: block; cursor: help; text-decoration: underline dotted {border}; text-underline-offset: 3px; }}
  .tip {{
    position: absolute; left: 50%; top: calc(100% + 7px); z-index: 30; width: 250px; max-width: calc(100vw - 40px);
    background: {card}; border: 1px solid {border}; border-radius: var(--r-lg); box-shadow: var(--sh-pop);
    padding: 10px 12px; font-size: var(--fs-sm); font-weight: 500; line-height: 1.6; color: {muted};
    text-align: left; white-space: normal; text-decoration: none; transform: translateX(-50%); pointer-events: none;
    /* 안 보일 땐 아예 그리지 않는다 — 숨긴 말풍선도 자리를 차지하면 오른쪽 칸의 말풍선이 창 밖으로
       나가 가로 스크롤이 생긴다. 0.15초 뒤에 떠서 스치듯 지나갈 땐 안 뜬다. */
    display: none;
  }}
  .tip b {{ display: block; margin: 0 0 3px; color: {header}; font-size: var(--fs-sm); }}
  .tip em {{ display: block; margin: 5px 0 0; font-style: normal; color: {text_faint}; font-size: var(--fs-xs); }}
  .tip.r {{ left: auto; right: 0; transform: none; }}
  .tw:hover > .tip {{ display: block; animation: tipIn .12s ease-out .15s both; }}
  @keyframes tipIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  body.pop-open .tip {{ display: none; }}

  /* 받는 것 칩 — 꺼짐 점선 · 켜짐 실선 */
  .rchip {{
    position: relative; display: inline-flex; align-items: center; justify-content: center; width: 100%; height: 30px;
    box-sizing: border-box; border: 1px dashed {border}; border-radius: var(--r-md); background: {card}; color: {text_faint};
    font-size: var(--fs-sm); font-weight: 600; cursor: pointer; user-select: none; white-space: nowrap;
  }}
  .rchip input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .rchip:has(input:focus-visible) {{ outline: 2px solid {accent}; outline-offset: 1px; }}
  .rchip:has(input:checked) {{ border: 1px solid {scoop_chip_border}; background: {scoop_bg}; color: {scoop_text}; }}
  /* 정기·수시 「기사 | 요약」 — 둘 중 하나를 고르는 칸이 아니라 각각 켜고 끄는 칸이라 4px 띄우고
     칸마다 제 테두리를 준다. 켜짐은 기사·요약 같은 모양(옅은 흐름 색 + 실선 + ✓), 꺼짐은 점선.
     시안 mockups/TELEGRAM_PAIR_CHIP_MOCKUP.html B안. */
  .rpair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }}
  .rpair label {{
    position: relative; display: inline-flex; align-items: center; justify-content: center; height: 30px;
    box-sizing: border-box; border: 1px dashed {border}; border-radius: var(--r-md);
    font-size: var(--fs-xs); font-weight: 600; color: {text_faint}; background: {card}; cursor: pointer; user-select: none;
    white-space: nowrap;
  }}
  .rpair input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .rpair label:has(input:focus-visible) {{ outline: 2px solid {accent}; outline-offset: 1px; }}
  .rpair.reg label:has(input:checked) {{ background: {hover}; color: {accent}; border: 1px solid {accent_border}; }}
  .rpair.adh label:has(input:checked) {{ background: {adhoc_bg}; color: {adhoc_text}; border: 1px solid {adhoc_border_strong}; }}
  .rpair label:has(input:checked)::before, .rchip:has(input:checked)::before {{ content: "✓"; margin-right: 3px; font-weight: 800; }}

  /* 알림 칩 — 늘 값이 있어 점선/실선 규칙 밖 */
  .nchip {{
    position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 4px; width: 100%; height: 30px;
    box-sizing: border-box; border: 1px solid {notify_chip_border}; border-radius: var(--r-md); background: {notify_chip_bg};
    color: {notify_chip_text}; font-size: var(--fs-xs); font-weight: 600; cursor: pointer; white-space: nowrap; padding: 0 4px;
    font-variant-numeric: tabular-nums;
  }}
  .nchip:hover {{ background: {notify_chip_bg}; border-color: {muted}; }}
  .nchip svg {{ width: 12px; height: 12px; flex-shrink: 0; }}

  /* 알림 창 */
  .npop {{
    position: absolute; z-index: 40; width: 280px; background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    box-shadow: var(--sh-pop); padding: 13px 14px 12px; font-size: var(--fs-sm); display: none;
  }}
  .npop.show {{ display: block; }}
  .npop h3 {{ margin: 0 0 3px; font-size: var(--fs-md); color: {header}; }}
  .npop .sub {{ margin: 0 0 10px; color: {muted}; font-size: var(--fs-xs); line-height: 1.6; }}
  .npop .opt {{ display: flex; align-items: flex-start; gap: 7px; padding: 7px 8px; margin: 0 0 3px; border-radius: var(--r-md); cursor: pointer; line-height: 1.45; }}
  .npop .opt:hover {{ background: {bg}; }}
  .npop .opt:has(input:checked) {{ background: {hover}; }}
  .npop .opt input {{ margin: 2px 0 0; accent-color: {accent}; }}
  .npop .opt b {{ display: block; font-weight: 600; color: {text}; }}
  .npop .opt small {{ display: block; color: {muted}; font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }}
  .npop .custom {{ margin: 4px 0 0 29px; display: none; }}
  .npop .custom.show {{ display: block; }}
  .npop .days {{ display: inline-flex; border: 1px solid {border}; border-radius: var(--r-md); overflow: hidden; margin: 0 0 7px; }}
  .npop .days label {{ padding: 4px 11px; font-size: var(--fs-sm); cursor: pointer; color: {muted}; position: relative; }}
  .npop .days label + label {{ border-left: 1px solid {border}; }}
  .npop .days input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .npop .days label:has(input:checked) {{ background: {accent}; color: {on_fill}; }}
  .npop .times {{ display: flex; align-items: center; gap: 5px; color: {muted}; font-size: var(--fs-sm); }}
  .npop select {{ font: inherit; font-size: var(--fs-sm); padding: 4px 5px; border: 1px solid {border}; border-radius: var(--r-md); background: {card}; width: auto; font-variant-numeric: tabular-nums; }}
  .npop .foot {{ margin: 10px 0 0; color: {muted}; font-size: var(--fs-xs); }}
  .npop .btns {{ display: flex; justify-content: flex-end; margin: 10px 0 0; padding: 10px 0 0; border-top: 1px solid {border}; }}
  .npop .btns button {{ width: auto; padding: 6px 16px; font-size: var(--fs-sm); }}

  .save-msg {{ display: none; align-items: flex-start; gap: 8px; font-size: var(--fs-sm); line-height: 1.5; border-radius: var(--r-md); padding: 9px 11px; margin-bottom: 10px; text-align: left; }}
  .save-msg.show {{ display: flex; }}
  .save-msg .dot {{ width: 8px; height: 8px; border-radius: var(--r-circle); flex-shrink: 0; margin-top: 5px; }}
  .save-msg.dirty {{ background: {warn_bg}; border: 1px solid {warn_border}; color: {warn_text}; }}
  .save-msg.dirty .dot {{ background: {warn_dot}; }}

  /* 좁은 화면 — 이름·chat id가 한 줄, 받는 것·알림이 그 아래 줄. 칩마다 이름이 적혀 있어 머리글은 숨긴다. */
  @media (max-width: 640px) {{
    .rcp-head {{ display: none; }}
    .tip {{ display: none !important; }}
    .rcp-row {{
      grid-template-columns: repeat(4, minmax(0, 1fr)) 24px; row-gap: 6px;
      padding: 10px 0; border-bottom: 1px solid {border};
    }}
    .rcp-row > .c-name {{ grid-column: 1 / 3; grid-row: 1; }}
    .rcp-row > .c-id {{ grid-column: 3 / 5; grid-row: 1; }}
    .rcp-row > .rcp-del {{ grid-column: 5; grid-row: 1; }}
    .rcp-row > .rchip {{ grid-row: 2; }}
    .rcp-row > .nchip {{ grid-column: 3 / 5; grid-row: 2; }}
    /* 머리글이 없으니 두 「기사 | 요약」이 어느 흐름인지 칸 위에 적는다 */
    .rcp-row > .rpair {{ grid-column: span 2; grid-row: 3; position: relative; margin-top: 14px; }}
    .rcp-row > .rpair::before {{ position: absolute; left: 2px; top: -15px; font-size: var(--fs-xs); font-weight: 700; color: {muted}; }}
    .rcp-row > .rpair.reg::before {{ content: "정기"; }}
    .rcp-row > .rpair.adh::before {{ content: "수시"; }}
  }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📤 텔레그램 받는 사람</h1>
  <p class="hint">
    받는 사람을 한 번만 등록해 두고, 무엇을 받을지와 알림이 울리는 시간을 사람마다 정합니다.
    정기 확정본이 언제 나가는지는 <a href="/auto-send">자동발송 대기시간 및 ON/OFF</a>, [단독]·[속보] 알림
    조건(감시 시간대·그룹)은 <a href="/breaking-alert">[단독]·[속보] 기사 알림</a>에서 정합니다.
  </p>
  <div class="status {status_class}">{status_text}</div>
  <div class="help-box">
    <p>받는 사람의 chat id 확인하는 법</p>
    <ol>
      <li>받을 사람이 텔레그램에서 이 봇을 찾아 대화를 시작합니다(아무 메시지나 1개 이상 전송).</li>
      <li>브라우저에서 <code>https://api.telegram.org/bot&lt;봇 토큰&gt;/getUpdates</code>를 열어 방금 보낸 메시지의 <code>chat.id</code> 값을 확인합니다.</li>
      <li>확인한 숫자를 아래 "받는 사람"에 등록합니다.</li>
    </ol>
  </div>
  <form method="POST" action="/save-telegram" id="tgForm">
    <p class="caption">받는 사람 (최대 {max_recipients}명) — 칸을 눌러 끄면 지우지 않고도 잠깐 대상에서 뺄 수 있습니다.</p>
    <div class="rcp-head">
      <span class="l">이름</span><span class="l">chat id</span>
      <span class="span2"><span class="tw">단독/속보{tip_alert}</span></span>
      <span><span class="tw">정기{tip_reg}</span></span>
      <span><span class="tw">수시{tip_adh}</span></span>
      <span><span class="tw">알림{tip_notify}</span></span><span></span>
    </div>
    <div id="rcpRows">
    {recipient_rows}
    </div>
    <p class="add-row">
      <button type="submit" formaction="/telegram/add-recipient-slot"{add_disabled}><span class="plus-glyph">+</span>받는 사람 추가</button>
    </p>
    <div class="note">
      "나"처럼 정기·수시는 다 끄고 [단독]·[속보]만 받을 수 있습니다 — 두 발송은 서로 독립적입니다
      (정기·수시는 보고서 전체, 알림은 해당 기사 1~3건만 즉시).
    </div>
    <div class="save-bar"><div class="save-bar-inner">
      <div class="save-msg dirty{dirty_class}" id="tgSaveMsg"><span class="dot"></span><span>아직 저장하지 않았습니다.</span></div>
      <button type="submit">저장</button>
    </div></div>
  </form>
</div>

<div class="npop" id="npop">
  <h3 id="np-title"></h3>
  <p class="sub">{p_sub}</p>
  <label class="opt"><input type="radio" name="np_mode" value="always"><span><b>{p_always}</b><small>{p_always_sub}</small></span></label>
  <label class="opt"><input type="radio" name="np_mode" value="work"><span><b>{p_work}</b><small>{p_work_sub}</small></span></label>
  <label class="opt"><input type="radio" name="np_mode" value="custom"><span><b>{p_custom}</b><small>{p_custom_sub}</small></span></label>
  <div class="custom" id="np-custom">
    <div class="days">
      <label><input type="radio" name="np_days" value="daily">{p_daily}</label>
      <label><input type="radio" name="np_days" value="weekday">{p_weekday}</label>
    </div>
    <div class="times"><select id="np-start">{time_options}</select>부터 <select id="np-end">{time_options}</select>까지</div>
  </div>
  <p class="foot">{p_foot}</p>
  <div class="btns"><button type="button" id="np-apply">{p_apply}</button></div>
</div>
{recipient_script}
</body>
</html>
"""
)

# 창 제목의 {name}은 JS가 채운다 — .format()을 안 거치는 별도 상수라 중괄호를 그대로 쓴다.
_TELEGRAM_RECIPIENT_SCRIPT = """<script>
(function () {
  var TITLE = __TITLE__;
  var FOOT = __FOOT__;
  var WORK = {days: "weekday", start: "09:00", end: "18:00"};
  var form = document.getElementById("tgForm");
  var pop = document.getElementById("npop");
  var current = null;

  function markDirty() { document.getElementById("tgSaveMsg").classList.add("show"); }
  form.addEventListener("change", function (e) {
    if (e.target.closest("#npop")) return;
    markDirty();
  });
  form.addEventListener("input", markDirty);

  // 🗑 — 저장 전까지 화면에서만 지운다(예전 del 버튼과 같은 흐름).
  document.getElementById("rcpRows").addEventListener("click", function (e) {
    var del = e.target.closest(".rcp-del");
    if (del) { del.closest(".rcp-row").remove(); markDirty(); return; }
    var chip = e.target.closest(".nchip");
    if (chip) { e.stopPropagation(); openPop(chip); }
  });

  function field(row, name) { return row.querySelector('input[data-n="' + name + '"]'); }
  function hh(t) { var p = t.split(":"); return String(+p[0]) + (p[1] === "00" ? "" : ":" + p[1]); }
  function label(mode, days, start, end) {
    if (mode === "always") return "하루 종일";
    if (mode === "work") return "업무 시간";
    return (days === "daily" ? "매일" : "평일") + " " + hh(start) + "–" + hh(end);
  }
  function sentence(mode, days, start, end) {
    if (mode === "always") return "하루 종일 울립니다.";
    if (mode === "work") { days = WORK.days; start = WORK.start; end = WORK.end; }
    return (days === "daily" ? "매일" : "월~금") + " " + start + " ~ " + end + (start > end ? " (다음 날)" : "") + "에 울립니다.";
  }
  function checked(name) { var el = pop.querySelector('input[name="' + name + '"]:checked'); return el ? el.value : null; }
  function setRadio(name, value) { var el = pop.querySelector('input[name="' + name + '"][value="' + value + '"]'); if (el) el.checked = true; }
  function syncCustom() { document.getElementById("np-custom").classList.toggle("show", checked("np_mode") === "custom"); }
  pop.addEventListener("change", syncCustom);

  function openPop(chip) {
    var row = chip.closest(".rcp-row");
    current = row;
    var name = row.querySelector(".c-name").value.trim() || "새 받는 사람";
    document.getElementById("np-title").textContent = TITLE.replace("{name}", name);
    setRadio("np_mode", field(row, "mode").value);
    setRadio("np_days", field(row, "days").value || "weekday");
    document.getElementById("np-start").value = field(row, "start").value || "09:00";
    document.getElementById("np-end").value = field(row, "end").value || "18:00";
    syncCustom();
    var rect = chip.getBoundingClientRect();
    var left = window.scrollX + rect.right - 280;
    pop.style.left = Math.max(window.scrollX + 8, left) + "px";
    pop.style.top = (window.scrollY + rect.bottom + 6) + "px";
    pop.classList.add("show");
    document.body.classList.add("pop-open");
  }
  function closePop() { pop.classList.remove("show"); document.body.classList.remove("pop-open"); current = null; }
  document.addEventListener("click", function (e) { if (current && !pop.contains(e.target)) closePop(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && current) closePop(); });

  document.getElementById("np-apply").addEventListener("click", function () {
    if (!current) return;
    var mode = checked("np_mode") || "always";
    var days = checked("np_days") || "weekday";
    var start = document.getElementById("np-start").value;
    var end = document.getElementById("np-end").value;
    if (mode === "custom" && start === end) {
      alert("시작과 끝 시간이 같아요 — 다르게 골라주세요.");
      return;
    }
    field(current, "mode").value = mode;
    field(current, "days").value = mode === "custom" ? days : "";
    field(current, "start").value = mode === "custom" ? start : "";
    field(current, "end").value = mode === "custom" ? end : "";
    var chip = current.querySelector(".nchip");
    chip.querySelector(".n-label").textContent = label(mode, days, start, end);
    var tip = chip.querySelector(".tip");
    tip.childNodes[1].textContent = sentence(mode, days, start, end);
    var em = tip.querySelector("em");
    if (!em) { em = document.createElement("em"); em.textContent = FOOT; tip.appendChild(em); }
    em.style.display = mode === "always" ? "none" : "";
    markDirty();
    closePop();
  });
})();
</script>"""


def _render_telegram_recipient_rows(recipients: list, slots: int) -> str:
    """받는 사람 줄을 렌더링한다. 폼 필드 이름은 tg_recipient{번호}_{항목} — 저장 핸들러
    (_parse_telegram_recipient_form)가 같은 이름으로 읽는다. 빈 칸(새로 추가한 줄)은
    받는 것 전부 꺼짐 + 알림 「업무 시간」으로 시작한다."""
    rows = []
    bell = icon("bell")
    for i in range(slots):
        n = i + 1
        if i < len(recipients):
            r = recipients[i]
            notify = normalize_notify(r.get("notify")) if "notify" in r else dict(LEGACY_NOTIFY)
        else:
            r = {}
            notify = dict(NEW_RECIPIENT_NOTIFY)

        def ck(field: str) -> str:
            return " checked" if r.get(field) else ""

        prefix = f"tg_recipient{n}"
        rows.append(
            '<div class="rcp-row">'
            f'<input type="text" class="c-name" name="{prefix}_name" value="{html.escape(r.get("name", ""))}" placeholder="이름">'
            f'<input type="text" class="c-id" name="{prefix}_chat_id" value="{html.escape(r.get("chat_id", ""))}" placeholder="chat id">'
            f'<label class="rchip tw"><input type="checkbox" name="{prefix}_alert_scoop" value="1"{ck("alert_scoop")}>[단독]{_tip_html("c_scoop")}</label>'
            f'<label class="rchip tw"><input type="checkbox" name="{prefix}_alert_flash" value="1"{ck("alert_flash")}>[속보]{_tip_html("c_flash")}</label>'
            '<div class="rpair reg">'
            f'<label class="a tw"><input type="checkbox" name="{prefix}_regular_articles" value="1"{ck("regular_articles")}>기사{_tip_html("reg_a")}</label>'
            f'<label class="s tw"><input type="checkbox" name="{prefix}_regular_summary" value="1"{ck("regular_summary")}>요약{_tip_html("reg_s")}</label>'
            "</div>"
            '<div class="rpair adh">'
            f'<label class="a tw"><input type="checkbox" name="{prefix}_adhoc_articles" value="1"{ck("adhoc_articles")}>기사{_tip_html("adh_a", right=True)}</label>'
            f'<label class="s tw"><input type="checkbox" name="{prefix}_adhoc_summary" value="1"{ck("adhoc_summary")}>요약{_tip_html("adh_s", right=True)}</label>'
            "</div>"
            f'<button type="button" class="nchip tw">{bell}<span class="n-label">{html.escape(notify_label(notify))}</span>'
            f"{_notify_tip_html(notify)}</button>"
            f'<input type="hidden" data-n="mode" name="{prefix}_notify_mode" value="{notify["mode"]}">'
            f'<input type="hidden" data-n="days" name="{prefix}_notify_days" value="{notify.get("days", "")}">'
            f'<input type="hidden" data-n="start" name="{prefix}_notify_start" value="{notify.get("start", "")}">'
            f'<input type="hidden" data-n="end" name="{prefix}_notify_end" value="{notify.get("end", "")}">'
            f'<button type="button" class="rcp-del" title="이 사람 지우기 (저장해야 반영)" aria-label="지우기">{icon("trash")}</button>'
            "</div>"
        )
    return "\n".join(rows)


def _parse_telegram_recipient_form(form: dict) -> list:
    """받는 사람 화면의 폼(tg_recipient{번호}_{항목})을 목록으로 읽는다. 저장과 「+ 받는 사람
    추가」가 같이 쓴다 — 둘이 따로 읽으면 한쪽에만 새 칸이 빠진다."""
    recipients = []
    for i in range(1, MAX_TELEGRAM_RECIPIENTS + 1):
        prefix = f"tg_recipient{i}"
        if f"{prefix}_chat_id" not in form:
            continue
        row = {
            "name": form.get(f"{prefix}_name", [""])[0],
            "chat_id": form.get(f"{prefix}_chat_id", [""])[0],
            "notify": {
                "mode": form.get(f"{prefix}_notify_mode", ["always"])[0],
                "days": form.get(f"{prefix}_notify_days", [""])[0],
                "start": form.get(f"{prefix}_notify_start", [""])[0],
                "end": form.get(f"{prefix}_notify_end", [""])[0],
            },
        }
        for field in RECEIVE_FIELDS:
            row[field] = f"{prefix}_{field}" in form
        recipients.append(row)
    return recipients


_BOT_NAME_SCRIPT = """<script>
(function () {
  var inp = document.getElementById("botName");
  if (!inp || inp.disabled) return;
  var before = document.getElementById("botNameBefore").value;
  var fallback = inp.getAttribute("placeholder");
  function firstChar(s) {
    var t = s.replace(/^[\\s\\p{Extended_Pictographic}\\uFE0F\\u200D]+/u, "");
    return (t || "?").charAt(0);
  }
  function sync() {
    var shown = inp.value.trim() || fallback;
    document.getElementById("bnCount").textContent = Array.from(inp.value).length + "/" + inp.maxLength;
    document.getElementById("bnShow").textContent = shown;
    document.getElementById("bnAvatar").textContent = firstChar(shown);
    var changed = shown !== before;
    inp.classList.toggle("changed", changed);
    document.getElementById("bnWas").classList.toggle("show", changed);
    return changed;
  }
  inp.addEventListener("input", function () {
    document.querySelectorAll(".bot-name .status").forEach(function (el) { el.remove(); });
    sync();
  });
  sync();
})();
</script>"""


def _render_bot_name_block(draft: Optional[str], before: Optional[str], notice: Optional[tuple]) -> str:
    """봇 이름 칸. 값은 화면을 열 때 텔레그램에서 불러온다(앱에 따로 저장하지 않는다).
    draft/before는 저장이 거절돼 입력하던 값을 다시 그릴 때, notice는 저장 결과
    (("ok"|"err", 문구)). 연동 › 텔레그램 발송 계정 화면에 있다."""
    if not telegram_is_configured():
        return (
            '<div class="bot-name"><label class="bn-label" for="botName">봇 이름</label>'
            f'<div class="bn-row"><input type="text" id="botName" value="{html.escape(DEFAULT_TELEGRAM_BOT_NAME)}" disabled></div>'
            '<p class="bn-hint">봇 토큰을 먼저 넣고 저장하면 이름을 바꿀 수 있습니다.</p></div>'
        )
    load_error = None
    if before is None:
        current, load_error = fetch_bot_name()
        before = current or DEFAULT_TELEGRAM_BOT_NAME
    value = before if draft is None else draft
    shown = value.strip() or DEFAULT_TELEGRAM_BOT_NAME
    status = ""
    if notice and notice[0] == "ok":
        status = f'<div class="status status-ok">{icon("check")} {html.escape(notice[1])}</div>'
    elif notice:
        status = f'<div class="status status-warn">{icon("alert")} {html.escape(notice[1])}</div>'
    elif load_error:
        status = (
            f'<div class="status status-warn">{icon("alert")} 텔레그램에서 지금 이름을 불러오지 못했어요 — '
            f'{html.escape(load_error)} 아래 칸은 기본 이름입니다.</div>'
        )
    esc = html.escape
    return f"""<div class="bot-name">
    <label class="bn-label" for="botName">봇 이름</label>
    <div class="bn-row">
      <input type="text" id="botName" name="bot_name" maxlength="{MAX_BOT_NAME_LEN}" value="{esc(value)}" placeholder="{esc(DEFAULT_TELEGRAM_BOT_NAME)}">
      <span class="bn-count" id="bnCount"></span>
    </div>
    <input type="hidden" id="botNameBefore" name="bot_name_before" value="{esc(before)}">
    <p class="bn-was" id="bnWas">지금 이름: <s>{esc(before)}</s> — 저장하면 바뀝니다.</p>
    <p class="bn-hint">받는 사람의 텔레그램 대화방 맨 위에 보이는 이름입니다. 한 봇을 쓰므로 <b>모든 받는 사람에게 같이 바뀝니다.</b> 비워 두고 저장하면 기본 이름으로 돌아갑니다.</p>
    <div class="bn-preview">
      <span class="cap">받는 사람 화면</span>
      <span class="bn-avatar" id="bnAvatar">{esc(shown[:1])}</span>
      <span class="bn-who"><b id="bnShow">{esc(shown)}</b><small>봇</small></span>
    </div>
    {status}
  </div>
  {_BOT_NAME_SCRIPT}"""


def render_telegram_settings(
    settings: dict, slots: Optional[int] = None, recipients: Optional[list] = None, dirty: bool = False,
) -> str:
    """텔레그램 받는 사람 설정 화면. dirty는 「+ 받는 사람 추가」로 저장 전 값을 다시 그릴 때."""
    if recipients is None:
        recipients = load_telegram_recipients()
    if slots is None:
        slots = max(len(recipients), 1)
    slots = min(max(slots, len(recipients)), MAX_TELEGRAM_RECIPIENTS)
    # 봇(토큰·이름)은 연동 › 텔레그램 발송 계정에서 정한다 — 여기선 어느 봇으로 나가는지만 알린다.
    sender_link = '<a href="/telegram-sender">설정 › 연동 › 텔레그램 발송 계정</a>'
    if telegram_is_configured():
        bot_name, _ = fetch_bot_name()
        who = f"<b>{html.escape(bot_name)}</b>" if bot_name else "등록됨"
        status_class, status_text = (
            "status-ok",
            f'{icon("check")} 보내는 봇: {who} — 바꾸려면 {sender_link} · <a href="/send-log">발송 기록 →</a>',
        )
    else:
        status_class, status_text = (
            "status-warn",
            f'{icon("alert")} 보내는 봇이 없어 아무것도 나가지 않습니다 — {sender_link}에서 넣어주세요.',
        )
    times = [f"{h:02d}:{m}" for h in range(24) for m in ("00", "30")]
    plain = {k: html.escape(v) for k, v in _TELEGRAM_TIPS.items() if isinstance(v, str)}
    return _TELEGRAM_SETTINGS_TEMPLATE.format(
        **_theme(),
        status_class=status_class,
        status_text=status_text,
        max_recipients=MAX_TELEGRAM_RECIPIENTS,
        recipient_rows=_render_telegram_recipient_rows(recipients, slots),
        add_disabled=" disabled" if slots >= MAX_TELEGRAM_RECIPIENTS else "",
        dirty_class=" show" if dirty else "",
        tip_alert=_tip_html("h_alert"),
        tip_reg=_tip_html("h_reg"),
        tip_adh=_tip_html("h_adh"),
        tip_notify=_tip_html("h_notify", right=True),
        time_options="".join(f"<option>{t}</option>" for t in times),
        recipient_script=_TELEGRAM_RECIPIENT_SCRIPT.replace("__TITLE__", json.dumps(_TELEGRAM_TIPS["p_title"])).replace(
            "__FOOT__", json.dumps(_TELEGRAM_TIPS["c_notify_foot"])
        ),
        index_href=_home_href(),
        **{k: v for k, v in plain.items() if k.startswith("p_")},
    )


# [추가: 2026-08-20] [단독]·[속보] 기사 알림 설정 화면 — 정기 자동발송(/auto-send)과는
# 완전히 별개의 트리거(감시 시간대·주기)를 갖는다. 예상 호출량은 서버가 렌더링 시점의
# 값으로 한 번 계산해 초기 표시하고, 이후 체크박스·시간·주기를 바꾸면 페이지 새로고침
# 없이 JS가 같은 계산을 다시 한다(설정을 "저장"하기 전에 먼저 감을 잡을 수 있도록).
_BREAKING_ALERT_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>[단독]·[속보] 기사 알림</title>
<style>"""
    + _BASE_STYLE
    + """
  .master {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    background: {warn_bg}; border: 1px solid {warn_border}; border-radius: var(--r-md); padding: 12px 14px; margin: 0 0 18px;
  }}
  .master-t {{ font-size: var(--fs-md); font-weight: 700; color: {warn_text}; }}
  .master-s {{ font-size: var(--fs-sm); color: {warn_sub}; margin-top: 2px; }}
  .toggle {{ position: relative; display: inline-flex; align-items: center; flex-shrink: 0; cursor: pointer; }}
  .toggle input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .toggle .track {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 36px; height: 24px; background: {border}; border-radius: var(--r-sm); transition: background 0.15s;
  }}
  .toggle input:checked ~ .track {{ background: {accent}; }}
  .toggle-text {{ font-size: 0.68rem; font-weight: 700; }}
  .toggle-text.on {{ display: none; color: {on_fill}; }}
  .toggle-text.off {{ display: inline; color: {muted}; }}
  .toggle input:checked ~ .track .toggle-text.on {{ display: inline; }}
  .toggle input:checked ~ .track .toggle-text.off {{ display: none; }}
  .sec {{ border-top: 1px solid {border}; padding: 16px 0 4px; }}
  .sec:first-of-type {{ border-top: none; }}
  .sec-t {{ font-size: var(--fs-md); font-weight: 700; color: {header}; margin: 0 0 4px; }}
  .sec-d {{ font-size: var(--fs-sm); color: {muted}; margin: 0 0 10px; line-height: 1.55; }}
  .grp {{ display: flex; align-items: center; gap: 8px; padding: 7px 0; font-size: var(--fs-md); }}
  .grp input[type=checkbox] {{ width: 16px; height: 16px; accent-color: {accent}; flex-shrink: 0; margin: 0; }}
  .grp .cnt {{ color: {muted}; font-size: var(--fs-sm); }}
  .grp .warn {{
    margin-left: auto; font-size: var(--fs-xs); color: {warn_accent}; background: {warn_bg};
    border: 1px solid {warn_border}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  .row {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 0 0 8px; }}
  .row label.k {{ font-size: var(--fs-md); color: {text}; }}
  .row select {{
    background: {bg}; color: {text}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 6px 8px; font-size: var(--fs-base); font-family: inherit;
  }}
  .est {{ background: {hover}; border-radius: var(--r-md); padding: 9px 12px; font-size: var(--fs-sm); color: {header}; line-height: 1.6; margin: 8px 0 4px; }}
  .est.hot {{ background: {error_bg}; color: {error_strong}; }}
  .sub {{ font-size: var(--fs-sm); color: {muted}; display: flex; align-items: center; gap: 7px; padding: 7px 0; }}
  .sub input[type=checkbox] {{ width: 15px; height: 15px; accent-color: {accent}; margin: 0; }}
  .who {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    background: {bg}; border: 1px solid {border}; border-radius: var(--r-md); padding: 10px 12px;
  }}
  .who .names {{ font-size: var(--fs-md); line-height: 1.6; }}
  .who a.btn {{ font-size: var(--fs-sm); padding: 6px 10px; white-space: nowrap; }}
  .foot {{ border-top: 1px dashed {border}; margin-top: 14px; padding: 12px 0 4px; font-size: var(--fs-sm); color: {muted}; line-height: 1.65; }}
  .foot b {{ color: {header}; }}
  .sub.sub-main {{ color: {text}; font-size: var(--fs-md); }}
  .burst-hist {{ margin: 4px 0 0; }}
  .burst-hist .dd {{ color: {muted}; display: inline-block; min-width: 3.2em; }}
  .burst-hist .n {{ font-weight: 700; color: {error_strong}; display: inline-block; min-width: 3.2em; }}
  .burst-hist .miss, .burst-hist .miss .n {{ color: {muted}; font-weight: 400; }}
  .container {{ padding-bottom: 88px; }}
  /* [추가: 2026-09-11] 800px 폭에 맞춰 감시 대상 그룹 체크를 한 줄에 세 개씩. 그룹이
     없을 때의 안내 문장은 세 칸을 다 쓴다. 좁은 화면에선 한 줄에 하나로 돌아간다. */
  .grp-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); column-gap: 16px; }}
  .grp-grid > :not(.grp) {{ grid-column: 1 / -1; }}
  @media (max-width: 640px) {{ .grp-grid {{ grid-template-columns: minmax(0, 1fr); }} }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>[단독]·[속보] 기사 알림</h1>
  <p class="hint">
    회차를 기다리지 않고, [단독]·[속보] 말머리가 붙은 기사가 올라오면 바로
    텔레그램으로 알려줍니다. 자동발송(<a href="/auto-send">/auto-send</a>)과는
    별개로 동작합니다.
  </p>
  {error_html}
  <form method="POST" action="/save-breaking-alert">
    <div class="master">
      <div>
        <div class="master-t">감시 사용</div>
        <div class="master-s">꺼도 회차 검사는 계속됩니다 (아래 설명 참고)</div>
      </div>
      <label class="toggle"><input type="checkbox" name="enabled"{enabled_checked}>
        <span class="track"><span class="toggle-text on">ON</span><span class="toggle-text off">OFF</span></span></label>
    </div>

    <div class="sec">
      <p class="sec-t">감시 대상 검색어 그룹</p>
      <p class="sec-d">고른 그룹의 검색 결과에서만 말머리를 찾습니다. 그룹이 많을수록 네이버 호출도 늘어납니다.</p>
      <div class="grp-grid">{group_rows}</div>
    </div>

    <div class="sec">
      <p class="sec-t">감시 시간대</p>
      <p class="sec-d">이 시간에만 확인합니다. 이 밖에는 호출하지 않으므로 알림도 오지 않습니다.</p>
      <div class="row">
        <input type="time" id="ba-start" name="start" value="{start}" onchange="baCalc()">
        <span style="color:{muted}">~</span>
        <input type="time" id="ba-end" name="end" value="{end}" onchange="baCalc()">
        <label class="k" style="margin-left:6px">확인 주기</label>
        <select id="ba-interval" name="interval_min" onchange="baCalc()">{interval_options}</select>
      </div>
      <div class="est" id="ba-est"></div>
      <label class="sub"><input type="checkbox" name="catch_up_enabled"{catch_up_checked}>
        감시 시간 밖에 나온 기사는 다음 감시 시작 때 몰아서 받기</label>
    </div>

    <div class="sec">
      <p class="sec-t">[속보] 몰림 알림</p>
      <p class="sec-d">[속보]가 짧은 시간에 여러 언론사에서 한꺼번에 나오면 한 번 더 알려 줍니다. 네이버 호출은 늘지 않아요.</p>
      <label class="sub sub-main"><input type="checkbox" id="bu-on" name="burst_enabled"{burst_checked} onchange="buCalc()">
        [속보] 몰림 알림 받기</label>
      <div class="row" id="bu-row">
        <select id="bu-win" name="burst_window_min" onchange="buCalc()">{burst_window_options}</select>
        <label class="k">안에</label>
        <select id="bu-n" name="burst_min_outlets" onchange="buCalc()">{burst_outlet_options}</select>
        <label class="k">개 언론사 이상이 [속보]를 내면</label>
      </div>
      <div class="est" id="bu-est"></div>
    </div>

    <div class="sec">
      <p class="sec-t">받는 사람</p>
      <p class="sec-d">말머리별로 누가 받을지는 텔레그램 받는 사람 화면에서 사람마다 지정합니다.</p>
      <div class="who">
        <div class="names">{recipient_summary}</div>
        <a class="btn" href="/telegram">받는 사람 관리 →</a>
      </div>
    </div>

    <p class="foot">
      <b>이 설정을 모두 꺼도</b> 회차가 기사를 모을 때 하는 말머리 검사는 그대로
      동작합니다 — 네이버 추가 호출이 없는 검사라 항상 켜져 있습니다. 위 수시 감시는
      그보다 빨리 알기 위한 기능입니다.
    </p>

    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
<script>
  var BA_GROUPS = {group_js};
  function baCalc() {{
    var n = 0;
    BA_GROUPS.forEach(function (g) {{
      var el = document.getElementById('ba-g-' + g.i);
      if (el && el.checked) n += g.n;
    }});
    var t1 = document.getElementById('ba-start').value.split(':');
    var t2 = document.getElementById('ba-end').value.split(':');
    var mins = (t2[0] * 60 + +t2[1]) - (t1[0] * 60 + +t1[1]);
    if (mins <= 0) mins += 1440;
    var iv = +document.getElementById('ba-interval').value;
    var polls = Math.floor(mins / iv);
    var calls = polls * n;
    var pct = calls / {daily_limit} * 100;
    var box = document.getElementById('ba-est');
    box.className = 'est' + (pct >= 70 ? ' hot' : '');
    if (n === 0) {{
      box.innerHTML = '감시할 그룹을 하나 이상 골라주세요.';
      return;
    }}
    box.innerHTML =
      '키워드 <b>' + n + '개</b> × 하루 <b>' + polls + '회</b> 확인 = 네이버 호출 <b>' +
      calls.toLocaleString() + '회/일</b> (일일 한도 {daily_limit_fmt}의 <b>' + pct.toFixed(1) + '%</b>)' +
      (pct >= 70
        ? '<br>⚠️ 한도에 가깝습니다 — 확인 주기를 늘리거나 감시 그룹을 줄여주세요.'
        : '<br>정기 스크랩·전체 기사가 쓰는 호출은 여기에 포함되지 않습니다.');
  }}
  baCalc();

  // [속보] 몰림 알림 — 저장된 정기 회차에서 날마다 가장 많이 몰린 구간(서버가 계산)을
  // 고른 기준에 대 보여 준다. 기준에 한 곳 모자란 날은 "안 울림"으로 같이 적는다.
  var BU_HIST = {burst_history_js};
  function buEsc(s) {{ return String(s).replace(/[&<>"]/g, function (c) {{
    return {{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}}[c]; }}); }}
  function buCalc() {{
    var on = document.getElementById('bu-on').checked;
    document.getElementById('bu-row').style.opacity = on ? 1 : 0.45;
    var box = document.getElementById('bu-est');
    if (!on || !BU_HIST.first) {{ box.style.display = 'none'; return; }}
    box.style.display = '';
    var rows = BU_HIST.win[document.getElementById('bu-win').value] || [];
    var n = +document.getElementById('bu-n').value;
    var hit = rows.filter(function (r) {{ return r.n >= n; }});
    var near = rows.filter(function (r) {{ return r.n === n - 1; }});
    var h = '정기 회차에 저장된 [속보](' + BU_HIST.first + '~' + BU_HIST.last +
      ')에 이 기준을 대 보면 <b>' + hit.length + '일</b> 울렸어요' + (hit.length ? ':' : '.');
    h += '<div class="burst-hist">';
    hit.forEach(function (r) {{
      h += '<div><span class="dd">' + r.d + '</span><span class="n">' + r.n + '곳</span>' +
        r.s + '~' + r.e + ' · ' + buEsc(r.t) + '</div>';
    }});
    near.forEach(function (r) {{
      h += '<div class="miss"><span class="dd">' + r.d + '</span><span class="n">' + r.n +
        '곳</span>안 울림 · ' + buEsc(r.t) + '</div>';
    }});
    box.innerHTML = h + '</div>';
  }}
  buCalc();
</script>
</body>
</html>
"""
)


def _breaking_alert_group_rows(settings: dict, selected_names: set) -> tuple:
    """감시 대상 그룹 체크박스 HTML과, 그룹별 활성 키워드 수(JS 예상 호출량 계산용)를
    함께 만든다. 활성 키워드 수는 app.breaking_alert_sender._watched_keywords와 같은
    기준(그룹 enabled + disabled_keywords 제외)으로 센다 — 화면 추정치와 실제 폴링이
    같은 숫자를 보게 하기 위해서다."""
    groups = active_search_groups(settings, settings.get("keyword_groups", []))
    rows, group_js = [], []
    for i, group in enumerate(groups):
        name = group.get("name", "")
        count = len(group.get("keywords", []))
        checked = " checked" if name in selected_names else ""
        warn = "" if group.get("enabled", True) else '<span class="warn">그룹 꺼짐</span>'
        rows.append(
            f'<label class="grp"><input type="checkbox" id="ba-g-{i}" name="group_names" '
            f'value="{html.escape(name)}"{checked} onchange="baCalc()">'
            f'{html.escape(name)} <span class="cnt">키워드 {count}개</span>{warn}</label>'
        )
        group_js.append({"i": i, "n": count})
    return "\n".join(rows), json.dumps(group_js, ensure_ascii=False)


def render_breaking_alert_settings(error: Optional[str] = None) -> str:
    """[단독]·[속보] 알림 설정 화면을 렌더링한다."""
    settings = load_settings()
    config = load_breaking_alert_settings()
    selected = set(config["group_names"])
    group_rows, group_js = _breaking_alert_group_rows(settings, selected)
    if not group_rows:
        group_rows = (
            '<p class="sec-d">등록된 검색어 그룹이 없습니다 — 먼저 '
            '<a href="/keywords">검색어</a>에서 그룹을 만들어주세요.</p>'
        )
    interval_options = "".join(
        f'<option value="{m}"{" selected" if m == config["interval_min"] else ""}>{m}분</option>'
        for m in (3, 5, 10, 15, 30)
    )
    recipients = load_telegram_recipients()
    scoop_names = [r.get("name") or r.get("chat_id", "") for r in recipients if r.get("alert_scoop")]
    flash_names = [r.get("name") or r.get("chat_id", "") for r in recipients if r.get("alert_flash")]
    if not scoop_names and not flash_names:
        recipient_summary = "받는 사람이 없습니다 — 텔레그램 받는 사람 화면에서 [단독]/[속보] 열을 체크해주세요."
    else:
        parts = []
        if scoop_names:
            parts.append(f'[단독] {", ".join(scoop_names)}')
        if flash_names:
            parts.append(f'[속보] {", ".join(flash_names)}')
        recipient_summary = " · ".join(parts)
    return _BREAKING_ALERT_SETTINGS_TEMPLATE.format(
        **_theme(),
        error_html=f'<p class="error">{html.escape(error)}</p>' if error else "",
        enabled_checked=" checked" if config["enabled"] else "",
        catch_up_checked=" checked" if config["catch_up_enabled"] else "",
        burst_checked=" checked" if config["burst_enabled"] else "",
        burst_window_options="".join(
            f'<option value="{m}"{" selected" if m == config["burst_window_min"] else ""}>{m}분</option>'
            for m in BURST_WINDOW_CHOICES
        ),
        burst_outlet_options="".join(
            f'<option value="{n}"{" selected" if n == config["burst_min_outlets"] else ""}>{n}</option>'
            for n in BURST_MIN_OUTLETS_CHOICES
        ),
        burst_history_js=json.dumps(burst_history(), ensure_ascii=False).replace("</", "<\\/"),
        group_rows=group_rows,
        group_js=group_js,
        start=html.escape(config["start"]),
        end=html.escape(config["end"]),
        interval_options=interval_options,
        recipient_summary=html.escape(recipient_summary),
        daily_limit=NAVER_DAILY_CALL_LIMIT,
        daily_limit_fmt=f"{NAVER_DAILY_CALL_LIMIT:,}",
        index_href=_home_href(),
    )


# [추가: 2026-08-06] 이메일 전송 설정 화면 — 텔레그램 화면과 같은 구조(여러 받는 사람 +
# 켜고 끄기). .env에 SMTP 정보가 없으면 켜도 조용히 건너뛴다(app.email_sender).
_EMAIL_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>이메일 받는 사람</title>
<style>"""
    + _BASE_STYLE
    + """
  .status {{ margin: 4px 0 20px; padding: 10px 14px; border-radius: var(--r-md); font-size: var(--fs-md); }}
  .status-ok {{ background: {hover}; color: {accent}; }}
  .status-warn {{ background: {error_bg}; color: {error}; }}
  .keyword-row {{ margin: 8px 0; display: flex; align-items: center; gap: 8px; }}
  .recipient-row input[type=text] {{ width: 130px; }}
  .add-row {{ margin: 4px 0 0; }}
  .add-row button {{ background: {card}; color: {accent}; border: 1px solid {accent}; }}
  .add-row button:hover {{ background: {hover}; }}
  .add-row button:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
  .toggle {{ position: relative; display: inline-flex; align-items: center; flex-shrink: 0; cursor: pointer; }}
  .toggle input {{ position: absolute; opacity: 0; width: 0; height: 0; }}
  .toggle .track {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 36px; height: 24px; background: {border}; border-radius: var(--r-sm); transition: background 0.15s;
  }}
  .toggle input:checked ~ .track {{ background: {accent}; }}
  .toggle-text {{ font-size: 0.68rem; font-weight: 700; }}
  .toggle-text.on {{ display: none; color: {on_fill}; }}
  .toggle-text.off {{ display: inline; color: {muted}; }}
  .toggle input:checked ~ .track .toggle-text.on {{ display: inline; }}
  .toggle input:checked ~ .track .toggle-text.off {{ display: none; }}
  .container {{ padding-bottom: 88px; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📧 이메일 받는 사람</h1>
  <p class="hint">
    확정본을 이메일로 받을 사람을 등록합니다. 언제 나가는지(직접 (발송) 누를 때 /
    자동발송)는 <a href="/auto-send">자동발송 대기시간 및 ON/OFF</a>에서 정합니다. 받는 사람 각자에게
    따로 발송되며, 서로의 주소는 보이지 않습니다.
  </p>
  <div class="status {status_class}">{status_text}</div>
  <form method="POST" action="/save-email">
    <p class="caption">받는 사람 (최대 {max_recipients}명) — 꺼두면(OFF) 지우지 않고도 잠깐 전송 대상에서 뺄 수 있습니다.</p>
    {recipient_rows}
    <p class="add-row">
      <button type="submit" formaction="/email/add-recipient-slot"{add_disabled}><span class="plus-glyph">+</span>받는 사람 추가</button>
    </p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{clear_script}
</body>
</html>
"""
)


def _render_email_recipient_rows(recipients: list, slots: int) -> str:
    """받는 사람 입력칸을 렌더링한다 — "저장된 개수만큼만 보여주고 + 버튼으로 늘리는" 방식."""
    rows = []
    for i in range(slots):
        recipient = recipients[i] if i < len(recipients) else {}
        name = html.escape(recipient.get("name", ""))
        email = html.escape(recipient.get("email", ""))
        enabled_checked = " checked" if recipient.get("enabled", True) else ""
        rows.append(
            '<div class="keyword-row recipient-row">'
            '<label class="toggle" title="전송 대상에서 켜고 끕니다(삭제 아님)">'
            f'<input type="checkbox" name="recipient{i + 1}_enabled" value="1"{enabled_checked}>'
            '<span class="track"><span class="toggle-text on">ON</span><span class="toggle-text off">OFF</span></span>'
            "</label>"
            f'<input type="text" name="recipient{i + 1}_name" value="{name}" placeholder="이름">'
            f'<input type="text" name="recipient{i + 1}_email" value="{email}" placeholder="이메일 주소">'
            f'<button type="button" class="del-btn" onclick="removeRow(this)" title="이 칸 지우기">del</button>'
            "</div>"
        )
    return "\n".join(rows)


def render_email_settings(settings: dict, slots: Optional[int] = None, recipients: Optional[list] = None) -> str:
    """이메일 자동 전송·받는 사람 설정 화면을 렌더링한다.

    slots/recipients를 생략하면 저장된 받는 사람 목록 그대로 보여준다(하나도 없으면
    빈 칸 1개) — "+ 받는 사람 추가"를 누르면 지금 입력 중이던 값을 유지한 채 slots만
    1 늘려 다시 렌더링한다(app.settings_server._handle_add_email_recipient_slot).
    """
    if recipients is None:
        recipients = load_email_recipients()
    if slots is None:
        slots = max(len(recipients), 1)
    slots = min(max(slots, len(recipients)), MAX_EMAIL_RECIPIENTS)
    if email_is_configured():
        status_class, status_text = (
            "status-ok",
            f'{icon("check")} 보내는 계정: {html.escape(email_sender_address() or "")} '
            '— 바꾸려면 <a href="/email-sender">설정 › 연동 › 이메일 발송 계정</a>',
        )
    else:
        status_class, status_text = (
            "status-warn",
            f'{icon("alert")} 보내는 계정이 없어 켜도 전송되지 않습니다 — '
            '<a href="/email-sender">설정 › 연동 › 이메일 발송 계정</a>에서 넣어주세요.',
        )
    return _EMAIL_SETTINGS_TEMPLATE.format(
        **_theme(),
        status_class=status_class,
        status_text=status_text,
        max_recipients=MAX_EMAIL_RECIPIENTS,
        recipient_rows=_render_email_recipient_rows(recipients, slots),
        add_disabled=" disabled" if slots >= MAX_EMAIL_RECIPIENTS else "",
        clear_script=_CLEAR_FIELD_SCRIPT,
        index_href=_home_href(),
    )


# [추가: 2026-08-20] /naver, /llm 두 화면이 공유하는 스타일·스크립트 — 저장소·마스킹·
# 연결 테스트·삭제라는 같은 UX 패턴을 쓰므로(app.credentials 설계 배경 참고) CSS·
# "보기/가리기" 토글도 하나로 묶는다. _BASE_STYLE과 같은 방식으로 이중 중괄호를 쓴다
# (아직 .format()을 거치기 전 — 각 페이지 템플릿에 문자열로 이어붙인 뒤 페이지
# 전체를 한 번에 .format()한다).
_CREDENTIAL_PAGE_STYLE = """
  label.field {{ display: block; font-size: var(--fs-md); font-weight: 600; color: {header}; margin: 18px 0 6px; }}
  input[type=password] {{
    background: {bg}; color: {text}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 8px 10px; font-size: var(--fs-base); font-family: inherit; width: 100%; box-sizing: border-box;
  }}
  .status {{
    display: flex; gap: 8px; align-items: flex-start; padding: 10px 12px; border-radius: var(--r-md);
    font-size: var(--fs-md); line-height: 1.5; margin-bottom: 4px;
  }}
  .status-ok {{ background: {ok_bg}; color: {ok_text}; border: 1px solid {ok_border}; }}
  .status-warn {{ background: {warn_bg}; color: {warn_text}; border: 1px solid {warn_border}; }}
  .status-info {{ background: {hover}; color: {header}; border: 1px solid {ghost_border}; }}
  .key-row {{ display: flex; gap: 6px; align-items: center; }}
  .key-row input {{ flex: 1; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .eye {{
    flex: 0 0 auto; background: transparent; color: {muted}; border: 1px solid {border};
    font-size: var(--fs-sm); padding: 8px 10px;
  }}
  .eye:hover {{ background: {hover}; color: {header}; }}
  .saved-key {{
    display: flex; align-items: center; gap: 8px; padding: 9px 12px; background: {bg};
    border: 1px solid {border}; border-radius: var(--r-md); font-family: ui-monospace, Menlo, monospace;
    font-size: var(--fs-md); color: {text}; margin-top: 8px;
  }}
  .saved-key .tag {{ margin-left: auto; font-family: {font_stack}; font-size: var(--fs-xs); color: {muted}; }}
  .test-row {{ display: flex; gap: 8px; align-items: center; margin-top: 10px; flex-wrap: wrap; }}
  .test-result {{ font-size: var(--fs-md); color: {muted}; }}
  .test-result.ok {{ color: {ok_text}; }}
  .test-result.err {{ color: {error}; }}
  details.howto {{ margin-top: 22px; border: 1px solid {border}; border-radius: var(--r-md); padding: 10px 12px; background: {bg}; }}
  details.howto summary {{ cursor: pointer; font-size: var(--fs-md); font-weight: 600; color: {header}; }}
  details.howto ol {{ margin: 10px 0 0; padding-left: 18px; font-size: var(--fs-md); line-height: 1.75; }}
  details.howto code {{ background: {card}; border: 1px solid {border}; border-radius: var(--r-sm); padding: 1px 5px; font-size: var(--fs-sm); }}
  .note {{ margin-top: 20px; font-size: var(--fs-sm); color: {muted}; line-height: 1.65; border-top: 1px dashed {border}; padding-top: 14px; }}
  .note code {{ background: {bg}; border: 1px solid {border}; border-radius: var(--r-sm); padding: 1px 5px; font-size: var(--fs-sm); }}
  .container {{ padding-bottom: 88px; }}
"""

# [추가: 2026-08-20] "보기/가리기" — 두 화면의 비밀값 입력칸이 같은 동작을 쓴다.
_CREDENTIAL_TOGGLE_SCRIPT = """<script>
function toggleKey(id, btn) {
  var i = document.getElementById(id);
  var show = i.type === "password";
  i.type = show ? "text" : "password";
  btn.textContent = show ? "가리기" : "보기";
}
</script>"""

_NAVER_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>네이버 뉴스 API 설정</title>
<style>"""
    + _BASE_STYLE
    + _CREDENTIAL_PAGE_STYLE
    + """
  button.ghost {{ background: transparent; color: {accent}; border: 1px solid {ghost_border}; }}
  button.ghost:hover {{ background: {hover}; color: {header}; border-color: {ghost_border_hover}; }}
  button.danger {{ background: transparent; color: {muted}; border: 1px solid {border}; font-size: var(--fs-sm); padding: 6px 12px; }}
  button.danger:hover {{ background: {error_bg}; color: {error}; border-color: {error}; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>🔑 네이버 뉴스 API 설정</h1>
  <p class="hint">
    기사를 가져오는 데 쓰는 키입니다. <b>이 키가 없으면 수집·전체 기사가 전부 멈춥니다</b>
    (AI 연동과 달리 이 키에는 대체 동작이 없습니다).
  </p>
  <div class="status {status_class}">{status_text}</div>
  <p class="caption">{status_caption}</p>

  <form method="POST" action="/save-naver">
    <label class="field" for="naver-id">Client ID</label>
    <input type="text" id="naver-id" name="naver_client_id" value="{client_id}" autocomplete="off" spellcheck="false">
    <p class="caption">비밀 값이 아니라 앱을 구분하는 식별자라 가리지 않고 그대로 보여줍니다.</p>

    <label class="field" for="naver-secret">Client Secret</label>
    {saved_secret_html}
    <div class="key-row" style="margin-top:8px">
      <input type="password" id="naver-secret" name="naver_client_secret" placeholder="{secret_placeholder}" autocomplete="off" spellcheck="false">
      <button type="button" class="eye" onclick="toggleKey('naver-secret', this)">보기</button>
    </div>
    <div class="test-row">
      <button type="button" class="ghost" onclick="testNaver()">연결 테스트</button>
      {delete_button}
      <span class="test-result" id="naver-test"></span>
    </div>

    <details class="howto">
      <summary>키를 어디서 받나요?</summary>
      <ol>
        <li><code>developers.naver.com</code> → <b>Application</b> → <b>애플리케이션 등록</b>.</li>
        <li>사용 API에서 <b>검색</b>을 고르고, 환경은 <b>WEB 설정</b>으로 등록합니다.</li>
        <li>등록하면 나오는 <b>Client ID</b>와 <b>Client Secret</b>을 위 칸에 붙여넣습니다.</li>
        <li>같은 화면에서 일일 호출 한도와 사용량을 볼 수 있습니다.</li>
      </ol>
    </details>

    <p class="note">
      <b>이 화면에 저장한 값이 <code>.env</code>보다 항상 우선합니다.</b> 비어 있을 때만
      <code>.env</code>의 <code>NAVER_CLIENT_ID</code>/<code>NAVER_CLIENT_SECRET</code>을
      씁니다 — 지금 <code>.env</code>로 쓰던 분은 아무것도 안 해도 그대로 돌아갑니다.<br><br>
      저장 위치는 <code>data/credentials.json</code>이고, 이 파일은 <code>.gitignore</code>에
      들어 있어 커밋되지 않습니다.
    </p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{toggle_script}
{page_script}
</body>
</html>
"""
)

# [추가: 2026-08-20] _NAVER_SETTINGS_TEMPLATE 안 {{page_script}} 자리에 값으로만
# 끼워 넣는다(_CLEAR_FIELD_SCRIPT와 같은 이유 — 큰 템플릿 문자열에 +로 직접 이어붙이면
# 이 JS 안의 실제 중괄호까지 바깥쪽 .format()에 걸려 KeyError가 난다).
_NAVER_PAGE_SCRIPT = """<script>
function testNaver() {
  var result = document.getElementById("naver-test");
  result.className = "test-result";
  result.textContent = "확인 중…";
  var params = new URLSearchParams({
    naver_client_id: document.getElementById("naver-id").value,
    naver_client_secret: document.getElementById("naver-secret").value
  });
  fetch("/test-naver", {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: params.toString()
  }).then(function (res) { return res.json(); }).then(function (data) {
    result.className = "test-result " + (data.ok ? "ok" : "err");
    result.textContent = (data.ok ? "✔ " : "✕ ") + data.message;
  }).catch(function () {
    result.className = "test-result err";
    result.textContent = "✕ 확인 요청에 실패했습니다 — 앱이 실행 중인지 확인해주세요.";
  });
}
function deleteNaverKey() {
  if (!confirm("저장된 네이버 키를 삭제할까요? .env에 값이 있으면 그쪽으로 자동 전환됩니다.")) return;
  fetch("/delete-naver", {method: "POST"}).then(function () { location.reload(); });
}
</script>"""

_LLM_SETTINGS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LLM(AI) 연동</title>
<style>"""
    + _BASE_STYLE
    + _CREDENTIAL_PAGE_STYLE
    + """
  button.ghost {{ background: transparent; color: {accent}; border: 1px solid {ghost_border}; }}
  button.ghost:hover {{ background: {hover}; color: {header}; border-color: {ghost_border_hover}; }}
  button.danger {{ background: transparent; color: {muted}; border: 1px solid {border}; font-size: var(--fs-sm); padding: 6px 12px; }}
  button.danger:hover {{ background: {error_bg}; color: {error}; border-color: {error}; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>🤖 LLM(AI) 연동</h1>
  <p class="hint">
    소제목 자동 분류·요약에 쓰는 Claude API 키입니다. <b>없어도 앱은 그대로 돌아갑니다</b> —
    규칙 기반(단어 빈도) 소제목으로 조용히 되돌아갑니다.
  </p>
  {error_html}
  <div class="status {status_class}">{status_text}</div>
  <p class="caption">{status_caption}</p>

  <form method="POST" action="/save-llm">
    <label class="field" for="llm-key">Claude API 키</label>
    {saved_key_html}
    <div class="key-row" style="margin-top:8px">
      <input type="password" id="llm-key" name="llm_api_key" placeholder="{key_placeholder}" autocomplete="off" spellcheck="false">
      <button type="button" class="eye" onclick="toggleKey('llm-key', this)">보기</button>
    </div>
    <div class="test-row">
      <button type="button" class="ghost" onclick="testLlm()">연결 테스트</button>
      {delete_button}
      <span class="test-result" id="llm-test"></span>
    </div>

    <details class="howto">
      <summary>키를 어디서 받나요?</summary>
      <ol>
        <li><code>console.anthropic.com</code> 로그인 → <b>API keys</b> → <b>Create key</b>.</li>
        <li><code>sk-ant-…</code> 문자열을 복사해 위 칸에 붙여넣습니다(콘솔은 한 번만 보여줍니다).</li>
        <li>결제 수단이 없으면 [연결 테스트]에서 크레딧 부족 오류가 납니다.</li>
      </ol>
    </details>

    <p class="note">
      <b>키는 이 컴퓨터 밖으로 나가지 않습니다.</b> <code>data/credentials.json</code>에만
      저장되고, 화면·복사·내보내기·발송 어디에도 실리지 않으며 로그에도 남기지 않습니다.<br><br>
      <b>틀린 키를 넣어도 앱은 안 멈춥니다</b> — 규칙 기반으로 내려가고, 초안 화면 맨 위에
      그 사실을 알리는 경고가 뜹니다. 그래서 저장 전 [연결 테스트]가 중요합니다.
    </p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{toggle_script}
{page_script}
</body>
</html>
"""
)

# [추가: 2026-08-20] _NAVER_PAGE_SCRIPT와 같은 이유로 별도 상수.
_LLM_PAGE_SCRIPT = """<script>
function testLlm() {
  var result = document.getElementById("llm-test");
  result.className = "test-result";
  result.textContent = "확인 중…";
  var params = new URLSearchParams({
    llm_api_key: document.getElementById("llm-key").value
  });
  fetch("/test-llm", {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: params.toString()
  }).then(function (res) { return res.json(); }).then(function (data) {
    result.className = "test-result " + (data.ok ? "ok" : "err");
    result.textContent = (data.ok ? "✔ " : "✕ ") + data.message;
  }).catch(function () {
    result.className = "test-result err";
    result.textContent = "✕ 확인 요청에 실패했습니다 — 앱이 실행 중인지 확인해주세요.";
  });
}
function deleteLlmKey() {
  if (!confirm("저장된 AI 키를 삭제할까요? .env에 값이 있으면 그쪽으로 자동 전환되고, 둘 다 없으면 규칙 기반 소제목으로 돌아갑니다.")) return;
  fetch("/delete-llm", {method: "POST"}).then(function () { location.reload(); });
}
</script>"""

def render_naver_settings_page() -> str:
    """네이버 검색 API 키 설정 화면을 렌더링한다.

    [추가: 2026-08-20] 이 화면 하나로 앱이 켜지느냐 마느냐가 갈린다(app.config가 더
    이상 키 없음을 RuntimeError로 막지 않으므로) — 그래서 상태 문구도 "아직 키가
    없다" 하나만으로 끝내지 않고, 이 키가 없으면 정확히 뭐가 멈추는지(수집·
    실시간 현황)까지 h1 아래 hint에서 먼저 밝힌다.
    """
    source = naver_source()
    if source == "none":
        status_class = "status-warn"
        status_text = f'{icon("alert")} 아직 키가 없습니다 — 기사를 한 건도 가져올 수 없습니다.'
        status_caption = "아래에 키를 넣고 [연결 테스트]로 확인한 뒤 저장하세요."
    elif source == "env":
        status_class = "status-info"
        status_text = f'{icon("check")} .env의 키를 쓰고 있습니다 — 여기에 저장하면 그 값이 우선합니다.'
        status_caption = "설정에 저장된 값이 없어 .env로 넘어간 상태입니다. 그대로 둬도 됩니다."
    else:
        status_class = "status-ok"
        status_text = f'{icon("check")} 설정에 저장된 키를 쓰고 있습니다.'
        saved_at = naver_saved_at()
        status_caption = (
            f"저장 시각: {saved_at}. 문제가 있어 보이면 아래 [연결 테스트]로 확인해보세요."
            if saved_at
            else "문제가 있어 보이면 아래 [연결 테스트]로 확인해보세요."
        )

    saved_secret = naver_saved_client_secret()
    if saved_secret:
        saved_at = html.escape(naver_saved_at() or "")
        saved_secret_html = (
            f'<div class="saved-key"><span>{html.escape(mask_credential(saved_secret))}</span>'
            f'<span class="tag">{saved_at} 저장</span></div>'
        )
        secret_placeholder = "새 값으로 바꿀 때만 입력"
    else:
        saved_secret_html = ""
        secret_placeholder = "Client Secret"

    delete_button = (
        '<button type="button" class="danger" onclick="deleteNaverKey()">키 삭제</button>'
        if source == "saved"
        else ""
    )

    return _NAVER_SETTINGS_TEMPLATE.format(
        **_theme(),
        status_class=status_class,
        status_text=status_text,
        status_caption=status_caption,
        client_id=html.escape(naver_client_id() or ""),
        saved_secret_html=saved_secret_html,
        secret_placeholder=secret_placeholder,
        delete_button=delete_button,
        toggle_script=_CREDENTIAL_TOGGLE_SCRIPT,
        page_script=_NAVER_PAGE_SCRIPT,
        index_href=_home_href(),
    )


def render_llm_settings_page(error: str = "") -> str:
    """AI 연동(Claude) 설정 화면을 렌더링한다.

    [수정: 2026-08-20] 모델 선택 라디오를 뺐다 — app.config.LLM_MODEL 주석 참고.
    이 화면은 이제 API 키만 다룬다(어느 모델을 쓸지는 코드가 정하는 값).
    """
    source = llm_source()
    if source == "none":
        status_class = "status-warn"
        status_text = f'{icon("alert")} 아직 키가 없습니다 — 소제목은 규칙 기반으로만 만들어집니다.'
        status_caption = "이 기능을 안 쓰기로 한 상태로도 정상입니다."
    elif source == "env":
        status_class = "status-info"
        status_text = f'{icon("check")} .env의 키를 쓰고 있습니다 — 여기에 저장하면 그 값이 우선합니다.'
        status_caption = "설정에 저장된 값이 없어 .env로 넘어간 상태입니다. 그대로 둬도 됩니다."
    else:
        status_class = "status-ok"
        status_text = f'{icon("check")} 설정에 저장된 키를 쓰고 있습니다.'
        saved_at = llm_saved_at()
        status_caption = (
            f"저장 시각: {saved_at}. 문제가 있어 보이면 아래 [연결 테스트]로 확인해보세요."
            if saved_at
            else "문제가 있어 보이면 아래 [연결 테스트]로 확인해보세요."
        )

    saved_key = llm_saved_api_key()
    if saved_key:
        saved_at = html.escape(llm_saved_at() or "")
        saved_key_html = (
            f'<div class="saved-key"><span>{html.escape(mask_credential(saved_key, keep_prefix=13))}</span>'
            f'<span class="tag">{saved_at} 저장</span></div>'
        )
        key_placeholder = "새 키로 바꿀 때만 입력"
    else:
        saved_key_html = ""
        key_placeholder = "sk-ant-api03-…"

    delete_button = (
        '<button type="button" class="danger" onclick="deleteLlmKey()">키 삭제</button>'
        if source == "saved"
        else ""
    )

    return _LLM_SETTINGS_TEMPLATE.format(
        **_theme(),
        error_html=f'<p class="error">{html.escape(error)}</p>' if error else "",
        status_class=status_class,
        status_text=status_text,
        status_caption=status_caption,
        saved_key_html=saved_key_html,
        key_placeholder=key_placeholder,
        delete_button=delete_button,
        toggle_script=_CREDENTIAL_TOGGLE_SCRIPT,
        page_script=_LLM_PAGE_SCRIPT,
        index_href=_home_href(),
    )


# 연동 › 텔레그램 발송 계정 — 봇 토큰(설정 화면 값 > .env)과 봇 이름. 「이메일 발송 계정」과
# 같은 틀이고, 봇은 자기 자신에게 메시지를 보낼 수 없어 시험 발송 대신 [연결 확인](getMe)이다.
# 시안 mockups/TELEGRAM_SENDER_MOCKUP.html.
_TELEGRAM_SENDER_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>텔레그램 발송 계정</title>
<style>"""
    + _BASE_STYLE
    + _CREDENTIAL_PAGE_STYLE
    + """
  button.ghost {{ background: transparent; color: {accent}; border: 1px solid {ghost_border}; }}
  button.ghost:hover {{ background: {hover}; color: {header}; border-color: {ghost_border_hover}; }}
  button.danger {{ background: transparent; color: {muted}; border: 1px solid {border}; font-size: var(--fs-sm); padding: 6px 12px; }}
  button.danger:hover {{ background: {error_bg}; color: {error}; border-color: {error}; }}
  .caption {{ margin: 6px 0 0; }}
  /* 봇 이름 — 받는 사람 대화방 맨 위에 보이는 이름. 시안 mockups/BOT_NAME_MOCKUP.html */
  .bot-name {{ margin: 22px 0 0; padding: 14px 16px; border: 1px solid {border}; border-radius: var(--r-lg); }}
  .bn-label {{ display: block; font-size: var(--fs-md); font-weight: 700; color: {header}; margin: 0 0 8px; }}
  .bn-row {{ display: flex; align-items: center; gap: 8px; }}
  .bn-row input[type=text] {{ flex: 1 1 auto; min-width: 0; box-sizing: border-box; }}
  .bn-row input.changed {{ border-color: {accent}; background: {card}; }}
  .bn-row input:disabled {{ color: {muted}; }}
  .bn-count {{ flex-shrink: 0; color: {text_faint}; font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }}
  .bn-hint {{ margin: 7px 0 0; color: {muted}; font-size: var(--fs-sm); line-height: 1.6; }}
  .bn-was {{ margin: 4px 0 0; color: {muted}; font-size: var(--fs-sm); display: none; }}
  .bn-was.show {{ display: block; }}
  .bn-was s {{ color: {text_faint}; }}
  .bn-preview {{ margin: 12px 0 0; display: flex; align-items: center; gap: 10px; padding: 9px 12px; background: {bg}; border-radius: var(--r-lg); }}
  .bn-preview .cap {{ font-size: var(--fs-xs); color: {text_faint}; font-weight: 600; margin-right: 2px; white-space: nowrap; }}
  .bn-avatar {{
    width: 34px; height: 34px; border-radius: var(--r-circle); background: {header}; color: {on_fill}; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: var(--fs-md);
  }}
  .bn-who {{ min-width: 0; }}
  .bn-who b {{ display: block; font-size: var(--fs-md); color: {text}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .bn-who small {{ color: {muted}; font-size: var(--fs-xs); }}
  .bot-name .status {{ margin: 10px 0 0; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>📨 텔레그램 발송 계정</h1>
  <p class="hint">
    정기·수시 보고서와 [단독]·[속보] 알림을 텔레그램으로 보내는 <b>봇</b>입니다. <b>없어도 앱은 그대로 돌아갑니다</b> —
    텔레그램만 건너뛰고 이메일은 평소대로 나갑니다. 누구에게 보낼지는 <a href="/telegram">받는 사람 지정 › 텔레그램</a>에서 정합니다.
  </p>
  <div class="status {status_class}">{status_text}</div>
  <p class="caption">{status_caption}</p>

  <form method="POST" action="/save-telegram-sender">
    <label class="field" for="tg-token">봇 토큰</label>
    {saved_token_html}
    <div class="key-row" style="margin-top:8px">
      <input type="password" id="tg-token" name="bot_token" placeholder="{token_placeholder}" autocomplete="off" spellcheck="false">
      <button type="button" class="eye" onclick="toggleKey('tg-token', this)">보기</button>
    </div>
    <p class="caption">BotFather가 봇을 만들 때 준 값입니다. 숫자 + 콜론(:) + 긴 글자 모양입니다.</p>

    <div class="test-row">
      <button type="button" class="ghost" onclick="testTelegramSender()">연결 확인</button>
      {delete_button}
      <span class="test-result" id="tg-test"></span>
    </div>
    <p class="caption">메시지는 보내지 않고, 이 토큰이 어느 봇인지만 텔레그램에 물어봅니다.</p>

    {bot_name_block}

    <details class="howto">
      <summary>봇 토큰은 어디서 받나요?</summary>
      <ol>
        <li>텔레그램에서 <b>@BotFather</b>를 찾아 대화를 엽니다.</li>
        <li><code>/newbot</code>을 보내고, 안내대로 봇 이름과 아이디(끝이 <code>bot</code>)를 정합니다.</li>
        <li>BotFather가 돌려준 <b>토큰</b>을 위 칸에 붙여 넣고 [연결 확인] → [저장].</li>
        <li>받는 사람마다 새 봇을 찾아 <b>시작(/start)</b>을 한 번 눌러야 메시지를 받을 수 있습니다.</li>
      </ol>
    </details>

    <p class="note">
      <b>토큰은 이 컴퓨터 밖으로 나가지 않습니다.</b> <code>data/credentials.json</code>에만
      저장되고, 화면·복사·내보내기·발송 어디에도 실리지 않으며 로그에도 남기지 않습니다.<br><br>
      <b>토큰을 다른 봇 것으로 바꾸면</b> 받는 사람들은 그 새 봇에서 시작(/start)을 다시 눌러야 합니다 —
      누르기 전엔 그 사람에게 가는 메시지가 실패로 기록됩니다(정기 보관함의 빨간 링).<br><br>
      <b>틀린 값을 넣어도 앱은 안 멈춥니다</b> — 텔레그램 발송만 실패로 기록되고 이메일은 그대로 나갑니다.
    </p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{toggle_script}
{page_script}
</body>
</html>
"""
)

_TELEGRAM_SENDER_PAGE_SCRIPT = """<script>
function testTelegramSender() {
  var result = document.getElementById("tg-test");
  result.className = "test-result";
  result.textContent = "확인 중…";
  var params = new URLSearchParams({bot_token: document.getElementById("tg-token").value});
  fetch("/test-telegram-sender", {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: params.toString()
  }).then(function (res) { return res.json(); }).then(function (data) {
    result.className = "test-result " + (data.ok ? "ok" : "err");
    result.textContent = (data.ok ? "✔ " : "✕ ") + data.message;
  }).catch(function () {
    result.className = "test-result err";
    result.textContent = "✕ 확인 요청에 실패했습니다 — 앱이 실행 중인지 확인해주세요.";
  });
}
function deleteTelegramSender() {
  if (!confirm("저장된 봇 토큰을 삭제할까요? .env에 값이 있으면 그쪽으로 자동 전환되고, 둘 다 없으면 텔레그램은 보내지 않습니다.")) return;
  fetch("/delete-telegram-sender", {method: "POST"}).then(function () { location.reload(); });
}
</script>"""


def render_telegram_sender_page(bot_name_notice: Optional[tuple] = None) -> str:
    """연동 › 텔레그램 발송 계정 화면."""
    source = telegram_source()
    if source == "none":
        status_class = "status-warn"
        status_text = f'{icon("alert")} 봇 토큰이 없습니다 — 텔레그램으로는 아무것도 나가지 않습니다.'
        status_caption = "텔레그램을 안 쓰기로 한 상태로도 정상입니다."
    elif source == "env":
        status_class = "status-info"
        status_text = f'{icon("check")} .env의 봇 토큰을 쓰고 있습니다 — 여기에 저장하면 그 값이 우선합니다.'
        status_caption = "설정에 저장된 값이 없어 .env로 넘어간 상태입니다. 그대로 둬도 됩니다."
    else:
        status_class = "status-ok"
        status_text = f'{icon("check")} 설정에 저장된 봇 토큰을 쓰고 있습니다.'
        status_caption = "문제가 있어 보이면 아래 [연결 확인]으로 확인해보세요."
    saved_token = telegram_saved_token()
    if saved_token:
        saved_token_html = (
            f'<div class="saved-key"><span>{html.escape(mask_credential(saved_token, keep_prefix=saved_token.find(":") + 1))}</span>'
            f'<span class="tag">{html.escape(telegram_saved_at() or "")} 저장</span></div>'
        )
        token_placeholder = "새 토큰으로 바꿀 때만 입력"
    else:
        saved_token_html = ""
        token_placeholder = "123456789:AAH…"
    delete_button = (
        '<button type="button" class="danger" onclick="deleteTelegramSender()">저장된 토큰 삭제</button>'
        if source == "saved"
        else ""
    )
    return _TELEGRAM_SENDER_TEMPLATE.format(
        **_theme(),
        status_class=status_class,
        status_text=status_text,
        status_caption=status_caption,
        saved_token_html=saved_token_html,
        token_placeholder=token_placeholder,
        delete_button=delete_button,
        bot_name_block=_render_bot_name_block(None, None, bot_name_notice),
        toggle_script=_CREDENTIAL_TOGGLE_SCRIPT,
        page_script=_TELEGRAM_SENDER_PAGE_SCRIPT,
        index_href=_home_href(),
    )


# [추가: 2026-09-18] 연동 › 이메일 보내는 계정 — 「AI 연동」 화면과 같은 틀. 메일 서비스는
# 네이버·Gmail 칩 둘뿐이고 서버·포트는 고른 서비스가 정한다(app.config.EMAIL_SERVICES).
# 시안 mockups/EMAIL_SENDER_MOCKUP.html.
_EMAIL_SENDER_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>이메일 발송 계정</title>
<style>"""
    + _BASE_STYLE
    + _CREDENTIAL_PAGE_STYLE
    + """
  button.ghost {{ background: transparent; color: {accent}; border: 1px solid {ghost_border}; }}
  button.ghost:hover {{ background: {hover}; color: {header}; border-color: {ghost_border_hover}; }}
  button.danger {{ background: transparent; color: {muted}; border: 1px solid {border}; font-size: var(--fs-sm); padding: 6px 12px; }}
  button.danger:hover {{ background: {error_bg}; color: {error}; border-color: {error}; }}
  .svc {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .svc input {{ position: absolute; opacity: 0; pointer-events: none; }}
  .svc label {{
    background: {card}; color: {text}; border: 1px solid {border}; border-radius: var(--r-pill);
    padding: 5px 14px; font-size: var(--fs-md); cursor: pointer;
  }}
  .svc label:hover {{ background: {hover}; }}
  .svc input:checked + label {{ background: {hover}; color: {accent}; border-color: {accent}; font-weight: 600; }}
  .svc input:focus-visible + label {{ outline: 2px solid {accent_border}; outline-offset: 1px; }}
  .server {{ margin: 8px 0 0; font-size: var(--fs-sm); color: {muted}; }}
  input.wide {{ width: 100%; max-width: 420px; box-sizing: border-box; }}
  .opt {{ color: {muted}; font-weight: 400; font-size: var(--fs-sm); }}
  .caption {{ margin: 6px 0 0; }}
</style>
</head>
<body>
"""
    + _TOP_BAR_HTML
    + """
<div class="container">
  <h1>✉️ 이메일 발송 계정</h1>
  <p class="hint">
    정기 확정본을 이메일로 보낼 때 쓰는 계정입니다. <b>없어도 앱은 그대로 돌아갑니다</b> —
    이메일만 건너뛰고 텔레그램은 평소대로 나갑니다.
  </p>
  {error_html}
  <div class="status {status_class}">{status_text}</div>
  <p class="caption">{status_caption}</p>

  <form method="POST" action="/save-email-sender">
    <label class="field">메일 서비스</label>
    <div class="svc">{service_chips}</div>
    <p class="server" id="svc-server">{server_text}</p>

    <label class="field" for="em-addr">보내는 사람 주소</label>
    <input type="text" id="em-addr" name="address" class="wide" value="{address}" placeholder="sweetgreetings@naver.com" autocomplete="off" spellcheck="false">
    <p class="caption">위에서 고른 서비스의 주소여야 합니다(네이버면 @naver.com). 다른 주소면 메일 서버가 발송을 거절합니다.</p>

    <label class="field" for="em-name">보내는 사람 이름 <span class="opt">(선택)</span></label>
    <input type="text" id="em-name" name="name" class="wide" value="{name}" placeholder="예: 재경부 디소팀" maxlength="40">
    <p class="caption">받는 사람 메일함에 주소 대신 보이는 이름입니다. 비우면 주소가 그대로 보입니다.</p>

    <label class="field" for="em-pw">앱 비밀번호</label>
    {saved_pw_html}
    <div class="key-row" style="margin-top:8px">
      <input type="password" id="em-pw" name="password" placeholder="{pw_placeholder}" autocomplete="off" spellcheck="false">
      <button type="button" class="eye" onclick="toggleKey('em-pw', this)">보기</button>
    </div>
    <p class="caption">계정 로그인 비밀번호가 아니라, 메일 서비스에서 따로 발급하는 「앱 비밀번호」입니다.</p>

    <div class="test-row">
      <button type="button" class="ghost" onclick="testEmailSender()">시험 메일 보내기</button>
      {delete_button}
      <span class="test-result" id="em-test"></span>
    </div>
    <p class="caption">시험 메일은 받는 사람 명단이 아니라 <b>보내는 사람 주소 자신</b>에게 한 통 갑니다.</p>

    <details class="howto">
      <summary>앱 비밀번호는 어디서 받나요?</summary>
      <ol>
        <li><b>네이버</b>: 메일 환경설정 → POP3/IMAP 설정에서 <b>SMTP 사용</b>을 켜고, 2단계 인증을 쓰면 네이버 보안 설정에서 <b>애플리케이션 비밀번호</b>를 만듭니다.</li>
        <li><b>Gmail</b>: Google 계정 → 보안 → 2단계 인증을 켠 뒤 → <b>앱 비밀번호</b>에서 16자리를 만듭니다.</li>
        <li>korea.kr 같은 기관 메일은 외부 앱 발송을 막는 경우가 많아 보내는 계정으로는 고를 수 없습니다 — 받는 사람으로는 그대로 쓸 수 있습니다.</li>
      </ol>
    </details>

    <p class="note">
      <b>비밀번호는 이 컴퓨터 밖으로 나가지 않습니다.</b> <code>data/credentials.json</code>에만
      저장되고, 화면·복사·내보내기·발송 어디에도 실리지 않으며 로그에도 남기지 않습니다.<br><br>
      <b>틀린 값을 넣어도 앱은 안 멈춥니다</b> — 이메일 발송만 실패로 기록되고(정기 보관함의
      빨간 링), 텔레그램은 그대로 나갑니다. 그래서 저장 전 [시험 메일 보내기]가 중요합니다.
    </p>
    <div class="save-bar"><div class="save-bar-inner"><button type="submit">저장</button></div></div>
  </form>
</div>
{toggle_script}
{page_script}
</body>
</html>
"""
)

_EMAIL_SENDER_PAGE_SCRIPT = """<script>
function _emailService() {
  var c = document.querySelector('input[name="service"]:checked');
  return c ? c.value : "";
}
document.querySelectorAll('input[name="service"]').forEach(function (r) {
  r.addEventListener("change", function () {
    document.getElementById("svc-server").textContent = "보내는 서버 " + r.dataset.server;
  });
});
function testEmailSender() {
  var result = document.getElementById("em-test");
  result.className = "test-result";
  result.textContent = "보내는 중…";
  var params = new URLSearchParams({
    service: _emailService(),
    address: document.getElementById("em-addr").value,
    name: document.getElementById("em-name").value,
    password: document.getElementById("em-pw").value
  });
  fetch("/test-email-sender", {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: params.toString()
  }).then(function (res) { return res.json(); }).then(function (data) {
    result.className = "test-result " + (data.ok ? "ok" : "err");
    result.textContent = (data.ok ? "✔ " : "✕ ") + data.message;
  }).catch(function () {
    result.className = "test-result err";
    result.textContent = "✕ 확인 요청에 실패했습니다 — 앱이 실행 중인지 확인해주세요.";
  });
}
function deleteEmailSender() {
  if (!confirm("저장된 보내는 계정을 삭제할까요? .env에 값이 있으면 그쪽으로 자동 전환되고, 둘 다 없으면 이메일은 보내지 않습니다.")) return;
  fetch("/delete-email-sender", {method: "POST"}).then(function () { location.reload(); });
}
</script>"""


def render_email_sender_page(error: str = "", attempted: Optional[dict] = None) -> str:
    """연동 › 이메일 보내는 계정 화면. attempted는 저장이 거절됐을 때 입력하던 값(비밀번호 제외)."""
    source = email_source()
    if source == "none":
        status_class = "status-warn"
        status_text = f'{icon("alert")} 아직 보내는 계정이 없습니다 — 이메일은 보내지 않습니다.'
        status_caption = "이메일을 안 쓰기로 한 상태로도 정상입니다."
    elif source == "env":
        status_class = "status-info"
        status_text = f'{icon("check")} .env의 계정을 쓰고 있습니다 — 여기에 저장하면 그 값이 우선합니다.'
        status_caption = "설정에 저장된 값이 없어 .env로 넘어간 상태입니다. 그대로 둬도 됩니다."
    else:
        status_class = "status-ok"
        status_text = f'{icon("check")} 설정에 저장된 계정을 쓰고 있습니다.'
        saved_at = email_saved_at()
        status_caption = (
            f"저장 시각: {saved_at}. 문제가 있어 보이면 아래 [시험 메일 보내기]로 확인해보세요."
            if saved_at
            else "문제가 있어 보이면 아래 [시험 메일 보내기]로 확인해보세요."
        )
    attempted = attempted or {}
    service = attempted.get("service") or email_service() or "naver"
    chips = []
    for key, spec in EMAIL_SERVICES.items():
        checked = " checked" if key == service else ""
        chips.append(
            f'<input type="radio" id="svc-{key}" name="service" value="{key}"'
            f' data-server="{spec["host"]} · 포트 {spec["port"]}"{checked}>'
            f'<label for="svc-{key}">{spec["label"]}</label>'
        )
    spec = EMAIL_SERVICES[service]
    server_text = f'보내는 서버 {spec["host"]} · 포트 {spec["port"]}'

    saved_pw = email_saved_password()
    if saved_pw:
        saved_pw_html = (
            f'<div class="saved-key"><span>{html.escape(mask_credential(saved_pw, keep_suffix=2))}</span>'
            f'<span class="tag">{html.escape(email_saved_at() or "")} 저장</span></div>'
        )
        pw_placeholder = "새 비밀번호로 바꿀 때만 입력"
    else:
        saved_pw_html = ""
        pw_placeholder = "앱 비밀번호"
    delete_button = (
        '<button type="button" class="danger" onclick="deleteEmailSender()">계정 삭제</button>'
        if source == "saved"
        else ""
    )
    return _EMAIL_SENDER_TEMPLATE.format(
        **_theme(),
        error_html=f'<p class="error">{html.escape(error)}</p>' if error else "",
        status_class=status_class,
        status_text=status_text,
        status_caption=status_caption,
        service_chips="".join(chips),
        server_text=html.escape(server_text),
        address=html.escape(attempted.get("address", email_sender_address() or "")),
        name=html.escape(attempted.get("name", email_sender_name())),
        saved_pw_html=saved_pw_html,
        pw_placeholder=pw_placeholder,
        delete_button=delete_button,
        toggle_script=_CREDENTIAL_TOGGLE_SCRIPT,
        page_script=_EMAIL_SENDER_PAGE_SCRIPT,
        index_href=_home_href(),
    )


def _known_articles_by_url(wanted_urls: set) -> dict:
    """숨긴 기사의 언론사·제목을 보여주려고, 보관 중인 회차에서 URL로 원본 정보를 찾는다
    (숨긴 목록 자체는 URL만 저장하므로).

    [수정: 2026-08-18] 예전엔 회차를 전부 읽어 URL 사전을 통째로 만들었다. 실제로
    필요한 건 wanted_urls(=옛 형식이라 outlet이 없는 기록)뿐이고 대개 0건이므로,
    최신 회차부터 읽다가 다 찾으면 즉시 멈춘다. 보관 기간을 늘려도 이 화면이
    무거워지지 않게 하려는 것(CODING_CONVENTIONS.md §1).
    """
    if not wanted_urls:
        return {}
    remaining = set(wanted_urls)
    lookup: dict = {}
    for meta in list_run_meta():  # 최신 회차부터
        run = load_run_file(meta["path"])
        if run is None:
            continue
        for article in run.get("articles", []):
            url = article.get("url")
            if url in remaining:
                lookup[url] = article
                remaining.discard(url)
        if not remaining:
            break
    return lookup


def _hide_meta_fallbacks(urls: set) -> dict:
    """숨기려는 기사들의 언론사·제목·발행시각·소제목을 서버가 직접 찾아본다(_handle_hide_article용).

    **진행 중인 초안 캐시를 먼저** 보고, 없을 때만 저장된 회차를 뒤진다 — 이 폴백이 걸리는 건
    대부분 초안에서 숨긴 기사인데, 초안 기사는 아직 회차로 저장된 적이 없어 회차 역조회로는
    못 찾고, 못 찾으면 보관 기간 전체의 회차 파일을 끝까지 읽는다. 초안 캐시와 회차 역조회는
    **한 번씩만** 한다.

    소제목 통째 숨기기가 한 요청에 기사를 모아 보내므로, 기사마다 초안 캐시를 다시 읽고
    회차 파일을 다시 뒤지면 모아 보낸 보람이 없다. 못 찾은 URL은 결과에 없다."""
    from app.preview_cache import load_preview_cache

    if not urls:
        return {}
    cache = load_preview_cache() or {}
    found = {a["url"]: a for a in cache.get("articles", []) if a.get("url") in urls}
    missing = set(urls) - set(found)
    if missing:
        found.update(_known_articles_by_url(missing))
    return found


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


# [복원: 2026-08-20] 이 상수는 2026-08-19 검색어 칩 재설계 때 같은 자리에서
# _KEYWORD_GROUPS_TEMPLATE으로 덮여 통째로 사라졌고, render_hidden_page()의 참조만
# 남아 /hidden 요청마다 NameError로 핸들러가 죽고 있었다(숨김을 되돌릴 유일한 화면이라
# 그동안 숨긴 기사를 아예 못 되돌렸다). 커밋된 마지막 정상본에서 그대로 되살린다 —
# 상단바(_HIDDEN_TOP_BAR_HTML)는 사라지지 않고 그 뒤 표기까지 갱신돼 있어 그대로 쓴다.
_HIDDEN_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>휴지통</title>
<style>"""
    + _BASE_STYLE
    + """
  /* [수정: 2026-09-02] 평평한 목록 → "한 번에 숨긴 덩어리"(app.curation.load_hidden_batches)
     묶음 카드. 정기는 소제목 통째 숨기기·체크박스 일괄 숨기기로 한 번에 수십 건이 들어와,
     한 줄씩 늘어놓으면 복구 버튼이 40개가 되고 그중 하나를 찾을 수도 없었다. */
  .hidden-row {{ display: flex; align-items: center; gap: 9px; padding: 8px 12px; font-size: var(--fs-md); border-top: 1px solid {divider_soft}; }}
  .hidden-row:first-child {{ border-top: none; }}
  .hidden-row:hover {{ background: {row_hover}; }}
  .hidden-row.sel {{ background: {hover}; }}
  .hidden-row .name {{ flex: 1; line-height: 1.45; }}
  /* [추가: 2026-09-11] 확정본·초안 기사 줄(.title-outlet)과 같은 값 — 괄호 없이 회색·조금 작은 언론사. */
  .hidden-row .title-outlet {{ color: {muted}; font-size: 0.86em; font-weight: 500; margin-right: 0.5em; }}
  /* [수정: 2026-07-26] 별도 flex 칸(오른쪽 고정폭)이었던 걸 제목 옆 인라인으로 옮겼다 —
     제목이 두 줄로 넘어가면 가운데 정렬된 시각이 줄바꿈된 글자 위에 겹쳐 보이는
     문제가 있었다. 제목 텍스트 흐름 안에 작게 끼워 넣으면 그럴 일이 없다. */
  .hidden-row .pub-time {{ color: {muted}; font-size: var(--fs-sm); white-space: nowrap; margin-left: 6px; }}
  /* [수정: 2026-09-16] ↩ 아이콘을 뗐다 — 이 앱에서 ↩는 좌하단 **되돌리기**(직전 동작
     취소)의 신호이고 여기 복구는 "이 기사 되살리기"라 뜻이 다른데, 확정본 좌하단
     팝오버에선 ↩ FAB 바로 위에 「↩ 복구」가 붙어 같은 그림이 40px 거리에 두 뜻으로
     있었다. 복구는 글자만, 원문은 아이콘만 — 앱 관례대로(글자=주 동작, 아이콘=가끔 한 번). */
  .undo-btn {{
    flex: none; display: inline-flex; align-items: center; justify-content: center; gap: 4px;
    height: 26px; padding: 0 9px; font-size: var(--fs-sm); background: transparent; color: {muted};
    border: 1px solid {border}; border-radius: var(--r-md); cursor: pointer; white-space: nowrap;
    font-family: inherit;
  }}
  .undo-btn:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  /* [수정: 2026-09-16, 2차] 행의 복구는 글자 없이 ↩ 아이콘만이다(사용자 결정) — 원문(↗)과
     나란히 같은 크기·같은 상자로 서서 오른쪽 묶음이 한 덩어리로 읽힌다. 글자를 남기는 건
     **한 번에 여러 건**을 되살리는 자리(묶음 헤더·선택 바·팝오버의 묶음 줄)뿐이다 —
     거기선 "모두"·"선택한"이 실제 정보라서다. */
  .undo-btn.ico {{ width: 30px; padding: 0; }}
  .undo-btn svg {{ width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; }}
  /* 잠긴 복구 — disabled 속성이 아니라 aria-disabled + 흐림이다. disabled 버튼은 브라우저가
     마우스 이벤트를 안 줘서 정작 "왜 못 누르는지"를 말할 title 툴팁이 안 뜬다(확정본
     「AI 기사 나누기」 잠김과 같은 방식). 숨김 유지 기간과 열람 기간이 같은 지금은 이 줄이
     나오지 않지만, 두 값이 갈리는 날 화면이 거짓말하지 않도록 남겨 둔다. */
  .undo-btn[aria-disabled="true"] {{ opacity: 0.38; cursor: default; }}
  .undo-btn[aria-disabled="true"]:hover {{ background: transparent; color: {muted}; border-color: {border}; }}
  .hid-group {{ border: 1px solid {border}; border-radius: var(--r-lg); margin-bottom: 10px; background: {card}; overflow: hidden; }}
  .hid-group > summary {{
    display: flex; align-items: center; gap: 9px; padding: 10px 12px; font-size: var(--fs-md);
    background: {bg}; cursor: pointer; list-style: none;
  }}
  .hid-group > summary::-webkit-details-marker {{ display: none; }}
  .hid-group[open] > summary {{ border-bottom: 1px solid {border}; }}
  .hid-group > summary::before {{ content: "▸"; color: {muted}; font-size: 0.72rem; width: 10px; flex: none; }}
  .hid-group[open] > summary::before {{ content: "▾"; }}
  .hid-group .gtitle {{ flex: 1; color: {text}; }}
  .hid-group .gtitle b {{ color: {header}; }}
  .hid-group .gwhen {{ color: {text_faint}; font-size: var(--fs-xs); margin-left: 5px; }}
  .hid-group .gcount {{
    flex: none; font-size: var(--fs-xs); font-weight: 700; color: {muted};
    background: {pill_bg}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  .hid-solo {{ border: 1px solid {border}; border-radius: var(--r-lg); margin-bottom: 10px; background: {card}; }}
  /* [추가: 2026-09-04] 날짜 구획 — 휴지통이 7일치를 보여주게 되면서(HIDDEN_VIEW_DAYS),
     "오늘 뺀 것"과 "지난 날 뺐던 것"이 한 목록에 섞이면 무엇을 되살릴 수 있는지가
     안 읽힌다. 오늘 구획만 펼친 채 열고(정기 보관함이 최신 날짜를 미리 펼치는 규칙과
     같다), 지난 날짜는 접어둔다. */
  .hid-day {{ margin-bottom: 14px; }}
  .hid-day > summary {{
    display: flex; align-items: center; gap: 8px; padding: 6px 2px; margin-bottom: 8px;
    font-size: var(--fs-md); font-weight: 700; color: {header}; cursor: pointer; list-style: none;
    border-bottom: 1px solid {border};
  }}
  .hid-day > summary::-webkit-details-marker {{ display: none; }}
  .hid-day > summary::before {{ content: "▸"; color: {muted}; font-size: 0.72rem; width: 10px; flex: none; }}
  .hid-day[open] > summary::before {{ content: "▾"; }}
  .hid-day .chip-today {{
    font-size: var(--fs-xs); font-weight: 600; background: {hover}; color: {accent};
    border: 1px solid {accent_border}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  .hid-day .dn {{ font-size: var(--fs-xs); font-weight: 700; color: {muted}; background: {pill_bg}; border-radius: var(--r-pill); padding: 1px 8px; }}
  /* 지난 날짜는 이미 숨김이 풀린 기록이라 되살릴 것이 없다 — 그 사실을 구획 머리에서
     한 번만 말하고, 행에는 복구 버튼 대신 원문 링크를 둔다(그 자리가 비면 왜 없는지
     설명할 곳이 사라진다). */
  .hid-day .ro {{ margin-left: auto; font-size: var(--fs-xs); font-weight: 500; color: {text_faint}; }}
  /* 원문 열기 — 확정본의 「원문보기 ↗」와 같은 외부 링크 아이콘. 이 행엔 다른 아이콘이
     없어서 2026-08-14에 확정본에서 ↗를 뺐던 이유(↗와 복사 아이콘이 17px에선 같은
     실루엣)가 여기선 걸리지 않는다. */
  .hidden-row .src {{
    flex: none; display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 26px; color: {muted}; text-decoration: none;
    border: 1px solid {border}; border-radius: var(--r-md);
  }}
  .hidden-row .src:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .hidden-row .src svg {{ width: 13px; height: 13px; fill: none; stroke: currentColor;
    stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }}
  /* 원문·복구 두 칸 묶음 — 행마다 같은 자리에 같은 것이 오도록 한 덩어리로 둔다. */
  .hidden-row .acts {{ flex: none; display: flex; align-items: center; gap: 6px; }}
  .hid-check {{ flex: none; width: 14px; height: 14px; margin: 0; cursor: pointer; }}
  /* 선택했을 때만 뜨는 일괄 복구 바 — 확정본·초안의 일괄이동 바와 같은 규칙(선택이
     없으면 자리 자체를 안 차지한다). */
  .bulk-restore {{
    display: none; position: sticky; top: 0; z-index: 5; align-items: center; gap: 8px;
    background: {hover}; border: 1px solid {accent_border}; border-radius: var(--r-lg);
    padding: 8px 12px; font-size: var(--fs-md); color: {header}; margin-bottom: 12px;
  }}
  .bulk-restore.on {{ display: flex; }}
  .bulk-restore .n {{ font-weight: 700; }}
  .bulk-restore .sp {{ flex: 1; }}
  .bulk-restore .go {{
    display: inline-flex; align-items: center; gap: 5px; background: {accent}; color: {on_fill};
    border: none; border-radius: var(--r-md); padding: 5px 11px; font-size: var(--fs-sm); font-weight: 600; cursor: pointer;
  }}
  .bulk-restore .go svg {{ width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; }}
  .bulk-restore .clr {{
    background: transparent; color: {muted}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 5px 10px; font-size: var(--fs-sm); cursor: pointer;
  }}
</style>
</head>
<body>
"""
    + _HIDDEN_TOP_BAR_HTML
    + """
<div class="container">
  <h1>🗑️ 휴지통{count_label}</h1>
  <p class="caption">🧹 숨긴 기사는 {view_days}일 동안 여기 남고, 그동안은 언제든 되살릴 수 있습니다.</p>
  <p class="hint">
    화면의 {trash_icon} 버튼으로 숨긴 기사 목록입니다. 최근에 숨긴 기사가 맨 위에 오고, 원본
    데이터는 지워지지 않으며, {undo_icon}를 누르면 그 기사가 다시 화면에 보이게 됩니다.
    한 번에 숨긴 기사는 묶음으로 접혀 있습니다 — 묶음째 되살리거나, 펼쳐서 한 건씩 고를 수 있습니다.
    {view_days}일이 지난 기록은 목록에서 사라지고, 그 기사는 다시 화면에 보이게 됩니다.
  </p>
  <div class="bulk-restore" id="bulk-restore">
    <span class="n" id="bulk-count">0개 선택됨</span>
    <span class="sp"></span>
    <button type="button" class="go" onclick="restoreSelected()">{undo_icon} 선택한 기사 복구</button>
    <button type="button" class="clr" onclick="clearRestoreSelection()">선택 해제</button>
  </div>
  {rows_html}
</div>
<script>
// [추가: 2026-09-02] 복구는 전부 이 함수 하나를 지난다 — 단건·묶음째·선택한 것 모두
// 같은 엔드포인트(/unhide-article)에 url을 여러 개 실어 보낸다. 서버가 한 번의
// 읽기-수정-쓰기로 처리하고(app.curation.unhide_articles) 되돌리기도 한 걸음으로 쌓인다.
// 실제 <form> 제출을 쓰는 건 이 화면의 다른 조작과 같은 관례이고, 서버가 303으로
// 이 화면에 되돌려 보내므로 목록이 저절로 최신 상태가 되기 때문이다.
function restoreUrls(urls) {{
  if (!urls.length) return;
  var form = document.createElement("form");
  form.method = "POST";
  form.action = "/unhide-article";
  urls.forEach(function (url) {{
    var input = document.createElement("input");
    input.type = "hidden"; input.name = "url"; input.value = url;
    form.appendChild(input);
  }});
  var back = document.createElement("input");
  back.type = "hidden"; back.name = "redirect"; back.value = "/hidden";
  form.appendChild(back);
  document.body.appendChild(form);
  form.submit();
}}
function restoreOne(btn) {{ restoreUrls([btn.dataset.url]); }}
// 잠긴 복구 — 흐리게만 두면 "왜 안 눌리지"로 끝나므로 눌렀을 때도 이유를 말한다
// (확정본 「AI 기사 나누기」 잠김과 같은 방식).
function alertExpiredRestore() {{
  alert("{view_days}일이 지나 숨김이 이미 풀린 기록이에요 — 그 기사는 다시 화면에 보입니다.");
}}
function restoreBatch(btn) {{
  var urls = JSON.parse(btn.dataset.urls || "[]");
  if (urls.length > 1 && !confirm(urls.length + "건을 한꺼번에 되살릴까요?")) return;
  restoreUrls(urls);
}}
// 묶음 헤더의 체크박스는 그 묶음 전체를 고른다 — 확정본·초안의 소제목 전체선택
// (.group-select-all)과 같은 한 방향 동작이다(개별 해제로 헤더가 풀리지는 않는다).
function toggleBatchSelection(cb) {{
  var box = cb.closest(".hid-group");
  box.querySelectorAll(".hid-check.row-check").forEach(function (rowCb) {{
    rowCb.checked = cb.checked;
    rowCb.closest(".hidden-row").classList.toggle("sel", cb.checked);
  }});
  refreshRestoreSelection();
}}
function toggleRowSelection(cb) {{
  cb.closest(".hidden-row").classList.toggle("sel", cb.checked);
  refreshRestoreSelection();
}}
function selectedRestoreUrls() {{
  return Array.prototype.slice.call(document.querySelectorAll(".hid-check.row-check:checked"))
    .map(function (cb) {{ return cb.dataset.url; }});
}}
function refreshRestoreSelection() {{
  var urls = selectedRestoreUrls();
  document.getElementById("bulk-count").textContent = urls.length + "개 선택됨";
  document.getElementById("bulk-restore").classList.toggle("on", urls.length > 0);
}}
function clearRestoreSelection() {{
  document.querySelectorAll(".hid-check").forEach(function (cb) {{ cb.checked = false; }});
  document.querySelectorAll(".hidden-row.sel").forEach(function (row) {{ row.classList.remove("sel"); }});
  refreshRestoreSelection();
}}
function restoreSelected() {{ restoreUrls(selectedRestoreUrls()); }}
{range_select_script}
</script>
</body>
</html>
"""
)


_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _hidden_date_label(date_str: str) -> str:
    """"9월 3일 (수)" — 정기 보관함(app.history_renderer._date_label)·수시 보관함과 같은 형식.
    "오늘" 여부는 별도 칩이 말하므로 이 문자열에는 안 섞는다. 값이 깨졌으면 지어내지 않고
    받은 문자열을 그대로 보여준다(빈 값이면 "날짜 모름")."""
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return html.escape(date_str or "날짜 모름")
    return f"{day.month}월 {day.day}일 ({_WEEKDAY_KR[day.weekday()]})"


def render_hidden_page() -> str:
    """숨긴 기사 목록과 되돌리기 버튼을 렌더링한다 (PRD.md 기능1 규칙 19).

    [수정: 2026-07-26] 가장 최근에 숨긴 기사가 맨 위에 오도록 정렬한다
    (app.curation.load_hidden_records가 이미 hidden_at 내림차순으로 정렬해 돌려준다).
    숨긴 시각은 화면에 따로 안 보여준다 — 최근 순 정렬 자체가 "새로 숨긴 것"을 이미
    알려주고, 자동 스크랩이 당일 발행 기사만 대상이라 "언제 숨겼는지"보다 "언제
    게시됐는지"가 더 의미 있다는 판단(놓친 기사는 어차피 담당자가 수작업으로 챙김).
    되돌리기 버튼은 글자 대신 ↩️ 아이콘만 써서 자리를 줄이고, 게시 시각은 제목 옆에
    작게 인라인으로 붙인다(별도 칸으로 두면 제목이 두 줄로 넘어갈 때 겹쳐 보였다).
    [추가: 2026-07-26] 자정이 지나면 숨김이 저절로 풀리던 시절이 있었다 — 숨김은 "오늘
    화면 정리용"이라는 판단이었고, 2026-09-04에 "기록까지 사라지면 안 된다"는 요청으로
    **열람만** HIDDEN_VIEW_DAYS(7일)로 늘렸다(그래서 오늘 구획에만 복구 버튼이 있고 지난
    날짜는 보기 전용이었다).

    [수정: 2026-09-16] **숨김 유지 기간도 같은 7일이 됐다**(사용자 결정, app.curation의
    load_hidden_urls 참고) — 휴지통에 보이는 동안은 전부 되살릴 수 있어 "보이는데 못
    되살리는 줄"이 없다. 날짜별로 나눠 그리는 것과 오늘 구획만 펼쳐 여는 것은 그대로다.

    [수정: 2026-09-02] **평평한 목록을 "한 번에 숨긴 덩어리"로 묶는다**
    (app.curation.load_hidden_batches). 정기는 소제목을 통째로 날리거나 체크박스로
    여러 건을 한꺼번에 숨기는 일이 흔해, 한 줄씩 늘어놓으면 복구 버튼이 수십 개가 되고
    그중 하나를 찾을 수도 되살릴 수도 없었다. 묶음은 카드로 접히고(가장 최근 묶음만
    펼친 채로 — 정기 보관함이 최신 날짜를 미리 펼쳐두는 것과 같은 규칙), 헤더에서 묶음째
    되살리거나 펼쳐서 한 건씩 고를 수 있다. 여러 묶음에 걸친 선택은 상단 일괄 복구 바가
    받는다. 1건짜리 묶음은 접지 않는다 — 접을 게 없는데 접으면 클릭만 는다.
    묶음 이름은 저장된 group(숨길 때의 소제목)이 그 묶음 안에서 하나로 모일 때만 붙고,
    섞였거나 없으면 "골라서 숨김"이다(load_hidden_batches 참고).
    """
    # [수정: 2026-09-04] 7일치를 읽되(HIDDEN_VIEW_DAYS) 날짜별로 나눠 그린다 —
    # 되살릴 수 있는 건 오늘 숨긴 기사뿐이고(어제 이전은 이미 자정에 숨김이 풀렸다),
    # 그 경계가 화면에서 안 보이면 담당자가 지난 날짜의 복구 버튼을 누르고 아무 일도
    # 안 일어나는 막다른 길이 생긴다.
    batches = load_hidden_batches()
    today = datetime.now().strftime("%Y-%m-%d")
    total_today = sum(len(batch["records"]) for batch in batches if batch.get("date") == today)
    if not batches:
        rows_html = '<p class="hint">숨긴 기사가 없습니다.</p>'
    else:
        # 언론사·제목이 기록에 없는 옛 항목만 회차 역조회로 채운다(아래 _row_html 주석 참고).
        need_lookup = {
            record["url"]
            for batch in batches
            for record in batch["records"]
            if not record.get("outlet")
        }
        lookup = _known_articles_by_url(need_lookup)

        def _row_html(record: dict, restorable: bool) -> str:
            url = record["url"]
            # [수정: 2026-07-27] 숨긴 기록 자체에 outlet/title/pub_date가 있으면(숨긴 시점에
            # 화면에서 그대로 실어 보낸 것) 그걸 우선 쓴다 — 정식 회차 역조회(lookup)로는
            # "다음 회차 초안"에서 숨긴 기사(아직 정식 회차로 저장된 적 없음)를 못 찾아
            # URL만 보이는 문제가 있었다. 기록에 없으면(예전 방식으로 숨긴 기록) 기존대로
            # 정식 회차 역조회로 대체한다.
            info = record if record.get("outlet") else lookup.get(url)
            escaped_url = html.escape(url, quote=True)
            # [수정: 2026-07-30] outlet_display_label이 이미 이스케이프된 HTML(매일경제는
            # 빨간 글자 span)을 돌려주므로 여기서 다시 html.escape()하지 않는다.
            label = (
                # [수정: 2026-08-14] 숨긴 기사 목록의 키가 곧 기사 URL이라 그대로 넘긴다 —
                # oid로 확정된 매일경제 기사엔 빨간 표식이 안 붙는다.
                # [수정: 2026-09-11] 괄호를 빼고 회색 언론사로 — 확정본 기사 줄과 같은 모양.
                f'<span class="title-outlet">{outlet_display_label(info["outlet"], url)}</span>'
                f'{html.escape(info["title"] or url)}'
                if info and info.get("outlet")
                else html.escape(url)
            )
            pub_hhmm = _format_pub_hhmm(info.get("pub_date")) if info else ""
            pub_html = f'<span class="pub-time">{pub_hhmm}</span>' if pub_hhmm else ""
            # [수정: 2026-09-16] **모든 행이 「원문 + 복구」 두 칸으로 같다**(사용자 결정).
            # 예전엔 오늘 행엔 복구, 지난 행엔 원문이 같은 자리에 번갈아 놓여 같은 목록의
            # 오른쪽 끝이 두 가지 말을 했다. 이제 원문은 늘 있고 복구는 되살릴 수 있을 때만
            # 눌린다 — 잠길 때도 버튼을 지우지 않고 흐리게 두고 이유를 툴팁으로 말한다
            # (aria-disabled인 이유는 위 CSS 주석 참고). 숨김 유지 기간과 열람 기간이 같은
            # 지금은 잠기는 줄이 없지만, 두 값이 갈리는 날을 위해 경로는 남겨 둔다.
            check_html = (
                f'<input type="checkbox" class="hid-check row-check" data-url="{escaped_url}" '
                'onchange="toggleRowSelection(this)">'
                if restorable
                else ""
            )
            origin_html = (
                f'<a class="src" href="{escaped_url}" target="_blank" rel="noopener" '
                f'title="원문 기사 열기" aria-label="원문 기사 열기">{icon("external_link")}</a>'
            )
            restore_html = (
                f'<button type="button" class="undo-btn ico" data-url="{escaped_url}" '
                'onclick="restoreOne(this)" title="이 기사만 되돌리기" '
                f'aria-label="이 기사만 되돌리기">{icon("undo")}</button>'
                if restorable
                else '<button type="button" class="undo-btn ico" aria-disabled="true" '
                f'title="{HIDDEN_VIEW_DAYS}일이 지나 숨김이 풀렸어요 — 되살릴 게 없습니다" '
                f'aria-label="되살릴 수 없음" onclick="alertExpiredRestore()">{icon("undo")}</button>'
            )
            return (
                '<div class="hidden-row">'
                f"{check_html}"
                f'<span class="name">{label}{pub_html}</span>'
                f'<span class="acts">{origin_html}{restore_html}</span>'
                "</div>"
            )

        def _batch_html(batch: dict, restorable: bool, open_attr: str) -> str:
            records = batch["records"]
            rows = "".join(_row_html(record, restorable) for record in records)
            if len(records) == 1:
                return f'<div class="hid-solo">{rows}</div>'
            urls_json = html.escape(json.dumps([r["url"] for r in records], ensure_ascii=False), quote=True)
            when = _format_pub_hhmm(batch.get("hidden_at"))
            when_html = f'<span class="gwhen">{when}</span>' if when else ""
            # [수정: 2026-09-16] 소제목 이름만 쓴다(사용자 결정) — "…에서 숨김"의 뒷말은
            # 휴지통이라는 화면 자체가 이미 하는 말이라 줄마다 반복될 이유가 없다.
            # 소제목이 섞였거나 없을 때만 "골라서 숨김"으로 그 자리를 대신한다.
            title_html = (
                f'<b>{html.escape(batch["group"])}</b>'
                if batch.get("group")
                else "골라서 숨김"
            )
            head_check = (
                '<input type="checkbox" class="hid-check" onclick="event.stopPropagation()" '
                'onchange="toggleBatchSelection(this)" title="이 묶음 전체 선택">'
                if restorable
                else ""
            )
            head_action = (
                f'<button type="button" class="undo-btn" data-urls="{urls_json}" '
                'onclick="event.stopPropagation(); restoreBatch(this)" '
                f'title="이 묶음을 통째로 되돌리기">{icon("undo")} 모두 복구</button>'
                if restorable
                else ""
            )
            return (
                # 가장 최근 묶음만 펼친 채로 — 나머지는 접어 목록 전체가 한눈에 들어오게 한다.
                f'<details class="hid-group"{open_attr}>'
                "<summary>"
                f"{head_check}"
                f'<span class="gtitle">{title_html}{when_html}</span>'
                f'<span class="gcount">{len(records)}건</span>'
                f"{head_action}"
                "</summary>"
                f"{rows}"
                "</details>"
            )

        # batches는 이미 최신순이라, 날짜가 바뀌는 지점에서 끊기만 하면 날짜도 최신순이 된다.
        days: list = []
        for batch in batches:
            date = batch.get("date") or ""
            if not days or days[-1][0] != date:
                days.append((date, []))
            days[-1][1].append(batch)

        # [수정: 2026-09-16] 되살릴 수 있는 날짜의 기준을 화면이 따로 계산하지 않고
        # 숨김 판정과 같은 헬퍼에서 받는다(app.curation.active_hidden_dates) — 예전엔
        # `date == today`라, 숨김 유지 기간이 바뀌어도 화면만 옛 규칙으로 남는다.
        restorable_dates = active_hidden_dates()
        blocks = []
        for date, day_batches in days:
            restorable = date in restorable_dates
            # [수정: 2026-09-16] "오늘이냐"와 "되살릴 수 있느냐"를 갈랐다 — 유지 기간이
            # 7일이 되며 지난 날짜도 되살아나므로, 예전처럼 restorable로 「오늘」 칩을
            # 그리면 7일치 전부에 「오늘」이 붙는다. 미리 펼치는 것도 오늘 구획뿐이다.
            is_today = date == today
            count = sum(len(batch["records"]) for batch in day_batches)
            # 접힌 묶음 중 **첫 번째**만 펼친다 — enumerate의 index를 쓰면 맨 위가 1건짜리
            # (접히지 않는 낱개)일 때 아무 묶음도 안 펼쳐진다.
            opened_first = False
            inner = []
            for batch in day_batches:
                open_attr = "" if (opened_first or not is_today) else " open"
                if len(batch["records"]) > 1:
                    opened_first = True
                inner.append(_batch_html(batch, restorable, open_attr))
            chip_html = '<span class="chip-today">오늘</span>' if is_today else ""
            # 유지 기간이 지나 숨김이 저절로 풀린 날짜에만 붙는다(지금 설정에선 안 나온다 —
            # 열람 기간과 유지 기간이 같은 값이라 목록에 남아 있는 동안은 전부 되살아난다).
            ro_html = "" if restorable else '<span class="ro">숨김이 풀린 기록 · 보기 전용</span>'
            blocks.append(
                # 오늘 구획만 펼친 채로 연다 — 지난 날짜는 "찾아볼 때 펼치는" 자리다.
                f'<details class="hid-day"{" open" if is_today else ""}>'
                "<summary>"
                f'<span class="d">{_hidden_date_label(date)}</span>'
                f"{chip_html}"
                f'<span class="dn">{count}건</span>'
                f"{ro_html}"
                "</summary>"
                + "".join(inner)
                + "</details>"
            )
        rows_html = "\n".join(blocks)
    return _HIDDEN_TEMPLATE.format(
        **_theme(),
        rows_html=rows_html,
        # 제목의 건수는 **오늘치**다 — 좌하단 휴지통 배지와 같은 값이어야 하고, 그 숫자가
        # 뜻하는 건 "지금 내 화면에서 빠져 있는 기사"라 지난 날짜가 섞이면 거짓말이 된다.
        count_label=f" · 오늘 {total_today}건" if total_today else "",
        view_days=HIDDEN_VIEW_DAYS,
        undo_icon=icon("undo"),
        home_href=_home_href(),
        preview_href=_preview_href(),
        scrap_href=_scrap_href(),
        trash_icon=icon("trash"),
        range_select_script=range_select_script(
            ".hid-check.row-check", ".hidden-row",
            each_js='box.closest(".hidden-row").classList.toggle("sel", box.checked);',
        ),
    )


# [추가: 2026-08-18] 🏷 라벨(PRD.md 기능10) — 정기·수시를 가로지르는 별도 축이라
# 기존 팔레트(파랑=담당자 액션/보라=AI 액션)와 겹치지 않는 앰버로 확정했다
# (MAIN_FLOW_MOCKUP.html --label-bg/--label-text 그대로, app.renderer의 라벨 CSS와
# 같은 값). 라벨 보관함(/labels)·라벨 관리(/label-manage) 두 화면이 공유한다.
_LABEL_STYLE = """
  /* [추가: 2026-09-02] 좌하단 ↩ 되돌리기 — `_label_undo_fab_html`이 예전부터
     .undo-fab 클래스를 달고 나갔는데 이 파일 어디에도 그 CSS가 없어서, 좌하단 고정
     원형 버튼이 아니라 본문 안 파란 네모 버튼으로 그려지고 있었다(라벨 보관함은 카드
     안쪽에, 라벨 관리는 화면 왼쪽 끝에 — 삽입 위치까지 두 화면이 서로 달랐다).
     값은 app/renderer.py의 .undo-fab을 그대로 가져온다. bottom을 20px이 아니라
     88px(위 칸)으로 두는 건 확정본·초안·수시에서 ↩가 늘 그 자리이기 때문이다 —
     이 두 화면엔 아래 칸을 쓰는 휴지통이 없지만, 같은 버튼이 화면마다 높이를
     바꾸는 것보다 왼쪽 아래 모서리가 비는 편이 낫다. */
  .undo-fab {{
    position: fixed; left: 20px; bottom: 88px; width: var(--fab-sm); height: var(--fab-sm); border-radius: var(--r-circle);
    background: {card}; color: {muted}; border: 1px solid {border}; font-size: 1.3rem;
    cursor: pointer; box-shadow: var(--sh-float); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .undo-fab:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .undo-fab:disabled {{ opacity: 0.5; cursor: progress; }}
  .lab-topbar {{
    display: flex; align-items: center; gap: 10px; padding-bottom: 12px;
    border-bottom: 1px solid {border}; margin-bottom: 16px; flex-wrap: wrap;
  }}
  .lab-topbar h1 {{ margin: 0; font-size: var(--fs-xl); }}
  .lab-topbar .spacer {{ margin-left: auto; }}
  a.btn.ghost {{ background: transparent; color: {muted}; border: 1px solid {border}; }}
  a.btn.ghost:hover {{ background: {hover}; color: {accent}; }}
  .lab-filter-chip {{
    display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-sm); font-weight: 600;
    background: {card}; border: 1px solid {border}; color: {text}; border-radius: var(--r-pill);
    padding: 5px 12px; margin: 0 6px 6px 0; text-decoration: none;
  }}
  .lab-filter-chip .n {{ color: {muted}; font-weight: 500; font-size: var(--fs-xs); }}
  .lab-filter-chip:hover {{ background: {label_bg}; }}
  .lab-filter-chip.on {{ background: {label_text}; border-color: {label_text}; color: {on_fill}; }}
  .lab-filter-chip.on .n {{ color: rgba(255, 255, 255, 0.75); }}
  .lab-chipbar {{ margin-bottom: 6px; }}
  .lab-listhead {{
    display: flex; align-items: center; gap: 8px; margin: 12px 0 10px; font-size: var(--fs-md); color: {header};
    font-weight: 700;
  }}
  /* [추가: 2026-09-15] 라벨 보관함 [복사][txt][xlsx] — 정기·수시 보관함의 .exp
     (app/history_renderer.py)와 같은 값이다. 한쪽을 바꾸면 이쪽도 같이 바꾼다.
     예전 xlsx 버튼은 class="btn ghost sm"이었지만 이 파일엔 a.btn.ghost(링크용)만
     있고 .sm은 아예 없어, 공용 button 규칙대로 큰 파란 채움 버튼으로 그려지고 있었다. */
  /* 모양은 확정본·초안 툴바의 가져가기 버튼(app.renderer.export_links_style)과 같다 —
     테두리·배경 없는 글자 버튼, hover에서만 accent + 밑줄. 네 자리(정기 보관함 .exp/
     .slot-export-actions, 수시 보관함 .exp, 라벨 보관함 .exp)가 같은 값이다. */
  .exp {{ display: inline-flex; gap: 2px; flex: none; margin-left: auto; }}
  .exp button {{ border: none; background: transparent; color: {text_soft}; font-weight: 500;
    border-radius: var(--r-md); padding: 4px 8px; font-size: var(--fs-sm); cursor: pointer;
    font-family: inherit; white-space: nowrap; }}
  .exp button:hover {{ background: transparent; color: {accent};
    text-decoration: underline; text-underline-offset: 3px; }}
  .exp.lg button {{ padding: 6px 8px; }}
  .arc-row {{ border: 1px solid {border}; border-radius: var(--r-lg); padding: 10px 12px; margin-bottom: 8px; }}
  .arc-row:hover {{ background: {hover}; }}
  .arc-title {{ font-size: var(--fs-md); color: {text}; }}
  .arc-meta {{
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 7px;
    font-size: var(--fs-xs); color: {muted};
  }}
  .snap-tag {{ font-size: var(--fs-xs); background: {bg}; color: {text}; border: 1px solid {border}; border-radius: var(--r-pill); padding: 1px 6px; }}
  .snap-tag.sub {{ background: {hover}; color: {header}; }}
  .snap-tag.src-reg {{ background: {bg}; color: {muted}; }}
  .snap-tag.src-adhoc {{ background: {adhoc_bg}; color: {adhoc_text}; }}
  .snap-tag.gone {{ background: {snap_gone_bg}; color: {snap_gone_text}; border-color: {snap_gone_border}; }}
  .hid-mark {{ font-size: var(--fs-xs); color: {muted}; }}
  .arc-meta a.origin-link {{ color: {accent}; text-decoration: none; }}
  .arc-labels {{ display: inline-flex; gap: 4px; margin-left: auto; flex-wrap: wrap; }}
  .lab-chip {{
    display: inline-flex; align-items: center; gap: 3px; font-size: var(--fs-xs); font-weight: 700;
    color: {label_text}; background: {label_bg}; border: 1px solid {label_border};
    border-radius: var(--r-pill); padding: 2px 8px; white-space: nowrap;
  }}
  .lab-chip form {{ display: inline; }}
  .lab-chip .x-btn {{
    background: none; border: none; padding: 0; margin: 0; color: {label_text}; opacity: 0.55;
    cursor: pointer; font-weight: 700; font-size: 0.9rem; line-height: 1;
  }}
  .lab-chip .x-btn:hover {{ opacity: 1; background: none; }}
  table.manage {{ width: 100%; border-collapse: collapse; font-size: var(--fs-md); }}
  table.manage th {{
    text-align: left; padding: 8px; border-bottom: 1px solid {border}; color: {muted};
    font-size: var(--fs-xs); font-weight: 700;
  }}
  table.manage td {{ padding: 9px 8px; border-bottom: 1px solid {border}; vertical-align: middle; }}
  table.manage tr:hover td {{ background: {hover}; }}
  table.manage td.name {{ font-weight: 700; color: {label_text}; }}
  table.manage td.cnt, table.manage td.at {{ color: {muted}; font-size: var(--fs-sm); white-space: nowrap; }}
  table.manage td.act {{ text-align: right; white-space: nowrap; }}
  table.manage td.act form {{ display: inline-flex; align-items: center; gap: 3px; margin-left: 4px; }}
  .mini {{
    font-size: var(--fs-xs); padding: 4px 9px; border-radius: var(--r-sm); cursor: pointer;
    background: {card}; border: 1px solid {border}; color: {muted};
  }}
  .mini:hover {{ background: {hover}; color: {accent}; }}
  .mini.danger:hover {{ background: {error_bg}; color: {error}; border-color: {error_border_soft}; }}
  select.merge-sel {{
    font-size: var(--fs-xs); color: {muted}; border: 1px solid {border}; border-radius: var(--r-sm);
    padding: 4px 5px; background: {card};
  }}
  input[name="new_name"] {{ font-size: var(--fs-sm); padding: 5px 8px; width: 120px; }}
  .reject-box {{
    margin: 8px 0 14px; font-size: var(--fs-sm); line-height: 1.8;
    background: {error_bg}; border: 1px solid {error_border_soft}; border-radius: var(--r-md); padding: 10px 12px;
  }}
  .reject-box .goto {{
    font-size: var(--fs-sm); font-weight: 700; padding: 4px 10px; border-radius: var(--r-sm); cursor: pointer;
    background: {label_bg}; color: {label_text}; border: 1px solid {label_border}; margin-left: 4px;
  }}
"""


def _label_hidden_urls() -> set:
    """지금 숨김 상태인 기사 URL 집합 — 라벨 보관함이 "🗑️ 숨김" 표시를 다는 데 쓴다.

    판정 함수(load_hidden_urls)를 그대로 쓰는 것이 핵심이다 — 이 표시가 뜻하는 건 "지금
    숨김 상태다"라, 판정과 다른 창을 보면 화면이 거짓말을 한다. [수정: 2026-09-16] 숨김이
    7일 유지로 바뀌며 이 표시도 자동으로 같은 7일을 본다(그 전엔 오늘 하루였다). 그보다
    오래된 기사는 숨김이 이미 풀려 표시가 안 붙는데, 라벨은 1년을 사는 값이라 어쩔 수 없는
    한계다 — 실시간 현황 등 다른 화면도 같은 한계를 이미 가지고 있다."""
    return load_hidden_urls()


def _label_undo_fab_html() -> str:
    """좌하단 ↩ 되돌리기 버튼 — 라벨 보관함(/labels)·라벨 관리(/label-manage) 공통
    (2026-08-18 q2-a 결정). app/renderer.py의 undo-fab과 같은 자리·같은 모양이지만
    별도 스택(app/label_undo.py)을 쓰므로 엔드포인트가 다르다(/undo-label).

    [주의] 이 함수의 반환값은 그대로 `_LABELS_TEMPLATE.format(body_html=...)`처럼
    다른 템플릿의 .format() 값 자리에 꽂힌다 — .format()은 이미 채워진 값 안의 중괄호를
    다시 풀어주지 않으므로, 여기서는 (템플릿 리터럴과 달리) `{{`/`}}` 이중화를 쓰지
    않고 실제 JS와 똑같이 홑겹 중괄호를 그대로 쓴다.
    """
    undo_label = label_undo_peek_label()
    if not undo_label:
        return ""
    return (
        f'<button type="button" class="undo-fab" onclick="undoLabelAction(this)" '
        f'title="{html.escape(undo_label)} 되돌리기">{icon("undo")}</button>'
        "<script>"
        "function undoLabelAction(btn) {"
        "  btn.disabled = true;"
        '  fetch("/undo-label", {method: "POST"}).then(function(res) {'
        "    if (res.ok) { location.reload(); }"
        '    else if (res.status === 409) { btn.disabled = false; alert("되돌릴 작업이 없습니다."); }'
        '    else { btn.disabled = false; alert("되돌리기에 실패했습니다. 다시 시도해주세요."); }'
        "  }).catch(function() {"
        "    btn.disabled = false;"
        '    alert("되돌리기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");'
        "  });"
        "}"
        "</script>"
    )


def _label_source_tag_html(source: dict) -> str:
    """출처 태그 — [결정: 2026-08-18] q1: 수시는 사안명만 보여준다(정기/수시가 이미
    색으로 구분되므로 "수시 ·" 접두어는 글자만 늘린다)."""
    if (source or {}).get("kind") == "adhoc":
        issue = (source or {}).get("issue") or "사안"
        return f'<span class="snap-tag src-adhoc">{html.escape(issue)}</span>'
    return '<span class="snap-tag src-reg">회차</span>'


def _render_label_archive_row(
    entry: dict, hidden_urls: set, overrides: dict, subheading_format: str, return_qs: str = ""
) -> str:
    url = entry["url"]
    escaped_url = html.escape(url)
    override_title = overrides.get(url, {}).get("title")
    title = html.escape(override_title or entry.get("title") or url)
    outlet = entry.get("outlet", "")
    outlet_html = outlet_display_label(outlet, url) if outlet else ""
    group = entry.get("group", "")
    group_html = (
        f'<span class="snap-tag sub">{apply_subheading_format(html.escape(subheading_format), html.escape(group))}</span>'
        if group
        else ""
    )
    hidden_html = '<span class="hid-mark">🗑️ 숨김</span>' if url in hidden_urls else ""
    pub_date = entry.get("pub_date", "")
    pub_html = html.escape(pub_date.replace("T", " ")[:16]) if pub_date else ""
    scrap_date = html.escape(entry.get("scrap_date", ""))
    scrap_end = html.escape(entry.get("scrap_end", ""))
    label_chips = "".join(
        '<span class="lab-chip">'
        f"{html.escape(name)} "
        f'<form method="POST" action="/remove-label">'
        f'<input type="hidden" name="url" value="{escaped_url}">'
        f'<input type="hidden" name="label" value="{html.escape(name)}">'
        f'<input type="hidden" name="redirect" value="/labels?{html.escape(return_qs)}">'
        '<button type="submit" class="x-btn" title="라벨 떼기">×</button>'
        "</form></span>"
        for name in entry.get("labels", [])
    )
    # [수정: 2026-08-18] 발행시각·언론사가 없는(레거시) 스냅샷도 있을 수 있어, 문자열을
    # 이어붙이는 방식 대신 조각을 리스트에 담아 join한다 — 삼항연산자를 문자열
    # concatenation 중간에 끼우면 그 앞 조각까지 통째로 날아가는 실수가 나기 쉽다.
    meta_parts = [_label_source_tag_html(entry.get("source")), f"<span>{scrap_date} {scrap_end}</span>"]
    if pub_html:
        meta_parts.append(f"<span>·</span><span>{pub_html} 발행</span>")
    if outlet_html:
        meta_parts.append(f"<span>·</span><span>{outlet_html}</span>")
    meta_parts.append(group_html)
    meta_parts.append(hidden_html)
    meta_parts.append(
        f'<a class="origin-link" href="{escaped_url}" target="_blank" rel="noopener noreferrer">원문보기 ↗</a>'
    )
    meta_parts.append(f'<span class="arc-labels">{label_chips}</span>')
    return (
        '<div class="arc-row">'
        f'<div class="arc-title">{title}</div>'
        f'<div class="arc-meta">{"".join(meta_parts)}</div>'
        "</div>"
    )


_LABELS_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>라벨 보관함</title>
<style>"""
    + _BASE_STYLE
    + _LABEL_STYLE
    + """
</style>
</head>
<body>
"""
    + _LABELS_TOP_BAR_HTML
    + """
<div class="container">
  <div class="lab-topbar">
    <h1>{icon_tag} 라벨 보관함</h1>
    <span class="spacer"></span>
    <a class="btn ghost" href="/label-manage">{icon_gear} 라벨 관리</a>
  </div>
  <div class="lab-chipbar">{chips_html}</div>
  {body_html}
</div>
{undo_fab_html}
<script>
// [추가: 2026-09-15] 두 보관함의 copyArchiveText와 같은 피드백(글자가 잠깐 바뀜).
function copyLabelText(btn) {{
  navigator.clipboard.writeText(btn.dataset.copyText).then(function () {{
    var original = btn.textContent;
    btn.textContent = '복사했습니다';
    setTimeout(function () {{ btn.textContent = original; }}, 900);
  }}).catch(function () {{
    alert('복사에 실패했습니다.');
  }});
}}
</script>
</body>
</html>
"""
)


# ── 발송 기록 (/send-log) ─────────────────────────────────────────────────────
# 시안 mockups/SEND_LOG_MOCKUP.html. 저장은 app.send_log. 휴지통처럼 날짜별 <details>, 오늘만
# 펼치고, 발송 한 번이 한 줄(펼치면 사람마다 결과). 필터(보고서/알림 · 실패만)는 화면에서만 거른다.
_SEND_LOG_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>발송 기록</title>
<style>"""
    + _BASE_STYLE
    + """
  .sl-filters {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 4px; }}
  .sl-chip {{ font-size: var(--fs-sm); padding: 3px 12px; border-radius: var(--r-pill); border: 1px solid {border};
    background: {card}; color: {text_soft}; cursor: pointer; font-family: inherit; height: auto; }}
  .sl-chip:hover {{ background: {row_hover}; }}
  .sl-chip.on {{ background: {hover}; border-color: {accent_border}; color: {accent}; font-weight: 600; }}
  .sl-chip.fail.on {{ background: {error_bg}; border-color: {error_border}; color: {error}; }}
  .sl-sep {{ width: 1px; height: 16px; background: {border}; margin: 0 6px; }}
  .sl-day > summary {{ display: flex; align-items: center; gap: 8px; padding: 6px 2px; margin: 18px 0 8px;
    font-size: var(--fs-md); font-weight: 700; color: {header}; cursor: pointer; list-style: none;
    border-bottom: 1px solid {border}; }}
  .sl-day > summary::-webkit-details-marker {{ display: none; }}
  .sl-day > summary::before {{ content: "▸"; color: {muted}; font-size: 0.72rem; width: 10px; flex: none; }}
  .sl-day[open] > summary::before {{ content: "▾"; }}
  .sl-day .chip-today {{ font-size: var(--fs-xs); font-weight: 600; background: {hover}; color: {accent};
    border: 1px solid {accent_border}; border-radius: var(--r-pill); padding: 1px 8px; }}
  .sl-day .dcnt {{ font-weight: 400; color: {muted}; font-size: var(--fs-sm); }}
  .sl-day .dfail {{ font-weight: 600; color: {error}; font-size: var(--fs-sm); }}
  .sl-send {{ border: 1px solid {border}; border-radius: var(--r-lg); margin: 0 0 8px; overflow: hidden; scroll-margin-top: 70px; }}
  .sl-send > summary {{ display: flex; align-items: center; gap: 9px; padding: 9px 12px; font-size: var(--fs-md);
    background: {bg}; cursor: pointer; list-style: none; }}
  .sl-send > summary::-webkit-details-marker {{ display: none; }}
  .sl-send > summary::before {{ content: "▸"; color: {muted}; font-size: 0.72rem; width: 10px; flex: none; }}
  .sl-send[open] > summary::before {{ content: "▾"; }}
  .sl-send[open] > summary {{ border-bottom: 1px solid {border}; }}
  .sl-send > summary:hover {{ background: {row_hover}; }}
  .sl-send.has-fail {{ border-color: {error_border}; }}
  .sl-send.is-focus {{ box-shadow: 0 0 0 2px {accent_border}; }}
  .sl-send .t {{ color: {muted}; font-variant-numeric: tabular-nums; flex: none; width: 3.1em; }}
  .sl-kind {{ flex: none; font-size: var(--fs-xs); font-weight: 600; border-radius: var(--r-pill); padding: 1px 8px; border: 1px solid; }}
  .k-regular {{ background: {hover}; color: {accent}; border-color: {accent_border}; }}
  .k-adhoc {{ background: {adhoc_bg}; color: {adhoc_text}; border-color: {flow_entry_adhoc_border}; }}
  .k-scoop, .k-flash, .k-burst {{ background: {error_bg}; color: {scoop_text}; border-color: {error_border}; }}
  .k-quota {{ background: {warn_bg}; color: {warn_text}; border-color: {warn_border}; }}
  .sl-send .what {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .sl-send .by {{ flex: none; color: {muted}; font-size: var(--fs-sm); }}
  .sl-send .tally {{ flex: none; font-size: var(--fs-sm); color: {muted}; font-variant-numeric: tabular-nums; }}
  .sl-send .tally b {{ font-weight: 600; color: {text}; }}
  .sl-send .tally .bad {{ color: {error}; font-weight: 700; }}
  .sl-send .mark {{ flex: none; width: 18px; text-align: center; font-weight: 700; color: {muted}; }}
  .sl-send .mark.ok {{ color: {send}; }}
  .sl-send .mark.bad {{ color: {error}; }}
  .sl-send.skipped > summary {{ color: {muted}; }}
  .sl-body {{ padding: 6px 12px 10px 31px; font-size: var(--fs-md); }}
  .sl-meta {{ color: {muted}; font-size: var(--fs-sm); margin: 4px 0 8px; line-height: 1.6; }}
  .sl-meta a {{ color: {muted}; }}
  .sl-meta a:hover {{ color: {accent}; }}
  .sl-rc {{ display: grid; grid-template-columns: minmax(0, 8em) 5.2em minmax(0, 1fr); gap: 0 12px; }}
  .sl-rc > div {{ padding: 6px 0; border-top: 1px solid {divider_soft}; line-height: 1.45; min-width: 0; overflow-wrap: anywhere; }}
  .sl-rc .h {{ border-top: none; color: {muted}; font-size: var(--fs-xs); padding: 2px 0; }}
  .sl-rc .ch {{ color: {muted}; font-size: var(--fs-sm); }}
  .st-ok {{ color: {send}; }}
  .st-bad {{ color: {error}; font-weight: 600; }}
  .st-note {{ color: {muted}; font-size: var(--fs-sm); }}
  .st-fix {{ display: block; color: {muted}; font-size: var(--fs-sm); font-weight: 400; margin-top: 2px; }}
  .st-fix a {{ color: {accent}; }}
  .sl-empty {{ color: {muted}; text-align: center; padding: 28px 0; }}
  .sl-foot {{ margin-top: 22px; color: {muted}; font-size: var(--fs-sm); text-align: center; }}
  .sl-hide {{ display: none !important; }}
  @media (max-width: 640px) {{
    .sl-send > summary {{ flex-wrap: wrap; row-gap: 2px; }}
    .sl-send .what {{ flex-basis: calc(100% - 5em); order: 5; padding-left: 19px; }}
    .sl-send .by {{ display: none; }}
    .sl-rc {{ grid-template-columns: minmax(0, 1fr); }}
    .sl-rc > .ch, .sl-rc > .h {{ display: none; }}
    .sl-rc > .res {{ border-top: none; padding-top: 0; }}
  }}
</style>
</head>
<body>
{topbar}
<div class="container">
  <h1>발송 기록</h1>
  <p class="hint">{retention_days}일 보관 · 도착했는지까지 적고, 읽었는지는 텔레그램·메일이 알려 주지 않아 적지 않습니다.</p>
  {filters_html}
  {days_html}
</div>
<script>
(function () {{
  var chips = document.querySelectorAll(".sl-chip[data-group]");
  var failChip = document.querySelector(".sl-chip.fail");
  var group = "all", failOnly = false;
  function apply() {{
    document.querySelectorAll(".sl-day").forEach(function (day) {{
      var shown = 0;
      day.querySelectorAll(".sl-send").forEach(function (row) {{
        var ok = (group === "all" || row.dataset.group === group) && (!failOnly || row.dataset.bad === "1");
        row.classList.toggle("sl-hide", !ok);
        if (ok) shown++;
      }});
      day.classList.toggle("sl-hide", shown === 0);
      // 거르는 동안엔 걸린 날짜를 펼쳐 보인다 — 접힌 날짜 속 실패를 못 보고 지나치지 않게.
      if ((group !== "all" || failOnly) && shown) day.open = true;
    }});
  }}
  chips.forEach(function (chip) {{
    chip.addEventListener("click", function () {{
      group = chip.dataset.group;
      chips.forEach(function (c) {{ c.classList.toggle("on", c === chip); }});
      apply();
    }});
  }});
  if (failChip) failChip.addEventListener("click", function () {{
    failOnly = !failOnly;
    failChip.classList.toggle("on", failOnly);
    apply();
  }});
  var focus = document.querySelector(".sl-send.is-focus");
  if (focus) focus.scrollIntoView({{ block: "start" }});
}})();
</script>
</body>
</html>
"""
)

_SEND_LOG_KIND_LABELS = {
    "regular": "정기", "adhoc": "수시", "scoop": "[단독]", "flash": "[속보]", "burst": "🔥 몰림", "quota": "한도 경고",
}


def _send_log_fix(channel: str, error: str) -> str:
    """실패 이유 밑에 붙이는 「어떻게 고치나」 한 줄(HTML). 모르는 이유면 빈 문자열."""
    if channel == "telegram":
        if "차단했습니다" in error:
            return "받는 사람이 텔레그램에서 봇 차단을 풀면 다음 발송부터 받습니다."
        if "대화를 시작하지" in error:
            return "받는 사람이 텔레그램에서 봇을 찾아 [시작]을 누르면 다음 발송부터 받습니다."
        if "(403)" in error:
            return "받는 사람이 봇을 차단했는지, 봇과 대화를 시작했는지 확인해 주세요."
        if "chat_id" in error:
            return '<a href="/telegram">텔레그램 받는 사람</a>에서 chat id가 맞는지 확인해 주세요.'
        if "(401)" in error or "토큰" in error:
            return '<a href="/telegram-sender">텔레그램 발송 계정</a>에서 봇 토큰을 확인해 주세요.'
        if "(429)" in error:
            return "텔레그램이 잠시 막았어요. 몇 분 뒤에 다시 보내면 됩니다."
    if channel == "email":
        if "인증" in error or "설정되지" in error:
            return '<a href="/email-sender">이메일 발송 계정</a>에서 주소·앱 비밀번호를 확인해 주세요.'
        if "거부" in error:
            return '<a href="/email">이메일 받는 사람</a>에서 주소가 맞는지 확인해 주세요.'
    if "연결" in error or "시간 초과" in error:
        return "인터넷 연결을 확인해 주세요."
    return ""


def _send_log_delivery_html(row: dict) -> str:
    channel = row.get("channel", "")
    channel_label = "텔레그램" if channel == "telegram" else "이메일"
    content = row.get("content", "")
    if row.get("ok"):
        notes = []
        if content:
            notes.append(content)
        if row.get("silent"):
            notes.append("🔕 알림 없이")
        if (row.get("parts") or 1) > 1:
            notes.append(f"{row['parts']}통으로 나눠 감")
        note_html = f' <span class="st-note">· {html.escape(" · ".join(notes))}</span>' if notes else ""
        result = "✓ 도착" if channel == "telegram" else "✓ 메일 서버가 받음"
        res_html = f'<span class="st-ok">{result}</span>{note_html}'
    else:
        error = row.get("error", "")
        fix = _send_log_fix(channel, error)
        fix_html = f'<span class="st-fix">{fix}</span>' if fix else ""
        res_html = f'<span class="st-bad">✕ {html.escape(error or "보내지 못했어요")}</span>{fix_html}'
    return (
        f'<div>{html.escape(row.get("name") or "")}</div>'
        f'<div class="ch">{channel_label}</div>'
        f'<div class="res">{res_html}</div>'
    )


def _send_log_entry_html(entry: dict, focus: bool) -> str:
    kind = entry.get("kind", "")
    status = entry.get("status", "")
    at = entry.get("at", "")
    hhmm = at[11:16]
    skipped = status == "skipped"
    bad = status in ("failed", "partial")
    if kind in ("regular", "adhoc"):
        by = "🤖 자동" if entry.get("auto") else "수동"
        if entry.get("send_no", 0) >= 2:
            by += f" · {entry['send_no']}회째"
        by_html = f'<span class="by">{by}</span>'
    else:
        by_html = ""
    if skipped:
        tally_html = '<span class="tally">보내지 않음</span>'
        mark_html = '<span class="mark">–</span>'
    else:
        parts = []
        for label, ok, total in send_log.tally(entry):
            ok_html = f'<span class="bad">{ok}</span>' if ok < total else f"<b>{ok}</b>"
            parts.append(f"{label} {ok_html}/<b>{total}</b>")
        tally_html = f'<span class="tally">{" · ".join(parts)}</span>'
        mark_html = '<span class="mark bad">✕</span>' if bad else '<span class="mark ok">✓</span>'
    title = entry.get("title", "")
    if skipped and entry.get("note"):
        title = f"{title} · {entry['note']}"
    meta = []
    if entry.get("repeat", 1) > 1:
        meta.append(
            f"자동발송이 같은 결과로 {entry['repeat']}번 시도했어요 · 마지막 {html.escape(entry.get('last_at', '')[11:16])}"
        )
    if entry.get("note") and not skipped:
        meta.append(html.escape(entry["note"]))
    articles = entry.get("articles") or []
    if articles:
        links = "<br>· ".join(
            f'<a href="{html.escape(a.get("url", ""), quote=True)}" target="_blank" rel="noopener">'
            f'{html.escape(a.get("outlet", ""))} {html.escape(a.get("title", ""))}</a>'
            for a in articles
        )
        meta.append(f"보낸 기사 · {links}")
    if kind == "regular" and skipped:
        meta.append("확정본에서 (발송)을 누르면 이 아래에 새 줄로 남습니다.")
    meta_html = "".join(f'<div class="sl-meta">{m}</div>' for m in meta)
    rows = entry.get("deliveries") or []
    grid_html = (
        '<div class="sl-rc"><div class="h">이름</div><div class="h ch">채널</div><div class="h">결과</div>'
        + "".join(_send_log_delivery_html(r) for r in rows)
        + "</div>"
        if rows else ""
    )
    body_html = f'<div class="sl-body">{meta_html}{grid_html}</div>' if (meta_html or grid_html) else ""
    group = "report" if kind in send_log.REPORT_KINDS else "alert"
    classes = "sl-send" + (" has-fail" if bad else "") + (" skipped" if skipped else "") + (" is-focus" if focus else "")
    return (
        f'<details class="{classes}" data-group="{group}" data-bad="{1 if bad else 0}"'
        f'{" open" if (focus or (bad and body_html)) else ""}>'
        "<summary>"
        f'<span class="t">{html.escape(hhmm)}</span>'
        f'<span class="sl-kind k-{html.escape(kind)}">{_SEND_LOG_KIND_LABELS.get(kind, html.escape(kind))}</span>'
        f'<span class="what" title="{html.escape(title, quote=True)}">{html.escape(title)}</span>'
        f"{by_html}{tally_html}{mark_html}"
        "</summary>"
        f"{body_html}"
        "</details>"
    )


def render_send_log_page(query: Optional[dict] = None) -> str:
    """발송 기록. query의 run(정기 회차 키)·card(수시 확정본 id)가 오면 그 보고서의 가장 최근
    발송 줄을 펼치고 그 날짜를 연다 — 확정본의 「발송 완료」·정기 보관함의 발송 점이 여기로 온다."""
    query = query or {}
    want_run = (query.get("run") or [""])[0]
    want_card = (query.get("card") or [""])[0]
    days = send_log.load_days()
    today = datetime.now().strftime("%Y-%m-%d")

    focus_id = None
    if want_run or want_card:
        for date_str, entries in days:
            for i, e in enumerate(entries):
                if (want_run and e.get("run_key") == want_run) or (want_card and e.get("card_id") == want_card):
                    focus_id = (date_str, i)
                    break
            if focus_id:
                break

    blocks = []
    fail_total = 0
    for date_str, entries in days:
        fails = sum(1 for e in entries if e.get("status") in ("failed", "partial"))
        fail_total += fails
        inner = "".join(
            _send_log_entry_html(e, focus_id == (date_str, i)) for i, e in enumerate(entries)
        )
        is_today = date_str == today
        open_day = is_today or (focus_id and focus_id[0] == date_str)
        blocks.append(
            f'<details class="sl-day"{" open" if open_day else ""}>'
            "<summary>"
            f"{_hidden_date_label(date_str)}"
            + ('<span class="chip-today">오늘</span>' if is_today else "")
            + f'<span class="dcnt">{len(entries)}회</span>'
            + (f'<span class="dfail">· 실패 {fails}</span>' if fails else "")
            + "</summary>"
            + inner
            + "</details>"
        )
    if blocks:
        days_html = "\n".join(blocks) + (
            f'<p class="sl-foot">{_hidden_date_label(days[-1][0])}부터 남아 있어요 · '
            f"{SEND_LOG_RETENTION_DAYS}일이 지나면 저절로 지워집니다</p>"
        )
        filters_html = (
            '<div class="sl-filters">'
            '<button type="button" class="sl-chip on" data-group="all">전체</button>'
            '<button type="button" class="sl-chip" data-group="report">보고서</button>'
            '<button type="button" class="sl-chip" data-group="alert">알림</button>'
            + (
                f'<span class="sl-sep"></span><button type="button" class="sl-chip fail">실패만 ({fail_total})</button>'
                if fail_total else ""
            )
            + "</div>"
        )
    else:
        days_html = '<p class="sl-empty">아직 보낸 기록이 없어요. 확정본을 보내거나 [단독]·[속보] 알림이 나가면 여기에 쌓입니다.</p>'
        filters_html = ""
    return _SEND_LOG_TEMPLATE.format(
        **_theme(),
        topbar=_TOP_BAR_HTML,
        retention_days=SEND_LOG_RETENTION_DAYS,
        filters_html=filters_html,
        days_html=days_html,
    )


def render_labels_page(selected: Optional[list] = None) -> str:
    """정기·수시를 가로질러 라벨이 붙은 기사만 모아 보는 화면 (PRD.md 기능10 규칙5).

    [결정: 2026-08-18] 칩은 다중 선택 + AND다 — 라벨은 한 기사에 여러 축(주제·용도
    등)이 동시에 겹쳐 붙는 직교 태그라, OR로 넓히면 서로 무관한 결과가 섞여 실제로
    쓸 데가 없다("세제"+"보고서"를 같이 고르는 이유 자체가 그 교집합을 좁혀 보려는
    것). app.labels.articles_for_labels_and가 실제 필터링을 맡는다.
    """
    selected = [s for s in (selected or []) if s]
    stats = label_stats()
    chip_parts = []
    for name, s in stats.items():
        is_on = name in selected
        new_selection = [n for n in selected if n != name] if is_on else selected + [name]
        query = "&".join(f"label={quote(n)}" for n in new_selection)
        href = f"/labels?{query}" if query else "/labels"
        chip_parts.append(
            f'<a class="lab-filter-chip{" on" if is_on else ""}" href="{href}">'
            f'{html.escape(name)}<span class="n">{s["count"]}</span></a>'
        )
    chips_html = "".join(chip_parts) if chip_parts else ""

    if not stats:
        body_html = '<div class="empty" style="text-align:center;padding:40px 0;">💤<p class="hint">아직 라벨을 붙인 기사가 없습니다. 정기 초안·확정본, 수시 확정본 화면에서 기사의 🏷 버튼으로 붙일 수 있어요.</p></div>'
    elif not selected:
        body_html = '<p class="hint">라벨을 하나 이상 골라 보세요.</p>'
    else:
        valid_selected = [n for n in selected if n in stats]
        rows = articles_for_labels_and(valid_selected)
        settings = load_settings()
        subheading_format = settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE)
        overrides = load_summary_overrides()
        hidden_urls = _label_hidden_urls()
        if not rows:
            body_html = '<div class="empty" style="text-align:center;padding:40px 0;">💤<p class="hint">이 조합에 걸리는 기사가 없어요.</p></div>'
        else:
            names_display = " + ".join(html.escape(n) for n in valid_selected)
            return_qs = "&".join(f"label={quote(n)}" for n in valid_selected)
            # [추가: 2026-08-18] 엑셀 내보내기(PRD.md 기능11) — 지금 화면에 뜬 rows를
            # 그대로 8열로 옮긴다. 기사제목은 화면과 똑같이 원문 교정(summary_overrides)이
            # 있으면 그 값을 우선한다(_render_label_archive_row와 같은 규칙). 소제목은
            # 스냅샷 시점 값 그대로(기능10 규칙6-1) — display 포맷을 입히지 않는다(데이터라서).
            # 날짜는 "뽑은 날"을 쓴다(기능11 규칙7 — 라벨은 소속 날짜가 없어 그대로 쓰면
            # 파일명이 겹친다).
            excel_rows = [
                {
                    "스크랩일자": r.get("scrap_date", ""),
                    "스크랩종료시간": r.get("scrap_end", ""),
                    "발행일 발행시간": format_pub_datetime(r.get("pub_date")),
                    "언론사명": r.get("outlet", ""),
                    "기사제목": overrides.get(r["url"], {}).get("title") or r.get("title") or r["url"],
                    "URL": r["url"],
                    "소제목": r.get("group", ""),
                    "라벨명": ", ".join(r.get("labels", [])),
                }
                for r in rows
            ]
            export_base = (
                f"{datetime.now().strftime('%Y-%m-%d')}_라벨_"
                f"{sanitize_filename_part('+'.join(valid_selected))}"
            )
            # [추가: 2026-09-15] 복사·txt — 두 보관함과 같은 [복사][txt][xlsx] 묶음(.exp).
            # 텍스트는 소제목별 복사와 **같은 빌더**(build_group_copy_texts)에 "고른 라벨
            # 이름을 소제목 이름으로 둔 그룹 하나"를 넘겨 만든다 — 머리줄에 담당자의
            # 소제목 형식이 입혀지고(<사진 아님>), 기사 줄 형식·기사 사이 빈 줄이 다른
            # 화면과 저절로 같아진다. 소제목으로 나누지 않는다(라벨 자체가 묶음이고,
            # 회차마다 AI가 이름을 새로 지어 나누면 같은 사안이 쪼개진다). 제목은
            # 화면·xlsx와 같은 원문 교정값, 순서도 화면 그대로(발행 최신순).
            copy_group_name = " + ".join(valid_selected)
            copy_text = build_group_copy_texts(
                "",
                [{
                    "name": copy_group_name,
                    "articles": [
                        {"outlet": r.get("outlet", ""), "title": x["기사제목"], "url": r["url"]}
                        for r, x in zip(rows, excel_rows)
                    ],
                }],
                settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE),
                {},
                subheading_format,
            )[copy_group_name]
            copy_attr = html.escape(copy_text)
            export_form_html = (
                '<span class="exp">'
                f'<button type="button" onclick="copyLabelText(this)" data-copy-text="{copy_attr}">복사</button>'
                '<form method="POST" action="/download-text" style="display:contents">'
                f'<input type="hidden" name="filename" value="{html.escape(export_base)}.txt">'
                f'<input type="hidden" name="text" value="{copy_attr}">'
                '<button type="submit">텍스트</button>'
                "</form>"
                '<form method="POST" action="/download-excel" style="display:contents">'
                f'<input type="hidden" name="filename" value="{html.escape(export_base)}.xlsx">'
                f'<input type="hidden" name="rows" value="{html.escape(json.dumps(excel_rows, ensure_ascii=False))}">'
                '<button type="submit">엑셀</button>'
                "</form>"
                "</span>"
            )
            body_html = (
                f'<div class="lab-listhead">{names_display} · {len(rows)}건 {export_form_html}</div>'
                + "".join(
                    _render_label_archive_row(r, hidden_urls, overrides, subheading_format, return_qs)
                    for r in rows
                )
            )
    undo_fab_html = _label_undo_fab_html()
    return _LABELS_TEMPLATE.format(
        **_theme(),
        icon_tag=icon("tag"),
        icon_gear=icon("gear"),
        chips_html=chips_html,
        body_html=body_html,
        undo_fab_html=undo_fab_html,
        index_href=_home_href(),
    )


def _render_label_manage_row(name: str, entry_stats: dict, stats: dict) -> str:
    esc_name = html.escape(name)
    others = [n for n in stats if n != name]
    merge_options = "".join(
        f'<option value="{html.escape(n)}">{html.escape(n)} ({stats[n]["count"]}건)</option>' for n in others
    )
    merge_html = (
        f'<form method="POST" action="/merge-label" data-count="{entry_stats["count"]}" '
        f'onsubmit="return confirmMerge(this, \'{esc_name}\')">'
        f'<input type="hidden" name="src" value="{esc_name}">'
        f'<select name="dst" class="merge-sel" required>'
        '<option value="" selected disabled>⇢ 합치기…</option>'
        f"{merge_options}"
        "</select>"
        '<button type="submit" class="mini">합치기</button>'
        "</form>"
        if others
        else ""
    )
    last_used = html.escape((entry_stats.get("last_used") or "")[:10])
    return (
        "<tr>"
        f'<td class="name">{esc_name}</td>'
        f'<td class="cnt">{entry_stats["count"]}건</td>'
        f'<td class="at">{last_used}</td>'
        '<td class="act">'
        f'<form method="POST" action="/rename-label" style="display:inline-flex;gap:3px;">'
        f'<input type="hidden" name="old" value="{esc_name}">'
        f'<input type="text" name="new_name" value="{esc_name}" required>'
        '<button type="submit" class="mini">✏️ 이름 변경</button>'
        "</form>"
        f"{merge_html}"
        f'<form method="POST" action="/delete-label" data-count="{entry_stats["count"]}" '
        f'data-orphan="{entry_stats["orphan_count"]}" onsubmit="return confirmDelete(this, \'{esc_name}\')">'
        f'<input type="hidden" name="name" value="{esc_name}">'
        '<button type="submit" class="mini danger">🗑 삭제</button>'
        "</form>"
        "</td>"
        "</tr>"
    )


_LABEL_MANAGE_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>라벨 관리</title>
<style>"""
    + _BASE_STYLE
    + _LABEL_STYLE
    + """
</style>
</head>
<body>
"""
    + _LABEL_MANAGE_TOP_BAR_HTML
    + """
<div class="container">
  <div class="lab-topbar">
    <h1>{icon_gear} 라벨 관리</h1>
  </div>
  <p class="hint">
    라벨을 붙이는 곳은 기사 행이고, 라벨 자체를 손보는 곳은 여기 하나입니다.
    이름 변경 · 합치기 · 삭제를 각각 따로 둡니다 — 이름 변경이 몰래 병합까지 하지 않습니다.
  </p>
  {error_html}
  {table_html}
</div>
<script>
function confirmMerge(form, srcName) {{
  var dstText = form.querySelector("select option:checked").textContent;
  var srcCount = form.dataset.count;
  return confirm(
    '"' + srcName + '"(' + srcCount + '건)를 ' + dstText + '에 합칠까요?\\n\\n' +
    '합친 뒤에는 "' + srcName + '" 라벨이 사라지고, 기사는 그대로 남은 채 라벨만 옮겨 붙습니다.'
  );
}}
function confirmDelete(form, name) {{
  var count = form.dataset.count;
  var orphan = form.dataset.orphan;
  var msg = '"' + name + '" 라벨을 지울까요?\\n\\n기사 ' + count + '건에서 이 라벨이 떨어집니다.';
  if (parseInt(orphan, 10) > 0) {{
    msg += ' 그중 ' + orphan + '건은 다른 라벨이 없어 라벨 보관함에서도 사라지고, 1년 자동 삭제 예외도 함께 풀립니다.';
  }}
  return confirm(msg);
}}
</script>
{undo_fab_html}
</body>
</html>
"""
)


def render_label_manage_page(error: Optional[str] = None, error_old: str = "", error_new: str = "") -> str:
    """라벨 이름 변경·합치기·삭제 — 라벨 보관함 안의 별도 페이지 (2026-08-18 q2 결정).

    이름 변경은 이름만 바꾼다. 이미 있는 이름과 부딪히면(app.labels.rename_label이
    LabelCollisionError를 던짐) 이 화면으로 다시 돌아와 거절 배너 + "⇢ 합치기" 버튼을
    보여준다 — app.curation의 소제목 이름 충돌(409)과 같은 규칙, 다만 여기는 합치기로
    건너뛸 길을 함께 열어둔다.
    """
    stats = label_stats()
    error_html = ""
    if error == "rename_collision":
        error_html = (
            '<div class="reject-box">'
            f"❌ 이미 &quot;{html.escape(error_new)}&quot;라는 라벨이 있습니다. 이름만 바꾸려면 다른 이름을 쓰세요.<br>"
            "두 라벨을 하나로 합치려던 것이라면 → "
            f'<form method="POST" action="/merge-label" style="display:inline">'
            f'<input type="hidden" name="src" value="{html.escape(error_old)}">'
            f'<input type="hidden" name="dst" value="{html.escape(error_new)}">'
            f'<button type="submit" class="goto">⇢ {html.escape(error_new)}와 합치기</button>'
            "</form></div>"
        )
    if not stats:
        table_html = '<p class="hint">아직 라벨이 없습니다.</p>'
    else:
        rows_html = "".join(_render_label_manage_row(name, s, stats) for name, s in stats.items())
        table_html = (
            '<table class="manage"><tr><th>라벨</th><th>기사</th><th>최근 사용</th><th></th></tr>'
            f"{rows_html}</table>"
        )
    undo_fab_html = _label_undo_fab_html()
    return _LABEL_MANAGE_TEMPLATE.format(
        **_theme(),
        icon_gear=icon("gear"),
        error_html=error_html,
        table_html=table_html,
        undo_fab_html=undo_fab_html,
        index_href=_home_href(),
    )


logger = logging.getLogger(__name__)


def _resolve_round_id(form: dict) -> Optional[tuple]:
    """이름표(rename-group)·소제목 순서(save-group-order) 저장이 "이번 회차"로 지정할
    round_id를 정한다. [추가: 2026-08-14]

    두 이름표/순서 파일이 이제 회차 단위로 저장되므로(사용자 요청 — "소제목은 그날그날
    다르게 쓰여도 된다"), 이 요청이 어느 회차를 위한 것인지 알아야 한다. 확정본·초안
    어느 화면에서 눌렀든 같은 엔드포인트로 온다(전에는 전역 파일이라 몰라도 됐다):

      - form에 run_slot이 있으면(확정본 JS가 #keyword-note-zone의 data-run-slot을
        같이 보낸다) 그 회차로 확정한다 — 확정본은 이미 끝난 회차를 보여주는 화면이라
        자동 판단에 맡기면 그새 시작된 다음 회차로 잘못 붙을 수 있다
        (app.manual_keyword_note._current_round_key와 정확히 같은 이유·같은 패턴).
      - 없으면(초안 화면) "지금 진행 중인 회차"로 자동 판단한다.

    판단할 회차 자체가 없으면(오늘 회차가 하나도 없음) None — 호출부는 이 경우
    round_id=None(회차 개념 없는 전용 버킷)으로 그냥 진행한다.
    """
    run_slot = form.get("run_slot", [""])[0].strip()
    if run_slot:
        return (datetime.now().strftime("%Y-%m-%d"), run_slot)
    slot = next_pending_slot(datetime.now())
    if slot is not None:
        return round_id_for_slot(slot)
    run = load_latest_run()
    return round_id_for_run(run) if run is not None else None


def _original_url_index() -> dict:
    """{원문 주소 키: 네이버 기사 주소} — 오늘 앱이 본 네이버 기사 전부에서 만든다.

    「+ 수기로 기사 추가」가 언론사 원문 주소를 받았을 때 수집이 저장한 네이버 주소로 바꿔
    읽는 데 쓴다. 재료: 오늘 저장된 회차 · 지금 초안에 붙잡힌 기사 · 담아둔 기사 · 전체 기사
    캐시(숨긴 기사도 여기 있다). 원문 주소(original_url)는 수집 때 네이버 API가 준 값이라
    그 필드가 생기기 전(2026-09-22 이전)에 모은 기사엔 없다.
    """
    from app.draft_seen import _read as _read_draft_seen
    from app.live_cache import load_live_cache
    from app.storage import load_today_runs

    pool = []
    for run in load_today_runs(datetime.now().strftime("%Y-%m-%d")):
        pool.extend(run.get("articles") or [])
    pool.extend((_read_draft_seen().get("articles") or {}).values())
    pool.extend(load_manual_articles())
    for arts in ((load_live_cache() or {}).get("keyword_articles") or {}).values():
        pool.extend(arts)
    index = {}
    for a in pool:
        if isinstance(a, dict) and a.get("original_url") and a.get("url"):
            for key in original_url_keys(a["original_url"]):
                index.setdefault(key, a["url"])
    return index

class _SettingsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in _STATIC_HTML_ROUTES:
            self._respond_static_html(_STATIC_HTML_ROUTES[self.path]())
        elif self.path.startswith("/history/day?"):
            self._handle_history_day()
        elif LOGO_PATH.exists() and self.path == f"/{LOGO_PATH.name}":
            self._respond_static_binary(LOGO_PATH, "image/svg+xml")
        elif self.path == "/":
            self._respond(render_menu_page())
        elif self.path == "/keywords":
            self._respond(render_keywords_page(load_settings()))
        elif self.path == "/outlets":
            self._respond(render_outlets_page(load_settings()))
        elif self.path == "/highlight":
            # 형광펜 설정 화면은 없앴다 — 확정본·초안·전체 기사 좌측 하단 🖍️가 같은 일을 한다.
            self._redirect("/")
        elif self.path == "/format":
            self._respond(render_format_page(load_settings()))
        elif self.path == "/subheading-format":
            self._respond(render_subheading_format_page(load_settings()))
        elif self.path == "/schedule":
            self._respond(render_schedule_page(load_settings()))
        elif self.path == "/hidden":
            self._respond(render_hidden_page())
        elif self.path == "/send-log" or self.path.startswith("/send-log?"):
            self._respond(render_send_log_page(parse_qs(urlsplit(self.path).query)))
        elif self.path == "/trend" or self.path.startswith("/trend?"):
            self._respond(render_trend_page(parse_qs(urlsplit(self.path).query)))
        elif self.path == "/labels" or self.path.startswith("/labels?"):
            query = parse_qs(urlsplit(self.path).query)
            self._respond(render_labels_page(query.get("label", [])))
        elif self.path == "/label-manage" or self.path.startswith("/label-manage?"):
            query = parse_qs(urlsplit(self.path).query)
            self._respond(
                render_label_manage_page(
                    error=query.get("error", [None])[0],
                    error_old=query.get("old", [""])[0],
                    error_new=query.get("new", [""])[0],
                )
            )
        elif self.path == "/wordcloud-exclude" or self.path.startswith("/wordcloud-exclude?"):
            self._respond(render_wordcloud_exclude_page(load_settings()))
        elif self.path == "/auto-send":
            self._respond(render_auto_send_settings(load_settings()))
        elif self.path == "/format-adhoc":
            self._respond(render_format_page(load_settings(), flow="adhoc"))
        elif self.path == "/subheading-format-adhoc":
            self._respond(render_subheading_format_page(load_settings(), flow="adhoc"))
        elif self.path == "/telegram":
            self._respond(render_telegram_settings(load_settings()))
        elif self.path == "/telegram-sender" or self.path.startswith("/telegram-sender?"):
            # 저장 직후 봇 이름 결과를 한 번 보여준다(bn=ok|err, msg=문구).
            query = parse_qs(urlsplit(self.path).query)
            kind = query.get("bn", [""])[0]
            notice = (kind, query.get("msg", [""])[0]) if kind in ("ok", "err") else None
            self._respond(render_telegram_sender_page(bot_name_notice=notice))
        elif self.path == "/breaking-alert":
            self._respond(render_breaking_alert_settings())
        elif self.path == "/email":
            self._respond(render_email_settings(load_settings()))
        elif self.path == "/naver":
            self._respond(render_naver_settings_page())
        elif self.path == "/llm":
            self._respond(render_llm_settings_page())
        elif self.path == "/email-sender":
            self._respond(render_email_sender_page())
        elif self.path.startswith("/adhoc"):
            # [추가: 2026-08-13] 수시 모니터링(비정기 스크랩) — app/adhoc/*는 이 한 지점
            # 말고는 정기 코드를 건드리지 않는다(app/adhoc/* -> app/* 단방향 의존,
            # ADHOC_DESIGN.md §4). 여기서 처리 못하는 하위 경로면 기존과 동일하게 404.
            if not adhoc_routes.handle_get(self, self.path):
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_history_day(self) -> None:
        """지난 기사 화면의 지연 로딩 — 오래된 날짜(EAGER_HISTORY_DAYS 밖)를 실제로 펼칠
        때만 그 하루치를 렌더링해 돌려준다(app.history_renderer.render_history_day_fragment,
        JS loadHistoryDay가 fetch로 부른다). 그 날짜에 저장된 회차가 없으면(보관 기간이
        막 지났거나 잘못된 요청) 404 — holder는 그대로 남아 사용자가 다시 펼쳐볼 수 있다.
        """
        query = parse_qs(urlsplit(self.path).query)
        date_str = query.get("date", [""])[0]
        settings = load_settings()
        fragment = render_history_day_fragment(
            date_str,
            highlight_words=settings["highlight_keywords"],
            line_template=settings["article_line_template"],
            subheading_format=settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE),
        )
        if fragment is None:
            self.send_response(404)
            self.end_headers()
            return
        self._respond(fragment)

    def _handle_delete_history_runs(self, form: dict) -> None:
        """정기 보관함의 줄 끝 🗑(하나)와 [선택 삭제](여러 개)가 같이 쓴다 — fetch로 부르고,
        지운 회차마다 그 자리에 들어갈 「삭제함 · 되살리기」 줄을 돌려준다(화면이 그 줄만
        갈아 끼우므로 새로고침·스크롤 튐이 없다). 한 요청에서 지운 것은 같은 시각을 붙인다.

        지우면 안 되는 회차(오늘·가장 최근 — app.storage.run_lock_reason)는 건너뛰고 이유를
        failed에 담는다. 화면이 애초에 🗑를 안 그리지만 열어둔 옛 탭에서 눌릴 수 있다.
        """
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        rows, failed = {}, []
        for key in form.get("key", []):
            date_str, _, run_slot = key.partition("|")
            try:
                deleted = delete_run(date_str, run_slot, stamp=stamp)
            except RunLockedError as e:
                failed.append(
                    f"{date_str} {run_slot} — "
                    + ("오늘 회차는 지울 수 없어요." if e.reason == "today" else "가장 최근 회차는 지울 수 없어요.")
                )
                continue
            if not deleted:
                failed.append(f"{date_str} {run_slot} — 이미 없는 회차예요.")
                continue
            row = render_deleted_slot_fragment(date_str, run_slot)
            if row:
                rows[key] = row
        if rows:
            self._regenerate_screens()
        self._respond_json({"rows": rows, "failed": failed})

    def _handle_restore_history_run(self, form: dict) -> None:
        """정기 보관함 「삭제함」 줄의 되살리기 — 되살린 회차 줄(펼치기 전 모양)을 돌려준다."""
        restored = restore_run(form.get("name", [""])[0])
        if restored is None:
            self._respond_json(
                {"error": "되살리지 못했어요 — 이미 되살렸거나, 같은 날짜·회차가 보관함에 있어요."}, status=409
            )
            return
        self._regenerate_screens()
        key = f"{restored['date']}|{restored['run_slot']}"
        self._respond_json({"key": key, "html": render_slot_fragment(restored["date"], restored["run_slot"]) or ""})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        # keep_blank_values=True — 기본값(False)이면 빈 문자열 값을 가진 필드를 통째로
        # 빼버려서, "이 칸이 실제로 화면에서 지워졌는지"와 "칸은 있는데 아직 안 채웠는지"를
        # 구분할 수 없게 된다(예: "+ 추가" 핸들러들의 `f"...{i}..." in form` 존재 확인이
        # 빈 칸을 del로 지운 것처럼 잘못 판단함).
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

        if self.path == "/download-text":
            self._handle_download_text(form)
        elif self.path == "/download-excel":
            self._handle_download_excel(form)
        elif self.path == "/download-excel-history":
            self._handle_download_excel_history(form)
        elif self.path == "/history/delete-runs":
            self._handle_delete_history_runs(form)
        elif self.path == "/history/restore-run":
            self._handle_restore_history_run(form)
        elif self.path == "/trend/add-word":
            self._handle_add_trend_word(form)
        elif self.path == "/trend/remove-word":
            self._handle_remove_trend_word(form)
        elif self.path == "/trend/download-excel":
            self._handle_download_trend_excel(form)
        elif self.path == "/save":
            self._handle_save_keywords(form)
        elif self.path == "/save-outlets":
            self._handle_save_outlets(form)
        elif self.path == "/move-outlet":
            self._handle_move_outlet(form)
        elif self.path == "/keywords/toggle-highlight":
            self._handle_toggle_highlight(form)
        elif self.path == "/keywords/cycle-highlight-color":
            self._handle_cycle_highlight_color(form)
        elif self.path == "/save-format":
            self._handle_save_format(form)
        elif self.path == "/save-subheading-format":
            self._handle_save_subheading_format(form)
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
        elif self.path == "/save-auto-send":
            self._handle_save_auto_send_settings(form)
        elif self.path == "/save-format-adhoc":
            self._handle_save_format(form, flow="adhoc")
        elif self.path == "/save-subheading-format-adhoc":
            self._handle_save_subheading_format(form, flow="adhoc")
        elif self.path == "/add-manual-article":
            self._handle_add_manual_article(form)
        elif self.path == "/add-article-by-url":
            self._handle_add_article_by_url(form)
        elif self.path == "/unpin-manual-article":
            self._handle_unpin_manual_article(form)
        elif self.path == "/promote-manual-article":
            self._handle_promote_manual_article(form)
        elif self.path == "/promote-manual-article-to-draft":
            self._handle_promote_manual_article_to_draft(form)
        elif self.path == "/promote-all-manual-to-draft":
            self._handle_promote_all_manual_to_draft()
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
        elif self.path == "/assign-unclassified":
            self._handle_assign_unclassified()
        elif self.path == "/cut-round":
            self._handle_cut_round(form)
        elif self.path == "/cut-round-cancel":
            self._handle_cut_round_cancel(form)
        elif self.path == "/split-group":
            self._handle_split_group(form)
        elif self.path == "/split-group-final":
            self._handle_split_group_final(form)
        elif self.path == "/regenerate-subheadings-draft":
            self._handle_regenerate_subheadings_draft()
        elif self.path == "/regenerate-subheadings-final":
            self._handle_regenerate_subheadings_final()
        elif self.path == "/undo":
            self._handle_undo()
        elif self.path == "/add-label":
            self._handle_add_label(form)
        elif self.path == "/remove-label":
            self._handle_remove_label(form)
        elif self.path == "/rename-label":
            self._handle_rename_label(form)
        elif self.path == "/merge-label":
            self._handle_merge_label(form)
        elif self.path == "/delete-label":
            self._handle_delete_label(form)
        elif self.path == "/undo-label":
            self._handle_undo_label()
        elif self.path == "/add-custom-group":
            self._handle_add_custom_group(form)
        elif self.path == "/remove-custom-group":
            self._handle_remove_custom_group(form)
        elif self.path == "/save-telegram":
            self._handle_save_telegram_settings(form)
        elif self.path == "/save-breaking-alert":
            self._handle_save_breaking_alert_settings(form)
        elif self.path == "/telegram-send-draft":
            self._handle_telegram_send_draft(form)
        elif self.path == "/telegram-send-scrap":
            self._handle_telegram_send_scrap(form)
        elif self.path == "/save-group-order":
            self._handle_save_group_order(form)
        elif self.path == "/save-email":
            self._handle_save_email_settings(form)
        elif self.path == "/email/add-recipient-slot":
            self._handle_add_email_recipient_slot(form)
        elif self.path == "/email-send-draft":
            self._handle_email_send_draft(form)
        elif self.path == "/email-send-scrap":
            self._handle_email_send_scrap(form)
        elif self.path == "/telegram/add-recipient-slot":
            self._handle_add_telegram_recipient_slot(form)
        elif self.path == "/send-scrap":
            self._handle_send_scrap(form)
        elif self.path == "/save-naver":
            self._handle_save_naver(form)
        elif self.path == "/test-naver":
            self._handle_test_naver(form)
        elif self.path == "/delete-naver":
            self._handle_delete_naver()
        elif self.path == "/save-llm":
            self._handle_save_llm(form)
        elif self.path == "/test-llm":
            self._handle_test_llm(form)
        elif self.path == "/delete-llm":
            self._handle_delete_llm()
        elif self.path == "/save-telegram-sender":
            self._handle_save_telegram_sender(form)
        elif self.path == "/test-telegram-sender":
            self._handle_test_telegram_sender(form)
        elif self.path == "/delete-telegram-sender":
            self._handle_delete_telegram_sender()
        elif self.path == "/save-email-sender":
            self._handle_save_email_sender(form)
        elif self.path == "/test-email-sender":
            self._handle_test_email_sender(form)
        elif self.path == "/delete-email-sender":
            self._handle_delete_email_sender()
        elif self.path.startswith("/adhoc"):
            if not adhoc_routes.handle_post(self, self.path, form):
                self.send_response(404)
                self.end_headers()
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
            "include_in_live": f"group{index}_include_in_live" in form,
            "include_in_scrap": f"group{index}_include_in_scrap" in form,
            "disabled_keywords": disabled_keywords,
        }

    def _handle_save_keywords(self, form: dict) -> None:
        # [수정: 2026-08-19] "+ 키워드 그룹 추가"/"+ 키워드 추가" 전용 핸들러(_handle_add_
        # keyword_group/_handle_add_keyword_slot)와 그 둘이 재렌더링용 칸 개수를 세던
        # _group_slot_count를 없앴다 — 이제 그룹/키워드 추가·삭제·ON-OFF·편집은 전부
        # 화면(_KEYWORD_GROUPS_SCRIPT)이 메모리에서 처리하고, 서버로 오는 POST는 실제
        # "저장" 버튼을 눌렀을 때 이 핸들러 하나뿐이다. 필드 이름 규칙(group{i}_name 등)은
        # 그대로라 _parse_group은 손대지 않았다.
        groups = [self._parse_group(form, g) for g in range(1, MAX_KEYWORD_GROUPS + 1)]
        try:
            save_keyword_groups(groups)
        except SettingsError as error:
            # 검증 실패 시 저장하지 않고, 입력했던 값을 그대로 보여주며 오류 메시지만 추가한다.
            attempted = [g for g in groups if g["name"].strip() or any(k.strip() for k in g["keywords"])]
            self._respond(render_keywords_page(load_settings(), error=str(error), groups=attempted))
            return
        self._redirect("/keywords")

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

    def _handle_save_auto_send_settings(self, form: dict) -> None:
        """자동발송 설정 화면의 "저장" — 사용 여부와 유예 시간(분)을 함께 저장한다.

        시간 값이 범위를 벗어나면(1~MAX_AUTO_SEND_GRACE_MIN) SettingsError가 올라오므로
        다른 설정 화면과 같은 방식으로 오류 화면을 보여준다.
        """
        try:
            save_auto_send_settings(
                "auto_send_enabled" in form,
                form.get("auto_send_grace_min", [""])[0],
            )
        except SettingsError as exc:
            attempted = {**load_settings(), "auto_send_enabled": "auto_send_enabled" in form}
            self._respond(
                render_auto_send_settings(
                    attempted, error=str(exc), grace_min=form.get("auto_send_grace_min", [""])[0]
                )
            )
            return
        self._redirect("/auto-send")

    def _handle_save_telegram_settings(self, form: dict) -> None:
        """텔레그램 설정 화면의 「저장」 — 받는 사람 목록 전체를 저장한다.
        🗑로 지운 줄은 폼에서 아예 빠져 목록에서도 빠진다."""
        save_telegram_recipients(_parse_telegram_recipient_form(form))
        self._redirect("/telegram")

    def _handle_add_telegram_recipient_slot(self, form: dict) -> None:
        """「+ 받는 사람 추가」 — 저장하지 않고 입력칸을 하나 더 보여준다(입력 중이던 값 유지)."""
        recipients = _parse_telegram_recipient_form(form)
        slots = min(len(recipients) + 1, MAX_TELEGRAM_RECIPIENTS)
        self._respond(render_telegram_settings(load_settings(), slots=slots, recipients=recipients, dirty=True))

    def _handle_save_breaking_alert_settings(self, form: dict) -> None:
        """[단독]·[속보] 알림 설정 화면(/breaking-alert)의 "저장"."""
        try:
            save_breaking_alert_settings(
                "enabled" in form,
                form.get("group_names", []),
                form.get("start", [""])[0],
                form.get("end", [""])[0],
                form.get("interval_min", [""])[0],
                "catch_up_enabled" in form,
                "burst_enabled" in form,
                form.get("burst_window_min", [""])[0],
                form.get("burst_min_outlets", [""])[0],
            )
        except BreakingAlertSettingsError as exc:
            self._respond(render_breaking_alert_settings(error=str(exc)))
            return
        self._redirect("/breaking-alert")

    def _handle_download_text(self, form: dict) -> None:
        """확정본·초안·지난 기사의 "다운로드" 버튼이 실제 `<form>` POST로 호출한다.

        [수정: 2026-08-12] 예전엔 `<a href="data:text/plain;...,{URL-encode된 전체 본문}">`
        하나로 처리했다 — 한글은 percent-encoding하면 글자당 3바이트가 %XX%XX%XX(9글자)로
        부풀어서, 지난 기사처럼 다운로드 링크가 여러 개(회차마다 하나) 있는 화면에서는
        그 인코딩된 문자열만 파일 용량의 상당 부분을 차지했다(history.html 실측: 전체
        3.4MB 중 389KB, 13%). 서버가 Content-Disposition으로 응답하는 표준 다운로드로
        바꾸면 화면에는 다운로드 버튼과 원문 텍스트(HTML 이스케이프만 적용, 부풀지 않음)
        만 남는다. 확정본·초안은 telegram-send와 동일하게 화면의 PLAIN_TEXT를 그대로
        받는다(화면과 다운로드 내용이 항상 일치하도록) — 지난 기사는 애초에 읽기 전용이라
        렌더링 시점에 이미 확정된 텍스트를 그대로 폼에 심어 보낸다.
        """
        text = form.get("text", [""])[0]
        filename = form.get("filename", ["download.txt"])[0]
        self._respond_download(text, filename)

    def _handle_download_excel(self, form: dict) -> None:
        """엑셀 내보내기(PRD.md 기능11) 공용 핸들러 — _handle_download_text의 xlsx판.

        화면마다 "rows를 어떻게 모으는가"만 다르고 그 뒤(워크북으로 만들어 파일로
        응답)는 하나다. rows는 렌더링 시점에 이미 계산해 hidden input에 JSON으로 심어
        보낸 값을 그대로 받는다 — 확정본·초안 등이 PLAIN_TEXT를 그대로 심어 보내는
        것과 같은 이유(화면과 내보내기 내용이 항상 일치하도록, PRD.md 기능11 규칙2).
        정기 보관함·수시 보관함처럼 화면이 지연 로딩으로 계속 바뀌는 곳은 이 자리를
        안 쓰고 그때그때 다시 계산하는 전용 엔드포인트를 따로 둔다.

        rows가 깨진 JSON이면(브라우저 확장 프로그램의 폼 간섭 등) 빈 파일 대신
        400을 돌려준다 — 조용히 빈 엑셀을 내려주면 담당자가 "내용이 없나?"로
        오해한다.
        """
        try:
            rows = json.loads(form.get("rows", ["[]"])[0])
        except json.JSONDecodeError:
            self._respond('<p style="font-family:sans-serif;padding:24px;">요청이 올바르지 않습니다. 새로고침 후 다시 시도해주세요.</p>', status=400)
            return
        filename = form.get("filename", ["download.xlsx"])[0]
        self._respond_excel_download(build_workbook_bytes(rows), filename)

    def _handle_download_excel_history(self, form: dict) -> None:
        """정기 보관함(history.html)의 "⬇ 엑셀" 버튼 — _handle_download_excel과 달리
        rows를 폼에서 받지 않고 dates(펼쳐진 날짜들, 반복 필드)만 받아 서버에서 다시
        계산한다. 이 화면은 지연 로딩으로 내용이 초기 렌더 이후에도 계속 바뀌므로
        (EAGER_HISTORY_DAYS 이후 날짜는 펼치는 순간에야 서버에서 가져온다), 렌더링
        시점에 rows를 미리 구워둘 수가 없다 — app.history_renderer.build_excel_rows_for_dates가
        그 시점에 다시 읽어 화면과 같은 그룹핑으로 만든다.
        """
        dates = [d for d in form.get("dates", []) if d]
        rows = build_excel_rows_for_dates(dates)
        filename = excel_filename_for_dates(dates)
        self._respond_excel_download(build_workbook_bytes(rows), filename)

    def _handle_telegram_send_draft(self, form: dict) -> None:
        """초안 화면(preview.html)의 "Telegram" 버튼이 fetch로 호출한다.

        초안 상태를 서버가 다시 계산하지 않고, 화면이 이미 갖고 있던 PLAIN_TEXT를 그대로
        받아 전송한다 — 재정렬·숨김 직후처럼 화면과 저장된 상태 사이에 미묘한 시차가
        있을 수 있는 상황에서도 "지금 화면에 보이는 그대로"가 항상 보장된다.
        preview.html은 이 서버와 같은 출처(127.0.0.1:{port})라 CORS 헤더가 필요 없다.
        """
        text = form.get("text", [""])[0]
        chat_ids = telegram_active_recipient_chat_ids()
        if text and chat_ids and telegram_send_text(text, chat_ids):
            self.send_response(204)
        else:
            self.send_response(502)
        self.end_headers()

    def _handle_telegram_send_scrap(self, form: dict) -> None:
        """스크랩 완성본(index.html)의 "Telegram" 버튼이 fetch로 호출한다.

        초안과 동일하게, 서버가 최신 회차를 다시 계산하지 않고 화면의 PLAIN_TEXT를
        그대로 받아 전송한다(_handle_telegram_send_draft와 같은 이유).
        """
        text = form.get("text", [""])[0]
        chat_ids = telegram_active_recipient_chat_ids()
        if text and chat_ids and telegram_send_text(text, chat_ids):
            self.send_response(204)
        else:
            self.send_response(502)
        self.end_headers()

    def _handle_send_scrap(self, form: dict) -> None:
        """완성본(index.html)의 (전송) 플로팅 버튼이 fetch로 호출한다.

        [추가: 2026-08-10] Telegram/Email 개별 버튼을 통합했다 — 설정된 채널
        전부(app.confirm_send.send_confirmed_run)로 보낸다. 화면이 이미 갖고 있던
        PLAIN_TEXT를 그대로 보낸다(text_override) — 기존 개별 버튼들과 같은 이유로,
        서버가 다시 계산한 상태가 아니라 지금 화면에 보이는 그대로(직전 수정 포함)가
        나가야 하기 때문이다. 2번째 이상 전송이면 send_confirmed_run이 제목 앞에
        "(수정)"을 자동으로 붙인다.

        [수정: 2026-08-24] 두 가지를 고쳤다.
        (1) send_confirmed_run이 이제 (회차, 실제로 나갔는지) 튜플을 돌려준다 — 채널이
        하나도 설정 안 됐거나 전송 자체가 실패했으면 502로 응답한다(같은 실패를
        _handle_telegram_send_scrap이 이미 502로 응답하던 것과 같은 관례). 예전엔
        무조건 204를 응답해 실패해도 JS가 "이메일 및 텔레그램으로 발송되었습니다"
        토스트를 그대로 띄웠다.
        (2) 성공했을 때 _regenerate_screens()를 호출한다 — 안 그러면 send_count·
        sent_by가 회차 파일엔 저장돼도 index.html은 재생성 전이라, sendReport()의
        location.reload()가 옛 화면(카운트다운 없는 빈 자리)을 그대로 다시 보여주고
        "✔️ HH시 MM분 발송 완료" 배지가 한 번 더 새로고침할 때까지 안 나타났다.

        [수정: 2026-08-26] 실패했을 때도 _regenerate_screens()를 부른다 — send_confirmed_run이
        실패 사유를 이미 회차 파일의 send_failure에 저장해두는데(app.storage.
        record_send_failure), 화면을 다시 안 그리면 그 사유가 index.html에 반영될
        기회가 이 요청 안에는 없다(다음에 우연히 다른 큐레이션 동작이 화면을 다시
        그릴 때까지 안 보임). sendReport()도 실패 시 새로고침하도록 같이 고쳤다 —
        재생성된 화면의 배너(app.renderer.render_page)가 바로 그 사유를 보여준다.
        """
        text = form.get("text", [""])[0]
        run = load_latest_run()
        if not text or run is None or not run.get("confirmed", True):
            self.send_response(400)
            self.end_headers()
            return
        updated, sent = send_confirmed_run(run, text_override=text)
        self._regenerate_screens()
        if not sent:
            self.send_response(502)
            self.end_headers()
            return
        if updated.get("send_failure"):
            # 일부만 받음 — 화면(renderer의 sendReport)이 "누가 못 받았는지 보라"고 안내한다.
            body = b"partial"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(204)
        self.end_headers()

    def _handle_save_email_settings(self, form: dict) -> None:
        """이메일 설정 화면의 "저장" — 자동 전송 체크박스와 받는 사람 목록을 함께 저장한다.

        받는 사람은 검색 키워드 칸과 같은 규칙으로, del로 지워져 폼에서 아예 빠진
        칸은 자연히 목록에서도 빠진다(app.email_recipients.save_email_recipients가
        이메일이 비어있는 항목도 마저 걸러낸다).
        """
        recipients = [
            {
                "name": form.get(f"recipient{i}_name", [""])[0],
                "email": form.get(f"recipient{i}_email", [""])[0],
                "enabled": f"recipient{i}_enabled" in form,
            }
            for i in range(1, MAX_EMAIL_RECIPIENTS + 1)
            if f"recipient{i}_email" in form
        ]
        save_email_recipients(recipients)
        self._redirect("/email")

    def _handle_add_email_recipient_slot(self, form: dict) -> None:
        """"+ 받는 사람 추가" 버튼 — 저장하지 않고 입력칸을 하나 더 보여준다(지금 입력
        중이던 값은 그대로 유지)."""
        recipients = [
            {
                "name": form.get(f"recipient{i}_name", [""])[0],
                "email": form.get(f"recipient{i}_email", [""])[0],
                "enabled": f"recipient{i}_enabled" in form,
            }
            for i in range(1, MAX_EMAIL_RECIPIENTS + 1)
            if f"recipient{i}_email" in form
        ]
        slots = min(len(recipients) + 1, MAX_EMAIL_RECIPIENTS)
        self._respond(render_email_settings(load_settings(), slots=slots, recipients=recipients))

    def _handle_save_naver(self, form: dict) -> None:
        """네이버 키 설정 화면의 "저장". Client ID는 화면에 그대로(마스킹 없이) 보이는
        값이라 제출된 값을 곧이곧대로 저장한다 — Client Secret은 마스킹돼 있어 화면
        JS가 실제 값을 읽을 방법이 없으므로, 칸이 비어 있으면 "바꾸지 않았다"는
        뜻으로 보고 지금 저장된 값을 그대로 유지한다(app.credentials.
        naver_saved_client_secret — .env 폴백 없이 저장소 원본만, 안 그러면 빈 칸으로
        저장해도 .env 값이 파일에 그대로 옮겨 적힌다)."""
        client_id = form.get("naver_client_id", [""])[0].strip()
        new_secret = form.get("naver_client_secret", [""])[0].strip()
        client_secret = new_secret or naver_saved_client_secret()
        save_naver_credentials(client_id, client_secret)
        self._redirect("/naver")

    def _handle_test_naver(self, form: dict) -> None:
        """[연결 테스트] — 화면에 지금 입력된 값으로 확인한다. Client ID는 화면에 이미
        채워진 값이 그대로 오고, Client Secret이 비어 있으면(안 바꿨다는 뜻) 지금
        효과가 있는 값(저장된 값 또는 .env)으로 보충해 "지금 실제로 쓰이는 값"을
        테스트한다 — 페이지를 막 열고 아무것도 안 건드린 채 눌러도 뜻이 통한다."""
        client_id = form.get("naver_client_id", [""])[0].strip() or (naver_client_id() or "")
        client_secret = form.get("naver_client_secret", [""])[0].strip() or (naver_client_secret() or "")
        ok, message = test_naver_credentials(client_id, client_secret)
        self._respond_json({"ok": ok, "message": message})

    def _handle_delete_naver(self) -> None:
        delete_naver_credentials()
        self.send_response(204)
        self.end_headers()

    def _handle_save_llm(self, form: dict) -> None:
        """AI 연동 화면의 "저장" — Claude API 키는 칸이 비어 있으면(안 바꿨다는 뜻)
        저장된 값을 유지한다(_handle_save_naver와 같은 이유). 모델 선택은 없다
        (app.config.LLM_MODEL 주석 참고 — 화면이 아니라 코드가 정하는 값)."""
        new_key = form.get("llm_api_key", [""])[0].strip()
        api_key = new_key or llm_saved_api_key()
        save_llm_credentials(api_key)
        self._redirect("/llm")

    def _handle_test_llm(self, form: dict) -> None:
        api_key = form.get("llm_api_key", [""])[0].strip() or (llm_api_key() or "")
        ok, message = test_llm_credentials(api_key, llm_model())
        self._respond_json({"ok": ok, "message": message})

    def _handle_delete_llm(self) -> None:
        delete_llm_credentials()
        self.send_response(204)
        self.end_headers()

    def _handle_save_telegram_sender(self, form: dict) -> None:
        """텔레그램 발송 계정 저장 — 토큰 칸이 비면 저장된 값을 유지한다(_handle_save_llm과 같은
        이유). 봇 이름은 바뀌었을 때만 텔레그램에 보낸다(자주 바꾸면 텔레그램이 막는다). 토큰만
        새 봇으로 바꿨으면 그 봇에 기본 이름을 한 번 건다(앱을 켤 때와 같은 규칙)."""
        new_token = form.get("bot_token", [""])[0].strip()
        token_changed = bool(new_token) and new_token != (telegram_bot_token() or "")
        if new_token:
            save_telegram_token(new_token)
        raw = form.get("bot_name", [None])[0]
        name_changed = (
            raw is not None and normalize_bot_name(raw) != form.get("bot_name_before", [""])[0]
        )
        if not telegram_is_configured() or not (name_changed or token_changed):
            self._redirect("/telegram-sender")
            return
        if not name_changed:
            apply_default_bot_name_once()
            self._redirect("/telegram-sender")
            return
        name = normalize_bot_name(raw)
        error = set_bot_name(name)
        if error:
            msg = f"봇 이름은 못 바꿨어요 — {error}" + (" 봇 토큰은 저장됐습니다." if new_token else "")
            self._redirect(f"/telegram-sender?bn=err&msg={quote(msg)}")
        else:
            msg = f"봇 이름을 「{name}」로 바꿨습니다. 이미 열려 있던 대화방은 텔레그램을 다시 열어야 새 이름이 보일 수 있어요."
            self._redirect(f"/telegram-sender?bn=ok&msg={quote(msg)}")

    def _handle_test_telegram_sender(self, form: dict) -> None:
        token = form.get("bot_token", [""])[0].strip() or (telegram_bot_token() or "")
        ok, message = check_bot_token(token)
        self._respond_json({"ok": ok, "message": message})

    def _handle_delete_telegram_sender(self) -> None:
        delete_telegram_token()
        self.send_response(204)
        self.end_headers()

    def _handle_save_email_sender(self, form: dict) -> None:
        """이메일 보내는 계정 저장 — 비밀번호 칸이 비면 저장된 값을 유지한다(_handle_save_llm과
        같은 이유). 처음 저장인데 비밀번호가 비면 .env 비밀번호를 옮겨 적지 않고 거절한다."""
        service = form.get("service", [""])[0]
        address = form.get("address", [""])[0].strip()
        name = form.get("name", [""])[0].strip()[:40]
        password = form.get("password", [""])[0].strip() or email_saved_password()
        attempted = {"service": service, "address": address, "name": name}
        if service not in EMAIL_SERVICES:
            error = "메일 서비스를 골라주세요."
        else:
            error = check_email_sender_address(service, address)
        if not error and not password:
            error = "앱 비밀번호를 입력해주세요."
        if error:
            self._respond(render_email_sender_page(error=error, attempted=attempted))
            return
        save_email_credentials(service, address, password, name)
        self._redirect("/email-sender")

    def _handle_test_email_sender(self, form: dict) -> None:
        service = form.get("service", [""])[0]
        password = form.get("password", [""])[0].strip() or (email_sender_password() or "")
        ok, message = send_test_mail(
            service, form.get("address", [""])[0], password, form.get("name", [""])[0]
        )
        self._respond_json({"ok": ok, "message": message})

    def _handle_delete_email_sender(self) -> None:
        delete_email_credentials()
        self.send_response(204)
        self.end_headers()

    def _handle_email_send_draft(self, form: dict) -> None:
        """초안 화면(preview.html)의 "Email" 버튼이 fetch로 호출한다.

        텔레그램과 같은 이유로 서버가 상태를 다시 계산하지 않고, 화면이 이미 갖고
        있던 PLAIN_TEXT를 그대로 받아 전송한다 — 첫 줄("언론 모니터링 [시각] 기준")을
        메일 제목으로, 전체를 본문으로 쓴다.
        """
        text = form.get("text", [""])[0]
        recipients = active_recipient_emails()
        if text and recipients and email_send_text(text.split("\n", 1)[0], text, recipients):
            self.send_response(204)
        else:
            self.send_response(502)
        self.end_headers()

    def _handle_email_send_scrap(self, form: dict) -> None:
        """스크랩 완성본(index.html)의 "Email" 버튼이 fetch로 호출한다(_handle_email_send_draft와 같은 이유)."""
        text = form.get("text", [""])[0]
        recipients = active_recipient_emails()
        if text and recipients and email_send_text(text.split("\n", 1)[0], text, recipients):
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

        [수정: 2026-08-14] 순서도 이제 회차 단위로 저장되므로(사용자 요청)
        _resolve_round_id로 "이 요청이 어느 회차를 위한 것인지" 먼저 정한다
        (app._handle_rename_group과 동일한 판단·동일한 이유).
        """
        raw_order = form.get("order", [""])[0]
        try:
            order = json.loads(raw_order)
        except json.JSONDecodeError:
            order = None
        if isinstance(order, list):
            save_group_order(order, round_id=_resolve_round_id(form))
            self._regenerate_screens()
            self.send_response(204)
        else:
            self.send_response(400)
        self.end_headers()

    def _handle_save_format(self, form: dict, flow: str = "regular") -> None:
        spec = _FORMAT_FLOWS[flow]
        template = form.get("line_template", [""])[0]
        save = save_adhoc_article_line_template if flow == "adhoc" else save_article_line_template
        try:
            save(template)
        except SettingsError as error:
            attempted = {**load_settings(), spec["line_key"]: template}
            self._respond(render_format_page(attempted, error=str(error), flow=flow))
            return
        self._redirect(spec["line_path"])

    def _handle_save_subheading_format(self, form: dict, flow: str = "regular") -> None:
        spec = _FORMAT_FLOWS[flow]
        template = form.get("subheading_format", [""])[0]
        save = save_adhoc_subheading_format_template if flow == "adhoc" else save_subheading_format_template
        try:
            save(template)
        except SettingsError as error:
            attempted = {**load_settings(), spec["sub_key"]: template}
            self._respond(render_subheading_format_page(attempted, error=str(error), flow=flow))
            return
        self._redirect(spec["sub_path"])

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

        [수정: 2026-08-10] "적용" 라디오(그룹 전체에서 1개만) 대신, 그룹마다 독립적인
        "사용" 체크박스(group{n}_enabled, 시간대별 enabled와 이름이 다름에 주의)와
        요일 체크박스(group{n}_days, 같은 name이 반복돼 form.get(..., [])이 체크된
        값 리스트를 돌려준다 — 언론사 화이트리스트 체크박스와 같은 패턴)를 읽는다.
        """
        # 화면에 떠 있던(×로 지우지 않은) 줄만, 화면 순서대로 모은다 — 가운데 줄을 지운 뒤
        # 「+ 시간대 추가」를 눌러도 빈 자리 없이 이어진다. 시간대마다 켜고 끄는 칸은 없어져
        # enabled는 늘 참이다(안 쓰는 시간대는 지운다).
        times = [
            {
                "start": self._combine_hhmm(form, f"group{group_index}_start{i + 1}"),
                "end": self._combine_hhmm(form, f"group{group_index}_end{i + 1}"),
                "enabled": True,
            }
            for i in range(MAX_SCHEDULE_TIMES)
            if f"group{group_index}_end{i + 1}_h" in form
        ]
        return {
            "name": form.get(f"group{group_index}_name", [""])[0],
            "times": times,
            "enabled": f"group{group_index}_enabled" in form,
            "days": [d for d in form.get(f"group{group_index}_days", []) if d in WEEKDAY_KEYS],
        }

    @staticmethod
    def _next_schedule_window(times: list) -> dict:
        """「+ 시간대 추가」로 생기는 새 줄 — 회차는 보통 이어지므로 시작을 윗줄 종료로,
        종료를 그 2시간 뒤로 채워 둔다(담당자가 보고 고친다, 저장 전이다). 윗줄 종료가
        비었거나 2시간 뒤가 자정을 넘으면 그 칸은 빈 채로 둔다."""
        prev_end = times[-1]["end"] if times else ""
        h, m = _split_hhmm(prev_end)
        if not h or not m:
            return {"start": "", "end": "", "enabled": True}
        end_min = int(h) * 60 + int(m) + 120
        end = f"{end_min // 60:02d}:{end_min % 60:02d}" if end_min < 24 * 60 else ""
        return {"start": prev_end, "end": end, "enabled": True}

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
        slots_by_group = {i + 1: max(len(g["times"]), 1) for i, g in enumerate(groups)}
        if len(groups) < MAX_SCHEDULE_GROUPS:
            groups.append({"name": "", "times": [], "enabled": False, "days": []})
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
        if 1 <= group_index <= len(groups):
            times = groups[group_index - 1]["times"]
            if len(times) < MAX_SCHEDULE_TIMES:
                times.append(self._next_schedule_window(times))
        slots_by_group = {i + 1: max(len(g["times"]), 1) for i, g in enumerate(groups)}
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

        [수정: 2026-08-24] undo_push("기사 순서 변경")을 추가했다 — 예전엔 이 액션만
        되돌리기 스택에 안 쌓여서, ↑/↓ 직후 ↩ 버튼을 눌러도 방금 한 이동이 아니라 그
        이전에 쌓인 엉뚱한 동작(예: 한참 전 AI 재분류)이 되돌아갔다. app.undo.push는
        실제로 상태가 바뀌기 **직전**에 불러야 하므로, move_article로 계산만 먼저 해서
        "이번 클릭으로 뭔가 바뀔지" 안 뒤에 바뀔 때만 쌓는다(소제목 맨 위/아래라 더
        옮길 곳이 없는 클릭까지 스택에 쌓이면 안 되므로).
        """
        url = form.get("url", [""])[0]
        direction = form.get("direction", [""])[0]
        if url and direction in ("up", "down"):
            run = load_latest_run()
            if run is not None:
                round_id = round_id_for_run(run)
                keywords = all_search_keywords(load_settings())
                overrides = load_group_overrides()
                new_articles, new_override = move_article(
                    run["articles"], keywords, url, direction, overrides, round_id=round_id
                )
                if new_articles is not run["articles"] or new_override is not None:
                    undo_push("기사 순서 변경", touched=[url])
                changed = False
                if new_override is not None:
                    set_group_override(*new_override)
                    overrides = load_group_overrides()
                    changed = True
                if new_articles is not run["articles"] or new_override is not None:
                    snapshot = snapshot_group_names(
                        new_articles, keywords, overrides, forced_group_target_names(), round_id=round_id
                    )
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

        [수정: 2026-08-14] 이름표가 이제 회차 단위로 저장되므로(사용자 요청) _resolve_round_id
        로 "이 요청이 어느 회차를 위한 것인지" 먼저 정하고, 그 회차에만 저장한다.
        """
        name = form.get("name", [""])[0]
        label = form.get("label", [""])[0].strip()
        active_names = set(form.get("active_names", []))
        round_id = _resolve_round_id(form)
        if not name or not label:
            self.send_response(204)
        elif display_name_in_use(label, active_names, exclude_name=name, round_id=round_id):
            self.send_response(409)
        else:
            undo_push("소제목 이름 변경")
            set_group_label(name, label, round_id=round_id)
            self._regenerate_screens()
            self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _handle_hide_article(self, form: dict) -> None:
        """index.html/history.html(file://로 열림)의 🗑️ 버튼이 fetch로 호출한다.
        file:// 오리진은 CORS상 "null"로 취급되므로, 성공/실패를 JS가 읽을 수 있도록
        Access-Control-Allow-Origin을 열어준다 (로컬 단일 사용자 도구라 위험 없음).

        [추가: 2026-08-20] 화면이 제목을 안 실어 보냈으면 **서버가 직접 찾아 채운다**
        (_hide_meta_fallbacks). 제목은 숨김이 계속 먹는지를 좌우하는 값이라
        (app.curation.filter_hidden — 네이버가 같은 기사를 새 기사 ID로 재게재하면
        URL 일치가 깨지고 제목 대조만 남는다) 호출부 하나가 빠뜨리면 그 기사는 조용히
        되살아난다. 실제로 일괄 숨기기·소제목 통째 숨기기가 url만 보내고 있었고
        (2026-08-20 제보), 그건 화면 쪽에서 고쳤지만 — 열어둔 옛 탭이나 앞으로 생길
        새 호출부가 또 빠뜨릴 수 있으므로 서버에서 한 번 더 받쳐준다."""
        # [수정: 2026-09-22] 기사 여러 건을 한 요청으로 받는다 — 소제목 통째 숨기기·선택 바
        # 일괄 숨기기가 url·outlet·title·pubDate·group을 기사마다 같은 순서로 되풀이해 싣는다
        # (빈 값도 자리를 지킨다 — 폼 파싱이 keep_blank_values라 줄이 어긋나지 않는다).
        # 예전엔 기사 수만큼 요청을 나눠 보내, 요청마다 되돌리기 스택(수십 MB) 쓰기와 화면
        # 세 개 재생성이 되풀이돼 소제목 하나 숨기는 데 몇 초가 걸렸다.
        urls = form.get("url", [])

        def field(name: str, i: int) -> Optional[str]:
            values = form.get(name, [])
            return (values[i] if i < len(values) else "") or None

        items = [
            {
                "url": url,
                "outlet": field("outlet", i),
                "title": field("title", i),
                "pub_date": field("pubDate", i),
                # [추가: 2026-09-02] group — 휴지통이 "한 번에 숨긴 덩어리"에 소제목 이름을
                # 붙이는 데만 쓴다(숨김 판정에는 관여하지 않는다).
                "group": field("group", i),
            }
            for i, url in enumerate(urls)
            if url
        ]
        if items:
            need = {it["url"] for it in items if not (it["title"] and it["outlet"] and it["group"])}
            metas = _hide_meta_fallbacks(need)
            for it in items:
                meta = metas.get(it["url"], {})
                for key in ("outlet", "title", "pub_date", "group"):
                    it[key] = it[key] or meta.get(key)
            # [추가: 2026-09-15] batch — 옛 탭(기사마다 요청을 나눠 보내던 화면)이 보내는
            # 요청도 같은 동작으로 알아보게 받아 둔다.
            batch = form.get("batch", [""])[0] or None
            undo_push("기사 숨기기", touched=[it["url"] for it in items], batch=batch)
            hide_articles(items)
            self._regenerate_screens()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _handle_unhide_article(self, form: dict) -> None:
        """숨김 해제 — 단건(url 하나)과 묶음(url 여러 개)을 같은 핸들러가 받는다.

        [수정: 2026-09-02] 호출하는 화면이 둘이라 응답 방식이 갈린다:
        - 휴지통 화면(/hidden, 설정 서버가 직접 서빙)은 실제 <form> 제출이라
          redirect 필드를 함께 보내고, 예전처럼 303으로 그 화면에 되돌려 보낸다.
        - 확정본·초안의 휴지통 팝오버는 file://로 열려 있을 수 있어 fetch로 부르고
          204를 받은 뒤 스스로 새로고침한다(_handle_hide_article과 같은 이유로 CORS를
          열어준다). 숨김 해제는 기사 목록과 소제목 구성이 같이 바뀌는 동작이라,
          팝오버만 고쳐 그리면 화면이 거짓말을 하게 되므로 부분 갱신을 쓰지 않는다.
        되돌리기는 건수와 무관하게 **한 걸음**이다 — 12건짜리 묶음을 되살린 뒤 ↩ 한 번이면
        통째로 다시 숨겨진다(묶는 창을 app.undo의 coalesce와 같은 3초로 맞춘 이유).
        """
        urls = [url for url in form.get("url", []) if url]
        if urls:
            undo_push("숨김 되돌리기", touched=urls)
            unhide_articles(urls)
            self._regenerate_screens()
        redirect_to = form.get("redirect", [""])[0]
        if redirect_to:
            self._redirect(redirect_to)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

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

    def _handle_cut_round(self, form: dict) -> None:
        """초안 머리줄의 「✂ 여기서 끊기」가 fetch로 부른다(app.cut_round.cut_round).

        at(HH:MM)을 생략하면 지금. 지금·지난 시각이면 초안 기사 그대로 곧바로 확정본을 만들고
        (mode=done), 앞 시각이면 예약만 한다(mode=pending). 거절하면 200 + ok=false + 이유 —
        담당자가 누른 동작이 조용히 사라지면 안 된다(CODING_CONVENTIONS §3).
        """
        from app.cut_round import CutError, cut_round

        at = form.get("at", [""])[0].strip() or None
        try:
            result = cut_round(at)
        except CutError as e:
            self._respond_json({"ok": False, "reason": str(e)})
            return
        except Exception:
            logger.exception("오늘만 회차 끊기 실패")
            self._respond_json({"ok": False, "reason": "확정본을 만들지 못했어요. 잠시 뒤 다시 눌러 주세요."})
            return
        self._respond_json({"ok": True, **result})

    def _handle_cut_round_cancel(self, form: dict) -> None:
        """머리줄 칩 「✂ 오늘만 16:00에 끊음 · 취소」가 부른다(app.cut_round.cancel_cut)."""
        from app.cut_round import CutError, cancel_cut

        try:
            cancel_cut(form.get("end", [""])[0].strip())
        except CutError as e:
            self._respond_json({"ok": False, "reason": str(e)})
            return
        self._respond_json({"ok": True})

    def _handle_add_article_by_url(self, form: dict) -> None:
        """초안의 "+ 수기로 기사 추가"가 fetch로 호출한다 — 담당자가 네이버에서 직접
        찾아온 기사를 주소만으로 📌 담아둔 기사 칸에 올린다.

        네이버 검색 API를 한 번도 안 부른다. 검색어 화면(상시 조건)을 건드리지 않고
        기사 한 건을 끌어오기 위한 입구다 — 일회용 키워드를 등록했다가 지우는 우회로는
        그 회차 확정본의 수집 조건까지 바꾸기 때문에 쓸 수 없다.

        착지점이 📌 담아둔 기사인 이유: 그 칸은 이미 "보고서 밖 임시보드"라(승격 전까지
        복사·txt·발송 어디에도 안 나간다) 밖에서 들어온 기사가 담당자 확인을 한 번
        거치게 된다. 승격은 기존 소제목 드롭다운을 그대로 쓴다.

        **한 번에 한 건만 받는다.** 여러 건을 받으면 결과가 목록이 되고 어느 줄이 왜
        실패했는지 대조해야 한다 — 손으로 고르는 동작이라 한 건이면 충분하다.

        거부해도 200으로 내려보내고 사유를 본문에 담는다(화면이 그대로 보여준다) —
        담당자가 누른 동작이 조용히 사라지면 안 된다(CODING_CONVENTIONS §3).
        """
        # 거부 문구는 둘로만 가른다. 원인(주소 형식·접속 실패·제목 없음)이 여럿이어도 담당자가
        # 할 일이 「주소 확인」 하나면 한 문구다. 기사가 아닌 주소(블로그·카페·커뮤니티)는
        # 할 일이 달라(다른 주소를 찾아야 한다) 따로 둔다. 화면은 reason을 굵게, detail을
        # 그 아래에 그대로 보여준다.
        cannot_load = {
            "ok": False,
            "reason": "해당 URL로 기사를 불러올 수 없습니다.",
            "detail": "URL을 확인해 주세요.",
        }
        url = form.get("url", [""])[0].strip()
        if not url.lower().startswith(("http://", "https://")):
            self._respond_json(cannot_load)
            return
        # 수집이 저장하는 모양으로 맞춘다(`?sid=` 꼬리·`/article/`·`m.news` 등) — 안 맞추면
        # 같은 기사가 아래 URL 대조를 전부 빠져나가 확정본에 두 번 실린다.
        url = normalize_article_url(url)
        screen = form.get("screen", ["confirmed"])[0]
        if is_non_news_url(url):
            self._respond_json(
                {
                    "ok": False,
                    "reason": "언론사 기사가 아니라서 담을 수 없습니다.",
                    "detail": "블로그·카페·커뮤니티 글은 담을 수 없어요. 언론사 기사 주소인지 확인해 주세요.",
                }
            )
            return

        # 언론사 원문 주소로 넣었으면, 오늘 모은 기사 중 원문 주소가 같은 네이버 기사로 바꿔 읽는다
        # — 그래야 아래 대조(숨김·이 회차·앞 회차·담아둠)가 수집이 저장한 주소로 돈다. 제목으로
        # 대조하지 않는다(중복·숨김은 URL로만).
        url_index = _original_url_index()
        mapped = next((url_index[k] for k in original_url_keys(url) if k in url_index), None)
        if mapped and mapped != url:
            logger.info("수기로 기사 추가 — 원문 주소 %s → 네이버 기사 %s로 대조", url, mapped)
            url = mapped

        # 이미 이 회차에 들어와 있는 기사는 다시 담지 않는다 — 담아둔 기사로 올려봐야
        # 승격할 때 중복이 된다. 판정은 URL로만 한다(숨김 판정과 같은 축).
        # 「이 회차」는 화면마다 다르다: 확정본은 저장된 최신 회차, 초안은 아직 저장 전인
        # 지금 초안(current_draft_urls). 초안에서 load_latest_run()을 보면 앞 회차와 비교하게 된다.
        run = load_latest_run()
        if screen == "preview":
            round_urls = current_draft_urls()
        else:
            round_urls = {a.get("url") for a in (run.get("articles") if run else []) or []}
        if is_hidden({"url": url}, load_hidden_urls()):
            # 담아도 📌 칸이 숨긴 기사를 걸러 성공 뒤에 아무것도 안 보인다 — 조용히 사라지지 않게 여기서 막는다.
            self._respond_json(
                {"ok": False, "reason": "숨긴 기사입니다.", "detail": "휴지통에서 되살려 주세요."}
            )
            return
        if url in round_urls:
            self._respond_json({"ok": False, "reason": "이미 이 회차에 있는 기사입니다."})
            return
        # 오늘 앞 회차에 이미 실린 기사 — 다시 담으면 보고서에 두 번 나간다.
        if url in _already_published_urls(datetime.now().strftime("%Y-%m-%d")):
            self._respond_json({"ok": False, "reason": "이미 앞 회차에 실린 기사입니다."})
            return
        if any(a.get("url") == url for a in load_manual_articles()):
            self._respond_json({"ok": False, "reason": "이미 담아둔 기사입니다."})
            return

        article = fetch_article_by_url(url)
        if not article:
            self._respond_json(cannot_load)
            return
        # 반대 방향 — 네이버 주소로 넣었는데 같은 기사를 전에 원문 주소로 담아 둔 경우.
        # 네이버 페이지의 「기사원문」 링크(original_url)로 담아둔 기사 주소와 대조한다.
        if article.get("original_url"):
            new_keys = original_url_keys(article["original_url"])
            if any(original_url_keys(a.get("url") or "") & new_keys for a in load_manual_articles()):
                self._respond_json({"ok": False, "reason": "이미 담아둔 기사입니다."})
                return
        if not add_manual_article(article, at_top=True):
            # 다른 탭에서 방금 담았을 때만 여기 온다(위에서 이미 걸렀다).
            self._respond_json({"ok": False, "reason": "이미 담아둔 기사입니다."})
            return

        self._regenerate_screens()
        logger.info("수기로 기사 추가 — %s (%s)", article["outlet"], url)
        # 확인 단계 없이 곧바로 담는다 — 화면은 새로고침해 📌 칸 맨 위(입력칸 바로 밑)에 붙은 그 기사 행을
        # 결과로 보여준다(url로 세이지를 칠한다).
        self._respond_json({"ok": True, "url": url})

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
        """확정본 화면의 "담아둔 기사" 소제목 드롭다운이 fetch로 호출한다(promoteManualArticle).

        manual_articles.json에서 그 기사를 빼서 오늘 최신 회차의 실제 기사 목록에
        더한다 — 이제부터 정식 스크랩 결과(화면·복사/내보내기)에 포함된다.
        오늘 회차 자체가 없으면(이 화면이 뜰 수 없는 상황) 되돌린다.

        [수정: 2026-08-11] 예전엔 화면이 소제목을 안 알려줘서, 여기서 회차를 다시 분류해
        "마지막 소제목"에 꽂았다 — 아무 근거 없는 자리라 승격한 기사를 되찾기 어려웠다.
        이제 담당자가 드롭다운에서 고른 소제목이 form["group"]으로 함께 오므로 그대로
        강제 배정한다(소제목 경계를 넘는 다른 기사 이동, app.curation.move_article과 같은
        방식). 재분류가 필요 없어져 여기서 classify_articles를 부르던 것도 없앴다.

        group이 비어 있으면(소제목이 하나도 없는 회차의 📥 버튼) 예전처럼 자동 분류에
        맡긴다. 화면이 낡아 이미 사라진 소제목 이름이 와도 안전하다 — app.classifier
        ._apply_forced_groups가 없는 소제목으로의 강제 이동을 조용히 무시하고 자연
        분류나 "기타"로 떨어뜨린다.
        """
        url = form.get("url", [""])[0]
        group = form.get("group", [""])[0].strip()
        if url:
            run = load_latest_run()
            if run is None:
                self.send_response(409)
                self.end_headers()
                return
            article = pop_manual_article(url)
            if article is not None:
                keywords = all_search_keywords(load_settings())
                custom_names = forced_group_target_names()
                if group:
                    set_group_override(url, group)
                overrides = load_group_overrides()
                # [수정: 2026-07-30] snapshot_group_names로 저장해, 이 회차가 나중에 "지난 기사
                # 더보기"로 넘어가도 방금 승격된 기사가 제 소제목에 들어간 채로 보이게 한다.
                new_articles = snapshot_group_names(
                    run["articles"] + [article], keywords, overrides, custom_names, round_id=round_id_for_run(run)
                )
                update_run_articles(run, new_articles)
                self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_promote_manual_article_to_draft(self, form: dict) -> None:
        """초안 화면의 "담아둔 기사" 소제목 드롭다운이 fetch로 호출한다
        (promoteManualArticleToDraft).

        아직 저장된 회차가 없어(다음 회차가 진행 중일 뿐) _handle_promote_manual_article처럼
        바로 끼워 넣을 곳이 없다 — 그래서 manual_articles.json에서 빼서 예약 목록
        (app.draft_articles)에 옮겨 담아두고, 다음 정식 회차가 실제로 저장될 때
        (app.scraper.collect_run) 자동으로 그 회차 기사 목록에 합쳐지도록 한다. 저장되기
        전에도 초안 화면 자체의 소제목 분류에는 바로 반영된다(app.preview_renderer
        ._compute_preview_articles가 매번 예약 목록을 같이 읽어온다) — 그래서 여기서는
        완성 화면과 달리 정식 회차 파일을 직접 건드리지 않는다.

        [수정: 2026-08-11] 담당자가 고른 소제목(form["group"])을 group_overrides.json에
        같이 기록한다. 이 저장소는 URL 기준 전역이라, 초안 화면에서 고른 소제목이 나중에
        정식 회차가 저장될 때(collect_run의 snapshot_group_names)까지 그대로 살아서
        따라간다 — 초안에서 골라두면 확정본에서도 그 자리에 있다.
        """
        url = form.get("url", [""])[0]
        group = form.get("group", [""])[0].strip()
        if url:
            article = pop_manual_article(url)
            if article is not None:
                if group:
                    set_group_override(url, group)
                add_draft_pending_article(article)
                self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_promote_all_manual_to_draft(self) -> None:
        """📌 담아둔 기사 구획의 "🤖 AI 기사 배정" 1단계 — 담아둔 기사를 **전부** 초안
        예약 목록으로 옮긴다(app.preview_renderer의 assignPinnedArticles JS가 호출).

        단건 승격(_handle_promote_manual_article_to_draft)과 다른 점은 둘뿐이다:
        대상이 전부라는 것, 그리고 **소제목을 지정하지 않는다**는 것. group_overrides에
        아무것도 안 쓰므로 이 기사들은 다음 렌더링에서 📂 소제목 미분류 칸에 모이고,
        JS가 이어서 부르는 /assign-unclassified가 그 칸을 그대로 처리한다 — 배정 로직을
        여기 복제하지 않는 이유다(복제하면 실패 사유 안내·새 소제목 등록·누락분 "기타"
        보정이 두 벌로 갈라진다).

        되돌리기는 undo_push 한 번으로 끝난다 — manual_articles.json과
        draft_pending_articles.json이 2026-08-26부터 app.undo의 스냅샷 대상이라,
        이 조작이 건드리는 상태가 전부 그 한 장에 담긴다.
        """
        try:
            articles = filter_hidden(load_manual_articles())
            if not articles:
                self._respond_json({"moved": 0})
                return
            undo_push("담아둔 기사 AI 배정", touched=[a["url"] for a in articles])
            moved = 0
            for article in articles:
                popped = pop_manual_article(article["url"])
                if popped is not None and add_draft_pending_article(popped):
                    moved += 1
            self._regenerate_screens()
            self._respond_json({"moved": moved})
            return
        except Exception:
            logger.exception("담아둔 기사 일괄 승격 실패")
            self.send_response(500)
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
            undo_push("기사 소제목 이동", touched=urls)
            bulk_reassign_group(urls, target)
            run = load_latest_run()
            if run is not None:
                keywords = all_search_keywords(load_settings())
                overrides = load_group_overrides()
                custom_names = forced_group_target_names()
                snapshot = snapshot_group_names(
                    run["articles"], keywords, overrides, custom_names, round_id=round_id_for_run(run)
                )
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

        [수정: 2026-08-24] _handle_move_article과 같은 이유로 undo_push("기사 순서
        변경")을 추가했다 — 라벨을 똑같이 맞춰서, 단건 ↑/↓와 일괄 ↑/↓를 연달아 눌러도
        (app.undo.push의 3초 coalesce_sec 덕에) 되돌리기 한 번으로 합쳐진다.
        """
        urls = [u for u in form.get("urls", []) if u]
        direction = form.get("direction", [""])[0]
        if urls and direction in ("up", "down"):
            run = load_latest_run()
            if run is not None:
                round_id = round_id_for_run(run)
                keywords = all_search_keywords(load_settings())
                overrides = load_group_overrides()
                new_articles = bulk_move_articles(
                    run["articles"], keywords, urls, direction, overrides, round_id=round_id
                )
                if new_articles is not run["articles"]:
                    undo_push("기사 순서 변경", touched=urls)
                    snapshot = snapshot_group_names(
                        new_articles, keywords, overrides, forced_group_target_names(), round_id=round_id
                    )
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
        """"+ 한 줄 메모"의 저장/삭제 버튼이 fetch로 호출한다(saveKeywordNote/
        clearKeywordNote) — 하단 💬 AI 요약 블록과 무관한, 이용자가 자유
        서식으로 적어두는 메모 한 줄을 저장한다(app.manual_keyword_note). 완성본은
        정적 파일이라 즉시 다시 그려야 다음 접속에도 바로 보인다(초안·실시간 현황은
        요청마다 새로 계산되므로 별도 처리가 필요 없다).

        [수정: 2026-08-07] 완성본(index.html)에서 저장할 때는 그 화면이 보여주는
        회차(run_slot)를 폼에 같이 실어 보낸다 — 안 실어 보내면(초안에서 저장할 때)
        "지금 진행 중인 회차" 기준으로 자동 판단한다(app.manual_keyword_note
        ._current_round_key). 완성본은 이미 끝난 회차를 보여주므로 자동 판단에
        맡기면 그새 시작된 다음 회차로 잘못 붙을 수 있어, 명시적으로 넘겨야 한다.
        """
        text = form.get("text", [""])[0]
        run_slot = form.get("run_slot", [""])[0].strip()
        run_key = (datetime.now().strftime("%Y-%m-%d"), run_slot) if run_slot else None
        save_manual_keyword_note(text, run_key)
        self._regenerate_screens()
        self.send_response(204)
        self.end_headers()

    def _handle_assign_unclassified(self) -> None:
        """📂 소제목 미분류 칸의 "🤖 미분류 배정" 버튼이 fetch로 호출한다(assignUnclassified).

        [수정: 2026-08-12] 예전엔 여기서도 **전체를 다시 분류**(force_llm=True)했는데,
        그게 담당자의 수동 정리(기사 이동·소제목 이름 변경·순서)를 소리 없이 날려버렸다.
        이제는 **증분 배정**만 한다 (사용자와 합의, 2026-08-12):

          - 📂 소제목 미분류에 있는 기사만 대상으로 한다.
          - 기존 소제목 중 맞는 곳에 넣고, 정말 없으면 새 소제목을 만든다.
          - **기존 소제목의 이름·구성은 절대 건드리지 않는다** — LLM에게 "묶어라"가 아니라
            "이 목록 중에서 골라라"를 시키므로 이름을 바꿀 방법 자체가 없다.
          - 담당자가 직접 만든 소제목(custom_groups)은 후보에서 뺀다 — 특정 기사만 넣으려고
            만든 칸일 수 있는데 모델은 그 의도를 알 수 없다.
          - 소제목이 MAX_SUBHEADINGS를 이미 채웠으면 새 이름 대신 "기타"로 보낸다.

        배정 결과는 group_overrides(URL→소제목)에 저장한다 — 초안은 저장된 회차가 없어서
        여기 말고 담아둘 곳이 없고, URL 기준 전역이라 정식 회차가 저장될 때
        (app.scraper.collect_run의 snapshot_group_names)까지 그대로 따라간다.

        [수정: 2026-08-26] 두 가지 실제 버그를 고쳤다(실사용 중 "눌러도 안 된다"로 확인):

        1. classify_articles에 custom_group_names로 담당자가 만든 소제목만 넘기고 있었다.
           그런데 이 버튼이 이전에 새로 지어낸 소제목(예: "세제개편 관련")은 그 목록에도
           없고 캐시에도 아직 없는 이름이라, 다음 렌더링에서 app.classifier.
           _apply_forced_groups가 "분류 결과에 없는 이름"으로 보고 조용히 무시했다 —
           방금 저장한 override가 화면에서는 다시 미분류로 보였다. forced_group_target_
           names()(app.custom_groups + app.assigned_groups)로 바꿔 새로 지은 이름도
           목적지로 계속 남게 했고, 성공 시 add_assigned_group으로 등록한다.
        2. 배정할 후보가 없거나(existing=[]) 모델이 정말 맞는 곳이 없다고 답했을 때
           (assigned=None) 204만 돌려줘 JS가 아무 안내 없이 새로고침만 했다 — 버튼을
           눌러도 미분류가 그대로인데 왜인지 알 방법이 없었다. 항상 200 JSON으로
           {"assigned": [...], "reason": ...}를 돌려주고, JS(assignUnclassified)가
           reason별 안내를 띄운다.

        후보 소제목은 이름뿐 아니라 표시 이름(담당자가 ✏️로 고친 이름)과 대표 기사 제목
        몇 건을 함께 모델에 보여준다(build_assign_candidates) — 이름 하나로는 안 잡히는
        분류 의도(예: "세제개편 관련"이라는 이름에 가업상속공제 기사가 이미 있다는 사실)를
        전달하기 위해서다.
        """
        try:
            settings = load_settings()
            slot = next_pending_slot(datetime.now())
            if slot is None:
                self.send_response(409)
                self.end_headers()
                return
            articles = _compute_preview_articles(settings, slot)
            custom_names = set(load_custom_groups())
            overrides = load_group_overrides()
            round_id = round_id_for_slot(slot)
            groups = classify_articles(
                articles,
                all_search_keywords(settings),
                forced_groups=overrides,
                custom_group_names=forced_group_target_names(),
                allow_llm_call=False,
                round_id=round_id,
            )
            pending = [
                a for g in groups if g["name"] == UNCLASSIFIED_GROUP_NAME for a in g["articles"]
            ]
            if not pending:
                self._respond_json({"assigned": [], "reason": "no_pending"})
                return

            # 담당자가 직접 옮긴 기사(override로 그 소제목에 들어간 것)를 대표 기사로
            # 우선 보여준다 — AI 자신의 자동 분류 결과보다 신뢰할 수 있는 신호다.
            priority_urls = {
                url for url, name in overrides.items() if name not in (UNCLASSIFIED_GROUP_NAME, "기타")
            }
            candidates = build_assign_candidates(
                groups, custom_names, load_group_labels(round_id), priority_urls
            )
            if not candidates:
                # 끼워 넣을 소제목이 하나도 없다(첫 분류의 기사를 전부 숨겨 모두 미분류가 된 회차
                # 등) — 흐트러질 기존 소제목이 없으니 처음부터 분류한다. 담당자의 이동(override)은
                # classify_articles가 그대로 다시 얹는다.
                undo_push("AI 소제목 분류")
                classify_articles(
                    articles,
                    all_search_keywords(settings),
                    forced_groups=overrides,
                    custom_group_names=forced_group_target_names(),
                    force_llm=True,
                    round_id=round_id,
                )
                if llm_last_classification_was_rule_based():
                    self._respond_json({"assigned": [], "reason": "api_error"})
                    return
                self._regenerate_screens()
                self._respond_json({"assigned": [a["url"] for a in pending]})
                return

            assigned = assign_to_existing(pending, candidates)
            if not assigned:
                # 호출 실패·후보 없음·모델이 정말 맞는 곳이 없다고 답함 — 아무것도 바꾸지
                # 않는다(미분류 상태 유지가 더 안전하다). 이유는 화면에 그대로 알린다.
                self._respond_json({"assigned": [], "reason": last_assign_failure_reason() or "no_match"})
                return

            undo_push("AI 소제목 분류")
            existing = [c["name"] for c in candidates]
            room = MAX_SUBHEADINGS - len(existing)
            accepted_new: set = set()
            for url, name in assigned.items():
                if name not in existing:
                    # 모델이 제안한 새 소제목 — 자리가 남아 있을 때만 받아들인다.
                    if name not in accepted_new:
                        if len(accepted_new) >= room:
                            name = "기타"
                        else:
                            accepted_new.add(name)
                set_group_override(url, name)
            # 새로 생긴 소제목은 등록해둬야 다음 렌더링에서도 목적지로 남는다(위 [수정:
            # 2026-08-26] 1번) — "기타"로 떨어진 것은 새 소제목이 아니라 제외한다.
            for name in accepted_new:
                add_assigned_group(name)
            # [수정: 2026-08-24] assign_to_existing이 돌려주는 assigned는 pending의
            # 부분집합일 수 있다 — 모델이 응답에서 기사를 통째로 빠뜨리거나(article_id를
            # 아예 안 씀), "기타"/빈 문자열로 답해 그 함수 내부에서 걸러졌을 때다. 예전엔
            # 그런 기사가 조용히 미분류 상태 그대로 남아, "한 번 눌렀는데 일부만 처리됐다"
            # (예: 5건 중 2건만 배정)로 보이고 왜 나머지가 안 됐는지 안내도 없었다 —
            # classify_with_llm._parse_groups의 "빠뜨린 기사는 기타로 모은다"(§전체
            # 재분류는 한 건도 안 빠짐)와 똑같은 보장을 여기도 준다. "기타"는 새 소제목이
            # 아니라 항상 열려 있는 칸이라 room 계산과 무관하게 바로 보낼 수 있다.
            leftover_urls = {a["url"] for a in pending} - set(assigned)
            for url in leftover_urls:
                set_group_override(url, "기타")
                assigned[url] = "기타"  # 응답·화면 하이라이트에도 포함시킨다
            self._regenerate_screens()
            # 배정된 URL을 돌려준다 — 화면이 그 기사들만 연보라로 한 번 표시한다
            # (어디에 배정됐는지 담당자가 바로 찾을 수 있도록).
            self._respond_json({"assigned": list(assigned)})
            return
        except Exception:
            logger.exception("미분류 기사 분류 실패")
            self.send_response(500)
        self.end_headers()

    def _handle_split_group(self, form: dict) -> None:
        """초안 하단바의 「AI 기사 나누기」 — 규칙은 _split_group 참고."""
        self._split_group(form, final=False)

    def _handle_split_group_final(self, form: dict) -> None:
        """확정본 하단바의 「AI 기사 나누기」 — 규칙은 _split_group 참고. [추가: 2026-09-15]"""
        self._split_group(form, final=True)

    def _split_group(self, form: dict, final: bool) -> None:
        """하단바의 「AI 기사 나누기」가 fetch로 호출한다(splitSelected, app.renderer.split_button_script).

        [추가: 2026-09-15] 사용자 결정 — 체크한 기사(한 소제목 안, 전체든 일부든)만 AI가 쟁점별로
        새 소제목에 나눠 담는다. **다른 소제목은 이름·구성·순서 한 글자도 안 건드린다** — 그게
        「AI 모든 기사 재분류」와 가르는 선이다. 시안은 SUBHEADING_SPLIT_MOCKUP.html.

        두 화면이 다른 건 **"지금 화면에 보이는 소제목"을 구하는 방법** 하나다 — 초안은 분류
        결과(classify_articles, 캐시만), 확정본은 회차 파일에 저장된 배정(groups_for_confirmed_run).
        각 화면의 렌더러와 같은 식으로 구해야 화면이 본 소제목과 서버가 나누는 소제목이 같다.

        저장은 「AI 기사 배정」(_handle_assign_unclassified)과 **같은 경로**다 — 옮긴 기사는
        group_overrides(URL → 새 소제목), 새 이름은 assigned_groups에 등록한다. 그래야 초안에서
        나눈 결과가 마감 후 확정본(app.scraper.collect_run → classify_for_finalize가 같은 두
        저장소를 읽는다)에도 그대로 따라간다. LLM 캐시에 써넣는 방식은 쓰지 않는다 — 새 기사가
        들어올 때마다 어느 캐시 항목이 재사용될지 달라져 결과가 흔들린다. 확정본은 여기에 더해
        **회차 파일의 그 기사들 배정만 직접 고쳐 쓴다** — 스냅샷을 통째로 다시 계산
        (snapshot_group_names)하지 않으므로 다른 소제목이 건드려질 여지 자체가 없다.

        새 소제목은 원래 소제목 **바로 뒤**에 끼운다(group_order) — 목록 끝에 붙으면 "그 칸이
        나뉘었다"로 안 읽힌다. 어느 기사를 어디로 옮길지(이름 겹침·칸 수 상한·"안 나뉨" 판정)는
        전부 app.group_split.plan_split 한곳에서 정한다.

        응답은 항상 200 JSON — {"moved": [url…]} 또는 {"moved": [], "reason": …}.
        화면이 moved만 연보라로 한 번 칠하고(「AI 기사 배정」 뒤와 같은 표시), reason이면
        이유별 안내를 띄운 채 아무것도 안 바꾼다.
        """
        try:
            urls = [u for u in form.get("urls", []) if u]
            posted_group = form.get("group", [""])[0]
            settings = load_settings()
            keywords = all_search_keywords(settings)
            custom_names = load_custom_groups()
            run = None
            if final:
                run = load_latest_run()
                if run is None:
                    self.send_response(409)
                    self.end_headers()
                    return
                round_id = round_id_for_run(run)
                # app.renderer.render_page와 같은 식 — 숨긴 기사를 빼고 저장된 배정을 읽는다.
                groups = groups_for_confirmed_run(
                    filter_hidden(run["articles"]),
                    keywords,
                    forced_groups=load_group_overrides(),
                    custom_group_names=custom_names,
                    round_id=round_id,
                )
            else:
                slot = next_pending_slot(datetime.now())
                if slot is None:
                    self.send_response(409)
                    self.end_headers()
                    return
                round_id = round_id_for_slot(slot)
                groups = classify_articles(
                    _compute_preview_articles(settings, slot),
                    keywords,
                    forced_groups=load_group_overrides(),
                    custom_group_names=forced_group_target_names(),
                    allow_llm_call=False,
                    round_id=round_id,
                )
            labels = load_group_labels(round_id)
            existing = {g["name"] for g in groups}
            render_groups = apply_group_order(
                groups + [{"name": n, "articles": []} for n in custom_names if n not in existing],
                round_id,
            )
            selected_set = set(urls)
            owners = [g for g in render_groups if any(a["url"] in selected_set for a in g["articles"])]
            if len(owners) > 1:
                self._respond_json({"moved": [], "reason": "mixed"})
                return
            group = owners[0] if owners else None
            # 화면이 본 소제목과 지금 서버가 계산한 소제목이 다르면(그 사이 다른 탭에서 옮김·숨김)
            # 아무것도 안 한다 — 담당자가 고른 적 없는 묶음을 나누면 안 된다.
            selected = [a for a in group["articles"] if a["url"] in selected_set] if group else []
            if group is None or group["name"] != posted_group or len(selected) != len(selected_set):
                self._respond_json({"moved": [], "reason": "stale"})
                return
            if group["name"] == UNCLASSIFIED_GROUP_NAME:
                self._respond_json({"moved": [], "reason": "unclassified"})
                return
            if len(selected) < MIN_SPLIT_ARTICLES:
                self._respond_json({"moved": [], "reason": "too_few"})
                return
            subheading_count = sum(1 for g in render_groups if g["name"] != UNCLASSIFIED_GROUP_NAME)
            if subheading_count >= MAX_SUBHEADINGS:
                self._respond_json({"moved": [], "reason": "full"})
                return

            display = display_group_name(group["name"], labels)
            other_names = set()
            for g in render_groups:
                if g["name"] not in (group["name"], UNCLASSIFIED_GROUP_NAME):
                    other_names |= {g["name"], display_group_name(g["name"], labels)}
            # 「기타」는 "겹치지 말 것" 목록에서 뺀다 — 어디에도 안 맞는 기사는 기타로 보내라는
            # 분류 프롬프트 규칙과 부딪힌다(이미 있는 기타로는 plan_split이 합쳐 준다).
            llm_groups = split_group_articles(
                selected,
                MAX_SUBHEADINGS - subheading_count + 1,
                display,
                sorted(n for n in other_names if n != ETC_GROUP_NAME),
            )
            if llm_groups is None:
                self._respond_json({"moved": [], "reason": last_split_failure_reason() or "api_error"})
                return
            plan = plan_split(
                group["name"],
                [a["url"] for a in selected],
                llm_groups,
                whole=len(selected) == len(group["articles"]),
                other_names=other_names,
                subheading_count=subheading_count,
                etc_exists=ETC_GROUP_NAME in existing,
                original_display=display,
            )
            if plan is None:
                self._respond_json({"moved": [], "reason": "no_split"})
                return

            undo_push("AI 기사 나누기")
            moves = plan["moves"]
            for url, name in moves.items():
                set_group_override(url, name)
            # 새 이름은 등록해둬야 다음 렌더링에서도 목적지로 남는다(「AI 기사 배정」과 같은 이유 —
            # 캐시에 없는 이름으로의 배정은 _apply_forced_groups가 조용히 무시한다).
            for name in plan["new_names"]:
                add_assigned_group(name)
            order = [g["name"] for g in render_groups if not is_order_locked(g["name"], labels)]
            save_group_order(
                order_with_new_names(order, group["name"], plan["new_names"], {ETC_GROUP_NAME, UNCLASSIFIED_GROUP_NAME}),
                round_id,
            )
            if run is not None:
                # 확정본은 저장된 배정을 읽어 그리므로 옮긴 기사의 group만 고쳐 쓴다. 요약은 그
                # 소제목에 원래 있던 것(기타로 합친 경우)을, 새 소제목이면 빈 값을 둔다 —
                # 초안에서 나눈 소제목과 같은 모양(첫 기사 발췌)이고, 나중에 다른 큐레이션이
                # 스냅샷을 다시 계산해도 같은 값이 나온다.
                summary_of = {}
                for a in run["articles"]:
                    summary_of.setdefault(a.get("group"), a.get("group_summary", ""))
                new_set = set(plan["new_names"])
                update_run_articles(
                    run,
                    [
                        dict(
                            a,
                            group=moves[a["url"]],
                            group_summary="" if moves[a["url"]] in new_set else summary_of.get(moves[a["url"]], ""),
                        )
                        if a["url"] in moves
                        else a
                        for a in run["articles"]
                    ],
                )
            self._regenerate_screens()
            self._respond_json({"moved": list(moves), "groups": plan["new_names"]})
            return
        except Exception:
            logger.exception("소제목 나누기 실패")
            self.send_response(500)
        self.end_headers()

    def _handle_regenerate_subheadings_draft(self) -> None:
        """초안 툴바의 "↻ 전체 기사 재분류" 버튼이 fetch로 호출한다(regenerateSubheadings,
        app/preview_renderer.py) — _handle_assign_unclassified(증분)와 달리 **전체를
        처음부터 다시 묶는다**(force_llm=True).

        [추가: 2026-08-12] "🤖 미분류 배정"은 기존 소제목을 절대 안 건드리지만, 회차
        초반 몇 건으로 잡은 분류가 기사가 30건 넘게 쌓이면 안 맞을 수 있다 — 확정본까지
        기다리면 이미 발송 대상이라 늦다(사용자 요청). 확정본의 같은 버튼과 동일하게
        기존 소제목 이름·구성·순서를 전부 새로 지으므로 파괴적이다 — 클라이언트가 같은
        문구의 확인창을 먼저 띄운다.

        확정본과 달리 결과를 파일에 저장하지 않는다 — 초안은 저장된 회차가 없고, 새로
        분류한 결과는 app.llm_classifier._cache(이제 디스크에도 남는다)에 남아 다음
        렌더링의 classify_articles(allow_llm_call=False) 호출이 그대로 읽어간다.
        """
        try:
            settings = load_settings()
            slot = next_pending_slot(datetime.now())
            if slot is None:
                self.send_response(409)
                self.end_headers()
                return
            articles = _compute_preview_articles(settings, slot)
            round_id = round_id_for_slot(slot)
            undo_push("AI 소제목 분류")
            classify_articles(
                articles,
                all_search_keywords(settings),
                forced_groups=load_group_overrides(),
                custom_group_names=forced_group_target_names(),
                force_llm=True,
                round_id=round_id,
            )
            # [추가: 2026-08-20] 담당자가 직접 누른 재분류 시도의 성공/실패를 기록한다
            # (app.reclassify_attempts) — 연속 2회 실패하면 초안 배너가 "재시도" 대신
            # "이번 회차는 수기로 정리하세요"로 바뀐다. 기사 수 초과로 매번 똑같이
            # 실패하는 경우는 재시도 횟수와 무관하게 별도 문구가 우선하므로 여기서는
            # 그냥 성공/실패만 남기고 판단은 app.preview_renderer가 한다.
            record_reclassify_attempt(round_id, success=not llm_last_classification_was_rule_based())
            self._regenerate_screens()
            self.send_response(204)
        except Exception:
            logger.exception("초안 전체 재분류 실패")
            self.send_response(500)
        self.end_headers()

    def _handle_regenerate_subheadings_final(self) -> None:
        """확정본의 "🤖 소제목 분류 요청하기" 버튼이 fetch로 호출한다(regenerateSubheadings,
        app/renderer.py).

        [추가: 2026-08-11] 확정본은 매 렌더링마다 기본적으로 재분류되지만(캐시 우선 —
        기사 구성이 그대로면 API를 다시 안 부름), 지금 나온 소제목 이름·묶음이 마음에
        안 들어 캐시를 무시하고 AI에게 새로 물어보고 싶을 때 쓰는 버튼이다. 초안의
        regen 핸들러와 달리 여기서는 결과를 실제로 회차 파일에 저장까지 해야 한다 —
        확정본은 preview.html처럼 요청마다 새로 계산하는 화면이 아니라 정적 파일이라,
        `update_run_articles`로 "group" 스냅샷을 갱신하고 `_regenerate_screens()`로
        즉시 다시 그려야 새로고침 없이도 반영된다(app.settings_server의 다른 큐레이션
        핸들러 — 예: `_handle_bulk_move_article` — 와 동일한 저장 패턴).
        """
        try:
            run = load_latest_run()
            if run is None:
                self.send_response(409)
                self.end_headers()
                return
            keywords = all_search_keywords(load_settings())
            overrides = load_group_overrides()
            custom_names = forced_group_target_names()
            undo_push("AI 소제목 분류")
            snapshot = snapshot_group_names(
                run["articles"], keywords, overrides, custom_names, force_llm=True, round_id=round_id_for_run(run)
            )
            update_run_articles(run, snapshot)
            self._regenerate_screens()
            self.send_response(204)
        except Exception:
            logger.exception("확정본 소제목 다시 만들기 실패")
            self.send_response(500)
        self.end_headers()

    def _handle_undo(self) -> None:
        """↩ 되돌리기 버튼이 fetch로 호출한다(undoLastAction, 확정본·초안 공통).

        [추가: 2026-08-11] 되돌릴 동작마다 역연산을 따로 만드는 대신 app/undo.py가
        큐레이션 상태를 통째로 스냅샷해뒀다가 복원한다 — 숨기기·소제목 이동·이름 변경·
        AI 재분류가 전부 같은 경로로 되돌아간다(자세한 설계 이유는 app/undo.py 참고).

        상태 복원 뒤 회차 파일의 "group" 스냅샷을 다시 맞춰준다 — 이 필드는 화면에는
        안 쓰이지만(확정본은 매번 새로 분류함) 나중에 이 회차가 "지난 기사"로 넘어갔을 때
        쓰이므로, 되돌린 결과와 어긋난 채로 두면 그때 엉뚱한 소제목으로 보인다.
        복원된 LLM 캐시를 그대로 쓰므로 여기서 API를 새로 부르지는 않는다.
        """
        restored = undo_restore()
        if restored is None:
            self.send_response(409)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        try:
            run = load_latest_run()
            if run is not None:
                snapshot = snapshot_group_names(
                    run["articles"],
                    all_search_keywords(load_settings()),
                    load_group_overrides(),
                    forced_group_target_names(),
                    round_id=round_id_for_run(run),
                )
                update_run_articles(run, snapshot)
            self._regenerate_screens()
        except Exception:
            logger.exception("되돌리기 후 화면 갱신 실패 — 상태 복원 자체는 이미 끝났습니다")
        # [추가: 2026-09-15] 되돌린 동작에서 담당자가 손댄 기사 URL — 화면이 새로고침 뒤
        # 그 기사만 세이지로 칠한다(undoLastAction → justMovedUrls, app.undo.push의 touched).
        self._respond_json({"label": restored["label"], "touched": restored["touched"]})

    def _handle_add_label(self, form: dict) -> None:
        """확정본·초안·수시 수집 확정본·[추가: 2026-09-16] 정기 보관함 화면의 🏷 팝오버가
        fetch로 호출한다(attachLabel).
        붙이는 시점의 스냅샷 필드(app.labels.attach_label 참고)를 함께 실어 보낸다 —
        회차 파일이 나중에 지워져도 라벨 보관함이 이 사본만으로 온전히 복원되게 하려면
        붙이는 이 순간에 받아둬야 한다. 응답은 그 기사에 지금 붙어 있는 라벨 전체
        목록(JSON) — 화면은 이 값 하나로 팝오버·카드 줄·배지를 다시 그린다.

        [추가: 2026-08-28] known — 페이지 전체가 공유하는 "이미 쓴 라벨" 칩 목록도 함께
        돌려준다. 그 목록은 페이지를 구울 때 한 번 만들어 박아둔 값이라, 방금 만든
        라벨이 새로고침 전까지 어느 행의 팝오버에도 안 나왔다. 건수까지 정확해야 하므로
        (이 페이지에 없는 기사에 붙은 몫도 포함) 화면이 ±1로 추측하지 않고 서버가
        만든 최종 목록을 그대로 보낸다."""
        url = form.get("url", [""])[0]
        label = form.get("label", [""])[0].strip()
        if url and label:
            article = {
                "url": url,
                "outlet": form.get("outlet", [""])[0],
                "title": form.get("title", [""])[0],
                "pub_date": form.get("pubDate", [""])[0],
                "scrap_date": form.get("scrapDate", [""])[0],
                "scrap_end": form.get("scrapEnd", [""])[0],
            }
            group = form.get("group", [""])[0]
            label_undo_push(f'라벨 "{label}" 붙이기')
            labels_now = attach_label(article, label, snapshot_source("regular"), group=group)
            self._regenerate_screens()
        else:
            labels_now = []
        self._respond_json({"labels": labels_now, "known": known_label_chips_html()})

    def _handle_remove_label(self, form: dict) -> None:
        """라벨 떼기 — 두 곳에서 온다: (1) 확정본·초안·수집 확정본의 🏷 팝오버(fetch,
        JSON 응답 기대) (2) 라벨 보관함 화면의 칩 × 버튼(실제 form 제출, redirect
        기대). redirect 필드의 유무로 호출부를 구분한다."""
        url = form.get("url", [""])[0]
        label = form.get("label", [""])[0].strip()
        redirect_to = form.get("redirect", [""])[0]
        labels_now = []
        if url and label:
            label_undo_push(f'라벨 "{label}" 떼기')
            labels_now = detach_label(url, label)
            self._regenerate_screens()
        if redirect_to:
            self._redirect(redirect_to)
            return
        self._respond_json({"labels": labels_now, "known": known_label_chips_html()})

    def _handle_rename_label(self, form: dict) -> None:
        """라벨 관리 화면의 "이름 변경" — 이름만 바꾼다(합치기는 별도 동작).
        이미 있는 이름과 부딪히면(app.curation의 소제목 이름 충돌과 같은 409 규칙)
        되돌리기 스택에 쌓아둔 "아무 일도 안 한" 스냅샷을 바로 걷어내고, 거절 배너 +
        "⇢ 합치기" 버튼을 보여줄 error 쿼리와 함께 되돌아간다."""
        old = form.get("old", [""])[0].strip()
        new = form.get("new_name", [""])[0].strip()
        if old and new and old != new:
            label_undo_push(f'라벨 "{old}" 이름 변경')
            try:
                rename_label(old, new)
            except LabelCollisionError:
                label_undo_restore()
                self._redirect(f"/label-manage?error=rename_collision&old={quote(old)}&new={quote(new)}")
                return
            self._regenerate_screens()
        self._redirect("/label-manage")

    def _handle_merge_label(self, form: dict) -> None:
        """라벨 관리 화면의 "⇢ 합치기" 드롭다운, 그리고 이름 변경 거절 배너의
        "⇢ 합치기" 버튼 둘 다 여기로 온다 — 둘 다 src(사라질 라벨)·dst(남을 라벨) 필드
        형태가 같다. **이 동작은 되돌리기 없이는 물리적으로 복구가 불가능**하다(합친
        뒤엔 어느 기사가 원래 src였는지 아무 데도 안 남는다) — label_undo_push를
        반드시 먼저 부른다."""
        src = form.get("src", [""])[0].strip()
        dst = form.get("dst", [""])[0].strip()
        if src and dst and src != dst:
            label_undo_push(f'라벨 "{src}"를 "{dst}"에 합치기')
            merge_labels(src, dst)
            self._regenerate_screens()
        self._redirect("/label-manage")

    def _handle_delete_label(self, form: dict) -> None:
        """라벨 관리 화면의 "🗑 삭제" — 이 라벨이 붙은 모든 기사에서 라벨을 뗀다.
        남은 라벨이 없어진 기사는 라벨 저장소에서 항목째 사라진다(=1년 보관 예외가
        풀린다). confirmDelete(JS)가 이미 건수를 보여주고 확인받았지만, 그 확인이
        전부라도 되돌리기는 마지막 안전망으로 남겨둔다."""
        name = form.get("name", [""])[0].strip()
        if name:
            label_undo_push(f'라벨 "{name}" 삭제')
            delete_label(name)
            self._regenerate_screens()
        self._redirect("/label-manage")

    def _handle_undo_label(self) -> None:
        """라벨 보관함·라벨 관리 화면의 좌하단 ↩가 fetch로 호출한다. app/undo.py
        (정기 큐레이션)와 별개의 스택(app/label_undo.py)이므로 이 엔드포인트도 분리했다."""
        label = label_undo_restore()
        if label is None:
            self.send_response(409)
            self.end_headers()
            return
        try:
            self._regenerate_screens()
        except Exception:
            logger.exception("라벨 되돌리기 후 화면 갱신 실패 — 상태 복원 자체는 이미 끝났습니다")
        self.send_response(204)
        self.end_headers()

    def _respond_conflict(self, message: str) -> None:
        """409와 함께 **왜 안 되는지 문장**을 실어 보낸다. [추가: 2026-09-03]

        화면 JS가 이 본문을 그대로 alert에 띄운다 — 본문이 없으면 예전 문구로 물러선다.
        "이미 있습니다"만 던지면 그 이름이 이 화면엔 안 보일 때(다른 회차의 이름표와
        겹친 경우) 담당자가 확인할 방법이 없다.
        """
        body = message.encode("utf-8")
        self.send_response(409)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_add_custom_group(self, form: dict) -> None:
        """"+ 새 소제목 만들기" 버튼이 fetch로 호출한다(createCustomGroup) — 자동
        분류로는 나오지 않는 이름을 미리 만들어, 기사가 없어도 화면에 띄워둔다.
        초안·완성본 둘 다에서 보여야 하므로(전역 목록) 완성본도 즉시 다시 그린다.

        [추가: 2026-08-04] 새로 만들 이름이 지금 이 화면에 이미 떠 있는 다른 소제목의
        표시 이름과 같으면(app.curation.display_name_in_use) 화면에 똑같은 소제목이
        두 개로 보이니 409로 막는다. [수정: 2026-08-04] 검사 범위는 app.settings_server
        ._handle_rename_group과 같은 이유로 "이 화면에 지금 같이 떠 있는 소제목"으로
        좁혔다 — 회차끼리는 같은 이름이어도 상관없다.

        [수정: 2026-09-16] **다른 회차의 이름표와 겹치는지는 더는 안 본다**(사용자 결정 —
        2026-09-03에 넣었던 _other_round_using_name/_round_still_shows/_live_round_ids를
        통째로 들어냈다). 같은 사안을 회차마다 이어서 볼 때 **같은 소제목 이름을 계속 쓰는
        것이 오히려 정상 업무**인데, 그걸 막을 만한 대가가 아니었다: 갓 만든 소제목은 비어
        있고 빈 소제목은 복사·txt·발송 텍스트에서 걸러지므로(app.renderer.render_page의
        `groups = [g for g in render_groups if g["articles"]]`) **보고서엔 두 줄이 안 나간다**,
        정기 보관함은 커스텀 소제목을 아예 안 그리고 회차별로 시간순으로 펼쳐지므로 거기서도
        안 겹친다. 남는 건 직전 확정본 화면에 빈 소제목 한 줄이 더 뜨는 것뿐이다.
        **같은 화면 안 중복(display_name_in_use)은 그대로 막는다** — 그건 한 화면·한 보고서에
        같은 제목이 두 번 찍히는 진짜 사고다.
        """
        name = form.get("name", [""])[0].strip()
        active_names = set(form.get("active_names", []))
        round_id = _resolve_round_id(form)
        if name and display_name_in_use(name, active_names, round_id=round_id):
            self._respond_conflict("이미 '%s'라는 이름의 소제목이 이 화면에 있습니다." % name)
            return
        else:
            # [추가: 2026-09-03] 새로 만드는 소제목은 **항상 빈 칸으로 시작한다** — 그
            # 이름을 가리키던 옛 배정 기록을 먼저 끊는다(clear_group_overrides_for).
            # 안 끊으면 _apply_forced_groups가 과거에 그 이름으로 옮겼던 기사를 전부
            # 다시 끌어와, 방금 만든 칸이 이미 차 있다(app.curation 같은 함수 참고).
            # 실제로 지운 게 있을 때만 되돌리기를 쌓는다 — 지울 게 없으면 소제목 만들기는
            # 원래 잃을 게 없는 동작이라, 매번 쌓으면 ↩ 스택만 의미 없이 밀린다.
            # (custom_groups.json도 되돌리기 스냅샷 대상이라 ↩ 한 번이면 소제목 생성과
            # 배정 기록이 함께 되돌아간다.)
            # undo_push는 "동작 **전** 상태"를 찍으므로 반드시 지우기보다 먼저 부른다.
            if name and name in load_group_overrides().values():
                undo_push("소제목 만들기")
                clear_group_overrides_for(name)
            # [수정: 2026-08-27] round_id는 위 중복 검사에만 쓴다 — add_custom_group은
            # 더 이상 group_order.json을 건드리지 않는다(새 소제목은 자연 순서대로 맨 아래).
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

    def _handle_add_trend_word(self, form: dict) -> None:
        """정책 단어 추이 전용 화면(/trend)의 "+ 추가"·등록 검색어 칩·"추이에 추가" 버튼
        공용 — 실패(중복·최대 개수 초과·빈 값)해도 조용히 무시하고 같은 조회 구간으로
        되돌린다. 화면이 이미 다 찼을 땐 입력 폼 자체를 안 그리고, 등록 검색어 목록을
        <datalist>로 자동완성 제안하므로 실패가 흔한 경로는 아니다 — 그래서 실패 사유를
        화면에 되돌려 보여주는 장치(querystring 등)까지는 만들지 않았다
        (app.home_trend.add_trend_word). 홈 카드도 같은 저장소를 읽으므로 여기서 고른
        단어가 홈에도(전부) 즉시 반영된다."""
        add_trend_word(form.get("word", [""])[0])
        self._redirect(self._trend_redirect_path(form))

    def _handle_remove_trend_word(self, form: dict) -> None:
        """추이 칩의 × 버튼 — 목록에 없는 단어를 지우려 해도 그냥 조용히 넘어간다."""
        remove_trend_word(form.get("word", [""])[0])
        self._redirect(self._trend_redirect_path(form))

    @staticmethod
    def _trend_redirect_path(form: dict) -> str:
        """단어 추가/삭제 폼이 hidden input으로 실어 보낸 현재 조회 구간(start/end)을
        그대로 붙여 되돌아간다 — 안 그러면 6개월 보던 중 단어 하나 추가했다고 기본
        7일 화면으로 튕겨나간다."""
        start, end = form.get("start", [""])[0], form.get("end", [""])[0]
        return f"/trend?start={quote(start)}&end={quote(end)}" if start and end else "/trend"

    def _handle_download_trend_excel(self, form: dict) -> None:
        """정책 단어 추이 화면의 "⬇ 엑셀" — app.excel_export의 기사 8열 스키마와는
        다른 모양(단어×기간 표)이라 전용 빌더(app.trend_export)를 쓴다. rows/labels는
        렌더링 시점에 이미 계산된 값을 hidden input에 그대로 심어 받는다(화면과
        내보내기 내용이 항상 일치하도록, 확정본 등과 같은 관례)."""
        try:
            labels = json.loads(form.get("labels", ["[]"])[0])
            rows = json.loads(form.get("rows", ["[]"])[0])
        except json.JSONDecodeError:
            self._respond('<p style="font-family:sans-serif;padding:24px;">요청이 올바르지 않습니다. 새로고침 후 다시 시도해주세요.</p>', status=400)
            return
        filename = form.get("filename", ["정책단어추이.xlsx"])[0]
        self._respond_excel_download(build_trend_workbook_bytes(labels, rows), filename)

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
        (app.preview_renderer.preview_move_article 등, JS가 파싱해서 DOM 조각을 갈아끼움).
        index.html/history.html은 file://로 열려 있을 수 있어(_handle_hide_article과
        같은 이유) CORS를 항상 열어둔다 — 로컬 단일 사용자 도구라 위험 없음."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def _respond_download(self, text: str, filename: str) -> None:
        """텍스트를 파일 다운로드로 응답한다(data: URI 대신 — _handle_download_text 참고).

        filename에 한글이 흔하므로(예: "2026-08-12_언론모니터링_17-00기준.txt") RFC 5987
        filename*=UTF-8''... 형태를 filename=(ASCII 폴백)과 함께 보낸다 — 최신 브라우저는
        filename*=를 우선 쓰고, 혹시 못 알아듣는 환경만 ASCII 폴백으로 받는다.
        """
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"download.txt\"; filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_excel_download(self, body: bytes, filename: str) -> None:
        """엑셀 바이트를 파일 다운로드로 응답한다 — _respond_download의 xlsx판(같은
        filename*=UTF-8'' 처리, 본문만 바이너리)."""
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"download.xlsx\"; filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_static_binary(self, path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 로컬 단일 사용자 도구라 매 요청을 콘솔에 찍지 않는다.


def create_settings_server(port: int = SETTINGS_SERVER_PORT) -> ThreadingHTTPServer:
    """설정 서버 소켓을 **지금 이 자리에서** 열어 돌려준다(serve_forever는 아직 안 부른다).

    ThreadingHTTPServer인 건 전체 기사(live.html)처럼 수십 초 걸리는 요청이 다른 화면을
    막지 않게 하려고서다. SERVER_HOST(.env)가 기본값 127.0.0.1이 아닐 때만 0.0.0.0으로 연다.

    bind를 serve_forever와 떼어 둔 이유는 하나다 — main.py가 이 bind를
    **앱 중복 실행 자물쇠**로 쓰기 때문이다. 예전엔 bind가 데몬 스레드 안에서 일어나서,
    두 번째로 켠 앱은 "Address already in use"로 그 스레드만 조용히 죽고 **스케줄러
    스레드는 멀쩡히 계속 돌았다** — 회차를 두 번 수집·저장하고, 서로의 LLM 분류 캐시를
    덮어쓰고, 초안 순서를 지워버렸다(2026-09-02 사고, HISTORY.md 참고).

    bind를 main 스레드에서 동기로 하면 그 실패가 "이미 앱이 켜져 있다"는 신호가 되어
    main.py가 곧바로 물러날 수 있다. 실측(2026-09-02): HTTPServer는 allow_reuse_address=1
    (SO_REUSEADDR)이지만, 다른 소켓이 **LISTEN 중인** 같은 주소·포트에 거는 bind는
    그래도 EADDRINUSE로 실패한다 — 자물쇠로 쓰기에 안전하다. PID 파일과 달리 앱이 강제
    종료돼도 OS가 포트를 알아서 풀어주므로 "죽은 자물쇠"가 남지 않는다.
    """
    bind_host = "0.0.0.0" if SETTINGS_SERVER_HOST != "127.0.0.1" else "127.0.0.1"
    return ThreadingHTTPServer((bind_host, port), _SettingsHandler)
