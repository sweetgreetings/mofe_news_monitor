# Design Ref: archive/DESIGN.md §0 진입 화면 (home.html — 앱이 브라우저로 맨 처음 여는 화면), PRD.md 기능3
#
# [수정: 2026-08-26] 홈 화면 재구성 — 예전엔 "실시간/정기/수시/라벨 4갈래 흐름도 +
# 워드클라우드"가 전부였다(워드클라우드는 2026-10-01에 없앴다 — HISTORY.md "빈도로 뽑은
# 단어"). 그 위에 "오늘 무슨 일이 있었나"를 답하는 층을 새로 얹었다:
# 오늘 건수·[단독]/[속보], 담당자가 고른 정책 단어의 7~30일 추이, 오늘의 주요 주제 순위.
# 배경·시행착오는 HISTORY.md "홈 화면 재구성" 참고. 핵심 설계 원칙 셋:
#   1) 전부 "정기 수집" 기준이다 — 수시 카드는 조건이 카드마다 달라 정기와 같은 잣대로
#      못 잰다(화면에도 그렇게 밝힌다). 흐름도의 수시 칸에서만 별도로 보여준다.
#   2) 새로 도는 API 호출이 없다 — 이미 저장된 회차 파일(app.storage)과 캐시 파일만
#      읽어 세고 정렬한다. LLM도, 네이버 검색도 이 화면 때문에 도는 일은 없다.
#   3) 담당자가 명시적으로 고른 것만 지켜본다(정책 단어) — 건수 자동 TOP3나 급등
#      자동 채택은 안 쓴다. 이유는 app.home_trend 모듈 docstring 참고.
import html
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.config import (
    AGENCY_ARCHIVE_TITLE,
    SHAPE_TOKENS_CSS,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    FONT_STACK,
    LANDING_HTML_PATH,
    LOGO_PATH,
    PALETTE,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.alerted_urls import alerted_items, already_alerted_urls
from app.credentials import llm_is_configured, naver_is_configured
from app.curation import display_group_name, filter_hidden, load_group_labels
from app.filters import is_schedule_listing
from app.llm_classifier import ETC_GROUP_NAME, round_id_for_run
from app.home_trend import load_trend_words
from app.icons import icon
from app.settings import load_settings
from app.storage import list_run_dates, list_runs_for_date, load_today_runs
from app.trend_chart import chart_css, render_chart
from app.trend_data import (
    articles_by_date_range,
    build_buckets,
    fill_bucket_status,
    last_complete_day,
    today_partial_column,
    word_hits,
)
from app.negative_guess import load_state as load_negative_guess

# [추가: 2026-08-18] home.html이 app.adhoc.card를 참조한다 — app/adhoc/* → app/* 단방향
# 의존 규칙(ADHOC_DESIGN.md)의 첫 예외다. 수시 모니터링을 개발 중엔 분리해뒀지만
# 이번 화면 재구성은 애초에 "결국 하나로 합쳐질 기능"이라는 전제로 설계됐다(사용자
# 결정) — home은 앱 전체의 진입점이라 정기·수시 어느 한쪽에 속하지 않는 상위 레이어다.
from app.adhoc import card as adhoc_card

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>재경부 AI 뉴스 모니터링</title>
<style>
  {shape_tokens}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: {bg}; color: {text}; font-family: {font_stack};
  }}
  /* [수정: 2026-09-11] 640 → 960. 확정본·초안·설정(800)보다 넓은 유일한 화면이다 —
     목록이 아니라 대시보드라, 오늘의 주요 주제 ∥ 정책 단어 추이를 한 줄에 두려면 두 칸
     안쪽이 각각 410px대는 돼야 주제 이름·칩·막대가 한 줄에 들어온다(800이면 330px대). */
  .page {{ max-width: 960px; margin: 0 auto; padding: 24px 20px 40px; }}
  /* 주제(왼쪽) ∥ 추이(오른쪽). 두 카드 밑선을 맞추고, 좁은 창에선 주제가 위로 쌓인다. */
  .pair {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 13px; margin-bottom: 13px; align-items: stretch; }}
  .pair > .card {{ margin-bottom: 0; }}
  @media (max-width: 800px) {{ .pair {{ grid-template-columns: minmax(0, 1fr); }} }}
  /* [수정: 2026-09-18] 가운데 큰 로고·제목 → 왼쪽 한 줄 머리(HOME_HEADER_COMPACT_MOCKUP.html
     B안). 로고 그림에 「재정경제부」가 이미 있어 제목은 「AI 뉴스 모니터링」만. ⚙ 설정은
     머리 오른쪽 끝(흐름도 카드의 「화면 바로 가기」 제목 줄은 없앴다). */
  .hd {{ display: flex; align-items: center; gap: 14px; margin: 0 0 14px; padding: 2px 2px 0; }}
  .hd .logo {{ height: 34px; width: auto; flex-shrink: 0; display: block; }}
  .hd .ttl {{ display: flex; flex-direction: column; gap: 1px; min-width: 0; }}
  .hd .logo + .ttl {{ padding-left: 14px; border-left: 1px solid {border}; }}
  h1 {{ font-size: 1.05rem; font-weight: 600; margin: 0; color: {header}; }}
  .today {{ color: {muted}; font-size: var(--fs-xs); margin: 0; }}
  .hd .gearlink {{ align-self: center; }}
  @media (max-width: 480px) {{ .hd {{ flex-wrap: wrap; }}
    .hd .logo + .ttl {{ padding-left: 0; border-left: 0; }} }}
  .card {{ background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    padding: 17px 20px; margin-bottom: 13px; }}
  .card h2 {{ font-size: var(--fs-sm); font-weight: 700; color: {muted}; margin: 0 0 13px;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  /* 흐름도 카드는 칸이 크고 줄이 많아 다른 카드보다 안쪽 여백을 넉넉히 */
  .flow-card {{ padding: 24px 26px; }}
  .card h2 .sub {{ font-weight: 500; opacity: 0.8; }}

  /* ── 남색 카드: 부정 추정 기사 + [단독]/[속보] (시안 mockups/HOME_NEGATIVE_GUESS_MOCKUP.html) ──
     한 줄: 「부정 추정 기사 N건 +M ▾ · HH:MM 회차까지」 │ [단독]/[속보]. 예전의 흰 건수 줄
     (오늘 N건 · 어제 같은 시각 대비)과 매시 논조 전광판은 없앴다. */
  .board {{ position: relative; background: {board_bg}; border-color: {board_bg}; color: {board_text};
    padding: 10px 16px; font-size: var(--fs-sm); }}
  .bd-row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; row-gap: 6px; }}
  .bd-lab {{ color: {board_neg}; font-weight: 700; font-size: var(--fs-md); }}
  .bd-wait {{ opacity: .85; }}
  .bd-wait a {{ color: {board_new}; }}
  .bd-slot {{ opacity: .7; font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }}
  details.ngbox {{ flex-shrink: 0; position: static; }}
  .ng-sum {{ display: inline-flex; align-items: center; gap: 5px; list-style: none; cursor: pointer;
    padding: 3px 6px; margin-left: -6px; border-radius: var(--r-sm); font-variant-numeric: tabular-nums; }}
  .ng-sum::-webkit-details-marker {{ display: none; }}
  .ng-sum:hover, .ngbox[open] > .ng-sum {{ background: {board_hover}; }}
  .ng-sum .cnt {{ font-weight: 600; opacity: .85; }}
  .ng-sum .dl {{ color: {board_new}; font-size: var(--fs-xs); font-weight: 700; }}
  .ng-sum .caret {{ font-size: var(--fs-xs); opacity: .7; }}
  .bd-right {{ margin-left: auto; display: inline-flex; align-items: center; gap: 8px; }}
  .bd-rule {{ width: 1px; height: 18px; background: {board_rule}; }}
  .board .hl.scoop {{ color: {board_scoop}; }}
  .board .hl.flash {{ color: {board_neg}; }}
  .board .hl.legacy {{ color: {board_text}; }}
  .board .hl:hover, .board .hlbox[open] > .hl {{ background: {board_hover}; }}
  .board .hllist {{ color: {text}; }}
  /* 부정 추정 기사 목록 — 단독/속보 목록과 같은 틀, 폭은 카드 전체 */
  .nglist {{ position: absolute; left: 0; right: 0; top: calc(100% + 6px); z-index: 6; background: {card};
    border: 1px solid {border}; border-radius: var(--r-lg); box-shadow: var(--sh-pop);
    color: {text}; font-size: var(--fs-sm); }}
  .nglist::before {{ content: ""; position: absolute; left: 0; right: 0; top: -8px; height: 8px; }}
  .ng-head {{ padding: 10px 14px 6px; font-size: var(--fs-xs); color: {muted}; border-bottom: 1px solid {row_hover}; }}
  .ng-head .c-neg {{ color: {error}; }}
  .nglist ul {{ list-style: none; margin: 0; padding: 4px 4px 6px; max-height: 380px; overflow-y: auto; }}
  li.ng {{ display: grid; grid-template-columns: 40px minmax(0,1fr) auto; column-gap: 10px; padding: 7px 10px;
    border-radius: var(--r-md); align-items: baseline; }}
  li.ng:hover {{ background: {row_hover}; }}
  .ng .ht {{ color: {muted}; font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }}
  .ng .tt {{ min-width: 0; }}
  .ng .o {{ color: {muted}; font-size: .86em; font-weight: 500; margin-right: .5em; }}
  .ng .t {{ color: {text}; text-decoration: none; }}
  .ng .t:hover {{ color: {accent}; text-decoration: underline; }}
  .ng .nw {{ font-size: var(--fs-xs); font-weight: 700; color: {board_new_chip_text}; background: {board_new_chip_bg};
    border-radius: var(--r-sm); padding: 1px 5px; margin-left: 6px; }}
  .ng .why {{ grid-column: 2 / 3; font-size: var(--fs-xs); color: {muted}; margin-top: 2px; }}
  .ng .why::before {{ content: "근거 · "; opacity: .75; }}
  .ng .acts {{ display: inline-flex; gap: 12px; white-space: nowrap; }}
  .ng .acts button {{ font: inherit; font-size: var(--fs-sm); color: {muted}; background: none; border: 0;
    padding: 0; cursor: pointer; text-decoration: none; }}
  .ng .acts button:hover {{ color: {accent}; text-decoration: underline; }}
  .ng .acts button.done {{ color: {accent}; text-decoration: none; }}
  @media (max-width: 640px) {{
    .bd-right {{ margin-left: 0; flex-basis: 100%; }}
    .bd-rule {{ display: none; }}
    .bd-right .hlset .hl:first-child {{ margin-left: -7px; }}
    li.ng {{ grid-template-columns: 36px minmax(0,1fr); }}
    .ng .acts {{ grid-column: 2 / 3; margin-top: 4px; }}
  }}
  .hlset {{ display: flex; gap: 2px; flex-wrap: wrap; }}
  .hl {{ display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-sm);
    font-weight: 700; line-height: 1.2; padding: 4px 7px; border-radius: var(--r-sm);
    cursor: default; list-style: none; }}
  .hl::-webkit-details-marker {{ display: none; }}
  .hl .ic {{ margin-right: -1px; }}
  .hl .n {{ font-variant-numeric: tabular-nums; font-weight: 600; opacity: 0.75; }}
  .hl.scoop {{ color: {scoop_text}; }}
  .hl.flash {{ color: {error}; }}
  .hl.legacy {{ color: {muted}; }}
  .hl:hover, .hlbox[open] > .hl {{ background: {row_hover}; }}
  .hlbox[open] > .hl .n {{ opacity: 1; }}
  .hllist {{ list-style: none; margin: 0; padding: 8px 4px; position: absolute;
    right: 0; top: calc(100% + 6px); z-index: 5; background: {card};
    border: 1px solid {border}; border-radius: var(--r-lg);
    box-shadow: var(--sh-pop); max-width: 100%; }}
  /* 칩과 목록 사이 6px 틈을 지나가는 동안 닫히지 않도록 하는 투명 다리 */
  .hllist::before {{ content: ""; position: absolute; left: 0; right: 0; top: -8px; height: 8px; }}
  .hllist li {{ display: flex; align-items: baseline; gap: 8px; padding: 6px 12px;
    font-size: var(--fs-sm); border-radius: var(--r-md); }}
  .hllist li:hover {{ background: {hover}; }}
  .hllist .ht {{ color: {muted}; font-size: var(--fs-xs); font-variant-numeric: tabular-nums; flex-shrink: 0; }}
  .hllist .ho {{ color: {muted}; font-size: var(--fs-xs); min-width: 56px; flex-shrink: 0; }}
  .hllist a {{ color: {text}; text-decoration: none; }}
  .hllist a:hover {{ color: {accent}; text-decoration: underline; }}
  .hllist .hl-copy {{ margin-left: auto; padding-left: 10px; flex-shrink: 0; font: inherit; font-size: var(--fs-sm);
    color: {muted}; background: none; border: 0; cursor: pointer; }}
  .hllist .hl-copy:hover {{ color: {accent}; text-decoration: underline; }}
  .hllist .hl-copy.done {{ color: {accent}; text-decoration: none; }}
  details.hlbox {{ position: static; }}

  /* ── 정책 단어 추이 (그래프 본체는 app.trend_chart.chart_css()가 {trend_chart_css}로 주입) ── */
  .trend-more {{ margin-left: auto; font-size: var(--fs-sm); color: {accent}; font-weight: 700;
    text-decoration: none; padding: 3px 8px; border-radius: var(--r-md); }}
  .trend-more:hover {{ background: {hover}; }}
  .trend-chart-link {{ display: block; text-decoration: none; color: inherit; }}
  .empty-trend {{ font-size: var(--fs-sm); color: {muted}; padding: 6px 0 2px; line-height: 1.7; }}
  .empty-trend a {{ color: {accent}; font-weight: 600; text-decoration: none; }}
  .empty-trend a:hover {{ text-decoration: underline; }}
  {trend_chart_css}

  /* ── 오늘의 주요 주제 ── */
  .irow {{ border-bottom: 1px solid {border}; padding: 10px 0; }}
  .irow:last-of-type {{ border-bottom: none; }}
  .rowhead {{ display: flex; align-items: center; gap: 9px; }}
  .rank {{ width: 19px; height: 19px; border-radius: var(--r-circle); background: {hover}; color: {accent};
    font-size: var(--fs-xs); font-weight: 700; flex-shrink: 0; display: flex; align-items: center;
    justify-content: center; }}
  /* [수정: 2026-09-11] 이름+칩을 한 상자로 — 반쪽 칸에서 이름이 길면 칩만 아랫줄로
     내려가고 막대는 오른쪽 자리를 지킨다. */
  .ibox {{ flex: 1; min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 3px 8px; }}
  .iname {{ font-size: var(--fs-md); font-weight: 600; color: {header}; }}
  .chips {{ display: flex; gap: 4px; flex-wrap: wrap; }}
  .gchip {{ font-size: var(--fs-xs); font-weight: 600; color: {muted}; border: 1px solid {border};
    border-radius: var(--r-pill); padding: 1px 7px; white-space: nowrap; }}
  /* [수정: 2026-09-02] 건수 숫자 → 막대. 1위를 100%로 둔 상대 길이라 모수를
     설명할 필요가 없다(그래서 부제·기타 줄을 통째로 지울 수 있었다). 정확한
     건수는 title 툴팁으로만 남긴다. */
  .ibar {{ margin-left: auto; width: 64px; height: 5px; border-radius: var(--r-sm);
    background: {border}; flex-shrink: 0; overflow: hidden; }}
  .ibar i {{ display: block; height: 100%; background: {accent}; opacity: 0.55;
    border-radius: var(--r-sm); }}
  .empty-issues {{ font-size: var(--fs-md); color: {muted}; padding: 8px 0; }}

  /* ── 흐름도 ── */
  /* [수정: 2026-09-15] 칸 이름만 크게 + 마우스를 올리면 말풍선(HOME_FLOW_CLEAN_MOCKUP.html
     D안). 건수 줄·줄 제목·화살표 설명을 칸에서 빼고 말풍선으로 옮겼다 — 오늘 몇 건
     모였는지는 바로 위 건수 줄이 이미 말한다.
     같은 날 2차(HOME_FLOW_ALIGN_MOCKUP.html B+D): 실시간을 정기 줄 맨 앞으로 옮겨 4칸 격자
     (실시간 → 초안 → 확정본 → 정기 보관함)로 만들고, 수시의 수집 원본은 두 칸 폭(.span2)을
     쓴다 — 수시엔 실시간 화면이 따로 없고 원본이 실시간·초안 자리를 겸하기 때문이다(CLAUDE.md
     정기↔수시 대응표). 실시간이 정기에만 이어진다는 걸 배치가 말한다. 칸 글자는 가운데 정렬. */
  /* [수정: 2026-09-15, 3차] 수시 4단 흐름(HOME_FLOW_ADHOC4_MOCKUP.html D안, 사용자 결정).
     두 줄이 4:4 — 줄 맨 앞은 조건을 정하는 좁은 「입구」 칸(🔍 검색어 / 🔎 새 수집), 그 뒤
     정기 초안·확정본·정기 보관함 ∥ 수시 원본·확정본·수시 보관함. 실시간은 흐름의 한 단계가
     아니라(초안은 정기 검색이 직접 채운다) 초안 위에 매단 낮은 점선 칸 + 「📌 담아두기」
     점선으로만 잇는다. 원본이 두 칸 폭이던 .span2는 없어졌다. 격자 셋(.fl-top·.fl-conn·
     본문)이 같은 칸 폭을 써서 위아래로 정확히 줄 선다. */
  /* [추가: 2026-09-15, 4차] 두 줄 왼쪽에 줄 이름표 「정기」「수시」(HOME_FLOW_ROWLABEL_MOCKUP.html
     B안, 사용자 결정 — 단어만). 그 전엔 정기·수시라는 말이 보관함 칸 이름에만 있어 두 줄이 색만
     다른 똑같은 줄로 읽혔다. 이름표가 줄을 말하므로 보관함 칸은 둘 다 「보관함」이다. 이름표는
     누르는 곳이 아니라 칸 모양 없이 글자 + 왼쪽 띠만 둔다. 격자 앞에 이름표 열·10px 틈 두 칸이
     붙어 입구 칸이 3열이 됐다(.side·.conn도 5열). */
  .fl {{ display: grid; grid-template-columns: 48px 10px minmax(0, .72fr) 26px minmax(0, 1fr) 26px
    minmax(0, 1fr) 26px minmax(0, 1fr); align-items: stretch; row-gap: 14px; }}
  .fl .gap {{ grid-column: 1 / -1; height: 4px; }}
  .fl .side {{ grid-column: 5; }}
  .fl .rl {{ grid-column: 1; display: flex; align-items: center; padding-left: 10px;
    border-left: 3px solid; font-size: var(--fs-md); font-weight: 800; white-space: nowrap; }}
  .fl .rl.reg {{ border-color: {flow_row_reg_bar}; color: {header}; }}
  .fl .rl.ad {{ border-color: {flow_row_adhoc_bar}; color: {adhoc_text}; }}
  .fl .tile.t-regkw, .fl .tile.t-adnew {{ grid-column: 3; }}
  .t-live.slim {{ min-height: 42px; background: {card}; border: 1px dashed {flow_live_side_border}; }}
  .t-live.slim .nm {{ font-size: var(--fs-md); }}
  .t-live.slim:hover {{ background: {live_bg}; border-style: solid; }}
  .fl-conn .conn {{ grid-column: 5; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 0; height: 28px; font-size: var(--fs-xs); font-weight: 600; color: {text_faint};
    position: relative; }}
  /* 「담아두기」 글자는 점선 오른쪽 — 실시간 → 초안 선이 끊기지 않고 곧게 내려온다
     (HOME_FLOW_ROOMY_MOCKUP.html C안) */
  .fl-conn .conn .cl {{ position: absolute; left: calc(50% + 10px); top: 50%;
    transform: translateY(-50%); white-space: nowrap; }}
  .fl-conn .conn::before {{ content: ""; height: 22px; border-left: 2px dotted {flow_live_side_border}; }}
  .fl-conn .conn::after {{ content: ""; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {flow_live_side_border}; }}
  /* .tile이 이 아래에서 테두리를 transparent로 다시 정하므로 두 클래스로 명시도를 올린다 */
  .tile.t-regkw {{ background: {card}; border: 1px dashed {flow_entry_reg_border}; color: {arch_reg_text}; }}
  .tile.t-regkw:hover {{ background: {bg}; border-style: solid; }}
  .tile.t-adnew {{ background: {card}; border: 1px dashed {flow_entry_adhoc_border}; color: {adhoc_text}; }}
  .tile.t-adnew:hover {{ background: {arch_adhoc_bg}; border-style: solid; }}
  .tile {{ position: relative; text-decoration: none; display: flex; align-items: center;
    justify-content: center; gap: 10px; padding: 0 12px; min-height: 56px; border-radius: var(--r-lg);
    border: 1px solid transparent;
    transition: background .12s, border-color .12s, transform .12s, box-shadow .12s; }}
  .tile .nm {{ display: flex; align-items: center; gap: 10px; font-size: var(--fs-base); font-weight: 700;
    letter-spacing: -0.01em; white-space: nowrap; }}
  .tile .nm .e {{ font-size: 1.15rem; }}
  /* z-index: 아래 줄 칸(뒤에 그려지는 형제)이 말풍선을 덮지 않게 올린 칸을 위로 */
  .tile:hover, .tile:focus-visible {{ transform: translateY(-1px); z-index: 5; outline: none;
    box-shadow: var(--sh-float); }}
  .fl .ar {{ display: flex; align-items: center; justify-content: center; color: {flow_arrow}; }}
  .fl .ar .ic {{ width: 16px; height: 16px; stroke-width: 2; }}
  /* 지금 손대는 칸은 색을 채우고, 보관함(지난 것)은 흰 바탕 + 테두리로 한 발 물러선다 */
  .t-live {{ background: {live_bg}; color: {error}; }}
  .t-live:hover {{ border-color: {error_border}; }}
  .t-reg {{ background: {accent_tonal}; color: {header}; }}
  .t-reg.draft {{ color: {accent}; }}
  .t-reg:hover {{ border-color: {accent_border}; }}
  .t-regarch {{ background: {card}; border-color: {border}; color: {arch_reg_text}; }}
  .t-regarch:hover {{ background: {bg}; border-color: {accent_border}; }}
  .t-ad {{ background: {adhoc_bg}; color: {adhoc_text}; }}
  .t-ad:hover {{ border-color: {adhoc_border_soft}; }}
  .t-adarch {{ background: {card}; border-color: {adhoc_border_soft}; color: {adhoc_text}; }}
  .t-adarch:hover {{ background: {arch_adhoc_bg}; }}
  /* 두 칸은 흐름의 단계가 아니라 두 흐름을 가로지르는 형제라 한 줄에 반씩 선다.
     가르는 자리는 6열(위 줄들의 「초안 → 확정본」 화살표 칸)이라 틈이 세로로 줄 맞는다. */
  .t-lab {{ grid-column: 1 / 6; background: {label_bg}; color: {label_text}; }}
  .t-agency {{ grid-column: 7 / -1; background: {card}; border-color: {border}; color: {arch_reg_text}; }}
  .t-agency:hover {{ background: {bg}; border-color: {ghost_border_hover}; }}
  .t-lab:hover {{ border-color: {label_border}; }}
  /* 말풍선 — 칸 가운데 아래에 뜬다. 칸 위를 스치듯 지나갈 때 툭툭 뜨지 않게 0.15초 뒤에
     연다([단독]/[속보] 칩과 같은 이유). 안에 누를 것이 없어 pointer-events를 끈다. */
  .tip {{ position: absolute; top: calc(100% + 8px); left: 50%; z-index: 10; width: 280px;
    max-width: calc(100vw - 40px); background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    box-shadow: var(--sh-pop); padding: 11px 14px 12px;
    display: flex; flex-direction: column; gap: 4px; text-align: left; white-space: normal;
    opacity: 0; visibility: hidden; transform: translate(-50%, -4px); pointer-events: none;
    transition: opacity .12s, transform .12s, visibility 0s linear .12s; }}
  .tip::before {{ content: ""; position: absolute; top: -6px; left: 50%; margin-left: -5px;
    width: 10px; height: 10px; background: {card}; border-left: 1px solid {border};
    border-top: 1px solid {border}; transform: rotate(45deg); }}
  .tile:hover .tip, .tile:focus-visible .tip {{ opacity: 1; visibility: visible;
    transform: translate(-50%, 0);
    transition: opacity .12s .15s, transform .12s .15s, visibility 0s .15s; }}
  .tip b {{ font-size: var(--fs-md); font-weight: 700; color: inherit; }}
  .tip .tb {{ font-size: var(--fs-sm); font-weight: 500; color: {muted}; line-height: 1.6; }}
  .tip .nx {{ display: flex; align-items: center; gap: 6px; margin-top: 3px; padding-top: 7px;
    border-top: 1px solid {border}; font-size: var(--fs-xs); font-weight: 600; color: {muted}; }}
  .tip .nx i {{ font-style: normal; color: {flow_arrow}; }}
  /* 창이 페이지 폭(960)보다 좁으면 칸이 줄어 맨 왼쪽·오른쪽 칸의 가운데 말풍선이 창 밖으로
     삐져나간다(숨어 있어도 가로 스크롤이 생긴다) — 그때만 칸 끝에 맞춘다. */
  @media (max-width: 1000px) {{
    .tip.l {{ left: 0; transform: translateY(-4px); }}
    .tip.r {{ left: auto; right: 0; transform: translateY(-4px); }}
    .tile:hover .tip.l, .tile:hover .tip.r,
    .tile:focus-visible .tip.l, .tile:focus-visible .tip.r {{ transform: none; }}
    .tip.l::before {{ left: 28px; margin-left: 0; }}
    .tip.r::before {{ left: auto; right: 28px; margin-left: 0; }}
  }}
  /* 계정 · ⚙ 설정 — 페이지 머리(.hd) 오른쪽 끝. 둘이 **같은 모양**이라 한 규칙을 쓴다. */
  .hd-right {{ margin-left: auto; display: inline-flex; align-items: center; gap: 8px; position: relative; }}
  .gearlink, .acctbtn {{ font-size: var(--fs-sm); font-weight: 500; color: {muted};
    text-decoration: none; background: {card}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 4px 11px; display: inline-flex; align-items: center; gap: 5px;
    cursor: pointer; font-family: inherit; }}
  .gearlink:hover, .acctbtn:hover {{ background: {hover}; }}
  .acctbtn .nm {{ color: {text}; }}
  .acctbtn .chev {{ font-size: var(--fs-xs); color: {text_faint}; }}
  .acctbtn.is-open {{ background: {hover}; border-color: {accent_border}; color: {accent}; }}

  /* 계정 메뉴 — 담는 틀이라 큰 네모(--r-lg) + 팝오버 그림자. */
  .acct-pop {{ position: absolute; top: calc(100% + 6px); right: 0; width: 248px; z-index: 10;
    background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    box-shadow: var(--sh-pop); padding: 6px; }}
  .acct-who {{ padding: 10px 10px 11px; border-bottom: 1px solid {divider_soft}; margin-bottom: 5px; }}
  .acct-who .nm {{ font-size: var(--fs-base); color: {header}; font-weight: 600; }}
  .acct-who .dept {{ font-size: var(--fs-sm); color: {muted}; margin-left: 5px; }}
  .acct-who .mail {{ font-size: var(--fs-sm); color: {text_faint}; margin-top: 2px; }}
  .acct-pop form {{ margin: 0; }}
  .acct-pop a, .acct-pop button {{ display: flex; align-items: center; gap: 8px; width: 100%;
    box-sizing: border-box; padding: 8px 10px; border-radius: var(--r-md);
    font-size: var(--fs-md); color: {text}; text-decoration: none; background: none;
    border: none; font-family: inherit; cursor: pointer; text-align: left; }}
  .acct-pop a:hover, .acct-pop button:hover {{ background: {row_hover}; }}
  .acct-pop .state {{ margin-left: auto; font-size: var(--fs-xs); color: {muted};
    background: {pill_bg}; border: 1px solid {border}; border-radius: var(--r-pill); padding: 1px 8px; }}
  .acct-pop .state.on {{ color: {ok_text}; background: {ok_bg}; border-color: {ok_border}; }}
  .acct-pop .sep {{ border-top: 1px solid {divider_soft}; margin: 5px 0; }}
  .acct-pop .out {{ color: {error}; }}
  .acct-pop .out:hover {{ background: {error_bg}; }}
  /* 좁은 화면에선 칸이 한 줄씩 쌓이고 이름표는 그 줄 칸들 위의 머리글로 눕는다(띠 → 밑줄).
     실시간은 격자(.fl-top)가 맨 위라 그대로 쌓으면 「정기」 머리글보다 위에 와 어느 줄에도 안
     속한 것처럼 읽힌다 — 격자 셋을 display: contents로 풀어 한 줄 흐름으로 만든 뒤 order로
     「정기」 → 실시간 → 나머지 순서를 잡는다. */
  @media (max-width: 640px) {{
    .flow {{ display: flex; flex-direction: column; gap: 8px; }}
    .flow > .fl {{ display: contents; }}
    .flow > .fl-conn, .fl .ar, .fl .blank {{ display: none; }}
    .fl .rl {{ order: 0; border-left: 0; border-bottom: 2px solid; padding: 10px 0 4px; }}
    .fl .rl.reg {{ order: -2; border-color: {flow_row_reg_bar}; }}
    .fl .rl.ad {{ margin-top: 8px; border-color: {flow_row_adhoc_bar}; }}
    .fl-top .tile {{ order: -1; }}
  }}

  .contact {{ color: {muted}; font-size: var(--fs-md); text-align: center; margin-top: 22px; }}
  .contact a {{ color: {accent}; }}
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
</style>
</head>
<body>
<div class="page">
  <header class="hd">{logo_html}<div class="ttl"><h1>AI 뉴스 모니터링</h1><p class="today">{today_label}</p></div><div class="hd-right">{account_html}{gear_html}</div></header>

  {board_html}
  <div class="pair">
    {issues_html}
    {trend_html}
  </div>
  {flow_html}

  <p class="contact">문의 {mail_icon} <a href="mailto:sweetgreetings@naver.com">sweetgreetings@naver.com</a></p>
