# Design Ref: archive/DESIGN.md §1 화면 구성 — 최신 회차를 밝은 카드형 정적 HTML 화면으로 렌더링
import html
import json
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.topnav import regular_nav, topnav_style
from app.classifier import (
    classify_articles,
    forced_group_target_names,
    groups_for_confirmed_run,
    run_looks_rule_based,
)
from app.config import (
    COLOR_ACCENT,
    COLOR_ACCENT_BORDER,
    COLOR_SCREEN_TAG_REG_BG,
    COLOR_SCREEN_TAG_REG_BORDER,
    COLOR_SCREEN_TAG_REG_TEXT,
    COLOR_SCREEN_TAG_WORK_BG,
    COLOR_SCREEN_TAG_WORK_BORDER,
    COLOR_SCREEN_TAG_WORK_TEXT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_HOVER,
    COLOR_TEXT,
    CUTE_FONT_BASE64,
    CUTE_FONT_NAME,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_HIGHLIGHT_KEYWORDS,
    DEFAULT_KEYWORDS,
    DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    FONT_STACK,
    HIDDEN_VIEW_DAYS,
    HIGHLIGHT_COLORS,
    MAX_SUBHEADINGS,
    OUTPUT_HTML_PATH,
    PALETTE,
    RECLASSIFY_SAFE_NOTE,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.assigned_groups import load_assigned_groups
from app.atomic_write import atomic_write_text
from app.group_split import MIN_SPLIT_ARTICLES
from app.credentials import naver_is_configured
from app.curation import (
    display_group_name,
    filter_hidden,
    load_group_labels,
    load_group_overrides,
    load_hidden_batches,
)
from app.summary_overrides import apply_summary_overrides
from app.manual_keyword_note import load_manual_keyword_note, past_notes
from app.custom_groups import load_custom_groups
from app.subheading_names import name_pool
from app.excel_export import format_pub_datetime
from app.llm_classifier import (
    UNCLASSIFIED_DISPLAY_TEXT,
    UNCLASSIFIED_GROUP_NAME,
    UNCLASSIFIED_ICON,
    round_id_for_run,
)
from app.llm_classifier import cached_group_names as llm_cached_group_names
from app.undo import peek_label as undo_peek_label
from app.group_order import apply_group_order, is_order_locked
from app.filters import headline_kind, HEADLINE_TAG_RE, looks_like_photo_caption, photo_badge_tip
from app.highlight import highlight_keywords
from app.icons import icon
from app.labels import label_stats, labels_for_url, snapshot_source
from app.manual_articles import load_manual_articles
from app.naver_api import outlet_display_label
from app.settings import all_search_keywords, auto_send_grace_sec, is_auto_send_enabled, load_settings
from app.storage import is_today, load_latest_confirmed_run
from app.summarizer import summarize_groups

# [추가: 2026-09-10] AI 요약 복사 텍스트 맨 위에 붙는 제목 한 줄 — 화면 h3와 같은 말이다.
# 화면 h3는 icon("chat") SVG를 쓰지만 복사 텍스트는 순수 문자열이라 이모지로 대신한다.
# 확정본·초안 두 화면이 같은 값을 써야 해서 여기 한 곳에 둔다(app.preview_renderer가 import).
AI_SUMMARY_HEADING = "🤖 AI가 읽은 소제목별 주요 요약"

# [추가: 2026-07-26] "뉴스가 잠잠" 빈 상태 문구용 손글씨체 — app.config.CUTE_FONT_BASE64
# 참고(오프라인에서도 깨지지 않도록 base64로 파일에 직접 박아 넣은 서브셋 폰트).
_CUTE_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{CUTE_FONT_NAME}'; "
    f"src: url(data:font/woff2;base64,{CUTE_FONT_BASE64}) format('woff2'); font-display: swap; }}"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>확정본 · 언론 모니터링 {run_slot} 기준</title>
<style>
  {cute_font_face}
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: var(--r-lg);
  }}
  /* [수정: 2026-09-16] 카드 위 여백 60→44px — 60px은 상단 고정바(54px)를 피하려는 값인데 글자 위로
     30px이 비었다. 44px이면 16px 남는다. 확정본·초안·실시간·정기 보관함·설정·수시·정책 단어 추이
     일곱 화면이 같은 값을 써야 화면을 오갈 때 제목이 들썩이지 않는다(수시·추이는 84→68px 형태).
     시안 SUBHEAD_SPACING_MOCKUP.html B안. */
  .container {{ padding-top: 44px; padding-bottom: 56px; }}
  /* 상단바 CSS는 app/topnav.py 한 곳 — 모든 화면이 같은 값을 쓴다. */
{topnav_style}
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
  /* [추가: 2026-08-03] 하단바 🖍️ — 형광펜 단어를 설정 화면까지 안 가고 이 화면에서
     바로 추가·삭제할 수 있는 팝오버. 휴지통(숨긴 기사 관리) 바로 왼쪽에 둔다. */
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
  /* [추가: 2026-08-05] 소제목 미니 목차 — 소제목 하나 안에 기사가 많아지면 원하는
     소제목을 찾으려고 계속 스크롤해야 하는 문제가 있어, 평소엔 숨겨진 버튼(펼침형,
     사용자가 고정형 대신 선택)을 눌러야만 목차가 나타나게 했다. 화면을 항상 가리지
     않으면서, 필요할 때 소제목 이름을 눌러 바로 그 위치로 스크롤 이동한다. */
  /* [수정: 2026-08-10] 원래 accent(파랑) 배경이라 (확정)/(전송) 버튼과 색이 겹쳐
     헷갈린다는 피드백 — 눈에 안 튀는 중립색(카드 배경+테두리)으로 바꿨다. */
  .toc-toggle-btn {{
    position: fixed; right: 20px; bottom: 20px; width: var(--fab-sm); height: var(--fab-sm); border-radius: var(--r-circle);
    background: {card}; color: {muted}; border: 1px solid {border}; font-size: 1.2rem; cursor: pointer;
    box-shadow: var(--sh-float); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .toc-toggle-btn:hover {{ background: {hover}; }}
  /* [추가: 2026-08-10] 확정된 회차를 텔레그램/이메일로 보내는 플로팅 버튼 — ☰ 바로
     위에 쌓는다. 이모지 없이 텍스트만(사용자 요청), 정원형 유지.
     색은 app.config COLOR_SEND — 그 값을 왜 그 톤으로 골랐는지는 config 쪽 주석에 있다. */
  /* [추가: 2026-08-11] ↩ 되돌리기 — 오른쪽 플로팅 버튼들(발송·목차)과 성격이
     반대인 "취소" 계열이라 사용자 요청대로 화면 왼쪽 아래에 따로 뒀다. 되돌릴 게 있을
     때만 렌더링한다(항상 떠 있으면 "뭘 되돌리는지" 알 수 없어 오히려 불안하다). */
  /* [수정: 2026-09-02] 왼쪽 아래는 이제 두 칸짜리 스택이다 — 아래가 휴지통(숨긴 기사),
     위가 ↩ 되돌리기. 수시 수집 결과 화면(.fab.left / .fab.left.upper)과 같은 배치라
     두 흐름을 오갈 때 같은 자리에 같은 것이 있다. 휴지통이 0건이라 안 그려질 때도 ↩는
     제자리(위 칸)에 둔다 — 숨기기 한 번에 버튼이 아래에서 위로 뛰면 그게 더 불안하다. */
  .undo-fab {{
    position: fixed; left: 20px; bottom: 88px; width: var(--fab-sm); height: var(--fab-sm); border-radius: var(--r-circle);
    background: {card}; color: {muted}; border: 1px solid {border}; font-size: 1.3rem;
    cursor: pointer; box-shadow: var(--sh-float); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .undo-fab:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .undo-fab:disabled {{ opacity: 0.5; cursor: progress; }}
{hidden_trash_style}
{scroll_top_style}
{hide_batch_style}
{url_add_style}
{note_history_style}
{name_picker_style}
  .confirm-send-fab {{
    position: fixed; right: 20px; bottom: 80px; width: var(--fab-lg); height: var(--fab-lg); border-radius: var(--r-circle);
    background: {send}; color: {on_fill}; border: none; font-size: var(--fs-md); font-weight: 700;
    cursor: pointer; box-shadow: var(--sh-float); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .confirm-send-fab:disabled {{ opacity: 0.6; cursor: not-allowed; }}
  /* 확정본 플로팅 버튼 순서(위→아래): 발송 → ☰ (56px 버튼 + 14px 간격 기준 위치 계산). */
  /* [추가: 2026-08-10] (확정) 직후 잠깐 뜨는 안내 — 화면이 바로 새로고침되므로 애니메이션
     없이 짧게(0.9초) 보여주기만 한다. */
  .confirm-toast {{
    position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 300;
    background: {header}; color: {on_fill}; padding: 10px 20px; border-radius: var(--r-lg);
    font-size: var(--fs-md); font-weight: 600; box-shadow: var(--sh-float);
  }}
  /* 카운트다운은 (발송) 버튼(bottom:80) 바로 위 146px에 둔다. */
  .send-countdown {{
    position: fixed; right: 20px; bottom: 146px; width: 130px; text-align: right;
    font-size: var(--fs-sm); font-weight: 700; color: {error}; z-index: 200; white-space: nowrap;
  }}
  /* [추가: 2026-08-10] 담당자가 아니라 시스템이 대신 전송한 회차 표시 — 카운트다운(빨강,
     경고성)과 다른 맥락이라 회색으로 톤을 낮췄다.
     [수정: 2026-08-13] 담당자가 직접 (발송)을 눌렀을 때도 같은 자리에 같은 스타일로
     표시한다(.auto-sent -> .sent-done으로 이름 변경) — 예전엔 자동발송만 흔적이 남고
     사람이 누른 건 토스트가 사라지면 아무것도 안 남아서, 새로고침 후 "내가 보냈나?"를
     확인할 방법이 없었다(재발송 위험). 사람/기계 구분은 색이 아니라 앞머리 이모지
     (✔️/🤖)가 맡는다 — 둘 다 "이미 끝난 일"이라 색까지 나눌 이유가 없다(사용자 선택).
     width는 "(2회)" 같은 꼬리표가 붙어도 잘리지 않게 고정값 대신 내용에 맞춘다
     (fixed 요소 + width:auto = 내용 폭, right:20px 기준으로 왼쪽으로 늘어난다). */
  .send-countdown.sent-done {{ color: {muted}; width: auto; }}
  /* 발송 완료 글자를 누르면 발송 기록(/send-log)의 그 회차 줄로 — 누가 받았는지 보는 길. */
  a.send-countdown.sent-done, .send-countdown .send-log-link {{ text-decoration: none; }}
  a.send-countdown.sent-done:hover {{ color: {accent}; text-decoration: underline; }}
  .send-countdown .send-log-link {{ color: inherit; font-weight: 600; }}
  .send-countdown .send-log-link:hover {{ text-decoration: underline; }}
  /* [추가: 2026-08-11] 카운트다운이 0에 닿는 순간 그 자리에서 바로 바뀌는 완료 문구 —
     "곧 나갑니다"에 가까운 낙관적 표시라, 서버가 실제로 확인해 남기는 회색 배지
     (.auto-sent)와는 색을 달리해 파란색으로 둔다. */
  .send-countdown.auto-sent-just-now {{ color: {accent}; width: 170px; }}
  /* [추가: 2026-08-12] 이번 회차 소제목 분류가 LLM 실패로 규칙 기반(단어 빈도)에
     떨어졌을 때 카운트다운 자리에 대신 뜨는 경고 — 자동발송도 이 경우 보류된다
     (app.confirm_send.check_pending_confirm_and_send). 빨강(.send-countdown 기본색)을
     그대로 쓴다 — 경고성이라는 점에서 카운트다운과 같은 색이 맞고, width만 문구
     길이에 맞춘다. */
  .send-countdown.classification-warning {{ width: 210px; white-space: normal; line-height: 1.4; }}
  /* [추가: 2026-08-26] 발송 실패 배너 — 위 classification-warning과 같은 이유로 같은
     빨강(.send-countdown 기본색)을 그대로 쓴다. 채널·대상 줄이 여러 줄일 수 있어
     조금 더 넓게 잡는다. */
  .send-countdown.send-failure {{ width: 260px; white-space: normal; line-height: 1.4; }}
  /* [수정: 2026-08-18] 하단 (취소)/(적용) 바가 스크롤을 따라 밀려나지 않도록,
     스크롤 영역(.toc-scroll)과 바깥 상자(.toc-popover)를 분리했다 — max-height와
     padding은 스크롤 영역 쪽으로 옮겼고 상자 자체의 치수는 그대로다. */
  .toc-popover {{
    display: none; position: fixed; right: 20px; bottom: 74px; width: 200px;
    background: {card}; border: 1px solid {border};
    border-radius: var(--r-lg); box-shadow: var(--sh-pop); z-index: 200;
  }}
  .toc-popover.is-open {{ display: block; }}
  .toc-scroll {{ max-height: 320px; overflow-y: auto; padding: 8px; }}
  /* [추가: 2026-08-05] 목차 안에서 소제목 순서까지 바로 바꿀 수 있게 항목마다 작은
     ▲▼를 붙였다 — 이름 클릭(이동)과 화살표 클릭(순서 변경)을 분리해서, 클릭 하나에
     동작 하나만 대응하도록 했다(선택 후 상단 고정 화살표 방식은 이름 클릭의 기존
     "이동" 의미와 충돌해서 채택하지 않음). */
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
  /* [추가: 2026-08-05] 방금 순서를 옮긴 항목을 기사의 "방금 승격" 표시와 같은 노란색으로
     한 번 표시한다 — 팝오버가 새로고침 후 자동으로 다시 열리는 동안, 여러 개를 연달아
     옮길 때 방금 뭘 옮겼는지 헷갈리지 않게.
     [수정: 2026-08-18] 이제 ▲▼가 새로고침을 일으키지 않으므로, 이 표시는 (적용)을
     누를 때까지 계속 남아 "이번에 뭘 옮겼는지"를 누적해서 보여준다. */
  .toc-row.toc-row-moved {{ background: {row_moved}; }}
  .toc-empty {{ color: {muted}; font-size: var(--fs-sm); padding: 6px 8px; }}
  /* [추가: 2026-08-18] 팝오버 하단 확정 바 — 담당자가 "(적용)을 누르기 전까지는 아무것도
     바뀌지 않았다"고 믿을 수 있어야 하므로, 서버로 나가는 길은 (적용) 하나뿐이다.
     구조는 .edit-summary-form을 그대로 따랐다(내용 위 / 오른쪽 정렬 액션 아래, 취소가
     왼쪽·커밋이 오른쪽) — 이 앱에서 유일하게 "즉시 저장이 아닌" 기존 화면이라 관례를
     새로 만들 이유가 없다. 바꾼 게 없으면 아예 나타나지 않는다(.undo-fab이 되돌릴 게
     있을 때만 뜨는 것과 같은 원칙). */
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
  /* 미확정 상태에서 팝오버를 닫으려 하면 닫지 않고, 어디서 결정해야 하는지만 가리킨다.
     경고음·모달·confirm() 없이 기존 {hover}색으로 두 번 깜빡이기만 한다 — 실수가 아니라
     아직 안 끝난 것뿐이라 겁을 줄 이유가 없다. 새 색은 만들지 않는다. */
  @keyframes toc-nudge {{ 0%, 100% {{ background: {card}; }} 25%, 75% {{ background: {hover}; }} }}
  .toc-foot.nudge {{ animation: toc-nudge 0.5s ease-in-out; }}
  /* [추가: 2026-07-30] app.preview_renderer와 동일한 체크박스 일괄이동 바 —
     평소엔 숨어있다가 체크박스를 선택하면 왼쪽에 나타나고, 🗑️는 그대로 오른쪽에 남는다. */
  .bulk-move-bar {{ display: none; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--fs-md); }}
  .bulk-move-bar.is-active {{ display: flex; }}
  .bulk-move-bar .bulk-move-count {{ font-weight: 600; color: {header}; white-space: nowrap; }}
  #bulk-move-select {{
    border: 1px solid {accent}; border-radius: var(--r-md); padding: 5px 8px; font-size: var(--fs-md);
    color: {text}; background: {card};
  }}
  .bulk-move-bar button {{ padding: 5px 12px; font-size: var(--fs-md); }}
  /* [추가: 2026-08-04] 일괄 위/아래 이동 버튼 — 선택한 기사가 서로 다른 소제목에
     걸쳐 있거나 소제목의 맨 위/아래에 닿으면 비활성화된다(updateBulkMoveBar). */
  #bulk-move-up, #bulk-move-down {{ padding: 5px 10px; }}
  #bulk-move-up:disabled, #bulk-move-down:disabled {{ opacity: 0.35; cursor: not-allowed; }}
  .bulk-move-bar .clear-btn {{ background: transparent; color: {muted}; }}
  .bulk-move-bar .clear-btn:hover {{ background: {hover}; }}
  /* [수정: 2026-09-18] 체크박스가 작아 누르기 어렵다는 지적 — 브라우저 기본 13px → 16px
     (시안 mockups/CHECKBOX_SIZE_MOCKUP.html B안). 소제목 머리 체크박스도 같은 크기,
     체크 색은 앱 파랑. 기사 체크박스는 커진 만큼 2px 내려 제목 첫 줄 가운데에 맞춘다. */
  .article-select, .group-select-all {{ width: 16px; height: 16px; margin: 3px 3px 0 4px;
    accent-color: {accent}; cursor: pointer; }}
  .article-select {{ flex-shrink: 0; position: relative; top: 2px; }}
  .group-select-all {{ vertical-align: -3px; }}
  .group-move-select {{
    flex-shrink: 0; width: 100px; height: var(--h-sm); box-sizing: border-box; border: 1px solid {border}; border-radius: var(--r-sm);
    padding: 0 4px; font-size: var(--fs-sm); color: {muted}; background: {card};
  }}
  /* 📌 담아둔 기사의 승격 드롭다운만 조금 넓다 — 「확정본에 넣기…」가 100px에선 잘린다
     (실측: 글자 81px + 안쪽 여백 8 + 테두리 2 + 화살표 자리 ≈ 111px). 나머지 규격은
     「다른 소제목」과 같다. */
  .group-move-select.promote-select {{ width: 114px; }}
  /* [추가: 2026-08-04] 소제목별/시간순/언론사순 보기 전환 — 실제 소제목 구성은 그대로
     두고 화면에 나열하는 순서만 바꾸는 용도라, 다른 액션 버튼과 톤을 맞추되 select임을
     알 수 있게 테두리를 살짝 강조한다. */
  .view-mode-select {{
    border: 1px solid {accent}; border-radius: var(--r-md); padding: 6px 10px; font-size: var(--fs-md);
    color: {text}; background: {card};
  }}
  /* [수정: 2026-08-04] 기사가 없을 때만 걸리던 점선 테두리를, 사용자가 직접 만든
     소제목이면 기사가 있어도 계속 유지되도록 바꿨다(.subheading-empty -> .subheading-custom)
     — 자동 분류 소제목과 구분되도록, 아이콘 대신 색을 진한 하늘색으로 키워 눈에 더
     잘 띄게 했다(사용자 피드백: 아이콘은 소제목이 길면 놓치기 쉽지만 테두리는 카드
     전체를 감싸 계속 보인다). */
  /* [수정: 2026-08-05] 빈 여백이 너무 넓어 보인다는 피드백 — 패딩을 줄였다. 사실 가장
     큰 원인은 아래 .subheading h2 쪽 브라우저 기본 margin이었다(같이 수정). */
  .subheading-custom {{ border: 2px dashed {custom_group_border}; border-radius: var(--r-lg); padding: 8px 16px; }}
  .empty-group-hint {{ color: {muted}; font-size: var(--fs-md); margin: 6px 0 0; }}
  header h1 {{ font-size: var(--fs-xl); margin: 0; color: {header}; }}
  /* [추가: 2026-08-10] "지난 기사"는 복사·다운로드 같은 이 화면의 액션이 아니라 다른
     화면으로 이동하는 링크라 툴바에서 빼고 제목 옆(내비게이션 자리)으로 옮겼다
     (시안 A, 사용자 선택) — 회색 톤으로 제목보다 눈에 덜 띄게 둔다. */
  .header-top {{ display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 12px; }}
  .actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  /* [추가: 2026-08-12] app.preview_renderer.py와 동일한 이유 — 🤖 이모지가 든 버튼만
     색이모지 글꼴의 line-height 때문에 다른 버튼보다 박스가 커 보이는 문제가 있어
     height를 고정값으로 못박는다. */
  .actions button, .actions a.btn {{ height: 32px; box-sizing: border-box; }}
  /* [추가: 2026-08-10] 정렬 보기 방식(소제목별/시간순 등)은 복사·다운로드·소제목 만들기
     같은 액션이 아니라 표시 옵션이라, 툴바 맨 오른쪽으로 분리했다(사용자 선택 — 붙여
     두면 그냥 버튼 하나처럼 섞여 구분이 안 된다는 피드백). margin-left: auto로 같은 줄
     안에서만 밀어내며, select 자체는 내용만큼만 짧게 유지한다(폭 지정 없음). */
  .actions .view-mode-select {{ margin-left: auto; }}
  /* [수정: 2026-08-03] button 태그는 브라우저 기본 스타일상 body의 font-family를 물려받지
     않아(a 태그와 달리) 지금까지 Arial 등 시스템 기본체로 렌더링되고 있었고, appearance:
     auto(네이티브 OS 버튼 껍데기)까지 남아있어 폰트를 맞춰도 렌더링이 미묘하게 달랐다 —
     font-family: inherit·appearance: none으로 고쳤는데도 "txt로 저장"(a 태그)만 여전히
     글자 위치가 달라 보인다는 세 번째 재지적으로, 크롬이 button 태그 텍스트는 내부적으로
     수직 중앙 정렬해주지만 a 태그는 그런 처리가 없다는 걸 마지막으로 발견 — align-items:
     center를 직접 지정해 브라우저 기본 동작에 기대지 않고 두 태그를 완전히 동일하게 그린다. */
  button, a.btn {{
    background: {accent}; color: {on_fill}; border: none; border-radius: var(--r-md);
    padding: 6px 14px; font-size: var(--fs-md); font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-flex; align-items: center; justify-content: center;
    user-select: none; -webkit-user-select: none;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  /* [수정: 2026-08-03] 화면 상단 액션 툴바(복사·txt로 저장·Telegram 전송·지난 기사)만
     "정직한 파란색" 원색 채움 대신 톤온톤 소프트 필로 — 디자인 시안 B안(사용자 선택).
     .actions button 쪽이 위 전역 button 규칙보다 더 구체적이라(class+element > element)
     이 툴바 안에서만 덮어쓰고, 🗑️/↑/↓ 등 다른 버튼은 그대로 원색 유지. */
  .actions button, .actions a.btn {{
    background: {hover}; color: {accent}; font-weight: 600; border-radius: var(--r-md);
    height: var(--h-md); padding-top: 0; padding-bottom: 0; box-sizing: border-box;
  }}
  .actions button:hover, .actions a.btn:hover {{ background: {accent_tonal}; }}
  /* [수정: 2026-08-03] "+ 새 소제목 만들기"만 나머지 툴바 버튼과 기능이 달라(내용을
     내보내는 게 아니라 새 그릇을 만드는 것) 시안 A(고스트 아웃라인)로 유일하게 다르게
     둔다 — .actions .create-group-btn이 위 .actions button보다 구체적이라(class 2개)
     덮어쓴다. */
  .actions .create-group-btn {{
    background: transparent; color: {accent}; border: 1px solid {ghost_border};
    border-radius: var(--r-md); font-weight: 400;
  }}
  .actions .create-group-btn:hover {{ background: {hover}; border-color: {ghost_border_hover}; }}
  /* [추가: 2026-08-11] 확정본 "🤖 전체 기사 재분류" 버튼 — 왼쪽 그룹 맨 끝(구분선 뒤)에 둔다.
     [수정: 2026-08-12] 처음엔 채운 연보라였는데, 초안에 같은 기능의 버튼("전체 기사
     재분류")을 고스트(테두리만)로 만들면서 화면마다 같은 버튼이 다르게 생기는 문제가
     생겼다(사용자 지적, 스크린샷으로 비교) — **파괴적 동작은 어느 화면에서든 채우면
     안 된다**는 원칙에 맞춰 확정본도 고스트로 통일했다. 자세한 이유는
     app/preview_renderer.py의 같은 자리 주석 참고. */
  .actions-divider {{ width: 1px; height: 26px; background: {border}; margin: 0 2px; }}
  /* [수정: 2026-08-26] 고스트(테두리만) → "AI 기사 배정"과 같은 채운 연보라. 사용자 결정 —
     app/preview_renderer.py의 .reclassify-btn과 같은 이유·같은 배색이라 두 화면이 계속
     같은 모양을 유지한다. 파괴적이라는 신호는 아이콘(새로고침)과 항상 뜨는 confirm()이 맡는다. */
  .actions .regen-btn {{
    position: relative; background: {ai_bg}; color: {ai_text}; border: 1px solid {ai_border};
  }}
  .actions .regen-btn:hover {{ background: {ai_bg_hover}; border-color: {ai_border_hover}; }}
  .actions .regen-btn:disabled {{ opacity: 0.5; cursor: progress; }}
  /* [추가: 2026-08-11] AI가 만든 소제목 표식. 화면 전용이며, 소제목 이름 문자열과
     완전히 분리된 별도 span이라 복사/다운로드/발송 텍스트에는 절대 따라가지 않는다. */
  .ai-badge {{ margin-left: 6px; font-size: var(--fs-sm); opacity: 0.75; vertical-align: middle; }}
  /* [추가: 2026-08-20] 소제목별 기사 건수 칩 — 이름 바깥 별도 span(복사/다운로드/발송
     텍스트에 안 새게). 📂 미분류의 빨간 unclassified-count와 같은 모양·중립 회색이라
     "빨강=처리할 것 / 회색=그냥 정보"로 구분된다. */
  .subheading-count {{
    margin-left: 6px; font-size: var(--fs-sm); font-weight: 700; color: {muted};
    background: {pill_bg}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  /* 헤더의 회차 전체 기사 건수 칩 — 같은 이유로 h1 옆 별도 span. */
  .total-count-badge {{
    margin-left: 8px; font-size: var(--fs-sm); font-weight: 700; color: {muted};
    background: {pill_bg}; border-radius: var(--r-pill); padding: 2px 10px; vertical-align: middle;
  }}
  /* 임시 보관함(아직 분류 안 된 기사) 제목 — 소제목이 아니라 대기실이라는 게 읽히도록
     꺾쇠 없이 회색 기울임꼴로, 확정된 소제목들과 시각적으로 확실히 구분한다. */
  /* [수정: 2026-08-26] font-style: italic 제거 — app/preview_renderer.py의 같은 자리와
     동일한 이유(옆 건수 칩·버튼과 밑줄이 안 맞아 한 줄로 안 읽힘). */
  .unclassified-title {{ color: {muted}; font-weight: 600;
    display: inline-flex; align-items: center; gap: 5px; }}
  /* [수정: 2026-08-21] 빨강 → 앰버(주의 세트) — app.preview_renderer의 같은 배지와
     동일한 이유(이 화면도 [단독]/[속보] 빨강과 겹친다)로 같이 맞춘다. */
  .unclassified-count {{
    margin-left: 4px; font-style: normal; font-weight: 700; font-size: var(--fs-sm);
    color: {warn_text}; background: {warn_chip_bg}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  /* [수정: 2026-09-16] 소제목 사이 28→20px — 소제목 밑줄이 이미 구분선 노릇을 한다.
     초안(app/preview_renderer.py)에도 같은 값이 있다. */
  .subheading {{ margin-top: 20px; }}
  /* [수정: 2026-08-05] h2 브라우저 기본 margin(위아래 약 15px)이 빈 소제목 카드를 실제
     내용보다 훨씬 커 보이게 만드는 주범이었다 — margin을 직접 지정해 제거. 겸사겸사
     ✏️/🗑️/↑↓를 제목 바로 옆이 아니라 줄 오른쪽 끝에 붙이고 싶다는 요청도 이 flex로
     함께 해결(왼쪽 = 제목, 오른쪽 = 아이콘 묶음). 기사 한 줄(.article summary)은 반대로
     "짧은 제목 뒤 버튼이 화면 끝까지 멀어지는" 문제 때문에 flex-start를 의도적으로 쓰고
     있어 그대로 둔다 — 소제목 헤더와 기사 한 줄은 서로 다른 문제라 같은 답을 쓰지 않는다. */
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
  /* [추가: 2026-08-13] 소제목 🗑️(빈 소제목 삭제·소제목 통째로 숨기기)는 ✏️(이름
     바꾸기)와 같은 .rename-btn을 공유해 hover 색이 없었다 — 파괴력이 다른 두 액션이
     같은 무반응 회색이라 어색하다는 지적으로, 기사 단위 🗑️(.hide-btn:hover)와 같은
     빨강을 이 아이콘에만 추가한다(✏️는 그대로 무채색 유지). */
  .rename-btn.group-hide-btn:hover {{ color: {error}; }}
  /* [추가: 2026-08-03] 소제목 순서 조정 버튼 — 기사용 .move-btn과 같은 톤이되 소제목
     헤더 안에 있어 별도 클래스로 둔다. */
  /* [수정: 2026-08-05] 기사 ↑/↓(.move-btn)과 가로폭이 안 맞아서 나란히 보면 좁아 보였다
     — 패딩·글자 크기를 .move-btn과 통일했다. */
  /* [수정: 2026-08-13] 소제목 순서 버튼을 ↑↓에서 ▲▼로 바꿨다 — 기사 순서 버튼(.move-btn)과
     글리프·색·크기까지 똑같아서 "뭘 옮기는 버튼인지"가 위치로만 구분됐고, 정작 같은 일을
     하는 목차 팝오버 버튼(.toc-order-btn)은 이미 ▲▼여서 오히려 그쪽과 어긋나 있었다.
     이제 삼각형=소제목 / 화살표=기사로 대상이 형태만 보고 구분된다.
     ▲▼는 같은 크기에서 ↑↓보다 크고 무겁게 보여 font-size를 1rem에서 낮춰 균형을 맞춘다. */
  .order-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.78rem; cursor: pointer;
    vertical-align: middle; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .order-btn:disabled {{ opacity: 0.35; cursor: default; }}
  /* [수정: 2026-09-16] 행 여백 한 단계씩 축소 — padding 6→4px, 제목·메타 줄 사이 4→2px,
     행 사이 10→6px. 글자·버튼 크기는 그대로다(한 건 74 → 64px). 확정본·초안·정기 보관함·
     수시 네 파일에 같은 값이 복제돼 있으니 한쪽만 고치지 않는다. 시안 ARTICLE_ROW_DENSITY_MOCKUP.html B안. */
  .article {{ margin: 6px 0; line-height: 1.5; padding: 4px 8px; border-radius: var(--r-md); }}
  /* [추가: 2026-08-20] [단독] 기사는 카드 자체를 강조한다(빨강 좌측바 + 연빨강 배경) —
     담당자 지목: "단독이 가장 중요"이므로 글자색 하나로는 [속보]가 여러 건 나올 때
     묻힐 수 있다(개수와 무관하게 항상 덩어리로 먼저 보이게). 다른 상태 배경(hover·
     체크됨·새로 왔다·AI 마감 배정·방금 이동/승격)보다 **먼저** 선언해 가장 낮은
     우선순위를 준다 — 저 상태들은 전부 "지금 이 순간의 정보"라 "이 기사는 원래
     중요하다"는 이 배경보다 항상 이겨야 한다(기존 우선순위 원칙과 동일한 방향).
     app.filters.headline_kind로 판정, app.live_renderer의 .t-scoop/.t-flash와 같은
     팔레트를 쓴다. */
  .article.art-scoop {{ background: {scoop_bg}; border-left: 3px solid {scoop_bar}; padding-left: 9px; }}
  .t-scoop {{ color: {scoop_text}; font-weight: 800; }}
  .t-flash {{ color: {flash_text}; font-weight: 600; }}
  /* [추가: 2026-08-05] 마우스를 올린 기사 줄을 연한 회색으로 표시해 긴 목록에서도 지금
     어느 줄을 보고 있는지 놓치지 않게 한다. 체크 표시({hover}, 파란색)·방금 승격
     표시(노란색)와 겹치지 않는 중립적인 회색을 골랐다 — 이 규칙을 :has(체크됨) 규칙보다
     먼저 둬서, 체크된 채로 마우스를 올려도 "체크됨" 파란색이 우선 보이게 한다(체크는
     능동적으로 선택한 상태라 스쳐 지나가는 마우스 오버보다 우선순위가 높아야 한다). */
  .article:hover {{ background: {row_hover}; }}
  /* [추가: 2026-08-21] 사진 추정 기사는 지우지 않고 행 전체를 흐리게 해 "훑을 때는
     건너뛰고, 확인하고 싶으면 마우스를 올리거나 체크한다"를 표현한다. opacity는
     background와 다른 속성이라 위/아래 배경색 규칙과 순서 다툼이 없다 — 어떤 배경이
     깔리든 그 위에 흐림만 얹힌다. 마우스 오버·체크 상태에선 다시 또렷해져, "지금
     내가 보고 있거나 고른 것"까지 흐려 보이지 않게 한다. */
  .article.is-photo {{ opacity: 0.55; }}
  .article.is-photo:hover, .article.is-photo:has(.article-select:checked) {{ opacity: 1; }}
  /* [추가: 2026-08-03] 일괄 이동용 체크박스를 켠 기사는 연한 배경으로 표시해 "지금
     뭘 골랐는지" 스크롤하면서도 바로 보이게 한다 (디자인 시안 A, 사용자 선택) —
     자바스크립트 변경 없이 :has()만으로 동작. */
  .article:has(.article-select:checked) {{ background: {hover}; }}
  /* [추가: 2026-08-13] 지난번 이 회차를 봤을 때는 없다가 새로 들어온 기사 — 초안에만
     있던 표시(app.preview_renderer .is-new-arrival)를 확정본에도 그대로 들여왔다.
     확정본은 저장된 스냅샷이라 "새 기사가 없다"고 생각하기 쉽지만, 실제로는 📌 담아둔
     기사 승격·초안에서 예약해둔 기사 합류로 회차 도중에도 늘어난다 — 그게 소제목 안에
     아무 표시 없이 섞여 들어가 못 찾겠다는 피드백. 색·의미 모두 초안과 동일한 옅은
     노랑이다(🤖가 움직인 결과인 연보라와는 다른 축 — 이건 "그냥 새로 왔다"는 정보).
     .just-promoted/.just-moved보다 **먼저** 선언해, 겹칠 때 그쪽이 이긴다: "새로 왔다"는
     화면을 열 때마다 판정되는 배경 정보고, 저 둘은 방금 내 조작의 결과라 더 급하다.
     [메모: 2026-09-04] 이 기준선은 📌 담아둔 기사 구획의 행까지 담으므로(그 구획도 같은
     render_article로 그려져 .article[data-url]이다), **승격은 이 노랑을 유발하지 않는다** —
     노랑이 붙는 건 담아두기로 그 행이 화면에 처음 생길 때다. 승격 직후 색은 초록이다. */
  .article.is-new-arrival {{ background: {row_new}; }}
  /* [추가: 2026-08-20] 초안 마감 시점에 방금 자동으로 소제목에 배정된 기사 — 담당자가
     못 본 사이 AI가 대신 판단해 붙인 것이라 "새로 왔다"(노랑)보다 더 구체적이고 급한
     정보다. is-new-arrival보다 뒤에 선언해 겹칠 때(거의 항상 겹친다 — 마감 시 새로
     들어온 기사는 대개 이번이 처음 보는 회차라 새-도착 기준선에도 안 걸려 있다) 이
     보라색이 이긴다. 다만 just-promoted/just-moved(내가 방금 조작한 결과)보다는
     앞에 둬 그쪽이 최종 우선한다 — "내 조작"이 "AI가 마감 때 한 일"보다 급하다는
     기존 원칙(.is-new-arrival 주석)을 그대로 잇는다. border-left는 배경색이 나중
     규칙에 덮여도(같은 속성이 아니므로) 계속 남아 "이 기사는 그 배정 대상"이라는
     표식이 사라지지 않는다 — 음영은 담당자가 손대도 계속 남아야 한다(사용자 확인). */
  .article.finalize-added {{
    border-left: 3px solid {ai_mark}; padding-left: 11px;
  }}
  /* [수정: 2026-08-27] 배경 음영만 별도 클래스로 뗐다 — 배너의 "확인했어요"를 누르면
     JS가 이 클래스만 걷어내고, 왼쪽 보라 선·🤖 배지는 그대로 남긴다("봤다"는 표시는
     하되 "어떤 기사였는지"는 계속 알아볼 수 있어야 한다 — 사용자 결정). 선언 위치는
     예전 그대로라 is-new-arrival(노랑)과의 우선순위도 그대로이고, 음영을 걷어낸
     뒤에야 그 노랑이 드러난다. */
  .article.finalize-added-bg {{ background: {ai_bg}; }}
  .article.finalize-added.is-focused {{ outline: 2px solid {ai_mark}; outline-offset: 2px; }}
  .finalize-added-badge {{
    flex-shrink: 0; padding: 1px 7px; border-radius: var(--r-pill); font-size: var(--fs-xs); font-weight: 700;
    background: {card}; color: {ai_text}; border: 1px solid {ai_border}; white-space: nowrap;
    position: relative; top: -0.15em;  /* [수정: 2026-08-21] 제목보다 글자가 작은 알약이라 baseline 정렬만으론 약간 아래로 보인다(실측: 위 여백 +1px, 아래 -2.3px) — 반 칸(0.15em) 올려 제목 글자 높이 한가운데에 맞춘다. */
  }}
  /* h2 옆 — 마감 시점에 이름 자체가 새로 생긴 소제목. */
  .new-subheading-badge {{
    font-size: var(--fs-xs); font-weight: 700; padding: 2px 7px; border-radius: var(--r-pill);
    background: {ai_bg}; color: {ai_text}; border: 1px solid {ai_border}; white-space: nowrap;
    margin-left: 4px; vertical-align: middle;
  }}
  /* [추가: 2026-08-20] 확인 배너 — 초안의 round-over-banner(마감 알림)와 같은 자리·형태,
     색만 AI 전용 연보라로 바꿨다. 자동 배정된 기사가 있을 때만 서버가 렌더링하고,
     JS가 이 회차에서 이미 "확인했어요"를 눌렀는지(localStorage)를 보고 보일지 정한다. */
  .finalize-banner {{
    display: none; position: fixed; top: 0; left: 0; right: 0; z-index: 21;
    background: {ai_banner_bg}; border-bottom: 1px solid {ai_border};
  }}
  .finalize-banner.is-visible {{ display: block; }}
  .finalize-banner-inner {{
    max-width: 800px; margin: 0 auto; padding: 11px 24px;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }}
  .finalize-banner .msg {{ flex: 1; min-width: 200px; font-size: var(--fs-md); color: {ai_text}; }}
  .finalize-banner .msg b {{ color: {ai_text_strong}; }}
  .finalize-banner .go {{
    background: {header}; color: {on_fill}; border: none; border-radius: var(--r-md);
    padding: 8px 14px; font-size: var(--fs-md); font-weight: 700; cursor: pointer; white-space: nowrap;
  }}
  .finalize-banner .go:hover {{ background: {header_pressed}; }}
  .finalize-banner .dismiss {{
    background: transparent; border: none; color: {ai_text_muted}; font-size: var(--fs-sm);
    cursor: pointer; text-decoration: underline; padding: 4px;
  }}
  /* 배너가 뜨면 그만큼 topbar와 본문을 아래로 밀어 겹치지 않게 한다(높이는 JS가 실측). */
  body.has-finalize-banner .topbar {{ top: var(--finalize-banner-h, 0px); }}
  body.has-finalize-banner .container {{ margin-top: calc(24px + var(--finalize-banner-h, 0px)); }}
  /* [추가: 2026-08-05, 수정: 2026-09-04] "📌 담아둔 기사"에서 승격시킨 기사가 자동분류
     목록 어디에 들어갔는지 한눈에 안 보여서, 새로고침 직후 딱 한 번 표시한다(체크박스
     선택 배경색{hover}과는 다른 색이어야 두 상태가 헷갈리지 않는다 — 승격은 "방금 옮김",
     체크는 "지금 선택 중"으로 의미가 다르다).
     색은 노랑(row_new)에서 이동 색(row_moved)으로 옮겼다 — 노랑은 "이 화면에서 처음 보는
     기사"인데 📌 구획은 승격 전부터 같은 화면 아래에 떠 있고(드롭다운이 그 구획 안에 있어
     안 볼 수가 없다), 아래 is-new-arrival 기준선도 .article[data-url] 전부를 담아 승격된
     기사를 이미 "새 도착 아님"으로 판정한다. 앱 자신의 판정과 어긋나는 색이었다. */
  .article.just-promoted {{ background: {row_promoted}; }}
  /* [수정: 2026-08-05] 노란색은 이미 초안 화면의 "새로 도착한 기사"(.is-new-arrival)
     표시에 쓰이고 있어 — 순서를 옮긴 기사까지 같은 노란색을 쓰면 서로 다른 의미가 한
     색으로 겹쳐 헷갈린다는 피드백. 순서 이동 전용으로 연두색을 새로 뺐다(기존 팔레트의
     형광펜용 라임색 #C6FF00과도 톤이 달라 헷갈리지 않는다).
     [수정: 2026-09-04] 승격(.just-promoted)도 이 색으로 합류했다 — 둘 다 "내가 방금
     만진 결과가 어디 갔나"라는 같은 질문에 답한다(위 주석 참고). */
  .article.just-moved {{ background: {row_moved}; }}
  /* [추가: 2026-09-15] 「AI 기사 나누기」가 방금 옮긴 기사 — 초안의 같은 클래스(「AI 기사 배정」
     결과)와 같은 연보라. 노랑(is-new-arrival)보다 뒤에 선언해 겹치면 이쪽이 이긴다(내가 시켜서
     AI가 옮긴 것이 "새로 왔다"보다 지금 묻는 질문에 가깝다). 다음 새로고침에 사라진다. */
  .article.just-classified {{ background: {ai_bg}; }}
{split_button_style}
{screen_tag_style}
  /* [수정: 2026-08-13] summary를 1행(제목이 게시시각·액션 묶음과 폭을 다투던 flex 한
     줄)에서 2행으로 나눴다 — 제목이 대부분 2~3줄로 꺾이던 문제(실측: 액션 묶음이
     카드 폭의 약 40%를 고정 점유) 때문. 1행(.title-row)은 체크박스+제목이 카드 전체
     폭을 쓰고, 2행(.action-row)은 게시시각(좌)+액션 묶음(우)이다. */
  .article summary {{
    cursor: pointer; display: flex; flex-direction: column; gap: 2px;
    -webkit-tap-highlight-color: transparent;
  }}
  .article summary::marker {{ color: {muted}; }}
  /* [수정: 2026-07-30, 유지: 2026-08-13] 🗑️를 제목 바로 옆(정확히는 같은 카드의 2행)에
     둔다 — 예전엔 하단 footer에 있어 요약을 펼치면 제목에서 멀어져 어떤 버튼이 어떤
     기사 것인지 헷갈렸다. */
  .title-row {{ display: flex; align-items: baseline; justify-content: flex-start; gap: 8px; }}
  /* [수정: 2026-08-13] 원문보기·복사하기가 액션 묶음에 추가되며 좁은 화면(모바일)에서
     한 줄에 다 안 들어가 옆으로 45px가량 넘치던 문제(실측) — action-row와 그 안의
     article-actions 둘 다 wrap을 허용해, 안 들어가면 액션 묶음이 통째로 다음 줄로
     내려가고(그래도 안 들어가면 아이콘 단위로 더 줄바꿈) 오른쪽 정렬은 유지된다. */
  .action-row {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; row-gap: 4px; }}
  /* [추가: 2026-08-07] 모바일 사파리에서 제목(summary)을 눌러 펼치면 그 뒤로 제목 글자가
     보라색으로 바뀌어 보인다는 제보 — 이 앱 CSS가 지정한 색이 아니라, 사파리가 탭할 때
     보여주는 하이라이트 효과(-webkit-tap-highlight-color)가 제대로 안 지워지고 남는
     것으로 보인다(iOS 사파리에서 종종 나타나는 현상). 색을 투명 처리해 아예 안 나타나게
     막는다. */
  .title-line {{ min-width: 0; overflow-wrap: anywhere; color: {text}; -webkit-tap-highlight-color: transparent; }}
  /* [추가: 2026-09-11] 제목 앞 언론사 — 괄호 대신 회색·조금 작은 글자로 제목과 가른다
     (render_article의 line_html). 초안·정기 보관함에도 같은 규칙이 있다 — 한쪽만 고치지 않는다. */
  .title-outlet {{ color: {muted}; font-size: 0.86em; font-weight: 500; margin-right: 0.5em; }}
  /* [수정: 2026-08-05, 2026-08-13] 다른 소제목/원문보기/복사하기/⋯/🗑️/↑↓ 묶음은
     margin-left: auto로 2행(.action-row) 오른쪽 끝에 고정 — 표 형태처럼 항상 같은
     자리에 있어야 여러 기사를 훑을 때 클릭 위치가 안 흔들린다. 게시시각은 왼쪽에
     그대로 둔다. */
  .article-actions {{
    margin-left: auto; display: flex; align-items: center; justify-content: flex-end;
    gap: 4px; flex-shrink: 0; flex-wrap: wrap; row-gap: 4px;
  }}
  /* [수정: 2026-08-21] 표식 없는 사진기사 추정 배지 — app.live_renderer의 실시간 현황과
     같은 "아이콘+문구" 칩·같은 채도 낮은 모래색으로 맞췄다(전에는 제목 줄이 빡빡하다는
     이유로 아이콘만 썼는데, 사진기사 제외 설정을 기본 끔으로 바꾸며 이 배지가 흔히
     보이게 됐으니 문구가 바로 읽혀야 한다). */
{late_badge_style}
{export_links_style}
  /* 상단 안내 배너 — 초안의 "AI 분류 실패" 상자와 같은 자리(기사 목록 위 흐름 안).
     회차 마감 배너처럼 화면 상단에 고정하지 않는다: 그건 "지금 하는 작업이 헛수고"라는
     급한 경고라 그렇게 한 것이고, 이건 그 정도가 아니다. 회수가 0건이면 서버가 이
     배너를 아예 안 그린다(0건이면 안 그린다 — 📂 미분류 배지·📷 사진 추정 버튼과 같은
     원칙. 늘 떠 있는 안내는 곧 안 읽히는 안내가 된다). */
  .late-banner {{
    margin: 0 0 14px; padding: 11px 16px; border-radius: var(--r-lg);
    background: {warn_bg}; border: 1px solid {warn_border}; color: {warn_text};
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  .late-banner .msg {{ flex: 1; min-width: 220px; font-size: var(--fs-md); line-height: 1.5; }}
  .late-banner .cnt {{
    font-family: inherit; font-weight: 800; font-size: var(--fs-md); cursor: pointer;
    border: 1px solid {warn_border}; border-radius: var(--r-md); padding: 2px 9px;
    background: {warn_chip_bg}; color: {warn_text};
  }}
  .late-banner .cnt:hover {{ background: {card}; }}
  .late-banner .q {{
    border: 1px solid {warn_border}; background: {card}; color: {warn_text};
    border-radius: var(--r-md); padding: 2px 7px; font-size: var(--fs-sm); cursor: help; line-height: 1.4;
  }}
  .photo-badge {{
    flex-shrink: 0; padding: 1px 7px; border-radius: var(--r-pill); font-size: var(--fs-xs);
    color: {photo_badge_text}; background: {photo_badge_bg}; border: 1px solid {photo_badge_border}; white-space: nowrap;
    position: relative; top: -0.15em;  /* [수정: 2026-08-21] 제목보다 글자가 작은 알약이라 baseline 정렬만으론 약간 아래로 보인다(실측: 위 여백 +1px, 아래 -2.3px) — 반 칸(0.15em) 올려 제목 글자 높이 한가운데에 맞춘다. */
  }}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: var(--fs-md); }}
  /* [수정: 2026-07-30] "이미 확인함" 표시를 브라우저에 영구 기억(localStorage)하는 대신,
     "지금 펼쳐서 보고 있는 기사"에만 실시간으로 적용한다 — 예전 방식은 클릭했던 기사가
     전부 보라색으로 남아 화면이 정신없어진다는 피드백에 따른 변경. details가 열려있는
     동안에만 :has()로 색을 입히므로(자바스크립트 불필요), 닫거나 다른 기사를 열면 이
     기사는 자동으로 원래 색으로 돌아간다. */
  /* [수정: 2026-08-21] 색(남색) 대신 opacity로 — 화면에 남는 파랑이 전부 "누를 수
     있는 것"이 되도록, 상태 표시는 색이 아닌 밝기로만 준다. */
  .article:has(details[open]) .title-line {{ opacity: 0.62; }}
  /* [수정: 2026-08-03] 게시 시각은 복사/내보내기 텍스트(_build_plain_text)에는 원래도
     포함되지 않지만, 화면에서 마우스로 직접 드래그해 복사할 땐 화면에 보이는 대로
     같이 딸려왔다 — user-select: none으로 이 부분만 드래그 선택 자체가 안 되게 한다
     (화면 표시는 그대로 유지, 클립보드에만 안 실림). */
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
  /* [수정: 2026-08-14] 원문보기를 아이콘 묶음에서 빼내 게시시각 옆 텍스트 링크로
     옮겼다 — ↗(원문보기)와 복사 아이콘이 17px 크기에선 둘 다 "네모 두 겹" 실루엣이라
     구분이 안 된다는 지적(사용자 스크린샷). 성격도 달라서(원문보기=이동, 나머지=편집)
     자리를 아예 분리하는 게 맞다고 판단 — 왼쪽(게시시각 옆)은 화면을 보는 동작, 오른쪽
     (article-actions)은 화면을 바꾸는 동작이라는 구획이 이제 색과 위치 둘 다로 드러난다.
     글자 링크라 클릭 영역도 아이콘보다 넓어져 모바일에서 유리하다. */
  .time-sep {{ color: {muted}; font-size: var(--fs-sm); margin: 0 2px; user-select: none; -webkit-user-select: none; }}
  .origin-link-text {{
    flex-shrink: 0; color: {accent}; font-size: var(--fs-sm); text-decoration: none;
    display: inline-flex; align-items: center; gap: 2px; white-space: nowrap;
  }}
  .origin-link-text:hover {{ text-decoration: underline; }}
  .origin-link-icon {{ width: 0.75em; height: 0.75em; }}
{kw_inline_style}
{photo_gather_style}
  /* [추가: 2026-08-14] 체크박스가 있는 화면(확정본/초안)에서 게시시각·원문보기가 제목
     글자 시작점과 다른 줄에서 시작해 왼쪽 끝이 들쭉날쭉하다는 지적(사용자 스크린샷의
     분홍 사각형) — 브라우저에서 title-line의 실제 렌더 좌표를 재서 맞췄다(28px,
     체크박스 자체의 기본 여백까지 포함된 값이라 "체크박스 폭+gap" 계산과는 다르다).
     체크박스가 없는 지난 기사 화면은 :has()가 매치되지 않아 원래대로 0부터 시작한다
     (article-select CSS 자체가 그 화면엔 없다). */
  /* 체크박스 16px 기준: 왼쪽 여백 4 + 16 + 오른쪽 여백 3 + gap 8 = 31px (13px일 땐 28px). */
  .article:has(.article-select) .action-row {{ padding-left: 31px; }}
  /* [추가: 2026-08-13] 복사하기 — 클릭하면 아이콘이 1초간 check로 바뀐다(copyArticleIcon).
     기본 상태는 복사 아이콘만, .is-copied 상태는 check 아이콘만 보인다 — 색은 두
     상태 모두 그대로 무채색(다른 아이콘 버튼과 통일, 전환 자체가 이미 충분한 신호). */
  /* [추가: 2026-09-01] 소제목 헤더의 📋(.group-copy-btn)도 같은 전환을 쓴다 — 같은
     일(복사)에 같은 피드백이어야 하므로 규칙을 복제하지 않고 선택자만 늘린다.
     색·크기는 옆의 ✏️🗑️와 맞추려고 .rename-btn을 함께 걸었고, 여기서는 아이콘
     두 개를 겹쳐 두기 위한 display만 준다. */
  .copy-btn, .group-copy-btn {{ display: inline-flex; align-items: center; }}
  .copy-btn .icon-done, .group-copy-btn .icon-done {{ display: none; }}
  .copy-btn.is-copied .icon-default, .group-copy-btn.is-copied .icon-default {{ display: none; }}
  .copy-btn.is-copied .icon-done, .group-copy-btn.is-copied .icon-done {{ display: inline-flex; }}
  /* [추가: 2026-08-05] 원문 다시 가져오기 버튼 — 평소엔 숨겨두고 그 기사 카드에
     마우스를 올렸을 때만 나타난다(제목 옆이 붐비지 않게). */
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
     아이콘 대신 글자로 이름을 보여준다. [수정: 2026-08-13] 복사하기는 다시 메뉴에서
     빠져나가 상시 노출 아이콘(.copy-btn)이 됐다 — URL 줄이 사라지며 화면에서
     클립보드로 가는 유일한 경로가 됐기 때문. */
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
  /* 🏷 라벨 팝오버 — 확정본·초안·정기 보관함 공용(app.renderer.label_popover_style). */
{label_popover_style}
  /* [추가: 2026-08-05] ✏️ 직접 수정 — 🔄가 원문에서도 못 찾는 경우의 최후 수단으로
     펼쳐지는 인라인 편집 칸. */
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
  .empty {{ text-align: center; margin-top: 80px; font-size: var(--fs-lg); color: {muted}; }}
  .cute-caption {{ font-family: '{cute_font_name}', sans-serif; font-size: 1.4rem; margin-top: 6px; }}
  .bottom {{ margin-top: 40px; background: {bg}; border: 1px solid {border}; border-radius: var(--r-lg); padding: 16px 18px; }}
  .bottom h3 {{ margin: 0 0 8px; font-size: var(--fs-base); color: {header}; }}
  .bottom-summary {{ padding-top: 2px; }}
  .bottom-header-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
  .bottom-header-row h3 {{ margin: 0; }}
  .bottom-summary-copy {{
    font-size: var(--fs-sm); color: {accent}; background: transparent; border: 1px solid {border};
    border-radius: var(--r-md); padding: 3px 10px; cursor: pointer; font-family: inherit;
  }}
  .bottom-summary-item {{ padding: 8px 0; border-bottom: 1px solid {border}; }}
  .bottom-summary-item:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .bottom-summary-item strong {{ display: block; color: {header}; margin-bottom: 3px; }}
  .bottom-summary-item p {{ margin: 0; white-space: pre-line; }}
  .manual-divider {{
    display: flex; align-items: center; gap: 10px; margin: 32px 0 4px;
    color: {muted}; font-size: var(--fs-sm);
  }}
  .manual-divider::before, .manual-divider::after {{ content: ""; flex: 1; border-top: 1px dashed {border}; }}
  /* 📌 담아둔 기사 칸 — "📂 소제목 미분류"(.subheading-unclassified)와 **같은 문법**을 쓰고
     색만 가른다: 왼쪽 4px 띠 + 옅은 바탕 + 오른쪽만 둥근 모서리. 둘 다 "보고서에 아직
     안 들어간 임시 칸"이라 모양을 맞추고, 앰버(=담당자가 아직 모르는 것)와 그보다 한 단계
     어두운 캐러멜(=한 번은 보고 골라둔 것)로 단계를 가른다. 파랑은 앱 전체에서 "담당자의
     동작"이라 이 칸에 쓰면 뜻이 겹친다(시안 mockups/PINNED_ICON_BADGE_MOCKUP.html). */
  .manual-zone {{
    border-left: 4px solid {pinned_bar}; background: {pinned_bg};
    border-radius: 0 var(--r-lg) var(--r-lg) 0; padding: 10px 16px 12px; margin-top: 10px;
  }}
  .manual-zone-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }}
  .manual-zone-title {{ font-weight: 600; color: {text}; font-size: var(--fs-base); }}
  .manual-zone-hint {{ font-size: var(--fs-sm); color: {muted}; }}
  /* 건수 칩 — 미분류 칸의 .unclassified-count(앰버)를 빌려 쓰지 않고 칸 색(캐러멜)을 따른다. */
  .manual-zone-count {{
    margin-left: 4px; font-weight: 700; font-size: var(--fs-sm); color: {pinned_text};
    background: {pinned_chip_bg}; border: 1px solid {pinned_border}; border-radius: var(--r-pill); padding: 1px 8px;
  }}
  /* [추가: 2026-08-26] 제목·건수·배정 버튼을 한 묶음으로 왼쪽에 붙인다 — 📂 소제목 미분류
     칸의 헤더와 같은 구성(제목 → 건수 → 배정 버튼)이라 두 칸이 같은 줄로 읽힌다.
     baseline 정렬로는 버튼이 글자 밑선에 매달려 보여 이 묶음만 center로 맞춘다. */
  .manual-zone-left {{ display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }}
  .manual-zone .article:first-child {{ margin-top: 10px; }}
  /* [추가: 2026-08-05] "+ 직접 키워드 작성하기" — AI 키워드 블록과 별개로, 이용자가
     자유 서식으로 적어두는 메모 한 줄. 저장된 메모가 있으면 처음부터 열려서 보이고,
     없으면 버튼을 눌러야 나타난다(is-open 토글). */
  /* [수정: 2026-08-07] 스크롤해서 기사를 읽다가 메모를 적으려면 위로 되돌아가야 했던
     문제 — sticky로 고정해 상단 바(topbar, 높이만큼 top: 60px) 바로 아래 붙어서 화면에
     계속 보이게 한다. background를 명시해야 아래로 스크롤된 기사 카드들이 비쳐 보이지
     않는다. */
  .keyword-note-zone {{
    display: none; align-items: center; gap: 8px; border: 1px dashed {border}; border-radius: var(--r-lg);
    padding: 10px 14px; margin: 10px 0 4px; flex-wrap: wrap;
    position: sticky; top: 60px; z-index: 15; background: {card};
  }}
  .keyword-note-zone.is-open {{ display: flex; }}
  /* [수정: 2026-09-07] 「마감 후 자동 배정」 배너가 뜨면 topbar가 배너 높이만큼
     내려가는데(body.has-finalize-banner .topbar) 이 sticky의 top은 60px 그대로여서
     메모 칸이 배너·topbar 뒤(z-index 15 < 20·21)에 완전히 가려졌다 — 담당자 눈에는
     "확정본만 상단 고정이 안 되는" 것으로 보인다. topbar와 같은 변수를 따라가게 한다. */
  body.has-finalize-banner .keyword-note-zone {{ top: calc(60px + var(--finalize-banner-h, 0px)); }}
  .keyword-note-zone input {{
    flex: 1; min-width: 220px; height: var(--h-md); box-sizing: border-box; padding: 0 10px; border: 1px solid {border}; border-radius: var(--r-md);
    font-size: var(--fs-md); color: {text}; background: {card};
  }}
  /* [추가: 2026-08-10] 저장된 메모는 곧바로 다시 손대지 않도록 잠가둔다 — (수정)을
     눌러야 편집 가능해진다(아래 toggleKeywordEditMode). "이미 저장됐다"는 상태를
     입력칸 자체로도 보여준다는 취지(사용자 피드백). */
  .keyword-note-zone input:disabled {{ background: {bg}; color: {muted}; }}
  .keyword-note-zone button {{ font-size: var(--fs-sm); height: var(--h-md); padding: 0 12px; box-sizing: border-box;
    border-radius: var(--r-md); white-space: nowrap; }}
  /* 모양 규칙: 지우기(삭제·취소)는 채움 파랑으로 두지 않는다 — 글자 버튼, 올리면 빨강. */
  .keyword-note-zone .clear-btn {{ background: transparent; color: {muted}; }}
  .keyword-note-zone .clear-btn:hover {{ background: transparent; color: {error}; }}
  /* [추가: 2026-08-26] 직전 회차 메모를 끌어와 미리 채워둔 상태 — 아직 "이번 회차 메모"가
     아니라는 뜻이라 "주의"(앰버) 계열을 쓴다(설정 화면의 "저장 안 함"과 같은 뜻).
     flex-wrap 컨테이너 안에서 width:100%로 다음 줄에 온다. */
  /* [추가: 2026-08-13] 이모지 대신 쓰는 단색 SVG 아이콘(app.icons) 공통 크기·색 —
     currentColor라 부모의 글자색을 그대로 물려받는다. */
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
</style>
</head>
<body>
{finalize_banner_html}
{topnav_html}
<div class="container">
  <header>
    <div class="header-top">
      <h1><span class="screen-tag final">확정본</span>언론 모니터링 {run_slot} 기준{total_count_html}</h1>
    </div>
    <div class="actions">
      <button class="create-group-btn" type="button" onclick="createCustomGroup()">+ 새 소제목</button>
      {keyword_note_btn_html}
      <span class="actions-divider"></span>
      {regen_btn_html}
      <span class="actions-right">
        {photo_gather_btn_html}
        <span class="export-links">
          <button type="button" onclick="copyPlainText()" title="보고서 텍스트를 클립보드에 복사">복사</button>
          <form method="POST" action="/download-text" style="display:contents" onsubmit="this.text.value=PLAIN_TEXT;">
            <input type="hidden" name="filename" value="{export_filename}">
            <input type="hidden" name="text" value="">
            <button type="submit" title="txt 파일로 받기">텍스트</button>
          </form>
          <form method="POST" action="/download-excel" style="display:contents">
            <input type="hidden" name="filename" value="{export_excel_filename}">
            <input type="hidden" name="rows" value="{export_rows_json}">
            <button type="submit" title="엑셀 파일로 받기">엑셀</button>
          </form>
        </span>
        <select class="view-mode-select" onchange="applyViewMode(this.value)" title="소제목 구성은 그대로 두고 화면에 나열하는 순서만 바꿉니다">
          <option value="subheading" selected>소제목 내 언론사순</option>
          <option value="group-time">소제목 내 시간순</option>
          <option value="time">시간순</option>
          <option value="outlet">언론사순</option>
        </select>
      </span>
    </div>
  </header>
  <div class="keyword-note-zone{note_open_class}" id="keyword-note-zone" data-run-slot="{run_slot_raw}">
    <input type="text" id="keyword-note-input" value="{note_value_attr}" placeholder="키워드 a, 키워드 b, 키워드 c..." onkeydown="keywordNoteKey(event)"{note_input_disabled_attr}>
    <button type="button" onclick="toggleKeywordEditMode(this)">{note_save_btn_label}</button>
    <button class="clear-btn" type="button" onclick="{note_clear_btn_onclick}">{note_clear_btn_label}</button>
    {note_history_html}
  </div>
  {late_pickup_banner_html}
  {body}
</div>
{send_countdown_html}
{action_fab_html}
{undo_fab_html}
{hidden_trash_html}
{name_picker_html}
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
{hidden_trash_script}
{scroll_top_script}
{hide_batch_script}
{range_select_script}
{url_add_script}
{note_history_script}
{name_picker_script}
{kw_inline_script}
{photo_gather_script}
const PLAIN_TEXT = {plain_text_json};
function copyPlainText() {{
  navigator.clipboard.writeText(PLAIN_TEXT)
    .then(() => alert("클립보드에 복사했습니다."))
    .catch(() => alert("복사에 실패했습니다."));
}}
// [추가: 2026-08-10] 기사 한 건만 복사 — 버튼 자체에 이미 이 기사 한 줄(data-copy-text)이
// 있어서 서버 호출 없이 바로 복사한다. 성공하면 아이콘을 잠깐 ✅로 바꿔 피드백을 준다
// (alert는 매번 뜨면 번거로워서 쓰지 않는다 — 카드 하나짜리 가벼운 동작이라 조용한
// 피드백이 더 어울린다는 판단).
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
// [수정: 2026-08-13] 복사하기가 "⋯ 더보기" 메뉴에서 나와 상시 노출 아이콘이 되며
// 글자 치환(구 copyArticleText, btn.textContent를 "복사했습니다"로 바꾸는 방식)
// 대신 아이콘 자체를 1초간 check로 바꾼다 — 메뉴가 아니라 카드에 항상 붙어 있는
// 버튼이라 글자 라벨을 넣을 자리가 없다. .copy-btn.is-copied가 CSS로 두 아이콘
// (.icon-default/.icon-done) 중 하나만 보여준다.
// [추가: 2026-09-01] 소제목 헤더의 📋(.group-copy-btn)도 이 함수를 그대로 쓴다 —
// 하는 일이 "data-copy-text를 클립보드에 넣고 아이콘을 1초간 ✓로"로 완전히 같아서,
// 이름만 다른 쌍둥이 함수를 만들면 한쪽만 고쳤을 때 피드백이 어긋난다.
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
// 🏷 라벨 팝오버 — 확정본·초안·정기 보관함 공용(app.renderer.label_popover_script).
{label_popover_script}
// [추가: 2026-08-10] 완료 안내 공통 토스트(발송·재분류·되돌리기 등에서 재사용) — alert() 팝업 대신
// 화면 상단에 잠깐 떴다 사라지는 배너(사용자 선택: "롤오버와 다르게 액션 효과를
// 알아볼 수 있어야 하니까" — hover 같은 은근한 효과가 아니라 눈에 띄되 클릭해서
// 닫을 필요는 없는 방식). thenFn을 주면 그 시간만큼 뒤에 실행(화면이 바뀌기 전
// 토스트를 먼저 보여주는 용도, 예: location.reload), 없으면 토스트 스스로 사라진다.
function showToast(message, thenFn) {{
  var toast = document.createElement("div");
  toast.className = "confirm-toast";
  toast.textContent = message;
  document.body.appendChild(toast);
  if (thenFn) {{ setTimeout(thenFn, 900); }}
  else {{ setTimeout(function() {{ toast.remove(); }}, 1500); }}
}}
// [추가: 2026-08-13] "AI가 읽은 소제목별 주요 요약" 복사 — 위쪽 기사 목록 복사(PLAIN_TEXT)와는
// 별도 텍스트라(_build_plain_text가 이 블록을 의도적으로 제외한다), 헷갈리지 않도록
// 한 번 더 확인창을 거친다.
function copyAiSummary(btn) {{
  if (!confirm("AI 요약을 복사할까요? (위 기사 목록 복사와는 별도로 복사됩니다)")) return;
  navigator.clipboard.writeText(btn.dataset.copyText).then(function() {{
    showToast("복사했습니다.");
  }}).catch(function() {{
    alert("복사에 실패했습니다.");
  }});
}}
// [수정: 2026-08-10] Telegram/Email 개별 버튼을 하나로 합쳤다 — 설정된 채널(텔레그램/
// 이메일 중 켜둔 쪽)에 전부 보낸다(app.confirm_send.send_confirmed_run). 2번째 이상
// 전송이면 서버가 제목 앞에 "(수정)"을 자동으로 붙인다.
function sendReport(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/send-scrap", {{
    method: "POST",
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: PLAIN_TEXT}})
  }}).then(function(res) {{
    // 200 = 일부만 받음(서버 본문 "partial"), 204 = 모두 받음. 일부만 받았을 때 다시 누르면 받은
    // 사람에게도 (수정)으로 한 번 더 가므로, 누가 못 받았는지부터 보게 한다.
    if (res.status === 200) {{ alert("일부는 못 받았어요 — 받은 사람에겐 이미 갔습니다. 새로고침 뒤 「받은 사람 보기」에서 누가 왜 못 받았는지 확인하세요."); location.reload(); }}
    else if (res.ok) {{ showToast("이메일 및 텔레그램으로 발송되었습니다.", function() {{ location.reload(); }}); }}
    // [수정: 2026-08-26] 예전엔 여기서 끝(재사용 가능하게 버튼만 풀어줌)이라, 실패
    // 사유가 이 토스트 한 줄 말고는 어디에도 안 남았다 — 새로고침하면 왜 실패했는지가
    // 사라졌다. 이제 서버가 실패 시에도 화면을 다시 그려두므로(app.settings_server.
    // _handle_send_scrap), 새로고침하면 카운트다운 자리에 정확한 사유가 뜬다.
    else {{ alert("발송에 실패했습니다 — 새로고침하면 사유가 화면에 표시됩니다."); location.reload(); }}
  }}).catch(function() {{
    alert("발송에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
    btn.disabled = false;
  }});
}}
// [추가: 2026-08-10] 확정 후 CONFIRM_SEND_GRACE_SEC(기본 5분) 안에 전송 안 하면 서버가
// 스스로 전송한다 — 이 카운트다운은 그 유예 시각을 그대로 화면에 보여주는 순수 표시용
// JS다(서버 스케줄러 tick이 실제 판단·전송을 한다. 이 탭이 닫혀 있어도 자동전송은
// 그대로 일어난다). send_count가 이미 1 이상이면(한 번이라도 보낸 뒤) 더 이상 자동
// 전송 대상이 아니므로 카운트다운을 표시하지 않는다.
const GRACE_DEADLINE_MS = {grace_deadline_ms};
function updateSendCountdown() {{
  var el = document.getElementById("send-countdown");
  if (!el) return;
  var remain = Math.max(0, Math.floor((GRACE_DEADLINE_MS - Date.now()) / 1000));
  // [추가: 2026-08-11] 0에 닿으면 "00:00 후 자동발송"에서 멈춰 있어(새로고침 전까지)
  // "안 보내진 건가?" 헷갈린다는 피드백 — 완료 문구(파란 글자)로 바꾼다. 실제 자동
  // 발송은 서버가 POLL_INTERVAL_SEC(10초)마다 확인하므로 이 문구가 실제 발송보다 최대
  // 10초쯤 먼저 뜰 수 있는 "곧 됩니다" 성격의 낙관적 표시다 — 화면을 다시 열면 서버가
  // 실제로 기록한 결과(회색 "🤖 N시 M분 자동발송 완료" 배지)로 정확히 바뀐다.
  if (remain <= 0) {{
    el.textContent = "자동발송 완료되었습니다.";
    el.classList.add("auto-sent-just-now");
    return;
  }}
  var mm = String(Math.floor(remain / 60)).padStart(2, "0");
  var ss = String(remain % 60).padStart(2, "0");
  el.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5.3l3.2 1.9"/></svg> ' + mm + ":" + ss + " {countdown_suffix}";
}}
if (GRACE_DEADLINE_MS) {{
  updateSendCountdown();
  setInterval(updateSendCountdown, 1000);
}}
// [추가: 2026-08-03] 하단바 🖍️ 팝오버 — 형광펜 단어를 이 화면에서 바로 추가·삭제한다.
// 추가/삭제 둘 다 app.settings.toggle_highlight_keyword(있으면 빼고 없으면 추가)
// 하나를 그대로 재사용한다 — 검색 키워드 설정 화면의 🖍️ 버튼과 완전히 같은 동작.
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
    // [수정: 2026-08-05] 칩 자체를 클릭하면 색이 팔레트 다음 순번으로 순환된다(본문
    // 안 단어 클릭으로 순환하던 방식은 삭제 요청에 따라 없앴다 — 이제 색 순환은 이
    // 팝오버 칩에서만 가능하다). ×(삭제) 버튼은 클릭이 칩까지 안 번지게 stopPropagation.
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
// [추가: 2026-08-05] 팝오버 바깥을 클릭하면 닫힌다(표준적인 팝오버 동작) — 🖍️를 다시
// 눌러 닫는 방법과 별개로 하나 더 생기는 것뿐이라 서로 안 부딪힌다. 🖍️ 버튼 자체도
// .highlight-wrap 안에 있어서, 여는 클릭이 그대로 "바깥 클릭"으로 오인돼 열리자마자
// 닫히는 일은 없다.
document.addEventListener("click", function(e) {{
  var pop = document.getElementById("highlight-popover");
  if (pop.classList.contains("is-open") && !e.target.closest(".highlight-wrap")) {{
    pop.classList.remove("is-open");
  }}
}});
// [추가: 2026-08-05] 소제목 미니 목차(펼침형) — 소제목 안에 기사가 많아지면 원하는
// 소제목을 찾으려고 계속 스크롤해야 하는 문제를 풀기 위해, 평소엔 숨겨진 버튼을
// 눌러야만 소제목 이름 목록이 나타나게 했다(고정형 대신 펼침형을 사용자가 선택 —
// 화면을 항상 가리지 않으면서, 필요할 때만 누르면 된다). 서버에 새 데이터를 요청하지
// 않고 화면에 이미 그려진 .subheading[data-toc-name]을 그대로 읽어서 만든다.
// [수정: 2026-08-18] 팝오버 안 ▲▼는 이제 누를 때마다 저장하지 않는다 — 팝오버가 들고
// 있는 순서만 바꾸고, (적용)을 눌렀을 때 /save-group-order + 새로고침을 딱 한 번 한다.
// 예전엔 한 칸 옮길 때마다 전체 페이지를 다시 그려서(index.html 286KB + 인라인 스크립트
// 재실행) 맨 아래 소제목을 맨 위로 올리는 데 새로고침이 7번 일어났다.
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
// 화면에 이미 그려진 .subheading[data-toc-name]을 그대로 읽는다 — 서버에 새로 묻지 않는다.
function _tocReadSections() {{
  return Array.prototype.map.call(document.querySelectorAll(".subheading[data-toc-name]"), function(sec) {{
    var renameBtn = sec.querySelector(".rename-btn[data-current]");
    return {{
      raw: renameBtn ? renameBtn.dataset.name : "",
      label: sec.dataset.tocName,
      id: sec.id,
      // [추가: 2026-09-10] 순서가 고정된 칸(기타) — 목차엔 보이되 ▲▼ 대상이 아니다.
      locked: sec.hasAttribute("data-order-locked"),
      // [추가: 2026-08-05] 사용자가 직접 만든 소제목(.subheading-custom, 화면에서 하늘색
      // 점선 테두리로 표시되는 것과 같은 기준)은 목차에서도 볼드로 표시한다.
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
// 이 칸을 건너뛰었다(그 결과 이 칸의 ▲▼는 눌러도 아무 일이 없었다). 목차에는 그대로
// 보여주되(그 위치로 이동하는 링크는 필요하다) ▲▼만 잠그고, 이웃을 찾을 때도 건너뛴다.
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
  // 직전 (적용)에서 옮긴 소제목이 있으면 새로고침 후 한 번만 초록으로 보여준다
  // (_tocApply가 남겨둔 값 — 한 번 쓰고 지운다). 이용자가 다시 ▲▼를 누르는 순간
  // _tocSwap이 이 값을 통째로 비운다.
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
  // 더는 "지금 상태"가 아니므로 통째로 지운다. 안 지우면 이번에 옮긴 자리 초록과
  // 예전 자리 초록이 동시에 남아 블럭이 여러 개로 보인다.
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
  // [수정: 2026-08-14] moveGroupOrder와 같은 이유 — 회차(run_slot)를 같이 실어 보낸다.
  var runSlot = document.getElementById("keyword-note-zone").dataset.runSlot;
  fetch("http://{settings_host}:{settings_port}/save-group-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{order: JSON.stringify(order), run_slot: runSlot}})
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
  // (app.renderer editSummary의 "취소"가 form.remove()만 하는 것과 같은 성격).
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
// [수정: 2026-08-18] 미확정 상태에서는 팝오버 바깥 클릭을 **캡처 단계에서 막는다**.
// 닫기만 막으면 그 클릭이 그대로 뒤로 통과해서, 팝오버 뒤에 있는 헤더 ▲▼(즉시 저장)가
// 눌려버린다 — 팝오버가 들고 있던 순서와 다른 순서가 저장되고 새로고침까지 일어난다.
// 미확정인 동안에는 화면의 나머지를 비활성으로 두는 게 "나가는 길은 (적용)과 (취소)
// 둘뿐"이라는 규칙과도 맞는다.
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
// [추가: 2026-08-04] 형광펜 단어를 본문에서 직접 클릭하면 팔레트 다음 색으로 바뀐다 —
// 같은 단어는 색이 전역으로 묶여 있으므로, 새로고침 없이 data-word가 같은 모든
// <span>(제목·요약, 다른 소제목의 다른 기사까지)을 한 번에 갱신한다.
// [수정: 2026-08-05] 형광펜 팝오버의 칩을 클릭하면 그 단어 색이 팔레트 다음 순번으로
// 바뀐다 — 본문 안 하이라이트된 단어를 직접 클릭해 바꾸던 방식은 삭제 요청에 따라
// 없앴고(app.highlight.highlight_keywords에서 onclick 제거), 색 순환은 이제 이
// 팝오버 칩에서만 가능하다. 같은 단어가 본문 여러 곳(제목·요약, 다른 기사)에 나와도
// data-word로 한 번에 찾아 배경색을 맞춘다 — 클릭 자체는 칩에서만 가능해도, 색은
// 여전히 단어 하나에 전역으로 묶여 있으므로 본문 표시도 즉시 따라와야 한다.
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
// 쓴다(숨김 판정에는 관여하지 않는다). 세 경로(단건·일괄·소제목 통째)가 이 함수 하나를
// 쓰므로 여기 한 줄이면 전부 실려 간다.
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
// [추가: 2026-08-05] "🔄 원문에서 다시 가져오기" — 이 기사 하나만 원문 페이지의
// og:title/og:description으로 제목·요약을 다시 가져와 저장한다(app.summary_overrides).
// 자동이 아니라 눌렀을 때만 호출되므로 전체 스크랩 속도에는 영향이 없다.
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
// [추가: 2026-08-05] ✏️ 직접 수정 — 🔄가 원문에서도 못 찾는 경우의 최후 수단. 클릭한
// 기사 카드 안에 제목·요약 입력 칸을 바로 펼친다(펼쳐진 요약 위치, details를 강제로 열어
// 보여준다). 저장하면 🔄와 같은 저장소(app.summary_overrides)에 그대로 담긴다.
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
// [추가: 2026-08-04] 소제목별/시간순/언론사순 보기 전환 — 실제 소제목 구성(분류·순서)은
// 서버에 저장된 그대로 두고, 화면에서 기사를 훑어보는 순서만 바꾼다. 시의성 있는 기사를
// 솎아낼 때 소제목을 넘나들며 찾는 대신 한 줄로 쭉 보고 🗑️로 바로 걸러낼 수 있게 하는
// 용도라, 카드(.article) DOM 노드를 실제로 옮겼다가 되돌린다 — 복제하면 🗑️ 등 기존
// 버튼이 참조하는 노드와 화면에 보이는 노드가 달라져 상태가 어긋나기 때문. 처음 전환할
// 때 각 기사의 원래 위치(부모 소제목·다음 형제)를 한 번만 기억해뒀다가, "소제목별"로
// 돌아갈 때 그대로 복원한다.
var _FLAT_VIEW_HOMES = null;
function _restoreSubheadingView() {{
  if (_FLAT_VIEW_HOMES) {{
    // 뒤에서부터 복원해야 한다 — 앞 기사의 home.next가 아직 flat-view 안에 있는 뒤 기사를
    // 가리킬 수 있는데, insertBefore는 참조 노드가 "지금" 같은 부모의 자식이어야 하기
    // 때문이다(순서대로 복원하면 아직 안 옮겨진 next를 참조하다 에러 남). 뒤에서부터
    // 복원하면 각 기사의 next는 이미 제자리로 돌아와 있어 항상 유효하다.
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
  // [추가: 2026-08-04] "소제목 내 시간순" — 소제목 경계는 그대로 두고, 각 소제목 안
  // 카드만 시간순으로 다시 배열한다(시간순/언론사순처럼 통째로 한 줄로 펼치지 않음).
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
// [추가: 2026-07-30] 스크랩 초안에 먼저 만든 체크박스 다중 선택 + 하단 "장바구니" 바를
// 완성본에도 그대로 붙였다(app.preview_renderer와 동일 패턴, 이 화면 자체 컨벤션대로
// reload 방식 유지). 소제목 이름은 화면에 이미 그려진 ✏️ 버튼의 data-name/data-current를
// 읽어서 쓴다 — 이름표(rename)가 붙어 있어도 서버에는 항상 원본 이름으로 보내야 한다.
function getAllGroups() {{
  return Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(btn) {{
    // [추가: 2026-08-07] 일괄이동 바 드롭다운도 사용자가 만든 소제목을 볼드로
    // 표시하기 위해, 그 소제목 카드에 이미 붙어있는 subheading-custom 클래스를 같이 읽는다.
    var custom = btn.closest(".subheading").classList.contains("subheading-custom");
    return {{value: btn.dataset.name, label: btn.dataset.current, custom: custom}};
  }});
}}
// [추가: 2026-08-03] 소제목 자체의 화면 순서를 바꾼다 — 서버가 "지금 순서"를 다시 계산할
// 필요 없이, 화면에 이미 그려진 소제목 순서(getAllGroups와 같은 셀렉터)를 그대로 읽어
// 인접한 두 개만 맞바꾼 뒤 전체 순서를 통째로 저장한다.
function moveGroupOrder(btn, direction) {{
  // [수정: 2026-09-10] 순서 고정 칸(기타)은 이웃·저장 목록에서 뺀다 — 순서는 서버가 붙인다.
  var names = Array.prototype.map.call(document.querySelectorAll(".subheading:not([data-order-locked]) h2 .rename-btn[data-current]"), function(b) {{ return b.dataset.name; }});
  var i = names.indexOf(btn.dataset.name);
  var j = direction === "up" ? i - 1 : i + 1;
  if (i < 0 || j < 0 || j >= names.length) {{ return; }}
  var tmp = names[i]; names[i] = names[j]; names[j] = tmp;
  // [수정: 2026-08-14] renameGroup과 같은 이유 — 순서도 이제 회차 단위 저장이라 이
  // 화면의 회차(run_slot)를 같이 실어 보낸다.
  var runSlot = document.getElementById("keyword-note-zone").dataset.runSlot;
  fetch("http://{settings_host}:{settings_port}/save-group-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{order: JSON.stringify(names), run_slot: runSlot}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-04] 소제목 헤더 체크박스 — 그 소제목(.subheading) 안의 기사 체크박스를
// 전부 같은 상태로 맞춘다. 반대 방향(기사 하나씩 해제했을 때 헤더가 자동으로 풀리는 것)은
// 굳이 동기화하지 않는다 — "전체 선택" 버튼 역할이면 충분하고, 되돌릴 땐 다시 눌러 끄면 된다.
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
  // [추가: 2026-08-04] 일괄 위/아래 이동 버튼 활성화 여부 — 선택한 기사가 전부 같은
  // 소제목 안에 있고, 그 소제목의 맨 위/아래에 닿지 않았을 때만 누를 수 있다.
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
// [추가: 2026-09-15] 「AI 기사 나누기」가 방금 옮긴 기사를 한 번 표시한다(초안의 같은 코드와 동일).
(function() {{
  var raw = sessionStorage.getItem("justClassifiedUrls");
  if (!raw) {{ return; }}
  sessionStorage.removeItem("justClassifiedUrls");
  JSON.parse(raw).forEach(function(u) {{
    var el = document.querySelector('.article[data-url="' + CSS.escape(u) + '"]');
    if (el) {{ el.classList.add("just-classified"); }}
  }});
}})();
function bulkMoveOrder(direction) {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  if (!urls.length) {{ return; }}
  var body = new URLSearchParams();
  urls.forEach(function(u) {{ body.append("urls", u); }});
  body.append("direction", direction);
  fetch("http://{settings_host}:{settings_port}/bulk-move-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    // [추가: 2026-08-05] 위/아래로 여러 번 눌러야 원하는 위치까지 옮겨지는 경우가
    // 많은데, 새로고침될 때마다 체크가 풀리면 매번 다시 체크해야 해서 헷갈린다는
    // 피드백 — 이동 직전 체크된 URL을 sessionStorage에 남겨두고, 새로고침 후
    // 아래 로직이 그 URL들을 다시 체크해준다.
    if (res.ok) {{ sessionStorage.setItem("bulkMoveCheckedUrls", JSON.stringify(urls)); location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] 체크박스로 선택한 기사 여러 개를 한꺼번에 숨긴다 — hideGroup(소제목
// 통째로 숨기기)과 같은 방식(postHideBatch로 /hide-article 한 번)이다. hideGroup과 달리 선택한 기사가 여러 소제목에 걸쳐 있어도
// 상관없다 — 숨기기는 소제목 경계와 무관한 동작이라서.
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
// [추가: 2026-08-11] 확정본용 소제목 재분류 버튼 — app/preview_renderer.py의 동명 함수와
// 같은 동작이고 엔드포인트만 다르다(캐시 있으면 매 렌더링마다 자동 재분류되는 확정본에서도,
// 캐시를 무시하고 AI에게 새로 분류해달라고 명시적으로 요청하는 수단이 필요해서 추가했다).
// [수정: 2026-08-12] 확정본은 이제 저장된 소제목 스냅샷을 그대로 보여주므로(재분류 안 함),
// 이 버튼이 **유일한** 재분류 경로다 = 유일하게 파괴적인 동작이다. 그래서 경고문에 무엇이
// 초기화되고 무엇이 남는지 정확히 적는다. "이번 회차 작업이 모두 초기화"처럼 과장하면
// 담당자가 겁먹어 필요할 때도 못 누르거나, 반대로 한 번 눌러보고 사실과 다른 걸 알면
// 다음부터 경고를 안 읽는다.
// [수정: 2026-08-12] 예전엔 옮겨둔 기사(edits)가 0건이면 확인창 자체를 건너뛰었는데,
// 이 버튼은 옮긴 기사가 없어도 **소제목 이름·순서는 항상** 새로 짓는다 — "잃을 게
// 없다"가 아니라 "손으로 기사를 옮긴 적이 없다"일 뿐이다. 특히 미분류가 0건인(=분류가
// 잘 정리된) 순간이 오히려 가장 잃을 게 많은데 그때 경고가 안 뜨는 게 실사용 중
// 확인됐다(사용자 제보) — 이제 항상 확인하고, "옮겨둔 기사 N건" 문장만 있을 때만 넣는다.
// [수정: 2026-09-01] 문구를 app.preview_renderer의 같은 함수와 다시 맞췄다 — 안심
// 문구는 config.RECLASSIFY_SAFE_NOTE 하나에서 오고(세 자리가 갈라질 수 없게), 옮긴
// 기사는 화면에 실제로 적힌 라벨(「다른 소제목」)로 가리킨다. 확정본에는 폴백 갈래가
// 없다 — 이 화면은 저장된 스냅샷만 읽어서(groups_from_snapshot) 화면을 여는 것만으로
// 폴백이 생길 수 없기 때문(app.preview_renderer의 degraded 분기 참고).
function regenerateSubheadings(btn) {{
  var edits = parseInt(btn.dataset.manualEdits || "0", 10);
  var editsLine = edits > 0
    ? "소제목의 이름과 순서가 초기화되고, 「다른 소제목」으로 옮긴 기사 " + edits + "건은 새 소제목을 따라갑니다.\\n"
    : "소제목의 이름과 순서가 초기화됩니다.\\n";
  if (!confirm(
      "전체 기사를 다시 분류합니다.\\n" + editsLine +
      "{reclassify_safe_note}\\n\\n계속할까요?")) {{
    return;
  }}
  btn.disabled = true;
  var label = btn.innerHTML;
  btn.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 3l1.9 5.1L18 10l-5.1 1.9L11 17l-1.9-5.1L4 10l5.1-1.9Z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8Z"/></svg> 분류하는 중…';
  fetch("http://{settings_host}:{settings_port}/regenerate-subheadings-final", {{
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
  // [수정: 2026-09-03] renameGroup과 같은 이름 고르기 창을 쓴다(같은 함수·같은 목록).
  openNamePicker({{ mode: "create", onPick: _createCustomGroupSubmit }});
}}
function _createCustomGroupSubmit(name) {{
  // [수정: 2026-08-04] 지금 이 화면에 떠 있는 소제목들과만 중복 검사하도록, 현재
  // 그려진 소제목 원래 이름 목록을 같이 보낸다(회차끼리는 같은 이름이어도 무방).
  // [수정: 2026-08-14] renameGroup과 같은 이유 — 이름표/중복검사가 회차 단위라 회차
  // (run_slot)를 같이 실어 보낸다.
  var runSlot = document.getElementById("keyword-note-zone").dataset.runSlot;
  var body = new URLSearchParams({{name: name, run_slot: runSlot}});
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
// [추가: 2026-08-05] "+ 직접 키워드 작성하기" — AI 키워드 블록과 별개로 자유 서식 메모를
// 적어두는 칸. 열기/닫기는 화면에서만 토글하고(저장 안 함), 저장·삭제는 새로고침해서
// 복사/txt/텔레그램 텍스트에도 바로 반영되게 한다(그 텍스트들은 페이지 로드 시점에
// 이미 다 만들어져 있어서 새로고침 없이는 갱신할 방법이 없다).
// [수정: 2026-08-10] 이 버튼은 메모가 없을 때만 렌더링되므로(app/renderer.py
// keyword_note_btn_html) 클릭은 항상 "열기"다 — 패널을 열면서 클릭한 버튼 자신도
// 바로 숨긴다(사용자 요청: 패널이 뜬 순간 이 버튼이 같이 남아있으면 안 된다). 취소하고
// 싶으면 패널의 "삭제"가 빈 값으로 저장 후 새로고침돼 버튼이 다시 나타난다.
function toggleKeywordNote(btn) {{
  document.getElementById("keyword-note-zone").classList.add("is-open");
  btn.style.display = "none";
}}
// [추가: 2026-08-10] 저장/수정 버튼을 하나로 합쳤다 — 입력칸이 잠겨있으면(이미 저장된
// 상태) 이 클릭은 편집을 여는 것뿐(서버 호출 없음, 버튼만 "저장"으로 바뀜), 잠겨있지
// 않으면(새로 쓰는 중) 실제 저장을 호출한다. 저장 성공 후 새로고침되면 서버가 다시
// 잠긴 상태로 그려주므로 별도로 되돌리는 코드가 필요 없다.
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
  var runSlot = document.getElementById("keyword-note-zone").dataset.runSlot;
  fetch("http://{settings_host}:{settings_port}/save-manual-keyword-note", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: text, run_slot: runSlot}})
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
// [추가: 2026-08-10] 저장된 메모가 없는 채로 처음 여는 중이면 "삭제"가 아니라 "취소"다 —
// 아직 서버에 아무것도 저장된 게 없으니 지울 것도 없다. 서버 호출 없이 그냥 새로고침만
// 하면(아무것도 안 바뀐 상태라) 패널이 닫히고 "+ 키워드 직접 작성" 버튼이 원래대로
// 돌아온다.
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
function moveArticle(btn) {{
  var url = btn.dataset.url;
  var direction = btn.dataset.direction;
  fetch("http://{settings_host}:{settings_port}/move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, direction: direction}})
  }}).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justMovedUrls", JSON.stringify([url])); location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [수정: 2026-08-11] el은 소제목 드롭다운(<select>)일 수도, 소제목이 없는 화면의
// 📥 버튼일 수도 있다 — <button>도 value 속성을 갖고 기본값이 ""이라 양쪽 다 같은
// 코드로 읽힌다(빈 값 = "소제목 안 정함", 서버가 자동 분류에 맡긴다).
// 실패하면 드롭다운을 원래 자리(안내 문구)로 되돌려 다시 고를 수 있게 한다 —
// 선택이 남아 있으면 같은 항목을 다시 골라도 change 이벤트가 안 떠서 재시도가 막힌다.
function _promoteManual(el, endpoint, failMsg) {{
  var url = el.dataset.url;
  var group = el.value || "";
  el.disabled = true;
  fetch("http://{settings_host}:{settings_port}/" + endpoint, {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, group: group}})
  }}).then(function(res) {{
    // [추가: 2026-08-05] 새로고침된 페이지에서 방금 승격된 기사를 찾아 표시할 수 있게,
    // sessionStorage에 남겨둔다(아래 justPromotedUrl 확인 로직 참고).
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
  // [수정: 2026-09-03] prompt() 대신 이름 고르기 창을 연다 — 직전 회차 소제목을 그대로
  // 보여주므로, 같은 사안에 AI가 새로 지은 이름을 옛 이름으로 되돌릴 때 다시 칠 필요가 없다.
  // 고르든 새로 치든 그 뒤 저장 경로는 예전과 완전히 같다(아래 _renameGroupSubmit).
  openNamePicker({{
    mode: "rename",
    self: btn.dataset.current,   // 자기 자신은 "이미 있는 이름" 판정에서 뺀다
    onPick: function(picked) {{ _renameGroupSubmit(btn.dataset.name, picked); }}
  }});
}}
function _renameGroupSubmit(name, newLabel) {{
  // [수정: 2026-08-04] createCustomGroup과 동일한 이유 — 지금 화면에 떠 있는 소제목만
  // 중복 검사 대상으로 보낸다.
  // [수정: 2026-08-14] 이름표가 이제 회차 단위로 저장되므로(사용자 요청 — 소제목은
  // 회차마다 새로 쓰여도 된다), 이 화면이 보여주는 회차(run_slot)를 같이 실어 보낸다
  // — saveKeywordNote와 같은 이유·같은 데이터 소스(#keyword-note-zone data-run-slot).
  var runSlot = document.getElementById("keyword-note-zone").dataset.runSlot;
  var body = new URLSearchParams({{name: name, label: newLabel, run_slot: runSlot}});
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
// [추가: 2026-08-13] 지난번 이 회차를 봤을 때 있던 기사 URL을 저장해뒀다가, 이번 로드에서
// 그 목록에 없는 것만 "새로 들어온 기사"(노랑)로 표시한다 — 초안(app.preview_renderer)과
// 같은 방식·같은 색이다. 다만 기준선을 회차별로 나누는 게 여기선 필수다: 확정본은 회차가
// 바뀌면 기사가 통째로 갈리므로, 전역 기준선 하나로 비교하면 새 회차를 열 때마다 화면
// 전체가 노랗게 물들어 아무 정보도 안 된다. 기준선이 없는 회차(처음 여는 회차)는 비교
// 대상이 없으므로 아무것도 표시하지 않고 이번 목록을 기준선으로만 저장한다.
(function() {{
  var storageKey = "{new_arrival_key}";
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
  // 지난 회차·지난 날짜의 기준선은 다시 쓸 일이 없으니 정리한다(안 그러면 회차마다 하나씩 쌓인다).
  for (var i = localStorage.length - 1; i >= 0; i--) {{
    var key = localStorage.key(i);
    if (key && key.indexOf("confirmedKnownUrls:") === 0 && key !== storageKey) {{
      localStorage.removeItem(key);
    }}
  }}
}})();
// [추가: 2026-08-20] "마감 후 자동 배정" 확인 배너 — 서버가 자동 배정된 기사가 있을
// 때만 배너 HTML을 심어두고, 이 회차에서 이미 "확인했어요"를 눌렀는지(localStorage,
// 회차별 키)는 여기서 판단한다. 탭을 닫아도 유지되도록 sessionStorage가 아니라
// localStorage를 쓴다(마감 배너와 다른 선택 — 그쪽은 "지금 하던 일이 헛수고가 된다"는
// 즉시성이라 세션 한정이 맞지만, 이건 "지나간 알림"이라 회차 동안 계속 안 떠야 한다).
(function() {{
  var banner = document.getElementById("finalize-banner");
  if (!banner) return;
  var dismissKey = "{finalize_dismiss_key}";
  function syncBannerHeight() {{
    var on = banner.classList.contains("is-visible");
    document.body.classList.toggle("has-finalize-banner", on);
    document.documentElement.style.setProperty("--finalize-banner-h", on ? banner.offsetHeight + "px" : "0px");
  }}
  // [추가: 2026-08-27] "확인했어요"는 배너만 닫는 게 아니라 기사 행의 연보라 배경도
  // 같이 걷어낸다 — 배너를 닫아도 음영이 그대로면 무엇이 확인된 건지 화면상 달라지는
  // 게 없다는 지적(사용자). 왼쪽 보라 선과 🤖 배지는 남겨, 어떤 기사였는지는 그 회차
  // 내내 계속 알아볼 수 있다. 회차 파일의 finalize_auto_assigned 값 자체는 안 건드리므로
  // 정기 보관함·다음 렌더링의 기록은 그대로다.
  function clearFinalizeShading() {{
    document.querySelectorAll(".article.finalize-added-bg").forEach(function (el) {{
      el.classList.remove("finalize-added-bg");
    }});
  }}
  var dismissed = false;
  try {{ dismissed = localStorage.getItem(dismissKey) === "1"; }} catch (e) {{ dismissed = false; }}
  if (!dismissed) {{
    banner.classList.add("is-visible");
  }} else {{
    clearFinalizeShading();
  }}
  syncBannerHeight();
  // 페이지 막 로드된 시점엔 레이아웃이 아직 자리잡기 전이라 배너 실제 높이가
  // 다르게 잡힐 수 있어(특히 좁은 화면에서 줄바꿈 계산이 안정되기 전), 다음
  // 페인트 프레임에 한 번 더 재보정한다.
  if (window.requestAnimationFrame) {{ requestAnimationFrame(syncBannerHeight); }}
  window.dismissFinalizeBanner = function () {{
    banner.classList.remove("is-visible");
    clearFinalizeShading();
    syncBannerHeight();
    try {{ localStorage.setItem(dismissKey, "1"); }} catch (e) {{ /* 저장 실패는 이번만 다시 뜨는 것뿐이라 무시 */ }}
  }};
  // 자동 배정된 기사는 여러 소제목에 흩어져 있을 수 있어, "어딘가 보라색으로
  // 표시돼 있다"로는 결국 못 찾는다 — 누를 때마다 하나씩 순서대로 펼쳐 보여준다.
  var cursor = -1;
  window.jumpToNextFinalizeAdded = function () {{
    var items = Array.prototype.slice.call(document.querySelectorAll(".article.finalize-added"));
    if (!items.length) return;
    items.forEach(function (n) {{ n.classList.remove("is-focused"); }});
    cursor = (cursor + 1) % items.length;
    var el = items[cursor];
    el.classList.add("is-focused");
    var d = el.querySelector("details");
    if (d) d.open = true;
    var top = el.getBoundingClientRect().top + window.pageYOffset - 140;
    window.scrollTo({{ top: top, behavior: "smooth" }});
    var goBtn = document.getElementById("finalize-go");
    if (goBtn) goBtn.textContent = (cursor + 1) + " / " + items.length + "  다음 →";
  }};
  // 지난 회차의 "확인했어요" 기록은 다시 쓸 일이 없으니 정리한다.
  for (var i = localStorage.length - 1; i >= 0; i--) {{
    var key = localStorage.key(i);
    if (key && key.indexOf("finalizeBannerDismissed:") === 0 && key !== dismissKey) {{
      localStorage.removeItem(key);
    }}
  }}
}})();
// [추가: 2026-09-03] 상단 "앞 회차 기사 N건" 배너의 [N건] 버튼 — 누르면 첫 해당 기사로
// 간다(초안의 [미분류 N건] 배지와 같은 동작). 여러 번 누르면 다음 것으로 순서대로 넘어가
// 흩어져 있어도 다 찾을 수 있다 — jumpToNextFinalizeAdded와 같은 방식.
// 대상은 서버가 구운 번호가 아니라 **누르는 시점의 DOM**에서 찾는다(소제목 순서가 바뀌거나
// 기사를 숨겨도 그대로 동작한다).
(function () {{
  var lateCursor = -1;
  window.jumpToLatePickup = function () {{
    var items = Array.prototype.slice.call(document.querySelectorAll(".late-badge"));
    if (!items.length) return;
    lateCursor = (lateCursor + 1) % items.length;
    var el = items[lateCursor].closest(".article") || items[lateCursor];
    var top = el.getBoundingClientRect().top + window.pageYOffset - 140;
    window.scrollTo({{ top: top, behavior: "smooth" }});
  }};
}})();
// [추가: 2026-08-05] promoteManualArticle(To Draft)이 남겨둔 "방금 승격한 URL"이 있으면
// 그 기사 카드를 한 번 표시하고 지운다(한 번 보고 나면 사라져야, 나중에 다른 이유로
// 새로고침했을 때 엉뚱하게 계속 표시되지 않는다).
(function() {{
  var justPromotedUrl = sessionStorage.getItem("justPromotedUrl");
  if (!justPromotedUrl) {{ return; }}
  sessionStorage.removeItem("justPromotedUrl");
  var el = document.querySelector('.article[data-url="' + CSS.escape(justPromotedUrl) + '"]');
  if (el) {{ el.classList.add("just-promoted"); }}
}})();
// [추가: 2026-08-05] bulkMoveOrder가 남겨둔 "이동 직전 체크했던 URL들"을 다시 체크해
// 준다 — 일괄 위/아래 이동은 목표 위치까지 여러 번 눌러야 하는 경우가 많은데,
// 새로고침 때마다 체크가 풀리면 그때마다 다시 선택해야 해서 불편했다.
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
// [추가: 2026-08-05] moveArticle/moveArticleToGroup/bulkMoveSelected가 남겨둔 "방금
// 옮긴 기사들"을 한 번 표시한다 — 소제목 미니 목차에서 방금 옮긴 소제목을 표시하는 것과
// 같은 이유로, 기사도 옮기고 나면 어떤 게 방금 움직였는지 한눈에 보여야 헷갈리지 않는다.
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
// [추가: 2026-08-05] 남겨둔 신호가 있으면 목차 팝오버를 다시 열어둔다 — ☰ 버튼을 매번
// 다시 누르지 않아도 되게.
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


def apply_line_template(template: str, outlet: str, title: str) -> str:
    """{outlet}·{title} 자리표시자를 치환한다 (PRD.md 기능1 규칙 5).

    str.format이 아니라 단순 문자열 치환(.replace)을 쓴다 — outlet/title 값이나
    template 자체에 우연히 중괄호가 들어 있어도 format 필드로 잘못 해석되거나
    깨지지 않는다(archive/DESIGN.md §3).
    """
    return template.replace("{outlet}", outlet).replace("{title}", title)


def apply_subheading_format(template: str, section_name: str) -> str:
    """{section} 자리표시자를 치환한다 (메일머지 기능 두 번째 항목, app.settings.

    validate_subheading_format_template 참고). apply_line_template과 같은 이유로
    str.format이 아니라 단순 문자열 치환을 쓴다. HTML로 쓰는 호출부는 apply_line_template과
    동일한 관례로 template을 먼저 html.escape한 뒤 넘겨야 한다(기본값 "<{section}>"의
    꺾쇠가 그대로 HTML 태그로 오인되는 걸 막기 위해) — 순수 텍스트(복사/다운로드/
    텔레그램·이메일) 호출부는 escape 없이 그대로 쓴다.
    """
    return template.replace("{section}", section_name)


def format_slot_time_kr(hhmm: str) -> str:
    """"HH:MM"을 "9시"/"9시 30분"/"12시"처럼 자연스러운 한국어 시각 표현으로 바꾼다.

    [추가: 2026-07-29] 헤더 "언론 모니터링 [HH:MM] 기준"의 워딩 변경 요청 — 시는 앞자리
    0을 없애고(09 -> 9), 분이 0이면 "0분"을 아예 생략한다("09:00" -> "9시", "09:30" ->
    "9시 30분", "12:00" -> "12시"). 파일명(export_filename)처럼 정렬·고유성이 필요한
    곳에는 이 표현을 쓰지 않고 원본 "HH:MM"을 그대로 쓴다.
    """
    hour_str, _, minute_str = hhmm.partition(":")
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    return f"{hour}시" if minute == 0 else f"{hour}시 {minute}분"


def _format_pub_time(article: dict, show_relative: bool = True) -> str:
    """게시 시각을 "HH:MM · N분 전" 형태로 화면에만 보여준다(순서 조정할 때 어느
    기사가 더 최근인지 참고용, 복사/내보내기 텍스트에는 안 들어간다 — _build_plain_text가
    이 값을 아예 안 쓰므로 자동으로 제외된다).

    [수정: 2026-07-27] "게시" 라벨은 없앴다 — 이 화면엔 다른 종류의 시각이 섞여 있지
    않아 라벨 없이도 무슨 시각인지 헷갈리지 않고, 기사마다 반복되니 자리만 차지한다는
    피드백(숨긴 기사 관리 화면에서 이미 같은 이유로 라벨 없이 시각만 보여주기로 한
    선례와도 일치한다).
    - "HH:MM"은 고정값이라 자동 새로고침으로 정적 파일을 다시 읽기만 해도 안 틀어진다.
    - "N분 전"은 페이지가 열릴 때마다(60초 자동 새로고침 포함) 브라우저에서 자바스크립트로
      다시 계산한다(renderRelativeTimes, live.html과 같은 방식) — 서버가 값을 굳혀두면
      시간이 지날수록 틀린 값이 되므로, data-pub-date만 실어두고 텍스트는 JS가 채운다.
    - pub_date가 없는 기사(이 필드 도입 전에 저장된 지난 회차 등)는 조용히 표시를 생략한다.

    show_relative: [추가: 2026-08-12] "지난 기사 더보기"(history_renderer.py)는 이미 다
    끝난 회차를 보는 화면이라 "N분 전"이라는 신선도 표현 자체가 안 맞는다(그 화면은
    renderRelativeTimes JS도 아예 안 불러와서, 지금까지는 이 값 없이 가운데점만 덩그러니
    남아 있었다 — 사실상 절반만 구현된 상태였다). False면 가운데점과 상대시각 span을
    통째로 빼고 "HH:MM"만 보여준다.
    """
    pub_date = article.get("pub_date")
    if not pub_date:
        return ""
    try:
        parsed = datetime.fromisoformat(pub_date)
    except ValueError:
        return ""
    if not show_relative:
        return f'<span class="pub-time">{parsed.strftime("%H:%M")}</span>'
    escaped_pub_date = html.escape(pub_date)
    return (
        f'<span class="pub-time">{parsed.strftime("%H:%M")} · '
        f'<span class="pub-time-relative" data-pub-date="{escaped_pub_date}"></span></span>'
    )


def render_article(
    article: dict,
    highlight_words: list,
    line_template: str = DEFAULT_ARTICLE_LINE_TEMPLATE,
    move: Optional[dict] = None,
    move_handler: str = "moveArticle",
    up_handler: Optional[str] = None,
    # [수정: 2026-08-14] ↑/↓가 소제목 안에서만 움직인다는 걸 툴팁으로 못박고, 소제목을
    # 넘기는 방법(드롭다운)을 같이 알려준다 — 예전 동작("끝에서 한 번 더 누르면 옆
    # 소제목으로")을 기억하는 담당자가 그대로 눌러보고 헤매지 않도록.
    up_title: str = "소제목 안에서 위로 이동 (다른 소제목으로 옮기려면 '다른 소제목' 드롭다운)",
    down_title: str = "소제목 안에서 아래로 이동 (다른 소제목으로 옮기려면 '다른 소제목' 드롭다운)",
    extra_buttons_html: str = "",
    checkbox: bool = False,
    group_select_html: str = "",
    outlet_rank: int = 0,
    show_relative_time: bool = True,
    show_label_control: bool = False,
    group_name: str = "",
    known_labels_html: str = "",
    scrap_date: str = "",
    scrap_end: str = "",
    show_matched_keywords: bool = False,
    # [추가: 2026-09-16] 정기 보관함처럼 한 페이지에 기사가 수백 건인 화면을 위해, 라벨
    # 저장소를 기사마다 다시 읽지 않고 페이지 단위로 한 번 읽은 것을 넘겨받는다
    # (known_url_labels_lookup이 원래 이 용도로 만들어져 있었다). 실측: 504건 화면에서
    # 파일을 504번 읽으면 83ms — 라벨이 늘수록 같이 커진다. 안 넘기면 예전 그대로.
    labels_lookup: Optional[dict] = None,
) -> str:
    """기사 한 건을 렌더링한다.

    제목 줄은 <details>/<summary>로 감싸 클릭하면 저장된 요약이 펼쳐지고(PRD 규칙13,
    자바스크립트 없이 HTML 기본 기능만 사용), URL은 새 탭으로 여는 링크다(PRD 규칙14).
    [수정: 2026-07-25] 제목·펼쳐지는 요약 둘 다에 형광펜 단어 하이라이트를 적용한다
    (PRD 규칙6) — 목록을 훑을 때 제목만 보고도 바로 눈에 띄는 게 낫다는 요청. 같은
    단어는 제목/요약 어디서든 항상 같은 색이다(app.highlight.highlight_keywords).
    highlight_words는 검색 키워드와 무관한, 설정 화면에서 별도로 지정하는 값이다.
    line_template은 규칙5 형식("ㅇ (언론사) 제목")을 복사·다운로드·발송 텍스트(copy_line)에만
    적용한다 — 화면 제목 줄(line_html)은 2026-08-19부터 고정 "(언론사) 제목" 형식이라
    line_template과 무관하다(둘 다 화면·전송 결과가 항상 일치해야 하는 다른 요소들과 달리,
    "ㅇ " 글머리 기호는 화면에서만 자리를 잡아먹을 뿐 정보가 없어 화면에서만 뺐다).

    move: {"disable_up": bool, "disable_down": bool} — 소제목 안에서 위/아래로 옮기는
    ↑/↓ 버튼을 보여준다(규칙21). None이면 버튼 자체를 안 보여준다 (지난 기사 화면·
    "직접 추가한 기사" 구획처럼 "그룹 내 순서"라는 개념이 없는 곳에서는 의미가 없다).
    move_handler/up_handler: ↑/↓ 버튼이 호출할 JS 함수 이름(up_handler를 생략하면
    move_handler와 같다).
    extra_buttons_html: [추가: 2026-07-28] 액션 줄(.article-actions) 맨 앞에 끼워 넣을
    추가 조작 HTML — "담아둔 기사" 구획(_render_manual_section)이 기사마다 승격 조작
    (화면별 소제목 드롭다운, 소제목이 없으면 📥 버튼)을 붙이는 데 쓴다.
    [수정: 2026-09-21] 기사 아래 별도 줄(.article-footer)에 있던 것을 액션 줄로 올렸다 —
    그 조작 하나 때문에 담아둔 기사만 행이 3줄이었고(다른 기사는 2줄), 같은 성격의
    "다른 소제목" 드롭다운(group_select_html)이 이미 액션 줄에 앉아 있어 자리가 갈릴
    이유도 없었다. 이제 그 바로 앞 자리에 같은 규격으로 붙는다. 이 인자가 비면
    액션 줄은 예전과 완전히 같다.

    checkbox: [추가: 2026-07-30] 기사 앞에 다중 선택용 체크박스를 보여준다 — 스크랩
    초안(preview.html)의 "여러 개 선택해서 한 번에 다른 소제목으로 옮기기" 하단 바가
    쓴다. 기본은 꺼짐(다른 화면엔 필요 없음).
    group_select_html: [추가: 2026-07-30] 🗑️ 앞에 끼워 넣을 "다른 소제목으로" 드롭다운
    HTML — 소제목 하나만 있거나 이동할 곳이 없으면 호출하는 쪽이 빈 문자열을 넘겨
    아예 안 보이게 한다.
    outlet_rank: [추가: 2026-08-04] "언론사순" 보기 전환(applyViewMode)이 정렬 기준으로
    쓰는 값 — 호출하는 쪽이 이미 언론사 우선순위로 정렬된 회차 전체 기사 목록에서의
    인덱스를 넘긴다. 기본값 0은 이 기능이 필요 없는 화면(지난 기사·직접 추가한 기사 등)의
    호출부에 영향 없다.
    show_relative_time: [추가: 2026-08-12] 게시 시각 뒤에 "· N분 전"을 붙일지 — 지난
    기사 화면(app.history_renderer)은 신선도 개념이 없는 과거 기록이라 False로 넘겨
    HH:MM만 보여준다(app.renderer._format_pub_time 참고).

    show_label_control: [추가: 2026-08-18] 🏷 라벨 버튼·붙은 라벨 줄을 보여줄지 (PRD.md
    기능10 규칙8 — 라벨을 붙이는 화면은 확정본·초안·수시 수집 확정본, [추가: 2026-09-16]
    그리고 **정기 보관함** 넷이다). 기본 False라서 호출부가 이 인자를 안 넘기면 지금과
    똑같이 아무 것도 안 그려진다(회귀 없음). group_name/scrap_date/scrap_end는
    show_label_control=True일 때만 의미가 있고, 라벨을 새로 붙이는 요청에 실어 보낼
    스냅샷 필드다(group_name은 "라벨 붙이는 시점의 소제목" — app.labels.attach_label
    참고). known_labels_html은 팝오버의 "이미 쓴 라벨" 칩 목록 — 기사마다 다시 계산하면
    낭비이므로 호출부(_render_groups 등)가 한 번만 만들어 그대로 넘긴다.

    show_matched_keywords: [추가: 2026-08-21] 이 기사가 어떤 검색어로 걸렸는지를 액션
    줄에 "검색어 기재부, 정부"로 보여줄지 (article["matched_keywords"] — app.scraper.
    collect_run이 수집 시점에 저장해 둔 값). 기본 False라 지난 기사(app.history_renderer)·
    "담아둔 기사" 구획처럼 이 인자를 안 넘기는 호출부는 지금과 완전히 같다(회귀 없음).
    값이 없는 기사(이 기능 이전에 저장된 회차, 직접 추가한 기사)는 True로 넘겨도 아무
    것도 안 그린다 — 없는 값을 지어내지 않는다.

    [수정: 2026-07-28] 🗑️ 숨기기 버튼을 하단 footer에서 제목 줄(summary) 옆으로
    옮겼다 — 요약을 펼치면 footer가 아래로 밀려나면서 숨기기 버튼이 제목에서 멀어져,
    기사가 많을 때 어떤 버튼이 어떤 기사 것인지 헷갈린다는 피드백에 따른 것. summary
    안에 버튼을 넣으면 클릭이 위(펼치기)로도 번질 수 있어 JS에서 stopPropagation으로
    막는다(hideArticle 자체는 그대로, 호출 전에 이벤트 전파만 끊는다).

    [수정: 2026-08-13] summary를 1행(제목만 있던 flex 한 줄)에서 2행(.title-row/
    .action-row)으로 나눴다 — 게시시각·드롭다운·⋯·🗑️·↑·↓가 제목과 한 줄을 다투면서
    제목이 쓸 수 있는 폭이 카드의 절반 남짓으로 줄어 40자 안팎 제목도 쉽게 2~3줄로
    꺾이던 문제(실측 스크린샷 기준) 때문. 1행은 체크박스+제목(전체 폭), 2행은
    게시시각(좌)+액션 묶음(우, 기존 .article-actions의 margin-left:auto가 그대로
    오른쪽 끝에 붙여준다). URL 줄(.article-footer의 <a class="url">)은 화면에서
    완전히 제거하고 "원문보기" 링크(.origin-link-text, [수정: 2026-08-14] 게시시각 옆
    텍스트로 재배치 — 아래 함수 본문 참고)로 대체했다 — 밑줄 텍스트 URL이
    화면을 산문하게 만들고 줄바꿈·잘림이 잦다는 지적. 같은 이유로 "복사하기"도
    "⋯ 더보기" 메뉴에서 꺼내 상시 노출 아이콘으로 승격했다 — URL이 화면에 안 보이면
    이게 화면에서 클립보드로 가는 유일한 경로가 되므로 🗑️·원문보기와 같은 급이다.
    액션 순서는 다른 소제목 → 원문보기 → 복사하기 → ⋯ → 🗑️ → ↑ → ↓ (기존 코드가
    이미 다른 소제목을 맨 앞에 두던 순서를 그대로 유지, 원문보기·복사하기만 그 뒤에
    끼워 넣었다). [수정: 2026-09-21] 항상 비어 있던 .article-footer div는 없앴다 —
    유일한 내용이던 extra_buttons_html이 액션 줄로 올라가면서 남길 이유가 사라졌다.
    """
    outlet = html.escape(article["outlet"])
    # [추가: 2026-07-29] 화면 표시용 언론사 이름만 outlet_display_label을 거친다(매일경제만
    # 빨간 글자로, 매경이코노미와 안 구분되는 걸 화면에서 알려주는 표식) — data-outlet
    # 속성(위 outlet)은 원본 그대로 둬서 숨긴 기사 메타데이터 등이 canonical 값을 유지한다.
    # [수정: 2026-07-30] outlet_display_label이 이제 이미 이스케이프된 HTML 조각(색
    # span 포함)을 돌려주므로 여기서 다시 html.escape()하면 안 된다(<span> 태그가
    # 그대로 글자로 보이게 됨).
    # [수정: 2026-08-14] 기사 URL을 함께 넘긴다 — 네이버 oid로 언론사가 확정된 기사는
    # 빨간 표식 없이 그냥 "매일경제"로 보인다(outlet_display_label 참고).
    outlet_display = outlet_display_label(article["outlet"], article.get("url"))
    # [추가: 2026-08-20] 제목 맨 앞 [단독]/[속보] 말머리는 배지로 감싸지 않고 글자색만
    # 바꾼다 — app.live_renderer와 같은 방식(제목 문자열 자체는 그대로 두므로 복사/
    # 다운로드/발송 텍스트엔 영향 없음, CODING_CONVENTIONS.md §4). 말머리 구간과
    # 나머지 구간을 나눠 각자 highlight_keywords를 태워, 형광펜 하이라이트와 겹칠 때도
    # 서로 안 지운다. [단독]은 이 소제목 안에서 이미 맨 위로 정렬돼 있으므로(app.filters.
    # sort_scoop_first, app.scraper.collect_run/app.preview_renderer 호출부) 카드 자체도
    # 강조한다(art-scoop) — 여러 건 나올 수 있는 [속보]는 자리는 그대로 두고 글자색만.
    _kind = headline_kind(article["title"])
    if _kind:
        _prefix_len = HEADLINE_TAG_RE.match(article["title"]).end()
        _headline_cls = "t-scoop" if _kind == "단독" else "t-flash"
        title = (
            f'<span class="{_headline_cls}">'
            f'{highlight_keywords(article["title"][:_prefix_len], highlight_words)}</span>'
            f'{highlight_keywords(article["title"][_prefix_len:], highlight_words)}'
        )
    else:
        title = highlight_keywords(article["title"], highlight_words)
    title_attr = html.escape(article["title"])
    pub_date_attr = html.escape(article.get("pub_date") or "")
    url = html.escape(article["url"])
    summary_html = highlight_keywords(article.get("summary", ""), highlight_words)
    # [수정: 2026-08-19] 화면 제목 줄은 더 이상 line_template(복사·다운로드·발송 전용)을
    # 따르지 않는다 — "ㅇ " 글머리 기호는 화면에서 정보 없이 자리만 차지해서 뺐다.
    # 괄호는 남긴다: 저장된 기사 제목의 약 25%가 [단독]/[속보]/따옴표 등 기호로
    # 시작해(2026-08-19 실측), 괄호가 없으면 언론사와 제목 경계가 눈에 안 들어온다.
    # [수정: 2026-09-11] 괄호도 뺐다 — 경계는 문장부호 대신 색·크기가 나눈다(.title-outlet,
    # 회색·조금 작은 글자, 시안 OUTLET_TITLE_MOCKUP.html A안). 복사·발송 텍스트는 위
    # line_template로 따로 만들므로 여기는 화면 전용이다.
    line_html = f'<span class="title-outlet">{outlet_display}</span>{title}'
    move_html = ""
    if move is not None:
        up_disabled = " disabled" if move.get("disable_up") else ""
        down_disabled = " disabled" if move.get("disable_down") else ""
        # hidden: 자리(폭)는 남기고 안 보이게, 누를 수도 없게 한다(📂 소제목 미분류).
        if move.get("hidden"):
            up_disabled = down_disabled = ' disabled aria-hidden="true" tabindex="-1" style="visibility:hidden"'
        effective_up_handler = up_handler or move_handler
        # [수정: 2026-07-30] ↑/↓가 summary 안으로 옮겨오면서, 🗑️와 같은 이유로
        # stopPropagation이 필요해졌다 — 없으면 클릭이 위(펼치기 토글)로도 번진다.
        move_html = (
            f'<button class="move-btn" type="button" data-url="{url}" data-direction="up" '
            f'onclick="event.stopPropagation(); {effective_up_handler}(this);"'
            f'{up_disabled} title="{up_title}">{icon("up")}</button>'
            f'<button class="move-btn" type="button" data-url="{url}" data-direction="down" '
            f'onclick="event.stopPropagation(); {move_handler}(this);"'
            f'{down_disabled} title="{down_title}">{icon("down")}</button>'
        )
    hide_btn_html = (
        f'<button class="hide-btn" type="button" data-url="{url}" data-outlet="{outlet}" '
        f'data-title="{title_attr}" data-pub-date="{pub_date_attr}" '
        f'data-group="{html.escape(group_name, quote=True)}" '
        f'onclick="event.stopPropagation(); hideArticle(this);" '
        f'title="이 기사 숨기기 (되돌리기 가능)">{icon("trash")}</button>'
    )
    # [추가: 2026-08-10] 기사 전체가 아니라 이 기사 한 건만 복사해야 할 때(가끔 1개
    # 기사만 보고하는 경우) — 🔄/✏️와 같은 자리·같은 숨김 방식(.refetch-btn, 평소엔
    # 안 보이다가 카드에 마우스를 올리면 나타남)을 그대로 써서, 기본 화면은 지금처럼
    # 깔끔하게 두면서도 아이콘을 더 늘리지 않는다. 복사 본문은 전체 복사(PLAIN_TEXT)와
    # 같은 줄 형식("ㅇ (언론사) 제목" + URL)이라 붙여넣었을 때 형식이 어긋나지 않는다.
    copy_line = apply_line_template(line_template, article["outlet"], article["title"])
    copy_text_attr = html.escape(f"{copy_line}\n{article['url']}")
    # [추가: 2026-08-13, 수정: 2026-08-14] 원문보기 — URL 줄(밑줄 텍스트)이 화면에서
    # 사라지며 그 역할을 대신한다. 새 탭 이동은 JS 없이 <a target=_blank>가 그대로
    # 담당(PRD 규칙14). [수정: 2026-08-14] 아이콘 단독 버튼이었다가 복사 아이콘과
    # 구분이 안 된다는 지적으로 "원문보기" 글자 링크로 바꿨다(강조색 — 아이콘 하나일 땐
    # 무채색이어도 됐지만 글자는 무채색이면 그냥 설명문처럼 보여 눌러도 되는지 알기
    # 어렵다). 게시시각 옆(action-row 왼쪽)으로 자리도 옮겼다 — 아래 time_row_html 참고.
    origin_link_html = (
        f'<a class="origin-link-text" href="{url}" target="_blank" rel="noopener noreferrer" '
        f'title="{url}">원문보기{icon("external_link", "ic origin-link-icon")}</a>'
    )
    # 복사하기는 "⋯ 더보기" 메뉴 첫 항목이다 — 한 건만 복사하는 건 가끔 하는 일인데
    # 모든 행에 아이콘이 붙어 있으면 목록이 어수선하다. 메뉴는 누르는 순간 닫히므로
    # 피드백은 ⋯ 아이콘 자체가 1초간 ✓로 바뀌는 것으로 준다(copyArticleIcon의 두 번째
    # 인자, CSS .more-btn.is-copied). 원문 다시 불러오기·기사제목 직접 수정과 함께
    # "가끔 쓰는, 한 번 누르면 끝나는" 동작이다. 🗑·↑↓는 자주 쓰므로 메뉴 밖에 둔다.
    more_menu_html = (
        '<span class="more-wrap">'
        '<button class="more-btn" type="button" '
        'onclick="event.stopPropagation(); toggleArticleMenu(this);" '
        'title="더보기" aria-label="더보기">'
        f'<span class="icon-default">{icon("dots")}</span>'
        f'<span class="icon-done">{icon("check")}</span></button>'
        '<span class="more-menu">'
        f'<button type="button" data-copy-text="{copy_text_attr}" '
        'onclick="event.stopPropagation(); closeArticleMenus(); '
        "copyArticleIcon(this, this.closest('.more-wrap').querySelector('.more-btn'));\">복사하기</button>"
        f'<button type="button" data-url="{url}" '
        'onclick="event.stopPropagation(); closeArticleMenus(); refetchSummary(this);">원문 다시 불러오기</button>'
        f'<button type="button" data-url="{url}" '
        f'data-title="{title_attr}" data-summary="{html.escape(article.get("summary", ""))}" '
        'onclick="event.stopPropagation(); closeArticleMenus(); editSummary(this);">기사제목 직접 수정</button>'
        "</span>"
        "</span>"
    )
    # [수정: 2026-07-30] 게시 시각과 🗑️/↑/↓를 전부 제목 줄(summary)로 옮겼다 — 예전엔
    # footer(URL 옆)에 있었는데, 요약을 펼치면 footer가 아래로 밀려나면서 "게시 시각이
    # URL 쪽으로 이동한 것처럼" 보여 헷갈린다는 피드백. 이제 제목만 보고 언제·뭘 할지
    # 바로 판단할 수 있다. 순서는 [🗑️][↑][↓] — "먼저 남길지 거를지 정하고(🗑️), 나중에
    # 순서를 정리한다(↑↓)"는 실제 작업 흐름 그대로다.
    # [추가: 2026-07-30] 체크박스는 summary 맨 앞에 둔다.
    # [수정: 2026-08-13] summary가 2행으로 나뉘며 정확히는 "1행(.title-row) 맨 앞"이
    # 됐다 — 제목과 같은 줄이라는 원래 의도는 그대로 유지. 클릭이 위(펼치기)로 안
    # 번지게 stopPropagation.
    checkbox_html = (
        f'<input type="checkbox" class="article-select" data-url="{url}" '
        'onclick="event.stopPropagation();" onchange="updateBulkMoveBar();">'
        if checkbox
        else ""
    )
    # [추가: 2026-08-21] 이 기사가 어떤 검색어로 걸렸는지 — 게시시각·원문보기와 같은
    # 줄, 같은 회색 톤이다("담당자가 누르는 것"이 아니라 참고용 메타 정보라는 뜻).
    # 칩(테두리 알약)으로 만들지 않은 이유: 확정본 한 회차가 50~100건인데 행마다 알약이
    # 붙으면 목록을 훑을 때 시선을 뺏는다.
    # [수정: 2026-08-21] 앞머리를 "검색어"라는 글자 라벨에서 돋보기 아이콘으로 바꿨다.
    # 처음엔 "아이콘만으로는 검색어라는 뜻이 안 통한다"고 보고 글자를 뒀는데, 실제 화면을
    # 띄워보니 같은 세 글자가 매 행 반복되며 정작 값(검색어)보다 먼저 눈에 걸렸다(사용자
    # 지적). 이 앱엔 이미 같은 관례도 있다 — 수시 수집 확정본 화면 상단 메타 칩이
    # app/adhoc/renderer.py에서 `icon("search") + 검색어들` 형태를 쓴다. 설명을 없앤 게
    # 아니라 title 툴팁으로 옮겼다(평소엔 안 보이지만 필요할 때 확인 가능한 자리).
    # [수정: 2026-09-14] 처음(2026-08-21)엔 "가장 긴 줄이 26자라 액션 줄을 안 밀어낸다"고
    # 보고 전부 보여줬는데, 검색어가 늘며 그 전제가 깨졌다(최근 305건: 최대 38자, 20자 초과
    # 17건) — 긴 행에서 버튼 묶음이 통째로 아랫줄로 밀렸다. 지금은 남는 자리만큼만 보여주고
    # 나머지는 …로 자른다(CSS·툴팁은 kw_inline_style/kw_inline_script). 구분점(·)을 span
    # 안에 넣은 건 좁은 화면에서 검색어가 다음 줄로 넘어갈 때 점만 윗줄 끝에 남지 않게 하려는 것.
    # 제목 문자열에 섞지 않고 별도 <span>이라 복사·.txt·발송 텍스트엔
    # 안 나간다(CODING_CONVENTIONS.md §4 — 그쪽은 _build_plain_text가 outlet/title/url
    # 세 값만 갖고 새로 조립하므로 화면에 뭘 붙이든 샐 길이 없다).
    kw_inline_html = ""
    if show_matched_keywords:
        matched = [k for k in (article.get("matched_keywords") or []) if k]
        if matched:
            kw_inline_html = (
                '<span class="kw-inline" title="이 기사가 수집된 검색어입니다">'
                '<span class="time-sep">·</span>'
                f'{icon("search", "ic kw-ic")}'
                f'<span class="kw-v">{html.escape(", ".join(matched))}</span></span>'
            )
    # [수정: 2026-08-21] 표식 없는 통신사 사진기사 추정 배지 — 실시간 현황과 같은
    # "아이콘+문구" 칩으로 바꿨다. 예전엔 "이 배지는 평소 거의 안 보인다"는 전제로
    # 아이콘만 쓰고 설명은 툴팁으로 뺐는데, 사진기사 제외 설정을 기본 끔으로 바꾸면서
    # (아래 article_class의 is-photo 참고) 그 전제가 깨졌다 — 목록의 절반 가까이가
    # 이 배지를 달 수 있는 상황에서는 문구가 바로 보여야 한다.
    is_photo = looks_like_photo_caption(article)
    # [추가: 2026-09-03] 같은 배지가 두 층을 겸하게 되면서(표식 / 추정) 툴팁만 갈라
    # 놓는다 — "표식은 없지만 추정"이라는 옛 문구는 [포토]가 붙은 기사에는 거짓말이다.
    # [수정: 2026-09-15] 문구는 app.filters.photo_badge_tip 한 곳에서 — 실제 붙은 표식을 적는다.
    photo_badge_html = (
        f'<span class="photo-badge" title="{html.escape(photo_badge_tip(article.get("title", ""), article.get("url", "")))}">'
        f'{icon("camera")} 사진 추정</span>'
        if is_photo
        else ""
    )
    # [추가: 2026-08-20] 초안 마감 시점에 방금 자동으로 소제목에 배정된 기사 — 담당자가
    # 초안에서 못 본 기사일 수 있다(app.scraper.collect_run이 article["finalize_auto_
    # assigned"]로 영구히 남긴 값, 지우는 로직 없음 — 계속 표시). 음영은 CSS
    # .article.finalize-added, 배지는 여기 별도 <span>(이름 문자열에 안 섞음 —
    # 복사/다운로드/발송 텍스트에 안 새어야 하므로 다른 배지들과 같은 원칙).
    finalize_added = bool(article.get("finalize_auto_assigned"))
    finalize_badge_html = (
        f'<span class="finalize-added-badge" title="초안 마감 시점에 자동으로 이 소제목에 배정됐습니다">'
        f'{icon("bot")} 마감 후 자동 배정</span>'
        if finalize_added
        else ""
    )
    # [추가: 2026-09-03] 회차 마감 순간 네이버 검색이 아직 안 줘서 다음 회차가 주워 담은
    # 기사(app.scraper.collect_run의 late_pickup). 제목 줄이 아니라 메타 줄에 붙인다 —
    # 위 finalize_badge_html(🤖)과 같은 기사에 함께 뜨는 게 보통이라 자리를 갈라둔다.
    # 이름 문자열에 안 섞고 별도 <span>이라 복사/다운로드/발송 텍스트엔 안 나간다
    # (다른 배지들과 같은 원칙 — CODING_CONVENTIONS.md §4).
    late_slot = article.get("late_pickup_slot") or ""
    late_badge_html = (
        '<span class="late-badge" title="'
        + html.escape(
            (f"{format_slot_time_kr(late_slot)} 회차 시간대 기사인데 " if late_slot else "앞 회차 시간대 기사인데 ")
            + "그때 수집되지 않아 이번 회차에 담겼습니다",
            quote=True,
        )
        # [수정: 2026-09-11] 칩 글자 「앞 회차 기사」 → 「늦게 들어온 기사」(사용자 결정) —
        # 옛 이름이 "이 기사는 앞 회차 소속"으로 읽혀 앞 회차로 옮겨질 것처럼 보였다.
        # 실제로는 이번 회차에 담기는 기사다. 툴팁·배너 문구는 그대로다.
        + '">⏱ 늦게 들어온 기사</span>'
        if article.get("late_pickup")
        else ""
    )
    article_class = "article finalize-added finalize-added-bg" if finalize_added else "article"
    # [추가: 2026-08-20] [단독] 기사는 카드 자체에 강조(테두리+배경)를 준다 — 여러 건
    # 나올 수 있는 [속보]와 달리 글자색만으로는 목록에 묻힐 수 있어서(담당자 지목:
    # "단독이 가장 중요"), 개수와 무관하게 항상 먼저 눈에 걸리는 형태(덩어리)로 준다.
    if headline_kind(article["title"]) == "단독":
        article_class += " art-scoop"
    # [추가: 2026-08-21] 사진 추정 기사는 지우지 않고 행을 흐리게 표시한다(CSS
    # .article.is-photo) — 판정이 틀렸을 때 담당자가 그 자리에서 처리(체크 해제)할 수
    # 있어야 한다는 결정. 마우스를 올리면 다시 밝아진다(아래 CSS). 실시간 현황이 이미
    # 같은 함수를 "지우지 않고 표시만" 하는 데 쓰고 있어(app.filters.looks_like_photo_
    # caption docstring), 그 원칙을 확정본·초안에도 맞춘 것이다.
    if is_photo:
        article_class += " is-photo"
    # [수정: 2026-08-14] 게시시각 옆에 "· 원문보기"를 붙인다 — pub_time_html이 비어있는
    # (pub_date 없는 옛 기사) 경우엔 구분점 없이 원문보기만 남긴다.
    pub_time_html = _format_pub_time(article, show_relative_time)
    time_row_html = (
        f'{pub_time_html}<span class="time-sep">·</span>{origin_link_html}'
        if pub_time_html
        else origin_link_html
    )
    # [추가: 2026-08-18] 🏷 라벨 — ⋯ 더보기 안에 넣지 않는다. 라벨은 "읽으면서 판단하는"
    # 주 동작이라 ⋯(가끔 한 번 쓰는 동작 전용)와 무게가 다르다(2026-08-18 목업 대화 ③).
    # 붙은 라벨은 카드 안 별도 줄(.lab-row)로 보여주고 0개면 그 줄 자체를 안 그려
    # 지금 화면과 완전히 같게 유지한다(A안 채택 — B안의 액션 줄 인라인은 2026-08-13에
    # 제목 줄을 2행으로 쪼갠 이유(폭 다툼)를 그대로 반복하는 구조라 기각).
    label_btn_html = ""
    label_row_html = ""
    if show_label_control:
        current_labels = (
            list((labels_lookup.get(article["url"]) or {}).get("labels", []))
            if labels_lookup is not None
            else labels_for_url(article["url"])
        )
        lab_btn_class = " on" if current_labels else ""
        lab_count_html = f'<span class="lab-count">{len(current_labels)}</span>' if current_labels else ""
        # [수정: 2026-08-18] 라벨 이름을 onclick 안 JS 문자열로 직접 끼워 넣지 않는다 —
        # html.escape는 HTML 파싱만 안전하게 만들 뿐, 그 결과가 브라우저에서 속성값으로
        # 디코딩된 뒤 JS로 실행되는 시점엔 이름에 작은따옴표가 하나만 있어도 문자열이
        # 깨진다. data-label 속성에 담아 JS가 dataset으로 읽게 한다(다른 버튼들이 이미
        # data-url/data-title 등을 쓰는 것과 같은 방식).
        attached_html = (
            "".join(
                f'<span class="lab-chip" data-label="{html.escape(n)}">{html.escape(n)} '
                '<span class="x" onclick="event.stopPropagation(); event.preventDefault(); detachLabelChip(this);">×</span></span>'
                for n in current_labels
            )
            if current_labels
            else '<span class="none">아직 없음</span>'
        )
        label_btn_html = (
            '<span class="lab-pop-wrap">'
            f'<button class="iconbtn lab-btn{lab_btn_class}" type="button" data-url="{url}" '
            f'data-outlet="{outlet}" data-title="{title_attr}" data-pub-date="{pub_date_attr}" '
            f'data-group="{html.escape(group_name, quote=True)}" '
            f'data-scrap-date="{html.escape(scrap_date, quote=True)}" '
            f'data-scrap-end="{html.escape(scrap_end, quote=True)}" '
            'onclick="event.stopPropagation(); event.preventDefault(); toggleLabelPopover(this);" '
            f'title="라벨">{icon("tag")}{lab_count_html}</button>'
            '<span class="lab-pop hidden" onclick="event.stopPropagation(); event.preventDefault();">'
            "<h4>이 기사에 붙은 라벨</h4>"
            f'<div class="attached">{attached_html}</div>'
            "<h4>이미 쓴 라벨 (눌러서 붙이기)</h4>"
            f'<div class="known">{known_labels_html}</div>'
            '<div class="addbox">'
            '<input type="text" placeholder="새 라벨 입력" class="lab-input" '
            "onkeydown=\"event.stopPropagation(); if(event.key==='Enter'){event.preventDefault(); "
            'addLabelFromInput(this);}" oninput="event.stopPropagation(); checkSimilarLabel(this);">'
            '<button type="button" onclick="event.stopPropagation(); event.preventDefault(); '
            'addLabelFromInput(this.previousElementSibling);">추가</button>'
            "</div>"
            '<div class="sim-warn hidden"></div>'
            '<p class="hint">라벨은 보고서 텍스트(복사·txt·발송)에 나가지 않습니다. '
            "화면과 엑셀 내보내기에만 표시됩니다.</p>"
            "</span>"
            "</span>"
        )
        if current_labels:
            label_row_html = f'<div class="lab-row"><span class="tagmark">{icon("tag")}</span>{attached_html}</div>'
    return (
        f'<div class="{article_class}" data-url="{url}" data-outlet-rank="{outlet_rank}">'
        "<details>"
        "<summary>"
        f'<div class="title-row">{checkbox_html}<span class="title-line">{line_html}</span>{photo_badge_html}{finalize_badge_html}</div>'
        '<div class="action-row">'
        # [수정: 2026-09-14] 칩을 검색어 앞으로 — 검색어가 남는 자리만큼 잘리게 되면서, 뒤에
        # 두면 칩의 가로 위치가 검색어 길이만큼 행마다 달라진다(목업 실측 108px 차이).
        # 원문보기보다는 뒤라 누르는 링크 자리는 안 흔들린다. KW_CHIP_MOCKUP.html C안.
        f'{time_row_html}{late_badge_html}{kw_inline_html}'
        f'<span class="article-actions">{extra_buttons_html}{group_select_html}{label_btn_html}'
        f"{more_menu_html}{hide_btn_html}{move_html}</span>"
        "</div>"
        f"{label_row_html}"
        "</summary>"
        f'<p class="article-summary">{summary_html}</p>'
        "</details>"
        "</div>"
    )


def known_label_chips_html() -> str:
    """라벨 팝오버의 "이미 쓴 라벨" 칩 목록 HTML — 건수 많은 순(app.labels.label_stats와
    같은 순서). 기사마다 다시 계산하면 낭비이므로(팝오버 내용은 페이지 전체에서 동일)
    호출부가 한 번만 만들어 render_article마다 그대로 넘긴다. 어떤 칩이 "이미 이 기사에
    붙어 있는지"는 여기서 판단하지 않는다 — 팝오버를 열 때 JS가 같은 카드 안 .attached의
    data-label과 비교해 .used 클래스를 그때그때 매긴다(toggleLabelPopover).
    """
    stats = label_stats()
    if not stats:
        return '<span class="none">아직 쓴 라벨이 없습니다</span>'
    return "".join(
        f'<span class="known-chip" data-label="{html.escape(name)}" '
        'onclick="event.stopPropagation(); event.preventDefault(); knownChipClick(this);">'
        f'{html.escape(name)}<span class="n">{s["count"]}</span></span>'
        for name, s in stats.items()
    )


def _group_select_html(current_name: str, all_names: list, labels: dict, custom_names: Optional[set] = None) -> str:
    """"다른 소제목으로" 드롭다운 — app.preview_renderer._group_select_html과 동일한
    이유·동작(스크랩 초안과 완성본 양쪽 다 자동분류가 완벽하지 않아 여러 개를 바로
    잡아야 하는 경우가 있어 붙였다). 지금 속한 소제목은 옵션에서 빼고, 옮길 곳이
    아예 없으면(소제목이 이거 하나뿐) 빈 문자열을 돌려줘 드롭다운을 숨긴다.

    [추가: 2026-08-07] 자동 분류 소제목과 사용자가 만든 소제목이 텍스트만 봐서는
    구분이 안 돼 헷갈린다는 피드백 — 화면 카드에 이미 쓰는 하늘색 점선 테두리 표식과
    같은 기준(custom_names)으로, 사용자가 만든 소제목만 볼드로 보여준다. 네이티브
    <select>는 배경색·아이콘 같은 꾸밈은 못 넣어도 font-weight 같은 글자 스타일은
    입력해도 되어(대부분의 데스크톱 브라우저에서 실제로 렌더링됨), 이 정도 구분에는
    충분하다.
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


def _group_option_label(name: str, labels: dict) -> str:
    """드롭다운 옵션 글자 — [수정: 2026-08-05] 원래 이름을 괄호로 같이 보여주던 걸
    되돌렸다. 이용자가 이름표를 한 번 바꾸면 그 이후로는 원래 이름을 신경 쓸 일이
    없다는 피드백에 따라 표시 이름 하나만 보여준다(app.renderer._group_select_html/
    app.preview_renderer._group_select_html 공용)."""
    return display_group_name(name, labels)


def _render_groups(
    groups: list, highlight_words: list, line_template: str, labels: dict, rank_by_url: dict, custom_names: set,
    ai_names: set = frozenset(), subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    show_label_control: bool = False, scrap_date: str = "", scrap_end: str = "",
    finalize_new_subheadings: Optional[set] = None,
    group_copy_texts: Optional[dict] = None,
) -> str:
    """소제목별 화면을 렌더링한다.

    ↑/↓ 버튼은 각 소제목 안에서만이 아니라 소제목 경계도 넘나들 수 있어(규칙21),
    맨 처음 소제목의 첫 기사(↑)·맨 마지막 소제목의 마지막 기사(↓)일 때만 버튼을
    비활성화한다. 그 외 소제목 경계에서는 버튼이 계속 활성 상태이고, 클릭하면
    서버(move_article)가 "같은 소제목 내 순서 변경"과 "옆 소제목으로 이동"을 알아서
    구분해 처리한다 — 화면(버튼) 쪽은 이 둘을 구분할 필요가 없다.

    소제목 옆 ✏️ 버튼은 표시 이름만 바꾼다(기능2 규칙 8) — 분류 자체는 항상 원래
    소제목 단어(group["name"])를 기준으로 하므로, 버튼의 data-name에는 원래 단어를
    그대로 담아 서버가 어떤 단어의 이름표를 바꿀지 정확히 알 수 있게 한다.

    [수정: 2026-07-30] 스크랩 초안에 먼저 만든 체크박스 다중 선택 + "다른 소제목으로"
    드롭다운을 완성본에도 그대로 붙였다(app.preview_renderer._render_preview_groups와
    동일 패턴) — 자동분류가 완벽하지 않아 여러 개를 한꺼번에 바로잡아야 하는 경우가
    초안뿐 아니라 완성본에서도 필요해서다. 기사가 0개인 소제목(사용자가 미리 만든
    빈 소제목)은 안내 문구를 보여주고, 헤더의 🗑️도 "삭제"로 동작한다.
    """
    all_names = [g["name"] for g in groups]
    finalize_new_set = finalize_new_subheadings or set()
    sections = []
    # [수정: 2026-09-10] ▼를 잠그는 기준은 "화면의 마지막 소제목"이 아니라 "옮길 수 있는
    # 마지막 소제목"이다 — 그 아래엔 순서가 고정된 기타·📂 미분류만 오므로(apply_group_order),
    # 예전처럼 전체 끝을 기준으로 삼으면 기타 바로 위 소제목의 ▼가 눌려도 아무 일이 없었다.
    orderable_indexes = [i for i, g in enumerate(groups) if not is_order_locked(g["name"], labels)]
    first_orderable_index = orderable_indexes[0] if orderable_indexes else -1
    last_orderable_index = orderable_indexes[-1] if orderable_indexes else -1
    # [추가: 2026-08-18] "이미 쓴 라벨" 칩 목록은 페이지 전체에서 내용이 같으므로 여기서
    # 딱 한 번만 만든다 — 기사 수만큼 label_stats()를 다시 부르는 낭비를 피한다.
    known_labels_html = known_label_chips_html() if show_label_control else ""
    for group_index, group in enumerate(groups):
        original_name = html.escape(group["name"])
        display_name = html.escape(display_group_name(group["name"], labels))
        if not group["articles"]:
            articles_html = (
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
            articles_html = "\n".join(
                render_article(
                    a,
                    highlight_words,
                    line_template,
                    # [수정: 2026-08-14] ↑/↓는 소제목 안에서만 움직이므로, 그 소제목의
                    # 맨 위/아래에서 바로 비활성화한다 — 예전엔 화면 전체의 첫/마지막
                    # 기사에서만 꺼져서, 눌리긴 하는데 아무 일도 안 일어나는(또는 예전엔
                    # 옆 소제목으로 넘어가버리던) 구간이 생겼다.
                    # 📂 소제목 미분류는 배정 전 임시 칸이라 순서를 맞출 일이 없다 — ↑↓를
                    # 자리만 남기고 숨긴다(옆 아이콘 위치가 다른 소제목 행과 같게).
                    move={
                        "disable_up": i == 0,
                        "disable_down": i == last_index,
                        "hidden": group["name"] == UNCLASSIFIED_GROUP_NAME,
                    },
                    checkbox=True,
                    group_select_html=group_select,
                    outlet_rank=rank_by_url.get(a["url"], 0),
                    show_label_control=show_label_control,
                    group_name=group["name"],
                    known_labels_html=known_labels_html,
                    scrap_date=scrap_date,
                    scrap_end=scrap_end,
                    show_matched_keywords=True,
                )
                for i, a in enumerate(group["articles"])
            )
            delete_btn = (
                '<button class="rename-btn group-hide-btn" type="button" onclick="hideGroup(this)" '
                f'title="이 소제목 기사 {len(group["articles"])}건 통째로 숨기기(기사 각각 숨긴 것과 동일 — 되돌리기 가능)">{icon("trash")}</button>'
            )
        # [추가: 2026-08-03] 소제목 자체의 화면 순서를 ↑/↓로 바꾼다(기사 순서와는 별개) —
        # app.group_order.save_group_order가 저장을 맡고, 여기서는 맨 처음/마지막
        # 소제목일 때만 버튼을 비활성화한다(기사 ↑/↓와 동일한 관례).
        # [수정: 2026-09-10] 기타는 맨 뒤 고정이라(app.group_order._lock_rank — 화면 이름 기준)
        # ▲▼를 아예 안 그린다 — 📂 미분류에서 ▲▼를 뺀 것과 같은 이유.
        order_locked = is_order_locked(group["name"], labels)
        order_up_disabled = " disabled" if group_index <= first_orderable_index else ""
        order_down_disabled = " disabled" if group_index >= last_orderable_index else ""
        order_html = "" if order_locked else (
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'up\');"{order_up_disabled} title="소제목 위로 이동">{icon("tri_up")}</button>'
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'down\');"{order_down_disabled} title="소제목 아래로 이동">{icon("tri_down")}</button>'
        )
        # [추가: 2026-08-04] 소제목 안 기사를 한 번에 전부 체크(다른 소제목으로 일괄
        # 이동용) — 기사가 없는 소제목엔 체크할 대상이 없으니 렌더링하지 않는다.
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
            # [수정: 2026-08-26] 앞의 📂 이모지를 떼고 같은 뜻의 단색 SVG를 붙인다
            # (app/preview_renderer.py의 같은 자리와 동일). 저장값은 그대로 둔다.
            title_html = (
                f'<span class="subheading-title unclassified-title">{select_all_html}'
                f'{icon(UNCLASSIFIED_ICON)}{UNCLASSIFIED_DISPLAY_TEXT} '
                f'<span class="unclassified-count">{len(group["articles"])}건</span></span>'
            )
        else:
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
            # [추가: 2026-08-20] 초안 마감 시점에 새로 생긴 소제목 — 이름을 바꿔도(✏️)
            # 계속 보여준다(원래 단어 group["name"] 기준이라 ai_badge와 달리
            # display_name == original_name 조건을 안 건다 — "이 소제목이 마감 시
            # 새로 생겼다"는 사실은 이름과 무관하게 유효하다).
            new_subheading_badge = (
                '<span class="new-subheading-badge" '
                'title="초안 마감 시점에 새로 만들어진 소제목입니다">마감 시 신규</span>'
                if group["name"] in finalize_new_set
                else ""
            )
            # [추가: 2026-08-12] 메일머지 "소제목 형식" — h2에 보이는 이름에만 감싸고,
            # data-current(rename 프롬프트 프리필)·중복이름 검사에 쓰이는 원본 display_name은
            # 그대로 둔다(편집 대상은 항상 가공 전 이름이어야 한다).
            formatted_name = apply_subheading_format(html.escape(subheading_format), display_name)
            count_html = f'<span class="subheading-count">{len(group["articles"])}</span>'
            title_html = (
                f'<span class="subheading-title">{select_all_html}{formatted_name}{ai_badge}'
                f'{new_subheading_badge}{count_html}</span>'
            )
        # [수정: 2026-08-21] 임시 보관함(📂 소제목 미분류)은 진짜 소제목이 아니라서
        # 이름 바꾸기/순서 이동/폴더 삭제라는 개념 자체가 안 맞는다 — 이름을 바꿔도
        # 내부적으로는 여전히 UNCLASSIFIED_GROUP_NAME이라 스타일·배정 대상이 그대로고,
        # 순서를 옮기면 apply_group_order가 그 자리를 이름으로 저장해 버려 다음 회차의
        # (내용이 전혀 다른) 미분류가 중간에 끼어든다(사용자 지적). 전체선택 체크박스와
        # (초안의) 🤖 미분류 배정만 남긴다.
        # [추가: 2026-09-01] 이 소제목만 복사 — 렌더링 시점에 미리 구운 텍스트를
        # (build_group_copy_texts) data-copy-text에 담는다(서버 왕복 0회).
        # [수정: 2026-09-02] 그 텍스트엔 회차 헤더·메모 줄이 없다(소제목 + 기사만).
        # 자리는 아이콘 묶음 맨 앞이다: 왼쪽부터 "보는 동작(📋) → 고치는 동작(✏️🗑️) →
        # 옮기는 동작(▲▼)" 순서라, 파괴적인 🗑️ 옆에 무해한 📋이 붙지 않는다.
        # 📂 소제목 미분류에는 안 붙인다 — 그 칸은 소제목이 아니라 대기실이라 ✏️🗑️▲▼도
        # 이미 다 빠져 있고, 갈 곳이 안 정해진 기사를 보고서 형식으로 뽑을 이유가 없다.
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
            "" if group["name"] == UNCLASSIFIED_GROUP_NAME else (
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
        section_class = "subheading subheading-custom" if group["name"] in custom_names else "subheading"
        toc_name = (
            display_name
            if group["name"] == UNCLASSIFIED_GROUP_NAME
            else apply_subheading_format(html.escape(subheading_format), display_name)
        )
        # [추가: 2026-09-10] 미니 목차·헤더 ▲▼(JS)가 "순서 대상이 아닌 칸"을 알아보는 표식.
        locked_attr = ' data-order-locked="1"' if order_locked else ""
        # [추가: 2026-09-15] 「AI 기사 나누기」(splitSelected)가 읽는 두 값 — app.preview_renderer와
        # 같다. 서버로 보낼 원래 이름(data-group)과, 담당자가 이름을 고쳐 둔 소제목인지(data-renamed —
        # 통째로 나누면 그 이름이 사라지므로 그때만 한 번 묻는다).
        split_attrs = f' data-group="{original_name}"' + (
            ' data-renamed="1"' if display_name != original_name else ""
        )
        sections.append(
            f'<section class="{section_class}" id="subheading-{group_index}" data-toc-name="{toc_name}"'
            f"{locked_attr}{split_attrs}>"
            f"{heading}{articles_html}</section>"
        )
    return "\n".join(sections)


def _format_summary_title(name: str, labels: dict, subheading_format: str) -> str:
    """💬 AI 요약 블록의 소제목 제목 한 줄 — 메일머지 소제목 형식을 적용하되, 임시 보관함
    (📂 소제목 미분류)은 진짜 소제목이 아니므로 감싸지 않는다(app.llm_classifier.
    UNCLASSIFIED_GROUP_NAME이 실제로 여기까지 들어오는 경로는 거의 없지만, 다른
    지점들과 동일하게 방어적으로 막아둔다)."""
    display_name = html.escape(display_group_name(name, labels))
    if name == UNCLASSIFIED_GROUP_NAME:
        return display_name
    return apply_subheading_format(html.escape(subheading_format), display_name)


def build_summary_text(
    groups: list, labels: dict,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    summaries: Optional[list] = None,
) -> str:
    """「💬 AI가 읽은 소제목별 주요 요약」 텍스트 — 화면 복사 버튼과 텔레그램 「요약」 발송이
    같이 쓴다. 맨 위에 블록 제목, 그 아래 소제목마다 `<소제목>` + 요약. 요약이 하나도
    없으면 빈 문자열(발송 쪽이 기사 목록으로 대신 보낸다).

    복사 텍스트는 화면 표시용 html.escape를 거치지 않은 원문 그대로 담는다
    (_build_plain_text와 같은 이유 — 클립보드에 &lt; 같은 엔티티가 붙으면 안 됨).
    """
    if summaries is None:
        summaries = summarize_groups(groups)
    lines = []
    for item in summaries:
        if not item.get("summary"):
            continue
        raw_title = display_group_name(item["name"], labels)
        if item["name"] != UNCLASSIFIED_GROUP_NAME:
            raw_title = apply_subheading_format(subheading_format, raw_title)
        lines.append(f'{raw_title}\n{item["summary"]}')
    if not lines:
        return ""
    # [수정: 2026-09-10] 맨 위에 이 블록의 제목을 함께 담는다(사용자 요청) — 붙여넣은
    # 쪽에서 이 문단이 무엇인지 알 수 있어야 한다.
    return "\n\n".join([AI_SUMMARY_HEADING] + lines)


def _render_bottom(
    groups: list, highlight_words: list, labels: dict,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
) -> str:
    # [수정: 2026-09-10] "🤖 AI가 추출한 주요 키워드" 블록을 없앴다 — 집계 대상이 이미
    # 담당자의 검색어로 걸러진 기사라 빈도를 세도 검색어 주변 단어만 되돌려줬다(수시가
    # 2026-08-20에 같은 이유로 이 블록을 안 넣기로 한 그 판단, ADHOC_DESIGN.md §463).
    # 그래서 이 블록엔 실제로 AI가 쓴 소제목별 요약만 남는다. 배경은 HISTORY.md 참고.
    summaries = summarize_groups(groups)
    summary_items = []
    for s in summaries:
        summary_items.append(
            f'<div class="bottom-summary-item">'
            f'<strong>{_format_summary_title(s["name"], labels, subheading_format)}</strong>'
            f'<p>{highlight_keywords(s["summary"], highlight_words)}</p>'
            "</div>"
        )
    # [수정: 2026-09-17] 복사 텍스트는 텔레그램 「요약」 발송과 같은 함수로 만든다 — 둘이
    # 갈리면 복사한 요약과 받는 사람이 받은 요약이 조용히 달라진다.
    copy_text = build_summary_text(groups, labels, subheading_format, summaries=summaries)
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


def _manual_promote_control_html(
    url: str, target: str, group_names: list, labels: dict, custom_names: set
) -> str:
    """"담아둔 기사" 한 건을 이 화면으로 올리는 조작 하나 — 소제목 드롭다운(있으면) 또는 📥 버튼.

    [수정: 2026-08-11] 원래는 기사마다 "📝(초안에 포함)"·"📗(확정본에 포함)" 버튼 두 개가
    **두 화면 모두에** 떠 있었다. 두 가지가 겹쳐 헷갈렸다(사용자 피드백): 이모지가 무슨
    뜻인지 안 와닿고, 눌러도 어느 소제목으로 들어가는지 알 수 없었다(_handle_promote_manual_article이
    "마지막 소제목"에 그냥 꽂았다 — 아무 근거 없는 자리라, 승격한 기사를 되찾으라고
    `.just-promoted` 배경 표시까지 따로 만들어야 했다).

    이제 **목적지는 지금 보고 있는 화면이 정하고**(초안에서 누르면 초안, 확정본에서
    누르면 확정본), 담당자는 소제목만 고른다 — 고르는 순간 그 소제목으로 들어가므로
    "어디 갔지?"가 아예 안 생긴다. 화면을 넘나드는 승격(초안 화면에서 확정본으로)은
    같이 없앴다: 두 화면에 버튼 두 개가 다 떠 있던 게 애초에 혼란의 원인이었고,
    확정본은 수집 5분 뒤 자동발송이라(app.confirm_send) 실제로 쓸 일도 거의 없다.

    소제목이 하나도 없는 화면(기사 0건 회차·회차 종료 안내·검색 오류)에서는 고를 게
    없으므로 📥 버튼 하나로 떨어뜨리고, 서버가 자동 분류에 맡긴다.
    """
    handler = "promoteManualArticle" if target == "final" else "promoteManualArticleToDraft"
    placeholder = "확정본에 넣기…" if target == "final" else "초안에 넣기…"
    url_attr = html.escape(url)
    if not group_names:
        return (
            f'<button class="move-btn" type="button" data-url="{url_attr}" '
            f'onclick="{handler}(this)" title="{placeholder}">{icon("in")}</button>'
        )
    options = "".join(
        f'<option value="{html.escape(n)}"'
        + (' style="font-weight:700"' if n in custom_names else "")
        + f">{html.escape(_group_option_label(n, labels))}</option>"
        for n in group_names
    )
    return (
        f'<select class="group-move-select promote-select" data-url="{url_attr}" '
        'onclick="event.stopPropagation();" '
        f'onchange="event.stopPropagation(); {handler}(this);">'
        f'<option value="" selected disabled>{placeholder}</option>'
        f"{options}"
        "</select>"
    )


def _promotable_group_names(groups: Optional[list]) -> list:
    """담아둔 기사를 넣을 수 있는 소제목 이름들 — 시스템이 만든 임시 칸은 뺀다.

    "📂 소제목 미분류"(UNCLASSIFIED_GROUP_NAME)는 분류가 아직
    안 끝난 기사를 잠시 모아두는 자리이지 담당자가 골라 넣을 목적지가 아니다.
    """
    return [g["name"] for g in (groups or []) if g["name"] != UNCLASSIFIED_GROUP_NAME]


def _render_manual_section(
    manual_articles: list,
    highlight_words: list,
    line_template: str,
    target: str = "final",
    group_names: Optional[list] = None,
    labels: Optional[dict] = None,
    custom_names: Optional[set] = None,
    show_label_control: bool = False,
    scrap_date: str = "",
    scrap_end: str = "",
    add_button_html: str = "",
    add_panel_html: str = "",
) -> str:
    """"직접 추가한 기사" 구획을 렌더링한다 (실시간 기사 현황의 "📌" 결과물).

    자동 소제목 그룹과 분리된 별도 목록이다 — <소제목N> 라벨이 없고, 최대 5개 제한과도
    무관하며, 복사/내보내기 텍스트(_build_plain_text)에도 포함하지 않는다(실시간 현황에서
    참고용으로 옮겨둔 것일 뿐 "공식 스크랩 결과"가 아니라는 성격 때문 — 당일 자정이 지나면
    app.manual_articles가 자동으로 비운다).

    [수정: 2026-07-28] 기사마다 승격 조작을 항상 보여준다 — 예전엔 맨 위 기사의 ↑ 버튼만
    승격으로 동작해서, 원하는 기사를 승격하려면 먼저 순서를 맨 위까지 옮겨야 하는
    번거로움이 있었다(피드백: "너무 빡세다"). 순서와 무관하게 아무 기사나 바로 승격할 수
    있게 되면서, 목록 안 순서바꾸기(↑/↓, moveManualArticle) 자체가 더 이상 필요 없어져
    같이 뺐다. 비어 있으면 아예 렌더링하지 않는다 — 단, 입구(add_button_html, 초안의
    「+ 수기로 기사 추가」)를 받으면 0건이어도 한 줄로 그린다. 입구가 이 칸 안에 있어서
    칸이 사라지면 입구도 사라지기 때문이다. add_panel_html(주소 입력칸)은 머리 바로 아래.

    [수정: 2026-08-11] 그 조작이 이모지 버튼 두 개에서 화면별 소제목 드롭다운 하나로
    바뀌었다 — 이유는 _manual_promote_control_html 참고. 이 함수는 초안·확정본이 공유하는데
    (app.preview_renderer._manual_section_html), 이제 화면마다 목적지와 소제목 목록이
    다르므로 target/group_names를 인자로 받는다.
    """
    if not manual_articles and not add_button_html:
        return ""
    labels = labels or {}
    custom_names = custom_names or set()
    group_names = group_names or []
    known_labels_html = known_label_chips_html() if show_label_control else ""
    # [추가: 2026-08-26] "🤖 AI 기사 배정" — 담아둔 기사를 전부 초안에 넣고 소제목 배정을
    # AI에 맡긴다. 📂 소제목 미분류 칸의 같은 이름 버튼과 자리·모양·색을 맞췄다: 두 버튼은
    # 실제로 같은 일(기존 소제목은 그대로 두고 끼워 넣기, assign_to_existing)을 하므로
    # 이름이 갈려 있을 이유가 없었다(사용자 판단). 「AI 모든 기사 재분류」만 이름과
    # 아이콘(새로고침)이 다르다 — 그쪽은 소제목을 처음부터 새로 짓는 다른 일이다.
    #
    # 초안 화면에만 둔다. 확정본에는 미분류 칸을 처리할 버튼이 없어서(초안 전용),
    # 거기서 AI에 맡기면 📂 칸에 떨어진 채 손쓸 자리가 없는 막다른 길이 된다.
    # 미분류 배정과 달리 confirm()을 한 겹 둔다 — 그 칸은 "비워야 정상"이라 전체 처리가
    # 곧 정답이지만, 담아둔 기사는 안 넣고 남겨두는 것도 정상인 참고용 임시보드라
    # "전부"가 항상 맞는 답이 아니다(그래서 일부만 넣는 길인 기사별 드롭다운도 그대로 둔다).
    assign_btn_html = (
        '<button class="assign-unclassified-btn" type="button" '
        'onclick="event.stopPropagation(); assignPinnedArticles(this);" '
        f'data-count="{len(manual_articles)}" '
        'title="담아둔 기사를 전부 초안에 넣고 기존 소제목에 배정합니다 '
        '(기존 소제목은 바뀌지 않습니다)">'
        f'{icon("bot")} AI 기사 배정</button>'
        if target == "draft" and manual_articles
        else ""
    )
    articles_html = "\n".join(
        render_article(
            a,
            highlight_words,
            line_template,
            extra_buttons_html=_manual_promote_control_html(
                a["url"], target, group_names, labels, custom_names
            ),
            show_label_control=show_label_control,
            known_labels_html=known_labels_html,
            scrap_date=scrap_date,
            scrap_end=scrap_end,
        )
        for a in manual_articles
    )
    return (
        '<div class="manual-divider">이 아래는 복사·내보내기에 포함되지 않음</div>'
        '<div class="manual-zone">'
        '<div class="manual-zone-head">'
        '<div class="manual-zone-left">'
        f'<span class="manual-zone-title">{icon("pin")} 담아둔 기사</span>'
        + (
            f'<span class="manual-zone-count">{len(manual_articles)}건</span>'
            if manual_articles
            else '<span class="manual-zone-empty">전체 기사에서 📌 담아두거나, 직접 넣을 수 있어요</span>'
        )
        + f"{assign_btn_html}{add_button_html}"
        "</div>"
        '<span class="manual-zone-hint">자정에 자동으로 비워져요</span>'
        "</div>"
        f"{add_panel_html}{articles_html}"
        "</div>"
    )


def send_log_href(run_date: str, run_slot: str) -> str:
    """발송 기록 화면에서 이 회차의 가장 최근 발송 줄을 펼쳐 여는 주소."""
    return (
        f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/send-log?run="
        + urllib.parse.quote(f"{run_date}|{run_slot}", safe="")
    )


def _send_failure_banner_html(failure: dict, send_count: int, settings: dict, log_href: str = "") -> str:
    """확정본의 카운트다운 자리에 대신 뜨는 발송 실패 경고.

    [추가: 2026-08-26] 사용자 지적 — "확정본이 지금 발송을 시도하는 바로 그 화면인데
    실패하면 아무것도 안 보여준다." run["send_failure"](app.storage.record_send_failure,
    app.confirm_send.send_confirmed_run이 채운다)를 여기서 처음으로 화면에 보여준다.

    send_count == 0(아무 채널도 안 나감)이면 다음에 뭐가 일어나는지도 같이 안내한다 —
    자동발송이 켜져 있으면 "곧 자동으로 다시 시도", 꺼져 있으면 "(발송)을 다시
    눌러주세요". send_count > 0(부분 실패 — 한 명이라도 받아 record_send로
    "발송 완료"가 확정된 상태)이면 "일부는 못 받았어요"로 바꾼다 — 정기 보관함의
    빨간 점 툴팁(app.history_renderer._send_failure_tooltip)과 같은 구분이다.
    """
    channel_lines = "<br>".join(
        html.escape(f"{ch.get('channel', '')} — {ch.get('target', '')}: {ch.get('error', '')}")
        for ch in failure.get("channels", [])
    )
    if send_count > 0:
        # 채널이 아니라 사람 단위다 — 같은 텔레그램 안에서도 한 명만 못 받을 수 있다(_delivered_any).
        headline = "일부는 못 받았어요 — 받은 사람에겐 다시 보내지 않아요"
    elif is_auto_send_enabled(settings):
        headline = "발송 실패 — 곧 자동으로 다시 시도합니다"
    else:
        headline = "발송 실패 — 아래 (발송)을 다시 눌러주세요"
    link_html = (
        f'<br><a class="send-log-link" href="{html.escape(log_href, quote=True)}">받은 사람 보기 →</a>'
        if log_href else ""
    )
    return (
        '<span class="send-countdown send-failure">'
        f'{icon("alert")} {html.escape(headline)}<br>{channel_lines}{link_html}</span>'
    )


# 「지난 회차 메모」 참고 목록 — 메모 칸 안, 입력칸 아래. 소제목 이름 고르기 창과 **일부러
# 다르게** 생겼다: 저쪽은 골라서 넣는 떠 있는 창(줄이 버튼), 이쪽은 쓰는 동안 옆에 펴 두고
# 보는 읽기 전용 글(줄을 눌러도 아무 일 없음, 끌어서 복사는 된다). 편집 중일 때만 보이고
# (잠긴 저장 메모 상태에선 숨김), 치는 단어와 **똑같은** 단어가 파랗게 따라 칠해진다.
# 단어 비교는 앞뒤 문장부호만 떼고 한다 — 「보도,」와 「보도」는 같은 단어. 떼는 문자는
# 아래 한 곳이고 Python(data-w)과 JS(치는 말)가 같이 쓴다.
_NOTE_WORD_PUNCT = ".,·;:!?()[]{}<>「」『』\"'“”‘’…"


def _note_word_key(word: str) -> str:
    return word.strip(_NOTE_WORD_PUNCT)


def keyword_note_history_html(run_key=None) -> str:
    """메모 칸 아래 「지난 회차 메모」 목록(없으면 빈 문자열). 재료는 저장된 메모뿐 — API 호출 없음."""
    rows = past_notes(run_key)
    if not rows:
        return ""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    for date, slots, text in rows:
        day = "오늘" if date == today else f"{int(date[5:7])}/{int(date[8:10])}"
        when = f'{day} {"·".join(html.escape(s) for s in slots)}'
        words = []
        for part in re.split(r"(\s+)", text):
            key = _note_word_key(part) if part and not part.isspace() else ""
            words.append(
                f'<span class="kn-w" data-w="{html.escape(key)}">{html.escape(part)}</span>' if key else html.escape(part)
            )
        lines.append(f'<div class="kn-h-row"><span class="kn-h-when">{when}</span><span class="kn-h-text">{"".join(words)}</span></div>')
    return (
        '<div class="kn-history" aria-label="지난 회차 메모">'
        '<div class="kn-h-head">지난 회차 메모</div>'
        f'<div class="kn-h-list">{"".join(lines)}</div></div>'
    )


KEYWORD_NOTE_HISTORY_STYLE = """
  .kn-history {{ flex-basis: 100%; margin-top: 2px; padding-top: 8px; border-top: 1px dashed {border}; }}
  .keyword-note-zone:has(#keyword-note-input:disabled) .kn-history {{ display: none; }}
  .kn-h-head {{ font-size: var(--fs-xs); color: {text_faint}; margin-bottom: 4px; }}
  .kn-h-list {{ max-height: 7.2em; overflow-y: auto; }}
  .kn-h-row {{ display: flex; gap: 10px; font-size: var(--fs-sm); line-height: 1.6; color: {text_soft}; cursor: text; }}
  .kn-h-when {{ flex: none; min-width: 7.5em; color: {text_faint}; font-variant-numeric: tabular-nums; }}
  .kn-h-text {{ min-width: 0; overflow-wrap: anywhere; }}
  .kn-w.is-same {{ color: {accent}; font-weight: 600; }}
"""


def keyword_note_history_style() -> str:
    return KEYWORD_NOTE_HISTORY_STYLE.format(**PALETTE)


def keyword_note_history_script() -> str:
    """치는 동안 같은 단어를 파랗게 — 입력할 때마다 다시 칠한다(중괄호 홑겹 완성 문자열)."""
    punct = json.dumps(_NOTE_WORD_PUNCT)
    return """
(function () {
  var input = document.getElementById("keyword-note-input");
  if (!input || !document.querySelector(".kn-history")) return;
  var P = """ + punct + """;
  function isP(c) { return P.indexOf(c) !== -1; }
  function key(w) {
    var a = 0, b = w.length;
    while (a < b && isP(w.charAt(a))) a++;
    while (b > a && isP(w.charAt(b - 1))) b--;
    return w.slice(a, b);
  }
  function paint() {
    var typed = {};
    input.value.split(/\\s+/).forEach(function (w) { var k = key(w); if (k) typed[k] = true; });
    document.querySelectorAll(".kn-history .kn-w").forEach(function (el) {
      el.classList.toggle("is-same", !!typed[el.dataset.w]);
    });
  }
  input.addEventListener("input", paint);
  input.addEventListener("focus", paint);
  paint();
})();
"""


def keyword_note_template_vars(run_key=None) -> dict:
    """"+ 직접 키워드 작성" 메모 칸이 쓰는 템플릿 변수 묶음. 확정본(app.renderer)과
    초안(app.preview_renderer)이 같은 칸을 그리므로 계산을 한 곳에 둔다 — 예전엔 같은
    6줄이 두 파일 다섯 자리에 흩어져 있어 한쪽만 고치면 두 화면이 어긋났다.

    [수정: 2026-08-27] 다른 회차 메모를 끌어와 미리 채우던 동작(2026-08-26)은
    되돌렸다 — 화면은 그 회차에 실제로 저장된 메모(load_manual_keyword_note)만 본다.
    """
    text = load_manual_keyword_note(run_key)
    saved = bool(text)
    return {
        # 지난 회차 메모 참고 목록(편집 중일 때만 CSS로 보인다) + 그 CSS·JS.
        "note_history_html": keyword_note_history_html(run_key),
        "note_history_style": keyword_note_history_style(),
        "note_history_script": keyword_note_history_script(),
        "note_open_class": " is-open" if text else "",
        "note_value_attr": html.escape(text),
        # [추가: 2026-08-10] 이미 저장된 메모가 있으면 입력칸을 잠가둔 채 시작한다 —
        # (수정)을 눌러야 편집 가능해진다(toggleKeywordEditMode). 새로 쓰는 중이면
        # 처음부터 편집 가능하고 버튼은 "저장".
        "note_input_disabled_attr": " disabled" if saved else "",
        "note_save_btn_label": "수정" if saved else "저장",
        # [추가: 2026-08-10] 저장한 적이 없으면(새로 쓰는 중) 지울 게 없으니 "삭제"가
        # 아니라 "취소"다 — 저장 이력이 있어야("수정" 상태) 진짜 삭제가 의미 있다.
        "note_clear_btn_label": "삭제" if saved else "취소",
        "note_clear_btn_onclick": "clearKeywordNote()" if saved else "cancelKeywordNote()",
        # [수정: 2026-08-10] 패널이 이미 열려있으면 그 안에 자체 버튼이 있으므로 툴바
        # 버튼은 뺀다 — 같은 기능이 두 곳에 중복돼 보인다는 지적(사용자 피드백).
        "keyword_note_btn_html": (
            ""
            if text
            else '<button class="create-group-btn" type="button" onclick="toggleKeywordNote(this)">+ 키워드 직접 작성</button>'
        ),
    }


def _build_plain_text(
    run_slot: str, groups: list, line_template: str, labels: dict,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    run_date: Optional[str] = None,
    note: Optional[str] = None,
    header: bool = True,
) -> str:
    """복사/내보내기용 메모장 형식 텍스트를 만든다 (PRD.md 기능1 규칙 5·10·11).

    화면에 표시된 언론사·기사제목·URL 목록만 담는다 — 하단의 💬 AI 요약
    블록은 규칙10이 "소제목별 스크랩 목록(언론사·기사제목·URL)"만 명시하므로 제외한다.
    첫 줄 형식은 화면과 동일하게 line_template을 따른다. 소제목 이름은 사용자가 붙인
    이름표(labels)가 있으면 그걸로 표시한다 — 화면과 항상 같은 이름을 보여줘야 한다.
    [추가: 2026-08-12] 메일머지 "소제목 형식"도 화면과 동일하게 적용한다(순수 텍스트라
    escape 불필요) — 단, 이 시점의 groups는 이미 실제 기사가 있는 소제목만 남아있고
    임시 보관함(UNCLASSIFIED_GROUP_NAME)은 호출부에서 걸러지거나 "기타"로 합쳐진
    뒤라 별도 방어 코드 없이도 안전하다(app.preview_renderer._merge_unclassified 참고).
    """
    # [추가: 2026-09-02] header=False면 회차 헤더 줄과 메모 줄을 통째로 뺀다 — 소제목별
    # 복사(build_group_copy_texts)가 "소제목 이름 + 그 안의 기사"만 내보내기 위해 쓴다.
    lines = []
    if header:
        lines.append(f"언론 모니터링 {format_slot_time_kr(run_slot)} 기준")
        # [추가: 2026-08-05] "+ 직접 키워드 작성하기"로 적어둔 메모가 있으면 헤더 바로 아래
        # "- {메모}" 한 줄로 끼워 넣는다 — AI 키워드 블록(하단)과 달리 이건 화면 맨 위에 있고,
        # 내보내기 텍스트에서도 항상 헤더 다음 줄에 온다.
        # [수정: 2026-08-26] 기준 날짜는 "오늘"이 아니라 **그 회차의 날짜**다 — 자정을 넘겨
        # 어제 마지막 회차 확정본을 보면 today와 어긋나 메모가 없는 것처럼 보였다.
        if note is None:
            note = load_manual_keyword_note((run_date or datetime.now().strftime("%Y-%m-%d"), run_slot))
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
                # [추가: 2026-07-27] 기사마다 빈 줄을 넣어 URL과 다음 기사 제목이 붙어
                # 보이지 않게 한다 — 보고용으로 복사해 쓸 때 훑어보기 쉽다는 요청.
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_group_copy_texts(
    run_slot: str, groups: list, line_template: str, labels: dict,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    run_date: Optional[str] = None,
) -> dict:
    """소제목 하나씩만 담은 복사 텍스트를 미리 만들어 {소제목 이름: 텍스트}로 돌려준다.

    [추가: 2026-09-01] 소제목 헤더의 📋 버튼용. 회차 전체 복사와 **같은 함수**
    (_build_plain_text)에 그룹 하나짜리 리스트만 넘긴 결과라, 기사 줄 형식·기사 사이
    빈 줄이 자동으로 같아진다 — 소제목별 전용 빌더를 따로 두면 한쪽만 고쳤을 때 두
    텍스트가 조용히 어긋난다.

    [수정: 2026-09-02] 다만 회차 헤더("언론 모니터링 N시 기준")와 메모 줄은 빼고
    소제목 이름 + 그 안의 기사만 담는다(header=False, 사용자 요청) — 소제목 하나를
    떼어 그때그때 붙여넣는 용도라 회차 정보가 붙으면 매번 지워야 했다.

    기사가 없는 소제목(담당자가 미리 만들어둔 빈 소제목)은 복사할 게 없으므로 아예
    넣지 않는다 — 호출부는 이 dict에 이름이 있는지로 버튼을 그릴지 정한다.
    """
    return {
        group["name"]: _build_plain_text(
            run_slot, [group], line_template, labels, subheading_format,
            run_date=run_date, header=False,
        )
        for group in groups
        # 📂 소제목 미분류는 화면에서도 버튼을 안 그리지만 여기서도 뺀다 — 그 이름은
        # 보고 텍스트에 절대 나가면 안 되는 화면 전용 표시라(CODING_CONVENTIONS.md §4),
        # 애초에 만들지 않는 쪽이 안전하다.
        if group["articles"] and group["name"] != UNCLASSIFIED_GROUP_NAME
    }


def latest_confirmed_report(
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
    subheading_format: Optional[str] = None,
) -> Optional[dict]:
    """최신 확정 회차의 발송용 텍스트 두 가지 — {"run", "text"(기사 목록), "summary"(요약)}.
    오늘 회차가 없으면 None. 화면(render_page)과 같은 소제목 계산을 거친다.

    [추가: 2026-09-17] 텔레그램 받는 사람마다 기사·요약을 고르게 되면서 한 번에 둘 다
    만든다 — 기사 목록과 요약이 서로 다른 시점의 소제목을 보면 안 된다.
    """
    run = load_latest_confirmed_run()
    if run is None or not is_today(run):
        return None
    if keywords is None or highlight_words is None or line_template is None or subheading_format is None:
        settings = load_settings()
        keywords = keywords if keywords is not None else all_search_keywords(settings)
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
        subheading_format = (
            subheading_format
            if subheading_format is not None
            else settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE)
        )
    articles = apply_summary_overrides(filter_hidden(run["articles"]))
    round_id = round_id_for_run(run)
    labels = load_group_labels(round_id)
    # [수정: 2026-08-12] 확정된 회차는 저장된 소제목 스냅샷을 그대로 쓴다 — 화면
    # (render_page)과 반드시 같은 함수를 써야 "화면과 다른 내용이 발송되는" 일이 없다.
    groups = groups_for_confirmed_run(
        articles,
        keywords,
        forced_groups=load_group_overrides(),
        custom_group_names=load_custom_groups(),
        round_id=round_id,
    )
    groups = apply_group_order(groups, round_id)
    return {
        "run": run,
        "text": _build_plain_text(
            run["run_slot"], groups, line_template, labels, subheading_format, run_date=run["run_at"][:10]
        ),
        "summary": build_summary_text(groups, labels, subheading_format),
    }


def render_page(
    run_slot: str,
    articles: list,
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
    subheading_format: Optional[str] = None,
    confirmed_at: Optional[str] = None,
    send_count: int = 0,
    run_at: Optional[str] = None,
    sent_by: Optional[str] = None,
    sent_at: Optional[str] = None,
    classification_degraded: bool = False,
    finalize_new_subheadings: Optional[list] = None,
    send_failure: Optional[dict] = None,
) -> str:
    """한 회차 데이터를 완성된 HTML 문서 문자열로 렌더링한다.

    수집된 기사가 없으면 (PRD.md 기능1 규칙 7) 본문을 "💤"로 대체한다.
    헤더의 회차 시각(run_slot)은 이 경우에도 그대로 표시한다.

    confirmed_at/send_count/sent_by/sent_at: [추가: 2026-08-10] 이 회차가 확정된 시각,
    지금까지 발송된 횟수, 그 발송이 사람이 누른 것인지 시스템이 대신한 것인지와 그 시각.
    confirmed_at은 화면에 직접 쓰이지 않지만(재발송 시 "(수정)" 접두어 판단은 send_count로
    이미 처리됨) 호출부 시그니처 호환을 위해 계속 받는다.

    [수정: 2026-08-11] "확정" 개념 자체가 없어졌다 — 회차는 수집되는 즉시 확정본이 되고
    (main._scrape_and_render), 담당자에게 남는 유일한 수동 조작은 (발송)뿐이다. 그래서
    이 함수의 mode 파라미터("final"/"pending")와 (확정) 버튼·확정 대기 화면도 함께
    없앴다. 카운트다운은 아직 한 번도 안 보냈을 때만(send_count==0) run_at +
    CONFIRM_SEND_GRACE_SEC 기준 "자동발송까지 남은 시간"으로 보여준다(실제 자동발송
    판단은 app.confirm_send가 서버에서 하고, 이건 그 시계를 그대로 비추는 순수 표시용).
    한 번이라도 보냈고 그게 자동이었으면 카운트다운 자리에 "🤖 N시 M분 자동발송 완료"
    배지가 대신 뜬다.

    keywords: 검색 키워드 — 소제목 분류·주요 키워드 추출에서 "뻔한 단어"를 제외하는 데 쓴다.
    highlight_words: 형광펜 단어 — 요약에 하이라이트를 칠하는 기준이며 keywords와 무관하다(규칙6).
    line_template: 기사 첫 줄("ㅇ (언론사) 제목") 형식. keywords/highlight_words와도 무관하다(규칙5).

    finalize_new_subheadings: [추가: 2026-08-20] run.get("finalize_new_subheadings") —
    초안 마감 시점에 새로 생긴 소제목 이름. h2 "마감 시 신규" 배지와 상단 확인 배너
    문구(몇 개가 새로 생겼는지)에 쓴다. 기사별 "마감 후 자동 배정" 표시는 articles의
    각 항목에 이미 담겨 오므로(article["finalize_auto_assigned"]) 별도 인자가 없다.
    """
    keywords = keywords if keywords is not None else DEFAULT_KEYWORDS
    highlight_words = highlight_words if highlight_words is not None else DEFAULT_HIGHLIGHT_KEYWORDS
    line_template = line_template if line_template is not None else DEFAULT_ARTICLE_LINE_TEMPLATE
    subheading_format = subheading_format if subheading_format is not None else DEFAULT_SUBHEADING_FORMAT_TEMPLATE
    # [수정: 2026-08-14] 이 회차의 round_id를 한 번만 계산해 이름표·순서·🤖 배지 판단에
    # 그대로 재사용한다 — 셋 다 "이 회차만" 봐야 하므로 같은 값을 써야 한다.
    round_id = round_id_for_run({"run_at": run_at, "run_slot": run_slot}) if run_at else None
    labels = load_group_labels(round_id)
    custom_names = load_custom_groups()
    overrides = load_group_overrides()
    groups = (
        groups_for_confirmed_run(
            articles,
            keywords,
            forced_groups=overrides,
            custom_group_names=custom_names,
            round_id=round_id,
        )
        if articles
        else []
    )
    # [추가: 2026-07-30] 스크랩 초안과 마찬가지로, "+ 새 소제목 만들기"로 미리 만들어둔
    # 빈 소제목도 완성본에 같이 보여준다(app.preview_renderer._compute_preview_content와
    # 같은 이유·같은 방식) — 요약/키워드 블록(_render_bottom)·복사 텍스트에는 원래의
    # groups만 넘겨 빈 placeholder가 섞이지 않게 한다.
    existing_names = {g["name"] for g in groups}
    empty_custom_groups = [{"name": n, "articles": []} for n in custom_names if n not in existing_names]
    # [추가: 2026-08-03] 사용자가 ↑/↓로 직접 정한 소제목 순서를 얹는다 — 빈 소제목까지
    # 포함한 전체 목록에 적용한 뒤, 요약·복사 텍스트에 넘길 groups도 이 순서를 그대로
    # 반영하도록 다시 추려낸다(화면과 복사 텍스트의 소제목 순서가 항상 같아야 하므로).
    render_groups = apply_group_order(groups + empty_custom_groups, round_id)
    groups = [g for g in render_groups if g["articles"]]
    total_article_count = sum(len(g["articles"]) for g in groups)
    # [추가: 2026-08-04] "언론사순" 보기가 참고할 순위 — articles는 이미 언론사 우선순위로
    # 정렬돼 저장된 회차 전체 목록이므로, 그 안 인덱스를 그대로 순위로 쓴다.
    rank_by_url = {a["url"]: i for i, a in enumerate(articles)}
    # [추가: 2026-08-11] 이 화면의 소제목 중 어떤 게 AI가 만든 것인지 — 🤖 배지 판단용.
    # 캐시만 읽으므로 API를 부르지 않고, 규칙 기반으로 폴백한 경우엔 빈 집합이 온다.
    # [수정: 2026-09-15] 「AI 기사 배정」·「AI 기사 나누기」가 새로 지은 이름도 넣는다(초안과 같은
    # 이유 — 배정 기록으로 생긴 소제목은 캐시에 없어 AI가 지은 이름인데 배지가 빠졌다).
    ai_names = llm_cached_group_names(articles, round_id=round_id) | set(load_assigned_groups())
    # [추가: 2026-08-18] 라벨 스냅샷용 — 확정본은 run_at(회차 수집 시각)의 날짜, run_slot이
    # 곧 scrap_end다. run_at이 없는(레거시) 회차는 오늘 날짜로 대체한다(라벨은 지금
    # 붙이는 것이므로 붙이는 시점 기준이 크게 어긋나지 않는다).
    # [수정: 2026-08-26] 이 회차의 날짜를 한 번만 구해 라벨 스냅샷·메모 조회·내보내기
    # 파일명이 같은 값을 쓰게 한다 — 예전엔 자리마다 따로 계산했고 메모 쪽만 run_at을
    # 안 봐서(datetime.now()) 자정 넘긴 확정본에서 메모가 사라졌다.
    run_date = (run_at or datetime.now().isoformat())[:10]
    label_scrap_date = run_date
    finalize_new_set = set(finalize_new_subheadings or [])
    if not render_groups:
        body = '<div class="empty">💤<div class="cute-caption">뉴스가 잠잠</div></div>'
    else:
        body = _render_groups(
            render_groups, highlight_words, line_template, labels, rank_by_url, set(custom_names), ai_names,
            subheading_format,
            show_label_control=True, scrap_date=label_scrap_date, scrap_end=run_slot,
            finalize_new_subheadings=finalize_new_set,
            group_copy_texts=build_group_copy_texts(
                run_slot, groups, line_template, labels, subheading_format, run_date=run_date,
            ),
        )
        if groups:
            body += _render_bottom(groups, highlight_words, labels, subheading_format)
    # "직접 추가한 기사"(실시간 현황 → 📌)는 이 회차에 실제로 수집된 기사가 하나도
    # 없어도(💤) 독립적으로 존재할 수 있으므로, 위 분기와 무관하게 항상 이어붙인다.
    # [수정: 2026-07-27] 전체를 자동으로 정식 결과에 합치는 대신, 맨 위 기사를 ↑로
    # "승격"시켜야만 실제 소제목(groups)에 들어간다(_handle_promote_manual_article,
    # app/settings_server.py) — 그전까지는 예전처럼 복사/내보내기에서 계속 제외된다.
    manual_articles = filter_hidden(load_manual_articles())
    # [수정: 2026-08-11] 이 화면(확정본)의 소제목 목록을 같이 넘겨, 담아둔 기사를 어느
    # 소제목에 넣을지 담당자가 바로 고르게 한다(_manual_promote_control_html).
    body += _render_manual_section(
        manual_articles,
        highlight_words,
        line_template,
        target="final",
        group_names=_promotable_group_names(render_groups),
        labels=labels,
        custom_names=set(custom_names),
        show_label_control=True,
        scrap_date=label_scrap_date,
        scrap_end=run_slot,        # 「+ 수기로 기사 추가」 — 초안과 같은 입구·입력칸(0건이어도 칸이 한 줄로 남는다).
        add_button_html=URL_ADD_BUTTON_HTML,
        add_panel_html=URL_ADD_PANEL_HTML,
    )

    plain_text = _build_plain_text(
        run_slot, groups, line_template, labels, subheading_format, run_date=run_date
    )
    # 파일명은 정기·수시 공통 규칙(날짜_종류_구분). 회차 자리는 머리줄처럼 「N시 기준」이라
    # `14-00기준`으로 적는다(`:`는 Windows 파일명에 못 쓴다). 초안·정기 보관함 회차 줄도 같은 모양.
    export_date = run_date
    export_filename = f"{export_date}_언론모니터링_{run_slot.replace(':', '-')}기준.txt"
    # [추가: 2026-08-18] 엑셀 내보내기(PRD.md 기능11) — groups를 그대로 순회해 rows를
    # 만든다. _build_plain_text와 달리 UNCLASSIFIED_GROUP_NAME("📂 소제목 미분류")을
    # "기타"로 접지 않는다 — 기능11 규칙4가 명시적으로 "빈 값이거나 미분류인 행도
    # 정상"이라고 못박아 뒀다(보고서 텍스트와 다른 규칙: §4는 화면 전용 표시가 보고서로
    # 새면 안 된다는 것이지, 이건 애초에 보고서가 아니라 "데이터"다). 소제목 표시 형식
    # (subheading_format)도 마찬가지 이유로 적용하지 않는다 — 기계가 다시 읽을 원본
    # 이름 그대로가 데이터 계약에 맞다.
    export_rows = build_export_rows(groups, export_date, run_slot)
    export_excel_filename = f"{export_date}_언론모니터링_{run_slot.replace(':', '-')}기준.xlsx"

    note_vars = keyword_note_template_vars((run_date, run_slot))
    # [수정: 2026-08-10] 카운트다운을 초안(pending)에서 확정본(final)으로 옮겼다 — 위
    # docstring 참고. 확정본이라도 이미 한 번 보냈으면(send_count>=1) 더 이상 자동 전송
    # 대상이 아니므로 보여주지 않는다.
    # [추가: 2026-08-11] 자동발송을 꺼두면(설정 /auto-send) 카운트다운도 안 띄운다 —
    # 아무 일도 안 일어나는데 "N분 후 자동발송"이 흐르면 명백한 거짓말이다(사용자 지적).
    # 유예 시간도 설정값을 그대로 따라간다.
    grace_deadline_ms = 0
    settings = load_settings()
    # [추가: 2026-08-12] 분류가 규칙 기반으로 떨어진 회차는 자동발송 자체가 보류되므로
    # (app.confirm_send) 카운트다운을 아예 안 띄운다 — 안 그러면 "곧 자동발송됩니다"가
    # 거짓말이 된다. 아직 한 번도 안 보낸 경우에만 경고를 보여준다(한 번이라도 보냈으면
    # 담당자가 이미 확인하고 발송한 것이므로 더 알릴 게 없다).
    if send_count == 0 and run_at and is_auto_send_enabled(settings) and not classification_degraded:
        deadline = datetime.fromisoformat(run_at) + timedelta(seconds=auto_send_grace_sec(settings))
        grace_deadline_ms = int(deadline.timestamp() * 1000)
    if classification_degraded and send_count == 0:
        # [수정: 2026-08-20] preview.html의 .classify-degraded 배너와 같은 첫 문장을
        # 쓴다 — 초안에서 이 경고를 이미 본 담당자가 확정본에서도 같은 사건인 줄
        # 바로 알아보도록(app.preview_renderer._classify_degraded_html 참고).
        send_countdown_html = (
            '<span class="send-countdown classification-warning">'
            f'{icon("alert")} AI가 소제목을 묶지 못했어요 — 단어 빈도수 기반 기계적 '
            "이름입니다. 확인 후 직접 발송해주세요</span>"
        )
    elif send_failure:
        # [추가: 2026-08-26] 실패를 카운트다운·성공 배지보다 먼저 본다 — 부분 실패
        # (send_count > 0이어도 다른 채널이 실패했으면) 여전히 여기로 온다. "뭔가
        # 안 갔다"는 신호가 "일단 하나는 나갔다"는 사실보다 담당자에게 먼저 보여야
        # 한다(정기 보관함의 빨간 점과 같은 우선순위 판단).
        send_countdown_html = _send_failure_banner_html(
            send_failure, send_count, settings, send_log_href(run_date, run_slot)
        )
    elif grace_deadline_ms:
        send_countdown_html = '<span class="send-countdown" id="send-countdown"></span>'
    elif send_count > 0 and sent_at:
        # [추가: 2026-08-10] 담당자가 반응하지 않아 시스템이 대신 전송한 경우를 표시한다
        # (사용자 요청 — 사람이 직접 보낸 건지 기계가 대신 보낸 건지 구분해야 함). 카운트
        # 다운이 있던 바로 그 자리를 재활용해 "전송 전엔 카운트다운, 전송 후엔 처리 결과"로
        # 자연스럽게 이어진다.
        # [수정: 2026-08-10] "HH:MM"(콜론) 형식이 위 카운트다운("MM:SS")과 겹쳐 보인다는
        # 피드백으로 "N시 M분" 한국어 표현(format_slot_time_kr, 헤더와 동일 방식)으로
        # 바꿨다. "전송"도 "발송"으로(아래 (전송)→(발송) 버튼 이름 변경과 통일).
        # [수정: 2026-08-13] 담당자가 직접 눌렀을 때(sent_by=="manual") 이 자리를 비워두던
        # 것을 "✔️ N시 M분 발송 완료"로 채운다 — 발송 직후 토스트는 0.9초 뒤 새로고침과
        # 함께 사라져서, 화면을 다시 열면 보냈다는 증거가 하나도 안 남는 게 문제였다
        # (기계가 보낸 건 배지가 남는데 사람이 보낸 건 안 남는 비대칭). 시각은 콜론 없는
        # 한국어 표현 그대로 — 위 카운트다운(hh:mm:ss)과 헷갈리지 않게 한 2026-08-10의
        # 결정을 되돌리지 않는다.
        sent_time_kr = html.escape(format_slot_time_kr(sent_at[11:16]))
        # 2회째부터 발송 횟수를 함께 보여준다 — 받는 사람에게 나가는 제목의 "(수정)"
        # 접두어(app.confirm_send)와 짝을 이뤄, 화면만 봐도 몇 번 내보냈는지 알 수 있다.
        repeat_suffix = f" ({send_count}회)" if send_count >= 2 else ""
        badge_text = (
            f'{icon("bot")} {sent_time_kr}에 자동발송 완료{repeat_suffix}'
            if sent_by == "auto"
            else f'{icon("check_circle")} {sent_time_kr} 발송 완료{repeat_suffix}'
        )
        send_countdown_html = (
            f'<a class="send-countdown sent-done" href="{html.escape(send_log_href(run_date, run_slot), quote=True)}" '
            f'title="받은 사람 보기">{badge_text}</a>'
        )
    else:
        send_countdown_html = ""
    countdown_suffix = "후 자동발송"
    # [추가: 2026-08-11] 확정본도 소제목을 다시 분류할 수 있어야 한다는 요청 — 확정본은
    # 매 렌더링마다 기본적으로 자동 재분류되지만(app.classifier.classify_articles의
    # allow_llm_call 기본값 True) 그건 "기사 구성이 바뀌었을 때만" 실제로 API를 다시
    # 부르는 캐시 우선 동작이라, 지금 이름·묶음이 마음에 안 들어 캐시를 무시하고 AI에게
    # 새로 물어보고 싶을 때 쓸 수단이 없었다 — 이 버튼이 그 수단이다(force_llm=True).
    # 수동으로 옮긴 기사가 있으면 초안과 동일하게 먼저 확인을 받는다(재분류가 소제목
    # "이름"을 키로 저장된 그 기록을 날릴 수 있어서).
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
    regen_btn_html = (
        f'<button class="create-group-btn regen-btn" type="button" data-manual-edits="{manual_edits}" '
        'onclick="regenerateSubheadings(this)" '
        f'title="전체 기사를 처음부터 다시 분류합니다">{icon("refresh")} AI 모든 기사 재분류</button>'
    )
    # [수정: 2026-09-15] 「📷 사진 추정 전체 선택」 → 「📷 사진 추정 모아 보기」 — 흩어진
    # 사진 추정 행을 한 목록으로 모아 보여주고, 전체 선택은 모아 보는 중 띠가 맡는다
    # (photo_gather_script). 0건이면 버튼 자체를 안 그리는 규칙은 그대로다.
    photo_suspect_count = sum(1 for a in articles if looks_like_photo_caption(a))
    photo_gather_btn_html = photo_gather_button_html(photo_suspect_count)
    # [추가: 2026-08-11] 되돌릴 게 있을 때만 ↩ 버튼을 그린다 — 항상 떠 있으면 무엇이
    # 되돌아가는지 알 수 없어 오히려 누르기 불안하다는 판단(사용자와 합의).
    undo_label = undo_peek_label()
    undo_fab_html = (
        f'<button type="button" class="undo-fab" onclick="undoLastAction(this)" '
        f'title="{html.escape(undo_label)} 되돌리기">{icon("undo")}</button>'
        if undo_label
        else ""
    )
    # [되돌림: 2026-08-10] 전송 횟수 원문자 배지(①②…)는 굳이 필요 없다는 피드백으로
    # 뺐다 — send_count는 그대로 저장/추적되고 "(수정)" 제목 접두어 판단에도 계속
    # 쓰이지만, 화면에 숫자로 노출하진 않는다.
    action_fab_html = (
        '<button type="button" class="confirm-send-fab send" onclick="sendReport(this)" '
        'title="이메일 및 텔레그램으로 발송">발송</button>'
    )
    # [추가: 2026-08-20] "마감 후 자동 배정" 확인 배너 — 담당자가 초안에서 못 본 사이
    # 마감 시점에 소제목이 자동으로 붙은 기사가 있으면 상단에 알려준다(초안의
    # round-over-banner와 같은 위치·형태, 색만 AI 전용 연보라). 자동 배정된 기사가
    # 하나도 없으면(가장 흔한 경우) 배너 자체를 아예 안 그린다.
    # [추가: 2026-09-03] "앞 회차 기사 회수" 안내 배너 — 회차 마감 순간 네이버 검색이
    # 아직 안 주던 기사를 이 회차가 주워 담았을 때만 그린다(app.scraper.collect_run).
    # 0건이면 아예 안 그려서, 회수가 없는 회차의 화면은 예전과 완전히 같다.
    #
    # 문구는 네이버에 대한 주장을 한 줄도 하지 않는다 — "네이버 검색에 늦게 올라왔다"고
    # 쓰면 담당자가 네이버 뉴스 화면에서 그 시각에 이미 그 기사를 봤을 때 곧바로 반박당한다
    # (실제로 그 둘은 다른 통로다 — 2026-09-03 아침에 담당자가 눈으로 찾은 기사를 앱은
    # 못 찾고 있었다). 그래서 본문은 "이번 회차에 담았다"는 우리 동작만 서술하고, 이유는
    # 전부 ⓘ 툴팁으로 내린다.
    late_articles = [a for a in articles if a.get("late_pickup")]
    if late_articles:
        late_slots = {a.get("late_pickup_slot") for a in late_articles if a.get("late_pickup_slot")}
        # 회수된 기사가 전부 같은 회차 것일 때만 그 회차 이름을 쓴다 — 섞였거나 값이
        # 없으면 "앞 회차"로만 말한다(없는 값을 지어내지 않는다).
        late_label = (
            f"{format_slot_time_kr(next(iter(late_slots)))} 회차 기사"
            if len(late_slots) == 1
            else "앞 회차 기사"
        )
        late_tip = (
            "네이버가 앱에 주는 검색 결과에는 기사가 몇 분~수십 분 늦게 올라오는 경우가 있습니다.\n"
            "네이버 뉴스 화면에는 이미 보이더라도 검색 결과에는 아직 없을 수 있습니다.\n"
            "그래서 앞 회차에 담기지 못한 기사를 다음 회차에서 함께 담습니다."
        )
        late_pickup_banner_html = (
            '<div class="late-banner">'
            # 건수 버튼을 문장 한가운데 두지 않는다 — 조사("2건이")가 버튼 경계로 끊겨
            # "2건 이"처럼 벌어져 보인다(실측). 초안의 [미분류 N건] 배지처럼 문장 끝에
            # 독립된 버튼으로 두고, 라벨에 동작("보기")을 적어 누를 수 있음을 알린다.
            f'<span class="msg">📥 <b>{html.escape(late_label)}</b>가 뒤늦게 수집돼 함께 담겼어요.</span>'
            '<button type="button" class="cnt" onclick="jumpToLatePickup()">'
            f'{len(late_articles)}건 보기</button>'
            f'<button type="button" class="q" title="{html.escape(late_tip, quote=True)}">&#8505;</button>'
            "</div>"
        )
    else:
        late_pickup_banner_html = ""
    finalize_added_count = sum(1 for a in articles if a.get("finalize_auto_assigned"))
    finalize_new_count = len(finalize_new_set)
    if finalize_added_count:
        new_sub_msg = f" 소제목 {finalize_new_count}개는 이때 새로 만들어졌어요." if finalize_new_count else ""
        finalize_banner_html = (
            '<div class="finalize-banner" id="finalize-banner">'
            '<div class="finalize-banner-inner">'
            '<span class="msg" id="finalize-msg">'
            f'<b>{html.escape(format_slot_time_kr(run_slot))} 회차가 마감되면서 기사 '
            f'{finalize_added_count}건이 자동으로 배정됐어요.</b> 초안에서 확인하지 못한 기사예요.'
            f'{html.escape(new_sub_msg)}'
            "</span>"
            '<button type="button" class="go" id="finalize-go" '
            'onclick="jumpToNextFinalizeAdded()">표시된 곳 보기 →</button>'
            '<button type="button" class="dismiss" onclick="dismissFinalizeBanner()">확인했어요</button>'
            "</div></div>"
        )
    else:
        finalize_banner_html = ""
    # [추가: 2026-08-20] 배너를 "확인했어요"로 닫으면 이 회차 동안은(탭을 새로 열어도)
    # 다시 안 뜨게 — 회차별 키라 다음 회차엔 새로 뜬다. 마감 배너(sessionStorage, 탭
    # 닫으면 초기화)와 달리 localStorage를 쓴다: 이건 "지나간 알림"이라 매번 탭을 새로
    # 열 때마다 다시 뜨면 오히려 신뢰를 잃는다(사용자 확인).
    finalize_dismiss_key = (
        f"finalizeBannerDismissed:{(run_at or datetime.now().isoformat())[:10]}:{run_slot}"
    )
    total_count_html = (
        f'<span class="total-count-badge" title="이 회차 기사 {total_article_count}건 — 📂 소제목 미분류 포함, 📌 담아둔 기사 제외">{total_article_count}건</span>'
        if total_article_count
        else ""
    ) + pinned_jump_badge_html(len(manual_articles))
    return _PAGE_TEMPLATE.format(
        run_slot=html.escape(format_slot_time_kr(run_slot)),
        run_slot_raw=html.escape(run_slot),
        total_count_html=total_count_html,
        body=body,
        send_countdown_html=send_countdown_html,
        countdown_suffix=countdown_suffix,
        action_fab_html=action_fab_html,
        regen_btn_html=regen_btn_html,
        photo_gather_btn_html=photo_gather_btn_html,
        undo_fab_html=undo_fab_html,
        grace_deadline_ms=grace_deadline_ms,
        # [추가: 2026-08-13] "새로 들어온 기사"(노랑) 비교 기준선을 담는 localStorage 키 —
        # (날짜, 회차)마다 달라야 회차가 바뀔 때 화면 전체가 노래지지 않는다. run_at이
        # 없는 호출(레거시 데이터)이면 오늘 날짜로 떨어뜨린다.
        new_arrival_key=(
            f"confirmedKnownUrls:{(run_at or datetime.now().isoformat())[:10]}:{run_slot}"
        ),
        finalize_banner_html=finalize_banner_html,
        late_pickup_banner_html=late_pickup_banner_html,
        late_badge_style=late_badge_style(),
        export_links_style=export_links_style(),
        kw_inline_style=kw_inline_style(),
        kw_inline_script=kw_inline_script(),
        photo_gather_style=photo_gather_style(),
        # 회차마다 다른 키 — 다음 회차 확정본이 모아 보기로 열리지 않게.
        photo_gather_script=photo_gather_script(json.dumps(
            f"photoGather:{(run_at or datetime.now().isoformat())[:10]}:{run_slot}"
        )),
        finalize_dismiss_key=finalize_dismiss_key,
        # [수정: 2026-08-26] 메모 칸 관련 변수 7개는 keyword_note_template_vars 한 곳에서
        # 만든다(확정본·초안 공용).
        **note_vars,
        # json.dumps로 JS 문자열 리터럴로 안전하게 이스케이프하고, "</script"가 섞여
        # 있어도 스크립트 태그가 조기 종료되지 않도록 "</"를 "<\/"로 한 번 더 바꾼다.
        plain_text_json=json.dumps(plain_text, ensure_ascii=False).replace("</", "<\\/"),
        highlight_words_json=json.dumps(
            [
                {"word": item["word"], "color_hex": HIGHLIGHT_COLORS[item.get("color", 0) % len(HIGHLIGHT_COLORS)]}
                for item in highlight_words
            ],
            ensure_ascii=False,
        ).replace("</", "<\\/"),
        export_filename=html.escape(export_filename),
        export_excel_filename=html.escape(export_excel_filename),
        export_rows_json=html.escape(json.dumps(export_rows, ensure_ascii=False)),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        hidden_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden",
        hidden_trash_style=hidden_trash_style(),
        hidden_trash_html=hidden_trash_html(
            f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden"
        ),
        hidden_trash_script=hidden_trash_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT),
        scroll_top_style=scroll_top_style(),
        scroll_top_html=scroll_top_html(),
        scroll_top_script=scroll_top_script(),
        hide_batch_style=hide_batch_style(),
        hide_batch_script=hide_batch_script(),
        range_select_script=range_select_script(".article-select", ".article"),
        url_add_style=url_add_style(),
        url_add_script=url_add_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT, "confirmed"),
        label_popover_style=label_popover_style(),
        label_popover_script=label_popover_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT),
        # [추가: 2026-09-15] 「AI 기사 나누기」 — 초안과 같은 버튼, 보내는 곳만 확정본용.
        split_button_style=split_button_style(),
        screen_tag_style=screen_tag_style(),
        split_button_html=split_button_html(),
        split_button_script=split_button_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT, "/split-group-final"),
        name_picker_style=name_picker_style(),
        name_picker_html=name_picker_html(),
        # 이름 고르기 창이 쓸 목록 — 이 화면이 보여주는 회차의 **직전** 회차가 기본 목록이
        # 된다(app.subheading_names). 회차 파일 10개를 읽어도 8ms라 매 렌더링 계산해도 무해하다.
        name_picker_script=name_picker_script(name_pool(round_id)),
        live_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/live.html",
        preview_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/preview.html",
        font_stack=FONT_STACK,
        cute_font_face=_CUTE_FONT_FACE_CSS,
        cute_font_name=CUTE_FONT_NAME,
        # [수정: 2026-08-21] 색 값은 전부 app.config.PALETTE 하나에서 온다 — 예전엔
        # seen_color/new_arrival_bg/tonal_hover/ghost_border가 여기 직접 적혀 있었고,
        # 같은 값이 app.preview_renderer에도 따로 적혀 있어 한쪽만 바꾸면 확정본과
        # 초안이 어긋났다.
        **PALETTE,
        reclassify_safe_note=RECLASSIFY_SAFE_NOTE,
        topnav_style=topnav_style(),
        topnav_html=regular_nav("final"),
    )


# [추가: 2026-07-26] 오늘 아직 완료된 회차가 하나도 없을 때(앱을 막 켰거나, 자정이 지나
# 어제 회차만 남아 있을 때) index.html 자리에 대신 보여줄 안내 화면. main.py(최초 실행 시)와
# app.scheduler.run_scheduler(매 tick, 자정 이후 오늘 첫 회차 전까지) 둘 다에서 쓴다 —
# 자정을 넘겨도 어제 스크랩 화면이 계속 떠 있는 걸 막기 위해서다.
def _waiting_page_html() -> str:
    """generate_waiting_page가 호출 시점마다 새로 만든다.

    [수정: 2026-08-20] 예전엔 모듈 임포트 시점에 f-string으로 한 번만 굳힌 상수
    (_WAITING_PAGE)였다 — naver_is_configured()가 설정 화면에서 키를 등록하는 순간
    바뀔 수 있는 값이라, 매번 다시 읽어야 "키를 막 등록했는데 안내 문구는 예전
    그대로"인 상태가 안 생긴다. 키가 있을 때의 문구·아이콘은 그대로 두고(기존
    test_stale_run_shows_waiting_page가 "🐰"·"⏱️"만 확인하므로 아이콘은 두 경우 모두
    유지), 없을 때만 원인과 등록 링크를 덧붙인다."""
    if naver_is_configured():
        message = "아직 스크랩 시간 전입니다.<br>예정 시각이 되면 자동으로 시작됩니다."
    else:
        naver_href = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/naver"
        message = (
            "네이버 검색 API 키가 없어 수집이 시작되지 않습니다.<br>"
            f'<a href="{naver_href}" style="color:{COLOR_ACCENT};font-weight:600;">'
            "설정 &gt; 연동에서 키를 등록해주세요 →</a>"
        )
    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8" /><title>확정본</title>
<style>body{{margin:0;background:{COLOR_BG};color:{COLOR_TEXT};font-family:{FONT_STACK};
display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;
font-size: var(--fs-lg);text-align:center;padding:60px 24px 0;line-height:1.8;box-sizing:border-box;}}
.icon{{font-size:2rem;}}
{topnav_style()}
.reload-btn{{display:inline-block;margin-top:14px;font-size: var(--fs-base);font-weight:600;color:{COLOR_ACCENT};
border:1px solid {COLOR_ACCENT_BORDER};background:{COLOR_CARD};border-radius:var(--r-md);padding:6px 16px;text-decoration:none;}}
.reload-btn:hover{{background:{COLOR_HOVER};}}</style>
</head>
<body>
{regular_nav("final")}
<div><div class="icon">🐰⏱️</div>{message}<br><a class="reload-btn" href="index.html">새로고침</a></div>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# [추가: 2026-09-02] 좌하단 휴지통(숨긴 기사) — 확정본·초안 공용
#
# 수시 수집 결과 화면(app/adhoc/renderer.py)의 좌하단 스택과 **겉모습을 똑같이** 맞춘다
# (아래가 휴지통, 위가 ↩ 되돌리기, 빨간 배지, 0건이면 아예 안 그림) — 담당자가 두 흐름을
# 오갈 때 같은 자리에 같은 것이 있어야 하기 때문. 안쪽은 다르다:
#   · 이름이 "숨긴 기사"다. 수시의 "목록 밖 기사"는 조건 밖 + 직접 숨김 두 종류를 담는
#     이름이었지만, 정기엔 조건 밖 개념이 없어 전부 담당자가 직접 누른 것이고 전부 복구된다.
#   · 팝오버는 요약만 맡는다 — 정기는 소제목 통째 숨기기로 한 번에 수십 건이 들어와,
#     한 건씩 늘어놓으면 팝오버가 스크롤 지옥이 된다. "한 번에 숨긴 덩어리"는 한 줄로
#     접고(app.curation.load_hidden_batches), 그 안에서 골라내는 일은 "더보기 → /hidden"이
#     맡는다. 그래서 이 팝오버에는 묶음을 펼치는 장치가 없다.
#   · 모수가 하루 전역이다 — 초안·확정본·실시간 어디서 숨겼든 같은 목록이라, 두 화면의
#     배지가 같은 숫자일 수 있고 그게 정상이다. 회차 교집합으로 거르면 초안에서 숨긴
#     기사(아직 회차 파일에 없다)가 어디에서도 안 보이는 막다른 길이 생긴다.
_TRASH_POPOVER_MAX_ROWS = 5

HIDDEN_TRASH_STYLE = """
  /* 좌하단 휴지통 — 수시 화면(.fab.left)과 같은 규격. ↩ 되돌리기가 위로 올라간다. */
  .trash-fab {{
    position: fixed; left: 20px; bottom: 20px; width: var(--fab-lg); height: var(--fab-lg); border-radius: var(--r-circle);
    background: {card}; color: {muted}; border: 1px solid {border};
    box-shadow: var(--sh-float); cursor: pointer; z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .trash-fab:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .trash-fab .badge {{
    position: absolute; top: -3px; right: -3px; background: {error}; color: {on_fill};
    font-size: var(--fs-xs); font-weight: 700; border-radius: var(--r-pill); padding: 0 5px; line-height: 1.55;
  }}
  .trash-pop {{
    display: none; position: fixed; left: 20px; bottom: 88px; width: 336px;
    background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    box-shadow: var(--sh-pop); padding: 12px 14px; z-index: 210;
  }}
  .trash-pop.is-open {{ display: block; }}
  .trash-pop h5 {{
    margin: 0 0 3px; font-size: var(--fs-md); color: {header};
    display: flex; align-items: center; gap: 6px;
  }}
  .trash-pop .tnote {{ font-size: var(--fs-xs); color: {muted}; margin: 0 0 4px; line-height: 1.45; }}
  .trash-pop .trow {{
    display: flex; gap: 8px; align-items: center; padding: 8px 0;
    border-top: 1px solid {divider_soft}; font-size: var(--fs-sm);
  }}
  .trash-pop .trow .tx {{ flex: 1; color: {muted}; line-height: 1.4; }}
  .trash-pop .trow .tx b {{ color: {header}; }}
  .trash-pop .trow .tm {{ color: {text_faint}; font-size: var(--fs-xs); margin-left: 4px; }}
  .trash-pop .cnt {{
    flex: none; font-size: var(--fs-xs); font-weight: 700; color: {muted};
    background: {pill_bg}; border-radius: var(--r-pill); padding: 1px 7px;
  }}
  .trash-pop .undo-mini {{
    flex: none; display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-xs);
    color: {muted}; background: transparent; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 3px 8px; cursor: pointer; white-space: nowrap;
  }}
  .trash-pop .undo-mini:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .trash-pop .undo-mini svg {{ width: 13px; height: 13px; }}
  .trash-pop .more {{
    display: block; text-align: center; margin: 6px -14px -12px; padding: 9px 0 10px;
    border-top: 1px solid {border}; font-size: var(--fs-sm); font-weight: 600;
    color: {accent}; text-decoration: none; border-radius: 0 0 var(--r-lg) var(--r-lg);
  }}
  .trash-pop .more:hover {{ background: {hover}; }}
"""


# ── 소제목 이름 고르기 창 (✏️ 이름 바꾸기 · + 새 소제목 만들기 공용) ──────────────
# [추가: 2026-09-03] 예전엔 둘 다 브라우저 기본 prompt() 창이라, AI가 회차마다 새로 지은
# 이름을 같은 사안의 옛 이름으로 되돌리려면 매번 손으로 다시 쳐야 했다(app.subheading_names의
# 주석에 실측이 있다). 이 창은 **직전 회차 소제목을 그대로 보여주고**, 치면 오늘·어제 전부에서
# 찾는다. 확정본·초안 두 화면이 같은 함수를 쓴다 — 각자 복제하면 한쪽만 고쳐져 갈라진다
# (render_article·휴지통과 같은 이유).
NAME_PICKER_STYLE = """
  #name-picker-back {{ position: fixed; inset: 0; z-index: 39; background: rgba(15,23,42,0.12); }}
  #name-picker {{ position: fixed; top: 13vh; left: 50%; transform: translateX(-50%); z-index: 40;
    width: 372px; max-width: calc(100vw - 32px); background: {card}; border: 1px solid {border};
    border-radius: var(--r-lg); box-shadow: var(--sh-modal); padding: 14px 14px 10px; }}
  .np-title {{ font-size: var(--fs-md); font-weight: 700; color: {header}; margin: 0 0 9px; }}
  #np-input {{ width: 100%; font: inherit; font-size: var(--fs-base); padding: 8px 10px;
    border: 1px solid {border}; border-radius: var(--r-md); color: {text}; background: {card}; }}
  #np-input:focus {{ outline: none; border-color: {accent}; box-shadow: 0 0 0 3px {hover}; }}
  #np-input.is-bad {{ border-color: {error}; box-shadow: 0 0 0 3px {error_bg}; }}
  .np-lead {{ font-size: var(--fs-sm); color: {muted}; margin: 11px 6px 2px; }}
  .np-list {{ max-height: 250px; overflow-y: auto; margin: 0 -4px; }}
  .np-row {{ display: flex; align-items: baseline; gap: 8px; width: calc(100% - 8px);
    margin: 0 4px; padding: 5px 8px; border: none; background: none; border-radius: var(--r-sm);
    font: inherit; text-align: left; cursor: pointer; color: {text}; }}
  .np-row:hover {{ background: {hover}; }}
  .np-row .np-nm {{ flex: 1; font-size: var(--fs-md); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  /* 볼드 = 담당자가 직접 고치거나 만든 이름. 직접 만든 소제목이 「다른 소제목」 드롭다운·미니
     목차에서 이미 볼드인 것과 같은 뜻이라 새 표시를 만들지 않고 그 관례를 그대로 이어쓴다. */
  .np-row.np-mine .np-nm {{ font-weight: 700; }}
  .np-row .np-when {{ font-size: var(--fs-xs); color: {muted}; white-space: nowrap; }}
  .np-row.np-used {{ color: {muted}; cursor: not-allowed; }}
  .np-row.np-used:hover {{ background: none; }}
  .np-row.np-used .np-nm {{ text-decoration: line-through; text-decoration-color: {chip_off_border}; font-weight: 400; }}
  .np-row .np-tag {{ font-size: var(--fs-xs); color: {muted}; border: 1px solid {border};
    border-radius: var(--r-pill); padding: 0 6px; white-space: nowrap; }}
  /* [추가: 2026-09-10] 회차 구분선 — 기본 목록(안 쳤을 때)에만 그린다. 치면 걸린 이름이
     대개 1~3건인데 서로 다른 회차라 구분선이 이름 수만큼 붙어(실측 '개각' 2건 → 4줄) 줄만
     두 배가 되고, 그때 담당자가 묻는 건 "몇 회차 것인가"가 아니라 "그 글자가 든 이름이
     있나"라서다. 줄 오른쪽 .np-when이 이미 회차를 말하므로 같은 말을 두 번 하는 셈이기도 하다. */
  .np-sep {{ font-size: var(--fs-xs); color: {muted}; margin: 9px 8px 2px; padding-top: 7px;
    border-top: 1px dashed {border}; }}
  .np-sep:first-child {{ border-top: none; padding-top: 0; margin-top: 2px; }}
  .np-hint {{ color: {muted}; font-size: var(--fs-sm); margin: 7px 6px 0; opacity: 0.85; }}
  .np-foot {{ display: flex; align-items: center; justify-content: space-between; gap: 10px;
    margin-top: 10px; padding-top: 9px; border-top: 1px solid {border}; }}
  .np-msg {{ font-size: var(--fs-sm); color: {muted}; flex: 1; }}
  .np-msg.bad {{ color: {error}; }}
  .np-msg.new {{ color: {accent}; }}
  .np-btns {{ display: flex; gap: 6px; }}
  .np-btns button {{ font: inherit; font-size: var(--fs-md); padding: 6px 13px; border-radius: var(--r-md); cursor: pointer; }}
  .np-cancel {{ background: {card}; color: {muted}; border: 1px solid {border}; }}
  .np-ok {{ background: {accent}; color: {on_fill}; border: none; font-weight: 700; }}
  .np-ok:hover {{ background: {accent_pressed}; }}
  .np-ok:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .np-key {{ font-size: var(--fs-xs); color: {muted}; text-align: right; margin: 6px 2px 0; }}
"""


def name_picker_style() -> str:
    """이름 고르기 창 CSS — 템플릿에 {{name_picker_style}}로 끼워 넣는다."""
    return NAME_PICKER_STYLE.format(**PALETTE)


def name_picker_html() -> str:
    """이름 고르기 창 마크업 — 화면당 하나만 두고 열 때마다 내용을 새로 그린다."""
    return """
<div id="name-picker-back" hidden onclick="closeNamePicker()"></div>
<div id="name-picker" role="dialog" hidden>
  <p class="np-title"></p>
  <input type="text" id="np-input" autocomplete="off" spellcheck="false">
  <p class="np-lead"></p>
  <div class="np-list"></div>
  <p class="np-hint"></p>
  <div class="np-foot"><span class="np-msg"></span><span class="np-btns">
    <button type="button" class="np-cancel" onclick="closeNamePicker()">취소</button>
    <button type="button" class="np-ok"></button>
  </span></div>
  <p class="np-key">Enter 적용 · Esc 닫기</p>
</div>
"""


def name_picker_script(pool: dict) -> str:
    """이름 고르기 창 JS — 템플릿에 {{name_picker_script}}로 끼워 넣는다.

    pool은 app.subheading_names.name_pool()이 만든 값(오늘·어제 전 회차의 소제목 이름)을
    렌더링 시점에 구워 넣은 것이다 — 창을 열 때 서버에 다시 묻지 않는다(실측 8ms).

    **고르는 것과 적용하는 것은 분리돼 있다.** 이 창은 이름 문자열 하나를 골라
    onPick으로 넘길 뿐이고, 실제 저장은 예전 그대로 각 화면의 /rename-group ·
    /add-custom-group 호출이 한다.
    """
    return """
var NAME_POOL = __POOL__;
// _npPristine: 창을 연 뒤 담당자가 아직 한 글자도 안 쳤다는 뜻. 이름 바꾸기는 현재 이름을
// 미리 채워 여는데(아래), 그 값을 곧바로 검색어로 쓰면 (1) 목록이 그 이름으로 걸러져 열자마자
// 「찾은 이름이 없어요」 상태로 시작하고 (2) 화면에 버젓이 있는 이름을 "처음 쓰는 이름"이라고
// 말하게 된다. 그래서 첫 입력 전까지는 프리필을 "검색어가 아닌 것"으로 다룬다.
var _npOnPick = null, _npTaken = [], _npMode = "rename", _npPristine = true;
function openNamePicker(opts) {
  _npOnPick = opts.onPick;
  _npMode = opts.mode;
  // 겹침 판정은 "지금 이 화면에 실제로 그려진 소제목의 표시 이름" — 서버의 409 검사와 같은
  // 기준(active_names)이라, 여기서 막힌 이름은 서버에서도 막힌다. 다만 여기선 **누르기 전에**
  // 보인다는 게 다르다(취소선).
  _npTaken = getAllGroups().map(function(g) { return g.label; }).filter(function(n) { return n !== opts.self; });
  var box = document.getElementById("name-picker");
  box.querySelector(".np-title").textContent = _npMode === "rename" ? "소제목 이름 바꾸기" : "새 소제목 만들기";
  box.querySelector(".np-ok").textContent = _npMode === "rename" ? "바꾸기" : "만들기";
  var input = document.getElementById("np-input");
  input.placeholder = _npMode === "rename" ? "새 이름" : "소제목 이름";
  // [수정: 2026-09-09] 이름 바꾸기는 현재 이름을 미리 채운다 — 예전 prompt()의 두 번째 인자가
  // 하던 일이고(2026-09-03에 이 창으로 옮기며 딸려 나갔다), 없으면 한 글자만 고치려 해도 이름
  // 전체를 다시 쳐야 한다. 전체 선택한 채로 여므로 통째로 새로 치는 쪽은 예전처럼 그냥 치면 된다.
  // 만들기는 채울 "현재 이름"이 없으므로 예전 그대로 빈 칸이다.
  input.value = _npMode === "rename" ? (opts.self || "") : "";
  _npPristine = true;
  document.getElementById("name-picker-back").hidden = false;
  box.hidden = false;
  _npCheck();
  input.focus();
  input.select();
}
function closeNamePicker() {
  document.getElementById("name-picker").hidden = true;
  document.getElementById("name-picker-back").hidden = true;
  _npOnPick = null;
}
function _npRender(q) {
  var box = document.getElementById("name-picker");
  var list = box.querySelector(".np-list");
  var lead = box.querySelector(".np-lead"), hint = box.querySelector(".np-hint");
  // [수정: 2026-09-10] 기본 목록이 오늘·어제 전부다(예전엔 직전 회차 하나 — 그 회차가 비면
  // 목록이 0줄이 돼 창이 통째로 입력칸만 남았다. app/subheading_names.py 참고).
  var hits = q ? NAME_POOL.all.filter(function(i) { return i.name.indexOf(q) >= 0; }) : null;
  // 걸린 이름이 없으면 기본 목록으로 되돌아간다 — 이름 바꾸기는 현재 이름을 미리 채워 열기
  // 때문에, 가운데를 눌러 한 글자만 고치면 그 긴 이름이 그대로 검색어가 돼 반드시 0건이 된다.
  // 그때 목록을 숨기면 "고치려고 열었더니 목록이 사라지는" 화면이 된다(2026-09-10 제보).
  var fellBack = !!(q && !hits.length);
  var items = (q && !fellBack) ? hits : NAME_POOL.all;
  // 구분선은 기본 목록일 때만(위 .np-sep 주석 참고). 되돌아온 목록도 기본 목록이다.
  var grouped = !q || fellBack;
  list.innerHTML = "";
  // 오늘·어제에 저장된 회차가 아예 없을 때만 목록 자리(안내줄·목록·힌트)를 통째로 안 그린다 —
  // 창이 입력칸+버튼만 남아 예전 prompt() 크기가 된다(2026-09-04 규칙 그대로).
  if (!items.length) {
    lead.hidden = true; list.hidden = true; hint.hidden = true;
    return;
  }
  var hintText = grouped ? "치면 그 자리에서 걸러져요" : "";
  lead.hidden = false; list.hidden = false; hint.hidden = !hintText;
  lead.textContent = fellBack ? "찾은 이름이 없어요 · 최근 쓴 이름"
                    : (q ? "오늘·어제에서 찾은 이름" : "최근 쓴 이름 · 오늘·어제");
  hint.textContent = hintText;
  var lastWhen = null;
  items.forEach(function(item) {
    if (grouped && item.when !== lastWhen) {
      var sep = document.createElement("p");
      sep.className = "np-sep";
      // 최신 회차순이라 첫 묶음이 곧 직전 회차 — 그 사실만 여기 적고 목록을 따로 뽑지 않는다.
      sep.textContent = item.when + (lastWhen === null ? " · 직전 회차" : "");
      list.appendChild(sep);
      lastWhen = item.when;
    }
    var used = _npTaken.indexOf(item.name) >= 0;
    var row = document.createElement("button");
    row.type = "button";
    row.className = "np-row" + (used ? " np-used" : "") + (item.kind === "mine" ? " np-mine" : "");
    row.innerHTML = '<span class="np-nm"></span>' + (used ? '<span class="np-tag">이 화면에 있음</span>' : '') +
                    '<span class="np-when"></span>';
    row.querySelector(".np-nm").textContent = item.name;
    // 구분선이 회차를 말하는 자리에서는 줄마다 또 적지 않는다(같은 말 두 번).
    row.querySelector(".np-when").textContent = grouped ? "" : (item.when || "");
    if (used) { row.title = "이 화면에 이미 있는 이름이에요"; }
    else { row.onclick = function() { var i = document.getElementById("np-input"); i.value = item.name; _npPristine = false; i.focus(); _npCheck(); }; }
    list.appendChild(row);
  });
}
function _npCheck() {
  var box = document.getElementById("name-picker");
  var input = document.getElementById("np-input");
  var v = input.value.trim();
  var clash = _npTaken.indexOf(v) >= 0;
  var known = NAME_POOL.all.some(function(i) { return i.name === v; });
  var msg = box.querySelector(".np-msg");
  input.classList.toggle("is-bad", clash);
  box.querySelector(".np-ok").disabled = !v || clash;
  // 미리 채워둔 이름은 담당자가 친 글자가 아니라 "지금 이 소제목의 이름"이다 — 아직 아무것도
  // 안 쳤으면 아랫줄을 비우고(그 이름은 화면에 있으니 "처음 쓰는 이름"이 거짓말이다) 목록도
  // 안 거른다(직전 회차 목록이 이 창의 존재 이유다).
  var q = _npPristine ? "" : v;
  msg.className = "np-msg" + (clash ? " bad" : (q && !known ? " new" : ""));
  msg.textContent = clash ? "이 화면에 이미 있는 이름이에요" : (!q ? "" : (known ? "" : "처음 쓰는 이름이에요"));
  _npRender(q);
}
(function() {
  var input = document.getElementById("np-input");
  var ok = document.getElementById("name-picker").querySelector(".np-ok");
  input.addEventListener("input", function() { _npPristine = false; _npCheck(); });
  input.addEventListener("keydown", function(e) {
    if (e.key === "Enter" && !ok.disabled) { e.preventDefault(); ok.click(); }
    if (e.key === "Escape") { e.preventDefault(); closeNamePicker(); }
  });
  ok.onclick = function() {
    var v = document.getElementById("np-input").value.trim();
    var pick = _npOnPick;
    if (!v || !pick) { return; }
    closeNamePicker();   // 먼저 닫는다 — 실패 alert이 창 뒤에 가리지 않도록
    pick(v);
  };
})();
""".replace("__POOL__", json.dumps(pool, ensure_ascii=False))


LATE_BADGE_STYLE = """
  /* [추가: 2026-09-03] "앞 회차 기사" 칩 — 회차 마감 순간 네이버 검색이 아직 안 주던
     기사를 다음 회차가 주워 담았을 때 붙는다(app.scraper.collect_run의 late_pickup,
     [추가: 2026-09-04] app.preview_renderer._compute_preview_articles의 같은 표식).
     자리는 제목 줄이 아니라 메타 줄이다 — 제목 줄엔 이미 🤖 "마감 후 자동 배정" 배지가
     붙고, 회수된 기사는 그 회차에서 처음 보는 기사라 거의 항상 그 배지도 함께 단다.
     두 표시가 서로 다른 줄을 쓰면 border-left(연보라 띠)를 두고 다툴 일이 없다.
     그래서 이 표시에는 왼쪽 띠를 주지 않는다 — 띠는 "훑을 때 덩어리로 보이는" 자리라
     하나만 써야 하고, "AI가 소제목을 대신 정했다"가 담당자가 손볼 일이 더 크다.
     색은 상단 배너와 같은 "주의"(앰버) 계열이라, 배너에서 [N건]을 눌러 내려왔을 때
     같은 색이 눈에 들어와 "배너가 말한 게 이거"로 이어져 읽힌다. */
  .late-badge {{
    flex-shrink: 0; margin-left: 6px; padding: 1px 7px; border-radius: var(--r-pill); font-size: var(--fs-xs);
    color: {warn_text}; background: {warn_chip_bg}; border: 1px solid {warn_border}; white-space: nowrap;
  }}
"""


def late_badge_style() -> str:
    """"앞 회차 기사" 칩 CSS — 확정본·초안 공용.

    [추가: 2026-09-04] 초안도 이 표식을 달게 되면서(같은 날 초안 검색 하한을 확정본과
    같은 창으로 맞췄다) 두 화면이 같은 규칙을 쓴다. .photo-badge처럼 양쪽에 복붙해 두면
    한쪽만 고쳤을 때 조용히 어긋나므로, name_picker_style/hidden_trash_style과 같은
    방식으로 한 곳에서 만들어 둘 다 이 값을 받아 쓴다.
    """
    return LATE_BADGE_STYLE.format(**PALETTE)


EXPORT_LINKS_STYLE = """
  /* 툴바 오른쪽 「복사 · 텍스트 · 엑셀」 — 확정본·초안 공용. 툴바 규칙: 박스는 내용을 바꾸는
     버튼(왼쪽), 글자는 가져가거나 보기만 바꾸는 것(오른쪽). 그래서 .actions button의 옅은
     파란 채움을 끄고 글자만 남긴다. hover는 배경 없이 글자색 + 밑줄.
     시안 mockups/TOOLBAR_GROUPING_MOCKUP.html A안. */
  .actions .actions-right {{ margin-left: auto; display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .actions .actions-right .view-mode-select {{ margin-left: 0; }}
  .actions .export-links {{ display: inline-flex; align-items: center; }}
  .actions .export-links button {{
    background: transparent; border: none; color: {text_soft}; font-weight: 500;
    padding: 6px 8px; border-radius: var(--r-md);
  }}
  .actions .export-links button:hover {{
    background: transparent; color: {accent}; text-decoration: underline; text-underline-offset: 3px;
  }}
"""


def export_links_style() -> str:
    """툴바 「복사 · 텍스트 · 엑셀」 글자 버튼 CSS — 확정본·초안 공용."""
    return EXPORT_LINKS_STYLE.format(**PALETTE)


def build_export_rows(groups: list, export_date: str, run_slot: str) -> list[dict]:
    """확정본·초안 엑셀 8열 행 — 화면의 소제목 순서대로 groups를 그대로 순회한다.

    보고서 텍스트와 달리 📂 소제목 미분류를 「기타」로 접지 않고 소제목 표시 형식도
    적용하지 않는다(PRD 기능11 규칙4 — 엑셀은 보고서가 아니라 데이터다).
    """
    return [
        {
            "스크랩일자": export_date,
            "스크랩종료시간": run_slot,
            "발행일 발행시간": format_pub_datetime(a.get("pub_date")),
            "언론사명": a.get("outlet", ""),
            "기사제목": a.get("title", ""),
            "URL": a.get("url", ""),
            "소제목": g["name"],
            "라벨명": ", ".join(labels_for_url(a["url"])),
        }
        for g in groups
        for a in g["articles"]
    ]


KW_INLINE_STYLE = """
  /* [추가: 2026-08-21] 🔍 검색어 — 이 기사가 어떤 검색어로 걸렸는지. 게시시각·원문보기와
     같은 줄·같은 크기(0.8rem)의 회색 글자다(칩으로 안 만든 이유는 render_article 주석).
     [수정: 2026-09-14] 남는 자리만큼만 보여주고 나머지는 …로 자른다 — 전부 보여주던 때는
     검색어가 긴 행에서 버튼 묶음이 통째로 아랫줄로 밀렸다. 세 값이 짝이다:
     flex-basis 0 → 줄바꿈을 판단할 때 검색어 길이를 안 친다(그래서 버튼을 밀어내지 않는다),
     flex-grow 1 + max-width: max-content → 남는 자리를 제 글자 길이까지만 채운다(남는 건
     버튼 묶음의 margin-left: auto가 가져가 버튼이 오른쪽 끝에 그대로 선다),
     min-width 5em → 그보다 자리가 적으면(좁은 화면) 검색어가 버튼과 함께 다음 줄로 넘어간다.
     고정 폭(실시간 현황의 210px)으로 자르지 않은 이유: 「⏱ 늦게 들어온 기사」 칩이 붙는
     행은 칩 폭(약 110px)만큼 자리가 모자라, 칩 없는 행 기준으로 정한 값으로는 여전히
     밀린다(KW_CHIP_MOCKUP.html 실측). 확정본·초안 공용 — 한 곳에서만 고친다. */
  .kw-inline {{
    font-size: var(--fs-sm); color: {muted};
    display: inline-flex; align-items: center; gap: 4px;
    flex: 1 1 0; min-width: 5em; max-width: max-content;
  }}
  /* 구분점은 span 안에 있다(다음 줄로 넘어갈 때 점만 윗줄에 남지 않게). 오른쪽 여백은
     밖에 있던 때의 간격(점 2px + 줄 gap 8px)을 그대로 맞춘 값. */
  .kw-inline > .time-sep {{ margin-right: 6px; }}
  /* 돋보기는 값보다 한 톤 흐리게 — 먼저 읽혀야 하는 건 검색어 자체다. 크기는 바로 옆
     원문보기 아이콘(.origin-link-icon)과 같은 0.75em이라 액션 줄에서 높이가 안 튄다. */
  .kw-inline .kw-ic {{ width: 0.75em; height: 0.75em; color: {text_faint_alt}; flex-shrink: 0; }}
  .kw-inline .kw-v {{
    color: {text_soft}; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
"""


def kw_inline_style() -> str:
    """🔍 검색어 CSS — 확정본·초안 공용(late_badge_style과 같은 방식)."""
    return KW_INLINE_STYLE.format(**PALETTE)


def kw_inline_script() -> str:
    """잘린 🔍 검색어에 마우스를 올리면 전체를 툴팁으로 — 확정본·초안 공용.

    잘림 여부는 창 폭에 따라 달라지므로(남는 자리만큼 자른다) 페이지를 열 때 한 번이
    아니라 마우스를 올리는 순간 판정한다. 안 잘린 행은 원래 설명 툴팁을 그대로 둔다.
    문서에 한 번 거는 위임이라 초안이 #preview-body를 통째로 갈아끼워도 그대로 동작한다.
    """
    return """
document.addEventListener("mouseover", function (e) {
  var kw = e.target.closest ? e.target.closest(".kw-inline") : null;
  if (!kw) { return; }
  var v = kw.querySelector(".kw-v");
  if (!v) { return; }
  if (kw.dataset.tip === undefined) { kw.dataset.tip = kw.title; }
  kw.title = v.scrollWidth > v.clientWidth + 1 ? v.textContent : kw.dataset.tip;
});
"""


# [추가: 2026-09-15] 📷 사진 추정 모아 보기 — 확정본·초안 공용(PHOTO_GROUP_MOCKUP.html B안).
# 사진 추정 행만 한 목록으로 모아 보여주는 **화면 전용 보기**다 — 「보기 순서」(시간순·
# 언론사순)와 같은 성격이라 저장 데이터·복사·txt·발송 텍스트는 한 글자도 안 바뀐다.
# 소제목 칸에서 사진을 늘 빼두는 안(A)은 기각했다: 숨기기 전까진 보고서엔 원래 소제목으로
# 나가는데 화면엔 거기 없어 화면과 보고서가 어긋난다.
PHOTO_GATHER_STYLE = """
  /* 툴바 오른쪽 「📷 사진 추정 (N)」 — 모양 규칙대로 알약이다(알약 = 상태를 알려 주고
     누르면 켜고 끄기만). 화면만 바꾸는 보기라 「보고서 내용을 바꾸는」 왼쪽 네모 버튼과
     섞이면 안 되고, 복사·텍스트·엑셀·「보기 순서」와 같은 오른쪽 묶음에 선다.
     .actions button(0,1,1)보다 이 선택자(0,2,0)가 구체적이라 툴바 기본 채움을 덮는다.
     시안 mockups/TOOLBAR_DEPTH_MOCKUP.html C안. */
  .actions .photo-gather-btn {{
    background: {card}; color: {text_soft}; border: 1px solid {border};
    border-radius: var(--r-pill); font-weight: 500; padding: 0 12px;
  }}
  .actions .photo-gather-btn:hover {{
    background: {hover}; color: {accent}; border-color: {accent_border};
  }}
  /* 켜진 동안 채운 파랑으로 — 지금 화면이 평소 화면이 아니라는 표시. 모아 보는 동안은
     소제목·📌·요약이 모두 숨으므로, 되돌아갈 스위치가 어디 있는지가 약해지면 안 된다
     (그래서 글자 버튼으로 내리지 않고 알약으로 뒀다). */
  .actions .photo-gather-btn.is-on, .actions .photo-gather-btn.is-on:hover {{
    background: {accent}; color: {on_fill}; border-color: {accent};
  }}
  /* 모아 보는 중 띠 — 수십 건을 훑으며 체크를 풀어도 「전체 선택」·「원래 화면으로」가
     화면 위로 사라지지 않게 sticky. 자리는 키워드 메모 칸과 같다(그 칸은 이 동안 숨는다).
     상단 배너가 topbar를 밀어내면 같은 변수만큼 따라 내려간다(메모 칸과 같은 규칙). */
  .photo-gather-strip {{
    position: sticky; top: 60px; z-index: 15; display: flex; align-items: center; gap: 10px;
    flex-wrap: wrap; background: {hover}; border: 1px solid {accent_border}; border-radius: var(--r-lg);
    padding: 9px 12px; margin: 12px 0 6px;
  }}
  body.has-finalize-banner .photo-gather-strip {{ top: calc(60px + var(--finalize-banner-h, 0px)); }}
  body.round-over .photo-gather-strip {{ top: calc(60px + var(--round-banner-h, 0px)); }}
  .photo-gather-strip label {{ display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-md); cursor: pointer; }}
  .photo-gather-strip .pg-msg {{ font-size: var(--fs-md); color: {header}; }}
  .photo-gather-strip .pg-sp {{ flex: 1; }}
  .photo-gather-strip button {{ padding: 5px 12px; font-size: var(--fs-md); }}
  .pg-done {{ text-align: center; color: {muted}; padding: 28px 0 16px; font-size: var(--fs-md); }}
  /* 원래 소제목 이름표 — 보고서에 나갈 자리. 📂 미분류 칸에서 온 기사엔 안 붙인다. */
  .pg-from {{
    flex-shrink: 0; max-width: 16em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-size: var(--fs-xs); color: {text_soft}; background: {bg}; border: 1px solid {border};
    border-radius: var(--r-md); padding: 0 6px;
  }}
  /* 모아 보는 중엔 전부가 사진 추정이라 흐림·📷 칩이 행마다 같은 값(=장식)이 된다. */
  body.photo-gather .article.is-photo {{ opacity: 1; }}
  body.photo-gather .photo-badge {{ display: none; }}
  /* 목록 밖의 것들은 이 동안 접는다. ↑↓는 소제목 안 순서라 한 줄로 모인 목록에선 뜻이 없다. */
  body.photo-gather .subheading:not(.photo-gather-view),
  body.photo-gather .manual-divider, body.photo-gather .manual-zone, body.photo-gather .bottom,
  body.photo-gather .keyword-note-zone, body.photo-gather .late-banner,
  body.photo-gather .toc-toggle-btn, body.photo-gather .toc-popover,
  body.photo-gather .move-btn, body.photo-gather #bulk-move-up, body.photo-gather #bulk-move-down {{
    display: none !important;
  }}
"""

PHOTO_GATHER_SCRIPT = """
// 📷 사진 추정 모아 보기 — 행을 옮기기 전 자리(부모·다음 형제)를 적어두고, 끌 때 뒤에서부터
// 되돌린다(applyViewMode의 flat-view와 같은 방식 — 앞 행의 next가 아직 모인 목록 안에 있는
// 뒤 행을 가리킬 수 있어서다).
var _pgHomes = null;
function _pgKey() { return PHOTO_GATHER_KEY_FN(); }
function _pgOriginLabel(article) {
  var sec = article.closest(".subheading");
  var btn = sec ? sec.querySelector("h2 .rename-btn[data-current]") : null;
  return btn ? btn.dataset.current : "";  // 📂 소제목 미분류 칸엔 이름 버튼이 없다
}
function _pgRefresh() {
  var strip = document.getElementById("photo-gather-strip");
  var view = document.getElementById("photo-gather-view");
  if (!strip || !view) { return; }
  var n = view.querySelectorAll(".article.is-photo").length;
  var all = strip.querySelector("input");
  all.disabled = n === 0;
  if (n === 0) { all.checked = false; }
  strip.querySelector(".pg-msg").innerHTML = n
    ? PHOTO_GATHER_ICON + " <b>사진 추정 " + n + "건</b>만 모아 보고 있어요"
    : "남은 사진 추정이 없어요";
  var done = view.querySelector(".pg-done");
  if (!n && !done) {
    view.insertAdjacentHTML("beforeend",
      '<div class="pg-done">다 치웠어요. 숨긴 기사는 왼쪽 아래 휴지통에서 되살릴 수 있어요.</div>');
  } else if (n && done) { done.remove(); }
}
function photoGatherOn() {
  if (document.body.classList.contains("photo-gather")) { return; }
  // 「보기 순서」가 시간순·언론사순이면 먼저 소제목별로 돌려놓는다 — 두 보기가 같은 행을
  // 서로 다른 자리로 옮기면 끌 때 제자리를 못 찾는다. 모아 보는 동안엔 잠근다.
  var sel = document.querySelector(".view-mode-select");
  if (sel) {
    if (sel.value !== "subheading") { sel.value = "subheading"; applyViewMode("subheading"); }
    sel.disabled = true;
  }
  // .subheading 안의 행만 — 📌 담아둔 기사 칸(보고서 밖)은 대상이 아니다.
  var rows = Array.prototype.slice.call(document.querySelectorAll(".subheading .article.is-photo"));
  _pgHomes = rows.map(function (el) { return { el: el, parent: el.parentElement, next: el.nextElementSibling }; });
  var anchor = document.querySelector(".subheading");
  var host = anchor ? anchor.parentNode : (document.getElementById("preview-body") || document.body);
  var view = document.createElement("section");
  view.id = "photo-gather-view";
  view.className = "subheading photo-gather-view";
  rows.forEach(function (el) {
    var from = _pgOriginLabel(el);
    var actionRow = el.querySelector(".action-row");
    if (from && actionRow) {
      var tag = document.createElement("span");
      tag.className = "pg-from";
      tag.textContent = from;
      tag.title = "보고서엔 「" + from + "」 소제목으로 나가요";
      actionRow.insertBefore(tag, actionRow.firstChild);
    }
    view.appendChild(el);
  });
  var strip = document.createElement("div");
  strip.id = "photo-gather-strip";
  strip.className = "photo-gather-strip";
  strip.innerHTML = '<label><input type="checkbox" onchange="photoGatherSelectAll(this.checked)"> 전체 선택</label>'
    + '<span class="pg-msg"></span><span class="pg-sp"></span>'
    + '<button type="button" onclick="photoGatherOff()">원래 화면으로</button>';
  if (anchor) { host.insertBefore(view, anchor); } else { host.appendChild(view); }
  host.insertBefore(strip, view);
  document.body.classList.add("photo-gather");
  document.querySelectorAll(".photo-gather-btn").forEach(function (b) { b.classList.add("is-on"); });
  try { sessionStorage.setItem(_pgKey(), "1"); } catch (e) {}
  // 🗑 한 건 숨기기는 새로고침 없이 행만 지우므로 건수를 따라 고친다.
  new MutationObserver(_pgRefresh).observe(view, { childList: true });
  _pgRefresh();
  if (typeof updateBulkMoveBar === "function") { updateBulkMoveBar(); }
}
function photoGatherOff() {
  if (!document.body.classList.contains("photo-gather")) { return; }
  (_pgHomes || []).slice().reverse().forEach(function (home) {
    if (!document.body.contains(home.el)) { return; }
    if (home.next && home.next.parentNode === home.parent) { home.parent.insertBefore(home.el, home.next); }
    else { home.parent.appendChild(home.el); }
  });
  _pgHomes = null;
  document.querySelectorAll(".pg-from").forEach(function (t) { t.remove(); });
  ["photo-gather-strip", "photo-gather-view"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) { el.remove(); }
  });
  document.body.classList.remove("photo-gather");
  document.querySelectorAll(".photo-gather-btn").forEach(function (b) { b.classList.remove("is-on"); });
  var sel = document.querySelector(".view-mode-select");
  if (sel) { sel.disabled = false; }
  try { sessionStorage.removeItem(_pgKey()); } catch (e) {}
  if (typeof updateBulkMoveBar === "function") { updateBulkMoveBar(); }
  window.scrollTo(0, 0);
}
function togglePhotoGather() {
  if (document.body.classList.contains("photo-gather")) { photoGatherOff(); return; }
  photoGatherOn();
  window.scrollTo(0, 0);
}
// 화면에 보이는 사진 추정 행만 고른다.
function photoGatherSelectAll(on) {
  var view = document.getElementById("photo-gather-view");
  if (!view) { return; }
  view.querySelectorAll(".article.is-photo .article-select").forEach(function (cb) {
    if (cb.closest(".article").offsetParent !== null) { cb.checked = on; }
  });
  if (typeof updateBulkMoveBar === "function") { updateBulkMoveBar(); }
}
// 숨기기·배정 등은 화면을 새로고침한다 — 이 회차 동안은 탭 안에서 모아 보기를 기억해
// 새로고침 뒤에도 그대로 이어간다. 키(회차)를 정하는 상수가 뒤쪽 스크립트에 있을 수 있어
// 모든 스크립트가 돈 뒤(DOMContentLoaded)에 판단한다.
document.addEventListener("DOMContentLoaded", function () {
  try { if (sessionStorage.getItem(_pgKey())) { photoGatherOn(); } } catch (e) {}
});
"""


def photo_gather_style() -> str:
    """📷 사진 추정 모아 보기 CSS — 확정본·초안 공용(kw_inline_style과 같은 방식)."""
    return PHOTO_GATHER_STYLE.format(**PALETTE)


def photo_gather_script(key_js: str) -> str:
    """📷 사진 추정 모아 보기 JS — 확정본·초안 공용.

    key_js: 모아 보기 상태를 기억할 sessionStorage 키를 만드는 **JS 식**. 회차마다 달라야
    다음 회차 화면이 모아 보기로 열리지 않는다(확정본은 날짜·회차 문자열, 초안은
    ROUND_DEADLINE_MS — 그 상수가 이 스크립트보다 뒤에 선언돼 있어 함수로 감싸 늦게 읽는다).
    """
    return (
        f"var PHOTO_GATHER_ICON = {json.dumps(icon('camera'))};\n"
        f"function PHOTO_GATHER_KEY_FN() {{ return {key_js}; }}\n"
        + PHOTO_GATHER_SCRIPT
    )


def photo_gather_button_html(count: int) -> str:
    """툴바 오른쪽 묶음의 「📷 사진 추정 (N)」 알약 — 사진 추정이 0건이면(가장 흔한 정상
    상태) 버튼 자체를 안 그린다(예전 「사진 추정 전체 선택」과 같은 규칙).

    화면만 바꾸는 보기라 왼쪽(보고서 내용을 바꾸는 네모 버튼)이 아니라 오른쪽에 서고,
    모양도 네모가 아니라 알약이다 — 확정본·초안이 같은 함수를 쓴다."""
    if not count:
        return ""
    return (
        '<button class="photo-gather-btn" type="button" onclick="togglePhotoGather()" '
        'title="사진 추정 기사만 한곳에 모아 봅니다 — 보고서(복사·txt·발송)는 그대로예요">'
        f"{icon('camera')} 사진 추정 ({count})</button>"
    )


def hidden_trash_style() -> str:
    """휴지통 CSS — 템플릿 안에 {{hidden_trash_style}}로 끼워 넣는다(이미 값이 채워진 문자열)."""
    return HIDDEN_TRASH_STYLE.format(**PALETTE)


# 맨 위로 ↑ — 확정본·초안이 같이 쓴다(동작을 바꾸면 두 화면이 같이 바뀐다).
# 목차 ☰ 바로 왼쪽, 같은 48px 동그라미·같은 중립색. 한 화면 이상 내려갔을 때만 보인다.
# 자리를 ☰ 위가 아니라 왼쪽으로 둔 건 확정본의 발송 버튼이 ☰ 위에 있어서다 — 두 화면에서 같은 자리.
SCROLL_TOP_STYLE = """
  .top-fab {{
    position: fixed; right: 80px; bottom: 20px; width: var(--fab-sm); height: var(--fab-sm); border-radius: var(--r-circle);
    background: {card}; color: {muted}; border: 1px solid {border}; font-size: 1.2rem; cursor: pointer;
    box-shadow: var(--sh-float); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .top-fab:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .top-fab[hidden] {{ display: none; }}
"""


def scroll_top_style() -> str:
    """맨 위로 버튼 CSS — 템플릿 안에 {{scroll_top_style}}로 끼워 넣는다."""
    return SCROLL_TOP_STYLE.format(**PALETTE)


def scroll_top_html() -> str:
    """맨 위로 버튼 마크업 — 처음엔 숨긴 채로 두고 스크롤 JS가 켠다."""
    return (
        '<button type="button" class="top-fab" id="top-fab" hidden '
        'onclick="window.scrollTo({top: 0, behavior: \'smooth\'})" title="맨 위로" aria-label="맨 위로">'
        f'{icon("to_top")}</button>'
    )


def scroll_top_script() -> str:
    """맨 위로 버튼 JS — 한 화면 이상 내려갔을 때만 보인다."""
    return """
(function() {
  var btn = document.getElementById("top-fab");
  if (!btn) return;
  function sync() { btn.hidden = window.scrollY < window.innerHeight; }
  window.addEventListener("scroll", sync, {passive: true});
  window.addEventListener("resize", sync);
  sync();
})();
"""

# [추가: 2026-09-16] 🏷 라벨 팝오버 CSS/JS — 확정본·초안·**정기 보관함** 세 화면이 같이 쓴다.
# 원래는 app/renderer.py와 app/preview_renderer.py에 같은 값이 복붙돼 있었는데, 실제로 이미
# 갈라져 있었다(초안 쪽 onclick 세 자리에 event.preventDefault()가 빠져 있었다). 정기 보관함이
# 세 번째 사본이 되기 전에 한 곳으로 모은다 — hidden_trash_style/photo_gather_style과 같은 이유.
LABEL_POPOVER_STYLE = """
  /* [추가: 2026-08-18] 🏷 라벨 — PRD.md 기능10, 2026-08-18 목업 대화. 앰버 계열은
     기존 COLOR_* 팔레트(파랑=담당자 액션/보라=AI 액션)와 겹치지 않는 세 번째 축으로
     확정했다(MAIN_FLOW_MOCKUP.html의 --label-bg/--label-text 그대로). */
  .lab-pop-wrap {{ position: relative; display: inline-flex; flex-shrink: 0; }}
  .lab-btn {{
    background: transparent; border: none; color: {muted}; font-size: 1rem; line-height: 1;
    cursor: pointer; padding: 3px 6px; border-radius: var(--r-sm); display: inline-flex; align-items: center;
    user-select: none; -webkit-user-select: none;
  }}
  .lab-btn:hover {{ background: {hover}; color: {accent}; }}
  .lab-btn.on {{ color: {label_text}; background: {label_bg}; }}
  .lab-btn .lab-count {{ font-size: var(--fs-xs); font-weight: 700; margin-left: 2px; }}
  .lab-pop {{
    position: absolute; right: 0; top: 100%; margin-top: 4px; z-index: 60; width: 260px;
    text-align: left; background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    padding: 11px 12px; box-shadow: var(--sh-pop);
  }}
  .lab-pop.hidden {{ display: none; }}
  .lab-pop h4 {{ margin: 0 0 6px; font-size: var(--fs-xs); color: {muted}; font-weight: 700; }}
  .lab-pop .attached {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; min-height: 20px; }}
  .lab-pop .attached .none {{ font-size: var(--fs-xs); color: {muted}; }}
  .lab-pop .known {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }}
  .lab-pop .known .none {{ font-size: var(--fs-xs); color: {muted}; }}
  .lab-chip {{
    display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-xs); font-weight: 700;
    color: {label_text}; background: {label_bg}; border: 1px solid {label_border};
    border-radius: var(--r-pill); padding: 2px 8px; white-space: nowrap;
  }}
  .lab-chip .x {{ color: {label_text}; opacity: 0.55; cursor: pointer; font-weight: 700; }}
  .lab-chip .x:hover {{ opacity: 1; }}
  .known-chip {{
    font-size: var(--fs-xs); font-weight: 600; color: {muted}; cursor: pointer;
    background: {card}; border: 1px solid {border}; border-radius: var(--r-pill); padding: 2px 8px;
  }}
  .known-chip:hover {{ background: {label_bg}; color: {label_text}; border-color: {label_border}; }}
  .known-chip.used {{ background: {label_bg}; color: {label_text}; border-color: {label_border}; font-weight: 700; }}
  .known-chip .n {{ opacity: 0.6; font-weight: 500; margin-left: 3px; }}
  .lab-pop .addbox {{ display: flex; gap: 5px; }}
  .lab-pop .addbox input {{
    flex: 1; font-size: var(--fs-xs); padding: 5px 7px; border: 1px solid {border}; border-radius: var(--r-sm);
  }}
  .lab-pop .addbox button {{
    font-size: var(--fs-xs); font-weight: 700; padding: 5px 10px; border-radius: var(--r-sm); cursor: pointer;
    background: {label_bg}; color: {label_text}; border: 1px solid {label_border};
  }}
  .lab-pop .sim-warn {{
    margin-top: 8px; font-size: var(--fs-xs); line-height: 1.7; color: {label_text}; background: {label_bg};
    border: 1px dashed {label_border}; border-radius: var(--r-md); padding: 6px 8px;
  }}
  .lab-pop .sim-warn.hidden {{ display: none; }}
  .lab-pop .sim-warn button {{
    font-size: var(--fs-xs); font-weight: 700; padding: 3px 8px; border-radius: var(--r-sm); cursor: pointer;
    margin-left: 3px; border: none;
  }}
  .lab-pop .sim-warn .use {{ background: {label_text}; color: {on_fill}; }}
  .lab-pop .sim-warn .keep {{ background: {card}; color: {muted}; border: 1px solid {border}; }}
  .lab-pop .hint {{ font-size: var(--fs-xs); color: {muted}; margin: 8px 0 0; line-height: 1.7; }}
  .lab-row {{
    display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin-top: 6px;
    padding-top: 6px; border-top: 1px dashed {border};
  }}
  .lab-row .tagmark {{ color: {label_text}; opacity: 0.75; }}
"""

LABEL_POPOVER_SCRIPT = """
// [추가: 2026-08-18] 🏷 라벨 — 형광펜 팝오버(toggleHighlightPopover)와 같은 패턴(한
// 번에 하나만 열림 + 바깥 클릭 시 닫힘). 서버가 붙이기/떼기 응답으로 그 기사의
// "지금 라벨 전체 목록"을 돌려주면, 그 목록 하나로 팝오버·카드 줄·배지 셋을 한 번에
// 다시 그린다(추측으로 DOM을 짜맞추면 서버 상태와 어긋날 수 있어, 항상 서버가 돌려준
// 최종 목록을 그대로 믿는다).
var LAB_TAG_ICON_SVG = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.6 12.7 12.7 20.6a2 2 0 0 1-2.8 0l-7.5-7.5a2 2 0 0 1-.6-1.4V4.3A2.3 2.3 0 0 1 4.1 2H11a2 2 0 0 1 1.4.6l8.2 8.2a2 2 0 0 1 0 2.8Z"/><circle cx="7.5" cy="7.5" r="1.4" fill="currentColor" stroke="none"/></svg>';
// [수정: 2026-08-28] "이미 쓴 라벨" 목록(.known)은 페이지를 구울 때 서버가 한 번
// 만들어(known_label_chips_html) 모든 팝오버에 같은 HTML로 박아둔 값이라, 방금 만든
// 라벨이 새로고침 전까지 어느 행의 목록에도 안 나왔다 — 붙이기 응답이 그 기사의
// .attached만 갱신했기 때문. 이제 서버가 응답에 최신 목록(known)을 함께 실어 보내고
// 여기 담아둔다. **열려 있는 팝오버는 다시 그리지 않는다** — 목록이 건수 내림차순이라
// 라벨을 붙이는 순간 칩 순서가 바뀔 수 있고, 그러면 칩을 연속으로 누르던 담당자가
// 커서 밑에서 움직인 엉뚱한 라벨을 누르게 된다. 대신 팝오버를 "열 때" 칠한다
// (toggleLabelPopover가 어차피 열 때마다 used를 다시 매기고 있어 그 자리에 얹힌다).
var KNOWN_CHIPS_HTML = null;
function closeLabelPopovers() {{
  document.querySelectorAll(".lab-pop").forEach(function(p) {{ p.classList.add("hidden"); }});
}}
function toggleLabelPopover(btn) {{
  var pop = btn.parentNode.querySelector(".lab-pop");
  var wasOpen = !pop.classList.contains("hidden");
  closeLabelPopovers();
  if (wasOpen) {{ return; }}
  pop.classList.remove("hidden");
  var known = pop.querySelector(".known");
  if (known && KNOWN_CHIPS_HTML !== null) {{ known.innerHTML = KNOWN_CHIPS_HTML; }}
  var attached = Array.prototype.map.call(pop.querySelectorAll(".attached .lab-chip"), function(c) {{ return c.dataset.label; }});
  pop.querySelectorAll(".known-chip").forEach(function(k) {{
    k.classList.toggle("used", attached.indexOf(k.dataset.label) !== -1);
  }});
}}
document.addEventListener("click", function(e) {{
  if (!e.target.closest(".lab-pop-wrap")) {{ closeLabelPopovers(); }}
}});
function escapeLabelHtml(s) {{
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}}
function applyLabelState(btn, labelsList) {{
  var article = btn.closest(".article");
  var pop = btn.parentNode.querySelector(".lab-pop");
  var chipsHtml = labelsList.length
    ? labelsList.map(function(n) {{
        return '<span class="lab-chip" data-label="' + escapeLabelHtml(n) + '">' + escapeLabelHtml(n) +
          ' <span class="x" onclick="event.stopPropagation(); event.preventDefault(); detachLabelChip(this);">×</span></span>';
      }}).join("")
    : '<span class="none">아직 없음</span>';
  pop.querySelector(".attached").innerHTML = chipsHtml;
  var row = article.querySelector(".lab-row");
  if (labelsList.length) {{
    var rowHtml = '<span class="tagmark">' + LAB_TAG_ICON_SVG + "</span>" + chipsHtml;
    if (row) {{ row.innerHTML = rowHtml; }}
    else {{
      row = document.createElement("div");
      row.className = "lab-row";
      row.innerHTML = rowHtml;
      btn.closest("summary").appendChild(row);
    }}
  }} else if (row) {{ row.remove(); }}
  btn.classList.toggle("on", labelsList.length > 0);
  var countEl = btn.querySelector(".lab-count");
  if (labelsList.length) {{
    if (!countEl) {{ countEl = document.createElement("span"); countEl.className = "lab-count"; btn.appendChild(countEl); }}
    countEl.textContent = labelsList.length;
  }} else if (countEl) {{ countEl.remove(); }}
  pop.querySelectorAll(".known-chip").forEach(function(k) {{
    k.classList.toggle("used", labelsList.indexOf(k.dataset.label) !== -1);
  }});
}}
function attachLabel(btn, label) {{
  label = (label || "").trim();
  if (!label) {{ return; }}
  fetch("http://{settings_host}:{settings_port}/add-label", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{
      url: btn.dataset.url, outlet: btn.dataset.outlet, title: btn.dataset.title,
      pubDate: btn.dataset.pubDate, group: btn.dataset.group,
      scrapDate: btn.dataset.scrapDate, scrapEnd: btn.dataset.scrapEnd, label: label
    }})
  }}).then(function(res) {{
    if (!res.ok) {{ throw new Error(); }}
    return res.json();
  }}).then(function(data) {{
    if (typeof data.known === "string") {{ KNOWN_CHIPS_HTML = data.known; }}
    applyLabelState(btn, data.labels);
  }})
  .catch(function() {{ alert("라벨을 붙이지 못했습니다 — 앱이 실행 중인지 확인해주세요."); }});
}}
function detachLabel(btn, label) {{
  fetch("http://{settings_host}:{settings_port}/remove-label", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: btn.dataset.url, label: label}})
  }}).then(function(res) {{
    if (!res.ok) {{ throw new Error(); }}
    return res.json();
  }}).then(function(data) {{
    if (typeof data.known === "string") {{ KNOWN_CHIPS_HTML = data.known; }}
    applyLabelState(btn, data.labels);
  }})
  .catch(function() {{ alert("라벨을 떼지 못했습니다 — 앱이 실행 중인지 확인해주세요."); }});
}}
function detachLabelChip(el) {{
  var chip = el.closest(".lab-chip");
  var btn = el.closest(".article").querySelector(".lab-btn");
  detachLabel(btn, chip.dataset.label);
}}
function knownChipClick(el) {{
  var btn = el.closest(".article").querySelector(".lab-btn");
  if (el.classList.contains("used")) {{ detachLabel(btn, el.dataset.label); }}
  else {{ attachLabel(btn, el.dataset.label); }}
}}
function addLabelFromInput(input) {{
  var btn = input.closest(".article").querySelector(".lab-btn");
  attachLabel(btn, input.value);
  input.value = "";
  var warn = input.closest(".lab-pop").querySelector(".sim-warn");
  warn.classList.add("hidden");
}}
// [추가: 2026-08-18] 새 라벨 입력 시 비슷한 이름 경고(목업 ③-3) — 병합보다 값싼
// 예방책이라는 판단(2026-08-18 대화). 편집거리 1 이하 또는 접두 일치(세제개편/세제
// 같은 경우)를 "비슷함"으로 본다. 막지는 않는다 — 진짜 다른 라벨일 수 있어서다.
function labelEditDistance1(a, b) {{
  if (a === b) {{ return true; }}
  var la = a.length, lb = b.length;
  if (Math.abs(la - lb) > 1) {{ return false; }}
  var i = 0, j = 0, edits = 0;
  while (i < la && j < lb) {{
    if (a[i] === b[j]) {{ i++; j++; continue; }}
    edits++;
    if (edits > 1) {{ return false; }}
    if (la === lb) {{ i++; j++; }}
    else if (la > lb) {{ i++; }}
    else {{ j++; }}
  }}
  return true;
}}
function checkSimilarLabel(input) {{
  var pop = input.closest(".lab-pop");
  var warn = pop.querySelector(".sim-warn");
  var typed = input.value.trim();
  if (!typed) {{ warn.classList.add("hidden"); return; }}
  var known = Array.prototype.map.call(pop.querySelectorAll(".known-chip"), function(k) {{ return k.dataset.label; }});
  var match = known.find(function(name) {{
    if (name === typed) {{ return false; }}
    return labelEditDistance1(name, typed) || name.indexOf(typed) === 0 || typed.indexOf(name) === 0;
  }});
  if (!match) {{ warn.classList.add("hidden"); return; }}
  warn.classList.remove("hidden");
  warn.innerHTML = "비슷한 라벨 <b>\\"" + escapeLabelHtml(match) + "\\"</b>가 있어요."
    + '<button type="button" class="use" onclick="event.stopPropagation(); event.preventDefault(); useSimilarLabel(this);" data-label="' + escapeLabelHtml(match) + '">' + escapeLabelHtml(match) + '로 붙이기</button>'
    + '<button type="button" class="keep" onclick="event.stopPropagation(); event.preventDefault(); this.closest(\\'.sim-warn\\').classList.add(\\'hidden\\');">그대로 쓰기</button>';
}}
function useSimilarLabel(btn) {{
  var input = btn.closest(".lab-pop").querySelector(".lab-input");
  input.value = btn.dataset.label;
  addLabelFromInput(input);
}}
"""


def label_popover_style() -> str:
    """🏷 라벨 팝오버 CSS — 템플릿에 {label_popover_style}로 끼워 넣는다(값이 채워진 문자열)."""
    return LABEL_POPOVER_STYLE.format(**PALETTE)


def label_popover_script(host: str, port: int) -> str:
    """🏷 라벨 팝오버 JS — 붙이기/떼기는 설정 서버로 fetch하므로 주소를 받아 채운다."""
    return LABEL_POPOVER_SCRIPT.format(settings_host=host, settings_port=port)


# [추가: 2026-09-15] 「AI 기사 나누기」 — 확정본·초안 하단바가 같이 쓴다(휴지통과 같은 이유 —
# 두 화면이 같은 자리에 같은 것을 보여줘야 하는데 코드가 갈리면 한쪽만 고쳐져 어긋난다).
# 세로선 왼쪽은 담당자가 직접 하는 일(옮기기·↑↓·🗑), 오른쪽은 AI에게 맡기는 일. 색은 「AI 기사
# 배정」·「AI 모든 기사 재분류」와 같은 채운 연보라(AI가 하는 일 셋이 한 계열로 읽히게). 잠길
# 때는 숨기지 않고 흐리게 두고 누르면 이유를 알린다(aria-disabled — disabled면 클릭이 안 와서
# 이유를 말할 기회가 없다). 📂 미분류처럼 진짜 소제목이 아닌 칸의 기사가 섞이면 아예 안 그린다.
SPLIT_BUTTON_STYLE = """
  .bulk-move-bar .bulk-split-divider {{ width: 1px; height: 24px; background: {border}; margin: 0 2px; }}
  .bulk-move-bar .split-btn {{
    display: inline-flex; align-items: center; gap: 5px; font-weight: 600; border-radius: var(--r-md);
    background: {ai_bg}; color: {ai_text}; border: 1px solid {ai_border};
  }}
  .bulk-move-bar .split-btn:hover {{ background: {ai_bg_hover}; border-color: {ai_border_hover}; }}
  .bulk-move-bar .split-btn[aria-disabled="true"] {{ opacity: 0.45; cursor: not-allowed; }}
  .bulk-move-bar .split-btn[aria-disabled="true"]:hover {{ background: {ai_bg}; border-color: {ai_border}; }}
  .bulk-move-bar .split-btn:disabled {{ opacity: 0.7; cursor: progress; }}
  .bulk-move-bar .split-btn[hidden], .bulk-move-bar .bulk-split-divider[hidden] {{ display: none; }}
  /* 이 드롭다운은 가장 긴 소제목 이름만큼 넓어져서(실측 199px) 나누기 버튼이 붙자 800px 바가
     두 줄로 넘어갔다. 닫혀 있을 땐 늘 「이동할 소제목」만 보이고, 펼친 목록은 이 폭과 무관하게
     이름 전체를 보여준다. */
  #bulk-move-select {{ max-width: 150px; }}
"""


def screen_tag_style() -> str:
    """제목 맨 앞의 화면 이름 칩 — 초안 (초안) 회색 · 확정본 (확정본) 파랑. 두 화면이 같은
    머리줄 「언론 모니터링 N시 기준」을 쓰고 어느 화면인지는 이 칩이 말한다(NAME_CHIP_MOCKUP.html).
    화면 전용이라 복사·txt·발송 텍스트엔 없다. 수시(app/adhoc/renderer.py)도 같은 모양·자리다."""
    return f"""
  .screen-tag {{ display: inline-flex; align-items: center; font-size: var(--fs-sm); font-weight: 700;
    padding: 3px 11px; border-radius: var(--r-pill); border: 1px solid; margin-right: 10px;
    vertical-align: 0.18em; line-height: 1.3; }}
  .screen-tag.work {{ background: {COLOR_SCREEN_TAG_WORK_BG}; border-color: {COLOR_SCREEN_TAG_WORK_BORDER};
    color: {COLOR_SCREEN_TAG_WORK_TEXT}; }}
  .screen-tag.final {{ background: {COLOR_SCREEN_TAG_REG_BG}; border-color: {COLOR_SCREEN_TAG_REG_BORDER};
    color: {COLOR_SCREEN_TAG_REG_TEXT}; }}
  .head-sub {{ font-size: 0.72em; font-weight: 500; color: {COLOR_SCREEN_TAG_WORK_TEXT}; margin-left: 6px; }}
"""


def split_button_style() -> str:
    """나누기 버튼 CSS — 템플릿 안에 {{split_button_style}}로 끼워 넣는다(값이 채워진 문자열)."""
    return SPLIT_BUTTON_STYLE.format(**PALETTE)


def split_button_html() -> str:
    """하단바의 세로선 + 나누기 버튼 — 🗑(일괄 숨기기)와 「선택 해제」 사이에 둔다."""
    return (
        '<span class="bulk-split-divider" id="bulk-split-divider"></span>'
        '<button type="button" class="split-btn" id="bulk-split-btn" onclick="splitSelected(this)">'
        f'{icon("split")} AI 기사 나누기</button>'
    )


def split_button_script(settings_host: str, settings_port: int, endpoint: str) -> str:
    """나누기 버튼 JS — 두 화면의 updateBulkMoveBar 끝에서 updateSplitButton을 부른다.

    endpoint: 초안은 /split-group, 확정본은 /split-group-final — 서버가 "지금 화면이 보는 소제목"을
    계산하는 방법만 다르고(초안은 분류 결과, 확정본은 회차 파일의 저장된 배정) 규칙은 같다.
    잠금 기준은 서버(app.settings_server._split_group)와 같은 상수를 싣는다.
    """
    icon_html = icon("split")
    return f"""
// [추가: 2026-09-15] 「AI 기사 나누기」 버튼 상태 — 잠글 때는 흐리게 두고 이유를 적어둔다(누르면
// 그 이유를 알린다). 진짜 소제목이 아닌 칸(📂 미분류·사진 모아 보기·시간순 보기)의 기사가
// 섞이면 아예 안 그린다.
function updateSplitButton(checked, sections, sameSection) {{
  var btn = document.getElementById("bulk-split-btn");
  var divider = document.getElementById("bulk-split-divider");
  if (!btn) {{ return; }}
  var notGroup = sections.some(function(s) {{ return !s || !s.dataset.group || s.classList.contains("subheading-unclassified"); }});
  btn.hidden = notGroup; divider.hidden = notGroup;
  if (notGroup) {{ return; }}
  var subheadingCount = document.querySelectorAll(".subheading[data-group]:not(.subheading-unclassified)").length;
  var reason = "";
  if (!sameSection) {{ reason = "한 소제목 안의 기사만 나눌 수 있어요."; }}
  else if (checked.length < {MIN_SPLIT_ARTICLES}) {{ reason = "{MIN_SPLIT_ARTICLES}건 이상 골라야 나눌 수 있어요."; }}
  else if (subheadingCount >= {MAX_SUBHEADINGS}) {{ reason = "소제목이 {MAX_SUBHEADINGS}개로 꽉 차 있어요. 빈 소제목을 지우거나 합친 뒤 다시 눌러 주세요."; }}
  btn.setAttribute("aria-disabled", reason ? "true" : "false");
  btn.dataset.reason = reason;
  btn.title = reason || ("고른 " + checked.length + "건만 AI가 쟁점별로 새 소제목으로 나눠요. 다른 소제목은 그대로예요.");
}}
var SPLIT_FAILURE_MESSAGES = {{
  mixed: "한 소제목 안의 기사만 나눌 수 있어요.",
  stale: "그 사이 화면이 바뀌었어요. 새로고침한 뒤 다시 골라 주세요.",
  unclassified: "소제목 미분류 칸은 「AI 기사 배정」으로 정리해 주세요.",
  too_few: "{MIN_SPLIT_ARTICLES}건 이상 골라야 나눌 수 있어요.",
  full: "소제목이 {MAX_SUBHEADINGS}개로 꽉 차 있어요. 빈 소제목을 지우거나 합친 뒤 다시 눌러 주세요.",
  not_configured: "AI 연동이 설정돼 있지 않아요."
}};
function splitSelected(btn) {{
  if (btn.getAttribute("aria-disabled") === "true") {{ alert(btn.dataset.reason); return; }}
  var checked = document.querySelectorAll(".article-select:checked");
  var section = checked.length ? checked[0].closest(".subheading") : null;
  if (!section) {{ return; }}
  var renameBtn = section.querySelector("h2 .rename-btn[data-current]");
  var name = renameBtn ? renameBtn.dataset.current : section.dataset.group;
  var whole = checked.length === section.querySelectorAll(".article-select").length;
  // 확인창은 담당자가 직접 해 둔 일이 사라질 때만 — 통째로 나누면 직접 고친 이름이 없어진다.
  // 그 밖엔 이 칸 안에서 잃을 게 없고(AI가 지은 이름·순서뿐) ↩ 한 번이면 돌아온다.
  // 이름 바로 뒤에 조사를 붙이지 않는다 — 받침 유무에 따라 은/는이 갈려서 이름을 모르는 채로는 틀린다.
  if (whole && section.dataset.renamed === "1") {{
    if (!confirm("소제목 이름 「" + name + "」 — 직접 고친 이름이에요. 나누면 이 이름은 없어지고 AI가 새 이름을 붙여요.\\n"
        + "(다른 소제목과 숨긴 기사, 담아둔 기사는 영향을 받지 않습니다.)\\n\\n"
        + "고른 " + checked.length + "건을 AI로 나눌까요?")) {{
      return;
    }}
  }}
  var body = new URLSearchParams();
  Array.prototype.forEach.call(checked, function(cb) {{ body.append("urls", cb.dataset.url); }});
  body.append("group", section.dataset.group);
  btn.disabled = true;
  var label = btn.innerHTML;
  btn.innerHTML = '{icon_html} 나누는 중…';
  var keep = "소제목은 「" + name + "」 그대로 뒀어요.";
  function fail(msg) {{ btn.disabled = false; btn.innerHTML = label; alert(msg); }}
  fetch("http://{settings_host}:{settings_port}{endpoint}", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (!res.ok) {{ fail("AI로 나누지 못했어요.\\n" + keep + "\\n잠시 후 다시 시도해주세요."); return; }}
    res.json().then(function(data) {{
      var moved = (data && data.moved) || [];
      if (moved.length) {{
        // 「AI 기사 배정」 뒤와 같은 표시 — AI가 방금 옮긴 기사만 연보라로 한 번 칠한다.
        sessionStorage.setItem("justClassifiedUrls", JSON.stringify(moved));
        location.reload();
        return;
      }}
      var reason = (data && data.reason) || "";
      if (reason === "no_split") {{ fail("더 나눌 쟁점을 찾지 못했어요.\\n" + keep); }}
      else if (SPLIT_FAILURE_MESSAGES[reason]) {{ fail(SPLIT_FAILURE_MESSAGES[reason]); }}
      else {{ fail("AI 호출이 실패했어요.\\n" + keep + "\\n잠시 후 다시 시도해주세요."); }}
    }}).catch(function() {{ location.reload(); }});
  }}).catch(function() {{
    fail("AI로 나누지 못했어요 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
"""


def _trash_batch_row_html(batch: dict) -> str:
    """팝오버의 한 줄 — 묶음이면 요약 한 줄, 1건이면 그 기사 제목 한 줄."""
    records = batch["records"]
    urls_attr = html.escape(json.dumps([r["url"] for r in records], ensure_ascii=False), quote=True)
    when = _hhmm_of(batch.get("hidden_at"))
    when_html = f'<span class="tm">{when}</span>' if when else ""
    if len(records) == 1:
        record = records[0]
        outlet = record.get("outlet") or ""
        title = record.get("title") or record["url"]
        text = f"({html.escape(outlet)}) {html.escape(title)}" if outlet else html.escape(title)
        count_html = ""
        # [수정: 2026-09-16, 2차] 낱개 줄은 ↩ 아이콘만(= /hidden의 행과 같은 모양).
        label = ""
    else:
        # [수정: 2026-09-16] 소제목 이름만 — /hidden의 묶음 헤더와 같은 규칙(사용자 결정).
        text = (
            f'<b>{html.escape(batch["group"])}</b>'
            if batch.get("group")
            else "골라서 숨김"
        )
        count_html = f'<span class="cnt">{len(records)}건</span>'
        label = "모두 복구"  # 아이콘과의 간격은 .undo-mini의 gap이 준다
    tip = "이 기사 되돌리기" if len(records) == 1 else "이 묶음을 통째로 되돌리기"
    return (
        '<div class="trow">'
        f'<span class="tx">{text}{when_html}</span>'
        f"{count_html}"
        # [수정: 2026-09-16, 2차] 복구는 ↩ 아이콘으로 통일한다(사용자 결정) — 낱개는
        # 아이콘만, 묶음은 "모두 복구"를 붙인다(한 번에 여러 건이라는 게 정보라서).
        # 좌하단 ↩ 되돌리기 FAB과 같은 그림이지만, 이 팝오버 안에서는 ↩가 언제나
        # "되살리기"라 한 화면 안의 뜻은 갈리지 않는다.
        f'<button type="button" class="undo-mini" data-urls="{urls_attr}" '
        f'title="{tip}" aria-label="{tip}" onclick="unhideFromTrash(this)">'
        f'{icon("undo")}{label}</button>'
        "</div>"
    )


def _hhmm_of(value) -> str:
    """ISO 시각에서 HH:MM만. 못 읽으면 빈 문자열(없는 값을 지어내지 않는다)."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def hidden_trash_html(hidden_href: str) -> str:
    """좌하단 휴지통 버튼 + 팝오버. 숨긴 기사가 하나도 없으면 빈 문자열(아무것도 안 그린다).

    [수정: 2026-09-04] **오늘치만 본다(days=1).** /hidden 화면은 7일치를 열람용으로
    보여주지만(app.config.HIDDEN_VIEW_DAYS), 이 배지 숫자가 뜻하는 건 "지금 이 화면에서
    빠져 있는 기사가 몇 건인가"라 지난 날짜가 섞이면 화면이 거짓말을 한다 — 어제 기록은
    이미 숨김이 풀려 이 화면 어디에도 영향을 안 준다.
    """
    batches = load_hidden_batches(days=1)
    total = sum(len(batch["records"]) for batch in batches)
    if not total:
        return ""
    rows = "".join(_trash_batch_row_html(batch) for batch in batches[:_TRASH_POPOVER_MAX_ROWS])
    # 더보기는 **항상** 그린다 — 하단바의 🗑️ 링크를 뺐으므로(같은 목적지가 둘이면 옆문이
    # 된다) 이 링크가 휴지통 화면으로 가는 유일한 길이다. 다만 문구는 무엇이 기다리는지에
    # 따라 갈린다: 묶음이 있으면 "펼쳐서 고를 수 있다"가 그 화면의 값어치라 그렇게 적는다.
    has_batch = any(len(batch["records"]) > 1 for batch in batches)
    more_text = "휴지통 열어서 하나씩 고르기 →" if has_batch else "휴지통 열기 →"
    return (
        f'<button type="button" class="trash-fab" onclick="toggleTrashPopover()" '
        f'title="숨긴 기사 {total}건">{icon("trash")}<span class="badge">{total}</span></button>'
        '<div class="trash-pop" id="trash-pop">'
        f'<h5>{icon("trash")} 숨긴 기사 {total}건</h5>'
        # [수정: 2026-09-16] 자정 해제가 없어졌다 — 숨김은 HIDDEN_VIEW_DAYS(7일)까지
        # 유지되고, 그동안은 여기서도 휴지통에서도 되살릴 수 있다.
        f'<p class="tnote">오늘 숨긴 기사예요. 숨김은 {HIDDEN_VIEW_DAYS}일 동안 유지되고, '
        f'그동안은 언제든 되살릴 수 있어요.</p>'
        f"{rows}"
        f'<a class="more" href="{html.escape(hidden_href, quote=True)}">{more_text}</a>'
        "</div>"
    )


def hidden_trash_script(settings_host: str, settings_port: int) -> str:
    """휴지통 팝오버 JS — 템플릿 안에 {{hidden_trash_script}}로 끼워 넣는다.

    복구하면 팝오버만 고쳐 그리지 않고 화면 전체를 다시 불러온다 — 숨김 해제는 기사 목록과
    소제목 구성이 같이 바뀌는 동작이라, 목록을 그대로 둔 채 배지만 줄이면 화면이 거짓말을
    한다. 되돌리기는 건수와 무관하게 한 걸음이라(app.settings_server._handle_unhide_article)
    12건짜리 묶음을 되살린 뒤 ↩ 한 번이면 통째로 다시 숨겨진다.
    """
    return f"""
function toggleTrashPopover() {{
  var pop = document.getElementById("trash-pop");
  if (pop) {{ pop.classList.toggle("is-open"); }}
}}
document.addEventListener("click", function (e) {{
  var pop = document.getElementById("trash-pop");
  if (!pop || !pop.classList.contains("is-open")) {{ return; }}
  if (e.target.closest(".trash-pop") || e.target.closest(".trash-fab")) {{ return; }}
  pop.classList.remove("is-open");
}});
function unhideFromTrash(btn) {{
  var urls = JSON.parse(btn.dataset.urls || "[]");
  if (!urls.length) {{ return; }}
  if (urls.length > 1 && !confirm(urls.length + "건을 한꺼번에 되살릴까요?")) {{ return; }}
  var body = new URLSearchParams();
  urls.forEach(function (url) {{ body.append("url", url); }});
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/unhide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function (res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ btn.disabled = false; alert("되돌리기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function () {{
    btn.disabled = false;
    alert("되돌리기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
"""


def pinned_jump_badge_html(count: int) -> str:
    """제목 옆 「📌 N」 — 📌 칸은 늘 맨 아래라, 미분류 뱃지처럼 눌러서 그 칸으로 내려간다.
    0건이면 안 그린다(늘 떠 있으면 안 읽게 된다). 글자 대신 칸 제목과 같은 핀 아이콘 + 숫자,
    뜻은 툴팁. 칸 자체는 0건이어도 그려서 「+ 수기로 기사 추가」 입구는 남는다."""
    if not count:
        return ""
    return (
        '<button type="button" class="pinned-jump-badge" onclick="scrollToPinned()" '
        f'title="담아둔 기사 {count}건 — 눌러서 그 칸으로 이동합니다" '
        f'aria-label="담아둔 기사 {count}건">{icon("pin")}{count}</button>'
    )


# 「+ 수기로 기사 추가」 — 네이버에서 직접 찾은 기사를 주소만으로 📌 담아둔 기사에 올린다.
# 입구가 툴바가 아니라 이 칸 머리에 있는 이유: 기사가 떨어지는 곳이 여기라서(툴바에 두면
# 입력은 맨 위, 결과는 맨 아래였다), 그리고 툴바 왼쪽은 「초안을 바꾸는 버튼」 자리인데
# 담아둔 기사는 보고서 밖이다. 확인 단계 없이 곧바로 담긴다(submitUrlAdd). 확정본·초안 공용 —
# CSS·JS도 url_add_style()/url_add_script() 한 곳이라, 동작을 바꾸면 두 화면이 같이 바뀐다.
URL_ADD_BUTTON_HTML = (
    '<button class="create-group-btn url-add-open-btn" type="button" id="url-add-open-btn" '
    'onclick="openUrlAdd()" title="네이버에서 직접 찾은 기사를 주소만으로 담아둡니다">'
    "+ 수기로 기사 추가</button>"
)
URL_ADD_PANEL_HTML = (
    '<div class="url-add-zone" id="url-add-zone">'
    '<div class="url-add-row">'
    '<input type="text" id="url-add-input" placeholder="네이버 기사 주소를 붙여넣으세요" '
    'onkeydown="urlAddKey(event)" oninput="clearUrlAddWarn()">'
    '<button type="button" id="url-add-btn" onclick="submitUrlAdd()">추가</button>'
    '<button class="url-add-close" type="button" onclick="closeUrlAdd()" title="닫기" '
    f'aria-label="닫기">{icon("x")}</button>'
    "</div>"
    '<div class="url-add-hint">붙여넣고 추가(또는 Enter)하면 바로 담아둔 기사에 들어가요.</div>'
    '<div class="url-add-warn" id="url-add-warn" role="alert"></div>'
    "</div>"
)


URL_ADD_STYLE = """
  /* 「+ 수기로 기사 추가」 — 입구와 입력칸이 📌 담아둔 기사 칸 안에 있다(넣는 곳 = 보이는
     곳). 칸이 이미 색(띠 + 옅은 바탕)을 가져서 입력칸까지 같은 계열이면 한 덩어리로 묻힌다 —
     흰 바탕 + 회색 테두리 + 옅은 그림자로 "칸 위에 얹힌 입력칸"으로 가른다(새 색 없음).
     시안 mockups/MANUAL_ADD_PLACEMENT_MOCKUP.html 「색 비교」 가안. */
  .manual-zone-empty {{ font-size: var(--fs-sm); color: {muted}; }}
  /* 툴바의 「+ 새 소제목」과 같은 테두리 버튼 — 전역 button 규칙(채운 파랑)을 덮는다.
     칸 바탕이 옅은 캐러멜이라 바탕만 흰색, 글자·테두리는 칸 색. */
  .manual-zone .url-add-open-btn {{
    font-size: var(--fs-sm); padding: 3px 9px; background: {card}; color: {pinned_text};
    border: 1px solid {pinned_border}; border-radius: var(--r-md); font-weight: 400; cursor: pointer;
  }}
  .manual-zone .url-add-open-btn:hover {{ background: {pinned_chip_bg}; }}
  .url-add-zone {{
    display: none; border: 1px solid {float_input_border}; border-radius: var(--r-lg);
    padding: 10px 14px; margin: 10px 0 2px; background: {card};
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.10);
  }}
  .url-add-zone.is-open {{ display: block; }}
  /* 한 번에 한 건만 받는다 — 여러 줄을 받으면 결과가 목록이 되고, 어느 줄이 왜
     실패했는지 대조해야 한다. 순서 [주소칸][추가][×] — 누를 것이 입력 바로 옆, 닫기는 끝. */
  .url-add-row {{ display: flex; align-items: center; gap: 8px; }}
  .url-add-row input {{
    flex: 1; min-width: 0; padding: 6px 10px; border: 1px solid {border}; border-radius: var(--r-md);
    font-size: var(--fs-md); color: {text}; background: {card};
  }}
  .url-add-row input:disabled {{ background: {bg}; color: {muted}; }}
  .url-add-hint {{ color: {muted}; font-size: var(--fs-sm); margin-top: 7px; }}
  .url-add-zone button {{ font-size: var(--fs-sm); padding: 5px 10px; white-space: nowrap; }}
  .url-add-zone .url-add-close {{
    background: none; border: none; color: {muted}; padding: 4px 6px; font-size: 1rem;
    line-height: 1; cursor: pointer;
  }}
  .url-add-zone .url-add-close:hover {{ color: {text}; }}
  /* 거부 사유는 지우지 않고 남긴다 — 실패가 조용히 사라지면 안 된다(CODING_CONVENTIONS §3).
     주의 세트(앰버) 재사용. 입력한 주소도 남겨 어디가 틀렸는지 보고 고치게 한다. */
  .url-add-warn {{
    display: none; margin-top: 8px; padding: 8px 11px; border-radius: var(--r-md); font-size: var(--fs-sm);
    line-height: 1.55; background: {warn_bg}; border: 1px solid {warn_border}; color: {warn_text};
  }}
  .url-add-warn.is-on {{ display: block; }}
  .url-add-warn b {{ display: block; }}
  .url-add-warn span {{ color: {warn_sub}; }}
  /* 제목 옆 「📌 N」 — 칸이 늘 맨 아래라 미분류 뱃지처럼 눌러서 내려간다. 칸과 같은 캐러멜.
     모양(아이콘 + 숫자, 화살표 없음)은 초안의 미분류 뱃지(.pending-count-badge)와 같다. */
  .pinned-jump-badge {{
    margin-left: 6px; font-size: var(--fs-sm); font-weight: 700; vertical-align: middle;
    display: inline-flex; align-items: center; gap: 4px;
    color: {pinned_text}; background: {pinned_chip_bg}; border: 1px solid {pinned_border};
    border-radius: var(--r-pill); padding: 2px 9px; cursor: pointer; font-family: inherit;
  }}
  .pinned-jump-badge:hover {{ background: {card}; }}
  .pinned-jump-badge .ic {{ width: 1.05em; height: 1.05em; }}
"""

URL_ADD_SCRIPT = """
// [추가: 2026-09-21] 「+ 수기로 기사 추가」 — 담당자가 네이버에서 직접 찾아온 기사를 URL만으로
// 📌 담아둔 기사 칸에 올린다. 서버가 원문 페이지에서 언론사·제목·요약·발행시각을 읽는다
// (app.naver_api.fetch_article_by_url). 검색 API는 안 부른다. 입구·입력칸 모두 📌 칸 안.
// 확인 단계가 없다 — 성공하면 곧바로 새로고침해 담긴 기사 행 자체를 결과로 보여주고(세이지),
// 입력칸은 비운 채 열어 둬 이어 붙일 수 있게 한다. 거부는 새로고침 없이 경고로 남긴다.
var URL_ADD_JUST_ADDED_KEY = "urlAddJustAdded";
function openUrlAdd() {{
  document.getElementById("url-add-zone").classList.add("is-open");
  var btn = document.getElementById("url-add-open-btn");
  if (btn) btn.style.display = "none";
  document.getElementById("url-add-input").focus();
}}
function closeUrlAdd() {{
  document.getElementById("url-add-zone").classList.remove("is-open");
  document.getElementById("url-add-input").value = "";
  clearUrlAddWarn();
  var btn = document.getElementById("url-add-open-btn");
  if (btn) btn.style.display = "";
}}
function clearUrlAddWarn() {{
  var warn = document.getElementById("url-add-warn");
  if (warn) {{ warn.classList.remove("is-on"); warn.innerHTML = ""; }}
}}
function showUrlAddWarn(reason, detail) {{
  var warn = document.getElementById("url-add-warn");
  warn.innerHTML = "<b>" + escapeUrlAddHtml(reason) + "</b>"
    + (detail ? "<span>" + escapeUrlAddHtml(detail) + "</span>" : "");
  warn.classList.add("is-on");
}}
function urlAddKey(event) {{
  if (event.key === "Enter") {{ event.preventDefault(); submitUrlAdd(); }}
}}
function submitUrlAdd() {{
  var input = document.getElementById("url-add-input");
  var btn = document.getElementById("url-add-btn");
  var url = input.value.trim();
  if (!url) {{ input.focus(); return; }}
  // 응답까지 몇 초 걸린다(원문 페이지를 받아온다) — 두 번 눌러 중복으로 담기지 않도록
  // 잠그고, 실패하면 반드시 푼다(성공하면 새로고침된다).
  btn.disabled = true;
  input.disabled = true;
  btn.textContent = "불러오는 중…";
  clearUrlAddWarn();
  var unlock = function() {{
    btn.disabled = false;
    input.disabled = false;
    btn.textContent = "추가";
    input.focus();
  }};
  fetch("http://{settings_host}:{settings_port}/add-article-by-url", {{
    method: "POST",
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, screen: "{screen}"}})
  }}).then(function(res) {{
    // 거부도 400이 아니라 본문의 reason으로 알린다 — 사유를 그대로 보여주기 위해서다.
    return res.json();
  }}).then(function(data) {{
    data = data || {{}};
    if (!data.ok) {{
      showUrlAddWarn(data.reason || "해당 URL로 기사를 불러올 수 없습니다.", data.detail || "");
      unlock();
      return;
    }}
    try {{
      sessionStorage.setItem(URL_ADD_JUST_ADDED_KEY, JSON.stringify({{url: data.url || url, at: Date.now()}}));
    }} catch (e) {{}}
    location.reload();
  }}).catch(function() {{
    showUrlAddWarn("추가하지 못했습니다.", "앱이 실행 중인지 확인해 주세요.");
    unlock();
  }});
}}
// 새로고침 뒤: 입력칸을 다시 열고, 방금 담긴 행을 세이지(담당자가 방금 손댄 것)로 칠해
// 보이게 한다. 10초 지난 기록은 버린다(다른 동작의 새로고침에 따라붙지 않게).
// load에서 도는 이유: 아래 "새로 들어온 기사" 기준선 코드가 이 행에 노랑을 먼저 붙이므로
// 그 뒤에 떼야 하고, 브라우저의 스크롤 복원보다도 뒤여야 scrollIntoView가 남는다.
window.addEventListener("load", function restoreUrlAdd() {{
  var raw = null;
  try {{
    raw = sessionStorage.getItem(URL_ADD_JUST_ADDED_KEY);
    sessionStorage.removeItem(URL_ADD_JUST_ADDED_KEY);
  }} catch (e) {{ return; }}
  if (!raw) return;
  var rec;
  try {{ rec = JSON.parse(raw); }} catch (e) {{ return; }}
  if (!rec || !rec.url || Date.now() - (rec.at || 0) > 10000) return;
  if (!document.getElementById("url-add-zone")) return;
  openUrlAdd();
  var rows = document.querySelectorAll(".manual-zone .article");
  for (var i = 0; i < rows.length; i++) {{
    if (rows[i].getAttribute("data-url") === rec.url) {{
      rows[i].classList.remove("is-new-arrival");
      rows[i].classList.add("just-promoted");
      rows[i].scrollIntoView({{block: "nearest"}});
      break;
    }}
  }}
}});
function scrollToPinned() {{
  var el = document.querySelector(".manual-zone");
  if (el) {{ el.scrollIntoView({{behavior: "smooth", block: "start"}}); }}
}}
function escapeUrlAddHtml(s) {{
  return String(s == null ? "" : s).replace(/[&<>"]/g, function(c) {{
    return {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}}[c];
  }});
}}
"""


def url_add_style() -> str:
    """「+ 수기로 기사 추가」 입력칸·제목 옆 「📌 담아둔 기사 N건 ↓」 CSS — 확정본·초안 공용."""
    return URL_ADD_STYLE.format(**PALETTE)


def url_add_script(settings_host: str, settings_port: int, screen: str) -> str:
    """「+ 수기로 기사 추가」 JS — 확정본·초안 공용(중괄호 홑겹 완성 문자열).

    screen("confirmed" | "preview")을 서버에 같이 보낸다 — "이미 이 회차에 있다"를 무엇과
    대조할지가 화면마다 다르다(확정본은 저장된 회차, 초안은 아직 저장 전인 지금 초안)."""
    return URL_ADD_SCRIPT.format(settings_host=settings_host, settings_port=settings_port, screen=screen)


# Shift+클릭 범위 선택 — 확정본·초안·수시 원본/확정본·휴지통의 기사 체크박스 공용.
# 기사 하나를 체크하고 Shift를 누른 채 다른 기사를 누르면 그 사이가 같은 상태(체크/해제)가
# 된다. 안 보이는 행(사진 모아 보기·필터·접힌 묶음)은 건너뛴다 — 안 보이는 기사가 선택에
# 섞이면 확인창 없는 🗑 일괄 숨김에 딸려 간다. skip_row에 걸리는 행도 건너뛴다(수시 원본의
# 숨김·보냄 행 — 체크해서 보내면 숨김 표시가 풀리므로 범위로 쓸려 들어가면 안 된다).
# 양 끝(직접 누른 기사)은 건너뛰지 않고 누른 쪽 상태를 따른다. 안내는 체크박스 툴팁뿐이다(선택 바엔 안 적는다). 시안 mockups/SHIFT_RANGE_SELECT_MOCKUP.html A안.
RANGE_SELECT_SCRIPT = """
(function () {
  var BOX = %(box)s, SKIP = %(skip)s, ROW = %(row)s;
  var anchor = null;
  function visible(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
  function skipped(cb) { if (!SKIP) { return false; } var row = cb.closest(ROW); return !!(row && row.matches(SKIP)); }
  // capture 단계 — 체크박스 자신의 onclick(stopPropagation)보다 먼저 돈다. 이 시점엔
  // 누른 체크박스의 checked가 이미 바뀌어 있다.
  document.addEventListener("click", function (e) {
    var cb = e.target;
    if (!cb.matches || !cb.matches(BOX)) { return; }
    if (e.shiftKey && anchor && anchor !== cb && document.contains(anchor) && visible(anchor)) {
      var all = Array.prototype.filter.call(document.querySelectorAll(BOX), visible);
      var i = all.indexOf(anchor), j = all.indexOf(cb);
      if (i >= 0 && j >= 0) {
        var lo = Math.min(i, j), hi = Math.max(i, j);
        for (var k = lo; k <= hi; k++) {
          var box = all[k];
          if (box === cb || (box !== anchor && skipped(box)) || box.checked === cb.checked) { continue; }
          box.checked = cb.checked;
          %(each)s
        }
        // Shift+클릭이 사이 글자를 파랗게 선택해 버리는 것을 지운다.
        try { window.getSelection().removeAllRanges(); } catch (err) {}
      }
    }
    anchor = cb;
  }, true);
  function addTips() {
    document.querySelectorAll(BOX).forEach(function (cb) {
      if (!cb.title) { cb.title = "선택 · Shift+클릭하면 사이 기사까지 한꺼번에"; }
    });
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", addTips); }
  else { addTips(); }
})();
"""


def range_select_script(box: str, row: str, skip_row: str = "", each_js: str = "") -> str:
    """Shift+클릭 범위 선택 JS — 중괄호 홑겹 완성 문자열.

    box: 체크박스 선택자, row: 그 체크박스의 행 선택자, skip_row: 범위에서 건너뛸 행 선택자,
    each_js: 상태를 바꾼 체크박스(`box`) 하나마다 실행할 JS 문장 — 선택 바 갱신 같은 화면별
    후처리는 끝에 누른 체크박스 자신의 onchange가 한 번 해 준다."""
    return RANGE_SELECT_SCRIPT % {
        "box": json.dumps(box),
        "row": json.dumps(row),
        "skip": json.dumps(skip_row),
        "each": each_js,
    }


# 여러 기사 한 번에 숨기기 — 확정본·초안의 소제목 🗑(hideGroup)와 선택 바 🗑(bulkHideSelected)
# 공용. 기사 수만큼 요청을 나눠 보내지 않고 한 요청에 모아 보낸다(서버 _handle_hide_article이
# 한 번에 받아 되돌리기 기록·화면 재생성을 한 번씩만 한다). 누르는 순간 그 기사들을 흐리게 하고
# 「숨기는 중…」을 띄운다 — 새로고침까지 걸리는 동안 눌렸는지 모르고 한 번 더 누르지 않게.
# keepalive를 안 쓴다: 그 본문 상한(64KB)을 기사 100건이면 넘는다. 결과를 기다려 새로고침하므로 필요도 없다.
HIDE_BATCH_STYLE = """
  .is-hiding {{ opacity: 0.4; pointer-events: none; transition: opacity 0.15s; }}
  .hiding-note {{ font-size: var(--fs-sm); font-weight: 600; color: {accent}; white-space: nowrap; }}
"""

HIDE_BATCH_SCRIPT = """
function postHideBatch(btns, batch) {
  var body = new URLSearchParams();
  btns.forEach(function (btn) {
    var one = hideArticleBody(btn);
    ["url", "outlet", "title", "pubDate", "group"].forEach(function (key) { body.append(key, one.get(key) || ""); });
  });
  if (batch) { body.set("batch", batch); }
  return fetch("http://%(host)s:%(port)s/hide-article", {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: body
  });
}
// 숨기는 동안 표시 — dimEls는 흐리게 할 요소들, noteAfter 바로 앞에 「숨기는 중…」을 끼운다.
// 돌려주는 함수를 부르면 원래대로 되돌린다(실패했을 때).
function showHiding(dimEls, noteAfter, n) {
  dimEls.forEach(function (el) { el.classList.add("is-hiding"); });
  var note = document.createElement("span");
  note.className = "hiding-note";
  note.setAttribute("role", "status");
  note.textContent = n + "건 숨기는 중…";
  if (noteAfter) { noteAfter.disabled = true; noteAfter.insertAdjacentElement("beforebegin", note); }
  return function () {
    dimEls.forEach(function (el) { el.classList.remove("is-hiding"); });
    if (noteAfter) { noteAfter.disabled = false; }
    note.remove();
  };
}
"""


def hide_batch_style() -> str:
    """여러 기사 숨기기 진행 표시 CSS — 값이 채워진 문자열."""
    return HIDE_BATCH_STYLE.format(**PALETTE)


def hide_batch_script() -> str:
    """여러 기사 한 번에 숨기기 JS — 중괄호 홑겹 완성 문자열."""
    return HIDE_BATCH_SCRIPT % {"host": SETTINGS_SERVER_HOST, "port": SETTINGS_SERVER_PORT}


def generate_waiting_page() -> Path:
    """오늘 첫 회차가 아직 끝나지 않았을 때 index.html 대신 보여줄 안내 화면을 만든다."""
    atomic_write_text(OUTPUT_HTML_PATH, _waiting_page_html())
    return OUTPUT_HTML_PATH


def generate_screen(
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
    subheading_format: Optional[str] = None,
) -> Path:
    """저장된 최신 회차를 읽어 index.html로 렌더링한다 (PRD.md 기능2 규칙 6: 최신 회차만 표시).

    keywords/highlight_words/line_template/subheading_format을 생략하면 설정 화면에
    저장된 값을 각각 쓴다(규칙2 검색 키워드, 규칙6 형광펜 단어, 규칙5 출력 줄 형식,
    메일머지 소제목 형식 — 서로 별개 설정).
    """
    # [수정: 2026-08-10] load_latest_run()(가장 최근 "수집된" 회차, 확정 여부 무관) 대신
    # load_latest_confirmed_run()을 쓴다 — 아니면 새 회차가 막 수집돼 확정 대기 상태로
    # 들어오는 순간, 방금 전까지 잘 보이던 이전 확정 회차 대신 "회차 없음" 취급돼 main.py
    # 시작 시(또는 재시작 시) 안내 화면(🐰⏱️)이 기존 확정본을 덮어써버리는 실사고가 있었다.
    # 이제는 최신 확정 회차를 그대로 계속 보여주고, 확정 대기 중인 더 최근 회차는(초안에서
    # 손보는 것과 무관하게) 완성본에 전혀 영향을 주지 않는다.
    run = load_latest_confirmed_run()
    if run is None or not is_today(run):
        # [수정: 2026-07-26] 회차가 아예 없을 때뿐 아니라, 자정이 지나 어제 회차만
        # 남아있을 때도 같은 예외로 취급한다 — 호출하는 쪽(main.py, app.scheduler)이
        # 어제 화면 대신 안내 화면(generate_waiting_page)을 보여주게 하기 위해서다.
        raise RuntimeError("오늘 완료된 회차가 없습니다 — 아직 스크랩 전이거나 자정이 지나 어제 회차만 남아있습니다.")
    if keywords is None or highlight_words is None or line_template is None or subheading_format is None:
        settings = load_settings()
        keywords = keywords if keywords is not None else all_search_keywords(settings)
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
        subheading_format = (
            subheading_format
            if subheading_format is not None
            else settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE)
        )
    articles = apply_summary_overrides(filter_hidden(run["articles"]))
    html_text = render_page(
        run["run_slot"],
        articles,
        keywords,
        highlight_words,
        line_template,
        subheading_format,
        confirmed_at=run.get("confirmed_at"),
        send_count=run.get("send_count", 0),
        run_at=run.get("run_at"),
        sent_by=run.get("sent_by"),
        sent_at=run.get("sent_at"),
        # [수정: 2026-08-27] run.get("classification_degraded")만 보면 2026-08-27 이전에
        # 저장된 회차는 그 필드 자체가 없어(app.scraper.collect_run이 저장 직후 날려먹고
        # 있었다 — HISTORY.md) 폴백 회차인데도 경고 없이 자동발송 카운트다운이 돈다.
        # run_looks_rule_based는 저장된 값이 있으면 그걸 그대로 쓰고, 없을 때만 소제목
        # 이름 모양으로 추정한다(app.classifier).
        classification_degraded=run_looks_rule_based(run),
        finalize_new_subheadings=run.get("finalize_new_subheadings", []),
        send_failure=run.get("send_failure"),
    )
    atomic_write_text(OUTPUT_HTML_PATH, html_text)
    return OUTPUT_HTML_PATH
