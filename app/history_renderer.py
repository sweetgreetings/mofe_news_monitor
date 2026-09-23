# Design Ref: archive/DESIGN.md §1 "지난 기사 더보기" — 보관 중인 회차(RETENTION_DAYS)를 날짜별 -> 시간대별 토글로 조회
import html
import json
import logging
from collections import OrderedDict
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Optional

from app.topnav import regular_nav, topnav_style
from app.config import (
    CUTE_FONT_BASE64,
    CUTE_FONT_NAME,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_HIGHLIGHT_KEYWORDS,
    DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    EAGER_HISTORY_DAYS,
    FONT_STACK,
    HISTORY_HTML_PATH,
    PALETTE,
    RETENTION_DAYS,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.classifier import run_looks_rule_based
from app.curation import display_group_name, filter_hidden, load_group_labels
from app.excel_export import date_range_label, format_pub_datetime
from app.group_order import apply_group_order
from app.labels import labels_for_url
from app.llm_classifier import UNCLASSIFIED_GROUP_NAME, round_id_for_run
from app.manual_keyword_note import load_manual_keyword_note
from app.summary_overrides import apply_summary_overrides
from app.icons import icon
from app.labels import load_labels
from app import send_log
from app.renderer import (
    send_log_href,
    apply_line_template,
    apply_subheading_format,
    format_slot_time_kr,
    known_label_chips_html,
    label_popover_script,
    label_popover_style,
    render_article,
)
from app.today_cuts import cut_origin
from app.settings import active_schedule_times, load_settings
from app.storage import (
    latest_run_key,
    list_deleted_runs,
    list_run_dates,
    list_run_meta,
    list_runs_for_date,
    run_lock_reason,
)

logger = logging.getLogger(__name__)

# [추가: 2026-07-26] "뉴스가 잠잠" 빈 상태 문구용 손글씨체 — app.renderer와 동일 규칙(오프라인
# 대비 base64 내장). 파일별로 독립적인 이 앱의 렌더러 구조를 따라 여기서도 따로 정의한다.
_CUTE_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{CUTE_FONT_NAME}'; "
    f"src: url(data:font/woff2;base64,{CUTE_FONT_BASE64}) format('woff2'); font-display: swap; }}"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>정기 보관함</title>
<style>
  {cute_font_face}
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: var(--r-lg);
  }}
  h1 {{ font-size: var(--fs-xl); color: {header}; margin: 0 0 4px; }}
  .page-sub {{ color: {muted}; font-size: var(--fs-sm); margin: 0 0 14px; }}
  .empty {{ color: {muted}; margin-top: 12px; }}
  .slot-empty {{ text-align: center; color: {muted}; padding: 8px 0; }}
  /* [추가: 2026-09-03] 예정돼 있었는데 저장된 회차가 없는 자리 — 실측(8/19~9/2)에서
     예정 78회차 중 14회가 비었는데, 빈 회차는 줄 자체가 안 그려져 **없어진 줄도 몰랐다.**
     details가 아니라 div다(펼칠 내용이 없다). 발송 점·AI 분류 실패 칩과 같은 자리에
     "수집 기록 없음"만 회색으로 둔다 — "누락"이라 단정하지 않는 이유는 아래 파이썬 쪽
     _MISSING_SLOT_TIP 주석 참고. */
  .slot-missing {{ margin: 0 0 0 22px; border-top: 1px solid {border}; padding: 8px 4px;
    display: flex; align-items: center; gap: 10px; color: {muted}; }}
  .slot-missing::before {{ content: "·"; color: {muted}; font-size: 0.75rem; }}
  .slot-missing .slot-label {{ flex: none; white-space: nowrap; }}
  .missing-mark {{ font-size: var(--fs-sm); color: {muted}; }}
  /* [추가: 2026-09-11] 담당자가 지운 회차 — 「수집 기록 없음」과 같은 자리에 「삭제함 ·
     되살리기」로 남는다. 지운 걸 "앱이 못 모은 것"처럼 보이게 두면 거짓말이 된다.
     수시 보관함의 같은 줄(.run-line.gone)과 같은 모양이다. */
  .slot-gone {{ margin: 0 0 0 22px; border-top: 1px solid {border}; padding: 8px 4px;
    display: flex; align-items: center; gap: 10px; color: {muted}; background: {bg}; }}
  .slot-gone::before {{ content: "·"; color: {muted}; font-size: 0.75rem; }}
  .slot-gone .slot-label {{ flex: none; white-space: nowrap; }}
  .gone-tag {{ flex: none; font-size: var(--fs-xs); border: 1px dashed {dash_border}; border-radius: var(--r-pill);
    padding: 0 8px; color: {muted}; background: {card}; }}
  .gone-when {{ font-size: var(--fs-sm); color: {text_faint}; }}
  .restore-btn {{ flex: none; margin-left: auto; border: none; background: none; color: {accent};
    font: inherit; font-size: var(--fs-sm); cursor: pointer; padding: 2px 4px; }}
  .restore-btn:hover {{ text-decoration: underline; text-underline-offset: 3px; }}
  details.slot.just-restored > summary {{ background: {row_moved}; }}
  /* 회차 줄 끝 🗑 — 확정본 기사 행의 🗑와 같은 무게(평소 회색, hover만 빨강). 확인창 없이
     바로 지우고, 그 자리에 「삭제함 · 되살리기」가 남는다. 오늘 회차·가장 최근 회차엔 안 그린다. */
  .slot-del {{ flex: none; margin-left: auto; border: none; background: none; color: {text_faint};
    cursor: pointer; padding: 3px 6px; border-radius: var(--r-sm); line-height: 1; display: inline-flex; font-size: 0.95rem; }}
  details.slot[open] > summary .slot-del {{ margin-left: 0; }}
  .slot-del:hover {{ color: {error}; background: {error_bg}; }}
  /* [추가: 2026-09-15] 날짜 줄 🗑 — 그 날의 (지울 수 있는) 회차 모두, 확인창 한 번. 모양은
     회차 줄 🗑와 같다. 통째로 지우면 날짜 줄이 all-gone이 되어 복사·txt·xlsx·🗑 대신
     「삭제함 · 모두 되살리기」가 보인다(수시 보관함 사안 줄과 같은 규칙). */
  .day-del {{ flex: none; border: none; background: none; color: {text_faint}; cursor: pointer;
    padding: 3px 6px; border-radius: var(--r-sm); line-height: 1; display: inline-flex; font-size: 0.95rem; font-weight: 400; }}
  .day-del:hover {{ color: {error}; background: {error_bg}; }}
  .day-gone-mark {{ display: none; align-items: center; gap: 6px; font-weight: 400; }}
  details.date.all-gone > summary .day-gone-mark {{ display: inline-flex; }}
  details.date.all-gone > summary .exp, details.date.all-gone > summary .day-del {{ display: none; }}
  .day-gone-mark .restore-btn {{ margin-left: 0; }}
  /* 선택 삭제 모드(body.hist-tidy) — 체크박스·회차별 건수·잠금 안내가 나오고, 꺼내는
     동작(복사·txt·xlsx)과 줄 끝 🗑는 숨는다. 건수는 이때만 보인다: 평소엔 찾는 기준이
     아니라 뺐지만(2026-08-21), 지울지 판단할 땐 근거가 된다. 0건은 앰버 굵게. */
  input.pick {{ display: none; width: 15px; height: 15px; margin: 0; flex: none; cursor: pointer; }}
  input.pick:disabled {{ cursor: not-allowed; opacity: 0.4; }}
  .pick-sp {{ display: none; width: 15px; flex: none; }}
  .slot-cnt, .lock-note, .tidy-only {{ display: none; }}
  .slot-cnt {{ flex: none; min-width: 34px; font-size: var(--fs-sm); color: {muted}; font-variant-numeric: tabular-nums; }}
  .slot-cnt.zero {{ color: {warn_accent}; font-weight: 700; }}
  .lock-note {{ font-size: var(--fs-xs); color: {muted}; font-weight: 400; }}
  body.hist-tidy input.pick, body.hist-tidy .pick-sp, body.hist-tidy .slot-cnt, body.hist-tidy .lock-note {{ display: inline-block; }}
  body.hist-tidy .tidy-only {{ display: inline-flex; }}
  body.hist-tidy .normal-only, body.hist-tidy .exp, body.hist-tidy .slot-del, body.hist-tidy .day-del,
  body.hist-tidy details.slot > summary .slot-export-actions {{ display: none; }}
  body.hist-tidy details.slot > summary::before, body.hist-tidy .slot-missing::before, body.hist-tidy .slot-gone::before {{ content: ""; }}
  details.slot:has(> summary input.pick:checked) > summary {{ background: {hover}; }}
  .result-bar .tidy-btn + .tidy-bar + .exp {{ margin-left: 0; }}
  .tidy-btn {{ margin-left: auto; border: 1px solid {border}; background: {card}; color: {muted}; border-radius: var(--r-md);
    padding: 6px 13px; font-size: var(--fs-sm); cursor: pointer; font-family: inherit; white-space: nowrap; }}
  .tidy-btn:hover {{ border-color: {error_border}; color: {error}; background: {error_bg}; }}
  .tidy-bar {{ margin-left: auto; align-items: center; gap: 8px; font-size: var(--fs-sm); color: {muted}; }}
  .pick-empty {{ border: 1px dashed {dash_border}; background: {card}; color: {text_soft}; border-radius: var(--r-md);
    padding: 5px 11px; font-size: var(--fs-sm); cursor: pointer; font-family: inherit; }}
  .pick-empty:hover {{ border-color: {accent}; color: {accent}; border-style: solid; }}
  .done-btn {{ border: 1px solid {accent}; background: {accent}; color: {on_fill}; border-radius: var(--r-md);
    padding: 5px 13px; font-size: var(--fs-sm); cursor: pointer; font-family: inherit; font-weight: 600; }}
  /* 고른 게 있을 때만 뜨는 바 — /hidden 일괄 복구 바·확정본 일괄이동 바와 같은 규칙과 색. */
  .sel-bar {{ display: none; position: sticky; top: 62px; z-index: 5; align-items: center; gap: 10px;
    background: {hover}; border: 1px solid {accent_border}; border-radius: var(--r-lg); padding: 8px 12px;
    margin: 0 0 8px; font-size: var(--fs-md); color: {header}; box-shadow: var(--sh-float); }}
  .sel-bar.on {{ display: flex; }}
  .sel-bar .sp {{ flex: 1; }}
  .sel-bar .clr {{ background: transparent; color: {muted}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 5px 10px; font-size: var(--fs-sm); cursor: pointer; font-family: inherit; }}
  .sel-bar .del {{ display: inline-flex; align-items: center; gap: 5px; background: {card}; color: {error};
    border: 1px solid {error}; border-radius: var(--r-md); padding: 5px 12px; font-size: var(--fs-sm); font-weight: 600;
    cursor: pointer; font-family: inherit; }}
  .sel-bar .del:hover {{ background: {error_bg}; }}
  .lazy-day-hint {{ color: {muted}; font-size: var(--fs-md); padding: 8px 4px; margin: 0; }}
  .cute-caption {{ font-family: '{cute_font_name}', sans-serif; font-size: 1.1rem; margin-top: 4px; }}
  /* [추가: 2026-08-21] 기간 조회 — app.adhoc.archive_renderer .period-bar와 같은 모양.
     다만 서버 왕복은 없다: 정기는 날짜 목록이 이미 전부 DOM에 있으므로(지연 로딩은
     "회차 안의 기사 내용"만 늦출 뿐 날짜 자체는 항상 렌더링돼 있다), 클라이언트 JS로
     날짜 블록을 보이기/숨기기만 하면 된다. */
  .period-bar {{ border: 1px solid {border}; border-radius: var(--r-lg); padding: 10px 12px; background: {bg}; margin: 14px 0; }}
  .prow {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  /* [수정: 2026-09-16] 두 줄(빠른 설정 / 직접 입력 + 사이 구분선)을 한 줄로 합쳤다 — 두 줄을 만들던 건
     안내 문구 둘(「날짜 기준으로 조회합니다」·「시작일 ~ 종료일을 직접 넣고 조회」)이었는데, 앞은
     「기간」 라벨이, 뒤는 날짜 칸 두 개와 ~가 이미 한 말이다. 컨트롤은 하나도 안 뺐다(101 → 52px).
     수시 보관함(app/adhoc/archive_renderer.py)도 같은 규칙. 시안 HISTORY_PERIOD_BAR_MOCKUP.html B안. */
  .prow .pdiv {{ width: 1px; align-self: stretch; background: {border}; margin: 0 2px; }}
  .prow .jlab {{ color: {muted}; font-size: var(--fs-sm); font-weight: 600; width: 44px; flex: none; }}
  .prow input[type=date] {{ font: inherit; font-size: var(--fs-md); border: 1px solid {border}; border-radius: var(--r-md);
    background: {card}; color: {text}; padding: 5px 8px; font-variant-numeric: tabular-nums; }}
  .prow .tilde {{ color: {muted}; margin: 0 2px; font-weight: 600; }}
  .prow .go {{ border: 1px solid {accent}; background: {accent}; color: {on_fill}; border-radius: var(--r-md);
    padding: 6px 14px; font-size: var(--fs-sm); font-weight: 600; cursor: pointer; font-family: inherit; }}
  .prow .go:hover {{ filter: brightness(1.08); }}
  .seg {{ display: inline-flex; border: 1px solid {border}; border-radius: var(--r-md); overflow: hidden; background: {card}; }}
  .seg-btn {{ border: none; background: {card}; color: {muted}; font: inherit; font-size: var(--fs-sm);
    padding: 6px 15px; cursor: pointer; border-right: 1px solid {border}; }}
  .seg-btn:last-child {{ border-right: none; }}
  .seg-btn.on {{ background: {accent}; color: {on_fill}; font-weight: 600; }}
  .result-bar {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 14px 2px 8px; }}
  .result-line {{ font-size: var(--fs-sm); color: {muted}; margin: 0; }}
  .result-line b {{ color: {header}; }}
  /* 복사/txt/엑셀 — 결과 전체·날짜·회차 세 층에 같은 모양(app.adhoc.archive_renderer의
     같은 클래스와 동일 규칙). 날짜·결과 전체 층은 "이미 펼쳐서(=로딩된) 화면에 있는
     내용"만 모아 클라이언트에서 이어붙인다 — 회차 층이 이미 만들어둔 data-copy-text를
     재사용하므로 서버에 텍스트를 새로 계산시키지 않는다. 엑셀만 예외로 서버 재계산이
     필요해 기존 /download-excel-history를 그대로 재사용한다(날짜 목록만 넘기면 된다). */
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
  /* 상태 칩 — 근거는 전부 이미 저장된 값(오늘 날짜 등)뿐, 새로 계산하지 않는다.
     회차 수·기사 건수는 상시 표시하지 않는다 — 하루 4회 고정이라 정보가 아니고,
     건수는 찾는 기준이 아니다(사용자 판단). 발송 여부는 더 이상 이 칩이 아니라
     아래 .sent-dot이 맡는다. */
  .chip {{ font-size: var(--fs-xs); border-radius: var(--r-pill); padding: 1px 9px; border: 1px solid; flex: none; }}
  .chip-today {{ background: {hover}; color: {accent}; border-color: {accent_border}; font-weight: 600; }}
  /* [추가: 2026-09-03] 그날 회차가 하나도 없는 날짜 — "주의"(앰버) 세트를 그대로 쓴다
     (회차 마감 배너와 같은 뜻: "지금 보고 있는 게 네가 생각하는 그거랑 조금 다르다").
     반드시 .chip 뒤에 와야 한다 — .chip의 `border: 1px solid` 단축 속성이 뒤에 오면
     여기 border-color를 덮어쓴다. */
  .chip-missing {{ background: {warn_bg}; color: {warn_text}; border-color: {warn_border}; }}
  /* [추가: 2026-09-11] 살아 있는 회차는 없고 지운 회차만 남은 날짜 — 「삭제함」 줄과 같은 점선. */
  .chip-gone {{ background: {card}; color: {muted}; border-color: {dash_border}; border-style: dashed; }}
  /* [추가: 2026-08-26] 발송 여부 = 칩 대신 점 하나(사용자 요청 — "발송 완료"보다
     키워드가 더 자주 보게 되는 정보라, 칩이 그 줄의 무게를 더 가져가면 안 된다).
     성공은 꽉 찬 초록 점, 실패는 속이 빈 빨간 링 — 색만 다르면 적록색약에서
     구분이 안 되므로 "형태"를 가른다. 사유는 title 툴팁으로(마우스오버). */
  .sent-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: var(--r-circle);
    background: {send}; flex: none; }}
  /* [추가: 2026-08-26] 실패 = 속 빈 빨간 링. 성공 점과 크기를 다르게 잡은 건(9px vs 7px)
     테두리만 그리면 채운 점보다 시각적으로 작아 보여서 — 링 두께(2px)를 감안해도
     같은 "무게"로 읽히게 살짝 키웠다. */
  /* [추가: 2026-08-27] 소제목이 규칙 기반(단어 빈도) 폴백으로 지어진 회차 표식.
     발송 점(.sent-dot)과 달리 "값"이 아니라 "주의"라 앰버 계열을 쓴다(CLAUDE.md
     "Design direction" — 주의는 앰버, 오류는 빨강). 판정은 app.classifier.
     run_looks_rule_based이고, 저장된 classification_degraded가 없는 옛 회차도
     소제목 이름 모양으로 소급 판정된다. */
  .degraded-mark {{ display: inline-block; margin-left: 6px; font-size: var(--fs-xs);
    color: {warn_text}; background: {warn_chip_bg}; border: 1px solid {warn_border};
    border-radius: var(--r-sm); padding: 0 5px; line-height: 1.5; cursor: help; }}
  .sent-dot-link {{ display: inline-flex; align-items: center; justify-content: center; flex: none;
    width: 18px; height: 18px; margin: -5px -4px; border-radius: var(--r-circle); }}
  .sent-dot-link:hover {{ background: {hover}; }}
  .sent-dot.fail {{ width: 9px; height: 9px; border-radius: var(--r-circle); background: transparent;
    border: 2px solid {error}; box-sizing: border-box; }}
  /* [추가: 2026-08-26] 회차별 키워드 메모 — 칩이 아니라 그냥 회색 글자다. 참고용 자유
     텍스트지 라벨·발송상태처럼 "구조화된 값"이 아니라서 테두리를 두르지 않는다
     (app.renderer의 "🔍 검색어" 표시와 같은 판단). 잘리면 title 툴팁에 전체가 뜬다. */
  /* 「✂ 오늘만 여기서 끊기」로 생긴 회차 — 중립 회색 작은 알약(설명은 title) */
  .cut-mark {{ flex: none; font-size: var(--fs-xs); color: {muted}; background: {pill_bg}; border: 1px solid {border};
    border-radius: var(--r-pill); padding: 0 7px; cursor: help; }}
  .slot-note {{ font-size: var(--fs-sm); color: {muted}; flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  details.slot > summary > .slot-label {{ flex: none; white-space: nowrap; }}
  details.date {{ margin: 0; border-top: 1px solid {border}; }}
  details.date > summary {{
    list-style: none; cursor: pointer; display: flex; align-items: center; gap: 10px;
    padding: 10px 4px; font-size: var(--fs-lg); color: {header}; font-weight: 600;
  }}
  details.date > summary::-webkit-details-marker {{ display: none; }}
  details.date > summary::before {{ content: "▸"; color: {muted}; font-size: 0.8rem; font-weight: 400; }}
  details.date[open] > summary::before {{ content: "▾"; }}
  details.date > summary:hover {{ background: {hover}; }}
  details.slot {{ margin: 0 0 0 22px; border-top: 1px solid {border}; }}
  /* [수정: 2026-08-12] 토글을 열었을 때 시간대 제목 옆에 회차 전체 복사·다운로드
     버튼을 붙인다(사용자 요청 — "1회차의 기사들을 내보내고 싶을 것 같다"). 여러 토글을
     동시에 열 수 있는 화면이라 액션 툴바처럼 파란 톤을 쓰면 화면이 시끄러워진다 —
     회색톤(사용자 선택)으로 존재감을 낮췄다. */
  details.slot > summary {{
    list-style: none; cursor: pointer; display: flex; align-items: center;
    justify-content: flex-start; gap: 10px; padding: 8px 4px;
  }}
  details.slot > summary::-webkit-details-marker {{ display: none; }}
  details.slot > summary::before {{ content: "▸"; color: {muted}; font-size: 0.75rem; }}
  details.slot[open] > summary::before {{ content: "▾"; }}
  details.slot > summary:hover {{ background: {hover}; }}
  .slot-export-actions {{ display: none; gap: 2px; flex-shrink: 0; margin-left: auto; }}
  details.slot[open] > summary .slot-export-actions {{ display: flex; }}
  .slot-export-actions button, .slot-export-actions a.btn {{
    background: transparent; color: {text_soft}; border: none; border-radius: var(--r-md);
    font-weight: 500; padding: 4px 8px; font-size: var(--fs-sm); font-family: inherit; cursor: pointer;
    text-decoration: none; display: inline-flex; align-items: center;
  }}
  .slot-export-actions button:hover, .slot-export-actions a.btn:hover {{
    background: transparent; color: {accent}; text-decoration: underline; text-underline-offset: 3px; }}
  /* [추가: 2026-07-30] 저장 시점의 소제목 구성(group 필드)이 있는 회차는 이 제목으로
     묶어서 보여준다 — 재분류가 아니라 저장해둔 결과를 그대로 복원하는 것뿐이다. */
  .history-group {{ margin-top: 14px; }}
  .history-group h3 {{
    font-size: var(--fs-base); color: {header}; border-bottom: 1px solid {border};
    padding-bottom: 4px; margin: 0 0 6px 20px;
  }}
  /* [수정: 2026-09-16] 행 여백 한 단계씩 축소 — padding은 원래 4px, 제목·메타 줄 사이 4→2px,
     행 사이 10→6px. 글자·버튼 크기는 그대로다(한 건 74 → 64px). 확정본·초안·정기 보관함·
     수시 네 파일에 같은 값이 복제돼 있으니 한쪽만 고치지 않는다. 시안 ARTICLE_ROW_DENSITY_MOCKUP.html B안. */
  .article {{ margin: 6px 0 6px 20px; line-height: 1.5; padding: 4px 6px; border-radius: var(--r-md); }}
  /* [추가: 2026-08-20] app.renderer와 동일한 이유(위 finalize-added 주석 참고) —
     render_article이 [단독] 기사에 항상 붙이는 art-scoop/t-scoop, [속보]의 t-flash도
     이 화면 CSS에 없으면 알맹이 없이 밋밋해진다. 이 화면엔 is-new-arrival/just-moved
     같은 경쟁 오버레이가 없어 순서 걱정 없이 그대로 옮겨 왔다. */
  .article.art-scoop {{ background: {scoop_bg}; border-left: 3px solid {scoop_bar}; padding-left: 9px; }}
  .t-scoop {{ color: {scoop_text}; font-weight: 800; }}
  .t-flash {{ color: {flash_text}; font-weight: 600; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 마우스 오버 시 연한 회색 표시. */
  .article:hover {{ background: {row_hover}; }}
  /* [수정: 2026-08-27] 초안 마감 시점에 자동 배정된 기사(render_article이
     article["finalize_auto_assigned"]를 보고 항상 붙이는 마크업, 이 화면도
     render_article을 그대로 재사용한다)의 **연보라 음영·왼쪽 선을 이 화면에서만 뺐다**
     — 2026-08-20엔 확정본과 똑같이 보여줬는데, 그 색이 하는 말은 "초안에서 못 본 사이
     AI가 붙였으니 확인해라"는 할 일 알림이고 보관함엔 확인할 수단이 아예 없다
     (「확인했어요」 배너도, 큐레이션 버튼도 없는 읽기 전용 화면이라 끌 수 없는 알림이
     영원히 켜져 있는 꼴이었다). 여러 회차를 동시에 펼쳐 훑는 화면이라 회차마다 흩뿌려진
     연보라가 정작 찾는 걸 가리기도 했다 — 발송 상태를 칩에서 점 하나로 줄인 것과 같은
     이유(사용자 결정). 배지(`.finalize-added-badge`)는 남긴다: 여기서 그건 알림이 아니라
     "이 배정은 담당자가 검토한 게 아니라 마감 때 AI가 붙인 것"이라는 출처 기록이고,
     작고 조용해서 스캔을 방해하지 않는다. 결과적으로 확정본에서 「확인했어요」를 누른
     뒤 상태와 같아진다("보관함 = 이미 지나간 것"). */
  .finalize-added-badge {{
    flex-shrink: 0; padding: 1px 7px; border-radius: var(--r-pill); font-size: var(--fs-xs); font-weight: 700;
    background: {card}; color: {ai_text}; border: 1px solid {ai_border}; white-space: nowrap;
    position: relative; top: -0.15em;  /* [수정: 2026-08-21] 제목보다 글자가 작은 알약이라 baseline 정렬만으론 약간 아래로 보인다(실측: 위 여백 +1px, 아래 -2.3px) — 반 칸(0.15em) 올려 제목 글자 높이 한가운데에 맞춘다. */
  }}
  /* [추가: 2026-09-03] "앞 회차 기사" 칩 — 회차 마감 순간 네이버 검색이 아직 안 주던
     기사를 다음 회차가 주워 담았을 때 render_article이 붙이는 마크업(late_pickup).
     이 화면도 render_article을 그대로 재사용하므로 여기에도 같은 값의 규칙이 필요하다 —
     없으면 서식 없는 맨 글자로 뜬다. 위 .finalize-added-badge와 같은 판단으로 **남긴다**:
     보관함에서 이건 알림이 아니라 "이 기사는 앞 회차 것인데 이 회차에 담겼다"는 출처
     기록이고, 지난 회차를 훑다 "왜 14시 회차에 10:57 기사가 있지?"를 묻는 자리가 바로
     여기다. 확정본 쪽 상단 배너(.late-banner)는 이 화면에 없다 — 그건 "지금 확인해라"는
     할 일 알림이라 읽기 전용 화면엔 끌 수단이 없다(연보라 음영을 뺀 것과 같은 이유). */
  .late-badge {{
    flex-shrink: 0; margin-left: 6px; padding: 1px 7px; border-radius: var(--r-pill); font-size: var(--fs-xs);
    color: {warn_text}; background: {warn_chip_bg}; border: 1px solid {warn_border}; white-space: nowrap;
  }}
  /* [수정: 2026-08-13] app.renderer와 동일 — summary를 1행에서 2행(.title-row/
     .action-row)으로 나눴다. 제목이 게시시각·액션 묶음과 폭을 다투다 대부분 2~3줄로
     꺾이던 문제 때문. */
  .article summary {{
    cursor: pointer; display: flex; flex-direction: column; gap: 2px;
    -webkit-tap-highlight-color: transparent;
  }}
  .title-row {{ display: flex; align-items: baseline; justify-content: flex-start; gap: 8px; }}
  /* [수정: 2026-08-13] app.renderer와 동일 — 좁은 화면에서 액션 묶음이 옆으로 넘치던
     문제(실측) 때문에 wrap을 허용한다. */
  .action-row {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; row-gap: 4px; }}
  /* [추가: 2026-08-10] app.renderer와 동일 — render_article()이 만드는 🗑️ 등 액션
     버튼 묶음(.article-actions)이 이 화면엔 margin-left:auto가 없어 제목 길이에 따라
     위치가 들쭉날쭉했다(사용자 스크린샷) — flex 구조와 함께 적용해야 2행 오른쪽 끝에
     고정된다. [수정: 2026-08-13] 그래도 좁은 화면에서 안 들어가면 wrap한다. */
  .article-actions {{
    margin-left: auto; display: flex; align-items: center; justify-content: flex-end;
    gap: 4px; flex-shrink: 0; flex-wrap: wrap; row-gap: 4px;
  }}
  .article summary::marker {{ color: {muted}; }}
  /* [추가: 2026-08-07] app.renderer와 동일 — 모바일 사파리 탭 하이라이트 잔상 방지. */
  .title-line {{ color: {text}; -webkit-tap-highlight-color: transparent; }}
  /* [추가: 2026-09-11] app.renderer와 동일 — 제목 앞 언론사를 괄호 대신 회색·조금 작은 글자로. */
  .title-outlet {{ color: {muted}; font-size: 0.86em; font-weight: 500; margin-right: 0.5em; }}
  /* [추가: 2026-08-10] app.renderer와 동일 — 게시 시각은 제목보다 작고 옅게 표시한다.
     render_article()이 만드는 <span class="pub-time">이 이 화면(지난 기사)에는 이
     CSS 규칙 자체가 빠져 있어서 제목과 똑같은 굵기/크기로 보이던 문제(발견: 사용자
     스크린샷). */
  .pub-time {{
    flex-shrink: 0; color: {muted}; font-size: var(--fs-sm); white-space: nowrap;
    user-select: none; -webkit-user-select: none;
  }}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: var(--fs-md); }}
  .hide-btn, .copy-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: var(--fs-base);
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .hide-btn:hover {{ color: {error}; }}
  /* [수정: 2026-08-14] app.renderer와 동일 — 원문보기가 아이콘에서 게시시각 옆 텍스트
     링크로 바뀌었다(복사 아이콘과 실루엣이 겹쳐 구분이 안 된다는 지적). 이 화면은
     체크박스가 없어 들여쓰기(app.renderer의 :has(.article-select) 규칙)는 적용
     대상이 아니다. */
  .time-sep {{ color: {muted}; font-size: var(--fs-sm); margin: 0 2px; user-select: none; -webkit-user-select: none; }}
  .origin-link-text {{
    flex-shrink: 0; color: {accent}; font-size: var(--fs-sm); text-decoration: none;
    display: inline-flex; align-items: center; gap: 2px; white-space: nowrap;
  }}
  .origin-link-text:hover {{ text-decoration: underline; }}
  .origin-link-icon {{ width: 0.75em; height: 0.75em; }}
  .copy-btn {{ display: inline-flex; align-items: center; }}
  .copy-btn .icon-done {{ display: none; }}
  .copy-btn.is-copied .icon-default {{ display: none; }}
  .copy-btn.is-copied .icon-done {{ display: inline-flex; }}
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
  /* [추가: 2026-07-26] 다른 화면들과 같은 상단 고정 바.
     [수정: 2026-08-10] 홈만 있던 것에 실시간 현황·스크랩 초안 바로가기를 추가했다
     (사용자 요청) — 다른 화면들과 같은 3칸 구성(왼쪽 홈·가운데 실시간 현황·오른쪽
     나머지 하나)으로 맞췄다. */
  /* [수정: 2026-09-16] 카드 위 여백 60→44px — 60px은 상단 고정바(54px)를 피하려는 값인데 글자 위로
     30px이 비었다. 44px이면 16px 남는다. 확정본·초안·실시간·정기 보관함·설정·수시·정책 단어 추이
     일곱 화면이 같은 값을 써야 화면을 오갈 때 제목이 들썩이지 않는다(수시·추이는 84→68px 형태).
     시안 SUBHEAD_SPACING_MOCKUP.html B안. */
  .container {{ padding-top: 44px; }}
{topnav_style}
  /* [추가: 2026-08-13] app.renderer와 동일 — 단색 SVG 아이콘(app.icons) 공통 크기·색. */
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
  /* [추가: 2026-09-16] 🏷 라벨 팝오버 — 확정본·초안과 같은 값(app.renderer.label_popover_style). */
{label_popover_style}
</style>
</head>
<body>
{topnav_html}
<div class="container">
  <h1><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10.5h18"/></svg> 정기 보관함</h1>
  <p class="page-sub">날짜별로 쌓인 스크랩 · {retention_label} 보관됩니다</p>
  {period_and_result_html}
  {body}
</div>
<script>
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
// [추가: 2026-08-12] 회차(시간대) 전체 복사 — ⋯ 메뉴 안이 아니라 항상 보이는
// 버튼이라 completion 표시 후 메뉴를 닫을 필요는 없다.
function copySlotText(btn) {{
  navigator.clipboard.writeText(btn.dataset.copyText).then(function() {{
    var original = btn.textContent;
    btn.textContent = "복사했습니다";
    setTimeout(function() {{ btn.textContent = original; }}, 900);
  }}).catch(function() {{
    alert("복사에 실패했습니다.");
  }});
}}
// [추가: 2026-09-16] 🏷 라벨 — 확정본·초안과 같은 코드(app.renderer.label_popover_script).
{label_popover_script}
function hideArticle(btn) {{
  var url = btn.dataset.url;
  var article = btn.closest(".article");
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
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
// [추가: 2026-08-12] 오래된 날짜는 처음엔 <summary>만 있고 안이 비어 있다 — 실제로
// 펼쳤을 때만 그 하루치를 서버에서 받아와 채운다(용량 다이어트). ontoggle은 열 때·닫을
// 때 둘 다 불리므로 details.open일 때만 동작하고, 이미 불러왔으면(.lazy-day-holder가
// 없어짐) 아무 일도 안 한다 — 같은 날짜를 여러 번 열고 닫아도 한 번만 요청한다.
function loadHistoryDay(details) {{
  if (!details.open) {{ return; }}
  var holder = details.querySelector(".lazy-day-holder");
  if (!holder) {{ return; }}
  fetch("http://{settings_host}:{settings_port}/history/day?date=" + details.dataset.date)
    .then(function(res) {{ return res.ok ? res.text() : Promise.reject(); }})
    .then(function(slotsHtml) {{ holder.outerHTML = slotsHtml; applyPendingPicks(details); }})
    .catch(function() {{ holder.innerHTML = '<p class="lazy-day-hint">불러오지 못했습니다 — 다시 펼쳐보세요.</p>'; }});
}}

// [추가: 2026-09-11] 회차 삭제 — 줄 끝 🗑(하나, 확인창 없음)와 [선택 삭제](여러 개, 확인창 한 번)가
// 같은 deleteRuns를 쓴다. 서버가 돌려준 「삭제함 · 되살리기」 줄로 그 자리만 갈아 끼우므로
// 새로고침이 없다 — 펼쳐 둔 날짜·스크롤 위치가 그대로다.
var HIST_API = "http://{settings_host}:{settings_port}";
var EMPTY_RUN_KEYS = {empty_run_keys_json};
var pendingPicks = new Set();
function slotByKey(key) {{
  return Array.prototype.find.call(document.querySelectorAll('[data-slot-key]'), function (el) {{ return el.dataset.slotKey === key; }});
}}
// 서버 응답(data)을 돌려주는 promise — 날짜 줄 🗑(deleteDay)가 지운 뒤 날짜 줄을 고친다. 실패면 null.
function deleteRuns(keys) {{
  if (!keys.length) {{ return Promise.resolve(null); }}
  var body = new URLSearchParams();
  keys.forEach(function (k) {{ body.append('key', k); }});
  return fetch(HIST_API + "/history/delete-runs", {{
    method: "POST", headers: {{"Content-Type": "application/x-www-form-urlencoded"}}, body: body
  }}).then(function (res) {{ return res.ok ? res.json() : Promise.reject(); }})
    .then(function (data) {{
      Object.keys(data.rows).forEach(function (k) {{
        var el = slotByKey(k);
        if (el) {{ el.outerHTML = data.rows[k]; }}
      }});
      onRunPickChange();
      if (data.failed && data.failed.length) {{ alert(data.failed.join('\\n')); }}
      return data;
    }})
    .catch(function () {{ alert("삭제하지 못했습니다 — 앱이 실행 중인지 확인해주세요."); return null; }});
}}
// [추가: 2026-09-15] 날짜 줄 🗑 — 그 날의 (지울 수 있는) 회차 전부. 여러 개라 확인창을 한 번
// 거친다. 회차 목록은 서버가 구워 넣은 값(data-keys)이다 — 아직 안 펼친 날짜는 회차 줄이
// 화면에 없어서다. 지운 뒤 안 불러온 날짜면 펼쳐 불러와 「삭제함」 줄을 보여준다.
function deleteDay(btn) {{
  var day = btn.closest('details.date');
  var keys = JSON.parse(btn.dataset.keys);
  if (!keys.length) {{ return; }}
  var label = day.querySelector(':scope > summary .d').textContent;
  var msg = label + ' 회차를 모두 지울까요?\\n회차 ' + keys.length + '개 · 기사 '
    + Number(btn.dataset.count || 0).toLocaleString() + '건';
  if (btn.dataset.keep) {{ msg += '\\n가장 최근 회차(' + btn.dataset.keep + ')는 남아요 — 확정본 화면과 자동발송이 보고 있어요.'; }}
  msg += '\\n\\n지운 자리에 「삭제함」이 남고, 거기서 되살릴 수 있어요.';
  if (!confirm(msg)) {{ return; }}
  deleteRuns(keys).then(function (data) {{
    if (!data || !Object.keys(data.rows).length) {{ return; }}
    btn.remove();
    if (!btn.dataset.keep) {{
      day.classList.add('all-gone');
      var restoreAll = day.querySelector(':scope > summary .day-gone-mark .restore-btn');
      if (restoreAll) {{ restoreAll.hidden = Object.keys(data.rows).length < 2; }}
    }}
    if (!historyDayIsLoaded(day) && day.open) {{ loadHistoryDay(day); }}
    else {{ day.open = true; }}
  }});
}}
// 날짜 줄 「모두 되살리기」 — 그 날의 「삭제함」 줄을 하나씩 차례로 되살린다(한꺼번에 보내지 않는다:
// 되살리기마다 인덱스·화면을 다시 쓰므로 순서대로가 안전하다).
function restoreDay(btn) {{
  var day = btn.closest('details.date');
  var buttons = Array.prototype.slice.call(day.querySelectorAll('.slot-gone .restore-btn'));
  btn.disabled = true;
  buttons.reduce(function (p, b) {{ return p.then(function () {{ return restoreRun(b); }}); }}, Promise.resolve())
    .then(function () {{
      btn.disabled = false;
      if (!day.querySelector('.slot-gone')) {{ day.classList.remove('all-gone'); }}
    }});
}}
function restoreRun(btn) {{
  var row = btn.closest('.slot-gone');
  btn.disabled = true;
  return fetch(HIST_API + "/history/restore-run", {{
    method: "POST", headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{name: btn.dataset.trash}})
  }}).then(function (res) {{ return res.json().then(function (data) {{ return {{ok: res.ok, data: data}}; }}); }})
    .then(function (r) {{
      if (!r.ok) {{ alert(r.data.error || "되살리지 못했습니다."); btn.disabled = false; return; }}
      row.outerHTML = r.data.html;
      var restored = slotByKey(r.data.key);
      if (restored) {{
        restored.classList.add('just-restored');
        setTimeout(function () {{ restored.classList.remove('just-restored'); }}, 1500);
      }}
    }})
    .catch(function () {{ alert("되살리지 못했습니다 — 앱이 실행 중인지 확인해주세요."); btn.disabled = false; }});
}}
function setHistoryTidy(on) {{
  document.body.classList.toggle('hist-tidy', on);
  if (!on) {{ clearRunPicks(); }}
}}
function pickedSlots() {{
  return Array.prototype.filter.call(document.querySelectorAll('details.slot > summary input.pick'), function (cb) {{ return cb.checked; }})
    .map(function (cb) {{ return cb.closest('details.slot'); }});
}}
function onRunPickChange() {{
  var slots = pickedSlots();
  var articles = slots.reduce(function (sum, s) {{ return sum + Number(s.dataset.count || 0); }}, 0);
  document.getElementById('hist-sel-n').textContent = slots.length;
  document.getElementById('hist-sel-n2').textContent = slots.length;
  document.getElementById('hist-sel-a').textContent = articles.toLocaleString();
  document.getElementById('hist-selbar').classList.toggle('on', slots.length > 0);
}}
function clearRunPicks() {{
  pendingPicks.clear();
  document.querySelectorAll('input.pick').forEach(function (cb) {{ cb.checked = false; }});
  document.querySelectorAll('details.date[data-pick-all]').forEach(function (d) {{ delete d.dataset.pickAll; }});
  onRunPickChange();
}}
// 날짜 줄 체크 = 그 날의 (지울 수 있는) 회차 전부. 아직 안 불러온 날짜면 펼쳐서 불러온 뒤에 고른다.
function pickDay(box) {{
  var day = box.closest('details.date');
  if (!historyDayIsLoaded(day)) {{
    if (box.checked) {{ day.dataset.pickAll = '1'; }} else {{ delete day.dataset.pickAll; }}
    day.open = true;
    return;
  }}
  day.querySelectorAll(':scope > details.slot > summary input.pick:not(:disabled)').forEach(function (cb) {{ cb.checked = box.checked; }});
  if (box.checked) {{ day.open = true; }}
  onRunPickChange();
}}
function applyPendingPicks(day) {{
  var all = day.dataset.pickAll === '1';
  delete day.dataset.pickAll;
  day.querySelectorAll(':scope > details.slot').forEach(function (slot) {{
    var cb = slot.querySelector(':scope > summary input.pick');
    if (!cb || cb.disabled) {{ return; }}
    if (all || pendingPicks.has(slot.dataset.slotKey)) {{ cb.checked = true; pendingPicks.delete(slot.dataset.slotKey); }}
  }});
  onRunPickChange();
}}
function pickEmptyRuns() {{
  EMPTY_RUN_KEYS.forEach(function (key) {{
    var day = document.querySelector('details.date[data-date="' + key.split('|')[0] + '"]');
    if (!day) {{ return; }}
    pendingPicks.add(key);
    if (historyDayIsLoaded(day)) {{ applyPendingPicks(day); day.open = true; }}
    else {{ day.open = true; }}
  }});
}}
function deletePickedRuns() {{
  var slots = pickedSlots();
  if (!slots.length) {{ return; }}
  var lines = slots.slice(0, 4).map(function (s) {{ return s.dataset.label + ' · ' + s.dataset.count + '건'; }});
  if (slots.length > 4) {{ lines.push('외 ' + (slots.length - 4) + '개'); }}
  if (!confirm('회차 ' + slots.length + '개를 삭제할까요?\\n' + lines.join('\\n') + '\\n\\n지운 자리에 「삭제함」이 남고, 거기서 되살릴 수 있어요.')) {{ return; }}
  deleteRuns(slots.map(function (s) {{ return s.dataset.slotKey; }}));
}}
// 선택 삭제 중엔 회차 줄을 눌러도 펼쳐지지 않고 체크된다(고르려다 기사 목록이 열리지 않게).
document.addEventListener('click', function (e) {{
  if (!document.body.classList.contains('hist-tidy')) {{ return; }}
  var summary = e.target.closest('details.slot > summary');
  if (!summary || e.target.matches('input.pick')) {{ return; }}
  e.preventDefault();
  var cb = summary.querySelector('input.pick');
  if (cb && !cb.disabled) {{ cb.checked = !cb.checked; onRunPickChange(); }}
}}, true);

// [추가: 2026-08-18] 엑셀 내보내기(PRD.md 기능11 규칙2) — 별도 기간 선택 UI 없이
// "지금 화면에 내용이 있는 날짜들"을 그대로 서버에 보낸다. 지연 로딩 날짜는 한 번이라도
// 펼쳐 불러온 뒤엔 다시 접어도 내용이 DOM에 남아 있으므로(loadHistoryDay가 통째로
// outerHTML을 바꿔치기), "열려 있는지"가 아니라 "불러왔는지"로 판단한다.
function historyDayIsLoaded(details) {{
  return !details.querySelector('.lazy-day-holder');
}}
function historyLoadedDayDetails() {{
  return Array.prototype.slice.call(document.querySelectorAll('details.date'))
    .filter(historyDayIsLoaded);
}}
function historySlotTexts(dayDetails) {{
  return Array.prototype.slice.call(dayDetails.querySelectorAll(':scope > details.slot .slot-export-actions [data-copy-text]'))
    .map(function (btn) {{ return btn.dataset.copyText; }});
}}
function historyDateRangeLabel(dates) {{
  var sorted = dates.slice().sort();
  if (sorted.length <= 1) {{ return sorted[0] || ''; }}
  return sorted[0] + '~' + sorted[sorted.length - 1].slice(5);
}}
function historyDownloadExcelForDates(dates) {{
  var form = document.createElement('form');
  form.method = 'POST';
  form.action = 'http://{settings_host}:{settings_port}/download-excel-history';
  dates.forEach(function (d) {{
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'dates';
    input.value = d;
    form.appendChild(input);
  }});
  document.body.appendChild(form);
  form.submit();
  form.remove();
}}
function historyDownloadText(filename, text) {{
  var form = document.createElement('form');
  form.method = 'POST';
  form.action = 'http://{settings_host}:{settings_port}/download-text';
  var fnInput = document.createElement('input');
  fnInput.type = 'hidden'; fnInput.name = 'filename'; fnInput.value = filename;
  var txtInput = document.createElement('input');
  txtInput.type = 'hidden'; txtInput.name = 'text'; txtInput.value = text;
  form.appendChild(fnInput);
  form.appendChild(txtInput);
  document.body.appendChild(form);
  form.submit();
  form.remove();
}}
function historyCopyText(btn, text) {{
  navigator.clipboard.writeText(text).then(function () {{
    var original = btn.textContent;
    btn.textContent = '복사했습니다';
    setTimeout(function () {{ btn.textContent = original; }}, 900);
  }}).catch(function () {{
    alert('복사에 실패했습니다.');
  }});
}}
// [추가: 2026-08-21] 날짜 층 복사/txt/엑셀 — 회차 층이 이미 만들어둔 data-copy-text를
// 그대로 이어붙인다(서버에 텍스트를 새로 계산시키지 않는다). 이 날짜를 한 번도 안
// 펼쳐봤으면(지연 로딩 미완료) 이어붙일 내용이 없으므로 먼저 펼치라고 안내한다.
function copyHistoryDayText(btn) {{
  var day = btn.closest('details.date');
  if (!historyDayIsLoaded(day)) {{ alert('날짜를 먼저 펼쳐주세요.'); return; }}
  historyCopyText(btn, historySlotTexts(day).join('\\n\\n'));
}}
function downloadHistoryDayText(btn) {{
  var day = btn.closest('details.date');
  if (!historyDayIsLoaded(day)) {{ alert('날짜를 먼저 펼쳐주세요.'); return; }}
  historyDownloadText(day.dataset.date + '_언론모니터링.txt', historySlotTexts(day).join('\\n\\n'));
}}
function downloadHistoryDayExcel(btn) {{
  var day = btn.closest('details.date');
  historyDownloadExcelForDates([day.dataset.date]);
}}
// [추가: 2026-08-21] 결과 전체(맨 위) 복사/txt/엑셀 — "지금 불러온 날짜들" 전부를
// 대상으로 한다(기존 엑셀 버튼과 같은 규칙을 셋 다에 동일하게 적용).
function copyHistoryAllText(btn) {{
  var days = historyLoadedDayDetails();
  var texts = [];
  days.forEach(function (d) {{ texts = texts.concat(historySlotTexts(d)); }});
  if (!texts.length) {{ alert('펼친 날짜가 없습니다. 내보낼 날짜를 먼저 펼쳐주세요.'); return; }}
  historyCopyText(btn, texts.join('\\n\\n'));
}}
function downloadHistoryAllText() {{
  var days = historyLoadedDayDetails();
  var texts = [];
  var dates = [];
  days.forEach(function (d) {{ texts = texts.concat(historySlotTexts(d)); dates.push(d.dataset.date); }});
  if (!texts.length) {{ alert('펼친 날짜가 없습니다. 내보낼 날짜를 먼저 펼쳐주세요.'); return; }}
  historyDownloadText(historyDateRangeLabel(dates) + '_언론모니터링.txt', texts.join('\\n\\n'));
}}
function downloadHistoryAllExcel() {{
  var dates = historyLoadedDayDetails().map(function (d) {{ return d.dataset.date; }});
  if (!dates.length) {{ alert('펼친 날짜가 없습니다. 내보낼 날짜를 먼저 펼쳐주세요.'); return; }}
  historyDownloadExcelForDates(dates);
}}
// [추가: 2026-08-21] 기간 조회 — 서버 왕복 없이 data-date만 보고 날짜 블록을
// 숨기고/보여준다(위 CSS 주석 참고). "오늘"은 클라이언트 시계로 계산하지만 이건
// 화면 필터일 뿐 저장 데이터에 영향이 없으므로(실시간 현황의 "N분 전"과 같은 성격)
// 시계가 조금 어긋나도 큰 문제가 안 된다.
function historyFmtDate(d) {{
  var mm = String(d.getMonth() + 1).padStart(2, '0');
  var dd = String(d.getDate()).padStart(2, '0');
  return d.getFullYear() + '-' + mm + '-' + dd;
}}
function historyPresetRange(unit) {{
  var today = new Date();
  if (unit === 'day') {{ return [historyFmtDate(today), historyFmtDate(today)]; }}
  if (unit === 'week') {{
    var s = new Date(today); s.setDate(s.getDate() - 6);
    return [historyFmtDate(s), historyFmtDate(today)];
  }}
  if (unit === 'month') {{
    var s2 = new Date(today.getFullYear(), today.getMonth(), 1);
    var e2 = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return [historyFmtDate(s2), historyFmtDate(e2)];
  }}
  return ['', ''];
}}
function historyRangeLabel(start, end) {{
  if (!start || !end) {{ return '전체 기간'; }}
  if (start === end) {{ return start; }}
  return start + ' ~ ' + end.slice(5);
}}
function applyHistoryPeriodFilter(start, end) {{
  var count = 0;
  document.querySelectorAll('details.date').forEach(function (d) {{
    var visible = (!start || d.dataset.date >= start) && (!end || d.dataset.date <= end);
    d.hidden = !visible;
    if (visible) {{ count++; }}
  }});
  document.getElementById('hist-result-count').textContent = count;
  var line = document.getElementById('hist-result-line');
  line.firstChild.textContent = historyRangeLabel(start, end) + ' · ';
  document.getElementById('hist-period-start').value = start;
  document.getElementById('hist-period-end').value = end;
}}
function setHistoryPeriodPreset(unit) {{
  var range = historyPresetRange(unit);
  document.querySelectorAll('#hist-period-seg .seg-btn').forEach(function (btn) {{
    btn.classList.toggle('on', btn.dataset.unit === unit);
  }});
  applyHistoryPeriodFilter(range[0], range[1]);
}}
function applyHistoryCustomRange() {{
  var start = document.getElementById('hist-period-start').value;
  var end = document.getElementById('hist-period-end').value;
  if (!start || !end) {{ return; }}
  if (start > end) {{ alert('시작일이 종료일보다 늦습니다.'); return; }}
  document.querySelectorAll('#hist-period-seg .seg-btn').forEach(function (btn) {{
    btn.classList.remove('on');
  }});
  applyHistoryPeriodFilter(start, end);
}}
</script>
</body>
</html>
"""


def _group_articles_by_saved_group(articles: list, run: dict) -> Optional["OrderedDict"]:
    """저장된 group 필드로 기사를 묶는다. 필드가 없는 옛 회차(레거시 데이터)는 None을
    돌려줘 호출부가 평평한 목록으로 대체 표시하게 한다(하위 호환) — HTML 렌더링
    (_render_slot)과 회차 전체 복사/다운로드 텍스트(_build_slot_plain_text)가 똑같은
    그룹 나눔을 봐야 "화면과 내보낸 게 다르다"가 안 생기므로 하나로 뺐다.

    [수정: 2026-09-10] 소제목 순서는 확정본과 같은 app.group_order.apply_group_order로
    정한다(그 회차의 round_id로 읽는다 — 이름표 load_group_labels와 같은 방식). 예전엔
    "기사 목록에서 먼저 나온 순"(= 언론사 우선순위순)을 그대로 써서, 담당자가 확정본에서
    ▲▼로 정한 순서가 보관함·복사·txt·엑셀에서 전부 사라졌다. 실제로 발송된 텍스트
    (app.renderer.latest_confirmed_report)에는 그 순서가 들어갔으므로, 보관함에서
    꺼낸 텍스트가 발송된 보고서와 달랐다(HISTORY.md "정기 보관함 소제목 순서" 참고).
    """
    if not articles or not all("group" in a for a in articles):
        return None
    grouped: "OrderedDict" = OrderedDict()
    for a in articles:
        grouped.setdefault(a["group"], []).append(a)
    ordered = apply_group_order(
        [{"name": name, "articles": items} for name, items in grouped.items()],
        round_id_for_run(run),
    )
    return OrderedDict((g["name"], g["articles"]) for g in ordered)


def build_excel_rows_for_dates(dates: list[str]) -> list[dict]:
    """엑셀 내보내기(PRD.md 기능11) — 지정한 날짜들의 회차를 rows로 만든다.

    화면(_render_slot)·복사 텍스트(_build_slot_plain_text)와 같은 그룹핑
    (_group_articles_by_saved_group)을 써서 소제목이 화면과 어긋나지 않는다.
    소제목은 스냅샷 시점 원본 이름 그대로 넣는다 — display_group_name/
    apply_subheading_format을 적용하지 않는다(app.renderer.render_page의 엑셀
    내보내기와 같은 이유: 이건 화면 표시가 아니라 데이터 계약이다). 레거시 회차
    (group 필드가 없는 옛 데이터)는 그룹 없이(빈 소제목) 평평하게 담긴다 — 화면도
    같은 회차를 평평한 목록으로 대체 표시한다.
    """
    rows = []
    for date_str in dates:
        for run in list_runs_for_date(date_str):
            articles = apply_summary_overrides(filter_hidden(run["articles"]))
            if not articles:
                continue
            grouped = _group_articles_by_saved_group(articles, run)
            groups_iter = grouped.items() if grouped is not None else [(None, articles)]
            for group_name, group_articles in groups_iter:
                for a in group_articles:
                    rows.append(
                        {
                            "스크랩일자": run["run_at"][:10],
                            "스크랩종료시간": run["run_slot"],
                            "발행일 발행시간": format_pub_datetime(a.get("pub_date")),
                            "언론사명": a.get("outlet", ""),
                            "기사제목": a.get("title", ""),
                            "URL": a.get("url", ""),
                            "소제목": group_name or "",
                            "라벨명": ", ".join(labels_for_url(a["url"])),
                        }
                    )
    return rows


def excel_filename_for_dates(dates: list[str]) -> str:
    """PRD.md 기능11 규칙7 — 여러 날짜를 펼쳐 뽑을 때는 범위로 표기한다
    (예: "2026-08-11~08-18_언론모니터링.xlsx")."""
    return f"{date_range_label(dates)}_언론모니터링.xlsx"


def _build_slot_plain_text(
    run: dict, articles: list, labels: dict, line_template: str, subheading_format: str
) -> str:
    """토글 옆 "복사"/"다운로드"가 쓰는, 회차 하나 전체의 로데이터 텍스트를 만든다.

    [추가: 2026-08-12] 헤더를 다른 화면들의 "언론 모니터링 OO시 기준"(오늘임을 전제하는
    시의성 있는 표현) 대신 "YYYY-MM-DD HH:MM 기준"으로 못박는다 — 지난 기사를 꺼내
    쓰는 목적 자체가 팩트체크·기록 확인이라, "오늘"이 생략된 표현은 그 용도에 안 맞는다
    (사용자 요청, "이건 로데이터 개념이니까"). 같은 이유로 AI 키워드/요약 블록·직접
    작성 메모는 다른 화면 내보내기와 마찬가지로 제외한다(원본 그대로의 언론사·제목·
    URL만 담는다).
    """
    date_str = run["run_at"][:10]
    lines = [f"{date_str} {run['run_slot']} 기준", ""]
    if not articles:
        lines.append("수집된 기사 없음")
        return "\n".join(lines).rstrip() + "\n"
    grouped = _group_articles_by_saved_group(articles, run)
    groups_iter = grouped.items() if grouped is not None else [(None, articles)]
    for name, group_articles in groups_iter:
        if name is not None:
            section_name = display_group_name(name, labels)
            if name != UNCLASSIFIED_GROUP_NAME:
                section_name = apply_subheading_format(subheading_format, section_name)
            lines.append(section_name)
        for a in group_articles:
            lines.append(apply_line_template(line_template, a["outlet"], a["title"]))
            lines.append(a["url"])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _slot_export_actions_html(
    run: dict, articles: list, labels: dict, line_template: str, subheading_format: str
) -> str:
    """토글을 열었을 때만 시간대 제목 옆에 나타나는 회색톤 복사·다운로드 버튼
    (details.slot[open] > summary .slot-export-actions, CSS로 열림 여부만 따라간다).
    여러 토글을 동시에 열 수 있는 화면이라 액션 툴바의 파란 톤 대신 눈에 덜 띄는 회색을
    쓴다(사용자 선택). event.stopPropagation()이 없으면 클릭이 토글 자체(펼치기/접기)로
    번진다 — 다른 화면의 summary 안 버튼들과 같은 이유."""
    plain_text = _build_slot_plain_text(run, articles, labels, line_template, subheading_format)
    date_str = run["run_at"][:10]
    # [수정: 2026-08-18] 정기·수시 공통 파일명 규칙(날짜_종류_구분)으로 통일 —
    # app.renderer.render_page의 export_filename과 동일한 형식.
    filename = html.escape(f"{date_str}_언론모니터링_{run['run_slot'].replace(':', '-')}기준.txt")
    copy_text_attr = html.escape(plain_text)
    # [수정: 2026-08-12] data: URI 대신 서버가 Content-Disposition으로 응답하는 표준
    # 다운로드로 바꿨다(app.settings_server._handle_download_text) — 지난 기사는 회차마다
    # 이 버튼이 하나씩 있어(최대 7일×여러 회차) 예전 방식(본문 전체를 percent-encode해
    # href에 통째로 박음)이 history.html 용량을 가장 많이 잡아먹었다(실측 389KB, 13%).
    # 이 화면은 읽기 전용이라 렌더링 시점 텍스트를 그대로 히든 인풋에 심으면 되고,
    # 확정본·초안과 달리 PLAIN_TEXT를 JS로 다시 채워 넣을 필요가 없다.
    return (
        '<span class="slot-export-actions">'
        '<button type="button" onclick="event.stopPropagation(); copySlotText(this);" '
        f'data-copy-text="{copy_text_attr}">복사</button>'
        '<form method="POST" action="/download-text" style="display:contents" '
        'onclick="event.stopPropagation();">'
        f'<input type="hidden" name="filename" value="{filename}">'
        f'<input type="hidden" name="text" value="{copy_text_attr}">'
        '<button type="submit" class="btn">텍스트</button>'
        '</form>'
        "</span>"
    )


def _sent_dot_link(run: dict, dot_class: str, tip: str) -> str:
    """발송 점 — 누르면 발송 기록(/send-log)의 그 회차 줄로. 점이 7px라 누르는 자리는 링크가 넓힌다.
    tip은 이미 이스케이프된 툴팁."""
    href = html.escape(send_log_href(run["run_at"][:10], run["run_slot"]), quote=True)
    return (
        f'<a class="sent-dot-link" href="{href}" title="{tip}&#10;누르면 받은 사람 보기" '
        f'onclick="event.stopPropagation()"><span class="{dot_class}"></span></a>'
    )


def _send_failure_tooltip(run: dict) -> str:
    """실패 점(.sent-dot.fail)의 title 툴팁 문구를 만든다.

    [추가: 2026-08-26] 회차 파일의 run["send_failure"]["channels"](app.storage.
    record_send_failure가 채운 값, [{"channel","target","error"}, ...])를 줄마다
    풀어 보여준다 — app.telegram_bot/app.email_sender가 이미 사람이 읽을 한국어로
    다듬어둔 사유(_describe_error)를 그대로 쓰므로 여기서 새로 판단하지 않는다.

    send_count > 0이면(다른 채널은 성공해 이미 "발송 완료"로 기록된 부분 실패
    상태 — app.confirm_send의 "채널 하나라도 전원 성공하면 발송 완료" 규칙) 그
    사실을 첫 줄에 덧붙인다 — 안 그러면 이 점(빨간 링)만 보고 아무 것도 안 나간
    걸로 오해할 수 있다.
    """
    failure = run.get("send_failure") or {}
    lines = (
        ["일부는 못 받았습니다(나머지는 정상 발송)"]
        if run.get("send_count", 0) > 0
        else ["발송 실패"]
    )
    for ch in failure.get("channels", []):
        target = ch.get("target")
        prefix = f"{ch.get('channel', '')} — {target}: " if target and target != "설정" else f"{ch.get('channel', '')} — "
        lines.append(f"{prefix}{ch.get('error', '')}")
    return "&#10;".join(html.escape(line) for line in lines)


_MISSING_SLOT_TIP = (
    "이 시각에 저장된 회차가 없습니다 — 그 시간에 앱이 꺼져 있었을 가능성이 큽니다.&#10;"
    "지금 설정된 스케줄 기준이라, 그날 스케줄이 달랐다면 원래 예정에 없던 회차일 수도 있어요."
)


def expected_slots_for_date(run_date) -> list:
    """그 날짜에 예정돼 있었을 회차 끝 시각 목록(최신 먼저).

    [추가: 2026-09-03] **지금 저장된 스케줄** 기준이라는 점이 이 함수의 한계이자 전부다 —
    그날 실제로 어떤 스케줄이 걸려 있었는지는 아무 데도 저장돼 있지 않다. 그래서 화면
    문구도 "누락"이 아니라 "수집 기록 없음"(사실)이고, 툴팁이 그 한계를 그대로 밝힌다
    (앱이 모르는 걸 단정하지 않는다 — 발송 기록 없는 회차를 "미발송"이라 안 쓰는 것과
    같은 규칙).

    요일만 보면 되므로 그 날짜의 정오를 넘긴다. 설정을 못 읽으면 빈 목록 —
    "없는 회차를 지어내는" 방향으로 틀리면 안 된다.
    """
    try:
        times = active_schedule_times(load_settings(), datetime.combine(run_date, dt_time(12, 0)))
    except Exception:  # 설정 파일이 깨졌거나 못 읽는 경우 — 이 표시는 부가 기능이라 조용히 포기한다.
        logger.warning("예정 회차를 계산하지 못해 '수집 기록 없음' 줄을 건너뜁니다", exc_info=True)
        return []
    slots = sorted({w["end"] for w in times}, reverse=True)
    now = datetime.now()
    if run_date == now.date():
        # 아직 오지 않은 회차는 "기록 없음"이 아니라 그냥 아직 안 온 것이다 — 오늘 줄에
        # 미래 시각을 빈칸으로 세우면 매일 오전 내내 "오늘은 다 비었다"로 읽힌다.
        slots = [slot for slot in slots if slot <= now.strftime("%H:%M")]
    return slots


def _render_missing_slot(slot: str) -> str:
    """"14:00 기준 · 수집 기록 없음" 한 줄. 펼칠 내용이 없으므로 details가 아니다."""
    return (
        f'<div class="slot-missing" title="{_MISSING_SLOT_TIP}">'
        f'<span class="slot-label">{html.escape(slot)} 기준</span>'
        f'<span class="missing-mark">수집 기록 없음</span></div>'
    )


def _render_slot(
    run: dict, highlight_words: list, line_template: str,
    subheading_format: str = DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    open_first: bool = False,
    lock_reason: Optional[str] = None,
) -> str:
    """회차 하나(시간대)를 토글로 렌더링한다.

    [수정: 2026-08-21] "1. 16:30" 같은 회차 번호를 뗐다 — 하루 4회로 고정이라 순번
    자체가 정보가 아니고, 시간대를 최신 먼저로 보여주면서(app.history_renderer 전체
    규칙) 번호가 "1번=최신"으로 오히려 헷갈렸다(사용자 판단). 발송 여부는 이미 저장된
    run["sent_by"]/["send_count"]/["sent_at"]를 그대로 읽을 뿐 —
    app.confirm_send.record_send가 채워둔 값이라 새로 계산하지 않는다. 발송 기록이
    없으면 표식 자체를 안 띄운다 — "미발송"처럼 앱이 모르는 걸 단정하지 않는다
    (담당자가 앱 밖에서 복사해 처리했을 수도 있다, 사용자 판단).

    [수정: 2026-08-26] 발송 여부 표시를 칩에서 점(.sent-dot) 하나로 낮췄다 — 담당자들이
    이 줄에서 실제로 더 자주 보는 건 발송 여부가 아니라 그 회차에 적어둔 키워드 메모라,
    칩이 그 줄의 무게를 다 가져가면 안 된다는 지적(사용자 판단, HISTORY.md 같은 항목
    참고). 시각·자동/수동 여부는 title 툴팁으로 옮겼다. 같은 자리에 그 회차의 키워드
    메모(app.manual_keyword_note)도 테두리 없는 회색 글자로 붙는다 — 라벨·발송상태
    같은 "값"이 아니라 참고용 자유 텍스트라 칩으로 감싸지 않는다.

    [수정: 2026-07-30] 예전엔 소제목 재분류를 안 하고 언론사 우선순위 순 평평한
    목록만 보여줬는데("당시 화면을 그대로 복원할 필요는 없어 단순화"), 저장 시점에
    함께 저장해둔 소제목 구성("group" 필드, app.classifier.snapshot_group_names)이
    있으면 그걸 그대로 써서 소제목별로 묶어 보여준다 — 재분류가 아니라 이미 저장된
    결과를 복원하는 것뿐이라 큐레이션 버튼(✏️/↑/↓)은 여전히 없다. 이 필드가 생기기
    전에 저장된 옛 회차(레거시 데이터)는 group 필드가 없으므로, 그때는 예전 방식대로
    평평한 목록으로 대체 표시한다(하위 호환).

    [수정: 2026-08-14] 이름표(labels)를 매개변수로 안 받고 이 회차 자신의 round_id로
    직접 읽는다 — 이름표가 회차 단위로 저장되므로(사용자 요청), 지난 기사 화면처럼
    여러 날짜·여러 회차를 한 페이지에 같이 보여줄 때 하나의 labels dict를 공유하면
    안 된다(공유하면 이름표가 회차를 넘어 새는, 지금 없애려는 그 문제가 이 화면에도
    똑같이 생긴다).
    """
    labels = load_group_labels(round_id_for_run(run))
    articles = apply_summary_overrides(filter_hidden(run["articles"]))
    # [추가: 2026-09-16] 🏷 라벨 — 이 화면에서도 붙일 수 있다. `부정기사`처럼 나중에
    # 알게 되는 라벨은 회차가 지난 뒤에 붙는데(사용자 요청: "어? 어제 부정기사였는데
    # 이것도 붙여야 되는 거 아니야?"), 지금까지는 붙일 자리가 오늘 회차에만 있었다.
    # 이 화면이 읽기 전용인 근거(숨김·이동·이름바꾸기는 이미 나간 보고서를 고치는 일이고
    # 큐레이션 핸들러가 load_latest_run 고정)는 라벨에는 안 걸린다 — 라벨은 보고서
    # 텍스트에 안 나가고(화면·엑셀 전용), 회차가 아니라 URL을 키로 하는 전역 저장소라
    # load_latest_run을 아예 안 쓴다. 스냅샷의 scrap_date/scrap_end는 그 회차 값이
    # 그대로 들어가고 labeled_at만 오늘이 된다 — 뒤늦게 붙였다는 사실이 그렇게 남는다.
    known_labels_html = known_label_chips_html()
    # 라벨 저장소는 기사마다가 아니라 이 회차에 한 번만 읽는다(실측: 504건 화면에서
    # 기사마다 읽으면 83ms — 라벨이 늘수록 같이 커진다).
    labels_lookup = load_labels()
    label_scrap_date = (run.get("run_at") or datetime.now().isoformat())[:10]
    if not articles:
        body = '<div class="slot-empty">💤<div class="cute-caption">뉴스가 잠잠</div></div>'
    else:
        grouped = _group_articles_by_saved_group(articles, run)
        if grouped is not None:
            sections = []
            for name, group_articles in grouped.items():
                display_name = html.escape(display_group_name(name, labels))
                # [추가: 2026-08-12] 메일머지 "소제목 형식" — 임시 보관함은 저장된 회차에
                # 남을 일이 거의 없지만, 다른 화면들과 동일하게 방어적으로 제외한다.
                heading_name = (
                    display_name
                    if name == UNCLASSIFIED_GROUP_NAME
                    else apply_subheading_format(html.escape(subheading_format), display_name)
                )
                articles_html = "\n".join(
                    render_article(
                        a, highlight_words, line_template, show_relative_time=False,
                        show_label_control=True, group_name=name,
                        known_labels_html=known_labels_html,
                        scrap_date=label_scrap_date, scrap_end=run["run_slot"],
                        labels_lookup=labels_lookup,
                    )
                    for a in group_articles
                )
                sections.append(f'<div class="history-group"><h3>{heading_name}</h3>{articles_html}</div>')
            body = "\n".join(sections)
        else:
            body = "\n".join(
                render_article(
                    a, highlight_words, line_template, show_relative_time=False,
                    show_label_control=True, known_labels_html=known_labels_html,
                    scrap_date=label_scrap_date, scrap_end=run["run_slot"],
                    labels_lookup=labels_lookup,
                )
                for a in articles
            )
    label = html.escape(f"{run['run_slot']} 기준")
    # [수정: 2026-08-26] "발송 완료" 칩 → 점 하나. 담당자들이 이 줄에서 실제로 더 자주
    # 찾는 건 발송 여부보다 "그 회차에 뭘 적어놨는지"(키워드 메모)라, 칩이 그 줄의
    # 무게를 다 가져가면 안 된다는 판단(사용자 지적) — 점으로 낮추고 시각은 title
    # 툴팁으로 옮긴다. 점이 없는 줄 = 여전히 "이 앱을 통해 나간 기록이 없음"이지
    # "미발송"으로 단정하지 않는다(위 .sent-dot CSS 주석과 같은 원칙).
    sent_dot_html = ""
    log_entry = send_log.latest_for_run(f"{run['run_at'][:10]}|{run['run_slot']}")
    log_tip = f"&#10;{html.escape(send_log.tally_text(log_entry))}" if log_entry else ""
    if run.get("send_failure"):
        # [추가: 2026-08-26] 실패(app.storage.record_send_failure가 채운 send_failure)를
        # 성공(send_count)보다 먼저 본다 — 텔레그램이 부분 실패해도 이메일이 전원
        # 성공하면 send_count가 이미 올라 "발송 완료"로 기록되지만(app.confirm_send가
        # 지키는 기존 규칙), 그래도 담당자가 봐야 할 건 "뭔가 안 갔다"는 사실 쪽이다.
        # 그 안에서 성공 여부는 툴팁 첫 줄이 말해준다(_send_failure_tooltip).
        sent_dot_html = _sent_dot_link(run, "sent-dot fail", f"{_send_failure_tooltip(run)}{log_tip}")
    elif run.get("send_count", 0) > 0 and run.get("sent_at"):
        sent_time_kr = html.escape(format_slot_time_kr(run["sent_at"][11:16]))
        sent_tip = (
            f"{sent_time_kr}에 자동발송 완료"
            if run.get("sent_by") == "auto"
            else f"{sent_time_kr} 발송 완료"
        )
        sent_dot_html = _sent_dot_link(run, "sent-dot", f"{sent_tip}{log_tip}")
    # [추가: 2026-08-26] 회차 옆 키워드 메모 — 그 회차에 실제로 저장된 것만(이어받은
    # 값은 화면(초안·확정본)에서만 보이고 여기엔 안 새어든다, app.manual_keyword_note).
    note = load_manual_keyword_note((run["run_at"][:10], run["run_slot"]))
    note_html = (
        f'<span class="slot-note" title="이 회차에 적어둔 키워드&#10;{html.escape(note)}">{html.escape(note)}</span>'
        if note
        else ""
    )
    # [추가: 2026-08-27] AI 분류가 규칙 기반으로 떨어진 회차 표식 — 소제목이 '이를'·'한다'
    # 같은 단어 조각으로 저장된 회차는 나중에 그 회차를 다시 열어봤을 때 "왜 이렇게
    # 이상하지"의 답이 화면 어디에도 없었다. 최신 회차가 아니면 재분류할 방법도 없으므로
    # (큐레이션 핸들러가 전부 load_latest_run 고정), 지금은 고칠 길을 주는 대신 "이 회차는
    # AI가 못 묶은 회차"라는 사실만 정직하게 알린다.
    degraded_html = (
        '<span class="degraded-mark" title="AI가 소제목을 묶지 못해 단어 빈도수로 '
        '기계적으로 지어진 이름입니다.&#10;기사 내용과 무관할 수 있어요.">AI 분류 실패</span>'
        if run_looks_rule_based(run)
        else ""
    )
    export_actions_html = _slot_export_actions_html(run, articles, labels, line_template, subheading_format)
    # [추가: 2026-09-11] 회차 삭제 — 체크박스(선택 삭제 모드에서만 보임)·건수(같음)·줄 끝 🗑.
    # 지우면 안 되는 회차(lock_reason: 오늘·가장 최근)는 🗑를 안 그리고 체크박스를 잠근다.
    key = _slot_key(run["run_at"][:10], run["run_slot"])
    lock_title = f' title="{_LOCK_TIPS[lock_reason]}"' if lock_reason else ""
    pick_html = (
        f'<input type="checkbox" class="pick" aria-label="이 회차 고르기"{" disabled" if lock_reason else ""}'
        f'{lock_title} onchange="onRunPickChange()">'
    )
    count_html = f'<span class="slot-cnt{" zero" if not articles else ""}">{len(articles)}건</span>'
    del_html = (
        ""
        if lock_reason
        else f'<button type="button" class="slot-del" title="이 회차 삭제" data-key="{html.escape(key)}" '
        f'onclick="event.preventDefault(); event.stopPropagation(); deleteRuns([this.dataset.key])">{icon("trash")}</button>'
    )
    # 「✂ 오늘만 여기서 끊기」로 생긴 회차 표식 — 나중에 「이날 왜 15:57 회차가 있지?」의 답
    # (app.today_cuts는 지난 날짜 기록을 지우지 않고 남긴다).
    cut_from = cut_origin(run["run_at"][:10], run["run_slot"])
    cut_html = (
        f'<span class="cut-mark" title="오늘만 끊은 회차 — 원래 {html.escape(cut_from or "")} 회차를 '
        f'{html.escape(run["run_slot"])}에 나눴어요">✂</span>'
        if cut_from is not None
        else ""
    )
    summary_html = (
        f'<summary>{pick_html}<span class="slot-label">{label}</span>{cut_html}{count_html}{sent_dot_html}{degraded_html}'
        f'{note_html}{export_actions_html}{del_html}</summary>'
    )
    open_attr = " open" if open_first else ""
    slot_label = html.escape(f"{_short_date(run['run_at'][:10])} {run['run_slot']} 기준")
    return (
        f'<details class="slot" data-slot-key="{html.escape(key)}" data-count="{len(articles)}" '
        f'data-label="{slot_label}"{open_attr}>{summary_html}{body}</details>'
    )


# [추가: 2026-09-11] 지우면 안 되는 회차의 이유 — app.storage.run_lock_reason과 짝이다.
_LOCK_TIPS = {
    "today": "오늘 회차는 지울 수 없어요 — 지우면 앱이 곧바로 그 회차를 다시 수집해요",
    "latest": "가장 최근 회차는 지울 수 없어요 — 확정본 화면과 자동발송이 이 회차를 보고 있어요",
}


def _slot_key(date_str: str, run_slot: str) -> str:
    """화면에서 회차 하나를 가리키는 키 — 삭제·되살리기 요청이 이 값으로 오간다."""
    return f"{date_str}|{run_slot}"


def _short_date(date_str: str) -> str:
    d = datetime.fromisoformat(date_str)
    return f"{d.month}월 {d.day}일"


def _deleted_when_label(deleted_at: datetime) -> str:
    """「삭제함」 줄의 지운 시각 — 오늘이면 "오늘 10:12", 아니면 "9월 10일 10:12"."""
    hm = deleted_at.strftime("%H:%M")
    if deleted_at.date() == datetime.now().date():
        return f"오늘 {hm}에 삭제"
    return f"{deleted_at.month}월 {deleted_at.day}일 {hm}에 삭제"


def _render_deleted_slot(entry: dict) -> str:
    """담당자가 지운 회차 자리 — 「HH:MM 기준 · 삭제함 · 지운 시각 · 되살리기」 한 줄.

    [추가: 2026-09-11] 「수집 기록 없음」 줄을 대신한다. 지운 회차는 파일째 trash로
    옮겨졌을 뿐이라(app.storage.delete_run) 되살리면 발송 기록까지 그대로 돌아온다.
    수시와 달리 다시 열어도 이 자리에 남는다 — 정기는 빈 자리가 어차피 한 줄로 그려지므로
    그 줄의 문구만 바꾸면 되고, 안 바꾸면 앱이 못 모은 날처럼 보인다.
    """
    key = html.escape(_slot_key(entry["date"], entry["run_slot"]))
    return (
        f'<div class="slot-gone" data-slot-key="{key}">'
        f'<span class="slot-label">{html.escape(entry["run_slot"])} 기준</span>'
        '<span class="gone-tag">삭제함</span>'
        f'<span class="gone-when">{html.escape(_deleted_when_label(entry["deleted_at"]))}</span>'
        f'<button type="button" class="restore-btn" data-trash="{html.escape(entry["trash_name"])}" '
        'onclick="restoreRun(this)">되살리기</button></div>'
    )


_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _date_label(run_date, today) -> str:
    """"8월 21일 (금)" — app.adhoc.archive_renderer._format_date_kr_full과 같은 형식으로
    통일했다(2026-08-21). "오늘" 여부는 별도 칩(.chip-today)이 말하므로 이 문자열 자체에는
    안 섞는다 — 예전엔 "당일 (08-21)"로 오늘만 다른 문구를 썼는데, 오늘이 아닌 날짜도
    요일이 안 보여 "무슨 요일이었는지" 바로 못 읽는 문제가 있었다."""
    return f"{run_date.month}월 {run_date.day}일 ({_WEEKDAY_KR[run_date.weekday()]})"


_DAY_EXPORT_ACTIONS_HTML = (
    '<span class="exp">'
    '<button type="button" onclick="event.stopPropagation(); copyHistoryDayText(this);">복사</button>'
    '<button type="button" onclick="event.stopPropagation(); downloadHistoryDayText(this);">텍스트</button>'
    '<button type="button" class="xls" onclick="event.stopPropagation(); downloadHistoryDayExcel(this);">엑셀</button>'
    "</span>"
)


def _retention_label() -> str:
    """보관 기간을 화면 문구로 — 365일은 "최근 1년"처럼 사람이 읽는 단위로 바꿔 쓴다."""
    if RETENTION_DAYS % 365 == 0:
        years = RETENTION_DAYS // 365
        return "최근 1년" if years == 1 else f"최근 {years}년"
    if RETENTION_DAYS % 30 == 0:
        return f"최근 {RETENTION_DAYS // 30}개월"
    return f"최근 {RETENTION_DAYS}일"


def _theme() -> dict:
    """이 페이지 템플릿이 공유하는 색상·폰트·포트 값. `.format(**_theme(), ...)`로 채운다."""
    return {
        "font_stack": FONT_STACK,
        "cute_font_face": _CUTE_FONT_FACE_CSS,
        "cute_font_name": CUTE_FONT_NAME,
        "settings_host": SETTINGS_SERVER_HOST,
        "settings_port": SETTINGS_SERVER_PORT,
        "live_href": f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/live.html",
        "preview_href": f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/preview.html",
        # [수정: 2026-08-21] 색 값은 전부 app.config.PALETTE 하나에서 온다.
        **PALETTE,
        # [추가: 2026-08-18] 보관 기간을 상수에서 그대로 읽어 제목에 쓴다 — 예전엔
        # "(최근 7일)"이 화면에 박혀 있어 RETENTION_DAYS를 바꿔도 화면이 거짓말을 했다.
        "retention_label": _retention_label(),
        # [추가: 2026-09-16] 🏷 라벨 팝오버 — 확정본·초안과 한 곳에서 온다.
        "label_popover_style": label_popover_style(),
        "label_popover_script": label_popover_script(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT),
        "topnav_style": topnav_style(),
        "topnav_html": regular_nav("archive"),
    }


def _render_day_slots(
    day_runs: list, highlight_words: list, line_template: str, subheading_format: str,
    open_first_slot: bool = False, run_date=None,
    deleted_entries: Optional[list] = None, latest_key: Optional[tuple] = None,
) -> str:
    """하루치 회차들(최신 먼저)의 슬롯 HTML을 만든다 — 지연 로딩(_render_history_day_fragment)과
    즉시 렌더링(render_history_page) 양쪽이 공유한다.

    open_first_slot: 이 날짜의 맨 위(가장 최신) 회차를 펼친 채로 보여줄지 — 화면에서
    가장 먼저 눈에 들어와야 할 오늘/최신 날짜에서만 True로 넘긴다(render_history_page).

    run_date: 이 날짜에 예정돼 있었는데 저장된 회차가 없는 자리를 "수집 기록 없음" 줄로
    함께 그린다([추가: 2026-09-03], expected_slots_for_date). None이면 예전처럼 실제
    회차만 그린다(호출부 호환).

    deleted_entries: [추가: 2026-09-11] 이 날짜에서 담당자가 지운 회차(app.storage.
    list_deleted_runs의 항목). 그 자리는 「수집 기록 없음」 대신 「삭제함 · 되살리기」다.
    latest_key: app.storage.latest_run_key() — 지우면 안 되는 "가장 최근 회차"를 가린다.
    """
    now = datetime.now()
    if latest_key is None:
        # 호출부가 안 넘겼다고 "가장 최근 회차"를 모른 척하면 그 회차에 🗑가 그려진다 —
        # 틀리는 방향이 "지울 수 있는 걸 못 지운다"여야지 그 반대면 안 된다.
        latest_key = latest_run_key()
    entries = [
        (
            r["run_slot"],
            _render_slot(r, highlight_words, line_template, subheading_format,
                         open_first=(open_first_slot and i == 0),
                         lock_reason=run_lock_reason(r["run_at"][:10], r["run_slot"], now=now,
                                                     latest_key=latest_key or ("", ""))),
        )
        for i, r in enumerate(day_runs)
    ]
    collected = {r["run_slot"] for r in day_runs}
    for entry in deleted_entries or []:
        if entry["run_slot"] not in collected:
            entries.append((entry["run_slot"], _render_deleted_slot(entry)))
            collected.add(entry["run_slot"])
    if run_date is not None:
        entries += [
            (slot, _render_missing_slot(slot))
            for slot in expected_slots_for_date(run_date)
            if slot not in collected
        ]
    # 이 화면 전체 규칙대로 최신 먼저 — 빈 줄도 제 시각 자리에 섞여야 "몇 시가 비었나"가
    # 읽힌다(뒤에 몰아두면 그냥 목록 끝의 장식이 된다).
    entries.sort(key=lambda e: e[0], reverse=True)
    return "\n".join(entry_html for _, entry_html in entries)


def _group_runs_by_date(runs: list) -> "OrderedDict":
    grouped: "OrderedDict" = OrderedDict()
    for run in runs:
        run_date = datetime.fromisoformat(run["run_at"]).date()
        grouped.setdefault(run_date, []).append(run)
    # [수정: 2026-08-12] 날짜(바깥) 레벨은 이미 최신 먼저인데 회차(안쪽) 레벨만 오래된
    # 순이라 방향이 엇갈려 있었다 — 최신이 궁금하다는 사용자 피드백에 맞춰 안쪽도
    # 최신 먼저로 통일한다(맨 위 회차가 곧 그날의 최신 시간대).
    return OrderedDict(
        (d, sorted(grouped[d], key=lambda r: r["run_slot"], reverse=True)) for d in sorted(grouped, reverse=True)
    )


def _period_and_result_html(total_days: int, empty_count: int = 0) -> str:
    """기간 조회 바 + 결과 요약 줄 — app.adhoc.archive_renderer의 같은 UI(.period-bar/
    .result-bar/.exp)와 같은 모양이지만 서버 왕복이 없다: 정기는 날짜 목록이 이미 전부
    DOM에 있으므로(위 CSS 주석 참고) 프리셋·직접입력 둘 다 클라이언트 JS
    (applyHistoryPeriodFilter)가 data-date만 보고 날짜 블록을 숨기고 보여준다. 초기
    상태는 "전체 기간"이라 아무것도 숨기지 않는다.

    [추가: 2026-09-11] 결과 줄 오른쪽 [선택 삭제] — 수시 보관함과 같은 자리·같은 모양.
    empty_count: 지울 수 있는 0건 회차 수. 있을 때만 [빈 회차(0건) 모두 고르기]를 그린다.
    """
    empty_btn = (
        f'<button type="button" class="pick-empty" onclick="pickEmptyRuns()">빈 회차(0건) 모두 고르기 ({empty_count})</button>'
        if empty_count
        else ""
    )
    return f"""<div class="period-bar">
  <div class="prow">
    <b class="jlab">기간</b>
    <div class="seg" id="hist-period-seg">
      <button type="button" class="seg-btn" data-unit="day" onclick="setHistoryPeriodPreset('day')">1일</button>
      <button type="button" class="seg-btn" data-unit="week" onclick="setHistoryPeriodPreset('week')">7일</button>
      <button type="button" class="seg-btn" data-unit="month" onclick="setHistoryPeriodPreset('month')">1개월</button>
      <button type="button" class="seg-btn on" data-unit="all" onclick="setHistoryPeriodPreset('all')">전체</button>
    </div>
    <span class="pdiv" aria-hidden="true"></span>
    <input type="date" id="hist-period-start">
    <span class="tilde">~</span>
    <input type="date" id="hist-period-end">
    <button type="button" class="go" onclick="applyHistoryCustomRange()">조회</button>
  </div>
</div>
<div class="result-bar">
  <p class="result-line" id="hist-result-line">전체 기간 · <b id="hist-result-count">{total_days}</b>일</p>
  <button type="button" class="tidy-btn normal-only" onclick="setHistoryTidy(true)">선택 삭제</button>
  <span class="tidy-bar tidy-only"><span>지울 회차를 고르세요</span>{empty_btn}
    <button type="button" class="done-btn" onclick="setHistoryTidy(false)">완료</button></span>
  <span class="exp lg">
    <button type="button" onclick="copyHistoryAllText(this)">복사</button>
    <button type="button" onclick="downloadHistoryAllText()">텍스트</button>
    <button type="button" class="xls" onclick="downloadHistoryAllExcel()">엑셀</button>
  </span>
</div>
<div class="sel-bar" id="hist-selbar">
  <span>회차 <b id="hist-sel-n">0</b>개 · 기사 <b id="hist-sel-a">0</b>건 골랐어요</span><span class="sp"></span>
  <button type="button" class="clr" onclick="clearRunPicks()">선택 해제</button>
  <button type="button" class="del" onclick="deletePickedRuns()">{icon("trash")}<span><span id="hist-sel-n2">0</span>개 삭제</span></button>
</div>"""


def _with_gap_dates(dates_with_runs: list) -> list:
    """회차가 **하나도** 없는 날짜도 목록에 끼워 넣는다(최신 먼저).

    [추가: 2026-09-03] 이 목록은 원래 list_run_dates() — "회차가 있는 날짜"라서, 앱이
    하루 종일 꺼져 있던 날은 날짜 줄 자체가 안 그려졌다. 실측(2026-08-19~09-02)에서
    누락 14회차 중 6회차가 그런 날(8/29·8/30 주말)이었는데, 화면에는 8/28 다음이 곧바로
    8/31이라 **이틀이 통째로 사라진 것을 알 방법이 없었다.**

    범위는 저장된 회차의 가장 오래된 날짜~가장 최신 날짜 사이로만 잡는다 — 그 바깥은
    앱을 쓰기 전이거나 보관 기간이 지나 지워진 구간이라, 빈 줄을 그리면 "안 돈 날"처럼
    보여 거짓말이 된다.
    """
    if len(dates_with_runs) < 2:
        return dates_with_runs
    newest, oldest = dates_with_runs[0], dates_with_runs[-1]
    filled, day = [], newest
    while day >= oldest:
        filled.append(day)
        day -= timedelta(days=1)
    return filled


def _day_pick_html(run_date, today, live_runs_known: bool) -> str:
    """날짜 줄의 체크박스(선택 삭제 모드에서만 보임) — 「이 날 회차 전부 고르기」.

    오늘은 회차가 전부 잠겨(지우면 곧바로 다시 수집된다) 고를 게 없으므로 자리만 둔다.
    살아 있는 회차가 없는 날(전부 지웠거나 수집 기록이 없는 날)도 마찬가지다.
    """
    if run_date == today or not live_runs_known:
        return '<span class="pick-sp"></span>'
    return (
        '<input type="checkbox" class="pick" aria-label="이 날 회차 전부 고르기" '
        'onclick="event.stopPropagation()" onchange="pickDay(this)">'
    )


def _day_lock_note_html(run_date, today, latest_key: Optional[tuple]) -> str:
    """선택 삭제 모드에서만 보이는 한 줄 — 이 날에 지울 수 없는 회차가 있으면 왜인지 말한다.
    체크박스를 흐리게 잠가두기만 하면 "왜 안 눌리지?"가 남는다(툴팁은 hover에만 보인다)."""
    if run_date == today:
        return '<span class="lock-note">오늘 회차는 지울 수 없어요 — 지우면 앱이 곧바로 다시 수집해요</span>'
    if latest_key and latest_key[0] == run_date.isoformat():
        return (
            f'<span class="lock-note">가장 최근 회차({html.escape(latest_key[1] or "")})는 지울 수 없어요 — '
            "확정본 화면과 자동발송이 보고 있어요</span>"
        )
    return ""


def _day_delete_targets(metas: list, latest_key: Optional[tuple], now: Optional[datetime] = None) -> dict:
    """날짜 줄 🗑가 지울 회차 — {날짜: {"keys": [회차 키…], "count": 기사 수}}.

    [추가: 2026-09-15] 인덱스만 본다(파일 읽기 0회). 지연 로딩이라 날짜 줄 🗑를 누르는 순간
    그 날의 회차 줄이 화면에 아직 없을 수 있어서, 서버가 목록을 구워 넣는다([빈 회차(0건)
    모두 고르기]와 같은 이유). 지우면 안 되는 회차(오늘·가장 최근 — run_lock_reason)는 뺀다.
    """
    now = now or datetime.now()
    targets: dict = {}
    for m in metas:
        date_str = m["run_at"][:10]
        if run_lock_reason(date_str, m["run_slot"], now=now, latest_key=latest_key) is not None:
            continue
        t = targets.setdefault(date_str, {"keys": [], "count": 0})
        t["keys"].append(_slot_key(date_str, m["run_slot"]))
        t["count"] += m.get("article_count") or 0
    return targets


def _day_delete_html(date_str: str, target: Optional[dict], latest_key: Optional[tuple]) -> str:
    """날짜 줄 끝 🗑 — 「이 날 회차 모두 삭제」, 확인창 한 번(수시 보관함 사안 줄 🗑와 같은 짝).
    지울 회차가 없으면(오늘, 또는 가장 최근 회차 하나뿐인 날) 안 그린다. 그 날에 가장 최근
    회차가 있으면 남는다는 사실을 확인창에 적도록 시각을 싣는다(data-keep)."""
    if not target or not target["keys"]:
        return ""
    keep = latest_key[1] if latest_key and latest_key[0] == date_str else ""
    return (
        f'<button type="button" class="day-del" title="이 날 회차 모두 삭제" '
        f'data-keys="{html.escape(json.dumps(target["keys"]))}" data-count="{target["count"]}" '
        f'data-keep="{html.escape(keep or "")}" '
        f'onclick="event.preventDefault(); event.stopPropagation(); deleteDay(this)">{icon("trash")}</button>'
    )


def _day_gone_mark_html(restore_all: bool) -> str:
    """날짜 줄이 통째로 「삭제함」일 때의 표시 — 점선 칩 + (지운 회차가 둘 이상이면)
    「모두 되살리기」. details.date.all-gone일 때만 보인다(날짜 줄 🗑로 지운 직후엔 화면이
    그 클래스를 붙이고, 다시 열면 서버가 붙여 그린다)."""
    restore_html = (
        '<button type="button" class="restore-btn" onclick="event.preventDefault(); '
        'event.stopPropagation(); restoreDay(this)">모두 되살리기</button>'
        if restore_all
        else ""
    )
    return f'<span class="day-gone-mark"><span class="chip chip-gone">삭제함</span>{restore_html}</span>'


def render_history_page(
    runs: list,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
    subheading_format: Optional[str] = None,
    all_dates: Optional[list] = None,
    deleted_runs: Optional[list] = None,
    latest_key: Optional[tuple] = None,
    empty_keys: Optional[list] = None,
    day_delete: Optional[dict] = None,
) -> str:
    """저장된 회차를 날짜별(내림차순) -> 시간대별(오름차순) 토글로 렌더링한다 (PRD.md 기능2 규칙 6).

    all_dates: 화면에 줄을 세울 **전체 날짜** 목록("YYYY-MM-DD" 최신순). runs에는 그중
    앞쪽 EAGER_HISTORY_DAYS일치 회차만 담겨 오면 된다 — 나머지 날짜는 어차피 껍데기만
    그리고 펼칠 때 불러오므로 기사를 읽어올 이유가 없다(2026-08-18 성능 정리).
    None이면 예전처럼 runs에서 날짜를 뽑는다(호출부 호환).

    highlight_words: 형광펜 단어(규칙6) — 검색 키워드와 무관한 별도 설정.
    line_template: 기사 첫 줄 형식(규칙5) — 메인 화면과 동일한 설정을 공유한다.
    subheading_format: 메일머지 소제목 형식 — 마찬가지로 메인 화면과 동일한 설정을 공유한다.

    [추가: 2026-09-11] 회차 삭제 — deleted_runs(app.storage.list_deleted_runs)는 그 자리에
    「삭제함 · 되살리기」로, latest_key(app.storage.latest_run_key)는 지우면 안 되는 "가장
    최근 회차"를 가리는 데, empty_keys는 [빈 회차(0건) 모두 고르기]에 쓴다. 안 넘기면
    직접 구한다(empty_keys만 빈 목록 — 그 버튼은 generate_history_page가 그릴 때만 필요하다).
    지운 회차만 남은 날짜도 날짜 줄이 그대로 남도록 날짜 목록에 합친다.

    [수정: 2026-08-12] 용량 다이어트 — 최근 EAGER_HISTORY_DAYS(2)일치만 슬롯 내용을 이
    파일에 바로 담고, 그보다 오래된 날짜는 <summary>(날짜 라벨)만 두고 내용은 비워둔다.
    담당자가 그 날짜를 실제로 펼칠 때만 app.settings_server의 /history/day가 그 하루치를
    새로 렌더링해 내려준다(ontoggle="loadHistoryDay(this)"). 대부분의 조회가 최근 날짜에
    몰리는데도 7일 전부를 매번 통째로 내려받던 것(실측 3.4MB)을 이렇게 줄인다.
    """
    highlight_words = highlight_words if highlight_words is not None else DEFAULT_HIGHLIGHT_KEYWORDS
    line_template = line_template if line_template is not None else DEFAULT_ARTICLE_LINE_TEMPLATE
    subheading_format = subheading_format if subheading_format is not None else DEFAULT_SUBHEADING_FORMAT_TEMPLATE
    deleted_runs = list_deleted_runs() if deleted_runs is None else deleted_runs
    latest_key = latest_run_key() if latest_key is None else latest_key
    empty_keys = empty_keys or []
    day_delete = _day_delete_targets(list_run_meta(), latest_key) if day_delete is None else day_delete

    deleted_by_date: dict = {}
    for entry in deleted_runs:
        deleted_by_date.setdefault(entry["date"], []).append(entry)

    grouped = _group_runs_by_date(runs)
    if all_dates is None:
        ordered_dates = list(grouped)
    else:
        ordered_dates = [datetime.fromisoformat(d).date() for d in all_dates]
    # [추가: 2026-09-03] 회차가 하나도 없는 날짜를 여기서 끼워 넣는다. 그 날짜는 아래에서
    # 지연 로딩(ontoggle)을 붙이면 안 된다 — 서버가 돌려줄 회차가 없어 영영 "펼치면
    # 불러옵니다…"에 머문다. 대신 "수집 기록 없음" 줄을 그 자리에서 바로 그린다.
    dates_with_runs = set(ordered_dates)
    ordered_dates = sorted(
        dates_with_runs | {datetime.fromisoformat(d).date() for d in deleted_by_date}, reverse=True
    )
    ordered_dates = _with_gap_dates(ordered_dates)

    if not ordered_dates:
        return _PAGE_TEMPLATE.format(
            body='<p class="empty">아직 저장된 지난 기사가 없습니다.</p>',
            period_and_result_html="",
            empty_run_keys_json="[]",
            **_theme(),
        )

    today = datetime.now().date()

    sections = []
    for day_index, run_date in enumerate(ordered_dates):
        date_str = run_date.isoformat()
        deleted_here = deleted_by_date.get(date_str, [])
        date_label = html.escape(_date_label(run_date, today))
        # [추가: 2026-08-21] "오늘" 여부는 날짜 문자열에 안 섞고 별도 칩으로 — 문구는
        # 며칠이 지나도 그대로고, 칩만 그날 하루 붙었다 떨어진다.
        today_chip_html = '<span class="chip chip-today">오늘</span>' if run_date == today else ""
        # 날짜·회차는 모두 접힌 채로 시작한다 — 담당자가 고른 것만 펼친다.
        open_attr = ""
        has_live = run_date in dates_with_runs
        head = (
            f'{_day_pick_html(run_date, today, has_live)}<span class="d">{date_label}</span>{today_chip_html}'
            f'{_day_lock_note_html(run_date, today, latest_key)}'
        )
        if not has_live:
            # 내보낼 것이 없으므로 복사/txt/엑셀 버튼은 안 붙인다(눌러도 빈 파일이 나온다).
            rows_html = _render_day_slots(
                [], highlight_words, line_template, subheading_format,
                run_date=run_date, deleted_entries=deleted_here, latest_key=latest_key,
            )
            if not rows_html:
                continue  # 그날 예정된 회차가 아예 없었다면(스케줄 밖) 줄을 만들지 않는다.
            if deleted_here:
                chip = _day_gone_mark_html(restore_all=len(deleted_here) > 1)
                gone_cls = " all-gone"
            else:
                chip = '<span class="chip chip-missing">회차 없음</span>'
                gone_cls = ""
            sections.append(
                f'<details class="date{gone_cls}" data-date="{date_str}"{open_attr}>'
                f"<summary>{head}{chip}</summary>{rows_html}</details>"
            )
            continue
        # [추가: 2026-09-15] 날짜 줄 🗑 + (그걸로 통째로 지웠을 때 보일) 「삭제함 · 모두 되살리기」.
        summary_inner = (
            f"{head}{_DAY_EXPORT_ACTIONS_HTML}{_day_delete_html(date_str, day_delete.get(date_str), latest_key)}"
            f"{_day_gone_mark_html(restore_all=True)}"
        )
        # 기사를 미리 읽어온 날짜만 즉시 렌더링한다 — all_dates를 받은 경우 앞쪽
        # EAGER_HISTORY_DAYS일치만 grouped에 들어 있고, 나머지는 자연히 지연 로딩으로 간다.
        if day_index < EAGER_HISTORY_DAYS and run_date in grouped:
            slots_html = _render_day_slots(
                grouped[run_date], highlight_words, line_template, subheading_format,
                run_date=run_date,
                deleted_entries=deleted_here, latest_key=latest_key,
            )
            # [추가: 2026-08-18] data-date는 지연 로딩 날짜만 갖고 있었는데, 엑셀 내보내기가
            # "지금 펼친 날짜"를 이 속성으로 찾으므로 즉시 렌더링된 날짜에도 필요하다.
            # ontoggle="loadHistoryDay(this)"는 안 붙인다 — 이 블록은 이미 실제 내용이
            # 있어(.lazy-day-holder가 없음) 붙여도 무해하지만(함수 자체가 그 요소를 못 찾으면
            # 조용히 return) 불필요한 재요청 여지를 아예 안 만드는 쪽을 택했다.
            sections.append(
                f'<details class="date" data-date="{date_str}"{open_attr}>'
                f'<summary>{summary_inner}</summary>{slots_html}</details>'
            )
        else:
            sections.append(
                f'<details class="date" data-date="{date_str}"{open_attr} ontoggle="loadHistoryDay(this)">'
                f'<summary>{summary_inner}</summary>'
                '<div class="lazy-day-holder"><p class="lazy-day-hint">펼치면 불러옵니다…</p></div>'
                "</details>"
            )

    return _PAGE_TEMPLATE.format(
        body="\n".join(sections),
        period_and_result_html=_period_and_result_html(len(ordered_dates), len(empty_keys)),
        empty_run_keys_json=json.dumps(empty_keys),
        **_theme(),
    )


def _display_settings() -> tuple:
    """(형광펜 단어, 기사 줄 형식, 소제목 형식) — 화면 조각을 따로 그릴 때 쓴다."""
    settings = load_settings()
    return (
        settings["highlight_keywords"],
        settings["article_line_template"],
        settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE),
    )


def render_history_day_fragment(
    date_str: str,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
    subheading_format: Optional[str] = None,
) -> Optional[str]:
    """하루치 회차만 slots_html로 렌더링한다 — /history/day가 지연 로딩용으로 호출한다.

    render_history_page의 즉시-렌더링 분기와 완전히 같은 함수(_render_day_slots)를 쓰므로
    나중에 펼쳐 봐도 처음부터 펼쳐져 있던 날짜와 100% 같은 모양이 나온다. 그 날짜에
    저장된 회차가 하나도 없으면(보관 기간이 막 지났거나 잘못된 날짜) None을 돌려준다.
    """
    highlight_words = highlight_words if highlight_words is not None else DEFAULT_HIGHLIGHT_KEYWORDS
    line_template = line_template if line_template is not None else DEFAULT_ARTICLE_LINE_TEMPLATE
    subheading_format = subheading_format if subheading_format is not None else DEFAULT_SUBHEADING_FORMAT_TEMPLATE

    # [수정: 2026-08-18] 전체 회차를 파싱한 뒤 걸러내던 것을 날짜로 먼저 좁힌다.
    day_runs = list_runs_for_date(date_str)
    if not day_runs:
        return None
    day_runs = sorted(day_runs, key=lambda r: r["run_slot"], reverse=True)
    # run_date를 넘겨야 "수집 기록 없음" 줄이 즉시-렌더링된 날짜와 똑같이 나온다 —
    # 이 함수의 존재 이유(두 경로가 100% 같은 모양) 그대로다. 지운 회차(「삭제함」 줄)도 같다.
    return _render_day_slots(
        day_runs, highlight_words, line_template, subheading_format,
        run_date=datetime.fromisoformat(date_str).date(),
        deleted_entries=[e for e in list_deleted_runs() if e["date"] == date_str],
    )


def render_slot_fragment(date_str: str, run_slot: str) -> Optional[str]:
    """회차 한 줄(펼치기 전 모양)만 — 되살리기 직후 「삭제함」 줄을 이것으로 갈아 끼운다.
    즉시·지연 렌더링과 같은 _render_slot을 쓰므로 모양이 어긋나지 않는다."""
    run = next((r for r in list_runs_for_date(date_str) if r["run_slot"] == run_slot), None)
    if run is None:
        return None
    highlight_words, line_template, subheading_format = _display_settings()
    return _render_slot(
        run, highlight_words, line_template, subheading_format,
        lock_reason=run_lock_reason(date_str, run_slot),
    )


def render_deleted_slot_fragment(date_str: str, run_slot: str) -> Optional[str]:
    """「삭제함 · 되살리기」 한 줄만 — 지운 직후 그 회차 줄을 이것으로 갈아 끼운다."""
    entry = next(
        (e for e in list_deleted_runs() if e["date"] == date_str and e["run_slot"] == run_slot), None
    )
    return _render_deleted_slot(entry) if entry else None


def generate_history_page(
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
    subheading_format: Optional[str] = None,
) -> Path:
    """저장된 모든 회차(보관 정책 RETENTION_DAYS와 연동)를 history.html로 렌더링한다."""
    if highlight_words is None or line_template is None or subheading_format is None:
        settings = load_settings()
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
        subheading_format = (
            subheading_format
            if subheading_format is not None
            else settings.get("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE)
        )
    # [수정: 2026-08-18] 큐레이션 동작마다(_regenerate_screens) 불리는 자리다 — 전체
    # 회차를 읽지 않고, 날짜 목록은 인덱스에서(파일 읽기 0회) 가져오고 실제 기사는
    # 즉시 렌더링할 최근 EAGER_HISTORY_DAYS일치만 읽는다.
    metas = list_run_meta()  # 인덱스 조회는 이 화면을 그리는 동안 딱 한 번만
    all_dates = list_run_dates(metas)
    eager_runs = [
        run
        for date_str in all_dates[:EAGER_HISTORY_DAYS]
        for run in list_runs_for_date(date_str, metas)
    ]
    # [추가: 2026-09-11] [빈 회차(0건) 모두 고르기] 대상 — 인덱스의 기사 수만 본다(파일 읽기
    # 0회). 지연 로딩이라 화면에 아직 없는 날짜의 회차도 있으므로 서버가 목록을 구워 넣는다.
    latest_key = latest_run_key(metas)
    now = datetime.now()
    empty_keys = [
        _slot_key(m["run_at"][:10], m["run_slot"])
        for m in metas
        if m.get("article_count") == 0
        and run_lock_reason(m["run_at"][:10], m["run_slot"], now=now, latest_key=latest_key) is None
    ]
    html_text = render_history_page(
        eager_runs, highlight_words, line_template, subheading_format, all_dates=all_dates,
        deleted_runs=list_deleted_runs(), latest_key=latest_key, empty_keys=empty_keys,
        day_delete=_day_delete_targets(metas, latest_key, now=now),
    )
    atomic_write_text(HISTORY_HTML_PATH, html_text)
    return HISTORY_HTML_PATH