</div>
<script>
{alert_hover_js}
{account_js}
{board_js}
</script>
</body>
</html>
"""
_BASE = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}"


def _account_html() -> str:
    """홈 머리 오른쪽의 계정 버튼과 그 메뉴 — **이름 칸을 비운 채로** 굽는다.

    홈은 파일로 한 벌만 만들어 두는 화면이라 여기에 이름을 박으면 사람마다 다른
    값을 줄 수 없고, 스케줄러가 다시 구울 때 남의 이름이 남는다. 그래서 화면이
    열릴 때 `/whoami`에 물어 채운다(`_ACCOUNT_JS`). 못 물어보면 「계정」인 채로
    남고 링크는 그대로 돈다 — 주소가 설정 서버 절대주소라 file://로 열려도 간다.
    """
    return (
        '<button class="acctbtn" id="acct-btn" type="button" aria-haspopup="true"'
        f' aria-expanded="false" data-base="{_BASE}">'
        f'{icon("user")}<span class="nm" id="acct-name">계정</span>'
        '<span class="chev" id="acct-chev" hidden>▾</span></button>'
        '<div class="acct-pop" id="acct-pop" hidden>'
        '<div class="acct-who"><div><span class="nm" id="acct-who-name"></span>'
        '<span class="dept" id="acct-who-dept"></span></div>'
        '<div class="mail" id="acct-who-mail"></div></div>'
        f'<a href="{_BASE}/me">{icon("user")} 나의 정보 관리</a>'
        f'<a href="{_BASE}/login-settings">{icon("lock")} 로그인 사용'
        '<span class="state" id="acct-lock"></span></a>'
        '<div class="sep"></div>'
        f'<form method="POST" action="{_BASE}/logout">'
        f'<button class="out" type="submit">{icon("logout")} 로그아웃</button></form>'
        "</div>"
    )


_ACCOUNT_JS = """
// 계정 버튼 — 「지금 누구인가」만 서버에 묻고, 나머지 링크는 구워진 그대로 쓴다.
// 주소는 버튼의 data-base에서 읽는다(JS 안에 박으면 홈을 구울 때마다 문자열이 섞인다).
(function () {
  var btn = document.getElementById("acct-btn");
  var pop = document.getElementById("acct-pop");
  if (!btn || !pop) return;
  var BASE = btn.dataset.base || "";
  var state = { logged_in: false, has_accounts: false };

  function open(v) {
    pop.hidden = !v;
    btn.classList.toggle("is-open", v);
    btn.setAttribute("aria-expanded", v ? "true" : "false");
  }

  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    // 로그인 전에는 메뉴를 열 게 없다 — 누르면 바로 그 화면으로 보낸다.
    if (!state.logged_in) {
      location.href = BASE + (state.has_accounts ? "/login" : "/signup");
      return;
    }
    open(pop.hidden);
  });
  document.addEventListener("click", function (e) {
    if (!pop.hidden && !pop.contains(e.target)) open(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") open(false);
  });

  fetch(BASE + "/whoami", { credentials: "include" })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      state = d;
      if (!d.logged_in) {
        document.getElementById("acct-name").textContent =
          d.has_accounts ? "로그인" : "계정 만들기";
        return;
      }
      document.getElementById("acct-name").textContent = d.name || "계정";
      document.getElementById("acct-chev").hidden = false;
      document.getElementById("acct-who-name").textContent = d.name || "";
      document.getElementById("acct-who-dept").textContent = d.department || "";
      document.getElementById("acct-who-mail").textContent = d.email || "";
      var lock = document.getElementById("acct-lock");
      lock.textContent = d.login_required ? "켜짐" : "꺼짐";
      lock.classList.toggle("on", !!d.login_required);
    })
    .catch(function () { /* 물어볼 수 없으면 「계정」인 채로 둔다 */ });
})();
"""


def _render_logo() -> str:
    """LOGO_PATH 파일이 있으면 보여주고, 없으면 자리를 아예 만들지 않는다 (깨진 이미지 아이콘 방지)."""
    if LOGO_PATH.exists():
        return f'<img class="logo" src="{LOGO_PATH.name}" alt="로고">'
    return ""


def _todays_adhoc_state() -> dict:
    """오늘 만든 수시 카드 요약 — 흐름도의 「수집 원본」·「수집 확정본」 두 칸이 쓴다.

    [수정: 2026-08-26, 동작은 그대로 · 비용만 줄임] 카드 id가 `YYYYMMDD-HHMMSS-hex`라
    파일명 접두사로 오늘 것만 골라 연다 — `app.adhoc.card.list_cards()`/`cards_for_date()`는
    저장된 카드 **전부**를 여는데(실측 365개 1,415ms, app.adhoc.card.latest_card_id
    참고), 홈은 매 요청마다 다시 그려지는 화면이라 그 비용을 얹을 수 없다. href를
    고르는 기준("가장 최근에 만든 것", PRD.md 기능9 규칙12)은 예전 `_latest_open_
    adhoc_card_href`와 동일하다 — 이번 변경은 여는 파일 수를 줄일 뿐이다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    compact = today.replace("-", "")
    cards = []
    for stem in sorted((p.stem for p in adhoc_card.CARDS_DIR.glob(f"{compact}-*.json")), reverse=True):
        try:
            data = json.loads((adhoc_card.CARDS_DIR / f"{stem}.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("collect_date") == today and data.get("status") != "archived":
            cards.append(data)
    # [수정: 2026-09-03] 수시 흐름이 「수집 원본 → 수집 확정본」 두 칸으로 갈리면서
    # 각각의 목적지를 따로 돌려준다. 종류를 안 가르면 흐름도의 두 칸이 같은 링크를
    # 가리켜 서로를 설명하지 못한다.
    # [수정: 2026-09-15] 카드 수 대신 **링크가 여는 그 카드**를 돌려준다 — 말풍선이
    # "누르면 무엇이 열리나"(사안명·시간창·검색어)를 말하고, 「오늘 N건」(실은 카드 수였다)은
    # 흐름도에서 뺐다.
    collects = [c for c in cards if (c.get("kind") or "collect") == "collect"]
    bundles = [c for c in cards if (c.get("kind") or "collect") == "bundle"]
    return {
        "collect": collects[0] if collects else None,
        "href": f"/adhoc/card?id={collects[0]['id']}" if collects else "/adhoc/new",
        "bundle": bundles[0] if bundles else None,
        # [수정: 2026-09-15] 확정본이 아직 없으면 그걸 만드는 곳(오늘 원본, 그것도 없으면 새
        # 수집)으로 보낸다 — 확정본은 원본에서 처음 보낼 때 저절로 생긴다.
        "bundle_href": (
            f"/adhoc/card?id={bundles[0]['id']}" if bundles
            else f"/adhoc/card?id={collects[0]['id']}" if collects
            else "/adhoc/new"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
# 남색 카드: 부정 추정 기사 + [단독]/[속보]
# ══════════════════════════════════════════════════════════════════════════
# 판정은 app.negative_guess(스케줄러가 회차마다 한 번)가 하고, 여기선 그 파일과 오늘 회차만
# 읽는다 — 홈은 API를 부르지 않는다. 예전 흰 건수 줄은 없앴고, 거기 있던 [단독]/[속보] 칩이
# 이 카드 오른쪽으로 옮겨 왔다(시안 mockups/HOME_NEGATIVE_GUESS_MOCKUP.html).

def _copy_text(item: dict, template: str) -> str:
    """홈 목록의 「복사」 — 보고서와 같은 한 줄(article_line_template) + 다음 줄 URL."""
    url = item.get("url") or ""
    return template.format(outlet=item.get("outlet") or "", title=item.get("title") or url) + "\n" + url


def _ng_rows(items: list, new_slot: Optional[str], template: str) -> str:
    rows = []
    for it in items:
        url = it.get("url") or ""
        outlet = it.get("outlet") or ""
        title = it.get("title") or url
        copy_text = _copy_text(it, template)
        is_new = ' <span class="nw">new</span>' if new_slot and it.get("run_slot") == new_slot else ""
        reason = it.get("reason") or ""
        why = f'<span class="why">{html.escape(reason)}</span>' if reason else ""
        rows.append(
            f'<li class="ng"><span class="ht">{html.escape((it.get("pub_date") or "")[11:16] or "--:--")}</span>'
            f'<span class="tt"><span class="o">{html.escape(outlet)}</span>'
            f'<a class="t" href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(title)}</a>'
            f"{is_new}</span>"
            # 원문은 제목 링크로 충분해 따로 안 둔다(사용자 결정) — 복사만.
            f'<span class="acts"><button type="button" class="ng-copy" data-copy="{html.escape(copy_text)}">복사</button></span>'
            f"{why}</li>"
        )
    return "".join(rows)


def _board_left_html(settings: dict, runs_today: list, now: datetime) -> str:
    state = load_negative_guess(now.strftime("%Y-%m-%d"))
    lab = '<span class="bd-lab">부정 추정 기사</span>'
    judged_slots = [run["run_slot"] for run in runs_today if run["run_slot"] in state["runs"]]
    if not judged_slots:
        if not llm_is_configured():
            llm_url = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/llm"
            wait = f'<a href="{llm_url}">AI 연동</a>을 하면 회차마다 골라 둡니다'
        elif not runs_today:
            wait = "첫 회차 뒤에 표시"
        else:
            wait = "판정 중"
        return f'{lab}<span class="bd-wait">{wait}</span>'

    latest = judged_slots[-1]
    slot_html = f'<span class="bd-slot">{html.escape(latest)} 회차까지</span>'
    items = filter_hidden(list(state["items"].values()))
    items.sort(key=lambda it: it.get("pub_date") or "", reverse=True)
    if not items:
        return f'<span class="bd-lab">부정 추정 기사 없음</span>{slot_html}'

    total = sum(r.get("count", 0) for r in state["runs"].values())
    tip = (f"정기 회차에 모인 기사 중 제목·요약에 「재경부」·「재정경제부」가 나온 {total}건을 AI가 읽고, "
           "재경부나 그 정책을 비판·우려·지적한다고 추정한 기사입니다. 회차가 끝날 때마다 새로 더해집니다.")
    # 그날 첫 회차는 전부 새로라 +N·new가 뜻이 없다 — 앞 회차가 있을 때만 단다.
    new_slot = latest if len(judged_slots) > 1 else None
    new_n = sum(1 for it in items if new_slot and it.get("run_slot") == new_slot)
    delta = f' <span class="dl" title="{html.escape(latest)} 회차에서 새로 더해진 기사">+{new_n}</span>' if new_n else ""
    template = settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)
    return (
        f'<details class="ngbox"><summary class="ng-sum" title="{html.escape(tip)}">'
        f'<span class="bd-lab">부정 추정 기사 <span class="cnt">{len(items)}건</span></span>{delta}'
        '<span class="caret">▾</span></summary>'
        f'<div class="nglist"><div class="ng-head">정기 기사 중 「재경부」·「재정경제부」가 나온 {total}건에서 '
        f'AI가 <b class="c-neg">부정</b>으로 추정한 {len(items)}건 · 최신순 · 숨긴 기사 제외</div>'
        f"<ul>{_ng_rows(items, new_slot, template)}</ul></div></details>{slot_html}"
    )


def _board_card_html(settings: dict, runs_today: list, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    left = _board_left_html(settings, runs_today, now)
    alerts = _alert_line_html(settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE))
    right = (f'<span class="bd-right"><span class="bd-rule" aria-hidden="true"></span>'
             f'<span class="hlset">{alerts}</span></span>') if alerts else ""
    return f'<div class="card board"><div class="bd-row">{left}{right}</div></div>'


# 부정 추정 기사 목록: 마우스를 올리면 열리고, 누르면 고정된다. 여는·닫는 지연과 "클릭 = 고정"은
# 아래 [단독]/[속보] 칩(_ALERT_HOVER_JS)과 같은 값이고, 둘은 한 번에 하나만 열린다.
_BOARD_JS = """
(function () {
  var box = document.querySelector('details.ngbox');
  if (!box) return;
  var alerts = Array.prototype.slice.call(document.querySelectorAll('details.hlbox'));
  var openT = null, closeT = null;
  function clearT() { clearTimeout(openT); clearTimeout(closeT); }
  function open() { alerts.forEach(function (a) { delete a.dataset.pinned; a.open = false; }); box.open = true; }
  box.addEventListener('mouseenter', function () { clearT(); openT = setTimeout(open, 120); });
  box.addEventListener('mouseleave', function () {
    clearT(); closeT = setTimeout(function () { if (!box.dataset.pinned) box.open = false; }, 220);
  });
  box.querySelector('summary').addEventListener('click', function (e) {
    e.preventDefault(); e.stopPropagation(); clearT();
    if (box.dataset.pinned) { delete box.dataset.pinned; box.open = false; }
    else { box.dataset.pinned = '1'; open(); }
  });
  box.querySelector('.nglist').addEventListener('click', function (e) { e.stopPropagation(); });
  alerts.forEach(function (a) {
    a.addEventListener('mouseenter', function () { if (!box.dataset.pinned) box.open = false; });
  });
  document.addEventListener('click', function () { delete box.dataset.pinned; box.open = false; });
})();
// 「복사」 — 부정 추정 기사·[단독]/[속보] 목록 공용
(function () {
  document.querySelectorAll('.ng-copy').forEach(function (b) {
    b.addEventListener('click', function () {
      var text = b.getAttribute('data-copy');
      function done() {
        b.textContent = '복사됨'; b.classList.add('done');
        setTimeout(function () { b.textContent = '복사'; b.classList.remove('done'); }, 1200);
      }
      function fallback() {
        var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
        ta.select(); document.execCommand('copy'); ta.remove(); done();
      }
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
      else fallback();
    });
  });
})();
"""


def _alert_line_html(template: str = DEFAULT_ARTICLE_LINE_TEMPLATE) -> str:
    """오늘 [단독]/[속보] 알림 표시 — **알림 발송 기록(app.alerted_urls) 하나만** 본다.

    회차 파일에서 말머리를 다시 세면 알림과 숫자가 어긋난다: 알림 폴링(app.
    breaking_alert_sender)은 감시 그룹의 키워드를 넓게 훑고, 정기 회차는
    include_in_scrap·그룹 OR/AND로 좁게 담기 때문이다 — "알림은 갔는데 화면엔
    없는" 기사가 생기면 안 된다(사용자 지적, 2026-08-26). 발송 기록이 곧 표시
    근거이므로 이 둘은 절대 어긋날 수 없다.
    """
    grouped: dict = {}
    for item in alerted_items():
        grouped.setdefault(item.get("kind"), []).append(item)
    if not any(grouped.values()):
        # items가 없는 옛 기록(이 저장 형식 이전에 나간 알림)이면 건수만 안다 —
        # 어느 쪽인지 지어내지 않고 [단독]·[속보] 둘을 나란히 적는다.
        legacy_n = len(already_alerted_urls())
        if not legacy_n:
            return ""
        return (
            f'<span class="hl legacy" title="오늘 알림이 나간 기사 수 — '
            f'제목·언론사·종류는 이 기능 이전 기록이라 남아 있지 않습니다">'
            f'{icon("siren")}[단독]·[속보] <span class="n">{legacy_n}</span></span>'
        )
    parts = []
    for kind, cls in (("단독", "scoop"), ("속보", "flash")):
        items = sorted(grouped.get(kind, []), key=lambda it: it.get("pub_date") or "", reverse=True)
        if not items:
            continue
        lis = "".join(
            f'<li><span class="ht">{html.escape((it.get("pub_date") or "")[11:16] or "--:--")}</span>'
            f'<span class="ho">{html.escape(it.get("outlet") or "")}</span>'
            f'<a href="{html.escape(it["url"])}" target="_blank" rel="noopener">'
            f'{html.escape(it.get("title") or it["url"])}</a>'
            # 부정 추정 기사 목록과 같은 복사 — 보고서 한 줄 형식 + URL(사용자 요청)
            f'<button type="button" class="ng-copy hl-copy" data-copy="{html.escape(_copy_text(it, template))}">복사</button></li>'
            for it in items
        )
        parts.append(
            f'<details class="hlbox"><summary class="hl {cls}">{icon("siren")}'
            f'[{kind}] <span class="n">{len(items)}</span></summary>'
            f'<ul class="hllist">{lis}</ul></details>'
        )
    return "".join(parts)


# ══════════════════════════════════════════════════════════════════════════
# [단독]/[속보] 칩 — 마우스오버로 펼침
# ══════════════════════════════════════════════════════════════════════════
# [추가: 2026-08-27] 예전엔 클릭해야만 열렸다(<details> 기본 동작). 건수 한 줄을
# 확인하는 데 클릭까지 요구하는 건 번거롭다는 사용자 지적으로 마우스오버로 바꿨다.
# <details>를 그대로 두고 open만 직접 제어한다 — 그래서 JS가 죽어도 클릭하면 열리는
# 원래 동작이 남는다(터치 화면도 hover가 없어 자연히 이 경로로 떨어진다).
#   · 여는 지연 120ms — 스트립 위를 스쳐 지나갈 때 툭툭 열리는 걸 막는다.
#   · 닫는 지연 220ms + 칩↔목록 사이 투명 다리(.hllist::before) — 목록으로 마우스를
#     내리는 도중에 닫히지 않는다.
#   · 열려 있는 칩이 있는 상태에서 옆 칩으로 옮기면 즉시 갈아탄다(둘이 같이 뜨지
#     않는다 — 예전엔 각각 열려 두 목록이 겹쳐 떴다).
#   · 클릭은 "고정"이다. 마우스가 벗어나도 안 닫혀서 링크를 여러 개 열 수 있고,
#     다시 클릭하거나 바깥을 누르면 풀린다.
_ALERT_HOVER_JS = """
(function () {
  var OPEN_DELAY = 120, CLOSE_DELAY = 220;
  var boxes = Array.prototype.slice.call(document.querySelectorAll('details.hlbox'));
  if (!boxes.length) return;
  var openT = null, closeT = null;
  function clearT() { clearTimeout(openT); clearTimeout(closeT); }
  function open(b) {
    boxes.forEach(function (o) { if (o !== b && !o.dataset.pinned) o.open = false; });
    b.open = true;
  }
  boxes.forEach(function (b) {
    var chip = b.querySelector('.hl');
    b.addEventListener('mouseenter', function () {
      clearT();
      var anyOpen = boxes.some(function (o) { return o.open; });
      openT = setTimeout(function () { open(b); }, anyOpen ? 0 : OPEN_DELAY);
    });
    b.addEventListener('mouseleave', function () {
      clearT();
      closeT = setTimeout(function () { if (!b.dataset.pinned) b.open = false; }, CLOSE_DELAY);
    });
    chip.addEventListener('click', function (e) {
      e.preventDefault();
      clearT();
      if (b.dataset.pinned) { delete b.dataset.pinned; b.open = false; }
      else {
        boxes.forEach(function (o) { delete o.dataset.pinned; });
        b.dataset.pinned = '1';
        open(b);
      }
    });
    chip.addEventListener('focus', function () { open(b); });
    b.addEventListener('focusout', function (e) {
      if (!b.contains(e.relatedTarget) && !b.dataset.pinned) b.open = false;
    });
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('.hlset')) return;
    boxes.forEach(function (b) { delete b.dataset.pinned; b.open = false; });
  });
})();
"""

# ══════════════════════════════════════════════════════════════════════════
# 정책 단어 추이 (담당자가 고른 단어, 최근 7일 고정)
# ══════════════════════════════════════════════════════════════════════════
# [수정: 2026-08-27] 예전엔 이 카드 안에서 7/14/30일 탭 전환·단어 추가/삭제·급등
# 제안까지 다 처리했다 — 홈은 매 요청마다 다시 그리는 화면이라 "한눈에 훑고 지나가는
# 자리"여야 하는데, 그러기엔 조작할 게 너무 많았다(사용자 판단). 그래서 홈은 최근
# 7일 고정 그래프 + "더보기"뿐으로 줄이고, 단어를 고르고 6개월·1년까지 보는 일은
# 전용 화면(/trend, app.trend_renderer)으로 옮겼다 — 단어 목록 저장소(app.home_trend)는
# 두 화면이 그대로 공유하므로, 전용 화면에서 고른 단어가 홈에도 즉시 반영된다(홈은
# 그 목록을 전부 그린다 — [수정: 2026-09-15] 예전엔 앞 3개만). 급등 제안도 "단어를 실제로
# 고치는 자리"인 전용 화면으로 함께 옮겼다.
#
# [추가: 2026-09-11] 홈 그래프는 반쪽 칸 안쪽 폭 그대로 그린다 — (960 − 좌우 여백 40 −
# 칸 사이 13) ÷ 2 − 카드 테두리·여백 42 ≈ 412. 기본값(760)으로 그리면 SVG가 칸에 맞춰
# 줄어들며 축 글자가 5px대로 작아진다(viewBox 글자 크기가 폭 비율대로 줄기 때문).
_HOME_CHART_WIDTH = 412


def _naver_key_empty_html() -> str:
    """네이버 검색 키가 없어 정기 회차가 한 번도 못 돈 상태 — 주제·추이 두 카드가 같은 문구를 쓴다
    (원인도 할 일도 하나라서, HOME_CARD_SCOPE_MOCKUP.html)."""
    url = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/naver"
    return ('<p class="empty-trend">정기 회차가 돌아야 채워져요.<br>'
            f'<a href="{html.escape(url)}">네이버 검색 키</a>를 먼저 넣어주세요.</p>')


def _trend_card_html(today_str: str, trend_url: str, has_today_run: bool = False) -> str:
    words = load_trend_words()
    more_html = f'<a class="trend-more" href="{html.escape(trend_url)}">더보기</a>'
    if not words:
        return (
            '<div class="card"><h2>정책 단어 추이 <span class="sub">'
            f'정기</span>{more_html}</h2>'
            '<p class="empty-trend">아직 지켜보는 단어가 없습니다.<br>'
            f'<a href="{html.escape(trend_url)}">더보기</a>에서 단어를 골라주세요.</p></div>'
        )

    # 끝은 "하루치가 다 찬 마지막 날"에서 7일 — 낮에는 어제, 오늘 예정 회차가 전부
    # 저장된 저녁부터는 오늘이다(app.trend_data.last_complete_day 참고). 덜 찬 날을 그리면
    # 급락처럼 보였다. 일별이라 점선 칸은 안 생긴다.
    end = last_complete_day(today_str)
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    by_date = articles_by_date_range(start, end)
    buckets, unit = build_buckets(start, end)
    fill_bucket_status(buckets, unit, by_date, today_str)

    # 지난 7일에 정기 회차가 하나도 없으면 빈 그래프 대신 까닭을 말한다 — 키가 없으면
    # 주제 카드와 같은 문구, 키가 있고 오늘 회차가 돌았으면 내일(어제가 차는 날)부터 그려진다.
    if not any(b["record"] for b in buckets):
        if not naver_is_configured():
            empty = _naver_key_empty_html()
        elif has_today_run:
            empty = '<p class="empty-trend">💤 내일부터 그려져요</p>'
        else:
            empty = '<p class="empty-trend">💤 정기 회차가 하루 치 쌓이면 다음 날부터 그려져요</p>'
        return (f'<div class="card"><h2>정책 단어 추이 <span class="sub">정기</span>'
                f'{more_html}</h2>{empty}</div>')

    colors = [PALETTE[f"trend_{i+1}"] for i in range(len(words))]
    series = []
    for word, color in zip(words, colors):
        values = []
        for b in buckets:
            if not b["record"]:
                values.append(None)
                continue
            values.append(sum(word_hits(word, by_date[d] or []) for d in b["dates"]))
        series.append({"word": word, "color": color, "values": values})

    # 낮에는 선 오른쪽에 점선으로 떼어 「오늘(집계 중)」 칸을 하나 더 그린다 — 선에 안
    # 잇고 빈 원만 찍으므로 오전의 덜 찬 값이 급락으로 읽히지 않는다(app.trend_data
    # today_partial_column). 오늘이 다 찬 저녁부터는 None이고 오늘이 마지막 버킷으로 선다.
    # 오른쪽 끝 라벨 숫자는 계속 **다 찬 마지막 날** 값이다(label_today=False) — 홈 카드엔
    # 툴팁이 없어, 라벨에 덜 찬 숫자를 쓰면 그게 하루치처럼 읽힌다.
    chart_html = render_chart("home-trend-chart", buckets, series, unit,
                              width=_HOME_CHART_WIDTH, hover=False,
                              today=today_partial_column(today_str, words),
                              label_today=False)
    # [수정: 2026-09-11] 범례(단어 · 7일 합계)와 각주를 뺐다 — 단어 이름은 그래프 오른쪽
    # 끝 라벨이 이미 말하고, 각주("숨긴 기사 포함 · 옅은 띠는 주말")는 본업 화면이 자기를
    # 해명하는 문장이다. 이 둘 때문에 추이 카드가 주제 카드보다 길어져 주제 밑이 비었다.
    # 합계·각주는 /trend(app.trend_renderer)에 그대로 남는다.
    return (
        # 부제는 실제 범위 — 「정기」가 수시가 안 섞였음을, 끝 날짜가 오늘이 들어갔는지를
        # 설명 문장 없이 말한다(HOME_CARD_SCOPE_MOCKUP.html A안).
        '<div class="card"><h2>정책 단어 추이 <span class="sub">'
        f'정기 · {buckets[0]["wd_label"]} ~ {buckets[-1]["wd_label"]}'
        f'</span>{more_html}</h2>'
        f'<a class="trend-chart-link" href="{html.escape(trend_url)}">'
        f'{chart_html}</a></div>'
    )


# ══════════════════════════════════════════════════════════════════════════
# 오늘의 주요 주제 (정기, "기타"·일정 나열 기사 제외 · 회차마다 갱신)
# ══════════════════════════════════════════════════════════════════════════

_ISSUE_TOP_N = 5
_STREAM_MERGE_THRESHOLD = 0.4  # 실측(2026-08-25 데이터)으로 검증한 문턱 — 아래 문서 참고
_TOKEN_SPLIT_RE = re.compile(r"[^가-힣A-Za-z0-9]+")


def _name_tokens(name: str) -> set:
    return {t for t in _TOKEN_SPLIT_RE.split(name) if len(t) > 1}


def _issue_streams(articles: list) -> list:
    """오늘 하루 여러 회차에 흩어진 소제목을 이름 겹침 기준으로 하나의 "주제"로 묶는다.

    회차마다 소제목을 **새로 분류**하므로(HISTORY.md "담당자 > AI" — carryover 이름
    힌트는 같은 회차를 다시 부를 때만 적용되고, 다른 슬롯으로 넘어가면 끊긴다) 같은
    사건이 회차마다 다른 이름을 받는 게 흔하다. 실측(2026-08-25): "공공기관 2차
    이전 계획"→"공공기관 2차 이전 추진", "2027년 예산안 당정협의"→"2027년 정부예산안
    당정협의" — 이름 완전일치만으로 세면 이런 쌍이 갈라져 순위가 실제보다 잘게 쪼개진다.

    토큰(2글자 이상 한글/영숫자) Jaccard 겹침이 0.4 이상이면 같은 주제로 합친다 —
    2026-08-25 실측 데이터(그날 소제목 39개) 전수로 이 문턱을 확인했고, 오탐(관계
    없는 두 소제목이 잘못 합쳐짐) 없이 정확히 이어져야 할 쌍만 합쳐졌다. **화면
    표시용 집계일 뿐**이고 group_order.json·group_overrides.json 등 저장 데이터는
    전혀 건드리지 않는다 — 다시 불러올 때마다 새로 계산한다.

    "기타"와 통신사 정형 일정 나열 기사([오늘의 주요일정] 류, app.filters.
    is_schedule_listing)는 대상에서 뺀다 — 확정본·수집엔 그대로 남고, 이 순위
    계산에서만 뺀다.

    **묶는 것도 "기타"를 가리는 것도 화면에 보이는 이름(display_group)으로 한다**
    — app.group_order._lock_rank가 "기타는 맨 뒤 고정"을 이름표 적용 후에 판단하는
    것과 같은 기준이다. 저장된 원래 이름으로 보면 두 가지가 한꺼번에 샌다:
    담당자가 직접 만든 "기타"의 속이름("기타 (5)" — 같은 회차에 이미 "기타"가 있어
    app.custom_groups.unique_key가 지은 것)이 정확일치를 빠져나가 짬뽕 칸이 순위에
    올라오고, 회차를 잇는 근거인 이름표를 못 봐서 같은 사안이 갈라진다(실측
    2026-10-12: "대외경제장관회의"+"공급망 안보 및 경제안보품목 확대" 5건·5건으로
    갈라졌던 것이 이름표 "…(지속)"으로 묶여 8건 한 줄).
    """
    streams: list = []
    for a in articles:
        name = a.get("display_group") or a.get("group")
        if not name or name.strip() == ETC_GROUP_NAME or is_schedule_listing(a.get("title", "")):
            continue
        tokens = _name_tokens(name)
        hit = None
        for s in streams:
            union = s["tokens"] | tokens
            if union and len(s["tokens"] & tokens) / len(union) >= _STREAM_MERGE_THRESHOLD:
                hit = s
                break
        if hit is None:
            hit = {"name": name, "tokens": set(tokens), "articles": []}
            streams.append(hit)
        hit["tokens"] |= tokens
        hit["articles"].append(a)
    streams.sort(key=lambda s: -len(s["articles"]))
    return streams


def _dominant_tags(articles: list, kw2g: dict) -> list:
    """이 주제 기사의 과반이 걸린 검색어 그룹만 — 소수 매칭까지 다 붙이면 칩이
    "어디서 왔나"라는 뜻을 잃는다(예: 대법관 인선 기사 5건 중 1건만 예결위에 우연히
    걸렸다고 [예결위] 칩을 달면 오해를 준다)."""
    counts: Counter = Counter()
    for a in articles:
        names = {g for w in (a.get("matched_keywords") or []) for g in kw2g.get(w, ())}
        counts.update(names)
    return [name for name, c in counts.most_common() if c * 2 >= len(articles)]


def _issues_empty_html(settings: dict, last_run: Optional[dict]) -> str:
    """주제가 없을 때 — 왜 비었는지에 따라 문구가 셋이다(HOME_CARD_SCOPE_MOCKUP.html B안).

    회차가 돌았는데 비었으면 기사가 전부 「기타」·일정 나열에 모였다는 뜻이라 담담하게
    「특이사항은 없어보입니다」로 푼다. 회차가 아직 없으면 언제 채워지는지를 말한다.
    """
    if last_run:
        return ('<p class="empty-issues">💤 '
                f'{html.escape(last_run["run_slot"])} 회차까지 특이사항은 없어보입니다.</p>')
    if not naver_is_configured():
        return _naver_key_empty_html()
    # 순환 import 회피 — _flow_card_html과 같은 이유.
    from app.scheduler import next_pending_slot
    from app.settings import active_schedule_times
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot:
        return f'<p class="empty-issues">💤 {html.escape(slot["end"])} 회차가 끝나면 채워져요</p>'
    return '<p class="empty-issues">💤 오늘 정기 회차가 아직 없어요</p>'


def _issues_card_html(settings: dict, articles: list, last_run: Optional[dict] = None) -> str:
    # 부제는 실제 범위 — 회차가 저장될 때마다 끝 시각이 늘어난다(A안).
    sub = f'정기 · {html.escape(last_run["run_slot"])} 회차까지' if last_run else "정기"
    head = f'<div class="card"><h2>오늘의 주요 주제 <span class="sub">{sub}</span></h2>'
    kw2g: dict = {}
    for group in settings.get("keyword_groups", []):
        for kw in group.get("keywords", []):
            word = kw["word"] if isinstance(kw, dict) else kw
            kw2g.setdefault(word, set()).add(group["name"])

    streams = _issue_streams(articles)
    if not streams:
        return head + _issues_empty_html(settings, last_run) + "</div>"

    top = streams[:_ISSUE_TOP_N]
    top_count = len(top[0]["articles"])  # 1위를 100%로 둔 상대 길이
    rows = []
    for i, s in enumerate(top, 1):
        n = len(s["articles"])
        tags = "".join(f'<span class="gchip">{html.escape(t)}</span>' for t in _dominant_tags(s["articles"], kw2g))
        rows.append(
            '<div class="irow"><div class="rowhead">'
            f'<span class="rank">{i}</span>'
            f'<span class="ibox"><span class="iname">{html.escape(s["name"])}</span>'
            f'<span class="chips">{tags}</span></span>'
            f'<span class="ibar" title="{n}건"><i style="width:{round(n * 100 / top_count)}%"></i></span>'
            "</div></div>"
        )
    return head + "".join(rows) + "</div>"


# ══════════════════════════════════════════════════════════════════════════
# 흐름도 — 칸 이름만, 설명은 마우스를 올리면 말풍선으로
# ══════════════════════════════════════════════════════════════════════════
# [수정: 2026-09-15] HOME_FLOW_CLEAN_MOCKUP.html D안(사용자 결정). 칸마다 붙어 있던
# 「오늘 0건」 같은 절대 건수 줄, 줄 제목(`모니터링 · 일자 › 시간대` …), 화살표 밑 설명
# (`회차 마감` …)을 칸에서 빼고 전부 말풍선으로 옮겼다. 말풍선은 세 층이다:
# 굵은 줄 = 지금 상태 / 가운데 = 어떤 곳인지 / 맨 아래 = 다음 단계(예전 화살표 설명).
#
# 말풍선 문구 중 고정된 것은 전부 아래 표 한 곳에 있다 — 문구를 바꿀 땐 여기만 고친다.
# 굵은 줄이 상황마다 달라지는 칸(초안·확정본·원본·수시 확정본)은 _flow_card_html이
# 채우고, 표에는 그 칸의 "상황이 없을 때" 문구만 둔다.
_FLOW_TIPS = {
    # [추가: 2026-09-15] 수시 4단 흐름(D안) — 두 줄 맨 앞의 입구 칸.
    "keywords": ("정기 스크랩 검색어",
                 "켜 둔 그룹은 오늘 전체 기사에, 정기 스크랩을 체크한 그룹은 초안·확정본에도 들어가요.",
                 ("저장", "오늘 전체 기사는 바로 · 정기는 다음 회차부터")),
    "adnew": ("사안·검색어·시간을 정해 모아요",
              "지난 1년 동안 한 수집이 조건째 목록으로 남아 있어, 골라서 다시 쓸 수 있어요.",
              ("수집", "원본")),
    "live": ("거르지 않은 오늘 기사 전부",
             "검색어에 걸린 오늘 기사를 시간순으로 다 보여줘요. 안 봐도 초안은 알아서 모여요.",
             ("📌 담아두기", "초안·확정본")),
    "draft": ("오늘 회차가 모두 끝났어요",
              "마감 전까지 모이는 기사를 미리 정리하는 곳이에요.",
              ("회차 마감", "확정본")),
    "done": ("오늘 마감된 회차가 아직 없어요",
             "마감된 회차의 보고서예요. 여기서 발송해요.",
             ("다음 회차 마감", "정기 보관함")),
    "regarch": ("지난 회차 모아보기",
                "날짜 › 회차별로 1년 동안 쌓여요.",
                None),
    "collect": ("오늘 만든 원본이 없어요",
                "새 수집에서 조건을 정하면 여기에 모여요.",
                ("골라 보내기", "확정본")),
    # [수정: 2026-09-15] 확정본은 원본에서 처음 보낼 때 저절로 생긴다 — 「누르면 새로
    # 만들어요」가 더는 사실이 아니다.
    "bundle": ("오늘 만든 확정본이 없어요",
               "원본에서 기사를 보내면 같은 이름으로 생겨요.",
               ("자정", "수시 보관함")),
    "adarch": ("지난 확정본 모아보기",
               "사안 › 날짜별로 1년 동안 쌓여요. 원본은 새 수집에서 다시 찾아요.",
               None),
    "label": ("라벨 붙인 기사 모아보기",
              "정기·수시를 가리지 않고 모여요. 원래 회차가 지워져도 남아요.",
              None),
    "agency": ("기관 기사를 날짜를 가로질러 찾기",
               "회차가 안 덮는 저녁 시간까지 하루 종일 쌓여요. 보고서가 아니라 찾아보는 곳이라 지우지 않아요.",
               None),
}


def _flow_tile(cls: str, href: str, emoji: str, name: str, head: str, body: str,
               nxt: Optional[tuple] = None, edge: str = "") -> str:
    """흐름도 칸 하나 + 말풍선. head·body는 이미 이스케이프된 HTML이어야 한다(body엔 <br>이 들어간다).

    말풍선은 칸 가운데 아래에 뜬다. edge="l"/"r"은 맨 왼쪽·오른쪽 칸 표시다 — 창이
    페이지 폭보다 좁을 때만 말풍선을 그 칸 끝에 맞춰 창 밖으로 삐져나가지 않게 한다(CSS 쪽).
    """
    nx = (
        f'<span class="nx">{html.escape(nxt[0])} <i>→</i> {html.escape(nxt[1])}</span>'
        if nxt else ""
    )
    return (
        f'<a class="tile {cls}" href="{href}">'
        f'<span class="nm"><span class="e">{emoji}</span>{html.escape(name)}</span>'
        f'<span class="tip{" " + edge if edge else ""}"><b>{head}</b><span class="tb">{body}</span>{nx}</span>'
        "</a>"
    )


def _flow_card_html(last_run: Optional[dict], settings: dict) -> str:
    # 순환 import를 피해 함수 안에서 부른다 — app.scheduler → app.confirm_send →
    # app.landing_renderer 순으로 이미 이 모듈을 import하고 있어서, 맨 위에서 부르면
    # 앱 시작 순서에 따라 next_pending_slot이 아직 정의되기 전에 읽힌다.
    from app.scheduler import next_pending_slot
    from app.settings import active_schedule_times

    base = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}"
    adhoc = _todays_adhoc_state()
    tips = {k: [html.escape(v[0]), html.escape(v[1]), v[2]] for k, v in _FLOW_TIPS.items()}

    # 초안 — 초안 화면(app.preview_renderer)과 같은 함수로 진행 중인 회차를 찾는다.
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot:
        tips["draft"][0] = f'{html.escape(slot["end"])} 회차 진행 중'
        tips["draft"][2] = (f'{slot["end"]} 회차 마감', "확정본")

    # 확정본 — 발송 기록이 있을 때만 「발송 완료」를 붙인다. 기록이 없다고 「미발송」이라
    # 단정하지 않는다(앱 밖에서 처리했을 수 있다 — 정기 보관함과 같은 규칙).
    if last_run:
        sent = " · 발송 완료" if last_run.get("send_count") else ""
        tips["done"][0] = f'{html.escape(last_run["run_slot"])} 회차{sent}'

    # 원본·수시 확정본 — 링크가 여는 그 카드(오늘 가장 최근에 만든 것)를 말한다.
    collect = adhoc["collect"]
    if collect:
        win = adhoc_card.window_of(collect)
        keywords = ", ".join(collect.get("keywords") or [])
        detail = " · ".join(p for p in (f'{win["start"]}~{win["end"]}' if win["start"] else "", keywords) if p)
        # 원본의 화면 이름은 **그 원본이 한 검색**이다(사안명이 아니다) — 말풍선 굵은 줄도
        # 탭·머리줄과 같은 값을 쓴다. 그래서 아랫줄 detail에는 시간창만 남긴다.
        tips["collect"][0] = html.escape(adhoc_card.screen_name(collect) or "(이름 없음)")
        detail = f'{win["start"]}~{win["end"]}' if win["start"] else ""
        tips["collect"][1] = (
            (f"{html.escape(detail)}<br>" if detail else "")
            + "조건에 걸린 기사를 거르지 않고 다 보여줘요. 누르면 이 카드가 열려요."
        )
    bundle = adhoc["bundle"]
    if bundle:
        # 불러올 때마다 같은 이름의 확정본이 새로 생기므로 시각까지 적는다(「인사청문회 15:05」).
        tips["bundle"][0] = html.escape(adhoc_card.bundle_label(bundle) if bundle.get("report_title") else "(이름 없음)")
        tips["bundle"][1] = "원본에서 보낸 기사를 언론사 순으로 정리해요. 소제목·숨기기는 여기서 해요."

    arrow = f'<span class="ar">{icon("flow_next")}</span>'
    # [수정: 2026-09-15, 3차] D안 — 정기 🔍검색어 → ⛏️초안 → 💎확정본 → 🗄️정기 보관함 (실시간은
    # 초안 위 부록) / 수시 🔎새 수집 → 📃원본 → 🗂️확정본 → 📚수시 보관함.
    # [수정: 2026-09-15, 5차] 수시 칸 이름에서 「수집」을 뗐다 — 줄 이름표가 정기와 가르므로
    # 두 줄에서 다른 이름은 「초안 ↔ 원본」 하나뿐이다(마감이 있냐 없냐 = 실제로 다른 지점).
    # [수정: 2026-09-15, 4차] 줄 앞에 이름표 「정기」「수시」 — 줄이 이름을 말하므로 보관함 칸은
    # 둘 다 「보관함」(말풍선의 「→ 정기 보관함」은 그 화면의 실제 제목이라 그대로 둔다).
    return f'''<div class="card flow-card">
    <div class="flow">
    <div class="fl fl-top">
      <span class="blank"></span><span class="blank"></span>
      {_flow_tile("t-live slim side", f"{base}/live.html", "🔴", "오늘 전체 기사", *tips["live"])}
    </div>
    <div class="fl fl-conn"><span></span><span></span><span class="conn"><span class="cl">📌 담아두기</span></span></div>
    <div class="fl">
      <span class="rl reg" title="정해진 시각마다 앱이 알아서 모아요">정기</span>
      {_flow_tile("t-regkw", f"{base}/keywords", "🔍", "검색어", *tips["keywords"], edge="l")}
      {arrow}
      {_flow_tile("t-reg draft", f"{base}/preview.html", "⛏️", "초안", *tips["draft"])}
      {arrow}
      {_flow_tile("t-reg", "index.html", "💎", "확정본", *tips["done"])}
      {arrow}
      {_flow_tile("t-regarch", "history.html", "🪎", "보관함", *tips["regarch"], edge="r")}
      <span class="rl ad" title="사안이 생기면 직접 모아요">수시</span>
      {_flow_tile("t-adnew", f"{base}/adhoc/new", "🔎", "새 수집", *tips["adnew"], edge="l")}
      {arrow}
      {_flow_tile("t-ad", base + adhoc["href"], "🍅", "원본", *tips["collect"])}
      {arrow}
      {_flow_tile("t-ad", base + adhoc["bundle_href"], "🥗", "확정본", *tips["bundle"])}
      {arrow}
      {_flow_tile("t-adarch", f"{base}/adhoc", "🧺", "보관함", *tips["adarch"], edge="r")}
      <div class="gap"></div>
      {_flow_tile("t-lab", f"{base}/labels", "🏷️", "라벨 보관함", *tips["label"])}
      {_flow_tile("t-agency", f"{base}/agency-archive", "🗄️", AGENCY_ARCHIVE_TITLE, *tips["agency"])}
    </div>
    </div>
  </div>'''


def render_landing_page(
    *,
    settings: Optional[dict] = None,
    today_str: Optional[str] = None,
    issue_articles: Optional[list] = None,
    last_run: Optional[dict] = None,
    runs_today: Optional[list] = None,
) -> str:
    """진입 화면 HTML을 렌더링한다 (PRD.md 기능3)."""
    settings = settings or load_settings()
    today_str = today_str or datetime.now().strftime("%Y-%m-%d")
    issue_articles = issue_articles if issue_articles is not None else []
    today_label = _today_label(last_run)

    return _PAGE_TEMPLATE.format(
        font_stack=FONT_STACK,
        shape_tokens=SHAPE_TOKENS_CSS,
        logo_html=_render_logo(),
        gear_html=f'<a class="gearlink" href="http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/">{icon("gear")} 설정</a>',
        account_html=_account_html(),
        account_js=_ACCOUNT_JS,
        today_label=today_label,
        board_html=_board_card_html(settings, runs_today if runs_today is not None else load_today_runs(today_str)),
        trend_html=_trend_card_html(today_str, f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/trend",
                                    has_today_run=last_run is not None),
        trend_chart_css=chart_css(),
        alert_hover_js=_ALERT_HOVER_JS,
        board_js=_BOARD_JS,
        issues_html=_issues_card_html(settings, issue_articles, last_run),
        flow_html=_flow_card_html(last_run, settings),
        mail_icon=icon("mail"),
        # [수정: 2026-08-21] 색 값은 전부 app.config.PALETTE 하나에서 온다.
        **PALETTE,
    )


_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")


def _today_label(last_run: Optional[dict]) -> str:
    now = datetime.now()
    base = f"{now.year}년 {now.month:02d}월 {now.day:02d}일 ({_WEEKDAY_KO[now.weekday()]})"
    if last_run:
        return f"{base} · {last_run['run_slot']} 회차까지 수집됨"
    return base


def _report_articles(runs: list) -> list:
    """회차들의 기사를 숨김 제외·URL 중복 제거해 한 목록으로 모은다(오늘의 주요 주제 입력).

    **화면 이름을 여기서 입혀 `display_group`으로 들려 보낸다** — 이름표는 회차
    단위라(app.curation.load_group_labels) 회차를 합치고 나면 어느 회차 이름표를
    봐야 할지 알 수 없다. 합치기 전, 회차마다 한 번만 읽어 입힌다.

    저장된 기사 dict는 건드리지 않는다(얕은 복사) — 같은 dict를 남색 카드
    (_board_card_html)도 보고, 여기서 키를 더하면 그쪽으로 샌다.
    """
    seen_urls: set = set()
    articles: list = []
    for run in runs:
        labels = load_group_labels(round_id_for_run(run))
        for article in filter_hidden(run["articles"]):
            if article["url"] in seen_urls:
                continue
            seen_urls.add(article["url"])
            tagged = dict(article)
            tagged["display_group"] = display_group_name(article.get("group") or "", labels)
            articles.append(tagged)
    return articles


def generate_landing_page() -> Path:
    """오늘 저장된 모든 회차를 합쳐 home.html을 렌더링한다.

    기사 스크랩(수집)과는 별개 파이프라인이다 — 이미 저장된 회차 데이터를 읽어
    집계만 할 뿐, 새로 수집하지 않는다(네이버 호출도, LLM 호출도 없다). 아직 저장된
    회차가 없어도 예외를 내지 않고 빈 화면을 보여준다 — 진입 화면은 앱이 가장 먼저
    여는 화면이라, 오늘 첫 스크랩 전에도 떠 있어야 한다.

    회차마다 같은 기사가 다시 잡힐 수 있어(회차별 시간창 수집이 적용되기 전에는
    특히), url 기준으로 중복 제거한 뒤 집계한다.
    """
    settings = load_settings()
    today_str = datetime.now().strftime("%Y-%m-%d")
    runs_today = load_today_runs(today_str)
    articles = _report_articles(runs_today)
    last_run = runs_today[-1] if runs_today else None

    html_text = render_landing_page(
        settings=settings,
        today_str=today_str,
        issue_articles=articles,
        last_run=last_run,
        runs_today=runs_today,
    )
    atomic_write_text(LANDING_HTML_PATH, html_text)
    return LANDING_HTML_PATH
