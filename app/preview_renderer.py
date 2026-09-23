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
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from app.topnav import regular_nav, topnav_style
from app.auto_classify_turn import take_turn as take_auto_classify_turn
from app.classifier import classify_articles, forced_group_target_names
from app.llm_classifier import (
    MAX_ARTICLES_FOR_CLASSIFY,
    MIN_ARTICLES_FOR_AUTO,
    UNCLASSIFIED_GROUP_NAME,
    UNCLASSIFIED_DISPLAY_TEXT,
    UNCLASSIFIED_ICON,
    round_id_for_slot,
)
from app.llm_classifier import cached_group_names as llm_cached_group_names
from app.llm_classifier import last_classification_was_rule_based as llm_last_classification_was_rule_based
from app.llm_classifier import (
    last_classification_skipped_too_many_articles as llm_last_skipped_too_many_articles,
)
from app.llm_classifier import (
    last_classification_failed_credit_error as llm_last_credit_error,
)
from app.reclassify_attempts import consecutive_failures as reclassify_consecutive_failures
from app.reclassify_attempts import recent_attempts as reclassify_recent_attempts
from app.config import (
    COLLECT_LOOKBACK_MIN,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    FONT_STACK,
    HIGHLIGHT_COLORS,
    PALETTE,
    RECLASSIFY_SAFE_NOTE,
    PREVIEW_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.assigned_groups import load_assigned_groups
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
from app.subheading_names import name_pool
from app.undo import peek_label as undo_peek_label
from app.undo import push as undo_push
from app.summary_overrides import apply_summary_overrides
from app.manual_keyword_note import load_manual_keyword_note
from app.group_order import apply_group_order, is_order_locked
from app.draft_articles import add_draft_pending_article, load_draft_pending_articles
from app.draft_seen import load_draft_seen, record_draft_seen, search_condition
from app.preview_cache import load_preview_cache, save_preview_cache
from app.preview_order import apply_preview_order, load_preview_order, save_preview_order
from app.filters import (
    deduplicate_by_title,
    filter_by_outlet_whitelist,
    looks_like_photo_caption,
    sort_scoop_first,
)
from app.highlight import highlight_keywords
from app.icons import icon
from app.manual_articles import load_manual_articles
from app.naver_api import incremental_search_after, kst_today_at, search_articles_by_groups
from app.renderer import (
    AI_SUMMARY_HEADING,
    build_export_rows,
    export_links_style,
    _format_summary_title,
    _group_option_label,
    _promotable_group_names,
    _render_manual_section,
    keyword_note_template_vars,
    apply_line_template,
    apply_subheading_format,
    format_slot_time_kr,
    known_label_chips_html,
    late_badge_style,
    kw_inline_style,
    kw_inline_script,
    photo_gather_button_html,
    photo_gather_script,
    photo_gather_style,
    render_article,
    hidden_trash_html,
    hidden_trash_script,
    hidden_trash_style,
    scroll_top_html,
    scroll_top_script,
    scroll_top_style,
    refresh_link_html,
    refresh_link_script,
    refresh_link_style,
    hide_batch_script,
    hide_batch_style,
    range_select_script,
    pinned_jump_badge_html,
    URL_ADD_BUTTON_HTML,
    URL_ADD_PANEL_HTML,
    url_add_script,
    url_add_style,
    label_popover_script,
    label_popover_style,
    name_picker_html,
    name_picker_script,
    name_picker_style,
    split_button_html,
    split_button_script,
    split_button_style,
    screen_tag_style,
)
from app.scheduler import next_pending_slot
from app.scraper import _already_published_urls, _slot_end_for_pub_date
from app.settings import active_schedule_times, active_search_groups, all_search_keywords, group_in_scrap, load_settings
from app.sorter import sort_by_outlet_priority
from app.summarizer import summarize_groups

logger = logging.getLogger(__name__)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{page_title}</title>
<style>
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: var(--r-lg); padding-top: 44px; padding-bottom: 56px;  /* [수정: 2026-09-16] 60→44px, app/renderer.py 같은 자리 참고 */
  }}
{topnav_style}
  /* [추가: 2026-08-18] 회차 종료(카운트다운 0) 안내 띠 — round-countdown이 0에 닿아도
     예전엔 화면이 아무 말도 안 해서, 담당자가 마감된 초안인 줄 모르고 계속 고치다
     그 작업이 어디에도 반영 안 되는 사고가 있었다(HISTORY.md 2026-08-18). topbar처럼
     position:fixed로 맨 위에 붙이고, JS(showRoundOverBanner)가 topbar를 그만큼
     아래로 밀어낸다 — 사용자가 목업 3안 중 "상단 고정 띠"를 골랐다: 스크롤 위치와
     무관하게 항상 보여야 놓치지 않는다는 이유. */
  .round-over-banner {{
    display: none; position: fixed; top: 0; left: 0; right: 0; z-index: 21;
    background: {round_over_bg}; border-bottom: 1px solid {round_over_border};
  }}
  .round-over-banner.is-visible {{ display: block; }}
  .round-over-banner-inner {{
    max-width: 800px; margin: 0 auto; padding: 11px 24px;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }}
  .round-over-banner .msg {{ flex: 1; min-width: 200px; font-size: var(--fs-md); color: {round_over_text}; }}
  .round-over-banner .msg b {{ color: {round_over_text_strong}; }}
  .round-over-banner .go {{
    background: {header}; color: {on_fill}; border: none; border-radius: var(--r-md);
    padding: 8px 14px; font-size: var(--fs-md); font-weight: 700; cursor: pointer; white-space: nowrap;
    text-decoration: none; display: inline-block;
  }}
  .round-over-banner .go:hover {{ background: {header_pressed}; }}
  .round-over-banner .dismiss {{
    background: transparent; border: none; color: {round_over_dismiss}; font-size: var(--fs-sm);
    cursor: pointer; text-decoration: underline; padding: 4px;
  }}
  /* [추가: 2026-08-20] AI 소제목 분류가 규칙 기반(단어 빈도)으로 떨어졌을 때의 경고.
     이 폴백은 여태 화면에 아무 흔적을 안 남겨서, 담당자가 <부총리>처럼 뭉개진 소제목을
     눈으로 발견할 때까지 아무도 몰랐다(2026-08-20 09:30 회차). 회차 마감 띠와 달리
     화면 위에 고정하지 않고 기사 목록 맨 위에 흐르는 박스로 둔다 — 마감은 "지금 하던
     일이 헛수고가 된다"는 즉시성이 있지만 이건 "지금 보이는 소제목을 믿지 마라"라서
     목록과 함께 스크롤되는 편이 맞다.
     [수정: 2026-08-20] 배너 안에 붉은 실행 버튼을 따로 두지 않는다 — 툴바에 이미
     같은 라벨의 "전체 기사 재분류" 버튼이 있고(스크롤 없이 90~120px 위, 실측), 같은
     라벨의 버튼이 화면에 둘이면 "다른 건가?"라는 새 의문만 늘어난다. 대신 문구가
     .btn-ref로 툴바 버튼을 가리킨다. */
  .classify-degraded {{
    background: {degraded_bg}; border: 1px solid {degraded_border}; border-left: 4px solid {error};
    border-radius: var(--r-lg); padding: 12px 14px; margin-bottom: 14px;
  }}
  .classify-degraded .msg {{ font-size: var(--fs-md); color: {degraded_text}; line-height: 1.6; }}
  .classify-degraded .msg b {{ color: {error}; }}
  /* [추가: 2026-08-20] 문구 안에서 툴바 버튼을 가리키는 인라인 칩 — 실제 그 버튼과
     같은 색·같은 아이콘(app/icons.py "bot")을 써서 "이게 그 버튼이다"가 바로
     읽히게 한다(app.preview_renderer._actions_html의 .reclassify-btn과 배색 통일). */
  .classify-degraded .btn-ref {{
    display: inline-flex; align-items: center; gap: 4px; background: {ai_bg};
    border: 1px solid {ai_border}; border-radius: var(--r-sm); padding: 1px 8px 1px 6px;
    font-weight: 700; color: {ai_text}; white-space: nowrap;
  }}
  .classify-degraded .btn-ref .ic {{ width: 0.95em; height: 0.95em; }}
  /* [추가: 2026-08-20] 연속 재분류 실패 이력 — "재분류마저 실패" 문구에서만 붙는다. */
  .classify-degraded .retry-log {{
    display: block; margin-top: 8px; padding-top: 7px; border-top: 1px dashed {degraded_rule};
    color: {degraded_sub}; font-size: var(--fs-sm); font-variant-numeric: tabular-nums;
  }}
  /* 배너가 뜨면 그만큼 topbar를 아래로 밀어 겹치지 않게 한다(높이는 JS가 실측).
     [수정: 2026-08-20] topbar만 밀고 .container는 그대로 뒀더니, .container의
     고정 margin-top(24px)이 밀려난 topbar보다 위에 있어 본문(제목 줄 포함)이
     topbar 밑에 가려졌다(확정본에 같은 표시를 새로 넣으며 발견 — HISTORY.md
     "마감 후 자동 배정" 참고). .container도 같은 높이만큼 같이 밀어야 한다. */
  body.round-over .topbar {{ top: var(--round-banner-h, 0px); }}
  body.round-over .container {{ margin-top: calc(24px + var(--round-banner-h, 0px)); }}
  .bottombar {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  }}
  .bottombar-inner {{
    max-width: 800px; margin: 0 auto; padding: 10px 24px;
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
  }}
  .bottombar a {{ color: {accent}; text-decoration: none; font-size: 1.1rem; padding: 6px 10px; border-radius: var(--r-md); }}
  .bottombar a:hover {{ background: {hover}; }}
  /* [추가: 2026-08-03] app.renderer와 동일한 이유 — 하단바 🖍️ 형광펜 편집 팝오버. */
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
  /* [추가: 2026-08-05] app.renderer와 동일한 소제목 미니 목차(펼침형). */
  /* [수정: 2026-08-10] app.renderer와 동일한 이유 — (확정)/(전송) 버튼과 색이 겹치지
     않도록 중립색으로 톤다운. */
  /* [추가: 2026-08-11] ↩ 되돌리기 — 오른쪽 플로팅 버튼들(발송·목차)과 성격이
     반대인 "취소" 계열이라 사용자 요청대로 화면 왼쪽 아래에 따로 뒀다. 되돌릴 게 있을
     때만 렌더링한다(항상 떠 있으면 "뭘 되돌리는지" 알 수 없어 오히려 불안하다). */
  /* [수정: 2026-09-02] 왼쪽 아래는 두 칸짜리 스택 — 아래가 휴지통, 위가 ↩ 되돌리기
     (확정본·수시 화면과 같은 배치). 휴지통이 0건이라 안 그려질 때도 ↩는 제자리에 둔다. */
  .undo-fab {{
    position: fixed; left: 20px; bottom: 88px; width: var(--fab-sm); height: var(--fab-sm); border-radius: var(--r-circle);
    background: {card}; color: {muted}; border: 1px solid {border}; font-size: 1.4rem;
    cursor: pointer; box-shadow: var(--sh-float); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .undo-fab:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .undo-fab:disabled {{ opacity: 0.5; cursor: progress; }}
{hidden_trash_style}
{scroll_top_style}
{refresh_link_style}
{hide_batch_style}
{name_picker_style}
  .toc-toggle-btn {{
    position: fixed; right: 20px; bottom: 20px; width: var(--fab-sm); height: var(--fab-sm); border-radius: var(--r-circle);
    background: {card}; color: {muted}; border: 1px solid {border}; font-size: 1.2rem; cursor: pointer;
    box-shadow: var(--sh-float); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .toc-toggle-btn:hover {{ background: {hover}; }}
  /* [추가: 2026-08-11] 확정본의 (발송) 카운트다운(빨강, 경고성)과 다른 맥락 — "이번 회차가
     언제 끝나는지" 참고용 정보라 회색으로 톤을 낮췄다. ☰ 버튼 바로 위에 둔다. */
  /* [수정: 2026-08-13] 시:분 → 시:분:초. 숫자가 매초 바뀌므로 tabular-nums로 자릿수 폭을
     고정한다 — 안 그러면 문구 전체가 초마다 미세하게 흔들려 시선을 뺏는다. */
  .round-countdown {{
    position: fixed; right: 20px; bottom: 74px; text-align: right;
    font-size: var(--fs-sm); font-weight: 700; color: {muted}; z-index: 199; white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }}
  /* [추가: 2026-08-13] 회차 종료 10분 이내 — 초는 늘 보이되 "지금 급한가"는 색이 말한다
     (9시간짜리 회차도 있어, 초가 흐른다는 것만으로는 임박 여부를 알 수 없다). 확정본
     발송 카운트다운과 같은 빨강({error})을 쓴다 — 같은 "마감 임박"이라는 뜻이므로. */
  .round-countdown.urgent {{ color: {error}; }}
  /* [수정: 2026-08-18] app.renderer와 동일 — 하단 (취소)/(적용) 바가 스크롤을 따라
     밀려나지 않도록 스크롤 영역(.toc-scroll)을 바깥 상자에서 분리했다. */
  .toc-popover {{
    display: none; position: fixed; right: 20px; bottom: 74px; width: 200px;
    background: {card}; border: 1px solid {border};
    border-radius: var(--r-lg); box-shadow: var(--sh-pop); z-index: 200;
  }}
  .toc-popover.is-open {{ display: block; }}
  .toc-scroll {{ max-height: 320px; overflow-y: auto; padding: 8px; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 목차 안에서 소제목 순서까지 바꿀 수 있게
     항목마다 작은 ▲▼를 붙였다(이름 클릭=이동, 화살표 클릭=순서 변경으로 분리). */
  .toc-row {{ display: flex; align-items: center; justify-content: space-between; border-radius: var(--r-sm); }}
  .toc-row a {{
    flex: 1; min-width: 0; padding: 6px 8px; font-size: var(--fs-md); color: {text};
    text-decoration: none; border-radius: var(--r-sm); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis;
  }}
  .toc-row a:hover {{ background: {hover}; }}
  .toc-row-btns {{ display: flex; gap: 1px; flex-shrink: 0; padding-right: 4px; }}
  .toc-order-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.72rem; cursor: pointer;
    padding: 2px 4px; border-radius: var(--r-sm);
  }}
  .toc-order-btn:hover {{ background: {hover}; }}
  .toc-order-btn:disabled {{ opacity: 0.3; cursor: default; }}
  /* [수정: 2026-08-18] ▲▼가 더는 새로고침을 일으키지 않으므로, 이 표시는 (적용)을
     누를 때까지 남아 "이번에 뭘 옮겼는지"를 누적해서 보여준다. */
  .toc-row.toc-row-moved {{ background: {row_moved}; }}
  .toc-empty {{ color: {muted}; font-size: var(--fs-sm); padding: 6px 8px; }}
  /* [추가: 2026-08-18] app.renderer와 동일한 팝오버 하단 확정 바 — 담당자가 "(적용)을
     누르기 전까지는 아무것도 바뀌지 않았다"고 믿을 수 있어야 하므로 서버로 나가는 길은
     (적용) 하나뿐이다. 구조는 .edit-summary-form을 따랐다(취소 왼쪽·커밋 오른쪽).
     바꾼 게 없으면 아예 나타나지 않는다. */
  .toc-foot {{
    display: none; flex-direction: column; gap: 6px;
    padding: 8px; border-top: 1px solid {border};
    background: {card}; border-radius: 0 0 var(--r-lg) var(--r-lg);
  }}
  .toc-foot.is-dirty {{ display: flex; }}
  .toc-dirty-count {{ font-size: var(--fs-sm); color: {muted}; }}
  .toc-foot-actions {{ display: flex; justify-content: flex-end; gap: 8px; }}
  .toc-foot-actions button {{ font-size: var(--fs-sm); padding: 5px 12px; border-radius: var(--r-sm); cursor: pointer; }}
  .toc-cancel-btn {{ background: transparent; color: {muted}; border: 1px solid {border}; }}
  .toc-cancel-btn:hover {{ background: {hover}; }}
  .toc-apply-btn {{ background: {accent}; color: {on_fill}; border: none; font-weight: 700; }}
  .toc-apply-btn:hover {{ background: {accent_pressed}; }}
  .toc-foot-actions button:disabled {{ opacity: 0.6; cursor: progress; }}
  @keyframes toc-nudge {{ 0%, 100% {{ background: {card}; }} 25%, 75% {{ background: {hover}; }} }}
  .toc-foot.nudge {{ animation: toc-nudge 0.5s ease-in-out; }}
  /* [추가: 2026-07-30] 체크박스로 기사를 선택했을 때만 나타나는 "일괄 이동" 바 —
     장바구니처럼 화면 하단에 붙어있다가, 선택이 하나도 없으면 숨어서 원래 있던
     🗑️(숨긴 기사 관리)만 오른쪽에 그대로 남는다. */
  .bulk-move-bar {{ display: none; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--fs-md); }}
  .bulk-move-bar.is-active {{ display: flex; }}
  .bulk-move-bar .bulk-move-count {{ font-weight: 600; color: {header}; white-space: nowrap; }}
  #bulk-move-select {{
    border: 1px solid {accent}; border-radius: var(--r-md); padding: 5px 8px; font-size: var(--fs-md);
    color: {text}; background: {card};
  }}
  .bulk-move-bar button {{ padding: 5px 12px; font-size: var(--fs-md); }}
  /* [추가: 2026-08-04] app.renderer와 동일 — 일괄 위/아래 이동 버튼. */
  #bulk-move-up, #bulk-move-down {{ padding: 5px 10px; }}
  #bulk-move-up:disabled, #bulk-move-down:disabled {{ opacity: 0.35; cursor: not-allowed; }}
  .bulk-move-bar .clear-btn {{ background: transparent; color: {muted}; }}
  .bulk-move-bar .clear-btn:hover {{ background: {hover}; }}
  /* [추가: 2026-09-15] 「AI 기사 나누기」 — 확정본과 같은 값(app.renderer.split_button_style). */
{split_button_style}
{screen_tag_style}
  .actions .create-group-btn {{
    background: transparent; color: {accent}; border: 1px solid {ghost_border};
    border-radius: var(--r-md); font-weight: 500;
  }}
  .actions .create-group-btn:hover {{ background: {hover}; border-color: {ghost_border_hover}; }}
  /* [수정: 2026-08-12] 초안 툴바에 있던 "🤖 미분류 배정"을 📂 소제목 미분류 칸의
     헤더로 옮겼다 — 미분류를 발견하는 그 자리에서 바로 누르도록(스크롤 불필요),
     그리고 미분류가 0건이면 그 칸 자체가 안 뜨니 버튼도 자동으로 사라진다(disabled
     처리가 따로 필요 없다). 대신 툴바엔 "🤖 전체 기사 재분류"가 새로 생겼다(아래).
     색은 여전히 연보라(#F0EAFB) — "파랑=담당자가 하는 일 / 연보라=AI가 하는 일"이라는
     색 언어를 따른 것(CLAUDE.md "Design direction"). 채운 배경으로 둔다 — 이 버튼은
     안전하고(기존 소제목을 안 건드림) 자주 눌러도 되는 쪽이라 눈에 잘 띄어야 한다. */
  .assign-unclassified-btn {{
    background: {ai_bg}; color: {ai_text}; border: 1px solid {ai_border}; border-radius: var(--r-md);
    font-size: var(--fs-sm); font-weight: 600; padding: 4px 10px; cursor: pointer;
  }}
  .assign-unclassified-btn:hover {{ background: {ai_bg_hover}; border-color: {ai_border_hover}; }}
  .assign-unclassified-btn:disabled {{ opacity: 0.5; cursor: progress; }}
  .actions-divider {{ width: 1px; height: 24px; background: {border}; margin: 0 2px; }}
  /* [추가: 2026-08-12] "🤖 전체 기사 재분류" — 기존 소제목 이름·구성을 전부 새로 짓는
     파괴적 동작이라, 안전한 "미분류 배정"과 달리 **테두리만**(고스트) 둬 한 발 물러나
     보이게 한다. 같은 연보라 계열이라 "AI가 하는 일"이라는 건 알 수 있으면서도, 채워
     칠하지 않아 상시 노출된 위험한 버튼처럼 부각되지 않는다. */
  /* [수정: 2026-08-26] 고스트(테두리만) → "AI 기사 배정"과 같은 채운 연보라. 사용자 결정:
     AI가 하는 일 셋이 색까지 갈려 있으면 계열이 하나로 안 읽힌다. 파괴적이라는 신호는
     아이콘(새로고침 = 처음부터 다시)과 항상 뜨는 confirm()이 계속 맡는다. */
  .actions .reclassify-btn {{
    background: {ai_bg}; color: {ai_text}; border: 1px solid {ai_border};
  }}
  .actions .reclassify-btn:hover {{ background: {ai_bg_hover}; border-color: {ai_border_hover}; }}
  .actions .reclassify-btn:disabled {{ opacity: 0.5; cursor: progress; }}
  .regen-badge {{
    display: inline-block; margin-left: 6px; min-width: 17px; padding: 0 5px;
    background: {error}; color: {on_fill}; border-radius: var(--r-pill);
    font-size: var(--fs-xs); font-weight: 700; line-height: 17px; text-align: center;
  }}
  /* "📂 소제목 미분류" 묶음 — 임시 상태라는 게 보이게 구분한다.
     [수정: 2026-08-21] 빨간 2px 점선 → 앰버 왼쪽 띠 + 옅은 앰버 바탕. 빨강({error})은
     같은 화면의 [단독]/[속보] 기사 행(scoop_bar·scoop_text)과 AI 분류 실패 배너가
     이미 쓰고 있어, 한 화면에 뜻이 다른 빨강이 셋이었다(사용자 지적). 미분류는 오류가
     아니라 "아직 안 끝난 일"이므로 CLAUDE.md "Design direction"의 앰버=주의 세트를
     그대로 쓴다 — 새로 만든 색은 없다. 점선을 뺀 건 색과 별개로 점선 사각 테두리 자체가
     낡아 보인다는 지적 때문. 바탕이 생겨 글자가 테두리에 붙지 않도록 padding을 준다. */
  .subheading.subheading-unclassified {{
    border-left: 4px solid {warn_dot}; background: {warn_bg};
    border-radius: 0 var(--r-lg) var(--r-lg) 0; padding: 10px 16px 12px;
  }}
  /* [추가: 2026-08-11] AI가 만든 소제목 표식. 화면 전용이며, 소제목 이름 문자열과
     완전히 분리된 별도 span이라 복사/다운로드/발송 텍스트에는 절대 따라가지 않는다. */
  .ai-badge {{ margin-left: 6px; font-size: var(--fs-sm); opacity: 0.75; vertical-align: middle; }}
  /* [추가: 2026-08-20] 소제목별 기사 건수 칩 — app.renderer._render_groups와 동일. */
  .subheading-count {{
    margin-left: 6px; font-size: var(--fs-sm); font-weight: 700; color: {muted};
    background: {pill_bg}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  /* 「✂ 오늘만 여기서 끊기」(app.cut_round, 시안 SLOT_CUT_MOCKUP.html) — 평소엔 회색 시각 글자와
     같은 색의 흐린 ✂ 하나, 마우스를 올리면 파랑 + 「여기서 끊기」 글자. 평소엔 안 쓰는 기능이라
     자리만 기억할 수 있게 둔다. 끊기 창은 머리줄 아래에 뜬다(h1이 기준 상자). */
  .preview-head h1 {{ position: relative; }}
  .cut-btn {{ display: inline-flex; align-items: center; gap: 4px; margin-left: 4px; vertical-align: middle;
    border: 1px solid transparent; background: transparent; color: {muted}; border-radius: var(--r-md);
    font-family: inherit; font-size: var(--fs-sm); font-weight: 600; padding: 1px 5px; cursor: pointer;
    opacity: 0.45; transition: opacity .12s, background .12s; }}
  .cut-btn .lbl {{ display: none; font-size: var(--fs-xs); }}
  .cut-btn:hover, .cut-btn.is-open {{ opacity: 1; color: {accent}; border-color: {accent_border}; background: {hover}; }}
  .cut-btn:hover .lbl, .cut-btn.is-open .lbl {{ display: inline; }}
  /* 끊은 상태 칩 — 오늘에만 해당하는 상태라 중립 회색, 취소 글자만 파랑(담당자의 동작) */
  .cut-chip {{ display: inline-flex; align-items: center; gap: 6px; margin-left: 8px; vertical-align: middle;
    font-size: var(--fs-xs); font-weight: 700; color: {text_soft}; background: {pill_bg}; border: 1px solid {border};
    border-radius: var(--r-pill); padding: 2px 4px 2px 10px; }}
  .cut-chip.done {{ padding-right: 10px; }}
  .cut-chip button {{ border: none; background: {card}; color: {accent}; border-radius: var(--r-pill); font-family: inherit;
    font-size: var(--fs-xs); font-weight: 600; padding: 1px 9px; cursor: pointer; }}
  .cut-chip button:hover {{ background: {hover}; }}
  .cut-pop {{ position: absolute; left: 0; top: calc(100% + 8px); z-index: 30; width: 380px; max-width: calc(100vw - 48px);
    background: {card}; border: 1px solid {border}; border-radius: var(--r-lg); box-shadow: var(--sh-pop);
    padding: 14px 16px 12px; font-size: var(--fs-md); font-weight: 400; color: {text}; box-sizing: border-box; }}
  .cut-pop[hidden] {{ display: none; }}
  .cut-pop h4 {{ margin: 0 0 3px; color: {header}; font-size: var(--fs-base); }}
  .cut-pop .sub {{ color: {muted}; font-size: var(--fs-sm); margin: 0 0 12px; line-height: 1.55; }}
  .cut-row {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
  .cut-tchip {{ border: 1px solid {border}; background: {card}; color: {text_soft}; border-radius: var(--r-md); font-family: inherit;
    font-size: var(--fs-sm); padding: 4px 10px; cursor: pointer; font-variant-numeric: tabular-nums; }}
  .cut-tchip:hover {{ background: {hover}; }}
  .cut-tchip.on {{ background: {accent}; border-color: {accent}; color: {card}; font-weight: 600; }}
  .cut-tchip small {{ opacity: .75; margin-left: 3px; }}
  .cut-time {{ font-family: inherit; font-size: var(--fs-md); border: 1px solid {float_input_border}; border-radius: var(--r-md);
    padding: 3px 8px; font-variant-numeric: tabular-nums; }}
  .cut-split {{ margin: 12px 0 0; border: 1px solid {border}; border-radius: var(--r-lg); overflow: hidden; }}
  .cut-split > div {{ display: flex; align-items: baseline; gap: 8px; padding: 7px 11px; font-size: var(--fs-sm); }}
  .cut-split > div + div {{ border-top: 1px solid {border}; }}
  .cut-split .k {{ color: {muted}; min-width: 92px; font-variant-numeric: tabular-nums; }}
  .cut-split .v b {{ color: {accent}; }}
  .cut-split .top {{ background: {hover}; }}
  .cut-when {{ margin: 10px 0 0; font-size: var(--fs-sm); color: {muted}; line-height: 1.55; }}
  .cut-when.warn {{ color: {warn_text}; }}
  .cut-when.err {{ color: {error}; }}
  .cut-foot {{ display: flex; justify-content: flex-end; gap: 6px; margin-top: 12px; }}
  .cut-foot button {{ font-family: inherit; font-size: var(--fs-md); border-radius: var(--r-md); padding: 6px 13px; cursor: pointer; }}
  .cut-foot .no {{ border: 1px solid {border}; background: {card}; color: {muted}; }}
  .cut-foot .ok {{ border: 1px solid {accent}; background: {accent}; color: {card}; font-weight: 600; }}
  .cut-foot .ok:disabled {{ opacity: .45; cursor: not-allowed; }}
  /* 끊은 직후 안내 — 흐름 안 한 줄(파랑 = 담당자의 동작) */
  .cut-done-line {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; background: {hover};
    border: 1px solid {accent_border}; color: {header}; border-radius: var(--r-lg); padding: 9px 12px;
    font-size: var(--fs-md); margin: 0 0 14px; }}
  .cut-done-line a {{ margin-left: auto; color: {accent}; font-weight: 600; text-decoration: none; }}
  .total-count-badge {{
    margin-left: 8px; font-size: var(--fs-sm); font-weight: 700; color: {muted};
    background: {pill_bg}; border-radius: var(--r-pill); padding: 2px 10px; vertical-align: middle;
  }}
  /* [추가: 2026-08-21] 제목 옆 "미분류 N건" — 📂 소제목 미분류 칸은 언제나 화면 맨
     아래라, 기사가 쌓이면 스크롤을 끝까지 내리기 전엔 있는 줄도 모른다(사용자 지적).
     아래 .unclassified-count와 같은 배색을 그대로 써서 "위의 이 숫자 = 아래 그 칸"이
     한눈에 이어지게 하고, 눌러서 바로 그 칸으로 내려가게 한다(scrollToUnclassified).
     0건(정상 상태)이면 아예 안 그린다 — 늘 떠 있으면 숫자를 안 읽게 된다.
     [수정: 2026-08-21] 빨강 → 앰버(주의 세트). 위 .subheading-unclassified 박스를
     앰버로 바꾸면서 이 배지만 빨강으로 남으면 박스 위에서 붕 뜨고, [단독]/[속보]
     빨강과도 계속 겹친다 — 박스·건수 배지·이 배지 셋을 한 번에 맞춘다. */
  .pending-count-badge {{
    margin-left: 6px; font-size: var(--fs-sm); font-weight: 700; vertical-align: middle;
    color: {warn_text}; background: {warn_chip_bg}; border: 1px solid {warn_border};
    border-radius: var(--r-pill); padding: 2px 9px; cursor: pointer; font-family: inherit;
    display: inline-flex; align-items: center; gap: 4px;
  }}
  .pending-count-badge:hover {{ background: {card}; }}
  /* 글자 대신 아래 칸 제목과 같은 열린 폴더 + 숫자(뜻은 툴팁) — 📌 뱃지와 같은 모양. */
  .pending-count-badge .ic {{ width: 1.05em; height: 1.05em; }}
  /* 임시 보관함(아직 분류 안 된 기사) 제목 — 소제목이 아니라 대기실이라는 게 읽히도록
     꺾쇠 없이 회색 기울임꼴로, 확정된 소제목들과 시각적으로 확실히 구분한다. */
  /* [수정: 2026-08-26] font-style: italic 제거 — 옆의 건수 칩·배정 버튼과 밑줄이
     안 맞아 한 줄로 안 읽혔다. "임시 칸"이라는 신호는 앰버 왼쪽 띠 + 회색 글자 +
     폴더 아이콘이 이미 충분히 한다(사용자 판단). */
  .unclassified-title {{ color: {muted}; font-weight: 600;
    display: inline-flex; align-items: center; gap: 5px; }}
  /* [수정: 2026-08-21] 빨강 → 앰버, 위 .pending-count-badge와 같은 이유·같은 배색. */
  .unclassified-count {{
    margin-left: 4px; font-style: normal; font-weight: 700; font-size: var(--fs-sm);
    color: {warn_text}; background: {warn_chip_bg}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  /* [수정: 2026-09-18] 체크박스가 작아 누르기 어렵다는 지적 — 브라우저 기본 13px → 16px
     (시안 mockups/CHECKBOX_SIZE_MOCKUP.html B안). 소제목 머리 체크박스도 같은 크기,
     체크 색은 앱 파랑. 기사 체크박스는 커진 만큼 2px 내려 제목 첫 줄 가운데에 맞춘다. */
  .article-select, .group-select-all {{ width: var(--chk-md); height: var(--chk-md); margin: 3px 3px 0 4px;
    accent-color: {accent}; cursor: pointer; }}
  .article-select {{ flex-shrink: 0; position: relative; top: 2px; }}
  /* 소제목 머리는 flex 줄이라 flex: none이 없으면 체크박스 폭이 눌린다(실측 16 → 13px). */
  .group-select-all {{ flex: none; vertical-align: -3px; }}
  .group-move-select {{
    flex-shrink: 0; width: 100px; height: var(--h-sm); box-sizing: border-box; border: 1px solid {border}; border-radius: var(--r-sm);
    padding: 0 4px; font-size: var(--fs-sm); color: {muted}; background: {card};
  }}
  /* app.renderer와 동일 — 📌 담아둔 기사의 승격 드롭다운만 조금 넓다 — 「확정본에 넣기…」가 100px에선 잘린다
     (실측: 글자 81px + 안쪽 여백 8 + 테두리 2 + 화살표 자리 ≈ 111px). 나머지 규격은
     「다른 소제목」과 같다. */
  .group-move-select.promote-select {{ width: 114px; }}
  /* [추가: 2026-08-04] app.renderer와 동일 — 소제목별/시간순/언론사순 보기 전환 select. */
  .view-mode-select {{
    border: 1px solid {accent}; border-radius: var(--r-md); font-size: var(--fs-md);
    height: var(--h-tb); padding: 0 9px; box-sizing: border-box;
    color: {text}; background: {card};
  }}
  /* [수정: 2026-08-04] app.renderer와 동일 — 사용자가 직접 만든 소제목은 기사가
     차 있어도 점선 테두리를 유지해 자동 분류 소제목과 구분되게 한다. */
  .subheading-custom {{ border: 2px dashed {custom_group_border}; border-radius: var(--r-lg); padding: 8px 16px; }}
  .empty-group-hint {{ color: {muted}; font-size: var(--fs-md); margin: 6px 0 0; }}
  /* 제목 아래 여백은 .preview-head가 쥔다(h1은 0) — 확정본 .header-top과 같은 값이라
     두 화면을 오갈 때 제목·툴바·메모 칸의 간격이 같다. */
  header h1 {{ font-size: var(--fs-xl); margin: 0; color: {header}; }}
  .preview-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
    margin-bottom: 16px; }}
  .preview-hint {{
    font-size: var(--fs-md); color: {muted}; background: {hover}; border-radius: var(--r-lg);
    padding: 10px 14px; margin-bottom: 20px; line-height: 1.6;
  }}
  /* [추가: 2026-08-11] hint가 빈 문자열이면(위 round-countdown 중복 안내문 삭제) 박스
     자체를 완전히 접어 빈 여백이 남지 않게 한다. */
  .preview-hint:empty {{ display: none; }}
  .actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  /* [추가: 2026-08-12] 툴바 버튼 높이를 고정값으로 못박는다 — 🤖 이모지가 들어간
     버튼만 브라우저에 따라 색이모지 글꼴의 내부 line-height가 라틴/한글 텍스트보다
     커서 박스 자체가 다른 버튼보다 눈에 띄게 부풀어 보이는 문제가 실제로 있었다
     (padding·font-size는 다른 버튼과 똑같은데도 발생). height를 고정하면 내용이
     이모지든 텍스트든 상관없이 박스 크기가 항상 같다. */
  .actions button, .actions a.btn {{ height: 32px; box-sizing: border-box; }}
  /* [추가: 2026-08-10] app.renderer와 동일 — 정렬 보기 방식은 액션이 아니라 표시
     옵션이라 툴바 맨 오른쪽으로 분리했다(사용자 선택). */
  .actions .view-mode-select {{ margin-left: auto; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유 — button은 a와 달리 font-family를
     상속받지 않고, appearance:auto(네이티브 OS 버튼 껍데기)까지 남아있어 폰트만 맞춰선
     완전히 똑같이 안 보인다. 크롬이 button 텍스트만 내부적으로 수직 중앙 정렬해주는
     것까지 발견해 align-items: center를 직접 지정했다. */
  button, a.btn {{
    background: {accent}; color: {on_fill}; border: none; border-radius: var(--r-md);
    padding: 6px 14px; font-size: var(--fs-md); font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-flex; align-items: center; justify-content: center;
    user-select: none; -webkit-user-select: none;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유로 액션 툴바만 소프트 필로(시안 B) */
  .actions button, .actions a.btn {{
    background: {hover}; color: {accent}; font-weight: 600; border-radius: var(--r-md);
    height: var(--h-tb); padding: 0 9px; box-sizing: border-box;
  }}
  .actions button:hover, .actions a.btn:hover {{ background: {accent_tonal}; }}
  /* scroll-margin-top — .topbar가 position:fixed라, 목차·"미분류 N건"으로 뛰어오면
     소제목 제목줄이 그 띠 아래에 깔려 안 보였다. 회차 마감 배너가 떠 있으면 그만큼 더
     내려야 하므로 --round-banner-h를 같이 더한다(배너가 없으면 0px). */
  /* [수정: 2026-09-16] 소제목 사이 28→20px — 소제목 밑줄이 이미 구분선 노릇을 한다.
     초안(app/preview_renderer.py)에도 같은 값이 있다. */
  .subheading {{ margin-top: 20px; scroll-margin-top: calc(64px + var(--round-banner-h, 0px)); }}
  /* [수정: 2026-08-05] app.renderer와 동일 — h2 기본 margin 제거 + 아이콘 오른쪽 정렬. */
  .subheading h2 {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    font-size: var(--fs-lg); color: {header}; border-bottom: 1px solid {border};
    margin: 0 0 6px; padding-bottom: 6px;
  }}
  .subheading-title {{ display: flex; align-items: center; gap: 4px; min-width: 0; }}
  .subheading-icons {{ display: flex; align-items: center; gap: 2px; flex-shrink: 0; }}
  .rename-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.9rem; cursor: pointer;
    vertical-align: middle; user-select: none; -webkit-user-select: none;
  }}
  /* [추가: 2026-08-13] app.renderer와 동일 — 소제목 🗑️만 hover 빨강, ✏️는 무채색 유지. */
  .rename-btn.group-hide-btn:hover {{ color: {error}; }}
  /* [추가: 2026-08-03] app.renderer와 동일한 이유 — 소제목 순서 조정 버튼. */
  /* [수정: 2026-08-05] app.renderer와 동일 — 기사 ↑/↓(.move-btn)과 가로폭 통일. */
  /* [수정: 2026-08-13] app.renderer와 동일 — 소제목은 ▲▼(목차 팝오버와 같은 기호),
     기사는 ↑↓로 대상을 형태로 구분한다. 두 화면이 갈리면 안 되므로 같이 바꾼다. */
  .order-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.78rem; cursor: pointer;
    vertical-align: middle; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .order-btn:disabled {{ opacity: 0.35; cursor: default; }}
  /* [수정: 2026-09-16] 행 여백 한 단계씩 축소 — padding 6→4px, 제목·메타 줄 사이 4→2px,
     행 사이 10→6px. 글자·버튼 크기는 그대로다(한 건 74 → 64px). 확정본·초안·정기 보관함·
     수시 네 파일에 같은 값이 복제돼 있으니 한쪽만 고치지 않는다. 시안 파일은 정리하며 없앴다(당시 B안). */
  .article {{ margin: 6px 0; line-height: 1.5; padding: 4px 8px; border-radius: var(--r-md); }}
  /* [추가: 2026-08-20] app.renderer와 동일 — [단독] 카드 강조 + 말머리 글자색.
     우선순위 원칙도 동일(다른 상태 배경보다 먼저 선언해 가장 낮은 우선순위). */
  .article.art-scoop {{ background: {scoop_bg}; border-left: 3px solid {scoop_bar}; padding-left: 9px; }}
  .t-scoop {{ color: {scoop_text}; font-weight: 800; }}
  .t-flash {{ color: {flash_text}; font-weight: 600; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 마우스 오버 시 연한 회색 표시. */
  .article:hover {{ background: {row_hover}; }}
  /* [추가: 2026-08-21] app.renderer와 동일 — 사진 추정 기사는 지우지 않고 행을
     흐리게만 표시한다. 여태 이 화면엔 .photo-badge CSS 자체가 없었다(사진기사 제외
     설정이 항상 켜져 있던 전제라 이 배지가 뜰 일이 실질적으로 없었음) — 설정 기본값을
     끔으로 바꾸며 처음으로 실제 렌더링 대상이 됐다. */
  .article.is-photo {{ opacity: 0.55; }}
  .article.is-photo:hover, .article.is-photo:has(.article-select:checked) {{ opacity: 1; }}
  /* [추가: 2026-08-03] app.renderer와 동일한 이유 — 체크된 기사만 연한 배경으로
     표시한다 (디자인 시안 A). */
  .article:has(.article-select:checked) {{ background: {hover}; }}
  /* [추가: 2026-08-05, 수정: 2026-09-04] app.renderer와 동일 — 승격 직후 그 기사를 한 번
     표시한다. 색은 노랑에서 이동 색으로 옮겼다(담아둔 기사는 승격 전부터 이 화면 아래
     구획에 떠 있어 "처음 보는 기사"가 아니다 — 자세한 이유는 app.renderer의 같은 자리). */
  .article.just-promoted {{ background: {row_promoted}; }}
  /* [수정: 2026-08-05] app.renderer와 동일 — 순서 이동은 노란색(새 기사 도착과 겹침)
     대신 연두색을 쓴다. [수정: 2026-09-04] 승격도 이 색으로 합류했다. */
  .article.just-moved {{ background: {row_moved}; }}
  /* [수정: 2026-08-13] app.renderer와 동일 — summary를 1행에서 2행(.title-row/
     .action-row)으로 나눴다. 제목이 게시시각·액션 묶음과 폭을 다투다 대부분 2~3줄로
     꺾이던 문제 때문. */
  .article summary {{
    cursor: pointer; display: flex; flex-direction: column; gap: 2px;
    -webkit-tap-highlight-color: transparent;
  }}
  .article summary::marker {{ color: {muted}; }}
  /* [수정: 2026-07-30, 유지: 2026-08-13] 🗑️를 제목 바로 옆(2행)에 둔다 — 예전 footer
     위치는 요약을 펼치면 제목에서 멀어져 헷갈렸다. */
  .title-row {{ display: flex; align-items: baseline; justify-content: flex-start; gap: 8px; }}
  /* [수정: 2026-08-13] app.renderer와 동일 — 좁은 화면에서 액션 묶음이 옆으로 넘치던
     문제(실측) 때문에 wrap을 허용한다. */
  .action-row {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; row-gap: 4px; }}
  /* [추가: 2026-08-07] app.renderer와 동일 — 모바일 사파리 탭 하이라이트 잔상 방지. */
  .title-line {{ min-width: 0; overflow-wrap: anywhere; color: {text}; -webkit-tap-highlight-color: transparent; }}
  /* [추가: 2026-09-11] app.renderer와 동일 — 제목 앞 언론사를 괄호 대신 회색·조금 작은 글자로. */
  .title-outlet {{ color: {muted}; font-size: 0.86em; font-weight: 500; margin-right: 0.5em; }}
  /* [수정: 2026-08-05, 2026-08-13] app.renderer와 동일 — 액션 묶음만 margin-left: auto로
     2행 오른쪽 끝에 붙이고, 그래도 안 들어가면 wrap한다. */
  .article-actions {{
    margin-left: auto; display: flex; align-items: center; justify-content: flex-end;
    gap: 4px; flex-shrink: 0; flex-wrap: wrap; row-gap: 4px;
  }}
  /* [추가: 2026-08-21] app.renderer와 동일 — 사진 추정 배지(아이콘+문구 칩). */
  .photo-badge {{
    flex-shrink: 0; padding: 1px 7px; border-radius: var(--r-pill); font-size: var(--fs-xs);
    color: {photo_badge_text}; background: {photo_badge_bg}; border: 1px solid {photo_badge_border}; white-space: nowrap;
    position: relative; top: -0.15em;  /* [수정: 2026-08-21] 제목보다 글자가 작은 알약이라 baseline 정렬만으론 약간 아래로 보인다(실측: 위 여백 +1px, 아래 -2.3px) — 반 칸(0.15em) 올려 제목 글자 높이 한가운데에 맞춘다. */
  }}
{late_badge_style}
{export_links_style}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: var(--fs-md); }}
  /* [수정: 2026-07-30] "이미 확인함"을 영구 기억(localStorage)하지 않고 "지금 펼쳐서
     보고 있는 기사"에만 실시간 적용 — app.renderer와 같은 이유·같은 방식(:has()). */
  /* [수정: 2026-08-21] 색(남색) 대신 opacity로 — 화면에 남는 파랑이 전부 "누를 수
     있는 것"이 되도록, 상태 표시는 색이 아닌 밝기로만 준다. */
  .article:has(details[open]) .title-line {{ opacity: 0.62; }}
  /* [추가: 2026-07-29] 지난번 이 화면을 봤을 때는 없다가 이번에 새로 들어온 기사 —
     초안은 새로고침할 때마다 다시 검색하므로 뭐가 새로 섞였는지 표시해준다
     (app.preview_renderer._compute_preview_articles가 매번 새로 검색·병합한 결과). */
  .article.is-new-arrival {{ background: {row_new}; border-radius: var(--r-md); padding: 8px 10px; }}
  /* [추가: 2026-08-12] "🤖 미분류 기사 분류"가 방금 자리를 정해준 기사 — 어디에 배정됐는지
     담당자가 바로 찾을 수 있게 한 번만 표시하고 다음 새로고침에 사라진다(.just-moved와 같은 방식).
     색은 🤖 버튼과 같은 연보라 — "저 버튼을 눌렀더니 이것들이 생겼다"가 그대로 이어진다.
     .is-new-arrival(노랑)보다 **뒤에** 선언해 겹칠 때 연보라가 이긴다: 노랑은 "기사가 왔다"는
     이미 아는 정보고, 연보라는 "내가 시켜서 방금 배정됐다"는 지금 확인해야 할 정보다. */
  .article.just-classified {{ background: {ai_bg}; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유 — 화면에서 직접 드래그해 복사할 때
     게시 시각이 같이 딸려오지 않도록 이 부분만 선택 자체를 막는다. */
  .pub-time {{
    flex-shrink: 0; color: {muted}; font-size: var(--fs-sm); white-space: nowrap;
    user-select: none; -webkit-user-select: none;
  }}
  .move-btn, .hide-btn, .copy-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .move-btn:disabled {{ color: {border}; cursor: not-allowed; }}
  .hide-btn:hover {{ color: {error}; }}
  /* [수정: 2026-08-14] app.renderer와 동일 — 원문보기가 아이콘에서 게시시각 옆 텍스트
     링크로 바뀌었다(복사 아이콘과 실루엣이 겹쳐 구분이 안 된다는 지적). */
  .time-sep {{ color: {muted}; font-size: var(--fs-sm); margin: 0 2px; user-select: none; -webkit-user-select: none; }}
  .origin-link-text {{
    flex-shrink: 0; color: {accent}; font-size: var(--fs-sm); text-decoration: none;
    display: inline-flex; align-items: center; gap: 2px; white-space: nowrap;
  }}
  .origin-link-text:hover {{ text-decoration: underline; }}
  .origin-link-icon {{ width: 0.75em; height: 0.75em; }}
{kw_inline_style}
{photo_gather_style}
  /* [추가: 2026-08-14] app.renderer와 동일 — title-line 렌더 좌표 실측(28px)만큼
     action-row를 들여써 게시시각·원문보기가 제목 글자 시작점과 같은 줄에서 시작하게 한다. */
  /* 체크박스 16px 기준: 왼쪽 여백 4 + 16 + 오른쪽 여백 3 + gap 8 = 31px (13px일 땐 28px). */
  .article:has(.article-select) .action-row {{ padding-left: 31px; }}
  /* [추가: 2026-09-01] app.renderer와 동일 — 소제목 헤더의 📋(.group-copy-btn)도
     같은 아이콘 전환을 쓴다(색·크기는 옆 ✏️🗑️와 같은 .rename-btn에서 물려받는다). */
  .copy-btn, .group-copy-btn {{ display: inline-flex; align-items: center; }}
  .copy-btn .icon-default, .group-copy-btn .icon-default {{ display: inline-flex; }}
  .copy-btn .icon-done, .group-copy-btn .icon-done {{ display: none; }}
  .copy-btn.is-copied .icon-default, .group-copy-btn.is-copied .icon-default {{ display: none; }}
  .copy-btn.is-copied .icon-done, .group-copy-btn.is-copied .icon-done {{ display: inline-flex; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 원문 다시 가져오기 버튼, 평소 숨김. */
  .refetch-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: var(--fs-base);
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
    opacity: 0; transition: opacity 0.15s;
  }}
  .article:hover .refetch-btn {{ opacity: 1; }}
  .refetch-btn:disabled {{ opacity: 0.35 !important; cursor: not-allowed; }}
  /* [추가: 2026-08-11] "⋯ 더보기" 메뉴 — 원문 다시 불러오기 / 기사제목 직접 수정.
     예전엔 이 둘과 복사하기까지 셋이 제목 줄에 아이콘으로 직접 붙어 있었고
     .refetch-btn의 opacity:0으로 숨겼다가 카드 호버 시에만 나타났는데, (1) 호버할
     때마다 아이콘이 4→8개로 늘어 목록을 훑을 때 산만했고 (2) **모바일엔 호버가 없어
     아예 누를 수 없었다.** 그래서 ⋯ 버튼은 호버와 무관하게 항상 보이고, 메뉴 안은
     아이콘 대신 글자로 이름을 보여준다. [수정: 2026-08-13] app.renderer와 동일 —
     복사하기는 다시 메뉴에서 빠져나가 상시 노출 아이콘(.copy-btn)이 됐다. */
  .more-wrap {{ position: relative; display: inline-flex; flex-shrink: 0; }}
  .more-btn {{
    background: transparent; border: none; color: {muted}; font-size: 1.05rem; line-height: 1;
    cursor: pointer; padding: 3px 7px; border-radius: var(--r-sm);
    user-select: none; -webkit-user-select: none;
  }}
  .more-btn:hover {{ background: {hover}; color: {accent}; }}
  .more-menu {{
    display: none; position: absolute; right: 0; top: 100%; margin-top: 4px; z-index: 60;
    min-width: 172px; background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    padding: 4px; box-shadow: var(--sh-pop);
  }}
  .more-wrap.is-open .more-menu {{ display: block; }}
  /* ⋯ 버튼 — 메뉴의 「복사하기」를 누르면 1초간 ✓로 바뀐다(copyArticleIcon). */
  .more-btn .icon-default {{ display: inline-flex; }}
  .more-btn .icon-done {{ display: none; }}
  .more-btn.is-copied .icon-default {{ display: none; }}
  .more-btn.is-copied .icon-done {{ display: inline-flex; }}
  .more-menu button {{
    display: block; width: 100%; text-align: left; background: transparent; border: none;
    padding: 8px 10px; border-radius: var(--r-sm); font-size: var(--fs-md); color: {text};
    cursor: pointer; white-space: nowrap;
  }}
  .more-menu button:hover {{ background: {hover}; }}
  .more-menu button:disabled {{ opacity: 0.5; cursor: progress; }}
  /* 🏷 라벨 팝오버 — 확정본과 같은 값(app.renderer.label_popover_style). */
{label_popover_style}
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
  .empty {{ text-align: center; margin-top: 60px; font-size: var(--fs-lg); color: {muted}; }}
  .bottom {{ margin-top: 40px; background: {bg}; border: 1px solid {border}; border-radius: var(--r-lg); padding: 16px 18px; }}
  .bottom h3 {{ margin: 0 0 8px; font-size: var(--fs-base); color: {header}; }}
  .bottom-summary {{ padding-top: 2px; }}
  .bottom-header-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
  .bottom-header-row h3 {{ margin: 0; }}
  .bottom-summary-copy {{
    font-size: var(--fs-sm); color: {accent}; background: transparent; border: 1px solid {border};
    border-radius: var(--r-md); padding: 3px 10px; cursor: pointer; font-family: inherit;
  }}
  /* 소제목 이름은 굵게·제목색으로 본문과 가르고, 요약 문단은 줄 간격을 띄운다.
     수시 카드의 같은 블록(app/adhoc/renderer.py `.adhoc-summary`)에 같은 값이
     복제돼 있으니 한쪽만 고치지 않는다. */
  .bottom-summary-item {{ padding: 12px 0; border-bottom: 1px solid {border}; }}
  .bottom-summary-item:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .bottom-summary-item strong {{ display: block; color: {header}; font-weight: 700; margin-bottom: 5px; }}
  .bottom-summary-item p {{ margin: 0; white-space: pre-line; line-height: 1.75; }}
  .manual-divider {{
    display: flex; align-items: center; gap: 10px; margin: 32px 0 4px;
    color: {muted}; font-size: var(--fs-sm);
  }}
  .manual-divider::before, .manual-divider::after {{ content: ""; flex: 1; border-top: 1px dashed {border}; }}
  /* app.renderer와 동일 — "📂 소제목 미분류" 칸과 같은 문법(왼쪽 4px 띠 + 옅은 바탕 +
     오른쪽만 둥근 모서리)을 쓰고 색만 캐러멜로 가른다. 이유는 app.renderer의 같은 자리. */
  .manual-zone {{
    border-left: 4px solid {pinned_bar}; background: {pinned_bg};
    border-radius: 0 var(--r-lg) var(--r-lg) 0; padding: 10px 16px 12px; margin-top: 10px;
  }}
  .manual-zone-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }}
  .manual-zone-title {{ font-weight: 600; color: {text}; font-size: var(--fs-base); }}
  .manual-zone-hint {{ font-size: var(--fs-sm); color: {muted}; }}
  /* app.renderer와 동일 — 건수 칩은 칸 색(캐러멜)을 따른다. */
  .manual-zone-count {{
    margin-left: 4px; font-weight: 700; font-size: var(--fs-sm); color: {pinned_text};
    background: {pinned_chip_bg}; border: 1px solid {pinned_border}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  /* [추가: 2026-08-26] app/renderer.py의 같은 자리와 동일 — 제목·건수·"AI 기사 배정"을
     한 묶음으로 왼쪽에 붙여, 📂 소제목 미분류 칸 헤더와 같은 구성으로 읽히게 한다. */
  .manual-zone-left {{ display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }}
  .manual-zone .article:first-child {{ margin-top: 10px; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — "+ 한 줄 메모" 칸. */
  /* [수정: 2026-08-07] app.renderer와 동일 — 스크롤 중에도 계속 보이도록 sticky 고정. */
  .keyword-note-zone {{
    display: none; align-items: center; gap: 8px; border: 1px dashed {border}; border-radius: var(--r-lg);
    padding: 10px 14px; margin: 14px 0 4px; flex-wrap: wrap;
    position: sticky; top: 60px; z-index: 15; background: {card};
  }}
  .keyword-note-zone.is-open {{ display: flex; }}
  /* [수정: 2026-09-07] app.renderer와 동일 — 회차 마감 배너가 뜨면 topbar가 배너
     높이만큼 내려가므로(body.round-over .topbar) 이 sticky의 top도 같이 따라간다. */
  body.round-over .keyword-note-zone {{ top: calc(60px + var(--round-banner-h, 0px)); }}
  .keyword-note-zone input {{
    flex: 1; min-width: 220px; height: var(--h-md); box-sizing: border-box; padding: 0 10px; border: 1px solid {border}; border-radius: var(--r-md);
    font-size: var(--fs-md); color: {text}; background: {card};
  }}
  /* [추가: 2026-08-10] app.renderer와 동일 — 저장된 메모는 (수정)을 눌러야 편집 가능. */
  .keyword-note-zone input:disabled {{ background: {bg}; color: {muted}; }}
  .keyword-note-zone button {{ font-size: var(--fs-sm); height: var(--h-md); padding: 0 12px; box-sizing: border-box;
    border-radius: var(--r-md); white-space: nowrap; }}
  /* 모양 규칙: 지우기(삭제·취소)는 채움 파랑으로 두지 않는다 — 글자 버튼, 올리면 빨강. */
  .keyword-note-zone .clear-btn {{ background: transparent; color: {muted}; }}
  .keyword-note-zone .clear-btn:hover {{ background: transparent; color: {error}; }}
  /* [추가: 2026-09-21] "+ URL로 추가" — 네이버에서 직접 찾아온 기사를 URL만으로
     📌 담아둔 기사 칸에 올리는 입구. 검색어 화면(상시 조건)을 안 거치는 게 요점이라
     모양은 키워드 메모 칸과 같은 점선 패널로 맞췄다. 여러 줄 붙여넣기를 받으므로
     input이 아니라 textarea다. */
  /* 테두리는 파랑(accent) 실선 — 색 계열 규칙상 파랑이 "담당자의 동작"이다(연보라는
     AI 전용, 청록은 수시 정체성). 점선을 안 쓰는 건 이 화면에 점선 상자가 이미 둘
     있어서다: 회색 점선 = 키워드 메모 칸, 하늘색 굵은 점선 = 직접 만든 소제목.
     셋이 한 화면에 서므로 실선으로 갈라야 서로 안 헷갈린다. */
{url_add_style}
{note_history_style}
  /* [추가: 2026-08-26] app.renderer와 동일 — 직전 회차 메모를 미리 채워둔 상태 표시. */
  /* [추가: 2026-08-13] app.renderer와 동일 — 단색 SVG 아이콘(app.icons) 공통 크기·색. */
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
</style>
</head>
<body>
<div class="round-over-banner" id="round-over-banner">
  <div class="round-over-banner-inner">
    <span class="msg" id="round-over-msg"></span>
    <a class="go" href="{scrap_href}">확정본 열기 →</a>
    <button type="button" class="dismiss" onclick="dismissRoundOverBanner()">나중에</button>
  </div>
</div>
{topnav_html}
<div class="container">
  <header class="preview-head">
    <h1><span class="screen-tag work">초안</span>{run_slot_html}{total_count_html}</h1>
  </header>
  <div class="actions" id="preview-actions">{actions_html}</div>
  <div class="keyword-note-zone{note_open_class}" id="keyword-note-zone">
    <input type="text" id="keyword-note-input" value="{note_value_attr}" placeholder="물가 동향, 공공기관 이전, 인사청문회 등" onkeydown="keywordNoteKey(event)"{note_input_disabled_attr}>
    <button type="button" onclick="toggleKeywordEditMode(this)">{note_save_btn_label}</button>
    <button class="clear-btn" type="button" onclick="{note_clear_btn_onclick}">{note_clear_btn_label}</button>
    {note_history_html}
  </div>
  <div class="preview-hint" id="preview-hint">{hint}</div>
  <div id="preview-body">{body}</div>
</div>
{undo_fab_html}
{hidden_trash_html}
{name_picker_html}
<span class="round-countdown" id="round-countdown"></span>
{scroll_top_html}
<button type="button" class="toc-toggle-btn" onclick="toggleTocPopover()" title="소제목 목차"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button>
<div class="toc-popover" id="toc-popover"></div>
<div class="bottombar"><div class="bottombar-inner">
  <div class="bulk-move-bar" id="bulk-move-bar">
    <span class="bulk-move-count" id="bulk-move-count"></span>
    <select id="bulk-move-select"></select>
    <button type="button" onclick="bulkMoveSelected()">옮기기</button>
    <button type="button" id="bulk-move-up" onclick="bulkMoveOrder('up')" title="선택한 기사들을 통째로 위로 이동(같은 소제목 안에서만)"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg></button>
    <button type="button" id="bulk-move-down" onclick="bulkMoveOrder('down')" title="선택한 기사들을 통째로 아래로 이동(같은 소제목 안에서만)"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg></button>
    <button type="button" onclick="bulkHideSelected(this)" title="선택한 기사 전부 숨기기 (되돌리기 가능)"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg></button>
    {split_button_html}
    <button class="clear-btn" type="button" onclick="clearSelection()">선택 해제</button>
  </div>
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
</div></div>
<script>
// 「✂ 오늘만 여기서 끊기」 — 창은 여는 순간 그린다(지금 시각·기사 게시시각이 그때 기준이어야 한다).
// 서버 동작·저장 규칙은 app.cut_round, 시간표 끼우기는 app.today_cuts.
var CUT_NOTICE_KEY = "cutRoundNotice";
var _cutSel = null, _cutIsNow = true;
function _cutPad(n) {{ return String(n).padStart(2, "0"); }}
function _cutNow() {{ var d = new Date(); return _cutPad(d.getHours()) + ":" + _cutPad(d.getMinutes()); }}
function _cutMin(t) {{ var p = t.split(":"); return (+p[0]) * 60 + (+p[1]); }}
function _cutKr(t) {{ var p = t.split(":").map(Number); return p[1] ? p[0] + "시 " + p[1] + "분" : p[0] + "시"; }}
function _cutEsc(t) {{ var d = document.createElement("div"); d.textContent = t; return d.innerHTML; }}
function _cutPubTimes() {{
  // 보고서에 들어가는 기사만(📌 담아둔 기사 칸은 소제목 section 밖이라 안 센다). 숨긴 기사는 화면에 없다.
  return Array.prototype.map.call(
    document.querySelectorAll("#preview-body section.subheading .article .pub-time-relative[data-pub-date]"),
    function (el) {{ return el.getAttribute("data-pub-date").slice(11, 16); }});
}}
function openCutPop(e) {{
  if (e) e.stopPropagation();
  var pop = document.getElementById("cut-pop");
  if (!pop) return;
  if (!pop.hidden) {{ closeCutPop(); return; }}
  _cutSel = _cutNow(); _cutIsNow = true;
  pop.hidden = false;
  var btn = document.querySelector(".cut-btn"); if (btn) btn.classList.add("is-open");
  renderCutPop();
}}
function closeCutPop() {{
  var pop = document.getElementById("cut-pop");
  if (pop) pop.hidden = true;
  var btn = document.querySelector(".cut-btn"); if (btn) btn.classList.remove("is-open");
}}
function pickCut(t, isNow) {{ if (!t) return; _cutSel = t; _cutIsNow = !!isNow; renderCutPop(); }}
function renderCutPop(errText) {{
  var pop = document.getElementById("cut-pop");
  var start = pop.dataset.start, end = pop.dataset.end, now = _cutNow();
  if (_cutIsNow) _cutSel = now;
  var sel = _cutSel;
  var valid = start < sel && sel < end;
  var future = sel > now;
  var chips = ['<button type="button" class="cut-tchip' + (_cutIsNow ? " on" : "") + '" data-t="' + now + '" onclick="pickCut(this.dataset.t, true)">지금 ' + now + '</button>'];
  for (var m = Math.floor(_cutMin(start) / 30) * 30 + 30; m < _cutMin(end); m += 30) {{
    var t = _cutPad(Math.floor(m / 60)) + ":" + _cutPad(m % 60);
    if (t === now) continue;
    chips.push('<button type="button" class="cut-tchip' + (!_cutIsNow && t === sel ? " on" : "") + '" data-t="' + t + '" onclick="pickCut(this.dataset.t, false)">'
      + t + (t > now ? "<small>예약</small>" : "") + '</button>');
  }}
  var pubs = _cutPubTimes();
  var upto = sel < now ? sel : now;
  var before = pubs.filter(function (t) {{ return t <= upto; }}).length;
  var after = pubs.filter(function (t) {{ return t > sel; }}).length;
  var top = future
    ? '<b>' + _cutKr(sel) + ' 확정본</b> — 지금까지 ' + before + '건 + ' + sel + '까지 더 들어올 기사'
    : '<b>' + _cutKr(sel) + ' 확정본</b> — ' + before + '건';
  var bottom = _cutKr(end) + ' 회차가 이어서 모아요' + (!future && after ? ' — 지금 ' + after + '건' : '');
  var when = errText ? '<p class="cut-when err">' + _cutEsc(errText) + '</p>'
    : !valid ? '<p class="cut-when err">' + start + ' 뒤, ' + end + ' 전 시각만 고를 수 있어요.</p>'
    : future ? '<p class="cut-when">' + sel + '이 되면 확정본이 만들어져요. 그 전엔 제목 옆 칩에서 취소할 수 있어요.</p>'
    : '<p class="cut-when warn">바로 확정본이 만들어져요. 오늘 회차라 지울 수 없어요.</p>';
  pop.innerHTML =
    '<h4>✂ 오늘만 이 시간까지 확정본으로</h4>'
    + '<p class="sub">' + _cutKr(end) + ' 회차를 둘로 나눠요. 수집 시간 설정은 그대로이고, 자정이 지나면 원래대로 돌아가요.</p>'
    + '<div class="cut-row">' + chips.join("")
    + '<input class="cut-time" type="time" value="' + sel + '" onchange="pickCut(this.value, false)" title="분 단위로 직접 고르기"></div>'
    + '<div class="cut-split"><div class="top"><span class="k">~ ' + sel + '</span><span class="v">' + top + '</span></div>'
    + '<div><span class="k">' + sel + ' ~ ' + end + '</span><span class="v">' + bottom + '</span></div></div>'
    + when
    + '<div class="cut-foot"><button type="button" class="no" onclick="closeCutPop()">닫기</button>'
    + '<button type="button" class="ok" id="cut-ok"' + (valid ? '' : ' disabled') + ' onclick="submitCut(this)">✂ ' + sel + '까지 확정본으로</button></div>';
}}
function submitCut(btn) {{
  var at = _cutIsNow ? "" : _cutSel;
  btn.disabled = true;
  btn.textContent = "만드는 중…";
  fetch("http://{settings_host}:{settings_port}/cut-round", {{
    method: "POST",
    headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
    body: "at=" + encodeURIComponent(at),
  }}).then(function (r) {{ return r.json(); }}).then(function (res) {{
    if (!res.ok) {{ renderCutPop(res.reason || "끊지 못했어요."); return; }}
    try {{ sessionStorage.setItem(CUT_NOTICE_KEY, JSON.stringify({{ mode: res.mode, end: res.end, t: Date.now() }})); }} catch (e) {{}}
    location.reload();
  }}).catch(function () {{ renderCutPop("앱에 연결하지 못했어요. 잠시 뒤 다시 눌러 주세요."); }});
}}
function cancelCut(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/cut-round-cancel", {{
    method: "POST",
    headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
    body: "end=" + encodeURIComponent(btn.getAttribute("data-end")),
  }}).then(function (r) {{ return r.json(); }}).then(function (res) {{
    if (!res.ok) {{ alert(res.reason || "취소하지 못했어요."); btn.disabled = false; return; }}
    location.reload();
  }}).catch(function () {{ alert("앱에 연결하지 못했어요."); btn.disabled = false; }});
}}
document.addEventListener("click", function (e) {{
  var pop = document.getElementById("cut-pop");
  if (pop && !pop.hidden && !e.target.closest(".cut-pop") && !e.target.closest(".cut-btn")) closeCutPop();
}});
document.addEventListener("keydown", function (e) {{ if (e.key === "Escape") closeCutPop(); }});
(function showCutNotice() {{
  var raw = null;
  try {{ raw = sessionStorage.getItem(CUT_NOTICE_KEY); sessionStorage.removeItem(CUT_NOTICE_KEY); }} catch (e) {{}}
  if (!raw) return;
  var n; try {{ n = JSON.parse(raw); }} catch (e) {{ return; }}
  if (!n || !n.end || Date.now() - (n.t || 0) > 30000) return;
  var line = document.createElement("div");
  line.className = "cut-done-line";
  line.innerHTML = n.mode === "done"
    ? "✂ " + _cutKr(n.end) + " 확정본을 만들었어요. 이어지는 회차는 " + n.end + " 뒤 기사부터 모아요.<a href='{scrap_href}'>확정본 열기 →</a>"
    : "✂ " + n.end + "에 끊도록 걸어 뒀어요. 그때 확정본이 만들어지고, 그 전엔 제목 옆 칩에서 취소할 수 있어요.";
  var head = document.querySelector(".preview-head");
  if (head && head.parentNode) head.parentNode.insertBefore(line, head.nextSibling);
}})();
{hidden_trash_script}
{scroll_top_script}
{refresh_link_script}
{hide_batch_script}
{range_select_script}
{name_picker_script}
{kw_inline_script}
{photo_gather_script}
// [수정: 2026-07-30] previewMoveArticle이 reload 없이 이 값을 새로 대입하므로 const가 아닌 let.
let PLAIN_TEXT = {plain_text_json};
// [추가: 2026-08-20] AI 소제목 분류가 규칙 기반 폴백 상태인지 + 지금 나갈 소제목 이름들.
// previewMoveArticle이 ↑/↓ 이동 뒤 이 값도 새로 대입한다(분류가 다시 계산되므로).
let CLASSIFY_DEGRADED = {classify_degraded_json};
let CLASSIFY_DEGRADED_NAMES = {classify_degraded_names_json};
// [추가: 2026-08-20] 복사·다운로드 직전, 폴백 상태면 한 번 더 묻는다 — 배너를 놓치고
// 스크롤을 내려 바로 복사/다운로드만 누르는 경우를 잡는 마지막 방어선이다. verb에 따라
// 마지막 줄만 갈린다("복사할까요?"/"내려받을까요?") — 실제로 나갈 텍스트가 다르지
// 않으므로 그 외 문구는 동일하다.
function classifyDegradedConfirmMsg(verb) {{
  // CLASSIFY_DEGRADED_NAMES는 이미 담당자의 "소제목 형식" 템플릿(기본 "<{{section}}>")이
  // 적용된 표시 이름이다 — 여기서 또 꺾쇠로 감싸면 이중으로 붙는다.
  return "AI가 소제목을 묶지 못했어요. 지금 소제목은 AI가 지은 이름이 아니라, 단어 빈도수에 따라 기계적으로 뽑은 단어입니다.\\n\\n"
      + CLASSIFY_DEGRADED_NAMES.join(", ")
      + "\\n\\n그대로 " + verb + "?";
}}
function copyPlainText() {{
  if (CLASSIFY_DEGRADED && !confirm(classifyDegradedConfirmMsg("복사할까요"))) {{ return; }}
  navigator.clipboard.writeText(PLAIN_TEXT)
    .then(() => alert("클립보드에 복사했습니다."))
    .catch(() => alert("복사에 실패했습니다."));
}}
// [추가: 2026-08-10] app.renderer와 동일 — 기사 한 건만 복사.
// [추가: 2026-08-11] 기사 행의 "⋯ 더보기" 메뉴 — 형광펜 팝오버와 같은 패턴
// (다시 누르면 닫힘 + 바깥 클릭 시 닫힘). 한 번에 하나만 열리게 해서, 다른 기사의 ⋯를
// 누르면 이전 것이 자동으로 닫힌다.
function closeArticleMenus() {{
  var open = document.querySelectorAll(".more-wrap.is-open");
  for (var i = 0; i < open.length; i++) {{ open[i].classList.remove("is-open"); }}
}}
function toggleArticleMenu(btn) {{
  var wrap = btn.closest(".more-wrap");
  var wasOpen = wrap.classList.contains("is-open");
  closeArticleMenus();
  if (!wasOpen) {{ wrap.classList.add("is-open"); }}
}}
document.addEventListener("click", function(e) {{
  if (!e.target.closest(".more-wrap")) {{ closeArticleMenus(); }}
}});
// [수정: 2026-08-13] app.renderer와 동일 — 복사하기가 상시 노출 아이콘이 되며
// 글자 치환 대신 아이콘을 1초간 check로 바꾼다.
// flashEl: ✓로 바꿀 대상(없으면 누른 버튼). ⋯ 메뉴의 「복사하기」는 메뉴가 곧 닫히므로 ⋯ 버튼을 넘긴다.
function copyArticleIcon(btn, flashEl) {{
  var mark = flashEl || btn;
  navigator.clipboard.writeText(btn.dataset.copyText).then(function() {{
    mark.classList.add("is-copied");
    clearTimeout(mark._copyTimer);
    mark._copyTimer = setTimeout(function() {{ mark.classList.remove("is-copied"); }}, 1000);
  }}).catch(function() {{
    alert("복사에 실패했습니다.");
  }});
}}
// 🏷 라벨 팝오버 — 확정본과 같은 코드(app.renderer.label_popover_script).
{label_popover_script}
// [추가: 2026-08-13] app.renderer와 동일 — 요약 복사는 위쪽 기사 목록 복사(PLAIN_TEXT)와
// 별도 텍스트라 한 번 더 확인창을 거친다.
function copyAiSummary(btn) {{
  if (!confirm("AI 요약을 복사할까요? (위 기사 목록 복사와는 별도로 복사됩니다)")) return;
  navigator.clipboard.writeText(btn.dataset.copyText).then(function() {{
    alert("복사했습니다.");
  }}).catch(function() {{
    alert("복사에 실패했습니다.");
  }});
}}
// [추가: 2026-08-11] 이번 회차 종료(=정식 수집으로 넘어가는 시각)까지 남은 시간 —
// 참고용 표시일 뿐 실제 전환은 app.scheduler의 tick이 한다(이 탭이 닫혀 있어도
// 그대로 일어남).
// [수정: 2026-08-13] 시:분 → hh:mm:ss. 원래도 1초마다 다시 그리고 있었는데 분 단위만
// 표시해 59초 동안 화면이 멈춘 것처럼 보였다(사용자 지적) — 초를 넣으면 계산 비용 없이
// "이 화면 살아있음"이 드러난다. 확정본 발송 카운트다운(MM:SS)과도 표기 체계가 맞는다.
// [수정: 2026-08-18] "0에 닿아도 완료 문구를 안 둔다"던 예전 판단(회차가 끝나는 순간
// 화면 자체가 확정본으로 넘어가므로 보여줄 사람이 없다)이 틀렸다 — 실제로는 이 탭이
// 계속 옛 초안을 보여준 채 멈춰 있고(정식 전환은 app.scheduler tick이 다음에 화면을
// 다시 그릴 때만 일어남), 담당자가 그걸 모르고 마감된 초안을 계속 고치다 그 작업이
// 확정본에 전혀 반영 안 되는 사고가 났다(HISTORY.md 2026-08-18). round-countdown이
// 0에 닿으면 showRoundOverBanner()로 상단에 노란 띠를 띄운다(목업 3안 중 "상단 고정
// 띠" — 스크롤 위치와 무관하게 항상 보여야 놓치지 않는다는 이유로 선택됨).
const ROUND_DEADLINE_MS = {round_deadline_ms};
function formatRoundEndTimeKr(ms) {{
  var d = new Date(ms);
  var hour = d.getHours();
  var minute = d.getMinutes();
  return minute === 0 ? (hour + "시") : (hour + "시 " + minute + "분");
}}
function showRoundOverBanner() {{
  var banner = document.getElementById("round-over-banner");
  if (!banner || banner.classList.contains("is-visible")) return;
  // [추가: 2026-08-18] "나중에"로 닫으면 이 회차(=이 마감 시각)가 끝나 있는 동안은
  // 다시 안 뜬다 — 탭을 새로고침할 때마다 방금 닫은 배너가 또 뜨면 "나중에"가
  // 의미 없어진다. sessionStorage라 이 탭을 닫으면(=세션 종료) 자연히 초기화된다.
  if (sessionStorage.getItem("roundOverDismissed:" + ROUND_DEADLINE_MS)) return;
  document.getElementById("round-over-msg").innerHTML =
    "<b>" + formatRoundEndTimeKr(ROUND_DEADLINE_MS) + " 회차가 마감됐어요.</b> 지금 보고 있는 건 마감 시점의 초안이에요. 여기서 더 고쳐도 확정본에는 반영되지 않아요.";
  banner.classList.add("is-visible");
  document.body.classList.add("round-over");
  document.documentElement.style.setProperty("--round-banner-h", banner.offsetHeight + "px");
  // [추가: 2026-08-20] 페이지를 열자마자(=이미 마감된 회차를 바로 여는 경우)
  // updateRoundCountdown이 곧바로 이 함수를 부르는데, 그 시점엔 레이아웃이 아직
  // 자리잡기 전이라 배너 높이가 잘못 잡힐 수 있다 — 다음 페인트 프레임에 한 번 더
  // 재보정한다(app.renderer의 finalize-banner와 같은 이유·같은 방식).
  if (window.requestAnimationFrame) {{
    requestAnimationFrame(function () {{
      document.documentElement.style.setProperty("--round-banner-h", banner.offsetHeight + "px");
    }});
  }}
}}
function dismissRoundOverBanner() {{
  sessionStorage.setItem("roundOverDismissed:" + ROUND_DEADLINE_MS, "1");
  document.getElementById("round-over-banner").classList.remove("is-visible");
  document.body.classList.remove("round-over");
}}
function updateRoundCountdown() {{
  var el = document.getElementById("round-countdown");
  if (!el) return;
  var remain = Math.max(0, Math.floor((ROUND_DEADLINE_MS - Date.now()) / 1000));
  var hh = String(Math.floor(remain / 3600)).padStart(2, "0");
  var mm = String(Math.floor(remain / 60) % 60).padStart(2, "0");
  var ss = String(remain % 60).padStart(2, "0");
  el.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5.3l3.2 1.9"/></svg> 수집 종료시간까지 ' + hh + ":" + mm + ":" + ss + " 남음";
  el.classList.toggle("urgent", remain <= 600);
  if (remain <= 0) {{ showRoundOverBanner(); }}
}}
if (ROUND_DEADLINE_MS) {{
  updateRoundCountdown();
  setInterval(updateRoundCountdown, 1000);
}}
// [수정: 2026-08-10] 초안의 개별 Telegram/Email 버튼을 없앴다 — 이제 초안은 (확정)만
// 하고, 전송은 확정된 뒤 완성본에서 한 번에 처리한다(app.confirm_send).
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
// [수정: 2026-08-18] app.renderer와 동일 — 팝오버 안 ▲▼는 이제 누를 때마다 저장하지
// 않는다. 팝오버가 들고 있는 순서만 바꾸고, (적용)을 눌렀을 때 /save-group-order +
// 새로고침을 딱 한 번 한다.
// 설계 전제: **담당자는 (적용)을 누르기 전까지 아무것도 안 바뀌었다고 믿는다.** 그래서
// 자동으로 저장되는 경로를 하나도 두지 않았다 — 바깥 클릭도, 이름 클릭도, 팝오버 닫기도
// 저장하지 않는다. 나가는 길은 (적용)과 (취소) 둘뿐이다.
// 헤더 <h2>의 ▲▼(moveGroupOrder)는 눈앞의 한 칸을 조정하는 용도라 지금처럼 즉시 저장이다.
var _tocItems = [];    // 팝오버가 들고 있는 (아직 저장 안 한) 순서
var _tocBase = [];     // 팝오버를 열었을 때의 순서 = 지금 저장돼 있는 순서
// [수정: 2026-09-03] 초록(.toc-row-moved)은 **언제나 딱 한 칸 — 마지막에 옮긴 그 행**이다
// (사용자 결정). 예전엔 "이번 팝오버에서 만진 것 전부"를 초록으로 칠해서, 두 개를 옮기면
// 초록이 두 개로 남았다 — 담당자에게 그건 "방금 뭘 만졌는지"가 아니라 "그동안 만진 목록"이라
// 지금 커서가 어디 있는지를 오히려 흐렸다(2026-08-26에 고친 "밀려난 이웃까지 초록" 증상과
// 겉모습이 똑같아서 같은 버그가 되살아난 것처럼 보이기까지 했다).
// 그래서 초록의 출처는 둘뿐이다 — "이번 팝오버에서 마지막으로 옮긴 행"(_tocMovedIdx, 이름이
// 아니라 **위치**로 기억한다: 같은 이름의 소제목이 둘 있어도 엉뚱한 행이 같이 켜지지 않게)과
// "직전 (적용)에서 옮겨 방금 저장된 행"(_tocJustApplied, 새로고침 뒤 한 번만 보여주는 1회성
// 신호). 밀려난 이웃은 예나 지금이나 초록이 아니다. 이용자가 이번 팝오버에서 처음 조작하는
// 순간(_tocSwap) _tocJustApplied를 통째로 비워 옛 초록이 겹쳐 남는 것도 막는다.
// _tocTouched는 이제 초록과 무관하다 — 하단 "N개 위치 바뀜"이 쓰는 **개수 세기 전용**이다.
var _tocTouched = {{}};      // 이번 팝오버에서 이용자가 ▲▼를 누른 소제목 (제자리로 돌아오면 지움) — 개수 세기 전용
var _tocMovedIdx = -1;      // 초록으로 칠할 단 하나 = 마지막에 옮긴 행의 지금 위치 (-1이면 없음)
var _tocJustApplied = {{}};  // 직전 (적용) 직후 새로고침에서만 보여주는 1회성 초록
var _tocBusy = false;  // (적용) 요청 진행 중
function _tocReadSections() {{
  return Array.prototype.map.call(document.querySelectorAll(".subheading[data-toc-name]"), function(sec) {{
    var renameBtn = sec.querySelector(".rename-btn[data-current]");
    return {{
      raw: renameBtn ? renameBtn.dataset.name : "",
      label: sec.dataset.tocName,
      id: sec.id,
      // [추가: 2026-09-10] 순서가 고정된 칸(기타) — 목차엔 보이되 ▲▼ 대상이 아니다.
      locked: sec.hasAttribute("data-order-locked"),
      // [추가: 2026-08-05] app.renderer와 동일 — 사용자가 직접 만든 소제목은 목차에서도 볼드로.
      custom: sec.classList.contains("subheading-custom")
    }};
  }});
}}
function _tocDirtyCount() {{
  var n = 0;
  for (var i = 0; i < _tocItems.length; i++) {{
    if (_tocItems[i].raw !== _tocBase[i]) {{ n++; }}
  }}
  return n;
}}
// 📂 소제목 미분류처럼 이름 바꾸기 버튼이 없는 칸은 raw가 빈 문자열이고, 순서 대상이
// 아니다 — 예전 moveTocOrder도 .rename-btn[data-current] 목록만 보고 순서를 계산해서
// 이 칸을 건너뛰었다. 목차에는 그대로 보여주되 ▲▼만 잠그고, 이웃도 건너뛴다.
// (초안은 미분류 칸이 실제로 자주 뜨는 화면이라 확정본보다 더 자주 걸리는 경로다.)
// [추가: 2026-09-10] 순서 대상인가 — 미분류(raw 없음)와 순서 고정 칸(기타, locked) 둘 다 아니다.
function _tocOrderable(item) {{ return !!(item && item.raw && !item.locked); }}
function _tocReorderNeighbor(idx, step) {{
  if (!_tocOrderable(_tocItems[idx])) {{ return -1; }}
  for (var k = idx + step; k >= 0 && k < _tocItems.length; k += step) {{
    if (_tocOrderable(_tocItems[k])) {{ return k; }}
  }}
  return -1;
}}
function buildTocPopover() {{
  _tocItems = _tocReadSections();
  _tocBase = _tocItems.map(function(item) {{ return item.raw; }});
  _tocTouched = {{}};
  _tocMovedIdx = -1;
  _tocJustApplied = {{}};
  _tocBusy = false;
  // 이용자가 다시 ▲▼를 누르는 순간 _tocSwap이 이 값을 통째로 비운다.
  var justMoved = sessionStorage.getItem("tocJustMovedNames");
  if (justMoved) {{
    sessionStorage.removeItem("tocJustMovedNames");
    try {{
      JSON.parse(justMoved).forEach(function(name) {{ _tocJustApplied[name] = true; }});
    }} catch (err) {{ /* 저장된 값이 깨졌으면 표시만 생략한다 */ }}
  }}
  _tocRender();
}}
function _tocRender() {{
  var pop = document.getElementById("toc-popover");
  pop.innerHTML = "";
  if (!_tocItems.length) {{
    pop.innerHTML = '<p class="toc-empty">소제목이 없어요.</p>';
    return;
  }}
  var scroll = document.createElement("div");
  scroll.className = "toc-scroll";
  _tocItems.forEach(function(item, idx) {{
    var row = document.createElement("div");
    // 초록 = 마지막에 옮긴 행 하나이거나, 직전 적용 직후 1회성으로 보여주는 행.
    // 앞서 옮긴 행도, 밀려난 이웃도 초록이 아니다 — 초록은 항상 한 칸이다.
    row.className = "toc-row" + (idx === _tocMovedIdx || _tocJustApplied[item.raw] ? " toc-row-moved" : "");
    var a = document.createElement("a");
    a.href = "#";
    a.textContent = item.label;
    if (item.custom) {{ a.style.fontWeight = "700"; }}
    a.onclick = function(e) {{ e.preventDefault(); scrollToSubheading(item.id); }};
    var btns = document.createElement("span");
    btns.className = "toc-row-btns";
    var up = document.createElement("button");
    up.type = "button"; up.className = "toc-order-btn"; up.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 15 6-6 6 6"/></svg>'; up.title = "위로";
    var prevIdx = _tocReorderNeighbor(idx, -1);
    var nextIdx = _tocReorderNeighbor(idx, 1);
    up.disabled = prevIdx < 0 || _tocBusy;
    up.onclick = function() {{ _tocSwap(idx, prevIdx); }};
    var down = document.createElement("button");
    down.type = "button"; down.className = "toc-order-btn"; down.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>'; down.title = "아래로";
    down.disabled = nextIdx < 0 || _tocBusy;
    down.onclick = function() {{ _tocSwap(idx, nextIdx); }};
    btns.appendChild(up);
    btns.appendChild(down);
    row.appendChild(a);
    row.appendChild(btns);
    scroll.appendChild(row);
  }});
  pop.appendChild(scroll);
  pop.appendChild(_tocBuildFoot());
}}
function _tocBuildFoot() {{
  var dirty = _tocDirtyCount();
  // [수정: 2026-09-03] 이 숫자는 더는 초록 블럭 수가 아니다 — 초록은 "방금 옮긴 한 칸"을
  // 가리키고(위 _tocMovedIdx), 이 숫자는 "적용을 누르면 저장될 소제목이 몇 개인가"를 말한다.
  // 둘이 갈라진 건 의도된 것이다: 세 개를 옮기면 초록은 마지막 한 칸, 숫자는 3이다. 한 칸
  // 옮기면 밀려난 이웃까지 자리가 바뀌어 _tocDirtyCount()는 2를 세지만 담당자가 옮긴 건
  // 하나다. (적용) 가능 여부·닫기 차단은 계속 _tocDirtyCount()(= 실제로 저장될 변화)로
  // 판단하고, 만에 하나 만진 기록은 비었는데 순서는 달라진 상태가 되면 그 숫자를 대신
  // 보여준다 — "0개 위치 바뀜"이라 적어놓고 적용할 게 남아 있는 상황(담당자 작업이 조용히
  // 사라지는 길)만은 만들지 않는다.
  var shown = Object.keys(_tocTouched).length || dirty;
  var foot = document.createElement("div");
  foot.id = "toc-foot";
  foot.className = "toc-foot" + (dirty ? " is-dirty" : "");
  var count = document.createElement("span");
  count.className = "toc-dirty-count";
  count.textContent = _tocBusy ? "반영하는 중…" : (shown + "개 위치 바뀜");
  var actions = document.createElement("div");
  actions.className = "toc-foot-actions";
  var cancel = document.createElement("button");
  cancel.type = "button"; cancel.className = "toc-cancel-btn"; cancel.textContent = "취소";
  cancel.disabled = _tocBusy;
  cancel.onclick = _tocCancel;
  var apply = document.createElement("button");
  apply.type = "button"; apply.className = "toc-apply-btn"; apply.textContent = "적용";
  apply.disabled = _tocBusy;
  apply.onclick = _tocApply;
  actions.appendChild(cancel);
  actions.appendChild(apply);
  foot.appendChild(count);
  foot.appendChild(actions);
  return foot;
}}
function _tocSwap(i, j) {{
  if (_tocBusy || j < 0 || j >= _tocItems.length) {{ return; }}
  if (!_tocOrderable(_tocItems[i]) || !_tocOrderable(_tocItems[j])) {{ return; }}  // 순서 대상이 아닌 칸은 안 건드린다
  // 이용자가 이번 팝오버에서 처음으로 뭔가 만지는 순간 — 직전 적용의 1회성 초록은
  // 더는 "지금 상태"가 아니므로 통째로 지운다.
  _tocJustApplied = {{}};
  var moving = _tocItems[i].raw;  // ▲▼를 누른 그 소제목만 초록 대상 (밀려난 이웃은 아니다)
  var tmp = _tocItems[i]; _tocItems[i] = _tocItems[j]; _tocItems[j] = tmp;
  _tocTouched[moving] = true;   // 개수 세기용 (초록과 무관)
  _tocMovedIdx = j;             // 초록은 여기 하나로 옮겨온다 — 앞서 켜져 있던 초록은 자연히 꺼진다
  // 제자리로 돌아온 소제목은 초록도 거둔다 — 만졌더라도 결과적으로 안 옮긴 것이므로,
  // 바뀐 게 0이면 표시도 0이어야 "확정할 게 없다"가 화면에 그대로 보인다.
  _tocItems.forEach(function(item, k) {{
    if (item.raw === _tocBase[k]) {{ delete _tocTouched[item.raw]; }}
  }});
  // 마지막에 옮긴 행이 결과적으로 제자리라면 초록도 거둔다 — 위 개수 세기와 같은 기준.
  if (_tocMovedIdx >= 0 && _tocItems[_tocMovedIdx].raw === _tocBase[_tocMovedIdx]) {{ _tocMovedIdx = -1; }}
  _tocRender();
}}
function _tocApply() {{
  if (_tocBusy || !_tocDirtyCount()) {{ return; }}
  _tocBusy = true;
  _tocRender();  // ▲▼·취소·적용 전부 잠근다 — 연타로 순서가 엇갈리지 않도록
  // 순서 대상이 아닌 칸(raw === "")은 빼고 보낸다 — 예전 moveTocOrder가 보내던 목록과 같다.
  var order = _tocItems.filter(_tocOrderable).map(function(item) {{ return item.raw; }});
  // 새로고침 뒤 초록으로 되짚어줄 이름 = 마지막에 옮긴 그 하나뿐 (화면의 초록은 항상 한 칸).
  var moved = _tocMovedIdx >= 0 ? [_tocItems[_tocMovedIdx].raw] : [];
  fetch("http://{settings_host}:{settings_port}/save-group-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{order: JSON.stringify(order)}})
  }}).then(function(res) {{
    if (res.ok) {{
      sessionStorage.setItem("tocJustMovedNames", JSON.stringify(moved));
      sessionStorage.setItem("tocPopoverReopen", "1");
      location.reload();
    }} else {{
      _tocBusy = false; _tocRender();
      alert("순서 변경에 실패했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    // 실패해도 옮겨둔 순서는 그대로 두고 잠금만 푼다 — 다시 (적용)을 누르면 된다.
    _tocBusy = false; _tocRender();
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function _tocCancel() {{
  if (_tocBusy) {{ return; }}
  // 서버를 부르지 않는다 — 팝오버가 들고 있던 순서만 원래대로 되돌리고 닫는다
  // (editSummary의 "취소"가 form.remove()만 하는 것과 같은 성격).
  var byName = {{}};
  _tocItems.forEach(function(item) {{ byName[item.raw] = item; }});
  _tocItems = _tocBase.map(function(name) {{ return byName[name]; }});
  _tocTouched = {{}};
  _tocMovedIdx = -1;
  _tocRender();
  document.getElementById("toc-popover").classList.remove("is-open");
}}
// 미확정 상태에서 닫으려 했을 때 — 닫지 않고 하단 바만 한 번 깜빡여 어디서 결정해야
// 하는지 가리킨다. 아무 반응이 없으면 "고장났나?"로 읽히기 때문에 무반응은 피한다.
function _tocNudge() {{
  var foot = document.getElementById("toc-foot");
  if (!foot) {{ return; }}
  foot.classList.remove("nudge");
  void foot.offsetWidth;  // 같은 애니메이션을 연속으로 다시 재생시키기 위한 리플로우
  foot.classList.add("nudge");
  setTimeout(function() {{ foot.classList.remove("nudge"); }}, 520);
}}
function _tocTryClose() {{
  if (_tocBusy) {{ return; }}
  if (_tocDirtyCount()) {{ _tocNudge(); return; }}
  document.getElementById("toc-popover").classList.remove("is-open");
}}
// [추가: 2026-08-10] 회차 종료 10분 전부터 뜨는 "지금 마감하기"(=확정) 버튼 — 지금
function toggleTocPopover() {{
  var pop = document.getElementById("toc-popover");
  if (pop.classList.contains("is-open")) {{ _tocTryClose(); return; }}
  pop.classList.add("is-open");
  buildTocPopover();
}}
function scrollToSubheading(id) {{
  // 이동은 팝오버를 닫는 동작이라 닫기와 같은 규칙을 탄다.
  if (_tocBusy) {{ return; }}
  if (_tocDirtyCount()) {{ _tocNudge(); return; }}
  var el = document.getElementById(id);
  if (el) {{ el.scrollIntoView({{behavior: "smooth", block: "start"}}); }}
  document.getElementById("toc-popover").classList.remove("is-open");
}}
// [추가: 2026-08-21] 제목 옆 "미분류 N건" 배지 → 📂 소제목 미분류 칸으로 이동.
// id(subheading-{{n}})는 소제목 순서가 바뀌면 같이 바뀌므로 서버가 구운 번호를 들고
// 있지 않고, 목차와 같은 방식으로 **누르는 시점의 DOM**에서 그 칸을 찾는다
// (#preview-body가 통째로 갈아끼워지는 화면이라 미리 잡아두면 안 된다).
function scrollToUnclassified() {{
  var el = document.querySelector(".subheading-unclassified");
  if (el) {{ el.scrollIntoView({{behavior: "smooth", block: "start"}}); }}
}}
// [수정: 2026-08-18] app.renderer와 동일 — 미확정 상태에서는 팝오버 바깥 클릭을 **캡처
// 단계에서 막는다**. 닫기만 막으면 그 클릭이 뒤로 통과해서 헤더 ▲▼(즉시 저장)가 눌리고,
// 팝오버가 들고 있던 순서와 다른 순서가 저장돼 버린다. 초안은 여기에 더해 #preview-body를
// 통째로 갈아끼우는 동작(기사 숨김·이동 등)까지 같은 클릭으로 튈 수 있어 더 필요하다.
document.addEventListener("click", function(e) {{
  var pop = document.getElementById("toc-popover");
  if (!pop.classList.contains("is-open")) {{ return; }}
  if (e.target.closest(".toc-popover")) {{ return; }}
  if (e.target.closest(".toc-toggle-btn")) {{ return; }}  // toggleTocPopover가 처리한다
  if (_tocBusy || _tocDirtyCount()) {{
    e.preventDefault();
    e.stopPropagation();
    _tocNudge();
    return;
  }}
  pop.classList.remove("is-open");
}}, true);
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
// [추가: 2026-08-20] 숨김 요청 본문을 한 곳에서 만든다. 예전엔 단건 🗑️만 언론사·제목·
// 발행시각을 함께 보내고, 일괄 숨기기(bulkHideSelected)와 소제목 통째 숨기기(hideGroup)는
// url 하나만 보냈다 — 그래서 그 두 경로로 숨긴 기사는 hidden_articles.json에 언론사·제목이
// null로 남았고, "네이버가 같은 기사를 새 기사 ID로 재게재하면 옛 URL 숨김이 안 먹는다"는
// 2026-08-13 사고 때 넣어둔 (언론사, 제목) 대조 보호(app.curation.filter_hidden)를 통째로
// 못 받았다. 실제로 2026-08-20 09:30 회차에서 일괄로 숨긴 뉴스1 기사가 같은 제목·같은
// 언론사로 기사 ID만 바뀌어 다시 들어왔다. 세 경로가 같은 본문을 쓰게 해서 원천 차단한다.
// (dataset 값이 없으면 "undefined" 문자열이 그대로 실려 가짜 언론사명이 저장되므로 || "".)
// [추가: 2026-09-02] group — 휴지통이 "한 번에 숨긴 덩어리"에 소제목 이름을 붙이는 데
// 쓴다(숨김 판정에는 관여하지 않는다). 확정본과 같은 본문이어야 두 화면이 같은 값을 남긴다.
// [추가: 2026-09-15] batch — 일괄·소제목 통째 숨기기가 한 번의 동작임을 서버에 알린다(지금은
// postHideBatch 한 요청으로 보내지만, 되돌린 뒤 전부 칠하는 표시로 계속 쓴다). 같은 값을 실어 보내 서버(app.undo.push)가 "한 동작"으로 알아보게 하고,
// 되돌린 뒤 그때 숨긴 기사를 전부 칠한다. 단건 🗑️는 안 붙인다 — 3초 안에 따로따로 누른
// 숨기기는 되돌리기는 한 번에 되지만 칠하는 건 마지막으로 누른 기사뿐이다.
function hideArticleBody(btn, batch) {{
  var params = new URLSearchParams({{
    url: btn.dataset.url,
    outlet: btn.dataset.outlet || "",
    title: btn.dataset.title || "",
    pubDate: btn.dataset.pubDate || "",
    group: btn.dataset.group || ""
  }});
  if (batch) {{ params.set("batch", batch); }}
  return params;
}}
function newHideBatchId() {{
  return String(Date.now()) + "-" + Math.random().toString(36).slice(2, 8);
}}
function hideArticle(btn) {{
  var article = btn.closest(".article");
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: hideArticleBody(btn)
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
    // [추가: 2026-08-07] app.renderer와 동일 — 사용자가 만든 소제목 여부도 같이 읽는다.
    var custom = btn.closest(".subheading").classList.contains("subheading-custom");
    return {{value: btn.dataset.name, label: btn.dataset.current, custom: custom}};
  }});
}}
// [추가: 2026-08-03] app.renderer와 동일한 이유·동작 — 소제목 화면 순서를 바꾼다.
function moveGroupOrder(btn, direction) {{
  // [수정: 2026-09-10] 순서 고정 칸(기타)은 이웃·저장 목록에서 뺀다 — 순서는 서버가 붙인다.
  var names = Array.prototype.map.call(document.querySelectorAll(".subheading:not([data-order-locked]) h2 .rename-btn[data-current]"), function(b) {{ return b.dataset.name; }});
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
    if (g.custom) {{ opt.style.fontWeight = "700"; }}
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
  updateSplitButton(checked, sections, sameSection);
}}
{split_button_script}
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
function bulkHideSelected(trigger) {{
  // [수정: 2026-08-20] 체크박스(url만 있음)가 아니라 그 행의 🗑️ 버튼을 모은다 —
  // 언론사·제목·발행시각까지 함께 보내려면 그 값들을 들고 있는 쪽이 필요하다(hideArticleBody).
  var btns = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{
    var article = cb.closest(".article");
    return article ? article.querySelector(".hide-btn[data-url]") : null;
  }}).filter(function(btn) {{ return btn; }});
  if (!btns.length || document.querySelector(".hiding-note")) {{ return; }}
  // [수정: 2026-09-22] 기사 수만큼 요청을 나눠 보내던 것을 한 요청으로(postHideBatch), 누르는
  // 순간 고른 기사를 흐리게 하고 「N건 숨기는 중…」을 띄운다(hide_batch_script).
  var restore = showHiding(btns.map(function(btn) {{ return btn.closest(".article"); }}), trigger, btns.length);
  postHideBatch(btns, newHideBatchId()).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ restore(); alert("숨기지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    restore();
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
// [추가: 2026-08-11] 📂 소제목 미분류에 있는 기사만 기존 소제목에 배정한다 — 기존
// 소제목의 이름·구성은 절대 건드리지 않으므로 잃을 게 없다(확인 팝업 없음).
// [수정: 2026-08-12] 이름을 assignUnclassified로 바꾸고(이제 "재분류"가 아니라
// "배정"), 툴바가 아니라 📂 칸 헤더의 버튼이 이걸 부른다.
// [수정: 2026-08-26] 예전엔 아무것도 못 배정했을 때(204)도 그냥 새로고침만 해서, 버튼을
// 눌러도 미분류가 그대로인데 왜인지 알 방법이 없었다 — 서버가 이제 항상 200 JSON으로
// {{assigned, reason}}을 돌려주고, 여기서 reason별로 안내한다. assigned가 비어 있으면
// (=바뀐 게 없으면) 새로고침도 하지 않는다 — 화면이 그대로인데 새로고침만 하면 "먹통"으로
// 보이기 쉽다.
var ASSIGN_FAILURE_MESSAGES = {{
  no_pending: null,  // 이미 다 배정된 상태 — 버튼이 안 보여야 하는 경우라 안내 불필요.
  no_candidates: "배정할 소제목이 아직 없어요. 소제목이 하나라도 생긴 뒤에 다시 눌러주세요.",
  not_configured: "AI 연동이 설정돼 있지 않아요.",
  api_error: "AI 호출이 실패했어요. 잠시 후 다시 시도해주세요.",
  no_match: "지금 있는 소제목 중에 맞는 곳이 없어요. 새 소제목을 만들어 옮기거나 「AI 모든 기사 재분류」를 눌러주세요."
}};
function assignUnclassified(btn) {{
  btn.disabled = true;
  var label = btn.innerHTML;
  btn.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 3l1.9 5.1L18 10l-5.1 1.9L11 17l-1.9-5.1L4 10l5.1-1.9Z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8Z"/></svg> 분류하는 중…';
  fetch("http://{settings_host}:{settings_port}/assign-unclassified", {{
    method: "POST", keepalive: true
  }}).then(function(res) {{
    if (res.ok) {{
      res.json().then(function(data) {{
        var assigned = (data && data.assigned) || [];
        if (assigned.length) {{
          sessionStorage.setItem("justClassifiedUrls", JSON.stringify(assigned));
          location.reload();
          return;
        }}
        btn.disabled = false; btn.innerHTML = label;
        var msg = ASSIGN_FAILURE_MESSAGES[(data && data.reason) || ""];
        if (msg) {{ alert(msg); }}
      }}).catch(function() {{ location.reload(); }});
    }}
    else {{
      btn.disabled = false; btn.innerHTML = label;
      alert("미분류 기사를 분류하지 못했습니다. 잠시 후 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    btn.disabled = false; btn.innerHTML = label;
    alert("미분류 기사를 분류하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-26] "🤖 AI 기사 배정"(📌 담아둔 기사 구획) — 담아둔 기사를 전부 초안에
// 넣은 다음, 📂 소제목 미분류 칸의 같은 이름 버튼과 **똑같은 배정**을 이어서 돌린다.
// 두 단계를 한 엔드포인트로 합치지 않은 이유: 배정 로직(_handle_assign_unclassified)은
// 실패 사유별 안내·새 소제목 등록·누락분 "기타" 보정까지 얽혀 있어 복제하면 두 벌이
// 갈라진다. 이렇게 두면 승격만 되고 배정이 실패해도 기사는 📂 칸에 그대로 보이고,
// 그 자리의 버튼으로 다시 시도할 수 있다(안전한 중간 상태).
function assignPinnedArticles(btn) {{
  var count = parseInt(btn.dataset.count || "0", 10);
  if (!confirm("담아둔 기사 " + count + "건을 전부 초안에 넣고 AI가 소제목을 정할까요?\\n"
      + "넣은 뒤에는 복사·내보내기·발송에 함께 나갑니다.\\n"
      + "일부만 넣으려면 기사별 소제목 드롭다운을 쓰세요.")) {{
    return;
  }}
  btn.disabled = true;
  var label = btn.innerHTML;
  btn.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 3l1.9 5.1L18 10l-5.1 1.9L11 17l-1.9-5.1L4 10l5.1-1.9Z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8Z"/></svg> 배정하는 중…';
  fetch("http://{settings_host}:{settings_port}/promote-all-manual-to-draft", {{
    method: "POST", keepalive: true
  }}).then(function(res) {{
    if (!res.ok) {{
      btn.disabled = false; btn.innerHTML = label;
      alert("담아둔 기사를 초안에 넣지 못했습니다. 잠시 후 다시 시도해주세요.");
      return;
    }}
    // 승격이 끝났으면 그 기사들은 지금 📂 미분류 칸에 있다 — 같은 배정을 그대로 이어 돌린다.
    fetch("http://{settings_host}:{settings_port}/assign-unclassified", {{
      method: "POST", keepalive: true
    }}).then(function(r2) {{
      if (r2.ok) {{
        r2.json().then(function(data) {{
          var assigned = (data && data.assigned) || [];
          if (assigned.length) {{
            sessionStorage.setItem("justClassifiedUrls", JSON.stringify(assigned));
          }}
          location.reload();
        }}).catch(function() {{ location.reload(); }});
      }} else {{
        // 승격은 됐다 — 되돌리지 않고 화면만 갱신한다. 기사는 📂 미분류 칸에서 보이고
        // 그 자리 버튼으로 다시 배정할 수 있다.
        location.reload();
      }}
    }}).catch(function() {{ location.reload(); }});
  }}).catch(function() {{
    btn.disabled = false; btn.innerHTML = label;
    alert("담아둔 기사를 초안에 넣지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-12] "🤖 전체 기사 재분류" — 기존 소제목을 전부 새로 짓는 파괴적 동작이라
// app.renderer의 같은 이름 함수와 동일한 확인창을 쓴다(문구도 동일 — 잃는 게 같으므로).
// [수정: 2026-08-12] 옮겨둔 기사(edits)가 0건이어도 항상 확인한다 — 이 버튼은 옮긴
// 기사가 없어도 소제목 이름·순서는 항상 새로 짓는다. "옮겨둔 기사 N건" 문장만
// 있을 때만 넣는다(app.renderer의 같은 함수와 동일한 이유).
// [수정: 2026-08-20] data-degraded="1"(지금 소제목이 규칙 기반 폴백 상태)이면 다른
// 확인창을 쓴다 — 평소 문구("이름·순서가 초기화됩니다")는 폴백 상태에서 거짓에
// 가깝다. 이름은 이미 단어 빈도로 뭉개졌고, 옮겨둔 배치(group_overrides)도 그
// 이름을 못 찾아 이미 무효이기 때문이다. 없어진 걸 잃는다고 겁주면 정작 눌러야
// 할 순간에 담당자가 망설이게 된다.
// [수정: 2026-09-01] 두 갈래의 골격을 [상황] → [안심] → 빈 줄 → [질문]으로 통일했다 —
// 폴백 갈래만 질문을 던져놓고 그 뒤에 안심 문구를 주고 있어서, 같은 버튼인데 상황에
// 따라 읽는 순서가 달라졌다. 안심 문구 자체도 config.RECLASSIFY_SAFE_NOTE 하나로 묶어
// 확정본(app.renderer)까지 세 자리가 갈라질 수 없게 했다.
// 그리고 "옮긴 기사"를 가리킬 때는 화면에 실제로 적힌 라벨(「다른 소제목」)을 쓴다 —
// "드롭다운"은 이 화면에 넷이나 있어(기사별·일괄이동 바·보기 순서·담아둔 기사 승격)
// 어느 것인지 특정이 안 되고, 담당자가 화면에서 눈으로 찾을 수 있는 글자도 아니다.
function regenerateSubheadings(btn) {{
  var msg;
  if (btn.dataset.degraded === "1") {{
    msg = "지금 소제목은 AI가 지은 이름이 아니라, 단어 빈도수에 따라 기계적으로 뽑은 단어입니다.\\n"
        + "{reclassify_safe_note}\\n\\nAI로 다시 묶을까요?";
  }} else {{
    var edits = parseInt(btn.dataset.manualEdits || "0", 10);
    var editsLine = edits > 0
      ? "소제목의 이름과 순서가 초기화되고, 「다른 소제목」으로 옮긴 기사 " + edits + "건은 새 소제목을 따라갑니다.\\n"
      : "소제목의 이름과 순서가 초기화됩니다.\\n";
    msg = "전체 기사를 다시 분류합니다.\\n" + editsLine
        + "{reclassify_safe_note}\\n\\n계속할까요?";
  }}
  if (!confirm(msg)) {{
    return;
  }}
  btn.disabled = true;
  var label = btn.innerHTML;
  btn.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 3l1.9 5.1L18 10l-5.1 1.9L11 17l-1.9-5.1L4 10l5.1-1.9Z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8Z"/></svg> 분류하는 중…';
  fetch("http://{settings_host}:{settings_port}/regenerate-subheadings-draft", {{
    method: "POST", keepalive: true
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{
      btn.disabled = false; btn.innerHTML = label;
      alert("소제목을 다시 만들지 못했습니다. 잠시 후 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    btn.disabled = false; btn.innerHTML = label;
    alert("소제목을 다시 만들지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}

// [추가: 2026-08-11] ↩ 되돌리기 — 숨기기·소제목 이동·이름 변경·AI 재분류를 모두
// 같은 경로로 한 단계 되돌린다(app/undo.py가 상태를 통째로 스냅샷해둔다).
function undoLastAction(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/undo", {{ method: "POST", keepalive: true }})
    .then(function(res) {{
      // [추가: 2026-09-15] 응답의 touched = 되돌린 동작에서 담당자가 손댄 기사. 새로고침 뒤
      // 그 기사만 세이지로 칠한다(아래 justMovedUrls). 칠한 기사가 화면에 하나도 안 보이면
      // 첫 기사로 스크롤해야 하는데(다른 소제목 이동을 되돌리면 멀리 떨어진 원래 소제목으로
      // 돌아간다), 브라우저의 스크롤 복원은 load 뒤 언제 끝날지 몰라 "보이나"를 판정할 수
      // 없다(실측: load 50ms 뒤엔 아직 맨 위). 그래서 이번 새로고침만 복원을 끄고 지금
      // 위치(justMovedScroll)를 직접 들고 가서 되살린 뒤 판정한다.
      if (res.ok) {{
        return res.json().catch(function() {{ return {{}}; }}).then(function(data) {{
          var touched = (data && data.touched) || [];
          if (touched.length) {{
            sessionStorage.setItem("justMovedUrls", JSON.stringify(touched));
            sessionStorage.setItem("justMovedScroll", String(window.scrollY));
            history.scrollRestoration = "manual";
          }}
          location.reload();
        }});
      }}
      else if (res.status === 409) {{ btn.disabled = false; alert("되돌릴 작업이 없습니다."); }}
      else {{ btn.disabled = false; alert("되돌리기에 실패했습니다. 다시 시도해주세요."); }}
    }}).catch(function() {{
      btn.disabled = false;
      alert("되돌리기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
    }});
}}
function createCustomGroup() {{
  // [수정: 2026-09-03] app.renderer와 동일 — 같은 창, 같은 목록.
  openNamePicker({{ mode: "create", onPick: _createCustomGroupSubmit }});
}}
function _createCustomGroupSubmit(name) {{
  // [수정: 2026-08-04] app.renderer와 동일 — 지금 화면에 떠 있는 소제목만 중복 검사.
  var body = new URLSearchParams({{name: name}});
  getAllGroups().forEach(function(g) {{ body.append("active_names", g.value); }});
  fetch("http://{settings_host}:{settings_port}/add-custom-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    // [수정: 2026-09-03] 409는 서버가 보내준 이유 문장을 그대로 보여준다 — 본문이 없을
    // 때만 옛 문구로 물러선다. [수정: 2026-09-16] 이제 409가 뜨는 건 "이 화면에 같은
    // 이름이 이미 있다" 하나뿐이다(다른 회차 이름표와의 겹침 검사는 걷어냈다 —
    // app.settings_server._handle_add_custom_group 참고).
    else if (res.status === 409) {{
      res.text().then(function(msg) {{
        alert(msg || ("이미 '" + name + "'라는 이름의 소제목이 있습니다."));
      }}).catch(function() {{ alert("이미 '" + name + "'라는 이름의 소제목이 있습니다."); }});
    }}
    else {{ alert("소제목 만들기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("소제목 만들기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [수정: 2026-08-10] app.renderer와 동일 — 메모 없을 때만 렌더링되는 버튼이라 클릭은
// 항상 "열기"다. 패널을 열면서 클릭한 버튼 자신도 바로 숨긴다.
function toggleKeywordNote(btn) {{
  document.getElementById("keyword-note-zone").classList.add("is-open");
  btn.style.display = "none";
}}
{url_add_script}
{note_history_script}
// [추가: 2026-08-10] app.renderer와 동일 — 저장/수정 버튼 통합.
function toggleKeywordEditMode(btn) {{
  var input = document.getElementById("keyword-note-input");
  if (input.disabled) {{
    input.disabled = false;
    input.focus();
    btn.textContent = "저장";
  }} else {{
    saveKeywordNote();
  }}
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
// [추가: 2026-08-21] 사용자 요청 — 메모 칸에서 엔터만 쳐도 "저장"/"수정" 버튼을 누른
// 것과 똑같이 저장된다. 한글은 조합 중 엔터로 글자를 확정하므로 그 엔터
// (isComposing / keyCode 229)는 무시한다 — 안 그러면 마지막 글자를 확정하려던 엔터가
// 그대로 저장으로 새어 나가 조합이 끊긴다.
function keywordNoteKey(ev) {{
  if (ev.key !== "Enter") return;
  if (ev.isComposing || ev.keyCode === 229) return;
  ev.preventDefault();
  saveKeywordNote();
}}
function clearKeywordNote() {{
  document.getElementById("keyword-note-input").value = "";
  saveKeywordNote();
}}
// [추가: 2026-08-10] app.renderer와 동일 — 저장된 메모가 없는 채로 처음 여는 중이면
// "삭제"가 아니라 "취소"다.
function cancelKeywordNote() {{
  location.reload();
}}
function removeCustomGroup(btn) {{
  var name = btn.dataset.name;
  // [수정: 2026-08-11] 확인창에는 화면에 보이는 이름(data-current)을 쓴다 — 예전엔
  // data-name(자동 생성된 원래 단어)을 그대로 보여줘서, ✏️로 이름을 바꿔둔 소제목을
  // 지우려 하면 "내가 본 적 없는 이름"이 뜨는 버그가 있었다(사용자 제보). 서버로
  // 보내는 건 여전히 원래 단어 name이다 — 저장 키가 그것이기 때문.
  if (!confirm('"' + (btn.dataset.current || name) + '" 소제목을 삭제할까요?')) return;
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
    // [추가: 2026-08-20] 이동 한 번마다 분류가 다시 계산되므로(app.preview_renderer.
    // _compute_preview_content), 복사·다운로드 확인창이 이동 직후에도 최신 폴백 상태를
    // 보도록 같이 갱신한다. actions_html도 새로 갈아끼워졌으니 재분류 버튼의
    // data-degraded도 이미 최신이다.
    CLASSIFY_DEGRADED = data.classify_degraded;
    CLASSIFY_DEGRADED_NAMES = data.classify_degraded_names;
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
// [수정: 2026-08-11] app.renderer와 동일 — 담아둔 기사의 승격 조작이 이모지 버튼
// 두 개에서 화면별 소제목 드롭다운 하나로 바뀌었다(이유는 app.renderer
// ._manual_promote_control_html 참고). el은 <select>일 수도 소제목이 없는 화면의
// 📥 <button>일 수도 있는데, 둘 다 value가 있어(버튼은 기본 "") 같은 코드로 읽힌다.
function _promoteManual(el, endpoint, failMsg) {{
  var url = el.dataset.url;
  var group = el.value || "";
  el.disabled = true;
  fetch("http://{settings_host}:{settings_port}/" + endpoint, {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, group: group}})
  }}).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justPromotedUrl", url); location.reload(); }}
    else {{ el.disabled = false; el.selectedIndex = 0; alert(failMsg + " 다시 시도해주세요."); }}
  }}).catch(function() {{
    el.disabled = false; el.selectedIndex = 0;
    alert(failMsg + " — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function promoteManualArticle(el) {{
  _promoteManual(el, "promote-manual-article", "확정본에 넣지 못했습니다.");
}}
function promoteManualArticleToDraft(el) {{
  _promoteManual(el, "promote-manual-article-to-draft", "초안에 넣지 못했습니다.");
}}
function renameGroup(btn) {{
  // [수정: 2026-09-03] app.renderer와 동일 — prompt() 대신 이름 고르기 창(openNamePicker).
  openNamePicker({{
    mode: "rename",
    self: btn.dataset.current,
    onPick: function(picked) {{ _renameGroupSubmit(btn.dataset.name, picked); }}
  }});
}}
function _renameGroupSubmit(name, newLabel) {{
  // [수정: 2026-08-04] app.renderer와 동일 — 지금 화면에 떠 있는 소제목만 중복 검사.
  var body = new URLSearchParams({{name: name, label: newLabel}});
  getAllGroups().forEach(function(g) {{ body.append("active_names", g.value); }});
  fetch("http://{settings_host}:{settings_port}/rename-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else if (res.status === 409) {{ alert("이미 '" + newLabel + "'라는 이름의 소제목이 있습니다."); }}
    else {{ alert("이름 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("이름 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function hideGroup(btn) {{
  var section = btn.closest(".subheading");
  // [수정: 2026-09-16] 확인창을 없앴다 — 보관함 줄 끝 🗑(확인창 없음)와 같은 근거다:
  // 되돌릴 자리가 곧바로 화면에 남는다(좌하단 ↩ 한 번이면 통째로 되살아나고, 휴지통
  // 팝오버엔 「소제목명 N건 · ↩ 모두 복구」 한 줄이 남는다). 소제목 하나가 통째로
  // 사라지는 건 화면에서 즉시 보이므로 "모르고 지나치는" 실수도 안 생긴다.
  // 하단바 일괄 🗑(bulkHideSelected)도 확인창이 없다.
  // [수정: 2026-08-20] url만 뽑아 보내던 것을, 버튼 자체를 넘겨 언론사·제목·발행시각까지
  // 함께 보내도록 바꿨다(hideArticleBody 주석 참고).
  if (section.querySelector(".hiding-note")) {{ return; }}
  var btns = Array.prototype.slice.call(section.querySelectorAll(".hide-btn[data-url]"));
  if (!btns.length) {{ return; }}
  // [수정: 2026-09-22] 기사 수만큼 요청을 나눠 보내던 것을 한 요청으로 — 요청마다 되돌리기
  // 기록 쓰기와 화면 재생성이 되풀이돼 기사가 많을수록 몇 초씩 걸렸다. 누르는 순간 그 소제목의
  // 기사 줄을 흐리게 하고 🗑 자리 앞에 「N건 숨기는 중…」을 띄운다(hide_batch_script) — 머리줄은
  // 흐리지 않는다(안내 글자까지 흐려져 안 읽힌다).
  var restore = showHiding(btns.map(function (b) {{ return b.closest(".article"); }}), btn, btns.length);
  postHideBatch(btns, newHideBatchId()).then(function (res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ restore(); alert("숨기지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function () {{
    restore();
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
// "내가 클릭해봤나"가 아니라 "지난 로드 이후 새로 생겼나"). 기준선이 아예 없으면
// (이 회차를 처음 여는 것) 비교 대상이 없으므로 아무것도 표시하지 않고 이번 목록을
// 기준선으로만 저장한다.
// [수정: 2026-08-13] 기준선을 회차별로 나눈다(예전엔 "previewKnownUrls" 하나로 전역).
// 회차가 바뀌면 검색 구간 자체가 달라져 기사가 통째로 갈리는데, 전역 기준선이면 그
// 순간 화면 전체가 노랗게 물들어 "새로 들어온 것"이라는 정보가 무의미해진다. 회차
// 종료 시각(ROUND_DEADLINE_MS)은 (날짜, 회차)마다 유일하므로 그대로 식별자로 쓴다 —
// 회차가 바뀌면 기준선이 없는 상태가 되어 위 규칙대로 아무것도 표시하지 않는다.
(function () {{
  var storageKey = "previewKnownUrls:" + ROUND_DEADLINE_MS;
  var currentUrls = Array.prototype.map.call(
    document.querySelectorAll(".article[data-url]"),
    function (el) {{ return el.dataset.url; }}
  );
  var known = null;
  try {{
    var raw = localStorage.getItem(storageKey);
    known = raw === null ? null : JSON.parse(raw);
  }} catch (e) {{ known = null; }}
  if (known !== null) {{
    document.querySelectorAll(".article[data-url]").forEach(function (el) {{
      if (known.indexOf(el.dataset.url) === -1) {{ el.classList.add("is-new-arrival"); }}
    }});
  }}
  try {{
    localStorage.setItem(storageKey, JSON.stringify(currentUrls));
  }} catch (e) {{ /* 저장 실패(용량 초과 등)는 표시가 한 번 빠지는 것뿐이라 무시한다 */ }}
  // 지난 회차·지난 날짜의 기준선은 다시 쓸 일이 없으므로 여기서 정리한다 — 그냥 두면
  // 회차마다 키가 하나씩 쌓여 localStorage에 계속 남는다.
  for (var i = localStorage.length - 1; i >= 0; i--) {{
    var key = localStorage.key(i);
    if (key && key.indexOf("previewKnownUrls:") === 0 && key !== storageKey) {{
      localStorage.removeItem(key);
    }}
  }}
  localStorage.removeItem("previewKnownUrls");  // 전역 방식이던 시절의 잔재 정리
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
// [수정: 2026-09-15] ↩ 되돌리기도 이 표시를 쓴다(undoLastAction이 담당자가 손댄 기사를 남긴다).
// 세이지가 칠해진 행에서는 노랑(.is-new-arrival)을 걷어낸다 — 숨긴 기사를 되살리면 그 기사는
// 직전 로드의 기준선에 없어 "새로 들어온 기사"로 판정되는데, 담당자가 방금 손댄 기사라
// 새로 온 게 아니다(위 기준선 코드가 먼저 돌아 노랑을 붙인 뒤라 여기서 떼면 된다).
(function() {{
  var raw = sessionStorage.getItem("justMovedUrls");
  var savedY = sessionStorage.getItem("justMovedScroll");
  sessionStorage.removeItem("justMovedScroll");
  // undoLastAction이 이번 새로고침만 꺼둔 스크롤 복원은 **이 페이지를 떠날 때** 되돌린다
  // (다른 동작의 새로고침은 예전처럼 브라우저가 알아서 복원한다). 여기서 곧바로 켜면
  // 브라우저가 load 뒤에 옛 위치를 되살려 아래에서 옮긴 스크롤을 덮어쓴다(실측).
  if (savedY !== null) {{
    window.addEventListener("pagehide", function() {{ history.scrollRestoration = "auto"; }});
  }}
  if (!raw) {{ return; }}
  sessionStorage.removeItem("justMovedUrls");
  var marked = [];
  JSON.parse(raw).forEach(function(u) {{
    var el = document.querySelector('.article[data-url="' + CSS.escape(u) + '"]');
    if (el) {{
      el.classList.add("just-moved");
      el.classList.remove("is-new-arrival");
      marked.push(el);
    }}
  }});
  if (savedY === null) {{ return; }}
  // 되돌리기 전 위치로 먼저 돌려놓고, 칠한 기사가 거기서 하나라도 보이면 그대로 둔다
  // (숨기기·↑↓를 되돌린 경우 대개 눈앞에 있다). 하나도 안 보일 때만 첫 기사로 옮긴다.
  function scrollIfHidden() {{
    window.scrollTo(0, Number(savedY) || 0);
    if (!marked.length) {{ return; }}
    var topbar = document.querySelector(".topbar");
    var top = topbar ? topbar.getBoundingClientRect().bottom : 0;
    var visible = marked.some(function(el) {{
      var r = el.getBoundingClientRect();
      return r.bottom > top && r.top < window.innerHeight;
    }});
    // 탭이 가려져 있으면 부드러운 스크롤은 미뤄지는 게 아니라 통째로 버려진다(실측) —
    // 그땐 곧바로 옮긴다.
    if (!visible) {{
      marked[0].scrollIntoView({{ block: "center", behavior: document.hidden ? "auto" : "smooth" }});
    }}
  }}
  if (document.readyState === "complete") {{ scrollIfHidden(); }}
  else {{ window.addEventListener("load", scrollIfHidden); }}
}})();
// [추가: 2026-08-12] "🤖 미분류 기사 분류"가 방금 배정한 기사들을 한 번 표시한다.
(function() {{
  var raw = sessionStorage.getItem("justClassifiedUrls");
  if (!raw) {{ return; }}
  sessionStorage.removeItem("justClassifiedUrls");
  JSON.parse(raw).forEach(function(u) {{
    var el = document.querySelector('.article[data-url="' + CSS.escape(u) + '"]');
    if (el) {{ el.classList.add("just-classified"); }}
  }});
}})();
// [추가: 2026-08-05] app.renderer와 동일 — 남겨둔 신호가 있으면 목차 팝오버를 다시
// 열어둔다.
// [수정: 2026-08-18] 이제 신호를 남기는 쪽은 _tocApply뿐이라, 여기서 다시 열리는 팝오버는
// "방금 적용한 결과"를 보여주는 자리다(옮긴 소제목이 초록으로 한 번 표시된다).
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


def _draft_heading(slot_end, now=None, slot: Optional[dict] = None) -> dict:
    """초안 제목·탭 제목 — 확정본과 같은 머리줄 「언론 모니터링 N시 기준」 + 뒤에 회색
    「(예정, 지금 N시 N분 기준)」. 어느 화면인지는 제목 앞 (초안) 칩이 말한다(NAME_CHIP_MOCKUP.html).
    탭 제목은 좁아지면 뒤가 잘리므로 화면 이름을 맨 앞에 둔다.

    slot을 넘기면(기사가 그려지는 초안) 회색 시각 바로 뒤에 「✂ 오늘만 여기서 끊기」 입구를
    붙인다(_cut_heading_html)."""
    if not slot_end:
        return {"run_slot_html": "", "page_title": "초안"}
    base = f"언론 모니터링 {format_slot_time_kr(slot_end)} 기준"
    sub = f"예정, 지금 {format_slot_time_kr(now)} 기준" if now else "예정"
    return {
        "run_slot_html": f'{html.escape(base)}<span class="head-sub">({html.escape(sub)})</span>'
        + (_cut_heading_html(slot) if slot else ""),
        "page_title": html.escape(f"초안 · {base}"),
    }


def _cut_heading_html(slot: dict) -> str:
    """머리줄의 「✂ 오늘만 여기서 끊기」 — 평소엔 흐린 ✂ 하나, 마우스를 올리면 글자가 붙는다.

    평소엔 쓸 일이 없는 기능이라 자리만 기억할 수 있게 흐리게 둔다(시안 SLOT_CUT_MOCKUP.html).
    누르면 끊기 창(#cut-pop)이 열리고, 창 안에서 한 번 더 눌러야 끊긴다. 끊은 상태는 옆 칩:
    예약이면 「✂ 오늘만 16:00에 끊음 · 취소」, 이미 확정본이 됐으면 「✂ 오늘 15:57에 끊음」.
    창의 내용(시각 칩·몇 건씩 갈리는지)은 JS가 여는 순간 그린다(openCutPop).
    """
    try:
        from app.cut_round import cut_state

        state = cut_state()
    except Exception:
        logger.exception("오늘만 끊기 상태를 읽지 못했습니다 — 머리줄 ✂를 그리지 않습니다")
        return ""
    chip = ""
    if state["pending"]:
        end = html.escape(state["pending"])
        chip = (
            f'<span class="cut-chip" title="오늘만 — {end}이 되면 확정본이 만들어집니다">'
            f'✂ 오늘만 {end}에 끊음<button type="button" data-end="{end}" onclick="cancelCut(this)">취소</button></span>'
        )
    elif state["done"]:
        ends = " · ".join(html.escape(e) for e in state["done"])
        chip = (
            f'<span class="cut-chip done" title="오늘만 — 자정이 지나면 원래 수집 시간으로 돌아갑니다">'
            f'✂ 오늘 {ends}에 끊음</span>'
        )
    return (
        '<button type="button" class="cut-btn" onclick="openCutPop(event)" '
        'title="오늘만 — 이 시간까지 확정본으로 넣고, 뒤 기사는 원래 회차가 이어서 모읍니다" '
        'aria-label="여기서 끊기">✂<span class="lbl">여기서 끊기</span></button>'
        + chip
        + f'<div class="cut-pop" id="cut-pop" hidden data-start="{html.escape(slot["start"])}" '
        f'data-end="{html.escape(slot["end"])}" onclick="event.stopPropagation()"></div>'
    )


def _theme() -> dict:
    # [수정: 2026-08-21] 색 값은 전부 app.config.PALETTE 하나에서 온다 — 예전엔
    # seen_color/new_arrival_bg/tonal_hover 같은 값이 여기 직접 적혀 있었고, 같은 색을
    # 쓰는 app.renderer에도 따로 적혀 있어 한쪽만 바꾸면 두 화면이 어긋났다.
    return {
        **PALETTE,
        "font_stack": FONT_STACK,
        "topnav_style": topnav_style(),
        "topnav_html": regular_nav("preview"),
        # [추가: 2026-09-01] 색은 아니지만 같은 이유로 여기 싣는다 — 「AI 모든 기사
        # 재분류」 확인창의 안심 문구를 확정본과 글자 하나까지 공유하기 위해서고,
        # _theme()을 거치면 이 파일의 format 자리 네 곳에 따로 등록할 필요가 없다
        # (str.format은 안 쓰는 키를 그냥 무시한다).
        "reclassify_safe_note": RECLASSIFY_SAFE_NOTE,
        # [추가: 2026-09-15] 「AI 기사 나누기」 — CSS·마크업·JS 모두 확정본(app.renderer)과 한 곳에서
        # 만든 값(휴지통과 같은 이유). 초안은 /split-group, 확정본은 /split-group-final로 보낸다.
        "split_button_style": split_button_style(),
        "screen_tag_style": screen_tag_style(),
        "split_button_html": split_button_html(),
        "split_button_script": split_button_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT, "/split-group"),
        # [추가: 2026-09-04] "앞 회차 기사" 칩 CSS — 확정본(app.renderer)과 한 곳에서
        # 만든 값을 그대로 받는다. 초안도 확정본과 같은 창을 보게 되면서 이 칩이 여기에도
        # 붙기 때문이고, .photo-badge처럼 복붙해 두면 한쪽만 고쳤을 때 어긋난다.
        "late_badge_style": late_badge_style(),
        "export_links_style": export_links_style(),
        # [추가: 2026-09-14] 🔍 검색어 CSS·툴팁 — 확정본과 한 곳에서 만든 값(같은 이유).
        "kw_inline_style": kw_inline_style(),
        "kw_inline_script": kw_inline_script(),
        # [추가: 2026-09-15] 📷 사진 추정 모아 보기 — 확정본과 한 곳에서 만든 값. 기억하는
        # 키는 회차 마감 시각(ROUND_DEADLINE_MS)이라 다음 회차 초안은 평소 화면으로 열린다.
        "photo_gather_style": photo_gather_style(),
        "photo_gather_script": photo_gather_script('"photoGather:preview:" + ROUND_DEADLINE_MS'),
        # [추가: 2026-09-02] 좌하단 휴지통 — CSS·마크업·JS 모두 확정본(app.renderer)의
        # 것을 그대로 쓴다. 두 화면이 같은 자리에 같은 것을 보여줘야 하는데 코드가 갈리면
        # 한쪽만 고쳐져 어긋난다(이 파일이 render_article을 import해 쓰는 것과 같은 이유).
        "hidden_trash_style": hidden_trash_style(),
        "hidden_trash_html": hidden_trash_html(_hidden_href()),
        "hidden_trash_script": hidden_trash_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT),
        "scroll_top_style": scroll_top_style(),
        "scroll_top_html": scroll_top_html(),
        "scroll_top_script": scroll_top_script(),
        # [추가: 2026-09-23] 툴바 오른쪽 끝의 「새로고침」 — 확정본(app.renderer)과 같은 코드.
        # 초안은 열 때마다 네이버를 다시 검색하므로, 이 버튼이 곧 「지금까지 모인 것 다시 보기」다.
        "refresh_link_style": refresh_link_style(),
        "refresh_link_script": refresh_link_script(),
        "hide_batch_style": hide_batch_style(),
        "hide_batch_script": hide_batch_script(),
        "range_select_script": range_select_script(".article-select", ".article"),
        "url_add_style": url_add_style(),
        "url_add_script": url_add_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT, "preview"),
        "label_popover_style": label_popover_style(),
        "label_popover_script": label_popover_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT),
        "name_picker_style": name_picker_style(),
        "name_picker_html": name_picker_html(),
        # 초안이 보여주는 회차는 **아직 저장되지 않은 진행 중인 회차**라, 저장된 회차 중
        # 가장 최근 것이 곧 "직전 회차"다 — name_pool(None)이 정확히 그 값을 준다
        # (round_id를 넘기는 확정본과 결과가 같아야 하는 게 아니라, 각자 자기 앞 회차를
        # 가리키는 게 맞다). _theme()에는 회차 정보가 없기도 하다.
        "name_picker_script": name_picker_script(name_pool(None)),
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
        "keywords": search_condition(groups),
        "slot_end": slot["end"],
        # 「✂ 오늘만 여기서 끊기」로 회차를 끊으면 끝은 그대로(17:00)인데 시작이 끊은 시각으로
        # 당겨진다 — 시작도 서명에 넣어야 끊기 전 넓은 창에서 모은 캐시를 이어 쓰지 않는다.
        "slot_start": slot.get("start"),
        # [추가: 2026-09-04] 검색 하한 규칙이 바뀌면 그 전에 쌓인 캐시는 더 좁은 창에서
        # 모은 것이라 그대로 이어 쓰면 안 된다 — 값을 서명에 넣어두면 규칙이 바뀌는
        # 순간 캐시가 자동으로 무효화돼 회차 시작−lookback부터 다시 한 번 훑는다.
        "lookback_min": COLLECT_LOOKBACK_MIN,
    }


def _preview_window_floor(slot: dict) -> datetime:
    """초안 검색 창의 바닥(이 시각 **초과**만 이 회차 초안 대상).

    [수정: 2026-09-04] 바닥을 회차 시작이 아니라 COLLECT_LOOKBACK_MIN만큼 앞으로
    물린다 — app.scraper.collect_run이 2026-09-03부터 쓰는 하한과 **같은 값**이다.
    그때 확정본에만 넣고 여기를 안 고쳐서, 앞 회차 마감 순간 네이버가 아직 색인하지
    않은 기사가 "확정본에는 뒤늦게 회수돼 들어오는데 초안에는 끝까지 안 보이는"
    상태가 됐다(실측 2026-09-04 09:54: 초안 11건 / 11:00 확정본이 볼 17건, 차이 7건).
    담당자 입장에서는 그냥 사라진 기사이고, 초안에서 다 정리해둔 뒤 확정본에 없던
    기사가 튀어나온다. 두 화면이 같은 창을 봐야 한다.
    [수정: 2026-09-11] 붙잡아 둔 기사(app.draft_seen)를 되돌려 넣을 때도 이 바닥으로
    거르느라 함수로 뺐다 — 회차 도중 스케줄을 바꿔 창이 좁아지면 확정본(collect_run)은
    창 밖 기사를 안 담는데 초안만 붙잡고 있으면 두 화면이 어긋난다.
    """
    slot_start = kst_today_at(slot["start"])
    return max(
        slot_start - timedelta(minutes=COLLECT_LOOKBACK_MIN),
        slot_start.replace(hour=0, minute=0, second=0, microsecond=0),
    )


def _load_preview_search_base(groups: list, slot: dict) -> tuple:
    """캐시가 유효하면(오늘 날짜 + 키워드 구성 + 회차 동일) 캐시된 기사와 "이번에 어디부터
    검색할지"를, 아니면(캐시 없음/날짜 바뀜/키워드 바뀜/회차 바뀜) 빈 목록과 이 회차의
    시작 시각을 돌려준다. app.live_renderer._live_search_plan과 같은 패턴이다.

    [수정: 2026-08-21] 하한을 캐시의 "마지막으로 본 시각"(=지금까지 본 가장 최신
    pub_date) 그대로 쓰지 않고 app.naver_api.incremental_search_after에 통과시킨다 —
    네이버가 발행시각 순서대로 색인하지 않아, 그대로 쓰면 뒤늦게 색인된 기사가 초안에서
    통째로 빠졌다(그쪽 docstring 참고). 저장하는 값(last_seen_pub_date)은 예전 그대로
    "가장 최신 pub_date"이고, 물러서는 건 읽는 이 시점뿐이다.
    """
    signature = _preview_keywords_signature(groups, slot)
    earliest = _preview_window_floor(slot)
    cache = load_preview_cache()
    if cache is not None and cache.get("signature") == signature:
        last_seen = cache.get("last_seen_pub_date")
        if last_seen:
            return cache.get("articles", []), incremental_search_after(
                datetime.fromisoformat(last_seen), earliest
            )
    return [], earliest


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

    [수정: 2026-08-13] app.naver_api.search_articles_by_groups가 이제 (articles,
    failed_keywords) 튜플을 돌려준다 — 키워드별 재시도까지 다 쓰고도 실패한 게 있으면
    (app.live_renderer.generate_live_page와 같은 이유로) 캐시를 갱신하지 않는다.
    last_seen_pub_date를 전진시킨 채로 저장하면 실패했던 구간이 다음 계산에서
    영영 건너뛰어진다 — 초안은 회차가 끝날 때까지 여러 번 다시 계산되므로, 이번에
    실패해도 다음 계산(다음 새로고침·다음 ↑/↓ 조작)이 같은 구간을 다시 시도하게
    비워두는 게 맞다.

    [추가: 2026-07-30] 마지막에 app.preview_order.apply_preview_order를 거친다 — 검색
    결과는 매번 새로 계산되지만(저장된 회차 없음), 사용자가 ↑/↓로 같은 소제목 안에서
    정리해둔 순서(preview_move_article이 저장)는 여기서 다시 입혀진다. 그 사이 새로
    나온 기사는 원래 순서(언론사 우선순위) 그대로 뒤에 붙는다.
    """
    groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if group_in_scrap(g)]
    )
    outlet_order = settings.get("outlet_order", [])
    signature = _preview_keywords_signature(groups, slot)
    cached_articles, after_dt = _load_preview_search_base(groups, slot)
    before_dt = kst_today_at(datetime.now().strftime("%H:%M"))
    failed_keywords: list = []
    try:
        # [추가: 2026-08-21] app.scraper.collect_run과 같은 이유 — 어떤 검색어로
        # 걸린 기사인지를 화면에 보여주려면 여기서 받아 초안 캐시에 담아둬야 한다.
        new_articles, failed_keywords = search_articles_by_groups(
            groups, after=after_dt, before=before_dt, track_keyword_matches=True
        )
    except requests.exceptions.RequestException:
        if not cached_articles:
            raise
        new_articles = []
    existing_cache_urls = {a["url"] for a in cached_articles}
    articles = cached_articles + [a for a in new_articles if a["url"] not in existing_cache_urls]
    if not failed_keywords:
        pub_dates = [datetime.fromisoformat(a["pub_date"]) for a in articles if a.get("pub_date")]
        newest = max(pub_dates) if pub_dates else after_dt
        save_preview_cache(signature, newest.isoformat(), articles)

    # [추가: 2026-09-04] 넓힌 하한 때문에 다시 딸려온 "앞 회차에 이미 실린 기사"를 뺀다 —
    # app.scraper.collect_run과 **같은 함수·같은 자리**(모든 필터보다 먼저)다. 먼저 안
    # 빼면 이미 보고서에 나간 기사가 deduplicate_by_title의 대표 자리를 차지해 새 기사를
    # 밀어낸다. 캐시에는 걸러내기 전 목록을 그대로 저장한다(위) — 회차가 넘어가며
    # "이미 나간 기사"의 범위가 넓어질 뿐이라 매번 다시 판정하는 게 맞다.
    # [추가: 2026-09-11] 초안에 한 번 보인 기사를 붙잡아 둔다(app.draft_seen) — 이번
    # 검색에 안 잡혔어도(네이버에서 사라짐 등) 목록에 되돌려 넣고, 같은 제목 기사가 새로
    # 들어와도 먼저 보인 쪽이 대표를 지킨다(아래 deduplicate_by_title의 prefer). 그 뒤의
    # 필터(이미 실림·선택 언론사·사진/인사 제외·숨김)는 붙잡은 기사에도 똑같이 걸린다 —
    # 담당자가 숨기거나 조건을 바꾼 건 붙잡기보다 앞선다(사용자 결정, 2026-09-11).
    # 확정본(app.scraper.collect_run)도 같은 목록을 같은 자리에서 합친다.
    round_id = round_id_for_slot(slot)
    condition = search_condition(groups)
    seen = load_draft_seen(round_id, condition)
    if seen["reset_reason"] == "condition":
        logger.info("검색어가 바뀌어 초안에 붙잡아 둔 기사 목록을 새로 시작합니다 (회차=%s)", slot["end"])
    pool_urls = {a["url"] for a in articles}
    floor_iso = _preview_window_floor(slot).isoformat()
    articles = articles + [
        dict(a) for u, a in seen["articles"].items()
        if u not in pool_urls and (a.get("pub_date") or "") > floor_iso
    ]
    pool_by_url = {a["url"]: a for a in articles}
    # 빠진 이유 기록용 — 단계마다 걸러진 URL에 그 단계 이름을 붙인다(_log_draft_drops).
    dropped_at: dict = {}

    def _track(before: list, after: list, reason: str) -> list:
        kept = {a["url"] for a in after}
        for a in before:
            if a["url"] not in kept:
                dropped_at.setdefault(a["url"], reason)
        return after

    slot_start_dt = kst_today_at(slot["start"])
    published = _already_published_urls(slot_start_dt.strftime("%Y-%m-%d"))
    if published:
        articles = _track(articles, [a for a in articles if a["url"] not in published], "앞 회차에 이미 실림")
    # 남은 것 중 이 회차 창보다 앞선 기사가 곧 "뒤늦게 회수된 기사"다. 확정본과 같은
    # 필드를 달아두면 app.renderer.render_article이 그리는 "⏱ 앞 회차 기사" 칩이 초안에도
    # 그대로 붙는다 — 왜 09시 기사가 11시 초안에 있는지 화면이 스스로 설명하게 된다.
    slot_start_iso = slot_start_dt.isoformat()
    schedule_times = active_schedule_times(settings)
    for a in articles:
        pub = a.get("pub_date")
        if pub and pub <= slot_start_iso:
            a["late_pickup"] = True
            a["late_pickup_slot"] = _slot_end_for_pub_date(pub, schedule_times)

    if outlet_order:
        articles = _track(articles, filter_by_outlet_whitelist(articles, outlet_order), "선택 언론사 밖")
        articles = sort_by_outlet_priority(articles, priority_outlets=outlet_order)
    else:
        articles = sort_by_outlet_priority(articles)
    articles = _track(articles, deduplicate_by_title(articles, prefer=seen["seen_order"]), "같은 제목 기사에 대표를 넘김")
    # [추가: 2026-08-20] [단독] 기사를 소제목 내 최상단으로 올리기 위한 첫 단계 —
    # app.filters.sort_scoop_first 참고, app.scraper.collect_run과 같은 자리(같은
    # 순서 규칙 — apply_preview_order보다 먼저).
    articles = sort_scoop_first(articles)
    # [추가: 2026-07-27] "위로"(예약) 눌러둔 기사를 검색 결과에 합쳐서, 이 회차가
    # 실제로 저장되기 전에도 초안 화면 소제목 분류에 바로 묶여 보이게 한다
    # (app.scraper.collect_run이 저장 시점에 똑같이 합치므로 나중에도 그대로 유지된다).
    pending = load_draft_pending_articles()
    if pending:
        existing_urls = {a["url"] for a in articles}
        articles = articles + [a for a in pending if a["url"] not in existing_urls]
    articles = apply_summary_overrides(_track(articles, filter_hidden(articles), "숨김"))
    articles = apply_preview_order(articles, load_preview_order())

    # [추가: 2026-09-11] 지금 보이는 기사를 붙잡기 목록에 더하고, 직전에 보였는데 이번에
    # 빠진 기사가 있으면 이유를 로그에 남긴다. 저장은 검색에서 온 원본(pool_by_url —
    # 제목 직접 수정 등이 입혀지기 전)으로 한다. "위로" 예약 기사(pending)는 그 경로가
    # 따로 확정본에 합쳐 주므로 여기 안 넣는다.
    visible_urls = {a["url"] for a in articles}
    _log_draft_drops(slot, seen["last_visible"], visible_urls, dropped_at, pool_by_url)
    record_draft_seen(round_id, condition, [pool_by_url[a["url"]] for a in articles if a["url"] in pool_by_url])
    return articles


def current_draft_urls() -> set:
    """지금 회차 초안에 **보이는** 기사 URL — 「+ 수기로 기사 추가」의 "이미 이 회차에 있다" 판정용.

    초안은 저장된 회차가 없어 `load_latest_run()`으로 보면 앞 회차와 비교하게 된다. 다시
    검색하지 않고 마지막으로 그린 초안의 목록(app.draft_seen의 last_visible)과 "위로"
    예약 기사(pending)를 합쳐 쓴다 — 담당자가 보고 있는 그 화면과 같은 집합이다. 숨긴
    기사는 둘 다 빠진다(보이지 않는 기사를 "이미 있다"고 막으면 찾을 길이 없다).
    """
    settings = load_settings()
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot is None:
        return set()
    groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if group_in_scrap(g)]
    )
    seen = load_draft_seen(round_id_for_slot(slot), search_condition(groups))
    urls = set(seen["last_visible"])
    urls |= {a["url"] for a in filter_hidden(load_draft_pending_articles()) if a.get("url")}
    return urls


def _log_draft_drops(slot: dict, last_visible: list, visible_urls: set, dropped_at: dict, pool_by_url: dict) -> None:
    """직전 초안에 있던 기사가 이번 초안에서 빠졌으면 **왜 빠졌는지** 로그에 남긴다.

    [추가: 2026-09-11] 2026-09-11 "소제목을 고쳤더니 늦게 들어온 기사가 사라졌다" 제보를
    그때 기록이 없어 끝내 재현하지 못했다 — 다음엔 이 로그 한 줄로 원인을 가린다.
    붙잡기(app.draft_seen) 이후 정상적인 이유는 담당자 동작(숨김)과 설정뿐이라,
    그 밖의 이유(검색 결과에서 사라짐·알 수 없음)는 WARNING으로 올린다.
    """
    gone = [u for u in last_visible if u not in visible_urls]
    if not gone:
        return
    expected = {"숨김", "선택 언론사 밖", "사진기사 제외 설정", "인사 기사 제외 설정", "앞 회차에 이미 실림", "같은 제목 기사에 대표를 넘김"}
    lines = []
    unexpected = False
    for url in gone:
        reason = dropped_at.get(url) or ("검색 결과에서 사라짐" if url not in pool_by_url else "알 수 없음")
        unexpected = unexpected or reason not in expected
        a = pool_by_url.get(url) or {}
        lines.append(f"  - {reason} | {a.get('outlet', '?')} | {(a.get('title') or url)[:60]}")
    (logger.warning if unexpected else logger.info)(
        "초안에서 빠진 기사 %d건 (회차=%s)\n%s", len(gone), slot["end"], "\n".join(lines)
    )


def _group_select_html(current_name: str, all_names: list, labels: dict, custom_names: Optional[set] = None) -> str:
    """"다른 소제목으로" 드롭다운 — 지금 속한 소제목은 옵션에서 빼고 나머지를 보여준다.

    [추가: 2026-07-30] 체크박스 하단 바와 별개로, 기사 하나만 바로 다른 소제목에
    옮기고 싶을 때 쓴다(경계를 여러 번 넘나들며 ↑/↓를 반복할 필요 없이 한 번에 이동).
    고르는 즉시 반영되고(onchange), 옮길 곳이 아예 없으면(소제목이 이거 하나뿐)
    빈 문자열을 돌려줘 드롭다운 자체를 숨긴다.

    [추가: 2026-08-07] app.renderer._group_select_html과 동일 — 사용자가 만든
    소제목만 볼드로 표시해 자동 분류 소제목과 구분한다.
    """
    custom_names = custom_names or set()
    others = [n for n in all_names if n != current_name]
    if not others:
        return ""
    options = "".join(
        f'<option value="{html.escape(n)}"'
        + (' style="font-weight:700"' if n in custom_names else "")
        + f'>{html.escape(_group_option_label(n, labels))}</option>'
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
    groups: list, highlight_words: list, line_template: str, labels: dict, rank_by_url: dict, custom_names: set,
    ai_names: set = frozenset(), subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    scrap_date: str = "", scrap_end: str = "",
    group_copy_texts: Optional[dict] = None,
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
    # [수정: 2026-09-10] app.renderer._render_groups와 같은 이유 — ▼는 "옮길 수 있는 마지막
    # 소제목"에서 잠근다(그 아래엔 고정 칸인 기타·📂 미분류만 온다).
    orderable_indexes = [i for i, g in enumerate(groups) if not is_order_locked(g["name"], labels)]
    first_orderable_index = orderable_indexes[0] if orderable_indexes else -1
    last_orderable_index = orderable_indexes[-1] if orderable_indexes else -1
    # [추가: 2026-08-18] app.renderer._render_groups와 같은 이유 — 페이지 전체에서 내용이
    # 같으므로 한 번만 만든다.
    known_labels_html = known_label_chips_html()
    for group_index, group in enumerate(groups):
        original_name = html.escape(group["name"])
        display_name = html.escape(display_group_name(group["name"], labels))
        if not group["articles"]:
            body = (
                '<p class="empty-group-hint">아직 기사가 없어요 — 체크박스로 선택하거나 '
                "드롭다운으로 기사를 여기로 옮겨보세요.</p>"
            )
            delete_btn = (
                f'<button class="rename-btn group-hide-btn" type="button" data-name="{original_name}" '
                f'data-current="{display_name}" '
                f'onclick="removeCustomGroup(this)" title="이 빈 소제목 삭제">{icon("trash")}</button>'
            )
        else:
            last_index = len(group["articles"]) - 1
            group_select = _group_select_html(group["name"], all_names, labels, custom_names)
            body = "\n".join(
                render_article(
                    a,
                    highlight_words,
                    line_template,
                    # [수정: 2026-08-14] app.renderer._render_groups와 같은 이유 —
                    # ↑/↓는 소제목 안 전용이라 그 소제목의 끝에서 바로 비활성화한다.
                    move={
                        "disable_up": i == 0,
                        "disable_down": i == last_index,
                        "hidden": group["name"] == UNCLASSIFIED_GROUP_NAME,
                    },
                    move_handler="previewMoveArticle",
                    checkbox=True,
                    group_select_html=group_select,
                    outlet_rank=rank_by_url.get(a["url"], 0),
                    show_label_control=True,
                    group_name=group["name"],
                    known_labels_html=known_labels_html,
                    scrap_date=scrap_date,
                    scrap_end=scrap_end,
                    show_matched_keywords=True,
                )
                for i, a in enumerate(group["articles"])
            )
            delete_btn = (
                f'<button class="rename-btn group-hide-btn" type="button" onclick="hideGroup(this)" '
                f'title="이 소제목 기사 {len(group["articles"])}건 통째로 숨기기(기사 각각 숨긴 것과 동일 — 되돌리기 가능)">{icon("trash")}</button>'
            )
        # [추가: 2026-08-03] app.renderer._render_groups와 동일한 이유 — 소제목 자체의
        # 화면 순서를 ↑/↓로 바꾼다.
        # [수정: 2026-09-10] 기타는 맨 뒤 고정이라 ▲▼를 안 그린다(app.renderer와 동일).
        order_locked = is_order_locked(group["name"], labels)
        order_up_disabled = " disabled" if group_index <= first_orderable_index else ""
        order_down_disabled = " disabled" if group_index >= last_orderable_index else ""
        order_html = "" if order_locked else (
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'up\');"{order_up_disabled} title="소제목 위로 이동">{icon("tri_up")}</button>'
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'down\');"{order_down_disabled} title="소제목 아래로 이동">{icon("tri_down")}</button>'
        )
        # [추가: 2026-08-04] app.renderer._render_groups와 동일 — 소제목 안 기사 전체 선택.
        select_all_html = (
            '<input type="checkbox" class="group-select-all" '
            'onclick="event.stopPropagation();" onchange="toggleGroupSelectAll(this);" '
            'title="이 소제목 기사 전체 선택"> '
            if group["articles"]
            else ""
        )
        # [추가: 2026-08-11] 임시 보관함은 소제목이 아니다 — 꺾쇠(<>)를 붙이면 확정본으로
        # 그대로 넘어갈 소제목처럼 보인다는 지적(사용자)으로, 꺾쇠를 빼고 폴더 느낌의
        # 회색 기울임꼴 + 건수 표기로 "대기 중"임을 드러낸다.
        if group["name"] == UNCLASSIFIED_GROUP_NAME:
            # [수정: 2026-08-26] 앞의 📂 이모지를 떼고 같은 뜻의 단색 SVG를 붙인다.
            # 저장값(UNCLASSIFIED_GROUP_NAME)은 그대로다 — 그 문자열이 group_order.json·
            # LLM 캐시의 키라, 상수를 고치면 저장된 순서·캐시가 이름을 못 찾는다.
            title_html = (
                f'<span class="subheading-title unclassified-title">{select_all_html}'
                f'{icon(UNCLASSIFIED_ICON)}{UNCLASSIFIED_DISPLAY_TEXT} '
                f'<span class="unclassified-count">{len(group["articles"])}건</span></span>'
            )
            # [추가: 2026-08-12] "🤖 미분류 배정" — 이 칸에 있는 기사만 기존 소제목에
            # 배정한다(신규 소제목 이름·구성은 절대 안 건드림). 미분류를 발견하는 이
            # 자리에서 바로 누르도록 헤더에 둔다(app.settings_server._handle_assign_unclassified,
            # JS assignUnclassified).
            assign_btn_html = (
                '<button class="assign-unclassified-btn" type="button" '
                'onclick="event.stopPropagation(); assignUnclassified(this);" '
                'title="이 기사들을 기존 소제목에 배정합니다 (기존 소제목은 바뀌지 않습니다)">'
                f'{icon("bot")} AI 기사 배정</button>'
            )
        else:
            assign_btn_html = ""
            # [추가: 2026-08-11] AI가 만든 소제목에만 🤖 배지를 붙인다(담당자가 ✏️로 이름을
            # 바꿨거나 직접 만든 소제목, 규칙 기반 폴백에는 안 붙는다 — ai_names 참고).
            # 반드시 이름 **바깥**의 별도 <span>이어야 한다: 이름 문자열에 섞어 넣으면
            # 복사/다운로드/텔레그램·이메일 발송 텍스트까지 배지가 따라 나간다.
            # [수정: 2026-09-01] custom_names에 있는 이름은 ai_names에도 들어 있어도
            # 배지를 안 단다 — 담당자가 「+ 새 소제목」으로 만든 이름을 AI가 우연히
            # 똑같이 지으면(가장 흔한 사례가 "기타": AI가 어디에도 안 맞는 기사를 담는
            # 이름으로 늘 쓴다) 그 소제목 하나가 파란 점선 테두리(=내가 만듦)와 🤖
            # 배지(=AI가 만듦)를 동시에 달아 서로를 부정했다. 담당자가 직접 만들었다는
            # 사실이 더 정확한 출처이므로 그쪽을 남긴다.
            ai_badge = (
                f'<span class="ai-badge" title="AI가 자동으로 묶은 소제목입니다">{icon("bot")}</span>'
                if group["name"] in ai_names
                and group["name"] not in custom_names
                and display_name == original_name
                else ""
            )
            # [추가: 2026-08-12] 메일머지 "소제목 형식" — app.renderer._render_groups와
            # 동일. data-current(rename 프리필)는 가공 전 이름을 그대로 유지한다.
            formatted_name = apply_subheading_format(html.escape(subheading_format), display_name)
            count_html = f'<span class="subheading-count">{len(group["articles"])}</span>'
            title_html = (
                f'<span class="subheading-title">{select_all_html}{formatted_name}{ai_badge}{count_html}</span>'
            )
        # [수정: 2026-08-21] 임시 보관함(📂 소제목 미분류)은 진짜 소제목이 아니라서
        # 이름 바꾸기/순서 이동/폴더 삭제라는 개념 자체가 안 맞는다 — 이름을 바꿔도
        # 내부적으로는 여전히 UNCLASSIFIED_GROUP_NAME이라 스타일·배정 대상이 그대로고,
        # 순서를 옮기면 apply_group_order가 그 자리를 이름으로 저장해 버려 다음 회차의
        # (내용이 전혀 다른) 미분류가 중간에 끼어든다(사용자 지적). 전체선택 체크박스와
        # 🤖 미분류 배정만 남긴다.
        # [추가: 2026-09-01] 이 소제목만 복사 — app.renderer._render_groups와 같은 자리
        # (아이콘 묶음 맨 앞)·같은 동작. [수정: 2026-09-02] 텍스트에 회차 헤더·메모
        # 줄이 없어(소제목 + 기사만) 확정본과 결과가 완전히 같다.
        # AI 분류 폴백 상태여도 확인창을 띄우지 않는다 — 전체 복사(copyPlainText)는
        # 회차 전체가 나가는 동작이라 한 번 더 묻지만, 소제목 하나를 뽑는 가벼운 동작에
        # 매번 확인창이 뜨면 정작 읽어야 할 그 경고를 안 읽게 된다(사용자 결정).
        copy_text = (group_copy_texts or {}).get(group["name"])
        copy_btn_html = (
            f'<button class="rename-btn group-copy-btn" type="button" '
            f'data-copy-text="{html.escape(copy_text)}" onclick="copyArticleIcon(this)" '
            f'title="이 소제목만 복사">'
            f'<span class="icon-default">{icon("copy")}</span>'
            f'<span class="icon-done">{icon("check")}</span></button> '
            if copy_text and group["name"] != UNCLASSIFIED_GROUP_NAME
            else ""
        )
        group_icons = (
            assign_btn_html if group["name"] == UNCLASSIFIED_GROUP_NAME else (
                f'{copy_btn_html}'
                f'<button class="rename-btn" type="button" data-name="{original_name}" '
                f'data-current="{display_name}" onclick="renameGroup(this)" title="소제목 이름 바꾸기">{icon("pencil")}</button> '
                f"{delete_btn}{order_html}"
            )
        )
        heading = (
            f'<h2>{title_html}'
            f'<span class="subheading-icons">{group_icons}</span></h2>'
        )
        if group["name"] == UNCLASSIFIED_GROUP_NAME:
            # [추가: 2026-08-11] 아직 소제목에 반영 안 된 새 기사 묶음 — 임시 상태라는 게
            # 보이도록 사용자 소제목(하늘색)과 다른 색 점선으로 구분한다.
            section_class = "subheading subheading-unclassified"
        elif group["name"] in custom_names:
            section_class = "subheading subheading-custom"
        else:
            section_class = "subheading"
        toc_name = (
            display_name
            if group["name"] == UNCLASSIFIED_GROUP_NAME
            else apply_subheading_format(html.escape(subheading_format), display_name)
        )
        # [추가: 2026-09-10] app.renderer와 동일 — JS가 순서 고정 칸을 알아보는 표식.
        locked_attr = ' data-order-locked="1"' if order_locked else ""
        # [추가: 2026-09-15] 「AI 기사 나누기」(splitSelected)가 읽는 두 값 — 서버로 보낼 원래
        # 이름(data-group, 화면이 본 소제목과 서버가 계산한 소제목이 같은지 대조한다)과, 담당자가
        # 이름을 직접 고쳐 둔 소제목인지(data-renamed — 통째로 나누면 그 이름이 사라지므로 그때만
        # 한 번 묻는다).
        split_attrs = f' data-group="{original_name}"' + (
            ' data-renamed="1"' if display_name != original_name else ""
        )
        sections.append(
            f'<section class="{section_class}" id="subheading-{group_index}" data-toc-name="{toc_name}"'
            f"{locked_attr}{split_attrs}>"
            f"{heading}{body}</section>"
        )
    return "\n".join(sections)


def _render_preview_bottom(
    groups: list, highlight_words: list, labels: dict,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
) -> str:
    # [수정: 2026-09-10] app.renderer._render_bottom과 동일 — "🤖 AI가 추출한 주요
    # 키워드" 블록을 없앴다(배경은 그쪽 주석·HISTORY.md 참고). 요약만 남는다.
    summaries = summarize_groups(groups)
    summary_items = []
    copy_lines = []
    for s in summaries:
        summary_items.append(
            f'<div class="bottom-summary-item">'
            f'<strong>{_format_summary_title(s["name"], labels, subheading_format)}</strong>'
            f'<p>{highlight_keywords(s["summary"], highlight_words)}</p>'
            "</div>"
        )
        raw_title = display_group_name(s["name"], labels)
        if s["name"] != UNCLASSIFIED_GROUP_NAME:
            raw_title = apply_subheading_format(subheading_format, raw_title)
        copy_lines.append(f'{raw_title}\n{s["summary"]}')
    # [수정: 2026-09-10] 확정본(app.renderer._render_bottom)과 같은 규칙 — 맨 위에 제목 한 줄.
    copy_text = "\n\n".join([AI_SUMMARY_HEADING] + copy_lines)
    return (
        '<div class="bottom">'
        '<div class="bottom-summary">'
        '<div class="bottom-header-row">'
        f'<h3>{icon("chat")} AI가 읽은 소제목별 주요 요약</h3>'
        f'<button type="button" class="bottom-summary-copy" onclick="copyAiSummary(this)" '
        f'data-copy-text="{html.escape(copy_text)}">복사</button>'
        "</div>"
        f'{"".join(summary_items)}'
        "</div>"
        "</div>"
    )


def _manual_section_html(
    highlight_words: list,
    line_template: str,
    groups: Optional[list] = None,
    labels: Optional[dict] = None,
    custom_names: Optional[set] = None,
    scrap_date: str = "",
    scrap_end: str = "",
) -> str:
    """"직접 추가한 기사"(app.manual_articles) 구획 — 미리보기 대상 회차가 14:00→17:00처럼
    넘어가도(app.scheduler.next_pending_slot) 이 구획은 영향받지 않는다. manual_articles.json은
    당일 자정에만 비워지는 완전히 별개 저장소라, 어느 회차를 미리보고 있는지와 무관하게
    항상 같은 내용을 보여준다(app.renderer.render_page가 index.html에서 하는 것과 동일).

    초안에 넣으면 아직 저장된 회차가 없어서 즉시 끼워 넣을 곳이 없으므로,
    app.draft_articles의 예약 목록에 담아뒀다가 다음 회차가 실제로 저장될 때
    (app.scraper.collect_run) 합쳐지고, 그 전까지도 이 초안 화면의 소제목 분류에는 바로
    반영된다(_compute_preview_articles).

    [수정: 2026-08-11] 예전엔 "📝 초안에 포함"·"📗 완성본에 포함" 버튼 두 개가 이 화면에도
    똑같이 떠서 app.renderer._render_manual_section을 인자 없이 그대로 재사용할 수 있었다.
    이제 목적지는 보고 있는 화면이 정하고 담당자는 소제목만 고르므로(그 이유는
    app.renderer._manual_promote_control_html 참고), 이 화면 몫인 target="draft"와
    지금 화면의 소제목 목록을 넘겨준다. groups가 없는 호출(회차 종료 안내·검색 오류
    화면)에서는 고를 소제목이 없어 📥 버튼 하나로 떨어진다.
    """
    manual_articles = filter_hidden(load_manual_articles())
    return _render_manual_section(
        manual_articles,
        highlight_words,
        line_template,
        target="draft",
        group_names=_promotable_group_names(groups),
        labels=labels,
        custom_names=custom_names,
        show_label_control=True,
        scrap_date=scrap_date,
        scrap_end=scrap_end,
        add_button_html=URL_ADD_BUTTON_HTML,
        add_panel_html=URL_ADD_PANEL_HTML,
    )


def _build_preview_plain_text(
    slot: dict, groups: list, line_template: str, labels: dict,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    note: Optional[str] = None,
    header: bool = True,
) -> str:
    """초안 화면의 copy/export용 텍스트를 만든다 (app.renderer._build_plain_text와 형식은
    같지만 헤더에 "[초안]"이 붙는다는 것만 다르다).

    [추가: 2026-07-27] 헤더 형식은 완성본과 똑같이 "언론 모니터링 {end} 기준"으로 두고
    맨 앞에 "[초안]"만 덧붙인다 — 처음엔 "지금 OO:OO 현재 기준"까지 같이 넣었는데,
    완성본과 긁는(추출하는) 방식 자체를 통일해달라는 요청에 따라 뺐다. "[초안]" 표시
    하나로도 정식 완성본과는 구분되고, 형식은 완성본과 동일해야 나중에 이 텍스트를
    완성본에 이어 붙이거나 비교할 때도 헷갈리지 않는다.
    """
    # [추가: 2026-09-02] app.renderer._build_plain_text와 동일 — header=False면 회차
    # 헤더 줄과 메모 줄을 빼고 소제목 + 기사만 담는다(소제목별 복사가 쓰는 길).
    lines = []
    if header:
        lines.append(f"[초안] 언론 모니터링 {format_slot_time_kr(slot['end'])} 기준")
        # [추가: 2026-08-05] app.renderer._build_plain_text와 동일 — 직접 작성한 메모가
        # 있으면 헤더 바로 아래 한 줄로 끼워 넣는다.
        if note is None:
            note = load_manual_keyword_note()
        if note:
            lines.append(f"- {note}")
        lines.append("")
    if not groups:
        lines.append("수집된 기사 없음")
    else:
        for group in groups:
            section_name = display_group_name(group['name'], labels)
            if group["name"] != UNCLASSIFIED_GROUP_NAME:
                section_name = apply_subheading_format(subheading_format, section_name)
            lines.append(section_name)
            for article in group["articles"]:
                lines.append(apply_line_template(line_template, article["outlet"], article["title"]))
                lines.append(article["url"])
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_preview_group_copy_texts(
    slot: dict, groups: list, line_template: str, labels: dict,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
) -> dict:
    """소제목 하나씩만 담은 초안 복사 텍스트 {소제목 이름: 텍스트}
    (app.renderer.build_group_copy_texts의 초안판).

    [수정: 2026-09-02] 정기 확정본과 같이 회차 헤더("[초안] 언론 모니터링 N시 기준")와
    메모 줄을 빼고 소제목 + 그 안의 기사만 담는다(header=False, 사용자 요청).

    📂 소제목 미분류는 대기실이라 애초에 버튼을 안 그리지만, 여기서도 빼둔다 —
    그 이름("📂 소제목 미분류")은 보고 텍스트에 절대 나가면 안 되는 화면 전용
    표시라서다(_merge_unclassified와 같은 이유, CODING_CONVENTIONS.md §4).
    """
    return {
        group["name"]: _build_preview_plain_text(
            slot, [group], line_template, labels, subheading_format, header=False,
        )
        for group in groups
        if group["articles"] and group["name"] != UNCLASSIFIED_GROUP_NAME
    }


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


# [추가: 2026-08-11] 이미 자동 소제목 분류를 시도한 회차는 app.auto_classify_turn이
# 파일로 기억한다 — 초안에서 자동 분류는 회차당 딱 한 번만 하고, 그 뒤로는 담당자가
# 버튼을 눌러야 다시 분류한다. 자동 재분류가 담당자의 수동 정리(기사 이동·이름 변경·
# 순서)를 소리 없이 날려버리기 때문이다 — 자세한 이유는 app/llm_classifier.py 참고.
# [수정: 2026-09-03] 그 기록이 이 파일의 모듈 전역 set에만 있던 것을 파일로 옮겼다
# (그때 주석에 "재시작하면 한 번 더 분류될 수 있다(무해)"라고 적어뒀는데 무해하지
# 않았다 — app/auto_classify_turn.py 머리말 참고).


def _merge_unclassified(groups: list) -> list:
    """화면 전용 임시 칸(UNCLASSIFIED_GROUP_NAME)을 "기타"로 합친 목록을 돌려준다.

    복사/다운로드/전송 텍스트를 만들 때만 쓴다 — 그 이름은 "아직 재분류 안 했다"는
    화면 상태 표시일 뿐이라 보고 문서에 나가면 안 되지만, 그 안의 기사는 빠뜨리면 안 된다.
    """
    if not any(g["name"] == UNCLASSIFIED_GROUP_NAME for g in groups):
        return groups
    # 원본 dict를 건드리지 않는다 — 같은 dict를 화면 렌더링(render_groups)과 공유하고
    # 있어서, 여기서 articles를 바꾸면 호출 순서에 따라 화면까지 조용히 달라진다.
    leftover = [a for g in groups if g["name"] == UNCLASSIFIED_GROUP_NAME for a in g["articles"]]
    merged = [
        {**g, "articles": g["articles"] + leftover} if g["name"] == "기타" else dict(g)
        for g in groups
        if g["name"] != UNCLASSIFIED_GROUP_NAME
    ]
    if not any(g["name"] == "기타" for g in merged):
        merged.append({"name": "기타", "articles": leftover})
    return merged


def _round_deadline_ms(slot_end: Optional[str]) -> int:
    """이번 회차 종료 시각(오늘 KST)을 밀리초 타임스탬프로 — round-countdown JS가 쓴다.

    slot_end가 없으면(오늘 남은 회차가 없는 안내 화면) 0을 돌려줘 JS가 카운트다운을
    표시하지 않게 한다(`if (ROUND_DEADLINE_MS)` 가드).
    """
    if not slot_end:
        return 0
    return int(kst_today_at(slot_end).timestamp() * 1000)


def _undo_fab_html() -> str:
    """↩ 되돌리기 버튼 — 되돌릴 게 있을 때만 그린다(app/undo.py, 확정본과 동일).

    항상 떠 있으면 "지금 이걸 누르면 뭐가 되돌아가는지" 알 수 없어 오히려 누르기
    불안하다는 판단으로, 스택이 비어 있으면 아무것도 렌더링하지 않는다.
    """
    label = undo_peek_label()
    if not label:
        return ""
    return (
        f'<button type="button" class="undo-fab" onclick="undoLastAction(this)" '
        f'title="{html.escape(label)} 되돌리기">{icon("undo")}</button>'
    )


def _reclassify_btn_ref_html() -> str:
    """배너 문구 안에서 툴바의 "전체 기사 재분류" 버튼을 가리키는 인라인 칩.

    [추가: 2026-08-20] 처음엔 배너 자체에도 같은 버튼을 하나 더 뒀는데, 실측해보니
    툴바 버튼이 배너에서 90~120px밖에 안 떨어져 있어(스크롤 없이 한눈에 보임) 굳이
    또 둘 이유가 없었다 — 같은 라벨의 버튼이 화면에 둘이면 "다른 건가?"라는 새 의문만
    생긴다(사용자 지적). 대신 문구가 실제 버튼과 같은 색·아이콘의 칩으로 그 버튼을
    가리킨다.
    """
    return f'<span class="btn-ref">{icon("refresh")} AI 모든 기사 재분류</span>'


def _retry_log_html(round_id: Optional[tuple]) -> str:
    """이 회차에서 담당자가 직접 누른 재분류 시도 이력을 한 줄로 만든다.

    [추가: 2026-08-20] "재분류마저 실패" 배너에서만 붙는다 — 몇 번 시도했고 각각
    성공/실패했는지가 없으면 담당자가 "한 번 더 해볼까"를 판단할 근거가 없다
    (app.reclassify_attempts). 최근 4건만 보여준다 — 그 이상은 화면을 어지럽힐 뿐
    "계속 실패하고 있다"는 결론에 새 정보를 더하지 않는다.
    """
    attempts = reclassify_recent_attempts(round_id)[-4:]
    if not attempts:
        return ""
    parts = []
    for i, (when, success) in enumerate(attempts):
        verb = "재분류 시도" if i == 0 else "재시도"
        outcome = "성공" if success else "실패 (AI 응답 없음)"
        parts.append(f'{when.strftime("%H:%M:%S")} {verb} → {outcome}')
    return '<span class="retry-log">↻ ' + " · ".join(parts) + "</span>"


def _classify_degraded_html(degraded: bool, article_count: int, round_id: Optional[tuple]) -> str:
    """AI 소제목 분류가 규칙 기반으로 떨어졌을 때 기사 목록 맨 위에 띄우는 경고.

    [추가: 2026-08-20] 이 폴백은 설계상 "조용히" 일어난다 — app.llm_classifier의 모든
    실패 경로가 예외 대신 None을 돌려주고 app.classifier가 단어 빈도 분류로 이어받기
    때문이다(앱이 절대 멈추지 않게 하려는 원래 의도는 그대로 옳다). 문제는 그 결과가
    화면에서 정상과 구분되지 않는다는 것이었다: 담당자가 09:30 회차 초안에서 소제목이
    <부총리>·<비상경제본부>처럼 뭉개진 걸 눈으로 발견하고서야 알았다.

    [수정: 2026-08-20] 원인에 따라 문구를 넷으로 나눈다 — 원인이 다르면 담당자가 할 일도
    다른데, 하나의 문구·버튼으로 뭉뚱그리면 "눌러도 되는지"조차 판단할 수 없다:
      1) 기사 수 초과(MAX_ARTICLES_FOR_CLASSIFY) — 재분류 버튼을 눌러도 기사 수를
         줄이지 않는 한 매번 똑같이 실패한다. 몇 건을 줄여야 하는지 숫자로 알려준다.
      2) 크레딧 부족(anthropic.BadRequestError) — 역시 재분류 버튼으로는 해결이 안
         된다. 확인할 곳(설정 → AI 연동)을 짚어준다.
      3) 이 회차에서 담당자가 이미 2회 연속 재분류를 시도했는데도 실패 — 세 번째
         시도를 권하는 대신(마감이 코앞일 수 있다) 손으로 정리하는 우회로를 제시한다.
      4) 그 외 일반 폴백 — 버튼을 한 번 눌러보라고 안내한다.
    우선순위가 1 → 2 → 3 → 4인 이유: 기사 수 초과·크레딧 부족(1·2번)은 재분류를 몇 번
    눌러도 원인이 안 없어져, 3번 조건(연속 실패 2회)에도 걸리지만 "손으로 고치라"는
    조언만으로는 부족하다 — 무엇을 확인해야 다시 되는지(기사 수를 줄인다 / 크레딧을
    충전한다)가 따로 있기 때문이다. [수정: 2026-08-20] 문구 자체는 preview.html·
    index.html이 같은 첫 줄("AI가 소제목을 묶지 못했어요." + "AI가 지은 이름이 아니라
    단어 빈도수에 따라 기계적으로 뽑은 단어")을 쓰도록 통일했다 — 예전엔 "임시 이름
    (단어 빈도수)"라고만 써서 담당자가 "단어 빈도수"가 뭘 뜻하는지 알 수 없었다.
    """
    if not degraded:
        return ""

    intro = "<b>⚠️ AI가 소제목을 묶지 못했어요.</b> 지금 보이는 소제목은 AI가 지은 이름이 아니라, 단어 빈도수에 따라 기계적으로 뽑은 단어입니다."

    if llm_last_skipped_too_many_articles():
        over = article_count - MAX_ARTICLES_FOR_CLASSIFY
        return (
            '<div class="classify-degraded"><div class="msg">'
            f"<b>⚠️ 지금 {article_count}건이라 AI가 소제목을 묶지 못했어요.</b> "
            f"(상한 {MAX_ARTICLES_FOR_CLASSIFY}건) 지금 보이는 소제목은 AI가 지은 이름이 아니라, "
            "단어 빈도수에 따라 기계적으로 뽑은 단어입니다.<br>"
            f"<b>{over}건</b>을 줄이고 위의 {_reclassify_btn_ref_html()}를 다시 눌러 요청하시면, "
            "기사들이 다시 묶입니다."
            "</div></div>"
        )

    # [추가: 2026-08-20] 크레딧 부족(anthropic.BadRequestError)도 기사 수 초과와 같은
    # 성질이다 — 재분류를 다시 눌러도 충전 전까진 매번 똑같이 실패한다. 그래서 연속
    # 실패 판정(3번)보다 먼저 확인하고, "다시 눌러보라"는 조언 대신 확인할 곳을 짚는다.
    if llm_last_credit_error():
        return (
            '<div class="classify-degraded"><div class="msg">'
            f"{intro}<br>"
            "<b>AI 사용 크레딧이 부족합니다.</b> 설정 → LLM(AI) 연동에서 결제 수단·"
            "남은 크레딧을 확인해주세요. 충전 전까지는 다시 눌러도 같은 결과라, "
            "이번 회차는 소제목 이름을 직접 수기로 고쳐주세요."
            "</div></div>"
        )

    if reclassify_consecutive_failures(round_id) >= 2:
        return (
            '<div class="classify-degraded"><div class="msg">'
            f"{intro}<br>"
            "이번 회차는 소제목 이름을 직접 수기로 고치길 추천합니다."
            f"{_retry_log_html(round_id)}"
            "</div></div>"
        )

    return (
        '<div class="classify-degraded"><div class="msg">'
        f"{intro}<br>"
        f"위의 {_reclassify_btn_ref_html()}를 눌러 다시 요청해주세요."
        "</div></div>"
    )


def _take_auto_classify_turn(slot: dict, article_count: int) -> bool:
    """이 회차의 "자동 분류 1회" 기회가 아직 남아 있으면 소비하고 True를 돌려준다.

    기사가 MIN_ARTICLES_FOR_AUTO 미만이면 기회를 쓰지 않고 그냥 False — 회차 시작
    직후 기사 2~3건으로 소제목을 나눠봐야 의미가 없고, 어차피 나중에 다시 만들어야 해서
    호출만 버리기 때문이다. 기사가 그만큼 쌓인 뒤 처음 열릴 때 기회를 쓴다.

    [수정: 2026-09-03] 소진 기록을 파일(app.auto_classify_turn)로 옮겼다 — 앱을 재시작하면
    기회가 되살아나 전체 재분류가 한 번 더 돌던 문제.
    """
    if article_count < MIN_ARTICLES_FOR_AUTO:
        return False
    return take_auto_classify_turn(slot["end"])


def _actions_html(
    plain_text: str, slot_end: str, pending_count: int = 0, manual_edits: int = 0,
    degraded: bool = False, photo_suspect_count: int = 0,
    export_rows: Optional[list] = None, export_date: str = "",
) -> str:
    # 파일명은 확정본(`날짜_언론모니터링_14-00기준`)과 같은 모양에 끝만 `_초안`을 붙여 구별한다.
    export_base = f"{export_date}_언론모니터링_{slot_end.replace(':', '-')}기준_초안"
    export_filename = f"{export_base}.txt"
    export_excel_filename = f"{export_base}.xlsx"
    # [수정: 2026-08-10] app.renderer와 동일 — 메모가 있으면 패널이 이미 열려있고 그 안에
    # 자체 (수정) 버튼이 있어, 툴바 버튼은 메모가 없을 때(패널을 열 방법이 필요한 경우)만
    # 보여준다.
    keyword_note_btn_html = (
        ""
        if load_manual_keyword_note()
        else '<button class="create-group-btn" type="button" onclick="toggleKeywordNote(this)"><span class="plus-glyph">+</span>한 줄 메모</button>'
    )
    # [수정: 2026-08-12] "🤖 미분류 기사 분류"(증분 배정)는 📂 소제목 미분류 칸의 헤더로
    # 옮겼다(_render_preview_groups 참고) — 미분류를 발견하는 자리에서 바로 누르도록,
    # 그리고 0건이면 그 칸 자체가 안 뜨니 버튼도 자연히 사라진다. 대신 여기 툴바엔
    # "🤖 전체 기사 재분류"를 새로 둔다 — 회차 초반 몇 건으로 잡은 분류가 기사가 쌓이며
    # 안 맞게 될 때, 확정본까지 기다리지 않고 통째로 다시 묶을 수단(사용자 요청).
    # 파괴적 동작(기존 소제목 이름·구성·순서가 전부 새로 지어짐)이라 확정본의 "🤖 전체
    # 기사 재분류"와 같은 확인창을 쓴다.
    # [수정: 2026-08-20] data-degraded — 지금 이 회차가 규칙 기반 폴백 상태인지를 버튼에
    # 실어 보낸다. 폴백 상태에서는 "옮겨둔 기사·이름·순서가 초기화됩니다"라는 기존 확인
    # 문구가 거짓에 가깝다 — 이름은 이미 단어 빈도로 뭉개졌고, 옮겨둔 배치도 소제목
    # 이름이 안 맞아 이미 무효이기 때문이다(regenerateSubheadings JS가 이 값으로
    # 확인창 문구를 분기한다).
    reclassify_btn_html = (
        f'<button class="create-group-btn reclassify-btn" type="button" data-manual-edits="{manual_edits}" '
        f'data-degraded="{"1" if degraded else "0"}" '
        'onclick="regenerateSubheadings(this)" '
        f'title="전체 기사를 처음부터 다시 분류합니다">{icon("refresh")} AI 모든 기사 재분류</button>'
    )
    # 「📷 사진 추정 (N)」 — app.renderer와 같은 함수(0건이면 안 그린다). 화면만 바꾸는
    # 보기라 왼쪽(내용을 바꾸는 네모)이 아니라 오른쪽 묶음에 알약으로 선다.
    photo_select_btn_html = photo_gather_button_html(photo_suspect_count)
    # 툴바 규칙: 왼쪽 = 초안을 바꾸는 박스 버튼, 오른쪽 = 가져가거나 보기만 바꾸는 것
    # (글자 복사·텍스트·엑셀 + 보기 선택). 확정본(app.renderer)과 같은 모양이고 CSS는
    # export_links_style() 공용. 시안 mockups/TOOLBAR_GROUPING_MOCKUP.html A안.
    # 폴백 상태면 복사·텍스트·엑셀 모두 제출 전에 한 번 더 묻는다 — false를 돌려주면
    # <form> 제출 자체가 취소된다.
    return (
        '<button class="create-group-btn" type="button" onclick="createCustomGroup()"><span class="plus-glyph">+</span>새 소제목</button>'
        f'{keyword_note_btn_html}'
        '<span class="actions-divider"></span>'
        f"{reclassify_btn_html}"
        '<span class="actions-right">'
        f'{photo_select_btn_html}'
        '<span class="export-links">'
        '<button type="button" onclick="copyPlainText()" title="보고서 텍스트를 클립보드에 복사">복사</button>'
        '<form method="POST" action="/download-text" style="display:contents" '
        'onsubmit="if (CLASSIFY_DEGRADED && !confirm(classifyDegradedConfirmMsg(\'내려받을까요\'))) return false; '
        'this.text.value=PLAIN_TEXT;">'
        f'<input type="hidden" name="filename" value="{html.escape(export_filename)}">'
        '<input type="hidden" name="text" value="">'
        '<button type="submit" title="txt 파일로 받기">텍스트</button>'
        '</form>'
        '<form method="POST" action="/download-excel" style="display:contents" '
        'onsubmit="if (CLASSIFY_DEGRADED && !confirm(classifyDegradedConfirmMsg(\'내려받을까요\'))) return false;">'
        f'<input type="hidden" name="filename" value="{html.escape(export_excel_filename)}">'
        f'<input type="hidden" name="rows" value="{html.escape(json.dumps(export_rows or [], ensure_ascii=False))}">'
        '<button type="submit" title="엑셀 파일로 받기">엑셀</button>'
        '</form>'
        '</span>'
        '<select class="view-mode-select" onchange="applyViewMode(this.value)" '
        'title="소제목 구성은 그대로 두고 화면에 나열하는 순서만 바꿉니다">'
        '<option value="subheading" selected>소제목 내 언론사순</option>'
        '<option value="group-time">소제목 내 시간순</option>'
        '<option value="time">시간순</option>'
        '<option value="outlet">언론사순</option>'
        "</select>"
        f"{refresh_link_html('preview')}"
        "</span>"
    )


def _compute_preview_content(
    slot: dict, articles: list, keywords: list, highlight_words: list, line_template: str, generated_at: str,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
) -> dict:
    """render_preview_page와 preview_move_article이 공유하는 실제 렌더링 계산 —
    소제목 분류·본문 HTML·안내문구·복사용 텍스트·상단 액션 버튼을 만든다.

    [추가: 2026-07-30] preview_move_article이 ↑/↓ 이동 직후 이 결과를 그대로
    JSON으로 돌려줄 수 있도록 render_preview_page에서 분리했다 — 페이지 전체를
    다시 감싸는 부분(_PAGE_TEMPLATE.format)만 render_preview_page에 남기고,
    실제로 매번 다시 계산해야 하는 내용은 여기 한 곳에만 있다.
    """
    # [추가: 2026-08-14] round_id_for_slot(slot) — "오늘, 이 회차"의 식별자. 초안이
    # 새로고침될 때마다 다시 계산되지만 round_id는 회차가 안 바뀌는 한 그대로라, 이
    # 값을 이름표·순서·분류 호출 전부에 넘겨 회차를 넘어 잘못 새거나 재사용하지
    # 않게 한다(app.llm_classifier._find_reusable/_find_partial/_last_names,
    # app.curation.load_group_labels, app.group_order.apply_group_order 참고).
    round_id = round_id_for_slot(slot)
    labels = load_group_labels(round_id)
    # [수정: 2026-08-26] custom_names는 아래에서도 "담당자가 직접 만든 소제목"이라는 순수한
    # 뜻으로 계속 쓰인다(굵은 글씨·점선 테두리·빈 채로도 유지 — 전부 이 이름 그대로일 때만
    # 맞는 규칙). classify_articles에 넘길 목적지 이름만 forced_group_target_names()로 따로
    # 넓힌다 — "🤖 미분류 배정"이 새로 지어낸 소제목(app.assigned_groups)도 포함해야, 그
    # 이름으로의 override가 다음 렌더링에서 조용히 무시되지 않는다.
    custom_names = load_custom_groups()
    overrides = load_group_overrides()
    groups = (
        classify_articles(
            articles,
            keywords,
            forced_groups=overrides,
            custom_group_names=forced_group_target_names(),
            allow_llm_call=_take_auto_classify_turn(slot, len(articles)),
            round_id=round_id,
        )
        if articles
        else []
    )
    # [추가: 2026-08-20] 분류 **직후**에 읽어야 유효한 값이다(다음 호출이 덮어쓴다).
    # 회차 초반(기사가 MIN_ARTICLES_FOR_AUTO 미만이라 아직 한 번도 시도조차 안 한 상태)은
    # 폴백이 아니라 의도된 대기 상태라 경고하지 않는다 — 그때 경고하면 매 회차 첫 몇 분마다
    # 무의미하게 떠서, 정작 진짜 폴백이 났을 때 담당자가 무시하게 된다.
    classify_degraded = (
        bool(articles)
        and len(articles) >= MIN_ARTICLES_FOR_AUTO
        and llm_last_classification_was_rule_based()
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
    render_groups = apply_group_order(groups + empty_custom_groups, round_id)
    groups = [g for g in render_groups if g["articles"]]
    total_article_count = sum(len(g["articles"]) for g in groups)
    # [추가: 2026-08-04] app.renderer.render_page와 동일 — "언론사순" 보기용 순위.
    rank_by_url = {a["url"]: i for i, a in enumerate(articles)}
    # [추가: 2026-08-11] 이 화면의 소제목 중 어떤 게 AI가 만든 것인지 — 🤖 배지 판단용.
    # 캐시만 읽으므로 API를 부르지 않고, 규칙 기반으로 폴백한 경우엔 빈 집합이 온다.
    # [수정: 2026-09-15] 「AI 기사 배정」·「AI 기사 나누기」가 새로 지은 이름(app.assigned_groups)도
    # 넣는다 — 캐시가 아니라 배정 기록으로 만들어지는 소제목이라 캐시만 보면 🤖 배지가 빠져,
    # AI가 지은 이름인데 담당자가 만든 것처럼 보였다. 배지 조건(직접 만든 이름·이름표로 고친
    # 이름은 제외)은 그대로다.
    ai_names = llm_cached_group_names(articles, round_id=round_id) | set(load_assigned_groups())
    # [추가: 2026-08-18] 라벨 스냅샷용 — 초안은 항상 오늘 날짜, scrap_end는 이 회차의
    # 종료 시각(slot["end"]).
    label_scrap_date = datetime.now().strftime("%Y-%m-%d")
    label_scrap_end = slot.get("end", "")

    # [추가: 2026-08-20] 폴백 상태에서 복사·다운로드를 누를 때 보여줄 소제목 이름 목록
    # ("<부총리>, <비상경제본부>, …" — _handle export confirm 참고). degraded일 때는
    # groups 전체가 규칙 기반이므로(부분 성공은 last_classification_was_rule_based가
    # False다) 예외 없이 groups 전부를 그대로 쓴다.
    degraded_names = (
        [
            apply_subheading_format(subheading_format, display_group_name(g["name"], labels))
            for g in groups
        ]
        if classify_degraded
        else []
    )

    if not render_groups:
        body = '<div class="empty">💤<br>아직 모인 기사가 없어요</div>'
    else:
        body = _classify_degraded_html(classify_degraded, len(articles), round_id) + _render_preview_groups(
            render_groups, highlight_words, line_template, labels, rank_by_url, set(custom_names), ai_names,
            subheading_format,
            scrap_date=label_scrap_date, scrap_end=label_scrap_end,
            group_copy_texts=_build_preview_group_copy_texts(
                slot, render_groups, line_template, labels, subheading_format,
            ),
        )
        if groups:
            body += _render_preview_bottom(groups, highlight_words, labels, subheading_format)
    body += _manual_section_html(
        highlight_words, line_template, render_groups, labels, set(custom_names),
        scrap_date=label_scrap_date, scrap_end=label_scrap_end,
    )
    # [삭제: 2026-08-11] "아직 정식 회차가 아니에요 — HH:MM에 자동으로 정식 수집됩니다"
    # 안내문을 없앴다 — 같은 정보(회차 종료까지 남은 시간)를 위 round-countdown이 더
    # 짧게 이미 보여주고 있어 중복이라는 사용자 판단(사용자 요청). 빈 문자열이면
    # `.preview-hint:empty`가 박스를 통째로 숨겨 여백도 안 남는다 — 오늘 회차가 모두
    # 끝났을 때(render_preview_done_page)·검색 오류일 때(render_preview_error_page)의
    # 안내문은 이 카운트다운과 무관한 별개 정보라 그대로 남겨둔다.
    hint = ""
    # [수정: 2026-08-11] 복사/다운로드 텍스트에는 "🆕 아직 소제목 안에 분류되지 않은 기사"라는
    # 임시 칸 이름이 절대 나가면 안 된다 — 화면에서만 쓰는 상태 표시인데 그대로 보고
    # 문서에 찍히기 때문이다(실제로 찍히는 걸 확인하고 고쳤다). 기사 자체는 빠뜨리면
    # 안 되므로 "기타"로 합쳐서 내보낸다(이미 "기타"가 있으면 그 뒤에 붙인다).
    plain_text = _build_preview_plain_text(
        slot, _merge_unclassified(groups), line_template, labels, subheading_format
    )
    # 버튼 배지용 — 아직 소제목에 반영 안 된 새 기사 수, 그리고 재분류로 날아갈 수 있는
    # 수동 이동 건수(지금 화면에 있는 기사에 한해서만 센다).
    pending_count = sum(
        len(g["articles"]) for g in render_groups if g["name"] == UNCLASSIFIED_GROUP_NAME
    )
    shown_urls = {a["url"] for a in articles}
    # [수정: 2026-09-01] 지금 화면에 있는 배정 기록을 전부 세지 않고, **재분류로 실제로
    # 흩어질 수 있는 것만** 센다. 배정 목적지가 담당자가 직접 만든 소제목이거나
    # 「AI 기사 배정」이 지어낸 소제목이면(둘 다 forced_group_target_names) 재분류
    # 결과에 그 이름이 없어도 app.classifier._apply_forced_groups가 빈 소제목으로 미리
    # 끼워 넣어 주므로 기사는 반드시 제자리로 돌아온다 — 그건 경고할 일이 아니다.
    # 흩어질 수 있는 건 "그 회차 전체 분류 때 AI가 지은 이름"으로 옮긴 경우뿐이다
    # (이름 자체가 사라지면 배정이 조용히 무시된다). 실측(2026-09-01, 오늘 쌓인 배정
    # 기록 62건): 보호 31건 / 흩어짐 31건으로 반반이라, 예전처럼 뭉뚱그려 세면 숫자가
    # 늘 부풀어 정작 진짜 위험한 날에도 담당자가 그 숫자를 안 믿게 된다.
    # 0이면 화면 JS가 그 문장 자체를 뺀다(줄의 있고 없음이 곧 신호 — "0건"이라 적으면
    # 그 신호가 죽는다. 📂 미분류 배지·📷 사진 추정 버튼과 같은 원칙).
    protected_group_names = set(forced_group_target_names())
    manual_edits = sum(
        1
        for url, target_name in overrides.items()
        if url in shown_urls and target_name not in protected_group_names
    )
    photo_suspect_count = sum(1 for a in articles if looks_like_photo_caption(a))
    return {
        "body": body,
        "hint": hint,
        "plain_text": plain_text,
        "actions_html": _actions_html(
            plain_text, slot["end"], pending_count, manual_edits, classify_degraded,
            photo_suspect_count,
            export_rows=build_export_rows(groups, label_scrap_date, label_scrap_end),
            export_date=label_scrap_date,
        ),
        # [추가: 2026-08-20] previewMoveArticle(JS)이 DOM 조각과 함께 이 값도 받아
        # CLASSIFY_DEGRADED/CLASSIFY_DEGRADED_NAMES를 갱신한다 — ↑/↓ 이동 한 번마다
        # 분류가 다시 계산되므로(위 classify_degraded), 복사·다운로드 확인창이 이동
        # 직후에도 최신 상태를 보게 하려면 페이지 전체를 새로고침하지 않는 이 경로도
        # 같은 값을 들고 있어야 한다.
        "classify_degraded": classify_degraded,
        "classify_degraded_names": degraded_names,
        # [추가: 2026-08-21] 건수 배지 옆에 "미분류 N건"을 같이 띄운다 — 📂 소제목
        # 미분류 칸은 늘 화면 맨 아래라 스크롤을 끝까지 내리기 전엔 있는 줄도 모른다는
        # 사용자 지적. 숫자만 알려주면 결국 또 내려가야 하므로 눌러서 그 칸으로 바로
        # 뛰는 버튼으로 만들었다. 0건이면 안 그린다.
        #
        # 이 값은 h1 안(=#preview-body 바깥)에 들어가 previewMoveArticle의 DOM 교체
        # 대상이 아니지만, 그 경로는 소제목 **안에서의** ↑/↓ 이동뿐이라 미분류 건수를
        # 바꿀 수 없다. 건수가 실제로 바뀌는 동작(미분류 배정·숨김·다른 소제목으로
        # 이동·재분류)은 전부 location.reload()라 새로 렌더링된다.
        "total_count_html": (
            (
                f'<span class="total-count-badge" title="이 회차 기사 {total_article_count}건 — 📂 소제목 미분류 포함, 📌 담아둔 기사 제외">{total_article_count}건</span>'
                if total_article_count
                else ""
            )
            + (
                '<button type="button" class="pending-count-badge" '
                'onclick="scrollToUnclassified()" '
                f'title="소제목 미분류 {pending_count}건 — 눌러서 그 칸으로 이동합니다" '
                f'aria-label="소제목 미분류 {pending_count}건">'
                f'{icon(UNCLASSIFIED_ICON)}{pending_count}</button>'
                if pending_count
                else ""
            )
            + pinned_jump_badge_html(len(filter_hidden(load_manual_articles())))
        ),
    }


def render_preview_page(
    slot: dict, articles: list, keywords: list, highlight_words: list, line_template: str, generated_at: str,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
) -> str:
    content = _compute_preview_content(
        slot, articles, keywords, highlight_words, line_template, generated_at, subheading_format
    )
    return _PAGE_TEMPLATE.format(
        **_theme(),
        undo_fab_html=_undo_fab_html(),
        round_deadline_ms=_round_deadline_ms(slot["end"]),
        **_draft_heading(slot["end"], generated_at, slot),
        total_count_html=content["total_count_html"],
        hint=content["hint"],
        body=content["body"],
        actions_html=content["actions_html"],
        # [수정: 2026-08-26] 메모 칸 변수는 확정본과 공용 함수 하나에서 만든다
        # (app.renderer.keyword_note_template_vars).
        **keyword_note_template_vars(),
        plain_text_json=json.dumps(content["plain_text"], ensure_ascii=False).replace("</", "<\\/"),
        classify_degraded_json=json.dumps(content["classify_degraded"]),
        classify_degraded_names_json=json.dumps(content["classify_degraded_names"], ensure_ascii=False),
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
    return _PAGE_TEMPLATE.format(
        **_theme(),
        undo_fab_html=_undo_fab_html(),
        round_deadline_ms=0,
        **_draft_heading(None),
        total_count_html="",
        hint="⏰ 오늘 예정된 회차가 모두 끝났어요.",
        body=body,
        actions_html="",
        # [수정: 2026-08-26] 메모 칸 변수는 확정본과 공용 함수 하나에서 만든다
        # (app.renderer.keyword_note_template_vars).
        **keyword_note_template_vars(),
        plain_text_json='""',
        classify_degraded_json="false",
        classify_degraded_names_json="[]",
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
    return _PAGE_TEMPLATE.format(
        **_theme(),
        undo_fab_html=_undo_fab_html(),
        round_deadline_ms=_round_deadline_ms(slot["end"]),
        **_draft_heading(slot["end"]),
        total_count_html="",
        hint="⏰ 네이버 검색 중 오류가 발생했습니다.",
        body=body,
        actions_html="",
        # [수정: 2026-08-26] 메모 칸 변수는 확정본과 공용 함수 하나에서 만든다
        # (app.renderer.keyword_note_template_vars).
        **keyword_note_template_vars(),
        plain_text_json='""',
        classify_degraded_json="false",
        classify_degraded_names_json="[]",
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

    [추가: 2026-08-10] 확정·전송 2단계 유예 도입으로, 방금 수집됐지만 아직 확정 안 된
    회차가 있으면 이 화면이 "확정 대기" 회차를 대신 보여주는 분기가 있었다.
    [삭제: 2026-08-11] 그 분기를 없앴다 — 회차는 이제 수집되는 즉시 확정되므로
    (main._scrape_and_render) 확정 대기 상태 자체가 생기지 않는다. 이 화면은 항상
    "다음 회차 미리보기"만 보여준다(이용자가 "회차 시각이 지났는데 왜 다음 회차로 안
    넘어가지?" 하고 혼란스러워하던 원인이었다).
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
        settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE),
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
    new_articles, new_override = move_article(
        articles, keywords, url, direction, overrides, round_id=round_id_for_slot(slot)
    )
    # [수정: 2026-08-24] 이 액션도 undo_push 대상이어야 한다(app.undo.py 모듈 docstring이
    # 원래도 "순서변경"을 포함한다고 적어뒀는데 실제로는 안 쌓이고 있었다 — 확정본 쪽과
    # 같은 사고). preview_order.json은 이미 app.undo._SNAPSHOT_FILES에 있으므로 여기는
    # push 호출만 추가하면 된다. 맨 위/아래라 더 옮길 곳이 없는 클릭(articles 그대로)은
    # 쌓지 않는다.
    if new_articles is not articles or new_override is not None:
        undo_push("기사 순서 변경", touched=[url])
    if new_override is not None:
        set_group_override(*new_override)
        final_articles = articles
    else:
        save_preview_order([a["url"] for a in new_articles])
        final_articles = new_articles

    highlight_words = settings.get("highlight_keywords", [])
    line_template = settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)
    subheading_format = settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE)
    generated_at = datetime.now().strftime("%H:%M")
    content = _compute_preview_content(
        slot, final_articles, keywords, highlight_words, line_template, generated_at, subheading_format
    )

    full_html = _PAGE_TEMPLATE.format(
        **_theme(),
        undo_fab_html=_undo_fab_html(),
        round_deadline_ms=_round_deadline_ms(slot["end"]),
        **_draft_heading(slot["end"], generated_at, slot),
        total_count_html=content["total_count_html"],
        hint=content["hint"],
        body=content["body"],
        actions_html=content["actions_html"],
        # [수정: 2026-08-26] 메모 칸 변수는 확정본과 공용 함수 하나에서 만든다
        # (app.renderer.keyword_note_template_vars).
        **keyword_note_template_vars(),
        plain_text_json=json.dumps(content["plain_text"], ensure_ascii=False).replace("</", "<\\/"),
        classify_degraded_json=json.dumps(content["classify_degraded"]),
        classify_degraded_names_json=json.dumps(content["classify_degraded_names"], ensure_ascii=False),
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
    new_articles = bulk_move_articles(
        articles, keywords, urls, direction, overrides, round_id=round_id_for_slot(slot)
    )
    if new_articles is articles:
        return False
    # [수정: 2026-08-24] preview_move_article과 같은 이유 — 같은 라벨("기사 순서
    # 변경")을 써서 단건·일괄 이동을 연달아 눌러도 되돌리기 한 번으로 합쳐지게 한다.
    undo_push("기사 순서 변경", touched=urls)
    save_preview_order([a["url"] for a in new_articles])
    generate_preview_page()
    return True
