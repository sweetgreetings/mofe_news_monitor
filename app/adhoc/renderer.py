# Design Ref: ADHOC_DESIGN.md §5(화면)·§6.5(소제목)·§6.6(요약)·§6.8(출력) — 이 파일은
# 카드 dict를 HTML/텍스트로 그리기만 한다. 저장은 app.adhoc.card, 수집은
# app.adhoc.collector가 맡는다. 실제 라우팅은 app.adhoc.routes가 아래 계약 그대로 구현한다.
#
# 라우팅 계약 (app/adhoc/routes.py):
#   GET  /adhoc?q=&start=&end=&preset=  → app.adhoc.archive_renderer.render_archive_page(q, start, end, preset)
#                                          ("지난 사안 더보기" — 사안 → 날짜 → 회차 3단 + 사안명·기간 조회,
#                                          ADHOC_DESIGN.md §5.4. preset은 "day"|"week"|"month"|"all" 중
#                                          하나면 start/end를 서버가 계산해 덮어쓴다.) 복사/txt/엑셀은
#                                          전용 엔드포인트 없이 렌더링 시점에 미리 구운 텍스트/rows를
#                                          기존 공용 /download-text·/download-excel로 그대로 보낸다
#                                          (이 화면은 지연 로딩이 없어 정기 확정본과 같은 패턴을 쓴다).
#   GET  /adhoc/new                     → render_new_card_page()
#   GET  /adhoc/card?id=<id>            → render_card_page(card, error=<query "error", 있으면>)
#   GET  /adhoc/card/download?id=<id>   → text/plain 첨부 다운로드(build_adhoc_plain_text)
#   GET  /adhoc/card/download-excel?id=<id> → .xlsx 첨부 다운로드(build_adhoc_excel_rows, PRD.md 기능11)
#   POST /adhoc/collect                 → 새 카드 생성 + 첫 수집 (card.new_card + collector.run_collect)
#   POST /adhoc/card/recollect          → id, window_start, window_end → 창 갱신 + 재수집
#   POST /adhoc/card/add-keyword         → id, keyword
#   POST /adhoc/card/remove-keyword      → id, keyword
#   POST /adhoc/card/add-must-keyword    → id, keyword
#   POST /adhoc/card/remove-must-keyword → id, keyword
#   POST /adhoc/card/rename             → id, report_title
#   POST /adhoc/card/hide               → id, url
#   POST /adhoc/card/unhide             → id, url
#   POST /adhoc/card/hide-group         → id, name (그 소제목에서 화면에 보이는 기사 전부 숨김)
#   POST /adhoc/card/move-article       → id, url, group
#   POST /adhoc/card/move-order         → id, url, direction ("up"|"down", 같은 소제목 안 이웃과 스왑)
#   POST /adhoc/card/bulk-move          → id, urls(반복 필드), group (체크박스 일괄이동)
#   POST /adhoc/card/classify           → id (app.adhoc.classifier.classify_card)
#   POST /adhoc/card/add-custom-group   → id, name (app.adhoc.card.add_custom_group)
#   POST /adhoc/card/remove-custom-group → id, name (app.adhoc.card.remove_custom_group,
#                                          화면에 보이는 기사가 없는 커스텀 소제목만)
#   POST /adhoc/card/move-group-order   → id, name, direction ("up"|"down", 소제목 자체의 순서)
#   POST /adhoc/card/delete             → id
#   POST /adhoc/card/undo               → id (app.adhoc.undo.undo — hide/unhide/hide-group/
#                                          move-article/move-order/bulk-move/classify/
#                                          add-custom-group만 대상)
#   POST /adhoc/card/add-label          → id, url, label, outlet, title, pubDate, group
#   POST /adhoc/card/remove-label       → id, url, label
#   POST /adhoc/card/refetch-summary    → id, url (원문 og:title/og:description 재수집)
#   POST /adhoc/card/edit-summary       → id, url, title, summary (직접 입력)
#                                          [추가: 2026-09-02] 둘 다 app.summary_overrides
#                                          (URL 키 전역)에 저장 — 정기 화면들과 같은 값을
#                                          본다. 화면에 입히는 지점은 _article_groups 하나.
#
# 전부 실제 <form> 제출(fetch 아님)이라, 성공하면 Post/Redirect/Get(303)으로 카드 화면에
# 되돌아간다 — settings_server.py의 기존 fetch+JSON 관행과 다르게 간 이유는, 이 화면들이
# 전부 "저장하고 같은 화면을 다시 본다"는 동일한 모양이라 fetch 왕복·JS 갱신 코드를 매
# 액션마다 새로 짤 필요가 없기 때문이다.
import html
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from app.adhoc import undo
from app.adhoc.card import (
    MAX_ADHOC_KEYWORDS,
    MAX_ADHOC_MUST_KEYWORDS,
    MAX_ADHOC_SUBHEADINGS,
    bundle_label,
    bundle_sources,
    bundle_time,
    bundles_for_date,
    card_kind,
    cards_for_date,
    default_bundle_for,
    is_bundle,
    is_raw,
    last_collect_new_urls,
    latest_card_id,
    list_card_summaries,
    load_issues,
    ordered_group_names,
    raw_row_states,
    sent_index,
    window_of,
)
from app.adhoc.collector import (
    article_in_condition,
    article_out_of_window,
    articles_in_condition,
    out_of_condition_reason,
)
from app.excel_export import format_pub_datetime
from app.config import (
    COLOR_ACCENT,
    COLOR_ADHOC_BG,
    COLOR_ADHOC_TEXT,
    COLOR_BORDER,
    COLOR_SEP_FAINT,
    COLOR_TEXT_FAINT,
    COLOR_TEXT_MUTED,
    FONT_STACK,
    PALETTE,
)
from app.filters import HEADLINE_TAG_RE, headline_kind, looks_like_photo_caption, photo_badge_tip
from app.sorter import sort_by_pub_desc
from app.highlight import highlight_keywords
from app.icons import icon
from app.labels import labels_for_url
from app.llm_classifier import MAX_ARTICLES_FOR_CLASSIFY
from app.renderer import apply_line_template, apply_subheading_format, known_label_chips_html
from app.settings import load_settings
from app.summary_overrides import apply_summary_overrides

UNCLASSIFIED_LABEL = "소제목 미분류"

# 시간 입력칸(수집시간/재수집) 공용 JS — <input type="time">이 오전/오후 표기를
# 브라우저 로케일에 맡기는 문제(실측: lang 속성으로도 못 바꿈)를 피하려고 일반 텍스트
# 입력으로 바꾸고, 숫자를 치면 콜론을 자동으로 넣어 24시간제(HH:MM)를 강제한다.
# 수집 원본(render_new_card_page)·재수집(render_card_page) 두 화면이 그대로 공유한다.
_TIME_INPUT_JS = """
function padHM(v) {
  v = v.replace(/[^0-9]/g, '').slice(0, 4);
  if (v.length >= 3) return v.slice(0, v.length - 2) + ':' + v.slice(-2);
  return v;
}
function attachHmInput(id) {
  var el = document.getElementById(id);
  if (!el) return;
  el.addEventListener('input', function () {
    this.value = padHM(this.value);
    this.setSelectionRange(this.value.length, this.value.length);
  });
}
function nowHM() {
  var d = new Date();
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
// ADHOC_DESIGN.md §6.4a — 검색어 편집 패널 접기/펼치기. 접힌 상태가 기본이고, 접혀
// 있을 때 보이는 메타 칩 줄이 곧 "지금 조건" 요약이다.
function toggleCardEdit() {
  var panel = document.getElementById('edit-panel');
  if (!panel) return;
  panel.classList.toggle('on');
  var btn = document.getElementById('edit-toggle');
  if (btn) btn.querySelector('span').textContent = panel.classList.contains('on') ? '닫기' : '편집';
  // [추가: 2026-09-15] 편집 창을 열면 헤더의 「지금까지 불러오기」는 숨긴다 — 창 안에
  // 「다시 불러오기」가 따로 있어, 불러오는 버튼이 한 화면에 둘 보이면 "둘이 다른 건가?"가
  // 된다(초안 AI 분류 실패 배너에서 한 번 겪은 일). 닫으면 다시 나온다.
  var quick = document.getElementById('reload-now-form');
  if (quick) quick.style.display = panel.classList.contains('on') ? 'none' : '';
}

function setNow(id) {
  document.getElementById(id).value = nowHM();
}
function subMinutesClamped(hm, mins) {
  var parts = hm.split(':');
  var total = parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) - mins;
  if (total < 0) total = 0;
  var h = Math.floor(total / 60), m = total % 60;
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}
function setQuick(startId, endId, kind) {
  var now = nowHM();
  document.getElementById(endId).value = now;
  if (kind === 'midnight') document.getElementById(startId).value = '00:00';
  else document.getElementById(startId).value = subMinutesClamped(now, kind === '1h' ? 60 : 180);
}
var HM_RE = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
function validWindow(startId, endId) {
  var s = document.getElementById(startId).value;
  var e = document.getElementById(endId).value;
  if (!HM_RE.test(s) || !HM_RE.test(e)) {
    alert('시간은 00:00~23:59 형식으로 입력해주세요.');
    return false;
  }
  if (s >= e) {
    alert('끝나는 시각이 시작 시각보다 빠릅니다 (' + s + ' ~ ' + e + ').');
    return false;
  }
  var now = nowHM();
  if (e > now) {
    alert('아직 ' + e + '이 안 됐어요 (지금 ' + now + '). ' + now + '까지만 수집할 수 있어요.');
    return false;
  }
  return true;
}
"""


def _format_hhmm_kr(hhmm: str) -> str:
    """"14:05" -> "14시 05분". app.renderer.format_slot_time_kr과 달리 분이 0이어도
    "00분"을 생략하지 않는다 — 정기 회차 시각은 항상 :00/:30이라 생략해도 헷갈리지
    않지만, 수시는 담당자가 임의 분 단위로 수집을 끝내므로 실제 입력값을 그대로
    보여줘야 한다는 사용자 결정 그대로다(ADHOC_DESIGN.md §6.8, "반올림 없이 입력값 그대로")."""
    hour_str, _, minute_str = hhmm.partition(":")
    return f"{int(hour_str)}시 {int(minute_str) if minute_str else 0:02d}분"


def _basis_time(card: dict) -> str:
    """머리줄 「N시 N분 기준」의 시각("HH:MM") — 원본은 수집 시간창의 끝, 확정본은 담긴 기사를
    보낸 순간의 원본 시각 중 가장 늦은 것(기사가 다 빠졌으면 이 확정본이 받던 원본 시각 —
    card.bundle_time). 없으면 빈 문자열."""
    return bundle_time(card) if is_bundle(card) else window_of(card)["end"]


def report_header_text(card: dict) -> str:
    """복사·txt 첫 줄이자 화면 머리줄의 글자 — 두 자리가 같은 함수를 봐야 어긋나지 않는다.

    [수정: 2026-09-15] 확정본도 시각을 붙인다(ADHOC_BUNDLE_HEADER_MOCKUP.html B안, 사용자
    결정) — 받는 사람이 이 보고서가 언제 기준인지 알아야 해서다. 원본·정기와 같은 모양이다.
    시각이 없는 확정본(이 기능 전에 보낸 기사만 있는 것)은 예전처럼 시각 없이 나간다.
    """
    basis = _basis_time(card)
    if not basis:
        return f"수시 모니터링 ({card['report_title']})"
    return f"수시 모니터링 {_format_hhmm_kr(basis)} 기준({card['report_title']})"


def _article_groups(card: dict) -> list[tuple[Optional[str], list[dict]]]:
    """기사를 group 값 기준으로 묶어, card.ordered_group_names가 계산한 순서(자동
    분류/직접 만든 소제목 + 담당자가 조정해둔 group_order)로 돌려준다. 미분류(None)는
    항상 맨 뒤. 순서 계산 자체는 card.py에 있다 — ▲▼ 버튼(card.move_group_order)이
    쓰는 것과 같은 함수여야 화면과 어긋나지 않는다.

    [수정: 2026-08-20] 지금 조건(검색어·꼭 포함할 검색어)에서 벗어난 기사는 여기서 뺀다
    (ADHOC_DESIGN.md §6.4) — 이 함수가 화면·복사·txt·엑셀이 모두 지나가는 길목이라,
    여기 한 곳만 걸러도 "화면엔 없는데 보고서엔 있는" 어긋남이 생기지 않는다."""
    # [추가: 2026-09-02] "🔄 원문 다시 불러오기"·"✏️ 기사제목 직접 수정"으로 고쳐둔
    # 제목·요약을 여기서 입힌다(app.summary_overrides — URL 키 전역 저장소라 정기
    # 화면들과 같은 값을 본다). 이 함수가 화면·복사·txt·엑셀이 모두 지나는 길목이라
    # 여기 한 곳이면 전부 덮인다 — 붙이는 곳(⋯ 메뉴)만 만들고 이 줄을 빠뜨리면
    # "수시에서 고쳤는데 그 화면만 그대로"라는 정반대 어긋남이 생긴다.
    # [추가: 2026-09-15] 로데이터 원본은 소제목이 없다 — 조건에 걸린 기사 전부를 한 목록,
    # 시간순(최신 먼저)으로(수시 4단 흐름, 실시간현황과 같은 규칙). 옛 카드에서 넘어온
    # group 값이 남아 있어도 여기서 안 쓴다. 숨긴 기사를 따로 거르지 않는 것도 같은 이유다
    # — 원본엔 숨기기가 없어 hidden 플래그가 붙을 길이 없다.
    if is_raw(card):
        return [(None, sort_by_pub_desc(apply_summary_overrides(articles_in_condition(card))))]
    buckets: dict[Optional[str], list[dict]] = {}
    for article in apply_summary_overrides(articles_in_condition(card)):
        buckets.setdefault(article.get("group"), []).append(article)
    for name in card["custom_groups"]:
        buckets.setdefault(name, [])

    ordered_keys = ordered_group_names(card) + ([None] if None in buckets else [])
    # buckets.get(...) — ordered_group_names는 조건 밖 기사까지 훑어 이름을 모으므로
    # (▲▼ 순서 저장이 화면 필터와 무관하게 일관돼야 하기 때문), 그 소제목의 기사가
    # 전부 조건 밖이면 여기 bucket이 아예 없다. buckets[key]로 읽으면 KeyError로
    # 화면이 통째로 죽는다 — 실제로 "소제목 배정 후 검색어를 좁히면" 바로 재현된다.
    # 빈 목록으로 돌려주면 아래 렌더링이 이미 하던 대로(직접 만든 소제목이 아니면
    # 건너뛴다) 처리한다.
    # [추가: 2026-09-03] **수집 원본은 소제목 안에서 시간순(최신 먼저)이 화면 규칙이다.**
    # 그래서 원본에는 기사 ↑↓가 없다 — 화면이 "시간순"이라고 말해놓고 손으로 뒤집을 수
    # 있으면 그 말이 거짓이 된다(정기 실시간현황에 ↑↓가 없는 것과 같은 이유). 순서를
    # 다듬는 일은 확정본이 맡는다. 확정본(bundle)은 여기서 다시 세우지 않는다 —
    # 언론사순은 기사가 **들어올 때** 자리를 잡고(card.send_to_bundle), 그 뒤 담당자의
    # ↑↓가 항상 이겨야 하기 때문이다(CLAUDE.md "담당자 > AI: 정렬·분류 우선순위").
    if not is_bundle(card):
        buckets = {key: sort_by_pub_desc(items) for key, items in buckets.items()}
    return [(key, buckets.get(key, [])) for key in ordered_keys]


def build_adhoc_plain_text(
    card: dict, settings: Optional[dict] = None, groups: Optional[list] = None,
    header: bool = True,
) -> str:
    """복사·다운로드에 쓰는 순수 텍스트를 만든다(ADHOC_DESIGN.md §6.8).

    기사 제목 형식·소제목 형식은 정기 설정을 그대로 상속한다 — 이 화면만 다른 형식을
    쓰면 담당자가 복사한 텍스트 두 개(정기/수시)가 서로 다르게 보여 혼란스럽다.
    UNCLASSIFIED_LABEL("소제목 미분류")은 담당자가 아직 아무것도 정하지 않은 임시
    칸이라 실제 보고 텍스트로 나가면 안 된다(CODING_CONVENTIONS.md §4-2와 같은 이유) —
    여기서는 아직 배정하지 않은 기사를 소제목 없이 그냥 나열해 흘려보낸다.

    [추가: 2026-09-01] groups를 넘기면 그 소제목들만 담는다 — 소제목 헤더의 📋(이
    소제목만 복사)이 쓰는 길이다. 전용 빌더를 따로 두지 않고 인자 하나만 늘린 이유는
    정기와 같다: 기사 줄 형식·빈 줄 규칙이 자동으로 카드 전체 복사와 같아진다.

    [수정: 2026-09-02] header=False면 맨 위 "수시 모니터링 N시 기준(사안명)" 줄을 뺀다 —
    소제목별 복사가 쓰는 길이고, 정기 두 화면(app.renderer/_build_plain_text,
    app.preview_renderer/_build_preview_plain_text)도 같은 인자로 같은 동작을 한다.
    """
    settings = settings or load_settings()
    line_template = settings.get("article_line_template", "ㅇ ({outlet}) {title}")
    subheading_template = settings.get("subheading_format_template", "<{section}>")

    lines = []
    if header:
        lines.append(report_header_text(card))
        lines.append("")

    for group_name, articles in (_article_groups(card) if groups is None else groups):
        visible = [a for a in articles if not a.get("hidden")]
        if not visible:
            continue
        if group_name is not None:
            lines.append(apply_subheading_format(subheading_template, group_name))
        for article in visible:
            lines.append(apply_line_template(line_template, article["outlet"], article["title"]))
            lines.append(article["url"])
            # [수정: 2026-09-01] 기사마다 빈 줄 — 정기(app.renderer._build_plain_text,
            # 2026-07-27)와 같은 규칙이다. 예전엔 소제목 끝에만 넣어 URL과 다음 기사
            # 제목이 붙어 보였다(수시만 정기와 어긋나 있었음).
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def download_filename(card: dict) -> str:
    """ADHOC_DESIGN.md §6.8 — 사안명에 파일명 금지 문자가 섞여 있으면 안전한 문자로 치환한다.

    [수정: 2026-08-18] 정기·수시 공통 파일명 규칙(날짜_종류_구분)으로 통일 —
    app.renderer.render_page의 export_filename과 동일한 형식.
    """
    safe_title = "".join(c if c not in '/:*?"<>|' else "_" for c in card["report_title"])
    return f"{card['collect_date']}_수시모니터링_{safe_title}.txt"


def build_adhoc_excel_rows(card: dict) -> list[dict]:
    """엑셀 내보내기(PRD.md 기능11) rows — _article_groups를 그대로 써서 소제목별
    순서·숨긴 기사 제외를 build_adhoc_plain_text와 똑같이 맞춘다. 미분류(group=None)는
    "📂 소제목 미분류" 같은 표시용 문자열이 애초에 없으므로 빈 칸으로 둔다 — 기능11
    규칙4가 정상 취급하라고 명시한 그 케이스다."""
    rows = []
    for group_name, articles in _article_groups(card):
        for article in articles:
            if article.get("hidden"):
                continue
            rows.append(
                {
                    "스크랩일자": card["collect_date"],
                    "스크랩종료시간": window_of(card)["end"],
                    "발행일 발행시간": format_pub_datetime(article.get("pub_date")),
                    "언론사명": article.get("outlet", ""),
                    "기사제목": article.get("title", ""),
                    "URL": article.get("url", ""),
                    "소제목": group_name or "",
                    "라벨명": ", ".join(labels_for_url(article["url"])),
                }
            )
    return rows


def download_excel_filename(card: dict) -> str:
    """download_filename의 xlsx판 — 확장자만 다르다."""
    safe_title = "".join(c if c not in '/:*?"<>|' else "_" for c in card["report_title"])
    return f"{card['collect_date']}_수시모니터링_{safe_title}.xlsx"


# --- 공통 페이지 뼈대 ------------------------------------------------------------

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
  /* [수정: 2026-09-16] 카드 위 여백 60→44px — 60px은 상단 고정바(54px)를 피하려는 값인데 글자 위로
     30px이 비었다. 44px이면 16px 남는다. 확정본·초안·실시간·정기 보관함·설정·수시·정책 단어 추이
     일곱 화면이 같은 값을 써야 화면을 오갈 때 제목이 들썩이지 않는다(수시·추이는 84→68px 형태).
     시안 SUBHEAD_SPACING_MOCKUP.html B안. */
  .container {{ max-width: 800px; margin: 0 auto; padding: 68px 24px 80px; }}
  .card {{ background: {card}; border: 1px solid {border}; border-radius: 12px; padding: 22px 24px; margin-bottom: 14px; }}
  /* [수정: 2026-08-19] 상단바 — 정기 화면(app/renderer.py .topbar)과 완전히 같은 모양·
     같은 여백으로 맞췄다. 예전엔 "← 수시 보관함" 링크 하나뿐이라 수시에 들어오면
     홈으로 돌아갈 길이 화면에 아예 없었다(사용자 지적). 정기가 fixed 상단바라
     .container도 같은 padding-top(60px + 원래 여백 24px)을 갖는다. */
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
  h1.page-title {{ color: {header}; font-size: 1.2rem; margin: 0 0 4px; }}
  .page-sub {{ color: {muted}; font-size: 0.82rem; margin: 0 0 16px; }}
  /* [추가: 2026-08-18] 수시 보관함(archive_renderer)의 엑셀 내보내기 헤더 — history.html
     의 .hist-head/.hist-excel-btn과 같은 모양(둘 다 "지금 펼친 것"을 그대로 내보낸다). */
  .arch-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
  .arch-excel-btn {{
    background: {accent}; color: {on_fill}; border: none; border-radius: 6px; padding: 7px 14px;
    font-size: 0.85rem; font-family: inherit; cursor: pointer; flex-shrink: 0;
  }}
  .arch-excel-btn:hover {{ filter: brightness(1.08); }}
  /* [추가: 2026-08-20] 수시 보관함 — 사안명·기간 조회 + 사안 ▸ 날짜 ▸ 회차 3단.
     세그먼트 버튼(1일/7일/1개월/전체)은 GET 폼의 submit 버튼이다(서버가 오늘 날짜
     기준으로 실제 start/end를 계산 — JS로 "오늘"을 흉내내지 않는다). */
  .period-bar {{ border: 1px solid {border}; border-radius: 10px; padding: 10px 12px; background: {bg}; margin: 14px 0; }}
  .period-bar form {{ margin: 0; }}
  .prow {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .prow + .prow, .period-bar form + form {{ margin-top: 9px; padding-top: 9px; border-top: 1px solid {border}; }}
  /* [추가: 2026-09-16] 기간 줄 안에서 빠른 설정과 직접 입력을 가르는 세로선 — 정기 보관함과 같다. */
  .prow .pdiv {{ width: 1px; align-self: stretch; background: {border}; margin: 0 2px; }}
  .prow .jlab {{ color: {muted}; font-size: 0.8rem; font-weight: 600; width: 44px; flex: none; }}
  .prow input[type=search] {{ font: inherit; font-size: 0.86rem; border: 1px solid {border}; border-radius: 7px;
    background: {card}; color: {text}; padding: 6px 10px; flex: 1; min-width: 160px; }}
  .prow input[type=search]:focus {{ outline: none; border-color: {accent}; box-shadow: 0 0 0 2px {hover}; }}
  .prow input[type=date] {{ font: inherit; font-size: 0.85rem; border: 1px solid {border}; border-radius: 6px;
    background: {card}; color: {text}; padding: 5px 8px; font-variant-numeric: tabular-nums; }}
  .prow .tilde {{ color: {muted}; margin: 0 2px; font-weight: 600; }}
  .prow .clr {{ border: 1px solid {border}; background: {card}; color: {muted}; border-radius: 6px;
    padding: 5px 12px; font-size: 0.8rem; cursor: pointer; font-family: inherit; text-decoration: none; }}
  .prow .clr:hover {{ background: {hover}; color: {accent}; }}
  .prow .go {{ border: 1px solid {accent}; background: {accent}; color: {on_fill}; border-radius: 6px;
    padding: 6px 14px; font-size: 0.82rem; font-weight: 600; cursor: pointer; font-family: inherit; }}
  .prow .go:hover {{ filter: brightness(1.08); }}
  .jhint {{ font-size: 0.78rem; color: {muted}; }}
  .seg {{ display: inline-flex; border: 1px solid {border}; border-radius: 8px; overflow: hidden; background: {card}; }}
  .seg-btn {{ border: none; background: {card}; color: {muted}; font: inherit; font-size: 0.82rem;
    padding: 6px 15px; cursor: pointer; border-right: 1px solid {border}; }}
  .seg-btn:last-child {{ border-right: none; }}
  .seg-btn.on {{ background: {accent}; color: {on_fill}; font-weight: 600; }}
  .badge-custom {{ font-size: 0.75rem; background: {hover}; color: {accent}; border: 1px solid {accent_border};
    border-radius: 12px; padding: 3px 11px; font-weight: 600; }}
  .result-bar {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 14px 2px 8px; }}
  .result-line {{ font-size: 0.82rem; color: {muted}; display: flex; align-items: center; gap: 8px;
    flex-wrap: wrap; flex: 1; min-width: 0; margin: 0; }}
  .result-line b {{ color: {header}; }}
  .fixhint {{ margin: 0 2px 10px; font-size: 0.8rem; color: {muted}; }}
  .fixhint a {{ color: {accent}; }}
  .qmark {{ background: {query_mark}; border-radius: 3px; padding: 0 2px; }}
  /* 복사/txt/엑셀 — 결과 전체·사안·날짜 세 층 모두 같은 모양. 렌더링 시점에 이미
     계산된 텍스트/rows를 hidden input에 심어 기존 공용 /download-text·/download-excel로
     보낸다(app.history_renderer의 회차별 복사·다운로드 버튼과 같은 패턴). */
  .exp {{ display: inline-flex; gap: 4px; flex: none; margin-left: auto; }}
  .exp button {{ border: 1px solid {border}; background: {card}; color: {muted}; border-radius: 6px;
    padding: 4px 11px; font-size: 0.75rem; cursor: pointer; font-family: inherit; white-space: nowrap; }}
  .exp button:hover {{ background: {hover}; color: {accent}; border-color: {accent_border}; }}
  .exp button.xls:hover {{ background: {hover}; color: {accent}; border-color: {accent_border}; }}  /* [수정: 2026-08-21] 초록→파랑, 발송 색과 겹치지 않게 */
  .exp.lg button {{ font-size: 0.8rem; padding: 6px 13px; }}
  details.day-block {{ border-top: 1px solid {divider_soft}; background: {surface_soft}; }}
  details.day-block > summary {{ list-style: none; cursor: pointer; display: flex; align-items: center; gap: 10px;
    padding: 8px 12px 8px 30px; font-size: 0.85rem; }}
  details.day-block > summary::-webkit-details-marker {{ display: none; }}
  details.day-block > summary::before {{ content: "▸"; color: {muted}; font-size: 0.72rem; }}
  details.day-block[open] > summary::before {{ content: "▾"; }}
  details.day-block > summary:hover {{ background: {row_hover}; }}
  .day-block .d {{ font-weight: 600; color: {header}; min-width: 96px; font-variant-numeric: tabular-nums; }}
  .day-block .dcnt {{ font-size: 0.78rem; color: {muted}; }}
  .run-row .win {{ color: {muted}; font-size: 0.79rem; min-width: 104px; font-variant-numeric: tabular-nums; }}
  .run-row .go {{ color: {accent}; font-size: 0.78rem; }}
  /* ADHOC_DESIGN.md §6.13 — 수시 보관함에서 모음 카드를 가리키는 유일한 표시.
     같은 자리·같은 폭(.win)이라 수집 카드와 나란히 서도 줄이 안 흔들린다. */
  .run-row .bundle-mark {{ background: {adhoc_bg}; border: 1px solid {adhoc_border_soft};
    color: {adhoc_text}; border-radius: 10px; padding: 1px 8px; font-size: 0.72rem; font-weight: 700; }}
  .field {{ display: flex; align-items: center; gap: 10px; margin: 14px 0; flex-wrap: wrap; }}
  .field > label {{ width: 78px; flex: none; font-size: 0.86rem; color: {muted}; font-weight: 600; }}
  input[type=text], input[type=time], select, textarea {{
    font: inherit; font-size: 0.9rem; padding: 7px 11px; border: 1px solid {border};
    border-radius: 8px; background: {card}; color: {text};
  }}
  input[type=text] {{ flex: 1; min-width: 120px; }}
  input.hm {{ flex: none; width: 68px; text-align: center; font-variant-numeric: tabular-nums; letter-spacing: 0.5px; min-width: 0; }}
  .now-btn {{ font: inherit; font-size: 0.8rem; font-weight: 600; padding: 6px 11px; border-radius: 8px; cursor: pointer;
    border: 1px solid {accent_border}; background: {hover}; color: {accent}; flex: none; }}
  .now-btn:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .quick {{ display: flex; gap: 6px; flex-wrap: wrap; margin: -4px 0 0 88px; }}
  .qchip {{ font-size: 0.78rem; padding: 4px 11px; border-radius: 20px; border: 1px dashed {border};
    background: {card}; color: {text_soft}; cursor: pointer; font-family: inherit; }}
  .qchip:hover {{ border-color: {accent}; color: {accent}; border-style: solid; }}
  /* [수정: 2026-08-25] 시작 시각 -1h/+1h 스테퍼는 만들었다가 도로 뺐다 — 시간 줄에
     테두리 상자가 셋(스테퍼 묶음·종료 입력·칩 줄)으로 늘어 패널이 지저분해졌고,
     0시~지금/최근 1시간/최근 3시간 프리셋이 이미 같은 일을 한 번에 한다. */
  /* [수정: 2026-08-25] '지금' 버튼(종료시각 채우기)은 '다시 불러오기'보다 훨씬
     자주 쓰는 동작이 아니라 입력을 거드는 부속품이라, 링크 톤으로 낮춰 실제
     실행 버튼(다시 불러오기)과 무게를 가른다. */
  .now-link {{ font: inherit; font-size: 0.8rem; color: {accent}; background: none; border: none; cursor: pointer;
    text-decoration: underline; text-underline-offset: 3px; padding: 4px 2px; flex: none; }}
  .now-link:disabled {{ opacity: 0.45; cursor: not-allowed; text-decoration: none; }}
  .recollect-action {{ display: flex; align-items: center; justify-content: space-between; gap: 12px;
    border-top: 1px solid {border}; margin-top: 4px; padding-top: 10px; }}
  .recollect-hint {{ font-size: 0.77rem; color: {text_faint}; }}
  .kw-box {{ flex: 1; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; border: 1px solid {border}; border-radius: 8px; padding: 6px 8px; background: {card}; min-width: 200px; }}
  .kw-chip {{ background: {hover}; color: {accent}; border: 1px solid {border}; border-radius: 14px; padding: 3px 10px; font-size: 0.82rem; display: inline-flex; gap: 6px; align-items: center; }}
  .kw-chip button {{ border: none; background: none; color: {muted}; cursor: pointer; font-size: 0.9rem; line-height: 1; padding: 0; }}
  .kw-add {{ border: none; background: none; color: {accent}; cursor: pointer; font-size: 0.82rem; font-weight: 600; padding: 3px 6px; flex: none; }}
  .kw-cnt {{ font-size: 0.76rem; color: {muted}; margin-left: auto; }}
  .tilde {{ color: {muted}; }}
  .btn {{ font: inherit; font-size: 0.88rem; padding: 7px 16px; border-radius: 8px; cursor: pointer; border: 1px solid {accent}; background: {accent}; color: {on_fill}; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; text-decoration: none; }}
  .btn.ghost {{ background: {card}; color: {accent}; }}
  .btn.mute {{ background: {card}; color: {muted}; border-color: {border}; }}
  .btn.danger {{ background: {card}; color: {error}; border-color: {error}; }}
  .btn.ai {{ background: transparent; color: {ai_text}; border-color: {ai_border}; }}
  .btn.ai:hover {{ background: {ai_bg}; border-color: {ai_border_hover}; }}
  .btn.sm {{ font-size: 0.8rem; padding: 5px 11px; }}
  .btn:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .adv {{ margin-top: 8px; border-top: 1px dashed {border}; padding-top: 13px; }}
  .adv-h {{ font-size: 0.82rem; color: {muted}; font-weight: 700; margin-bottom: 10px; }}
  .adv-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; font-size: 0.85rem; }}
  .adv-grid label {{ display: flex; gap: 8px; align-items: center; cursor: pointer; }}
  .warn {{ background: {error_bg}; border: 1px solid {error_border_mid}; color: {error}; border-radius: 9px; padding: 11px 14px; margin: 12px 0; font-size: 0.86rem; }}
  .issue-warn {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 6px 0 0 88px;
    background: {warn_bg}; border: 1px solid {warn_border}; color: {warn_text}; border-radius: 8px; padding: 7px 11px; font-size: 0.82rem; }}
  .issue-warn.hidden {{ display: none; }}
  .issue-warn button {{ font: inherit; font-size: 0.78rem; font-weight: 600; padding: 3px 10px; border-radius: 6px; cursor: pointer; }}
  .issue-warn .use {{ background: {warn_accent}; color: {on_fill}; border: none; }}
  .issue-warn .keep {{ background: {card}; color: {warn_text}; border: 1px solid {warn_border}; }}
  /* 오늘의 수집 결과 — 사안명 탭. 오늘 만든 카드가 2개 이상일 때만 그려진다. */
  .tabwrap {{ background: {adhoc_bg}; margin: -22px -24px 18px; padding: 12px 18px 0; border-radius: 12px 12px 0 0; }}
  .tabwrap-h {{ font-size: 0.74rem; font-weight: 700; color: {adhoc_text}; margin-bottom: 9px; }}
  .tabs {{ display: flex; gap: 4px; align-items: flex-end; overflow-x: auto; scrollbar-width: none; }}
  .tabs::-webkit-scrollbar {{ display: none; }}
  .tab {{ font-size: 0.8rem; padding: 8px 15px; border-radius: 8px 8px 0 0; background: rgba(255,255,255,0.5);
    color: {adhoc_text}; font-weight: 600; border: 1px solid transparent; border-bottom: none;
    white-space: nowrap; text-decoration: none; position: relative; top: 1px; display: inline-block; }}
  .tab:hover {{ background: rgba(255,255,255,0.8); }}
  .tab.on {{ background: {card}; font-weight: 700; border-color: {border}; color: {header}; padding-bottom: 9px; }}
  .tab.on:hover {{ background: {card}; }}
  .tab .n {{ color: {muted}; font-weight: 500; font-size: 0.72rem; margin-left: 5px; }}
  /* [수정: 2026-08-25] × 는 그 카드를 삭제한다(탭 갈아타기와 다른 동작이라
     preventDefault+stopPropagation으로 <a>의 이동을 막는다) — 사용자 확인:
     이 화면에 탭 전용의 별도 '치우기' 상태는 필요 없다(삭제만 있으면 된다). */
  .tab .del {{ margin-left: 7px; color: {muted}; font-weight: 400; padding: 1px 3px; border-radius: 4px; }}
  .tab .del:hover {{ color: {error}; background: rgba(0,0,0,0.06); }}
  /* [수정: 2026-08-25] 다른 탭과 헷갈리지 않게 옅은 청록 음영 + 점선 테두리로
     "이동"이 아니라 "만들기"라는 걸 구분한다 — opacity로 흐리기만 하던 예전
     방식은 비활성 탭처럼 보였다(사용자 지적). */
  .tab.add {{ background: rgba(20,107,94,0.08); color: {adhoc_text}; font-weight: 500;
    border: 1px dashed {adhoc_text}; margin-left: 8px; }}
  .tab.add:hover {{ background: rgba(20,107,94,0.14); }}
  .report-head {{ display: flex; align-items: center; gap: 2px; flex-wrap: wrap; font-size: 1.1rem; font-weight: 700; color: {header}; }}
  .issue-input {{ font: inherit; font-size: 1.02rem; font-weight: 700; color: {header}; border: none; border-bottom: 2px dashed {accent}; background: {hover}; padding: 2px 8px; border-radius: 4px 4px 0 0; width: 180px; }}
  .meta-row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 12px; font-size: 0.8rem; }}
  .chip {{ background: {divider_soft}; border: 1px solid {border}; border-radius: 14px; padding: 3px 11px; color: {muted}; }}
  /* ADHOC_DESIGN.md §6.4a — 검색어 편집 패널. 평소엔 접혀 있고, 접힌 상태가 곧 위
     메타 칩 줄이다(새 줄을 만들지 않는다). 펼치면 검색어·꼭 포함할 검색어·시간창이 나온다. */
  .edit-btn {{ font: inherit; font-size: 0.76rem; padding: 3px 11px; border-radius: 14px; cursor: pointer;
    border: 1px dashed {accent_border}; background: {card}; color: {accent}; font-weight: 600;
    display: inline-flex; align-items: center; gap: 5px; }}
  .edit-btn:hover {{ background: {hover}; border-style: solid; }}
  .edit-panel {{ display: none; border: 1px solid {accent_border_soft}; background: {accent_panel_bg};
    border-radius: 10px; padding: 14px 16px 10px; margin-top: 12px; }}
  .edit-panel.on {{ display: block; }}
  .edit-panel .field {{ margin: 0 0 4px; }}
  .edit-panel .field > label {{ width: 104px; line-height: 1.25; }}
  .edit-panel .field > label.must-label {{ color: {must_text}; }}
  .kw-box.must {{ border-color: {must_border}; background: {must_box_bg}; }}
  .kw-chip.must {{ background: {must_bg}; color: {must_text}; border-color: {must_border}; }}
  .kw-chip.must button {{ color: {must_accent}; }}
  .kw-add.must {{ color: {must_accent}; }}
  .kw-addform {{ display: inline-flex; align-items: center; gap: 2px; flex: 1; min-width: 130px; }}
  .kw-addform input {{ border: none; flex: 1; min-width: 90px; padding: 3px 4px; background: transparent; font-size: 0.85rem; }}
  .kw-hint {{ font-size: 0.75rem; color: {text_faint}; margin: 0 0 11px 114px; }}
  /* 새 수집 화면의 검색 방식 선택 — 라벨 폭이 78px이라 편집 패널(104px)과 들여쓰기가
     다르다. 경고 문구에 .warn을 쓰면 페이지에 이미 있는 빨간 오류 상자와 이름이 겹쳐
     오류처럼 보이므로(색 규칙: 빨강=오류, 앰버=주의) .is-over로 따로 둔다. */
  .kw-mode {{ margin: -4px 0 0 88px; display: flex; flex-direction: column; gap: 3px; }}
  .kw-mode.off {{ opacity: 0.45; }}
  .mode-opt {{ display: flex; align-items: center; gap: 6px; font-size: 0.8rem;
               color: {muted}; cursor: pointer; }}
  .mode-opt input {{ margin: 0; }}
  .mode-opt b {{ font-weight: 700; color: {text}; }}
  .mode-opt.on {{ color: {text}; }}
  .mode-opt.on.all, .mode-opt.on.all b {{ color: {must_text}; }}
  .mode-note {{ font-size: 0.75rem; color: {text_faint}; margin: 2px 0 0 20px; }}
  .mode-note:empty {{ display: none; }}
  .mode-note.is-over {{ color: {warn_accent}; font-weight: 600; }}
  .chip .must-kw {{ color: {must_text}; font-weight: 600; }}
  .toolbar {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 11px 0; border-top: 1px solid {border}; border-bottom: 1px solid {border}; margin: 16px 0; }}
  /* [수정: 2026-09-15] 체크박스 일괄이동 바 — 툴바 밑(목록 맨 위)에서 화면 맨 아래 흰 띠로
     옮겼다(시안 ADHOC_BULK_BAR_MOCKUP.html A안). 스크롤을 내려 기사를 체크하면 바가 화면 밖에
     있어 다시 맨 위까지 올라가야 했다. 모양은 정기 확정본·초안의 .bottombar(app/renderer.py)와
     같은 값이다 — 같은 일을 하는 바가 두 흐름에서 같은 자리에 있어야 한다. 정기는 🖍 때문에
     띠가 늘 떠 있지만 여기엔 올릴 게 이것뿐이라 체크했을 때만 아래에서 올라온다(.on).
     .container의 아래 여백(80px)이 띠 높이보다 커서 마지막 기사가 가려지지 않는다. */
  .bulk-bar {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
    transform: translateY(100%); visibility: hidden; transition: transform .16s ease-out, visibility .16s; }}
  .bulk-bar.on {{ transform: none; visibility: visible; }}
  .bulk-bar-inner {{ max-width: 800px; margin: 0 auto; padding: 10px 24px;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.85rem; }}
  .bulk-bar .bulk-count {{ font-weight: 600; color: {header}; white-space: nowrap; margin-right: 4px; }}
  .bulk-bar .bulk-div {{ width: 1px; height: 20px; background: {border}; margin: 0 4px; }}
  .bulk-bar .clear-btn {{ margin-left: auto; background: transparent; border-color: transparent; color: {muted}; }}
  .bulk-bar .clear-btn:hover {{ background: {hover}; }}
  h2.sub {{ color: {header}; font-size: 1rem; margin: 20px 0 6px; display: flex; align-items: center; gap: 6px; }}
  h2.sub .cnt {{ font-size: 0.76rem; color: {muted}; font-weight: 400; }}
  h2.sub .sub-acts {{ margin-left: auto; display: flex; align-items: center; gap: 2px; }}
  .order-btn {{ background: transparent; border: none; color: {muted}; font-size: 0.78rem; cursor: pointer;
    padding: 2px 4px; display: inline-flex; }}
  .order-btn:disabled {{ opacity: 0.35; cursor: default; }}
  .group-block.sub-custom {{ border-left: 3px dashed {custom_group_border}; padding-left: 10px; margin-left: -13px; }}
  .custom-tag {{ font-size: 0.72rem; color: {custom_tag_text}; background: {custom_tag_bg}; border-radius: 10px; padding: 1px 8px; font-weight: 400; }}
  /* [수정: 2026-09-02] 기사 행 규격을 정기 확정본(app/renderer.py .article)과 같은
     값으로 맞췄다 — 담당자가 두 화면을 오갈 때 글자 크기·여백·들여쓰기가 미묘하게
     달라 "다른 앱 같다"는 지적(사용자, 2026-09-02). 카드 여백 8/9px·위아래 2px +
     아래 실선 → 정기와 같은 6/8px·위아래 10px·실선 없음. 행 사이를 실선으로 가르던
     걸 없애도 목록이 안 뭉개지는 건, 늘어난 위아래 여백이 그 역할을 대신하기 때문이다. */
  /* [수정: 2026-09-16] 행 여백 한 단계씩 축소 — padding 6→4px, 제목·메타 줄 사이(.a-bot 위 여백) 4→2px,
     행 사이 10→6px. 글자·버튼 크기는 그대로다(한 건 74 → 64px). 확정본·초안·정기 보관함·
     수시 네 파일에 같은 값이 복제돼 있으니 한쪽만 고치지 않는다. 시안 ARTICLE_ROW_DENSITY_MOCKUP.html B안. */
  .article {{ padding: 4px 8px; border-radius: 8px; margin: 6px 0; line-height: 1.5; }}
  .article.art-scoop {{ background: {scoop_bg}; border-left: 3px solid {scoop_bar}; padding-left: 9px; }}
  .article:hover {{ background: {row_hover}; }}
  .article.out {{ background: {out_bg}; border: 1px dashed {out_border}; }}
  /* [추가: 2026-09-15] 마지막 불러오기로 새로 들어온 기사 — 정기 초안·확정본의 「새로
     들어온 기사」와 같은 노랑(row_new). hover보다 뒤에 둬 마우스를 올려도 노랑이 남는다
     (정기와 같은 규칙 — 상태 색이 hover 회색을 이긴다). */
  .article.is-new-arrival {{ background: {row_new}; }}
  /* [추가: 2026-09-15] 헤더 오른쪽 「지금까지 불러오기」 — 편집 칩과 같은 알약 모양의
     채운 파랑(이 화면의 주 동작). margin-left:auto로 헤더 줄 오른쪽 끝에 붙고, 창이
     좁아 줄이 넘치면 아랫줄 오른쪽으로 내려간다. */
  .reload-now-form {{ margin: 0 0 0 auto; display: inline-flex; }}
  .now-btn {{ font: inherit; font-size: 0.8rem; padding: 5px 12px; border-radius: 14px; cursor: pointer;
    border: 1px solid {accent}; background: {accent}; color: {on_fill}; font-weight: 600;
    display: inline-flex; align-items: center; gap: 5px; }}
  .now-btn:hover {{ background: {accent_pressed}; border-color: {accent_pressed}; }}
  .now-btn:disabled {{ opacity: 0.6; cursor: progress; }}
  .now-btn:disabled .ic {{ animation: now-spin 0.9s linear infinite; }}
  @keyframes now-spin {{ to {{ transform: rotate(360deg); }} }}
  /* [추가: 2026-09-02] [단독] 카드 강조·사진 추정 흐림 — 정기와 같은 규칙·같은 색.
     .art-scoop을 hover/out보다 **먼저** 선언해 가장 낮은 우선순위를 준다(정기의 같은
     주석 참고 — "지금 이 순간의 정보"가 "원래 중요하다"를 항상 이긴다). */
  .article.is-photo {{ opacity: 0.55; }}
  .article.is-photo:hover, .article.is-photo:has(.bulk-chk:checked) {{ opacity: 1; }}
  /* ADHOC_DESIGN.md §6.13 「보낸 기사 표시」 — 띠 + 내용 흐리기 + ✓칩 세 겹.
     ① 띠는 border-left가 아니라 inset box-shadow다: .art-scoop([단독])이 이미
        border-left를 쓰고 있어, 테두리로 그리면 둘 중 하나가 상대를 지운다. 그림자로
        넣으면 두 사실("단독이다" + "보냈다")이 같이 보인다.
     ② 흐리기는 행이 아니라 「내용」(.a-title/.a-bot)에만 건다 — 행에 걸면 띠와 ✓칩까지
        같이 흐려져 정작 눈에 걸려야 할 두 신호가 약해진다. 신호는 100%, 내용만 55%.
     ③ 배경색은 안 건드린다 — 이 행엔 이미 hover 회색·체크 파랑·.out 점선이 붙어서,
        네 번째 배경색을 넣으면 겹칠 때 무엇이 이기는지 알 수 없게 된다. */
  .article.is-sent {{ box-shadow: inset 3px 0 0 {adhoc_text}; }}
  .article.is-sent .a-title, .article.is-sent .a-bot {{ opacity: 0.55; transition: opacity 0.12s; }}
  .article.is-sent:hover .a-title, .article.is-sent:hover .a-bot {{ opacity: 1; }}
  .article.is-sent .a-top {{ align-items: center; }}
  .sent-tag {{ font-size: 0.72rem; color: {adhoc_text}; background: {adhoc_bg};
    border: 1px solid {adhoc_border_soft}; border-radius: 10px; padding: 1px 8px; margin-left: 6px;
    display: inline-flex; align-items: center; gap: 3px; white-space: nowrap; flex: none; }}
  /* [추가: 2026-09-15] 로데이터 원본의 보내기 — [확정본으로 | ▾] 한 몸 버튼. 기본은 이 원본의
     확정본으로 한 번에, 다른 확정본은 ▾ 뒤로. 보낸 기사는 이 자리가 흐린 「✓ 보냄」이 된다
     (실시간현황의 「📌 담아두기 ↔ ✔️ 스크랩됨」과 같은 모양 — 칩을 따로 달지 않는다). */
  .send-split {{ display: inline-flex; align-items: stretch; border: 1px solid {adhoc_border_soft};
    border-radius: 7px; flex-shrink: 0; }}
  .send-split .send-go, .send-split .send-more {{ border: none; background: {adhoc_bg}; color: {adhoc_text};
    font: inherit; font-size: 0.76rem; font-weight: 700; cursor: pointer; }}
  .send-split .send-go {{ padding: 3px 9px; border-radius: 6px 0 0 6px; min-width: 74px; }}
  .send-split .send-more {{ padding: 3px 6px; border-left: 1px solid {adhoc_border_soft};
    border-radius: 0 6px 6px 0; font-size: 0.68rem; }}
  .send-split .send-go:hover, .send-split .send-more:hover {{ filter: brightness(0.96); }}
  .send-split .more-menu button small {{ display: block; color: {text_faint}; font-size: 0.72rem; font-weight: 400; }}
  .send-split .more-menu button {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0 5px; }}
  .send-split .more-menu button small {{ flex-basis: 100%; padding-left: 1.3em; }}
  .more-menu .menu-sep {{ display: block; height: 1px; background: {border}; margin: 4px 0; }}
  .more-menu.up {{ top: auto; bottom: 100%; left: 0; right: auto; margin: 0 0 6px; }}
  .send-done {{ border: 1px solid {border}; background: {card}; color: {text_faint}; font: inherit;
    font-size: 0.76rem; font-weight: 600; border-radius: 7px; padding: 3px 9px; min-width: 74px;
    display: inline-flex; align-items: center; justify-content: center; gap: 3px; flex-shrink: 0; }}
  .bulk-bar .send-split .send-go {{ font-size: 0.82rem; padding: 5px 12px; }}
  .bulk-bar .send-split .send-more {{ font-size: 0.74rem; padding: 5px 8px; }}
  /* 원본 툴바 오른쪽 끝 — 이 원본의 확정본으로 가는 길. 아직 없으면 흐린 안내만. */
  .pair-link {{ display: inline-flex; align-items: center; gap: 5px; font-size: 0.8rem; font-weight: 700;
    color: {adhoc_text}; background: {adhoc_bg}; border: 1px solid {adhoc_border_soft};
    border-radius: 16px; padding: 5px 12px; text-decoration: none; white-space: nowrap; }}
  a.pair-link:hover {{ filter: brightness(0.96); }}
  .pair-link.none {{ background: {card}; color: {text_faint}; border-style: dashed; font-weight: 500; }}
  /* [추가: 2026-09-15] 새 수집의 「지난 수집」 — 날짜 · 사안명 · 검색어를 단어로만 한 줄씩.
     1년치가 쌓여도 카드가 길어지지 않게 목록만 스크롤한다. */
  .hist-h {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .hist-h h2 {{ font-size: 1rem; color: {header}; margin: 0; }}
  .hist-sub {{ font-size: 0.76rem; color: {text_faint}; }}
  .hist-h input[type=search] {{ margin-left: auto; font: inherit; font-size: 0.82rem; width: 220px; max-width: 100%;
    border: 1px solid {border}; border-radius: 8px; padding: 6px 10px; background: {card}; color: {text}; }}
  .hist {{ max-height: 330px; overflow-y: auto; margin-top: 10px; border-top: 1px solid {divider_soft}; }}
  .hist .hm {{ font-size: 0.72rem; font-weight: 700; color: {text_faint}; padding: 10px 6px 3px; }}
  .hist .hr {{ display: flex; align-items: baseline; gap: 12px; padding: 7px 8px; border-radius: 7px;
    cursor: pointer; font-size: 0.86rem; }}
  .hist .hr:hover {{ background: {row_hover}; }}
  .hist .hr.picked {{ background: {adhoc_bg}; }}
  .hist .h-d {{ width: 64px; flex-shrink: 0; color: {text_faint}; font-size: 0.78rem; font-variant-numeric: tabular-nums; }}
  .hist .h-is {{ font-weight: 700; color: {text}; flex-shrink: 0; max-width: 40%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }}
  .hist .h-kw {{ color: {muted}; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .hist .h-all {{ font-size: 0.7rem; color: {adhoc_text}; background: {adhoc_bg}; border-radius: 8px;
    padding: 0 6px; margin-left: 5px; font-weight: 600; }}
  .hist .h-must {{ color: {must_text}; font-size: 0.8rem; }}
  .hist .h-x {{ font-size: 0.72rem; color: {text_faint}; flex-shrink: 0; }}
  .hist .h-open {{ margin-left: auto; font-size: 0.74rem; color: {accent}; text-decoration: none; flex-shrink: 0;
    opacity: 0; }}
  .hist .hr:hover .h-open, .hist .h-open:focus {{ opacity: 1; }}
  .hist-none {{ padding: 14px 8px; color: {text_faint}; font-size: 0.82rem; }}
  #new-card.flash {{ animation: new-card-flash 0.9s ease; }}
  @keyframes new-card-flash {{ 0% {{ box-shadow: 0 0 0 3px {adhoc_border_soft}; }} 100% {{ box-shadow: none; }} }}
  /* 원본 목록 머리 — 소제목이 없어 건수와 「최신순」만 */
  h2.sub.raw-head {{ gap: 8px; }}
  h2.sub.raw-head .cnt {{ font-size: 0.86rem; color: {header}; font-weight: 700; }}
  h2.sub.raw-head .raw-order {{ font-size: 0.76rem; color: {muted}; font-weight: 500; }}
  .sel-sm.to-bundle {{ border-color: {adhoc_border_soft}; color: {adhoc_text}; }}
  .sel-sm.to-bundle:disabled {{ color: {text_faint}; border-color: {border}; }}
  .chip.src {{ background: {adhoc_bg}; border-color: {adhoc_border_soft}; color: {adhoc_text};
    font-weight: 600; text-decoration: none; }}
  .chip.src:hover {{ filter: brightness(0.97); }}
  .chip.bundle-tag {{ background: {adhoc_bg}; border-color: {adhoc_border_soft}; color: {adhoc_text};
    font-weight: 700; font-size: 0.78rem; margin-left: 8px; }}
  .sent-filter {{ display: inline-flex; align-items: center; gap: 6px; font-size: 0.82rem;
    color: {muted}; cursor: pointer; }}
  /* ☑ 아직 안 보낸 기사만 — 순서는 한 톨도 안 건드리고 감추기만 한다(§6.13 C안 기각). */
  body.unsent-only .article.is-sent {{ display: none; }}
  /* [추가: 2026-09-15] 원본의 「🗑 숨김」 — 실시간현황 .live-row.is-hidden과 같은 값(흐리게 +
     취소선)이고 목록에서 빼지 않는다. 보내기 자리의 「🗑 숨김」 버튼(되돌리는 곳)은 또렷하게
     둔다 — 보낸 기사처럼 「신호는 또렷하게, 내용만 흐리게」. */
  body.unsent-only .article.is-raw-hid {{ display: none; }}
  .article.is-raw-hid .a-title {{ opacity: 0.5; text-decoration: line-through; }}
  .article.is-raw-hid .a-bot > :not(.a-acts) {{ opacity: 0.5; }}
  .send-done.raw-unhide {{ color: {muted}; }}
  .send-done.raw-unhide:not(:disabled) {{ cursor: pointer; }}
  .send-done.raw-unhide:not(:disabled):hover {{ border-color: {text_faint}; color: {text}; }}
  .sent-filter small {{ color: {text_faint}; font-size: 0.76rem; }}
  /* 「처리 안 한 N건 전부 확정본으로」 — 원본의 보내기 버튼과 같은 청록, 테두리만. */
  .btn.send-all {{ background: {card}; color: {adhoc_text}; border-color: {adhoc_border_soft}; }}
  .btn.send-all:hover {{ background: {adhoc_bg}; }}
  /* [추가: 2026-09-15] 📷 사진 추정 모아 보기 — 정기 확정본·초안과 같은 이름·같은 동작
     (app.renderer.PHOTO_GATHER_STYLE). 화면만 바꾸는 보기라 카드·복사·txt·엑셀은 그대로다.
     툴바엔 자리가 없어(넣으면 xlsx가 홀로 아랫줄로 떨어진다) 목록 바로 위 한 줄에 둔다. */
  .photo-row {{ display: flex; gap: 8px; align-items: center; margin: -6px 0 10px; }}
  .btn.photo-gather-btn.is-on {{ background: {accent}; color: {on_fill}; border-color: {accent}; }}
  .photo-gather-strip {{
    position: sticky; top: 60px; z-index: 15; display: flex; align-items: center; gap: 10px;
    flex-wrap: wrap; background: {hover}; border: 1px solid {accent_border}; border-radius: 10px;
    padding: 9px 12px; margin: 4px 0 6px;
  }}
  .photo-gather-strip label {{ display: inline-flex; align-items: center; gap: 6px; font-size: 0.85rem; cursor: pointer; }}
  .photo-gather-strip .pg-msg {{ font-size: 0.88rem; color: {header}; }}
  .photo-gather-strip .pg-sp {{ flex: 1; }}
  .pg-done {{ text-align: center; color: {muted}; padding: 28px 0 16px; font-size: 0.9rem; }}
  .pg-from {{
    flex-shrink: 0; max-width: 16em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-size: 0.74rem; color: {text_soft}; background: {bg}; border: 1px solid {border};
    border-radius: 6px; padding: 0 6px;
  }}
  body.photo-gather .article.is-photo {{ opacity: 1; }}
  body.photo-gather .photo-badge {{ display: none; }}
  body.photo-gather .group-block:not(.photo-gather-view), body.photo-gather .adhoc-summary,
  body.photo-gather .art-order-btn {{ display: none !important; }}
  .t-scoop {{ color: {scoop_text}; font-weight: 800; }}
  .t-flash {{ color: {flash_text}; font-weight: 600; }}
  .photo-badge {{
    flex-shrink: 0; padding: 1px 7px; border-radius: 999px; font-size: 0.72rem;
    color: {photo_badge_text}; background: {photo_badge_bg}; border: 1px solid {photo_badge_border};
    white-space: nowrap; position: relative; top: -0.15em;
  }}
  .article details {{ display: block; }}
  .article summary {{ list-style: none; cursor: pointer; }}
  .article summary::-webkit-details-marker {{ display: none; }}
  /* [추가: 2026-08-19] 요약 펼침 — app/renderer.py의 "이미 확인한 기사" 규칙과 동일:
     details가 열려있는 동안만 :has()로 제목 색을 바꾼다(자바스크립트 불필요, 닫으면
     자동으로 원래 색으로 돌아온다). */
  .article:has(details[open]) .a-title {{ opacity: 0.62; }}  /* [수정: 2026-08-21] 색 대신 opacity */
  .a-top {{ display: flex; align-items: flex-start; gap: 8px; }}
  .bulk-chk {{ margin-top: 4px; flex: none; }}
  /* [수정: 2026-09-02] 제목 0.93 → 1rem, 메타 줄 0.78 → 0.8rem, 들여쓰기 25 → 28px.
     셋 다 정기 확정본의 실측값 그대로다(.title-line은 body 기본 크기를 그대로 쓰고,
     28px은 체크박스 자체 여백까지 포함해 브라우저에서 잰 값이다 — app/renderer.py의
     ".article:has(.article-select) .action-row" 주석 참고). */
  .a-title {{ font-size: 1rem; }}
  /* [수정: 2026-09-11] 정기 확정본 .title-outlet과 같은 값 — 괄호 없이 회색·조금 작은 글자. */
  .a-title .outlet {{ color: {muted}; font-size: 0.86em; font-weight: 500; margin-right: 0.5em; }}
  .a-bot {{ display: flex; align-items: center; gap: 8px; margin: 2px 0 0 28px;
    flex-wrap: wrap; row-gap: 4px; }}
  .a-time {{ font-size: 0.8rem; color: {muted}; white-space: nowrap;
    user-select: none; -webkit-user-select: none; }}
  .a-time-sep {{ color: {muted}; font-size: 0.8rem; margin: 0 2px;
    user-select: none; -webkit-user-select: none; }}
  /* [추가: 2026-09-02] 🔍 이 기사가 걸린 검색어 — 정기 확정본의 .kw-inline과 같은
     자리(게시시각 뒤)·같은 크기·같은 두 톤(돋보기는 한 톤 흐리게, 값이 먼저 읽히도록).
     값은 카드에 저장된 matched_keywords로, 수집할 때마다 현재 검색어 기준으로 다시
     쓰인다(app/adhoc/collector.py) — 검색어를 고치면 이 표시도 따라 바뀐다. */
  .kw-inline {{ font-size: 0.8rem; color: {muted};
    display: inline-flex; align-items: center; gap: 4px; }}
  .kw-inline .kw-ic {{ width: 0.75em; height: 0.75em; color: {text_faint_alt}; flex-shrink: 0; }}
  .kw-inline .kw-v {{ color: {text_soft}; }}
  /* [추가: 2026-08-19] URL 통짜 텍스트 줄(.a-url) 대신 원문보기 링크 — 정기 확정본의
     .origin-link-text와 같은 자리·같은 색(app/renderer.py 참고). 액션 줄에 있던 ↗
     원문 열기 아이콘은 여기로 통합해 없앤다(같은 일을 하는 입구를 두 개 두지 않음). */
  .origin-link {{ color: {accent}; text-decoration: none; font-size: 0.8rem; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; flex-shrink: 0; }}
  .origin-link:hover {{ text-decoration: underline; }}
  .origin-link .ic {{ width: 0.75em; height: 0.75em; }}
  .a-acts {{ display: flex; gap: 4px; align-items: center; margin-left: auto;
    justify-content: flex-end; flex-shrink: 0; flex-wrap: wrap; row-gap: 4px; }}
  .icon-btn {{ border: none; background: none; color: {muted}; border-radius: 6px; padding: 4px 6px; cursor: pointer; display: inline-flex; align-items: center; }}
  .icon-btn:hover {{ background: {border}; color: {text}; }}
  /* [추가: 2026-09-02] 기사 행 안의 아이콘만 정기 규격으로 따로 잡는다 — 소제목 헤더의
     📋🗑▲▼(같은 .icon-btn)는 예전 그대로 둔다. 정기에서 복사·🗑·↑↓는 hover 배경이
     없고(.move-btn/.hide-btn/.copy-btn), ⋯·🏷만 옅은 파랑 배경이 깔린다(.more-btn/
     .lab-btn) — 그 구분을 그대로 옮겼다. */
  .a-acts .icon-btn {{ padding: 2px 6px; font-size: 1rem; border-radius: 4px; }}
  .a-acts .icon-btn:hover {{ background: none; color: {text}; }}
  .a-acts .hide-btn:hover {{ color: {error}; }}
  .a-acts .more-btn, .a-acts .lab-btn {{ padding: 3px 7px; }}
  .a-acts .more-btn:hover, .a-acts .lab-btn:hover {{ background: {hover}; color: {accent}; }}
  /* 기사 복사 — 정기 copyArticleIcon과 같은 피드백(1초간 ✓). 예전엔 버튼 안 HTML을
     통째로 갈아끼웠는데, 아이콘 폭이 바뀌며 옆 버튼들이 흔들렸다. */
  .a-copy-btn .icon-done {{ display: none; }}
  .a-copy-btn.is-copied .icon-default {{ display: none; }}
  .a-copy-btn.is-copied .icon-done {{ display: inline-flex; }}
  /* [추가: 2026-09-02] ⋯ 더보기 — 정기와 같은 마크업·같은 치수. 담기는 건 원문 다시
     불러오기·기사제목 직접 수정 둘이고, 저장은 정기와 같은 전역 저장소를 쓴다
     (app.summary_overrides — 같은 URL이면 어느 화면에서 봐도 같은 제목이어야 한다). */
  .more-wrap {{ position: relative; display: inline-flex; flex-shrink: 0; }}
  .more-menu {{
    display: none; position: absolute; right: 0; top: 100%; margin-top: 4px; z-index: 60;
    min-width: 172px; background: {card}; border: 1px solid {border}; border-radius: 8px;
    padding: 4px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.13);
  }}
  .more-wrap.is-open .more-menu {{ display: block; }}
  .more-menu button {{
    display: block; width: 100%; text-align: left; background: transparent; border: none;
    padding: 8px 10px; border-radius: 5px; font-size: 0.85rem; color: {text};
    cursor: pointer; white-space: nowrap;
  }}
  .more-menu button:hover {{ background: {hover}; }}
  /* ✏️ 직접 수정 — 요약 아래에 펼쳐지는 입력칸. 이 화면 관례대로 실제 <form> 제출이라
     저장하면 페이지가 통째로 다시 그려진다(정기의 fetch+204와 다른 점). */
  .edit-box {{ margin: 6px 0 4px 28px; padding: 10px 12px; background: {bg};
    border: 1px solid {border}; border-radius: 8px; }}
  .edit-box.hidden {{ display: none; }}
  .edit-box label {{ display: block; font-size: 0.72rem; color: {muted}; margin: 0 0 3px; }}
  .edit-box input, .edit-box textarea {{ width: 100%; box-sizing: border-box; font: inherit;
    font-size: 0.85rem; padding: 6px 8px; border: 1px solid {border}; border-radius: 6px;
    margin-bottom: 8px; background: {card}; color: {text}; }}
  .edit-box textarea {{ min-height: 60px; resize: vertical; }}
  .edit-box .acts {{ display: flex; gap: 6px; justify-content: flex-end; }}
  /* [추가: 2026-09-02] 소제목 헤더의 🗑(통째 숨기기·빈 소제목 삭제)만 hover 빨강 —
     같은 묶음의 📋·▲▼는 무채색 그대로다(정기 .rename-btn.group-hide-btn과 같은 규칙). */
  .icon-btn.group-hide-btn:hover {{ color: {error}; }}
  /* [추가: 2026-09-01] 소제목 헤더의 📋 — 정기 화면(.group-copy-btn)과 같은 전환:
     누르면 1초간 ✓로 바뀐다. 색·크기는 옆 ▲▼·🗑과 같은 .icon-btn에서 물려받는다. */
  .group-copy-btn .icon-done {{ display: none; }}
  .group-copy-btn.is-copied .icon-default {{ display: none; }}
  .group-copy-btn.is-copied .icon-done {{ display: inline-flex; }}
  .sel-sm {{ font: inherit; font-size: 0.78rem; padding: 3px 8px; border: 1px solid {border}; border-radius: 7px; background: {card}; color: {muted}; }}
  .out-tag {{ font-size: 0.72rem; color: {out_tag_text}; background: {out_tag_bg}; border: 1px solid {out_border}; border-radius: 10px; padding: 1px 8px; margin-left: 6px; }}
  /* [추가: 2026-08-19] 펼쳐지는 요약 — app/renderer.py .article-summary와 같은 목적,
     이 화면 카드 톤(옅은 배경+왼쪽 테두리)에 맞춰 살짝 감쌌다. */
  /* [수정: 2026-09-02] 정기 .article-summary와 같은 값(여백만, 배경 상자 없음)으로
     맞췄다 — 수시 쪽 상자가 더 예뻤지만, 두 화면이 같아 보이는 게 먼저다. */
  .a-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: 0.95rem; }}
  .a-summary.none {{ color: {text_faint}; font-style: italic; }}
  .empty {{ color: {muted}; font-size: 0.85rem; padding: 12px 0; }}
  /* [수정: 2026-09-02] 좌하단 쓰레기통 스택은 정기 확정본·초안(app.renderer의
     .trash-fab/.undo-fab/.trash-pop)과 같은 자리·같은 규격이어야 한다 — 문서에는
     그렇게 적혀 있었지만 실제 값이 4px씩 어긋나 있었다(16↔20, 52↔56, 46↔50,
     78↔88, 320↔336). 두 흐름을 오갈 때 같은 버튼이 조금씩 움직이면 안 되므로
     정기 값으로 통일한다. */
  .fab {{ position: fixed; z-index: 25; box-sizing: border-box; width: 50px; height: 50px; padding: 0; border-radius: 50%; background: {card}; color: {muted}; border: 1px solid {border}; box-shadow: 0 4px 12px rgba(0,0,0,.12); display: flex; align-items: center; justify-content: center; cursor: pointer; font: inherit; }}
  .fab.left {{ left: 20px; bottom: 20px; width: 56px; height: 56px; }}
  .fab.left.upper {{ bottom: 88px; width: 50px; height: 50px; font-size: 1.3rem; }}
  .fab.left.upper:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .fab .badge {{ position: absolute; top: -3px; right: -3px; background: {error}; color: {on_fill}; font-size: 0.66rem; font-weight: 700; border-radius: 9px; padding: 0 5px; line-height: 1.55; }}
  .fab-pop {{ display: none; position: fixed; left: 20px; bottom: 88px; width: 336px; max-height: 60vh; overflow-y: auto; background: {card}; border: 1px solid {border}; border-radius: 11px; box-shadow: 0 8px 24px rgba(0,0,0,.16); padding: 12px 14px; z-index: 30; }}
  .fab-pop.is-open {{ display: block; }}
  .fab-pop h5 {{ margin: 0 0 8px; font-size: 0.85rem; color: {header}; }}
  .fab-pop .row {{ display: flex; gap: 8px; align-items: flex-start; padding: 7px 0; border-top: 1px solid {divider_soft}; font-size: 0.82rem; }}
  .fab-pop .row .tx {{ flex: 1; color: {muted}; }}
  details.issue-block {{ margin: 7px 0; }}
  details.issue-block summary {{ list-style: none; cursor: pointer; display: flex; align-items: center; gap: 10px;
    padding: 10px 12px; border-radius: 8px; border: 1px solid {border}; background: {card}; font-size: 0.88rem; }}
  details.issue-block summary::-webkit-details-marker {{ display: none; }}
  details.issue-block summary::before {{ content: "▸"; color: {muted}; font-size: 0.78rem; }}
  details.issue-block[open] summary::before {{ content: "▾"; }}
  details.issue-block summary:hover {{ background: {hover}; }}
  .issue-block .nm {{ font-weight: 600; color: {header}; }}
  .issue-block .kw {{ color: {muted}; font-size: 0.8rem; }}
  .issue-block .n {{ margin-left: auto; font-size: 0.78rem; color: {muted}; }}
  /* [수정: 2026-09-11] 회차 줄이 <a> 하나에서 「체크박스 · 링크 · 🗑」 세 칸(.run-line)이
     됐다 — 여백·밑줄·hover는 바깥 줄이 맡고, 링크(.run-row)는 남은 폭을 채운다. */
  .run-line {{ display: flex; align-items: center; gap: 10px; padding: 0 8px 0 52px; font-size: 0.83rem;
    border-bottom: 1px solid {divider_soft}; min-height: 36px; }}
  .run-line:hover {{ background: {row_hover}; }}
  .run-row {{ flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; padding: 7px 0;
    text-decoration: none; color: {text}; }}
  .st {{ font-size: 0.74rem; border-radius: 10px; padding: 1px 9px; border: 1px solid; flex: none; }}
  .st.done {{ background: {hover}; color: {accent}; border-color: {accent_border}; }}  /* [수정: 2026-08-21] 초록→파랑 */
  .st.wip {{ background: {warn_bg}; color: {warn_accent}; border-color: {warn_border}; }}
  .run-row .n {{ margin-left: auto; color: {muted}; font-size: 0.78rem; }}
  /* [추가: 2026-09-11] 수시 보관함 삭제 — 줄 끝 🗑(하나, 확인창 없음)와 [선택 삭제](여러 개,
     확인창 한 번). 🗑는 확정본 기사 행의 🗑와 같은 무게: 평소 회색, hover만 빨강. */
  .row-del {{ flex: none; border: none; background: none; color: {text_faint}; cursor: pointer;
    padding: 4px 6px; border-radius: 5px; line-height: 1; display: inline-flex; font-size: 0.95rem; }}
  .row-del:hover {{ color: {error}; background: {error_bg}; }}
  /* [추가: 2026-09-15] 사안 줄 🗑(그 사안의 회차 모두, 확인창 한 번) — 회차 줄 🗑와 같은 모양.
     사안 줄이 통째로 「삭제함」이 되면 그 자리에 「모두 되살리기」가 붙는다. */
  .issue-block summary .issue-del {{ margin: 0 -6px 0 -4px; }}
  .issue-block .n .restore-btn {{ margin-left: 6px; }}
  /* 방금 지운 회차 — 제자리에 남는 「삭제함 · 되살리기」 줄(정기 보관함의 같은 줄과 같은 모양). */
  .run-line.gone {{ background: {bg}; color: {text_faint}; }}
  .run-line.gone:hover {{ background: {bg}; }}
  .run-line.gone .run-row, .run-line.gone .run-row .win, .run-line.gone .run-row .n {{ color: {text_faint}; }}
  .gone-tag {{ flex: none; font-size: 0.72rem; border: 1px dashed {dash_border}; border-radius: 10px;
    padding: 0 8px; color: {muted}; background: {card}; font-weight: 400; }}
  .restore-btn {{ flex: none; border: none; background: none; color: {accent}; font: inherit; font-size: 0.78rem;
    cursor: pointer; padding: 2px 4px; margin-left: auto; }}
  .run-line .restore-btn {{ margin-left: 0; }}
  .restore-btn:hover {{ text-decoration: underline; text-underline-offset: 3px; }}
  .run-line.just-restored {{ background: {row_moved}; }}
  .issue-block .nm.noname {{ font-weight: 600; font-style: italic; color: {muted}; }}
  /* 선택 삭제 모드 — body.arch-tidy일 때만 체크박스가 나오고, 꺼내는 동작(복사·txt·xlsx·
     열기)과 줄 끝 🗑는 숨는다. 고른 줄은 확정본에서 체크한 기사 행과 같은 옅은 파랑. */
  input.pick {{ display: none; width: 15px; height: 15px; margin: 0; flex: none; cursor: pointer; }}
  .pick-sp {{ display: none; width: 15px; flex: none; }}
  body.arch-tidy input.pick {{ display: inline-block; }}
  body.arch-tidy .pick-sp {{ display: inline-block; }}
  body.arch-tidy .row-del, body.arch-tidy .exp, body.arch-tidy .run-row .go, body.arch-tidy .normal-only {{ display: none; }}
  .run-line:has(input.pick:checked) {{ background: {hover}; }}
  .tidy-only {{ display: none; }}
  body.arch-tidy .tidy-only {{ display: inline-flex; }}
  .tidy-btn {{ margin-left: auto; border: 1px solid {border}; background: {card}; color: {muted}; border-radius: 6px;
    padding: 6px 13px; font-size: 0.8rem; cursor: pointer; font-family: inherit; white-space: nowrap; }}
  .tidy-btn:hover {{ border-color: {error_border}; color: {error}; background: {error_bg}; }}
  .tidy-bar {{ margin-left: auto; align-items: center; gap: 8px; font-size: 0.78rem; color: {muted}; }}
  .pick-empty {{ border: 1px dashed {dash_border}; background: {card}; color: {text_soft}; border-radius: 6px;
    padding: 5px 11px; font-size: 0.78rem; cursor: pointer; font-family: inherit; }}
  .pick-empty:hover {{ border-color: {accent}; color: {accent}; border-style: solid; }}
  .done-btn {{ border: 1px solid {accent}; background: {accent}; color: {on_fill}; border-radius: 6px;
    padding: 5px 13px; font-size: 0.8rem; cursor: pointer; font-family: inherit; font-weight: 600; }}
  /* 고른 게 있을 때만 뜨는 바 — /hidden 일괄 복구 바·확정본 일괄이동 바와 같은 규칙과 색. */
  .sel-bar {{ display: none; position: sticky; top: 62px; z-index: 5; align-items: center; gap: 10px;
    background: {hover}; border: 1px solid {accent_border}; border-radius: 8px; padding: 8px 12px;
    margin: 0 0 8px; font-size: 0.84rem; color: {header}; box-shadow: 0 2px 8px rgba(15, 23, 42, 0.08); }}
  .sel-bar.on {{ display: flex; }}
  .sel-bar .sp {{ flex: 1; }}
  .sel-bar .clr {{ background: transparent; color: {muted}; border: 1px solid {border}; border-radius: 6px;
    padding: 5px 10px; font-size: 0.8rem; cursor: pointer; font-family: inherit; }}
  /* 맨 아래 「최근 삭제」 — 기본은 접힌 한 줄. */
  details.recent-del {{ margin-top: 16px; border: 1px dashed {dash_border}; border-radius: 10px; background: {bg}; }}
  details.recent-del > summary {{ list-style: none; cursor: pointer; padding: 9px 12px; font-size: 0.82rem;
    color: {muted}; display: flex; align-items: center; gap: 7px; }}
  details.recent-del > summary::-webkit-details-marker {{ display: none; }}
  details.recent-del > summary::before {{ content: "▸"; font-size: 0.72rem; }}
  details.recent-del[open] > summary::before {{ content: "▾"; }}
  .rbatch {{ border-top: 1px solid {border}; padding: 6px 12px 8px; }}
  .rbatch-h {{ display: flex; align-items: center; gap: 8px; font-size: 0.78rem; color: {muted}; margin: 2px 0 4px; }}
  .rrow {{ display: flex; align-items: center; gap: 10px; padding: 4px 0 4px 14px; font-size: 0.82rem; color: {muted}; }}
  .rrow .rnm {{ color: {text}; }}
  .rrow .noname {{ color: {muted}; }}
  /* [추가: 2026-08-18] 🏷 라벨(PRD.md 기능10) — app.renderer/app.preview_renderer의
     라벨 CSS와 같은 앰버 값(MAIN_FLOW_MOCKUP.html --label-bg/--label-text). 이 화면은
     실제 <form> 제출 방식(app/adhoc/renderer.py 상단 라우팅 계약 주석)이라 팝오버
     자체는 열고닫기만 JS로 하고, 붙이기/떼기는 페이지 전체를 새로고침한다. */
  .lab-pop-wrap {{ position: relative; display: inline-flex; }}
  .lab-btn.on {{ color: {label_text}; background: {label_bg}; }}
  .lab-btn .lab-count {{ font-size: 0.62rem; font-weight: 700; margin-left: 2px; }}
  .lab-pop {{
    position: absolute; right: 0; top: 100%; margin-top: 4px; z-index: 60; width: 250px;
    text-align: left; background: {card}; border: 1px solid {border}; border-radius: 8px;
    padding: 11px 12px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.13);
  }}
  .lab-pop.hidden {{ display: none; }}
  .lab-pop h4 {{ margin: 0 0 6px; font-size: 0.66rem; color: {muted}; font-weight: 700; }}
  .lab-pop .known {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }}
  .lab-pop .known .none {{ font-size: 0.66rem; color: {muted}; }}
  .known-chip {{
    font-size: 0.64rem; font-weight: 600; color: {muted}; cursor: pointer;
    background: {card}; border: 1px solid {border}; border-radius: 20px; padding: 2px 8px;
  }}
  .known-chip:hover {{ background: {label_bg}; color: {label_text}; border-color: {label_border}; }}
  .known-chip.used {{ background: {label_bg}; color: {label_text}; border-color: {label_border}; font-weight: 700; }}
  .known-chip .n {{ opacity: 0.6; font-weight: 500; margin-left: 3px; }}
  .lab-pop .addbox {{ display: flex; gap: 5px; }}
  .lab-pop .addbox input {{
    flex: 1; font-size: 0.76rem; padding: 5px 7px; border: 1px solid {border}; border-radius: 5px;
  }}
  .lab-pop .addbox button {{
    font-size: 0.72rem; font-weight: 700; padding: 5px 10px; border-radius: 5px; cursor: pointer;
    background: {label_bg}; color: {label_text}; border: 1px solid {label_border};
  }}
  .lab-row {{ display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin: 4px 0 0 25px; }}
  .lab-row .tagmark {{ color: {label_text}; opacity: 0.75; }}
  .lab-chip {{
    display: inline-flex; align-items: center; gap: 3px; font-size: 0.66rem; font-weight: 700;
    color: {label_text}; background: {label_bg}; border: 1px solid {label_border};
    border-radius: 20px; padding: 2px 8px; white-space: nowrap;
  }}
  .lab-chip form {{ display: inline; }}
  .lab-chip .x-btn {{
    background: none; border: none; padding: 0; margin: 0; color: {label_text}; opacity: 0.55;
    cursor: pointer; font-weight: 700; font-size: 0.85rem; line-height: 1;
  }}
  .lab-chip .x-btn:hover {{ opacity: 1; }}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
{nav}
</div></div>
<div class="container">
{body}
</div>
{script}
</body>
</html>
"""


# [추가: 2026-08-19] 상단바 링크 — 화면마다 갈 수 있는 곳이 달라 각 render_*가 직접
# 정해 넘긴다. 지금 보고 있는 화면 자신은 목록에서 뺀다(정기 확정본이 상단바에
# "확정본" 링크를 안 다는 것과 같은 규칙).
#   /adhoc/card (kind=collect → 수집 원본)   → 홈 · 새 수집 · 수집 확정본 · 수시 보관함
#   /adhoc/card (kind=bundle  → 수집 확정본) → 홈 · 새 수집 · 수집 원본 · 수시 보관함
#   /adhoc/new  (새 수집)                    → 홈 · 수집 원본 · 수집 확정본 · 수시 보관함
#   /adhoc      (수시 보관함)                → 홈
# [수정: 2026-09-03] 이름을 한 칸씩 옮겼다 — 예전엔 입력 폼(/adhoc/new)이 "수집 원본",
# 검색 결과 화면(/adhoc/card)이 "수집 확정본"이라 실제 하는 일과 이름이 어긋나 있었다.
# 지금은 결과 화면이 종류에 따라 원본/확정본으로 갈리고, 폼은 "새 수집"이다.
# "홈"만 정기 화면(home.html)이고 나머지는 수시 안에서 도는 링크다. 수시 화면은
# 전부 설정 서버가 직접 서빙하므로(file:// 로 열리는 정기 화면과 달리) 절대주소가
# 필요없어 상대경로를 쓴다.
NAV_HOME = ("홈", "/home.html")
NAV_NEW = ("새 수집", "/adhoc/new")
NAV_ARCHIVE = ("수시 보관함", "/adhoc")


def nav_html(items: list) -> str:
    """(라벨, href) 목록을 상단바 <a> 줄로. href가 None인 항목은 통째로 뺀다."""
    return "\n".join(
        f'  <a href="{html.escape(href)}">{html.escape(label)}</a>'
        for label, href in items
        if href
    )


def base_page(title: str, body: str, script: str = "", nav: str = "") -> str:
    return _PAGE_TEMPLATE.format(
        title=title,
        body=body,
        script=script,
        nav=nav,
        # [수정: 2026-08-21] 색 값은 전부 app.config.PALETTE 하나에서 온다.
        **PALETTE,
        font_stack=FONT_STACK,
    )


# --- 화면 1: 수집 원본 (ADHOC_DESIGN.md §5.2) ---------------------------------------


def _nav_latest(kind: str, label: str) -> tuple:
    """그 종류의 가장 최근 카드로 가는 상단바 항목. 없으면 href가 None이라 nav_html이
    항목째 뺀다 — 갈 곳이 없는 링크를 흐리게 남겨두지 않는다."""
    card_id = latest_card_id(kind)
    return (label, f"/adhoc/card?id={quote(card_id)}" if card_id else None)


NAV_LATEST_COLLECT = ("collect", "원본")
NAV_LATEST_BUNDLE = ("bundle", "확정본")


def _bundle_nav() -> str:
    """「새 확정본 만들기」 폼의 상단바 — 자기 자신(확정본)은 빼고 원본으로 돌아갈
    길만 둔다."""
    return nav_html([
        NAV_HOME,
        NAV_NEW,
        _nav_latest(*NAV_LATEST_COLLECT),
        NAV_ARCHIVE,
    ])


def render_new_bundle_page(error: Optional[str] = None) -> str:
    """빈 모음 만들기 (ADHOC_DESIGN.md §6.13) — 입력은 이름 한 줄뿐이다.

    검색어·시간대 칸을 **비활성이 아니라 아예 안 그린다**: 모음은 검색을 안 하는
    카드라 그 칸들이 가리킬 대상 자체가 없다. 흐리게 남겨두면 "언젠가 쓸 수 있나"를
    묻게 만든다.
    """
    error_html = f'<div class="warn">{icon("alert")} {html.escape(error)}</div>' if error else ""
    body = f"""
<div class="card">
  <h1 class="page-title">새 확정본 만들기</h1>
  <p class="page-sub">여러 원본에서 고른 기사를 한곳에 모아 하나의 보고서로 묶습니다</p>
  {error_html}

  <form method="POST" action="/adhoc/bundle/create">
    <div class="field">
      <label>확정본 이름</label>
      <input type="text" name="name" maxlength="40" required autofocus
             placeholder="예: 9/2 예결위 보고"
             style="flex:1;min-width:180px;font:inherit;font-size:0.92rem;padding:8px 11px;
                    border:1px solid {COLOR_BORDER};border-radius:8px">
      <button type="submit" class="btn">{icon("in")} 확정본 만들기</button>
    </div>
    <p style="margin:-4px 0 0 88px;font-size:0.78rem;color:{COLOR_TEXT_FAINT};line-height:1.7">
      검색은 하지 않습니다 — 기사는 <b>원본</b> 화면에서 각 기사의
      <b>「확정본으로」</b>로 보내주세요.<br>
      소제목·순서·복사·엑셀은 수집 카드와 똑같이 씁니다.
    </p>
  </form>
</div>
"""
    return base_page("수시 모니터링 — 새 확정본 만들기", body, "", nav=_bundle_nav())


_WEEKDAYS_KR = "월화수목금토일"


def _history_entries() -> list[dict]:
    """[추가: 2026-09-15] 새 수집의 「지난 수집」 — 원본 카드마다 한 줄(최신순), 같은 날·
    같은 사안·같은 검색어·같은 검색 방식은 한 줄로 합쳐 `count`로 센다(실측: 카드 25장 → 16줄).

    보관 기간이 1년이라 목록도 저절로 1년치다. 카드 파일은 요약 캐시(list_card_summaries)로만
    읽는다 — 이 화면은 자주 여는데 카드를 통째로 열면 1년치에서 초 단위가 걸린다.
    `all`은 「검색어가 모두 있는 기사만」으로 만든 카드(검색어 = 꼭 포함할 검색어, 2개 이상),
    `must`는 그와 별개로 나중에 붙인 꼭 포함할 검색어다 — 폼은 둘 중 앞의 것만 되살릴 수 있다.
    """
    entries: list[dict] = []
    index: dict = {}
    for s in list_card_summaries():
        if s.get("kind") != "collect":
            continue
        keywords = [k for k in (s.get("keywords") or []) if k]
        must = [k for k in (s.get("must_keywords") or []) if k]
        all_mode = len(keywords) >= 2 and sorted(must) == sorted(keywords)
        extra_must = [] if all_mode else must
        key = (s.get("collect_date"), s.get("report_title"), tuple(keywords), all_mode, tuple(extra_must))
        if key in index:
            index[key]["count"] += 1
            index[key]["card_ids"].append(s["id"])
            continue
        entry = {
            "date": s.get("collect_date") or "",
            "title": s.get("report_title") or "",
            "issue_id": s.get("issue_id") or "",
            "keywords": keywords,
            "all": all_mode,
            "must": extra_must,
            "count": 1,
            "card_id": s["id"],
            "card_ids": [s["id"]],
        }
        index[key] = entry
        entries.append(entry)
    return entries


def _history_html(entries: list[dict]) -> str:
    """「지난 수집」 카드 — 줄마다 날짜 · 사안명 · 검색어(단어만). 누르면 폼이 채워지고,
    올리면 오른쪽에 「원본 열기 ↗」. 달이 바뀌면 옅은 구분선. 목록이 비면 카드째 안 그린다."""
    if not entries:
        return ""
    this_year = datetime.now().year
    rows = []
    last_month = None
    for i, e in enumerate(entries):
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d")
        except ValueError:
            continue
        month = (d.year, d.month)
        if month != last_month:
            label = f"{d.month}월" if d.year == this_year else f"{d.year}년 {d.month}월"
            rows.append(f'<div class="hm">{label}</div>')
            last_month = month
        kw_html = html.escape(" · ".join(e["keywords"]))
        if e["all"]:
            kw_html += '<span class="h-all" title="검색어가 모두 있는 기사만 가져왔어요">모두</span>'
        if e["must"]:
            kw_html += (
                f' <span class="h-must" title="나중에 붙인 꼭 포함할 검색어 — 누르면 채워지지 않아요">'
                f'+ 꼭 {html.escape(", ".join(e["must"]))}</span>'
            )
        hay = f'{e["title"]} {" ".join(e["keywords"] + e["must"])}'
        count_html = (
            f'<span class="h-x" title="같은 조건으로 {e["count"]}번 모았어요">×{e["count"]}</span>'
            if e["count"] > 1
            else ""
        )
        rows.append(
            f'<div class="hr" data-i="{i}" data-hay="{html.escape(hay)}" onclick="pickHistory(this)" '
            f'title="누르면 이 사안·검색어로 위 칸을 채워요">'
            f'<span class="h-d">{d.month}/{d.day} ({_WEEKDAYS_KR[d.weekday()]})</span>'
            f'<span class="h-is">{html.escape(e["title"])}</span>'
            f'<span class="h-kw">{kw_html}</span>{count_html}'
            f'<a class="h-open" href="/adhoc/card?id={quote(e["card_id"])}" onclick="event.stopPropagation();">'
            f"원본 열기 ↗</a></div>"
        )
    return f"""
<div class="card hist-card">
  <div class="hist-h">
    <h2>지난 수집</h2>
    <span class="hist-sub">누르면 위 칸이 채워져요</span>
    <input type="search" id="hist-q" placeholder="사안명·검색어로 찾기" oninput="filterHistory(this.value)">
  </div>
  <div class="hist" id="hist">{"".join(rows)}<div class="hist-none" id="hist-none" hidden>찾은 수집이 없어요</div></div>
</div>"""


def render_new_card_page(
    issues: Optional[list[dict]] = None, error: Optional[str] = None, kind: str = "", pick: str = ""
) -> str:
    """사안·검색어·시간대 3줄 입력 + 접힌 고급 설정. 입력이 30초 안에 끝나야 한다는
    원칙(ADHOC_DESIGN.md §5.2)에 따라 필수 입력을 최소화했다.

    error: 카드 생성 자체가 실패했을 때(routes.py의 검증 실패) 폼 위에 보여줄 메시지.
    아직 카드가 없어 돌아갈 카드 화면이 없으므로 이 폼에 얹어 다시 보여준다.

    kind="bundle"이면 "빈 모음 만들기" 폼으로 갈라진다(ADHOC_DESIGN.md §6.13) — 같은
    경로의 두 번째 갈래다. 분기 안에서 폼을 조립하지 않고 함수를 통째로 나눈 이유:
    이 함수의 본문과 JS(검색어 칩·검색 방식·시간창 검증)가 전부 "검색을 한다"는 전제
    위에 있어서, if를 흩뿌리면 한 함수가 두 화면을 어중간하게 말하게 된다.
    """
    if kind == "bundle":
        return render_new_bundle_page(error=error)
    issues = issues if issues is not None else load_issues()
    now_hhmm = datetime.now().strftime("%H:%M")

    issue_options = "".join(
        f'<option value="{html.escape(i["id"])}" '
        f'data-keywords=\'{html.escape(__import__("json").dumps(i.get("last_keywords", [])))}\'>'
        f'{html.escape(i["name"])}</option>'
        for i in issues
    )
    error_html = f'<div class="warn">{icon("alert")} {html.escape(error)}</div>' if error else ""

    # [수정: 2026-09-15] 「또는 검색 없이 빈 수집 확정본 만들기 →」 링크는 뺐다 — 확정본은
    # 원본에서 처음 보낼 때 저절로 생기고, 다른 이름의 확정본이 필요하면 원본의 「확정본으로 ▾」
    # 메뉴에서 「+ 새 확정본 만들어 보내기」로 만든다. 이 화면은 조건을 정하는 곳이다.
    history_entries = _history_entries()
    body = f"""
<div class="card" id="new-card">
  <h1 class="page-title">새 수집</h1>
  <p class="page-sub">오늘 안에서 시간대를 정해 한 번에 모읍니다 · 검색어는 최대 {MAX_ADHOC_KEYWORDS}개</p>
  {error_html}

  <form method="POST" action="/adhoc/collect" id="new-card-form">
    <div class="field">
      <label>사안</label>
      <select id="issue-select" name="issue_id" onchange="onIssueChange()">
        <option value="__new__">+ 새 사안 만들기</option>
        {issue_options}
      </select>
      <input type="text" id="new-issue-name" name="new_issue_name" placeholder="새 사안 이름 (예: 재경위)"
             oninput="checkSimilarIssue(this)">
      <div class="issue-warn hidden" id="issue-sim-warn"></div>
    </div>
    <div class="field">
      <label>검색어</label>
      <div class="kw-box" id="kw-box">
        <input type="text" id="kw-input" placeholder="검색어 입력"
               style="border: none; flex: 1; min-width: 140px; padding: 3px 4px;"
               onkeydown="if(event.key==='Enter'){{event.preventDefault(); addKeyword();}}">
        <button type="button" class="kw-add" id="kw-add-btn" onclick="addKeyword()">+ 추가</button>
        <span class="kw-cnt" id="kw-cnt">0 / {MAX_ADHOC_KEYWORDS}</span>
      </div>
      <div id="kw-hidden-inputs"></div>
    </div>
    <div class="kw-mode" id="kw-mode">
      <label class="mode-opt"><input type="radio" name="match_mode" value="any" checked onchange="onModeChange()">
        <span>검색어 중 <b>하나라도</b> 있으면 가져와요</span></label>
      <label class="mode-opt"><input type="radio" name="match_mode" value="all" onchange="onModeChange()">
        <span>검색어가 <b>모두</b> 있는 기사만 가져와요</span></label>
      <div class="mode-note" id="mode-note"></div>
    </div>
    <div class="field">
      <label>수집시간</label>
      <input type="text" class="hm" id="win-start" name="window_start" inputmode="numeric" maxlength="5" placeholder="00:00" value="00:00" required>
      <span class="tilde">~</span>
      <input type="text" class="hm" id="win-end" name="window_end" inputmode="numeric" maxlength="5" placeholder="00:00" value="{now_hhmm}" required>
      <button type="button" class="now-btn" onclick="setNow('win-end')">지금</button>
      <span style="flex:1"></span>
      <button type="submit" class="btn">{icon("search")} 수집</button>
    </div>
    <div class="quick">
      <button type="button" class="qchip" onclick="setQuick('win-start','win-end','midnight')">0시 ~ 지금</button>
      <button type="button" class="qchip" onclick="setQuick('win-start','win-end','1h')">최근 1시간</button>
      <button type="button" class="qchip" onclick="setQuick('win-start','win-end','3h')">최근 3시간</button>
    </div>

    <div class="adv">
      <div class="adv-h">이 회차에만 적용</div>
      <div class="adv-grid">
        <label><input type="checkbox" name="use_outlet_whitelist" value="1"> 등록 언론사만 보기</label>
        <label><input type="checkbox" name="exclude_photo" value="1"> 사진기사 제외</label>
        <label><input type="checkbox" name="exclude_personnel" value="1"> 인사 발령 기사 제외</label>
      </div>
    </div>
  </form>
</div>
{_history_html(history_entries)}
"""

    # 「지난 수집」 줄을 누르면 채울 값 — 사안명은 자유 입력이라 </script>가 섞여도 스크립트가
    # 안 끊기게 "</"를 풀어 쓴다.
    history_json = __import__("json").dumps(
        [{"issue_id": e["issue_id"], "title": e["title"], "keywords": e["keywords"],
          "all": e["all"], "card_ids": e["card_ids"]} for e in history_entries],
        ensure_ascii=False,
    ).replace("</", "<\\/")
    script = f"""<script>
var MAX_ANY = {MAX_ADHOC_KEYWORDS};   // keywords 상한
var MAX_ALL = {MAX_ADHOC_MUST_KEYWORDS};   // must_keywords 상한 — "모두" 모드는 이쪽에 묶인다
var MAX_KEYWORDS = MAX_ANY;
var keywords = [];

function currentMode() {{
  var el = document.querySelector('input[name=match_mode]:checked');
  return el ? el.value : 'any';
}}

function onModeChange() {{
  renderKeywords();
}}

function renderMatchMode() {{
  var mode = currentMode();
  var row = document.getElementById('kw-mode');
  var note = document.getElementById('mode-note');
  var opts = row.querySelectorAll('.mode-opt');

  // 검색어가 1개 이하면 두 선택지가 같은 뜻이라 고를 이유가 없다.
  var usable = keywords.length >= 2;
  row.classList.toggle('off', !usable);
  row.querySelectorAll('input[type=radio]').forEach(function (r) {{ r.disabled = !usable; }});
  opts.forEach(function (o, i) {{
    o.classList.toggle('on', usable && ((i === 0) === (mode === 'any')));
    o.classList.toggle('all', i === 1);
  }});

  // "모두"는 must_keywords로 저장되므로 그쪽 상한(MAX_ALL)에 묶인다. 저장 단계에서
  // 튕기는 대신 누르기 전에 막는다 — 카드가 만들어진 뒤에 실패하면 되돌릴 자리가
  // 화면에 없다.
  var over = mode === 'all' && keywords.length > MAX_ALL;
  note.classList.toggle('is-over', over);
  if (over) {{
    note.textContent = '\u2018모두 있는\u2019은 검색어 ' + MAX_ALL + '개까지예요 \u2014 '
      + (keywords.length - MAX_ALL) + '개를 빼주세요';
  }} else if (usable && mode === 'all') {{
    note.textContent = '예: ' + keywords.slice(0, 2).join(' + ') + ' \u2192 둘 다 들어간 기사만';
  }} else {{
    note.textContent = '';
  }}
  document.querySelector('.btn[type=submit]').disabled = over;
  return over;
}}

function renderKeywords() {{
  MAX_KEYWORDS = currentMode() === 'all' ? MAX_ALL : MAX_ANY;
  document.querySelectorAll('.kw-chip').forEach(function (el) {{ el.remove(); }});
  var box = document.getElementById('kw-box');
  var input = document.getElementById('kw-input');
  keywords.forEach(function (word, idx) {{
    var chip = document.createElement('span');
    chip.className = 'kw-chip';
    chip.innerHTML = word.replace(/</g, '&lt;').replace(/>/g, '&gt;') +
      ' <button type="button" onclick="removeKeyword(' + idx + ')">×</button>';
    box.insertBefore(chip, input);
  }});
  document.getElementById('kw-cnt').textContent = keywords.length + ' / ' + MAX_KEYWORDS;
  input.disabled = keywords.length >= MAX_KEYWORDS;
  document.getElementById('kw-add-btn').disabled = keywords.length >= MAX_KEYWORDS;

  var hidden = document.getElementById('kw-hidden-inputs');
  hidden.innerHTML = '';
  keywords.forEach(function (word) {{
    var h = document.createElement('input');
    h.type = 'hidden'; h.name = 'keywords'; h.value = word;
    hidden.appendChild(h);
  }});

  renderMatchMode();
}}

function addKeyword() {{
  var input = document.getElementById('kw-input');
  var word = input.value.trim();
  if (!word || keywords.length >= MAX_KEYWORDS || keywords.indexOf(word) !== -1) return;
  keywords.push(word);
  input.value = '';
  renderKeywords();
}}

function removeKeyword(idx) {{
  keywords.splice(idx, 1);
  renderKeywords();
}}

function onIssueChange() {{
  var select = document.getElementById('issue-select');
  var nameInput = document.getElementById('new-issue-name');
  var isNew = select.value === '__new__';
  nameInput.style.display = isNew ? '' : 'none';
  nameInput.required = isNew;
  if (!isNew) {{
    var opt = select.options[select.selectedIndex];
    try {{
      var last = JSON.parse(opt.dataset.keywords || '[]');
      keywords = last.slice(0, MAX_KEYWORDS);
      renderKeywords();
    }} catch (e) {{}}
  }}
}}

// [추가: 2026-08-25] 같은 사안을 매번 새 이름으로 타이핑하면(예: "예결위" → "예결위 상정")
// 보관함에서 다른 사안으로 갈라진다 — get_or_create_issue가 이름 완전 일치로만 묶기
// 때문(app/adhoc/card.py). 완전 차단하지 않고 라벨의 "비슷한 라벨 있어요"와 같은 방식으로
// 안내만 한다(막으면 정말 새 사안인 경우를 못 만든다).
var ISSUE_NAMES = {__import__("json").dumps([i["name"] for i in issues], ensure_ascii=False)};
function issueEditDistance1(a, b) {{
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
function checkSimilarIssue(input) {{
  var warn = document.getElementById('issue-sim-warn');
  var typed = input.value.trim();
  if (!typed) {{ warn.classList.add('hidden'); return; }}
  var match = ISSUE_NAMES.find(function (name) {{
    if (name === typed) {{ return false; }}
    return issueEditDistance1(name, typed) || name.indexOf(typed) === 0 || typed.indexOf(name) === 0;
  }});
  if (!match) {{ warn.classList.add('hidden'); return; }}
  warn.classList.remove('hidden');
  var safe = match.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  var attrSafe = safe.replace(/"/g, '&quot;');
  warn.innerHTML = '비슷한 사안 <b>"' + safe + '"</b>이 있어요.'
    + '<button type="button" class="use" onclick="useSimilarIssue(this)" data-name="' + attrSafe + '">' + safe + ' 선택</button>'
    + '<button type="button" class="keep" onclick="document.getElementById(\\'issue-sim-warn\\').classList.add(\\'hidden\\');">새로 만들기</button>';
}}
function useSimilarIssue(btn) {{
  var name = btn.dataset.name;
  var select = document.getElementById('issue-select');
  var opt = Array.prototype.find.call(select.options, function (o) {{ return o.textContent === name; }});
  if (opt) {{
    select.value = opt.value;
    onIssueChange();
  }}
  document.getElementById('issue-sim-warn').classList.add('hidden');
}}

{_TIME_INPUT_JS}
attachHmInput('win-start');
attachHmInput('win-end');

document.getElementById('new-card-form').addEventListener('submit', function (e) {{
  if (keywords.length === 0) {{
    e.preventDefault();
    alert('검색어를 하나 이상 입력해주세요.');
    return;
  }}
  if (renderMatchMode()) {{  // "모두"인데 상한을 넘음 — 버튼 말고 엔터 제출도 막는다
    e.preventDefault();
    return;
  }}
  if (!validWindow('win-start', 'win-end')) {{
    e.preventDefault();
  }}
}});

// [추가: 2026-09-15] 「지난 수집」 — 줄을 누르면 그 사안·검색어·검색 방식으로 위 칸을 채운다.
// 수집 시간은 안 가져온다(지난날의 시간창은 오늘 의미가 없다). 사안이 목록에 없으면(사안
// 정보가 지워진 옛 카드) 「새 사안」 칸에 그 이름을 적어 둔다 — 같은 이름이면 서버가 같은
// 사안으로 묶는다(get_or_create_issue).
var HISTORY = {history_json};
function pickHistory(row) {{
  var h = HISTORY[+row.dataset.i];
  if (!h) return;
  var select = document.getElementById('issue-select');
  var known = Array.prototype.some.call(select.options, function (o) {{ return o.value === h.issue_id; }});
  select.value = known ? h.issue_id : '__new__';
  onIssueChange();
  if (!known) document.getElementById('new-issue-name').value = h.title;
  var mode = document.querySelector('input[name=match_mode][value=' + (h.all ? 'all' : 'any') + ']');
  if (mode) mode.checked = true;
  keywords = h.keywords.slice(0, h.all ? MAX_ALL : MAX_ANY);
  renderKeywords();
  document.querySelectorAll('#hist .hr.picked').forEach(function (r) {{ r.classList.remove('picked'); }});
  row.classList.add('picked');
  var form = document.getElementById('new-card');
  form.classList.remove('flash'); void form.offsetWidth; form.classList.add('flash');
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
}}
function filterHistory(q) {{
  q = q.trim();
  var any = false, sep = null, sepHasRow = false;
  function closeSep() {{ if (sep) sep.hidden = !sepHasRow; }}
  document.querySelectorAll('#hist > .hm, #hist > .hr').forEach(function (el) {{
    if (el.classList.contains('hm')) {{ closeSep(); sep = el; sepHasRow = false; return; }}
    var show = !q || el.dataset.hay.indexOf(q) !== -1;
    el.hidden = !show;
    if (show) {{ any = true; sepHasRow = true; }}
  }});
  closeSep();
  var none = document.getElementById('hist-none');
  if (none) none.hidden = any;
}}
onIssueChange();
renderKeywords();
// ?from=<카드 id> — 지난 원본의 「이 조건으로 오늘 새로 모으기」가 이 화면을 채운 채로 연다.
(function () {{
  var pick = {__import__("json").dumps(pick)};
  if (!pick) return;
  var i = HISTORY.findIndex(function (h) {{ return h.card_ids.indexOf(pick) !== -1; }});
  var row = i === -1 ? null : document.querySelector('#hist .hr[data-i="' + i + '"]');
  if (row) {{ pickHistory(row); row.scrollIntoView({{ block: 'nearest' }}); window.scrollTo(0, 0); }}
}})();
</script>"""

    # 원본·확정본 둘 다 "가장 최근 것"으로 보낸다 — 그 종류의 카드가 하나도 없으면
    # (nav_html이 None을 걸러내) 링크 자체가 안 나온다. 갈 데가 없는 링크를 회색으로
    # 남겨두는 것보다 아예 없는 편이 낫다(정기가 그 화면 자신인 링크를 통째로 빼는
    # 것과 같은 판단).
    nav = nav_html([
        NAV_HOME,
        _nav_latest(*NAV_LATEST_COLLECT),
        _nav_latest(*NAV_LATEST_BUNDLE),
        NAV_ARCHIVE,
    ])
    return base_page("수시 모니터링 — 새 수집", body, script, nav=nav)


# --- 화면 2: 카드 정리 (ADHOC_DESIGN.md §5.3) -------------------------------------


def _render_issue_tabs(card: dict, today_cards: list[dict]) -> str:
    """"오늘의 수집 결과" 사안명 탭 — 실제 <a href> 링크라 fetch 없이 그 카드
    화면(GET /adhoc/card?id=…)으로 이동한다.

    [수정: 2026-08-25] "보관" 상태는 없앴다(카드 자체가 이미 항상 수시 보관함에
    있어 "보관 처리"할 게 없었고, 아무도 안 눌렀다 — HISTORY.md 같은 항목). 대신
    탭마다 × 로 그 카드를 바로 삭제할 수 있다 — × 를 브라우저 탭 닫기로 오인해
    "치우기"를 기대하는 사용자는 없다고 보되(사용자 판단), deleteCard()의 확인창이
    오클릭을 막는다([수정: 2026-09-11] 지운 카드는 수시 보관함에서 되살릴 수 있게 됐다 —
    확인창 문구도 "되돌릴 수 없어요"에서 "거기서 되살릴 수 있어요"로 바뀌었다). 헤더 옆
    안내 문구는 그 확인창을 보기도 전에 먼저 안심시키는 역할(둘이 같은 사실을
    양쪽에서 말해줘야 "삭제 안 하면 다 남는다"가 직관적으로 읽힌다).

    [수정: 2026-08-25] 예전엔 카드가 2개 이상일 때만 그렸는데(1개면 헛클릭이라),
    이제 1개여도 그린다 — 이 줄이 삭제(×)와 "+ 새 수집"이 사는 유일한 자리가 됐고,
    카드가 하나뿐인 날에 그 둘이 통째로 사라지면 안 되기 때문. 탭이 하나뿐이면
    "지금 보는 카드"를 가리키는 라벨 역할을 하므로 헛클릭이라 볼 수도 없다.
    """
    # [수정: 2026-09-15] 같은 종류끼리만 탭으로 선다 — 원본에서 처음 보낼 때 같은 이름의
    # 확정본이 저절로 생기므로, 섞어 두면 「인사청문회 183건」·「인사청문회 12건」이 나란히
    # 서 무엇이 원본인지 탭만으론 알 수 없다. 반대 종류로 가는 길은 원본의 🗂️ 알약·확정본의
    # 📥 칩·상단바가 맡는다.
    today_cards = [c for c in today_cards if card_kind(c) == card_kind(card)]
    if not today_cards:
        return ""
    tabs = []
    for c in today_cards:
        visible_count = sum(1 for a in c["articles"] if not a.get("hidden"))
        is_on = c["id"] == card["id"]
        cls = "tab on" if is_on else "tab"
        cid = html.escape(c["id"])
        # 확정본은 불러올 때마다 같은 이름으로 새로 생기므로 시각을 곁들인다(card.bundle_label).
        tab_name = bundle_label(c) if is_bundle(c) else c["report_title"]
        tabs.append(
            f'<a class="{cls}" href="/adhoc/card?id={cid}">'
            f'{html.escape(tab_name)} <span class="n">{visible_count}건</span>'
            f'<span class="del" title="이 카드 삭제" '
            f"onclick=\"event.preventDefault();event.stopPropagation();deleteCard('{cid}')\">×</span>"
            f"</a>"
        )
    # [수정: 2026-08-25] 새 창에서 연다 — 지금 정리 중인 카드를 덮어쓰고 빈 입력
    # 화면으로 넘어가 버리는 게 어색하다는 사용자 지적. 다른 탭들은 "같은 자리에서
    # 갈아타기"라 그대로 두고, 이것만 성격이 달라(만들기) 새 창으로 뗀다.
    tabs.append('<a class="tab add" href="/adhoc/new" target="_blank" rel="noopener">+ 새 수집</a>')
    # 원본은 수시 보관함에 안 쌓인다(새 수집의 「지난 수집」에서 다시 찾는다) — 옛 문구
    # 「모든 수집은 수시 보관함에 저장돼요」는 확정본에만 맞는 말이 됐다.
    heading = (
        "오늘의 확정본 · 수시 보관함에 저장돼요"
        if is_bundle(card)
        else "오늘의 원본" if is_raw(card)
        else "오늘의 수집 결과 · 모든 수집은 수시 보관함에 저장돼요"
    )
    return f"""<div class="tabwrap">
  <div class="tabwrap-h">{icon("calendar")} {heading}</div>
  <div class="tabs">{"".join(tabs)}</div>
</div>"""


def _keyword_counts(card: dict) -> dict:
    """검색어별로 실제 걸린 기사 수를 센다(숨김 포함 — "검색에 몇 건 걸렸는지"를 보여주는
    용도라 화면에서 뺀 것과는 별개). 재수집으로 병합만 하고 검색어 구성 자체는 안 바뀌므로
    matched_keywords 저장값을 그대로 합산하면 된다(collector.run_collect 참고)."""
    counts: dict = {kw: 0 for kw in card["keywords"]}
    # 조건 밖 기사는 화면에 없으므로 건수에서도 뺀다 — 칩의 숫자와 목록의 건수가
    # 어긋나면 담당자가 어느 쪽을 믿어야 할지 알 수 없다.
    for article in articles_in_condition(card):
        for kw in article.get("matched_keywords", []):
            if kw in counts:
                counts[kw] += 1
    return counts


def _kw_chips_html(card: dict, field: str) -> str:
    """검색어 칩 하나하나가 "빼기" 폼이다. 수시 화면은 전부 실제 <form> 제출 + 303
    리다이렉트라(fetch 아님) 칩의 × 도 같은 방식이어야 한다 — 폼은 중첩할 수 없으므로
    칩 각각이 독립된 작은 폼이고, 아래 추가 입력칸도 형제 폼이다."""
    card_id = html.escape(card["id"])
    is_must = field == "must_keywords"
    action = "remove-must-keyword" if is_must else "remove-keyword"
    cls = " must" if is_must else ""
    return "".join(
        f'<form method="POST" action="/adhoc/card/{action}" class="kw-chip{cls}">'
        f'<input type="hidden" name="id" value="{card_id}">'
        f'<input type="hidden" name="keyword" value="{html.escape(word)}">'
        f'{html.escape(word)} <button type="submit" title="빼기">×</button></form>'
        for word in card.get(field, [])
    )


def _render_edit_panel(card: dict, quick_chips_html: str, recollect_disabled: str,
                       recollect_end_value: str) -> str:
    """ADHOC_DESIGN.md §6.4a — 접히는 검색어 편집 패널.

    검색어(OR)는 추가하면 기사가 늘고, 꼭 포함할 검색어(AND)는 추가하면 줄어든다.
    두 줄의 색을 갈라(파랑/앰버) 방향이 반대라는 걸 눈으로 알 수 있게 했고, 화면에
    OR/AND라는 말은 쓰지 않는다 — 담당자에게 검색 문법을 가르칠 이유가 없다.
    """
    card_id = html.escape(card["id"])
    base_full = len(card["keywords"]) >= MAX_ADHOC_KEYWORDS
    must_full = len(card.get("must_keywords", [])) >= MAX_ADHOC_MUST_KEYWORDS
    return f"""
<div class="edit-panel" id="edit-panel">
  <div class="field">
    <label>검색어</label>
    <div class="kw-box">
      {_kw_chips_html(card, "keywords")}
      <form method="POST" action="/adhoc/card/add-keyword" class="kw-addform">
        <input type="hidden" name="id" value="{card_id}">
        <input type="text" name="keyword" placeholder="검색어 입력" maxlength="30"
          {"disabled" if base_full else ""}>
        <button type="submit" class="kw-add" {"disabled" if base_full else ""}>+ 추가</button>
      </form>
      <span class="kw-cnt">{len(card["keywords"])} / {MAX_ADHOC_KEYWORDS}</span>
    </div>
  </div>
  <div class="kw-hint">하나라도 걸리면 가져와요 · 추가하면 기사가 늘어나요</div>

  <div class="field">
    <label class="must-label">꼭 포함할<br>검색어</label>
    <div class="kw-box must">
      {_kw_chips_html(card, "must_keywords")}
      <form method="POST" action="/adhoc/card/add-must-keyword" class="kw-addform">
        <input type="hidden" name="id" value="{card_id}">
        <input type="text" name="keyword" placeholder="예: 공공기관" maxlength="30"
          {"disabled" if must_full else ""}>
        <button type="submit" class="kw-add must" {"disabled" if must_full else ""}>+ 추가</button>
      </form>
      <span class="kw-cnt">{len(card.get("must_keywords", []))} / {MAX_ADHOC_MUST_KEYWORDS}</span>
    </div>
  </div>
  <div class="kw-hint">이 말이 다 들어간 기사만 남겨요 · 추가하면 기사가 줄어들어요</div>

  <form method="POST" action="/adhoc/card/recollect" id="recollect-form" class="field" style="align-items:flex-start">
    <input type="hidden" name="id" value="{card_id}">
    <label style="padding-top:7px">수집 시간</label>
    <div style="display:flex;flex-direction:column;gap:8px;flex:1;min-width:0">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <input type="text" class="hm" id="recol-start" name="window_start" inputmode="numeric" maxlength="5"
          value="{html.escape(card["window"]["start"])}" {recollect_disabled}>
        <span class="tilde">~</span>
        <input type="text" class="hm" id="recol-end" name="window_end" inputmode="numeric" maxlength="5"
          value="{html.escape(recollect_end_value)}" {recollect_disabled}>
        <button type="button" class="now-link" onclick="setNow('recol-end')" {recollect_disabled}>지금</button>
      </div>
      {quick_chips_html}
      <div class="recollect-action">
        <span class="recollect-hint">지금 조건으로 목록을 새로 만들어요</span>
        <button type="submit" class="btn" {recollect_disabled}>{icon("refresh")} 다시 불러오기</button>
      </div>
    </div>
  </form>
</div>"""


def _render_bundle_meta_chips(card: dict) -> str:
    """모음 카드 상단 — 「어느 원본에서 몇 건 왔는지」 (ADHOC_DESIGN.md §6.13).

    검색어·시간창·수집 이력 칩은 안 그린다: 전부 검색 조건이 있어야 뜻이 서는 것들이다.
    대신 칩 하나하나가 **그 원본 카드로 가는 링크**다 — 모음에서 "이거 어디서 왔더라,
    더 없나"가 생기는 유일한 자리이고, 칩에 이미 원본 이름이 적혀 있으므로 새 UI를
    만들 게 아니라 링크만 걸면 된다. 이름은 저장된 sent_from.report_title이라 원본
    카드가 1년 뒤 삭제돼도 이 표시가 안 깨진다(다만 링크는 그때 404가 된다 —
    "그 카드는 이제 없다"가 사실 그대로다).
    """
    sources = bundle_sources(card)
    if not sources:
        return (
            '<div class="meta-row"><span class="chip">아직 보내온 기사가 없습니다 — '
            '원본 화면에서 「확정본으로」로 넣어주세요.</span></div>'
        )
    total = sum(1 for a in card["articles"] if not a.get("hidden"))
    chips = "".join(
        f'<a class="chip src" href="/adhoc/card?id={quote(source_id)}" '
        f'title="「{html.escape(name)}」 원본 카드 열기">{icon("in")} {html.escape(name)} {n}건</a>'
        for source_id, name, n in sources
    )
    return f'<div class="meta-row">{chips}<span class="chip">전체 {total}건</span></div>'


# 수집 이력이 이만큼 쌓이면 📥 칩은 첫·마지막만 보여주고 전체는 툴팁으로 돌린다.
_LOG_COMPACT_FROM = 4


def _collect_log_text(log: list[dict]) -> tuple[str, str]:
    """📥 칩 글자와 툴팁(없으면 빈 문자열).

    [수정: 2026-09-15] 헤더 「지금까지 불러오기」로 누르기가 쉬워지면 이력이 한 줄씩
    길어진다. 그래서 둘을 줄였다:
      ① 수집한 시각과 끝 시각이 같으면 `(~10:15)`를 뺀다 — 「지금까지」로 불러오면 늘
         같아 같은 말을 두 번 하는 셈이다. 과거 시간창(예: 10:15에 ~10:00)으로 불러온
         줄만 괄호가 남아, 괄호가 곧 "이건 지금까지가 아니었다"는 표시가 된다.
      ② _LOG_COMPACT_FROM번째부터 `10:06 6건 · … · 10:59 +1건 (6회)` — 첫 수집(바탕)과
         마지막(방금 무엇이 왔나)만 남기고 전체는 툴팁에 한 줄씩.
    """
    entries = []
    for i, entry in enumerate(log):
        at = entry["at"][11:16]
        end_part = "" if at == entry["window_end"] else f" (~{entry['window_end']})"
        entries.append(f"{at}{end_part} {'+' if i > 0 else ''}{entry['added']}건")
    if not entries:
        return "아직 수집 전", ""
    if len(entries) >= _LOG_COMPACT_FROM:
        return f"{entries[0]} · … · {entries[-1]} ({len(entries)}회)", "\n".join(entries)
    return " · ".join(entries), ""


def _render_meta_chips(card: dict) -> str:
    if is_bundle(card):
        return _render_bundle_meta_chips(card)
    # 검색어가 2개 이상이고 한 번이라도 수집됐으면 검색어별 건수를 함께 보여준다
    # (예: "치킨 33 · 햄버거 4 · bcc 0") — 오타 검색어가 0건으로 조용히 묻히지 않게.
    # 검색어가 1개뿐이면 아래 📥 칩의 총건수와 같은 정보라 그냥 이름만 보여준다.
    if len(card["keywords"]) >= 2 and card["collect_log"]:
        counts = _keyword_counts(card)
        keywords_str = " · ".join(f"{html.escape(k)} {counts[k]}" for k in card["keywords"])
    else:
        keywords_str = " · ".join(html.escape(k) for k in card["keywords"])
    if card.get("must_keywords"):
        keywords_str += f' <span style="color:{COLOR_SEP_FAINT}">+</span> ' + " · ".join(
            f'<span class="must-kw">{html.escape(k)}</span>' for k in card["must_keywords"]
        )
    _win = window_of(card)
    window_str = f"{_win['start']} ~ {_win['end']}"
    log_str, log_title = _collect_log_text(card["collect_log"])
    log_title_attr = f' title="{html.escape(log_title)}"' if log_title else ""
    return f"""<div class="meta-row">
  <span class="chip">{icon("search")} {keywords_str}</span>
  <span class="chip">{icon("clock")} {window_str}</span>
  <span class="chip"{log_title_attr}>{icon("in")} {log_str}</span>
  <button type="button" class="edit-btn" id="edit-toggle" onclick="toggleCardEdit()">
    {icon("pencil")} <span>편집</span></button>
</div>"""


def _render_article_row(
    card: dict, article: dict, group_names: list[str], is_first: bool, is_last: bool,
    known_labels_html: str = "", sent_to: str = "", bundle_options_html: str = "",
    is_new: bool = False, raw_menu_html: str = "", raw_hidden_tip: str = "",
) -> str:
    """sent_to: 이 기사를 이미 받아간 모음 이름(없으면 빈 문자열). 저장된 값이 아니라
    card.sent_index가 매번 다시 계산한 값이다 — 모음에서 숨기면 그 즉시 풀려야 하므로
    (ADHOC_DESIGN.md §6.13 규칙 9).

    bundle_options_html: 「모음으로 ▾」 드롭다운에 채울 <option> 목록. 빈 문자열이면
    그 드롭다운 자체를 안 그린다(모음 카드 자신 · 자정 잠긴 카드).

    is_new: 마지막 불러오기로 새로 들어온 기사면 True — 노랑(.is-new-arrival)으로 칠한다
    (card.last_collect_new_urls).

    raw_menu_html: [추가: 2026-09-15] 로데이터 원본(card.is_raw)의 「확정본으로 ▾」 메뉴에
    들어갈 버튼들. 원본 행엔 「다른 소제목」·숨기기·🏷 라벨이 없고, 보내기 한 가지만 남는다
    (판단은 전부 확정본에서 — 수시 4단 흐름). 빈 문자열이면(잠긴 카드) 보내기 버튼도 없다.

    raw_hidden_tip: [추가: 2026-09-15] 로데이터 원본에서 「숨김」 표시가 붙은 기사면 그 툴팁
    (원본에서 🗑 / 확정본에서 뺌 — card.raw_row_states). 실시간현황의 🗑와 같은 모양이다:
    행은 제자리에 흐리게 + 취소선으로 남고, 보내기 자리가 「🗑 숨김」 버튼이 돼 다시 누르면
    되돌린다."""
    raw = is_raw(card)
    settings = load_settings()
    highlight_words = settings.get("highlight_keywords", [])
    # [추가: 2026-09-02] 제목 맨 앞 [단독]/[속보]는 배지로 감싸지 않고 글자색만 바꾼다 —
    # 정기 확정본·실시간현황과 같은 방식(제목 문자열 자체는 그대로라 복사·txt·엑셀
    # 텍스트엔 영향이 없다, CODING_CONVENTIONS.md §4). 말머리 구간과 나머지를 나눠
    # 각자 highlight_keywords를 태워야 형광펜과 겹칠 때 서로 안 지운다.
    _kind = headline_kind(article["title"])
    if _kind:
        _prefix_len = HEADLINE_TAG_RE.match(article["title"]).end()
        _headline_cls = "t-scoop" if _kind == "단독" else "t-flash"
        title_html = (
            f'<span class="{_headline_cls}">'
            f'{highlight_keywords(article["title"][:_prefix_len], highlight_words)}</span>'
            f'{highlight_keywords(article["title"][_prefix_len:], highlight_words)}'
        )
    else:
        title_html = highlight_keywords(article["title"], highlight_words)
    # 표식 없는 통신사 사진기사 추정 — 정기와 같은 함수·같은 칩. 지우지 않고 흐리게만
    # 한다(판정이 틀렸을 때 담당자가 그 자리에서 확인할 수 있어야 한다).
    is_photo = looks_like_photo_caption(article)
    photo_badge_html = (
        f'<span class="photo-badge" title="{html.escape(photo_badge_tip(article["title"]))}">'
        f'{icon("camera")} 사진 추정</span>'
        if is_photo
        else ""
    )
    out_of_window = article_out_of_window(article, window_of(card))
    out_tag = f'<span class="out-tag">구간 밖 · {article["pub_date"][11:16]}</span>' if out_of_window else ""
    other_group_options = "".join(
        f'<option value="{html.escape(g)}">{html.escape(g)}</option>'
        for g in group_names
        if g != article.get("group")
    )

    copy_text = f'{apply_line_template(settings.get("article_line_template", "ㅇ ({outlet}) {title}"), article["outlet"], article["title"])}\n{article["url"]}'
    card_id = html.escape(card["id"])
    url = html.escape(article["url"])

    # [추가: 2026-08-18] 🏷 라벨 — PRD.md 기능10 규칙8, 라벨을 붙이는 세 화면(확정본·
    # 초안·수시 수집 확정본) 중 마지막 하나. 이 화면은 fetch가 아니라 실제 form 제출
    # 방식(위 라우팅 계약 주석)이라 붙이기/떼기도 다른 액션들과 똑같이 post()로 전체
    # 페이지를 새로고침한다 — 팝오버가 열고닫기 상태를 유지 못 하는 대신, 이 화면의
    # 다른 모든 조작(숨기기·이동 등)과 동작 방식이 완전히 일관된다.
    current_labels = labels_for_url(article["url"])
    lab_count_html = f'<span class="lab-count">{len(current_labels)}</span>' if current_labels else ""
    attached_html = "".join(
        f'<span class="lab-chip" data-label="{html.escape(name)}">'
        f"{html.escape(name)} "
        f'<form method="POST" action="/adhoc/card/remove-label">'
        f'<input type="hidden" name="id" value="{card_id}">'
        f'<input type="hidden" name="url" value="{url}">'
        f'<input type="hidden" name="label" value="{html.escape(name)}">'
        '<button type="submit" class="x-btn" title="라벨 떼기">×</button>'
        "</form></span>"
        for name in current_labels
    )
    lab_row_html = (
        f'<div class="lab-row"><span class="tagmark">{icon("tag")}</span>{attached_html}</div>'
        if current_labels
        else ""
    )
    attached_pop_html = attached_html or '<span class="none">아직 없음</span>'
    lab_html = (
        '<span class="lab-pop-wrap">'
        f'<button type="button" class="icon-btn lab-btn{" on" if current_labels else ""}" '
        f'data-id="{card_id}" data-url="{url}" data-outlet="{html.escape(article["outlet"])}" '
        f'data-title="{html.escape(article["title"])}" data-pub-date="{html.escape(article.get("pub_date") or "")}" '
        f'data-group="{html.escape(article.get("group") or "")}" '
        'onclick="event.stopPropagation(); toggleLabelPopover(this);" '
        f'title="라벨">{icon("tag")}{lab_count_html}</button>'
        '<span class="lab-pop hidden" onclick="event.stopPropagation();">'
        "<h4>이 기사에 붙은 라벨</h4>"
        f'<div class="attached">{attached_pop_html}</div>'
        "<h4>이미 쓴 라벨 (눌러서 붙이기)</h4>"
        f'<div class="known">{known_labels_html}</div>'
        '<div class="addbox">'
        f'<form method="POST" action="/adhoc/card/add-label" style="display:flex;gap:5px;flex:1;">'
        f'<input type="hidden" name="id" value="{card_id}">'
        f'<input type="hidden" name="url" value="{url}">'
        f'<input type="hidden" name="outlet" value="{html.escape(article["outlet"])}">'
        f'<input type="hidden" name="title" value="{html.escape(article["title"])}">'
        f'<input type="hidden" name="pubDate" value="{html.escape(article.get("pub_date") or "")}">'
        f'<input type="hidden" name="group" value="{html.escape(article.get("group") or "")}">'
        '<input type="text" name="label" placeholder="새 라벨 입력" required>'
        "<button type=\"submit\">추가</button>"
        "</form>"
        "</div>"
        "</span>"
        "</span>"
    )

    # [추가: 2026-08-19] 요약문 펼쳐보기 — app/renderer.py render_article과 같은 구조
    # (<details><summary>제목·액션</summary><p>요약</p></details>). 데이터는 이미
    # collector가 카드 JSON에 저장해 두고 있어(app/adhoc/collector.py) 화면만 새로
    # 그리면 된다. summary 안에 폼 버튼·체크박스·드롭다운이 그대로 들어가므로, 각각에
    # event.stopPropagation()을 붙여야 버튼을 누를 때 요약이 같이 펼쳐지지 않는다.
    summary_text = article.get("summary", "")
    summary_html = (
        highlight_keywords(summary_text, highlight_words)
        if summary_text
        else "저장된 요약이 없습니다."
    )
    summary_class = "a-summary" if summary_text else "a-summary none"

    # [추가: 2026-09-02] 게시시각 뒤 "N분 전" — 서버가 굳혀두면 시간이 지날수록 틀린
    # 값이 되므로 data-pub-date만 실어두고 텍스트는 JS(renderRelativeTimes)가 채운다.
    # 정기 _format_pub_time과 같은 방식이다.
    pub_iso = html.escape(article.get("pub_date") or "")
    time_html = (
        f'<span class="a-time">{article["pub_date"][11:16]} · '
        f'<span class="a-time-rel" data-pub-date="{pub_iso}"></span></span>'
        if pub_iso
        else ""
    )
    # 🔍 이 기사가 걸린 검색어 — 값이 없으면(옛 카드, 직접 추가한 기사) 아무 것도 안
    # 그린다. 없는 값을 지어내지 않는다.
    matched = [k for k in (article.get("matched_keywords") or []) if k]
    kw_inline_html = (
        '<span class="a-time-sep">·</span>'
        '<span class="kw-inline" title="이 기사가 걸린 검색어">'
        f'{icon("search", "ic kw-ic")}'
        f'<span class="kw-v">{html.escape(", ".join(matched))}</span></span>'
        if matched
        else ""
    )
    # ⋯ 더보기 — 이 화면 관례대로 실제 <form> 제출이다(정기는 fetch+204). 두 항목 다
    # app.summary_overrides(URL 키 전역)에 저장하므로, 여기서 고친 제목은 정기 확정본·
    # 정기 보관함에서도 같이 보인다 — 담기는 값이 "이 URL의 진짜 제목"이라는 사실
    # 하나라서 흐름을 가로질러 공유하는 게 맞다(📌 담아두기가 옮기던 "담당자의 판단"과
    # 다른 점). 화면에 입히는 지점은 _article_groups 한 곳이다.
    edit_id = f"edit-{abs(hash(article['url'])) % (10 ** 10)}"
    more_menu_html = (
        '<span class="more-wrap">'
        '<button type="button" class="icon-btn more-btn" '
        'onclick="event.stopPropagation(); toggleArticleMenu(this)" '
        f'title="더보기" aria-label="더보기">{icon("dots")}</button>'
        '<span class="more-menu" onclick="event.stopPropagation();">'
        f'<form method="POST" action="/adhoc/card/refetch-summary">'
        f'<input type="hidden" name="id" value="{card_id}">'
        f'<input type="hidden" name="url" value="{url}">'
        '<button type="submit">원문 다시 불러오기</button>'
        "</form>"
        f'<button type="button" data-target="{edit_id}" '
        'onclick="event.stopPropagation(); openEditSummary(this)">기사제목 직접 수정</button>'
        "</span>"
        "</span>"
    )
    edit_box_html = (
        f'<div class="edit-box hidden" id="{edit_id}">'
        f'<form method="POST" action="/adhoc/card/edit-summary">'
        f'<input type="hidden" name="id" value="{card_id}">'
        f'<input type="hidden" name="url" value="{url}">'
        '<label>기사 제목</label>'
        f'<input type="text" name="title" value="{html.escape(article["title"])}">'
        '<label>요약</label>'
        f'<textarea name="summary">{html.escape(summary_text)}</textarea>'
        '<div class="acts">'
        f'<button type="button" class="sel-sm" onclick="closeEditSummary(\'{edit_id}\')">취소</button>'
        '<button type="submit" class="sel-sm">저장</button>'
        "</div></form></div>"
    )

    # ADHOC_DESIGN.md §6.13 「보낸 기사 표시」 — 띠(.is-sent) + 내용 흐리기(CSS) + 칩.
    # 칩을 .a-title 밖(.a-top의 형제)에 두는 게 중요하다: 안에 넣으면 제목과 함께
    # 흐려져, 정작 눈에 걸려야 할 신호가 약해진다("신호는 100%, 내용만 55%").
    sent_tag_html = (
        f'<span class="sent-tag" title="이미 「{html.escape(sent_to)}」 확정본으로 보낸 기사입니다">'
        f'{icon("check")} {html.escape(sent_to)}로 보냄</span>'
        if sent_to and not raw
        else ""
    )
    if raw:
        # [추가: 2026-09-15] 원본의 보내기 — 한 번 누르면 「이 원본의 확정본」(없으면 그 순간
        # 새로)으로 간다. 다른 확정본을 고르는 길은 ▾ 뒤로. 보낸 기사는 칩 대신 이 자리가
        # 「✓ 보냄」이 된다 — 실시간현황의 「📌 담아두기 ↔ ✔️ 스크랩됨」과 같은 모양이다.
        if sent_to:
            bundle_select_html = (
                f'<button type="button" class="send-done" disabled '
                f'title="이미 「{html.escape(sent_to)}」 확정본으로 보낸 기사예요 — '
                f'빼려면 그 확정본에서 🗑를 누르면 돼요">{icon("check")} 보냄</button>'
            )
        elif raw_hidden_tip:
            # 잠긴(어제) 카드에선 표시만 — 되돌려도 보낼 수 없어서다.
            bundle_select_html = (
                f'<button type="button" class="send-done raw-unhide" data-id="{card_id}" data-url="{url}" '
                f'title="{html.escape(raw_hidden_tip)}" '
                + ('onclick="event.stopPropagation(); rawUnhide(this)"' if raw_menu_html else "disabled")
                + f'>{icon("trash")} 숨김</button>'
            )
        elif raw_menu_html:
            bundle_select_html = (
                f'<span class="send-split" data-id="{card_id}" data-url="{url}">'
                '<button type="button" class="send-go" data-bundle="__auto__" '
                'onclick="event.stopPropagation(); sendRaw(this)" '
                'title="이 원본의 확정본으로 보냅니다 (원본에는 그대로 남아요)">확정본으로</button>'
                '<span class="more-wrap">'
                '<button type="button" class="send-more" onclick="event.stopPropagation(); toggleArticleMenu(this)" '
                'title="다른 확정본으로 보내기" aria-label="다른 확정본으로 보내기">▾</button>'
                f'<span class="more-menu" onclick="event.stopPropagation();">{raw_menu_html}</span>'
                "</span></span>"
            )
        else:
            bundle_select_html = ""
    elif sent_to:
        bundle_select_html = (
            '<select class="sel-sm to-bundle" disabled title="이미 보낸 기사입니다 — '
            '되돌리려면 그 확정본에서 숨기면 됩니다"><option>보냄</option></select>'
        )
    elif bundle_options_html:
        bundle_select_html = (
            f'<select class="sel-sm to-bundle" data-id="{card_id}" data-url="{url}" '
            'onclick="event.stopPropagation();" '
            'onchange="event.stopPropagation(); sendToBundle(this)" '
            'title="이 기사를 확정본으로 보냅니다 (원본에는 그대로 남습니다)">'
            f'<option value="">확정본으로</option>{bundle_options_html}</select>'
        )
    else:
        bundle_select_html = ""

    # [추가: 2026-09-03] 기사 ↑↓는 **수집 확정본에만** 있다. 수집 원본은 소제목 안이
    # 시간순 고정이라(_article_groups) 손으로 옮겨도 다음 렌더링에 제자리로 돌아간다 —
    # 안 눌리는 버튼을 남겨두면 화면이 거짓말을 한다. 소제목 자체의 ▲▼는 두 화면 모두
    # 그대로다(그건 기사 순서가 아니라 소제목 순서다).
    order_btns_html = (
        f'<button type="button" class="icon-btn art-order-btn" data-id="{card_id}" data-url="{url}" '
        f'{"disabled" if is_first else ""} onclick="event.stopPropagation(); moveOrder(this,\'up\')" '
        f'title="소제목 안에서 위로 이동 (다른 소제목으로 옮기려면 \'다른 소제목\' 드롭다운)">{icon("up")}</button>'
        f'<button type="button" class="icon-btn art-order-btn" data-id="{card_id}" data-url="{url}" '
        f'{"disabled" if is_last else ""} onclick="event.stopPropagation(); moveOrder(this,\'down\')" '
        f'title="소제목 안에서 아래로 이동 (다른 소제목으로 옮기려면 \'다른 소제목\' 드롭다운)">{icon("down")}</button>'
        if is_bundle(card)
        else ""
    )

    # 원본(raw)엔 「다른 소제목」·숨기기·🏷 라벨이 없다 — 판단은 확정본에서 한다.
    group_select_html = (
        ""
        if raw
        else (
            f'<select class="sel-sm" data-id="{card_id}" data-url="{url}" onclick="event.stopPropagation();" '
            'onchange="event.stopPropagation(); moveArticle(this)">'
            f'<option value="">다른 소제목</option>{other_group_options}</select>'
        )
    )
    hide_btn_html = (
        (
            # [추가: 2026-09-15] 원본의 🗑 — 실시간현황처럼 목록에서 빼지 않고 「숨김」 표시만
            # 붙인다(card.set_raw_mark). 처리 안 한 기사에만 있다 — 보낸 기사는 확정본에서 빼고,
            # 숨긴 기사는 보내기 자리의 「🗑 숨김」을 다시 누른다. 잠긴 카드엔 없다.
            f'<button type="button" class="icon-btn hide-btn" data-id="{card_id}" data-url="{url}" '
            f'onclick="event.stopPropagation(); rawHide(this)" '
            f'title="숨기기 — 목록에 남기고 흐리게만 해요 (되돌리기 가능)">{icon("trash")}</button>'
            if raw_menu_html and not sent_to and not raw_hidden_tip
            else ""
        )
        if raw
        else (
            f'<button type="button" class="icon-btn hide-btn" data-id="{card_id}" data-url="{url}" '
            f'onclick="event.stopPropagation(); hideArticle(this)" title="숨기기">{icon("trash")}</button>'
        )
    )
    if raw:
        lab_html = lab_row_html = ""

    article_class = "article"
    if is_new:
        article_class += " is-new-arrival"
    if sent_to:
        article_class += " is-sent"
    if raw_hidden_tip:
        article_class += " is-raw-hid"
    if out_of_window:
        article_class += " out"
    if is_photo:
        article_class += " is-photo"
    if _kind == "단독":
        article_class += " art-scoop"

    return f"""<div class="{article_class}">
  <details>
    <summary>
      <div class="a-top">
        <input type="checkbox" class="bulk-chk" data-url="{url}" onclick="event.stopPropagation();" onchange="updateBulkBar()">
        <div class="a-title"><span class="outlet">{html.escape(article["outlet"])}</span>{title_html}{out_tag}{photo_badge_html}</div>
        {sent_tag_html}
      </div>
      <div class="a-bot">
        {time_html}
        <span class="a-time-sep">·</span>
        <a class="origin-link" href="{url}" target="_blank" rel="noopener" onclick="event.stopPropagation();" title="원문 열기">원문보기{icon("external_link")}</a>
        {kw_inline_html}
        <span class="a-acts">
          {group_select_html}
          {bundle_select_html}
          <button type="button" class="icon-btn a-copy-btn" data-copy-text="{html.escape(copy_text)}" onclick="event.stopPropagation(); copyArticleIcon(this)" title="복사" aria-label="복사"><span class="icon-default">{icon("copy")}</span><span class="icon-done">{icon("check")}</span></button>
          {lab_html}
          {more_menu_html}
          {hide_btn_html}
          {order_btns_html}
        </span>
      </div>
      {lab_row_html}
    </summary>
    <p class="{summary_class}">{summary_html}</p>
    {edit_box_html}
  </details>
</div>"""


# [추가: 2026-09-15] 📷 사진 추정 모아 보기 + 선택 바 「🗑 숨기기」 JS — 정기의
# app.renderer.PHOTO_GATHER_SCRIPT와 같은 동작이되, 이 화면의 DOM(.group-block·.a-bot·
# .bulk-chk)과 폼 제출 방식에 맞췄다. render_card_page의 스크립트에 **스크롤 복원보다
# 먼저** 끼워 넣는다 — 숨기기는 POST → 303 → GET이라 페이지가 새로 뜨는데, 저장된 스크롤
# 위치가 모아 보기 화면 기준이라 그 화면을 먼저 되살려야 제자리로 돌아간다. 이 문자열은
# f-string의 값으로 들어가므로 중괄호를 두 번 쓰지 않는다.
_PHOTO_GATHER_JS = """
var _pgHomes = null;
function _pgRefresh() {
  var strip = document.getElementById('photo-gather-strip');
  var view = document.getElementById('photo-gather-view');
  if (!strip || !view) return;
  var n = view.querySelectorAll('.article.is-photo').length;
  var all = strip.querySelector('input');
  all.disabled = n === 0;
  if (n === 0) all.checked = false;
  strip.querySelector('.pg-msg').innerHTML = n
    ? ADHOC_PHOTO_ICON + ' <b>사진 추정 ' + n + '건</b>만 모아 보고 있어요'
    : '남은 사진 추정이 없어요';
  var done = view.querySelector('.pg-done');
  if (!n && !done) {
    view.insertAdjacentHTML('beforeend',
      '<div class="pg-done">다 치웠어요. 숨긴 기사는 왼쪽 아래 쓰레기통에서 되살릴 수 있어요.</div>');
  } else if (n && done) { done.remove(); }
}
function photoGatherOn() {
  if (document.body.classList.contains('photo-gather')) return;
  var rows = Array.prototype.slice.call(document.querySelectorAll('.group-block .article.is-photo'));
  // 옮기기 전 자리를 적어두고 끌 때 뒤에서부터 되돌린다(앞 행의 next가 아직 모인 목록 안에
  // 있는 뒤 행을 가리킬 수 있어서다 — 정기 flat-view와 같은 방식).
  _pgHomes = rows.map(function (el) { return {el: el, parent: el.parentElement, next: el.nextElementSibling}; });
  var anchor = document.querySelector('.group-block');
  var host = anchor ? anchor.parentNode : document.querySelector('.container');
  var view = document.createElement('div');
  view.id = 'photo-gather-view';
  view.className = 'group-block photo-gather-view';
  rows.forEach(function (el) {
    // 원래 소제목 이름표 — 보고서에 나갈 자리. 미분류 칸(이름 없음)에서 온 기사엔 안 붙인다.
    var block = el.closest('.group-block');
    var from = block ? (block.dataset.groupName || '') : '';
    var bot = el.querySelector('.a-bot');
    if (from && bot) {
      var tag = document.createElement('span');
      tag.className = 'pg-from';
      tag.textContent = from;
      tag.title = '보고서엔 「' + from + '」 소제목으로 나가요';
      bot.insertBefore(tag, bot.firstChild);
    }
    view.appendChild(el);
  });
  var strip = document.createElement('div');
  strip.id = 'photo-gather-strip';
  strip.className = 'photo-gather-strip';
  strip.innerHTML = '<label><input type="checkbox" onchange="photoGatherSelectAll(this.checked)"> 전체 선택</label>'
    + '<span class="pg-msg"></span><span class="pg-sp"></span>'
    + '<button type="button" class="btn ghost sm" onclick="photoGatherOff()">원래 화면으로</button>';
  if (anchor) host.insertBefore(view, anchor); else host.appendChild(view);
  host.insertBefore(strip, view);
  document.body.classList.add('photo-gather');
  document.querySelectorAll('.photo-gather-btn').forEach(function (b) { b.classList.add('is-on'); });
  try { sessionStorage.setItem(ADHOC_PHOTO_KEY, '1'); } catch (e) {}
  _pgRefresh();
  updateBulkBar();
}
function photoGatherOff() {
  if (!document.body.classList.contains('photo-gather')) return;
  (_pgHomes || []).slice().reverse().forEach(function (home) {
    if (!document.body.contains(home.el)) return;
    if (home.next && home.next.parentNode === home.parent) home.parent.insertBefore(home.el, home.next);
    else home.parent.appendChild(home.el);
  });
  _pgHomes = null;
  document.querySelectorAll('.pg-from').forEach(function (t) { t.remove(); });
  ['photo-gather-strip', 'photo-gather-view'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.remove();
  });
  document.body.classList.remove('photo-gather');
  document.querySelectorAll('.photo-gather-btn').forEach(function (b) { b.classList.remove('is-on'); });
  try { sessionStorage.removeItem(ADHOC_PHOTO_KEY); } catch (e) {}
  updateBulkBar();
  window.scrollTo(0, 0);
}
function togglePhotoGather() {
  if (document.body.classList.contains('photo-gather')) { photoGatherOff(); return; }
  photoGatherOn();
  window.scrollTo(0, 0);
}
// 화면에 보이는 행만 고른다(「아직 안 보낸 기사만」으로 감춘 행은 제외).
function photoGatherSelectAll(on) {
  var view = document.getElementById('photo-gather-view');
  if (!view) return;
  view.querySelectorAll('.article.is-photo .bulk-chk').forEach(function (cb) {
    if (cb.closest('.article').offsetParent !== null) cb.checked = on;
  });
  updateBulkBar();
}
// 선택 바의 「🗑 숨기기」 — 체크한 기사 중 화면에 보이는 것만 한 번에 숨긴다
// (/adhoc/card/bulk-hide, 되돌리기 한 걸음).
function bulkHide(id) {
  var urls = Array.prototype.filter.call(document.querySelectorAll('.bulk-chk:checked'), function (cb) {
    return cb.closest('.article').offsetParent !== null;
  }).map(function (cb) { return cb.dataset.url; });
  if (!urls.length) return;
  if (!confirm(urls.length + '건을 숨길까요?\\n\\n(왼쪽 아래 쓰레기통에서 하나씩, 되돌리기(↩)로는 한 번에 되돌릴 수 있어요.)')) return;
  postWithUrls('/adhoc/card/bulk-hide', {id: id}, urls);
}
try { if (sessionStorage.getItem(ADHOC_PHOTO_KEY)) photoGatherOn(); } catch (e) {}
"""


def render_card_page(card: dict, error: Optional[str] = None) -> str:
    """카드 정리 화면(ADHOC_DESIGN.md §5.3) — 요약은 페이지 맨 아래(§6.6), 소제목
    아래에는 기사 제목만 나열해 담당자가 흐름을 훑는 데 방해가 안 되게 한다.

    error: 수집 실패 직후 리다이렉트로 돌아왔을 때 보여줄 메시지(ADHOC_DESIGN.md §6.9 —
    자동 재시도 없이 즉시 표시). 카드 자체는 실패해도 그대로이므로("이미 모아둔 기사는
    그대로 있습니다") 화면 나머지는 평소와 동일하게 그린다.

    [수정: 2026-09-02] "⋯더보기(원문 재수집/제목 수정)는 1차 범위 밖"이라던 옛 메모는
    폐기됐다 — 잘린 제목은 같은 네이버 검색 결과를 쓰는 수시에도 똑같이 나오고,
    저장소(app.summary_overrides)는 원래부터 두 흐름이 함께 쓰라고 app/ 최상위에 둔
    것이라 공유가 오히려 맞다(사용자 결정). 나머지 미구현 항목은 모두 구현됐다.
    """
    groups = _article_groups(card)
    group_names = [name for name, _ in groups if name is not None]
    # 조건 밖 기사도 여기 모은다 — 지워진 게 아니라 "지금 조건으로는 안 불러와지는"
    # 상태이므로, 몇 건이 빠졌는지는 이 좌하단 배지가 말해준다(결과 배너를 따로 띄우지
    # 않는 이유 — 같은 사실을 두 번 말하면 그게 TMI다, 사용자 지적 2026-08-20).
    # 좌하단 「목록 밖 기사」에도 같은 오버라이드를 입힌다 — 제목을 고쳐둔 기사를
    # 숨기면 여기만 옛 제목으로 남아, 무엇을 되살리는지가 화면마다 달라진다.
    hidden_articles = apply_summary_overrides(
        [a for a in card["articles"] if a.get("hidden") or not article_in_condition(a, card)]
    )
    unclassified_count = sum(
        1 for a in articles_in_condition(card) if a.get("group") is None and not a.get("hidden")
    )
    # app.adhoc.classifier.classify_card가 "처음부터 새로 묶기"(classify_with_llm, 최대
    # MAX_ARTICLES_FOR_CLASSIFY=150건)로 갈지 "기존 소제목에 끼워넣기"(assign_to_existing,
    # 상한 없음)로 갈지는 이미 배정된 소제목이 하나라도 있는지로 갈린다 — 그 조건과 똑같이
    # 계산해야 "이번에 눌러도 조용히 아무 일도 안 일어나는" 상황을 미리 경고할 수 있다.
    llm_candidates_count = len(set(group_names) - set(card["custom_groups"]) - {"기타"})
    # 원본(raw)엔 소제목 배정 자체가 없으니 이 경고도 없다.
    over_classify_limit = (
        not is_raw(card) and llm_candidates_count == 0 and unclassified_count > MAX_ARTICLES_FOR_CLASSIFY
    )
    undo_label = undo.peek_label(card["id"])
    # [추가: 2026-08-18] "이미 쓴 라벨" 칩 목록 — 페이지 전체에서 내용이 같으므로 한
    # 번만 만든다(app.renderer._render_groups와 같은 이유).
    known_labels_html = known_label_chips_html()
    card_id_esc = html.escape(card["id"])

    # --- 모음 카드 문맥 (ADHOC_DESIGN.md §6.13) ---------------------------------
    bundle_mode = is_bundle(card)
    _today = datetime.now().strftime("%Y-%m-%d")
    # 보내기는 「오늘 원본 → 오늘 모음」 사이에서만 된다(규칙 2, 자정 잠금). 조건이 안
    # 맞으면 목록을 아예 안 만들어 드롭다운이 통째로 사라진다 — 못 누르는 컨트롤을
    # 회색으로 남겨두지 않는다(이 프로젝트의 기존 관례: 갈 데 없는 링크는 안 그린다).
    can_send = not bundle_mode and card.get("collect_date") == _today
    # [수정: 2026-09-15] 보낸 표시는 그 카드 날짜의 확정본으로 센다 — 예전엔 오늘 카드에서만
    # 셌는데, 지난 원본을 「지난 수집」에서 다시 열었을 때 무엇을 보냈는지가 안 보였다.
    day_bundles = bundles_for_date(card["collect_date"]) if not bundle_mode else []
    today_bundles = day_bundles if can_send else []
    sent_map = sent_index(card["id"], day_bundles) if not bundle_mode else {}
    # [추가: 2026-09-15] 마지막 불러오기로 새로 들어온 기사 — 노랑으로 칠한다.
    new_urls = last_collect_new_urls(card)
    # [추가: 2026-09-15] 로데이터 원본 — 이 원본의 확정본(같은 날·같은 사안, 없으면 None)과
    # 「확정본으로 ▾」 메뉴. 확정본은 처음 보내는 순간 서버가 만든다(__auto__).
    raw_mode = is_raw(card)
    raw_default = default_bundle_for(card, day_bundles) if raw_mode else None
    # [추가: 2026-09-15] 「불러올 때마다 새 확정본」 — 이 원본 시각의 확정본이 아직 없으면 보낼
    # 때 이 이름으로 생긴다(card.bundle_label과 같은 모양).
    raw_default_label = (
        bundle_label(raw_default) if raw_default
        else f'{card["report_title"]} {window_of(card)["end"]}'.strip()
    )
    # 원본 행 상태 — 보냄 / 숨김(원본에서 🗑, 또는 확정본에서 뺌). 처리 안 한 기사는 안 담긴다.
    raw_states = raw_row_states(card, day_bundles) if raw_mode else {}

    def _raw_hidden_tip(url: str) -> str:
        state = raw_states.get(url)
        if not state or state[0] != "hidden":
            return ""
        if state[1]:
            return f"「{state[1]}」 확정본에서 뺀 기사예요 — 눌러서 「처리 안 함」으로 되돌려요"
        return "숨긴 기사예요 — 눌러서 되돌려요"

    def _raw_menu(onclick: str) -> str:
        if not (raw_mode and can_send):
            return ""
        own = (
            f'<button type="button" data-bundle="__auto__" onclick="{onclick}">'
            f'{icon("folder")} {html.escape(raw_default_label)}'
            f'<small>이 원본의 확정본{"" if raw_default else " · 처음 보내면 생겨요"}</small></button>'
        )
        others = "".join(
            f'<button type="button" data-bundle="{html.escape(b["id"])}" onclick="{onclick}">'
            f'{icon("folder")} {html.escape(bundle_label(b))}<small>오늘 만든 다른 확정본</small></button>'
            for b in today_bundles
            if not raw_default or b["id"] != raw_default["id"]
        )
        return (
            own + others
            + f'<span class="menu-sep"></span><button type="button" data-bundle="__new__" onclick="{onclick}">'
            "+ 새 확정본 만들어 보내기</button>"
        )

    raw_menu_html = _raw_menu("sendRaw(this)")
    bundle_options_html = (
        (
            "".join(
                f'<option value="{html.escape(b["id"])}">{html.escape(bundle_label(b))}</option>'
                for b in today_bundles
            )
            + '<option value="__new__">+ 새 확정본 만들기</option>'
        )
        if can_send
        else ""
    )
    # [추가: 2026-09-01] 소제목별 복사 텍스트에 쓸 설정 — 소제목마다 다시 읽지 않도록
    # 한 번만 읽어 넘긴다(기사 제목 형식·소제목 형식은 정기 설정을 그대로 상속).
    settings = load_settings()
    sections = []
    raw_sent_n = raw_hidden_n = raw_open_n = 0
    if raw_mode:
        # 원본은 소제목 없이 한 목록 — 머리에 건수와 「최신순」만. .group-block·.cnt를 그대로
        # 쓰는 건 「아직 안 보낸 기사만」(applyUnsentFilter)이 그 자리의 건수를 다시 세기 때문이다.
        raw_articles = groups[0][1] if groups else []
        if raw_articles:
            raw_rows = "".join(
                _render_article_row(
                    card, a, [], False, False,
                    sent_to=sent_map.get(a["url"], ""),
                    # 숨김 표시가 붙은 기사는 노랑(새 기사)을 안 칠한다 — 이미 손댄 기사다.
                    is_new=a["url"] in new_urls and not _raw_hidden_tip(a["url"]),
                    raw_menu_html=raw_menu_html,
                    raw_hidden_tip=_raw_hidden_tip(a["url"]),
                )
                for a in raw_articles
            )
            for a in raw_articles:
                state = raw_states.get(a["url"], ("",))[0]
                if state == "sent":
                    raw_sent_n += 1
                elif state == "hidden":
                    raw_hidden_n += 1
                elif not a.get("hidden"):
                    raw_open_n += 1
            sections.append(
                f'<div class="group-block raw-list" data-group-name="">'
                f'<h2 class="sub raw-head"><span class="cnt">{len(raw_articles)}건</span>'
                f'<span class="raw-order">최신순</span></h2>{raw_rows}</div>'
            )
        groups = []
    for group_name, articles in groups:
        visible = [a for a in articles if not a.get("hidden")]
        is_custom = group_name in card["custom_groups"]
        # 자동 분류 소제목은 기사가 다 숨겨지면 화면에서 사라진다 — 굳이 빈 채로 남길
        # 이유가 없다. 담당자가 직접 만든 소제목만 비어 있어도 계속 보여준다(자리를
        # 미리 만들어두는 게 목적이므로).
        if not visible and not is_custom:
            continue
        label = html.escape(group_name) if group_name is not None else UNCLASSIFIED_LABEL
        style = "" if group_name is not None else ' style="color:%s;font-style:italic"' % COLOR_TEXT_MUTED
        custom_tag = ' <span class="custom-tag">직접 만든 소제목</span>' if is_custom else ""

        # 소제목(▲▼) 순서 조정 — 미분류(None) 칸은 항상 맨 뒤 고정이라 대상이 아니다.
        # 삼각형=소제목, 화살표=기사(CLAUDE.md 글리프 관례 그대로).
        order_html = ""
        if group_name is not None:
            gi = group_names.index(group_name)
            esc_name = html.escape(group_name)
            order_html = (
                f'<button class="order-btn" type="button" {"disabled" if gi == 0 else ""} '
                f'data-id="{card_id_esc}" data-name="{esc_name}" '
                f'onclick="moveGroupOrder(this, \'up\')" '
                f'title="소제목 위로">{icon("tri_up")}</button>'
                f'<button class="order-btn" type="button" {"disabled" if gi == len(group_names) - 1 else ""} '
                f'data-id="{card_id_esc}" data-name="{esc_name}" '
                f'onclick="moveGroupOrder(this, \'down\')" '
                f'title="소제목 아래로">{icon("tri_down")}</button>'
            )
        # [추가: 2026-09-01] 이 소제목만 복사 — 정기 확정본·초안과 같은 자리(아이콘
        # 묶음 맨 앞)·같은 동작이고, 텍스트도 카드 전체 복사와 같은 함수를 쓴다
        # (build_adhoc_plain_text에 소제목 하나짜리 groups를 넘긴 결과). [수정:
        # 2026-09-02] 헤더 "수시 모니터링 N시 기준(사안명)" 줄은 빼고 소제목 +
        # 기사만 담는다(header=False, 정기 두 화면과 같은 규칙). 미분류(None) 칸과
        # 기사가 하나도 안 남은 빈 커스텀 소제목에는 안 붙인다 — 각각 "소제목이
        # 아니라 대기실"·"복사할 게 없음"이라 정기와 같은 판단이다.
        copy_html = ""
        if group_name is not None and visible:
            group_copy_text = build_adhoc_plain_text(
                card, settings, groups=[(group_name, visible)], header=False,
            )
            copy_html = (
                f'<button class="icon-btn group-copy-btn" type="button" '
                f'data-copy-text="{html.escape(group_copy_text)}" onclick="copyGroupText(this)" '
                f'title="이 소제목만 복사">'
                f'<span class="icon-default">{icon("copy")}</span>'
                f'<span class="icon-done">{icon("check")}</span></button>'
            )
        # 소제목 헤더의 🗑 — 상태에 따라 뜻이 갈린다(정기 확정본·초안과 같은 규칙):
        # 기사가 있으면 "통째로 숨기기", 화면에 보이는 기사가 하나도 없는 커스텀
        # 소제목이면 "빈 소제목 삭제". 미분류(None) 칸엔 둘 다 안 붙인다 — 그 칸은
        # 소제목이 아니라 대기실이라 ▲▼·📋도 이미 빠져 있다.
        # [추가: 2026-09-02] "삭제"가 아니라 "숨기기"인 게 핵심이다 — 기사는 카드에서
        # 지워지지 않고 hidden 플래그만 붙어 좌하단 「목록 밖 기사」에서 하나씩 복구되고,
        # ↩ 되돌리기로는 한 번에 돌아온다. 정기 숨김(hidden_articles.json)과는 완전히
        # 별개라 이 카드 밖으로는 아무 영향이 없다.
        remove_html = ""
        if group_name is not None and visible:
            remove_html = (
                f'<button class="icon-btn group-hide-btn" type="button" '
                f'data-id="{card_id_esc}" data-name="{html.escape(group_name)}" '
                f'onclick="hideGroup(this)" '
                f'title="이 소제목 기사 {len(visible)}건 통째로 숨기기(기사 각각 숨긴 것과 동일 — 되돌리기 가능)">'
                f'{icon("trash")}</button>'
            )
        elif is_custom and not visible:
            remove_html = (
                f'<button class="icon-btn group-hide-btn" type="button" '
                f'data-id="{card_id_esc}" data-name="{html.escape(group_name)}" '
                f'onclick="removeCustomGroup(this)" '
                f'title="빈 소제목 삭제">{icon("trash")}</button>'
            )

        rows = (
            "".join(
                _render_article_row(
                    card, a, group_names, i == 0, i == len(visible) - 1,
                    known_labels_html,
                    sent_to=sent_map.get(a["url"], ""),
                    bundle_options_html=bundle_options_html,
                    is_new=a["url"] in new_urls,
                )
                for i, a in enumerate(visible)
            )
            if visible
            else '<p class="empty">아직 기사가 없습니다 — 다른 소제목의 "다른 소제목" 메뉴로 옮겨보세요.</p>'
        )
        # ADHOC_DESIGN.md §6.13 「소제목 단위로 통째 보내기」 — 아이콘 순서는
        # 📋(보는 동작) → 📥(보내는 동작) → 🗑(고치는 동작) → ▲▼(옮기는 동작).
        # 아직 안 보낸 기사가 0건이면 아예 안 그린다(규칙 10 "다시 누르면 남은 것만" —
        # 남은 게 없으면 버튼도 없다).
        unsent = [a for a in visible if a["url"] not in sent_map] if can_send else []
        send_group_html = ""
        if unsent and group_name is not None:
            choices = "".join(
                f'<button type="button" data-id="{card_id_esc}" '
                f'data-name="{html.escape(group_name)}" data-bundle="{html.escape(b["id"])}" '
                f'data-count="{len(unsent)}" onclick="sendGroupToBundle(this)">'
                f'「{html.escape(bundle_label(b))}」로 {len(unsent)}건</button>'
                for b in today_bundles
            )
            send_group_html = (
                '<span class="more-wrap">'
                f'<button class="icon-btn group-send-btn" type="button" onclick="toggleArticleMenu(this)" '
                f'title="이 소제목의 안 보낸 {len(unsent)}건을 확정본으로 보내기 (소제목 이름도 함께 갑니다)" '
                f'aria-label="확정본으로 보내기">{icon("in")}</button>'
                '<span class="more-menu">'
                + choices
                + f'<button type="button" data-id="{card_id_esc}" '
                f'data-name="{html.escape(group_name)}" data-bundle="__new__" '
                f'data-count="{len(unsent)}" onclick="sendGroupToBundle(this)">'
                f"+ 새 확정본으로 {len(unsent)}건</button>"
                "</span></span>"
            )
        sections.append(
            f'<div class="group-block{" sub-custom" if is_custom else ""}" '
            f'data-group-name="{html.escape(group_name or "")}">'
            f'<h2 class="sub"{style}>{label}{custom_tag} <span class="cnt">{len(visible)}건</span>'
            f'<span class="sub-acts">{copy_html}{send_group_html}{remove_html}{order_html}</span></h2>'
            f"{rows}</div>"
        )

    if not sections:
        sections.append('<p class="empty">아직 수집된 기사가 없습니다.</p>')

    # [추가: 2026-09-15] 📷 사진 추정 모아 보기 — 정기와 같은 이름. 화면에 보이는 기사
    # (조건 안 + 숨기지 않음) 중 사진 추정이 0건이면 줄 자체를 안 그린다.
    photo_count = sum(
        1 for _, arts in groups for a in arts
        if not a.get("hidden") and looks_like_photo_caption(a)
    )
    photo_row_html = (
        '<div class="photo-row">'
        '<button type="button" class="btn ghost sm photo-gather-btn" onclick="togglePhotoGather()" '
        'title="사진 추정 기사만 한곳에 모아 봅니다 — 복사·txt·엑셀은 그대로예요">'
        f'{icon("camera")} 사진 추정 모아 보기 ({photo_count})</button></div>'
        if photo_count
        else ""
    )

    summary_html = ""
    if card.get("group_summaries"):
        items = "".join(
            f'<dt>{html.escape(name)}</dt><dd>{html.escape(summary)}</dd>'
            for name, summary in card["group_summaries"].items()
        )
        summary_html = f"""<div class="card adhoc-summary" style="margin-top:20px">
  <h2 class="sub" style="margin-top:0">{icon("chat")} AI가 읽은 소제목별 주요 요약</h2>
  <dl>{items}</dl>
</div>"""

    # 목록 밖 기사는 두 종류다 — 담당자가 직접 숨긴 것(복구 버튼 있음)과, 지금 조건에서
    # 벗어난 것(ADHOC_DESIGN.md §6.4, 조건을 되돌리면 저절로 돌아오므로 복구 버튼이
    # 없다 — 여기서 되살려도 다음 렌더링에 다시 빠지므로 오히려 거짓말이 된다).
    hidden_rows = "".join(
        f'<div class="row"><span class="tx">({html.escape(a["outlet"])}) {html.escape(a["title"])}</span>'
        + (
            f'<span class="out-tag">{html.escape(out_of_condition_reason(a, card) or "")}</span>'
            if not article_in_condition(a, card)
            else f'<button type="button" class="btn sm mute" data-id="{html.escape(card["id"])}" '
            f'data-url="{html.escape(a["url"])}" onclick="unhideArticle(this)">'
            f'{icon("undo")} 복구</button>'
        )
        + "</div>"
        for a in hidden_articles
    )

    # ADHOC_DESIGN.md §6.3 — collect_date가 실제 오늘과 다를 때만 잠근다. status만으로
    # 판단하면(예전 버그) 오늘 만든 카드에도 매번 경고가 뜨는 거짓 알림이 된다.
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")
    now_hhmm = now_dt.strftime("%H:%M")
    is_stale = card["collect_date"] != today_str
    # stale(어제 이전) 카드는 "오늘의 수집 결과" 탭에 낄 수 없다 — 이미 warn_html이
    # 이 카드가 오늘 카드가 아니라고 알려주는데 탭까지 오늘 카드들 옆에 보이면 모순이다.
    tabs_html = "" if is_stale else _render_issue_tabs(card, cards_for_date(today_str))
    # [수정: 2026-08-25] 삭제 진입점은 화면에 언제나 정확히 하나만 둔다 — 탭 줄이
    # 그려지면 탭의 × 가 그 역할이라 하단 버튼은 빼고, 탭 줄이 없는 경우(어제 이전
    # 카드를 수시 보관함에서 열었을 때)에만 하단 버튼을 그린다. 둘 다 보이면 같은
    # 파괴적 동작의 입구가 둘이라 "뭘 눌러야 하지"가 다시 생긴다(사용자 지적).
    delete_btn_html = (
        ""
        if tabs_html
        else f'<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">'
        f'<button type="button" class="btn danger sm" onclick="deleteCard(\'{html.escape(card["id"])}\')">'
        f"{icon('trash')} 사안 목록에서 삭제</button></div>"
    )
    if not is_stale:
        warn_html = ""
    elif bundle_mode:
        # 모음은 검색을 안 하므로 §6.3의 원래 근거("시간창이 오늘로 해석된다")가 안
        # 걸린다 — 그래도 수시 전체를 하루 단위로 유지하려고 같은 잠금을 건다(규칙 2).
        # 문구는 그 사실만 말한다: 재수집·시간대 얘기를 하면 이 화면에 없는 걸 가리킨다.
        # [수정: 2026-09-15] 확정본은 이제 원본에서 처음 보낼 때 저절로 생긴다 — 「새로
        # 만들어주세요」 대신 그 길을 가리킨다.
        warn_html = (
            f'<div class="warn">{icon("clock")} <b>{card["collect_date"]}에 만든 확정본</b>입니다 '
            f"(오늘은 {today_str}). 지난 날짜의 확정본에는 기사를 더 보낼 수 없어요 — "
            f'오늘 <a href="/adhoc/new">새 수집</a>의 원본에서 보내면 오늘 확정본이 새로 생겨요.</div>'
        )
    else:
        # [수정: 2026-09-15] 같은 조건으로 오늘 새로 모으는 길을 바로 건다 — 새 수집이 이
        # 카드의 사안·검색어를 채운 채로 열린다(render_new_card_page의 pick).
        warn_html = (
            f'<div class="warn">{icon("clock")} <b>{card["collect_date"]} 기준</b>으로 수집한 카드입니다 '
            f'(오늘은 {today_str}). 시간대를 그대로 다시 수집하면 <b>{today_str}</b> 날짜로 검색됩니다 — '
            f'<a href="/adhoc/new?from={quote(card["id"])}">이 조건으로 오늘 새로 모으기 →</a></div>'
        )

    recollect_disabled = "disabled" if is_stale else ""
    # 재수집 종료시각은 열 때마다 현재 시각으로 미리 채운다 — "지금까지 끌어오기"가
    # 클릭 한 번(다시 수집)으로 끝나게 하려는 것. 시작시각은 저장된 창을 그대로 보여준다
    # (위 회색 칩의 "지금까지 모은 기준"과 나란히 비교돼야 하므로). 이미 잠긴(stale) 카드는
    # 미리 채우지 않는다 — 어차피 못 누르는데 오늘 시각이 떠 있으면 더 헷갈린다.
    recollect_end_value = window_of(card)["end"] if is_stale else now_hhmm
    # [수정: 2026-08-25] margin-left 오프셋을 없앴다 — 이제 이 칩 줄이
    # _render_edit_panel의 시간 입력 바로 아래 열(label 폭만큼 이미 들여진 flex
    # 컬럼) 안에 놓이므로, 예전처럼 자기 몫의 왼쪽 여백을 또 더하면 이중으로 밀린다.
    quick_chips_html = (
        ""
        if is_stale
        else """
    <div class="quick" style="margin:0">
      <button type="button" class="qchip" onclick="setQuick('recol-start','recol-end','midnight')">0시 ~ 지금</button>
      <button type="button" class="qchip" onclick="setQuick('recol-start','recol-end','1h')">최근 1시간</button>
      <button type="button" class="qchip" onclick="setQuick('recol-start','recol-end','3h')">최근 3시간</button>
    </div>"""
    )
    error_html = f'<div class="warn">{icon("alert")} {html.escape(error)}</div>' if error else ""
    limit_warn_html = (
        f'<div class="warn">{icon("alert")} 기사 {unclassified_count}건 — 소제목 자동 배정은 '
        f'{MAX_ARTICLES_FOR_CLASSIFY}건까지만 됩니다. 시간대를 좁히거나 검색어를 줄여주세요.</div>'
        if over_classify_limit
        else ""
    )

    # 헤더 — 모음은 시각을 안 붙인다(수집 시간창이 없어 "N시 기준"이 가리킬 대상이 없다).
    _title_input = (
        f'<input class="issue-input" id="report-title-input" '
        f'value="{html.escape(card["report_title"])}" '
        f"onblur=\"renameCard('{card_id_esc}', this.value)\">"
    )
    if bundle_mode:
        # [수정: 2026-09-15] 가장 늦은 원본 시각을 붙인다(report_header_text와 같은 규칙 —
        # 화면 머리줄과 복사 첫 줄은 같은 글자여야 한다). 시각이 없으면 예전 모양 그대로.
        _basis = _basis_time(card)
        _basis_html = (
            f'<span title="담긴 기사를 보낸 순간의 원본 기준 시각 중 가장 늦은 시각이에요">'
            f'{_format_hhmm_kr(_basis)} 기준</span>'
            if _basis
            else ""
        )
        report_head_html = (
            f'<div class="report-head">수시 모니터링 {_basis_html}({_title_input})'
            f'<span class="chip bundle-tag">확정본</span></div>'
        )
    else:
        # [추가: 2026-09-15] 「지금까지 불러오기」 — 시작은 그대로, 끝만 지금으로 한 번에
        # (ADHOC_RELOAD_NOW_MOCKUP.html C안). 헤더 줄 오른쪽은 원래 비어 있어 칩 줄을 안
        # 밀고, 누르면 바뀌는 「N시 N분 기준」이 바로 같은 줄이다. 시각은 서버가 누른
        # 순간에 정한다(to_now — routes._handle_recollect). 잠긴(어제) 카드엔 안 그린다:
        # 자정 잠금이라 어차피 못 누르고, 편집 창의 버튼도 이미 잠겨 있다.
        reload_now_html = (
            ""
            if is_stale
            else (
                f'<form method="POST" action="/adhoc/card/recollect" class="reload-now-form" '
                f'id="reload-now-form">'
                f'<input type="hidden" name="id" value="{card_id_esc}">'
                f'<input type="hidden" name="to_now" value="1">'
                f'<button type="submit" class="now-btn" '
                f'title="{html.escape(window_of(card)["start"])}부터 지금까지로 새로 불러와요">'
                f'{icon("refresh")} <span>지금까지 불러오기</span></button></form>'
            )
        )
        report_head_html = (
            f'<div class="report-head">수시 모니터링 '
            f'{_format_hhmm_kr(window_of(card)["end"])} 기준({_title_input}){reload_now_html}</div>'
        )

    # 모음에는 검색어 편집 패널을 안 그린다 — 고칠 조건이 없다(§6.13).
    edit_panel_html = (
        ""
        if bundle_mode
        else _render_edit_panel(card, quick_chips_html, recollect_disabled, recollect_end_value)
    )

    # ☑ 아직 안 보낸 기사만 — 규칙 1(복사)의 직접적 대가라 짝으로 간다. 보낸 기사가
    # 0건이면 아예 안 그린다: 아무것도 못 거르는 필터가 늘 떠 있으면 그 체크박스를
    # 안 읽게 된다(📂 미분류 배지·📷 사진 추정 버튼과 같은 원칙).
    unsent_filter_html = (
        '<label class="sent-filter" title="이미 확정본으로 보낸 기사를 목록에서 잠시 감춥니다">'
        '<input type="checkbox" id="unsent-only" onchange="applyUnsentFilter()"> '
        f"아직 안 보낸 기사만 ({len(sent_map)}건 보냄)</label>"
        if sent_map
        else ""
    )
    send_all_html = ""
    if raw_mode:
        # [수정: 2026-09-15] 원본은 「숨김」 표시가 생겨 이름이 실시간현황과 같은 「아직 처리 안
        # 한 기사만」이 됐다 — 보냄·숨김을 둘 다 가린다. 둘 다 0이면 안 그린다.
        _done_parts = [
            f"{label} {n}" for label, n in (("보냄", raw_sent_n), ("숨김", raw_hidden_n)) if n
        ]
        unsent_filter_html = (
            '<label class="sent-filter" title="보냄·숨김 — 이미 손댄 기사를 모두 보이지 않게 합니다">'
            '<input type="checkbox" id="unsent-only" onchange="applyUnsentFilter()"> '
            f'아직 처리 안 한 기사만 <small>({" · ".join(_done_parts)})</small></label>'
            if _done_parts
            else ""
        )
        # [추가: 2026-09-15] 「처리 안 한 N건 전부 확정본으로」 — 필터 바로 옆에 둔다: 필터를 켜면
        # 보이는 목록이 곧 이 버튼이 보낼 목록이라 짝이다. 무엇을 보낼지는 서버가 다시 센다
        # (all_open — routes._handle_send_to_bundle). 0건이면 안 그린다.
        if can_send and raw_open_n:
            send_all_html = (
                f'<button type="button" class="btn sm send-all" data-id="{card_id_esc}" '
                f'data-count="{raw_open_n}" data-target="{html.escape(raw_default_label)}" '
                f'onclick="sendAllOpen(this)" '
                f'title="숨김·보냄 표시가 없는 기사를 모두 「{html.escape(raw_default_label)}」 확정본으로 보내요 '
                f'(원본에는 그대로 남아요)">{icon("in")} 처리 안 한 {raw_open_n}건 전부 확정본으로</button>'
            )
    bulk_bundle_html = (
        # 세로선 — 「소제목 옮기기」와 「확정본으로 보내기」는 다른 동작이라 한 줄 안에서 가른다.
        '<span class="bulk-div"></span>'
        f'<select id="bulk-bundle-select" class="sel-sm to-bundle">'
        f'<option value="">확정본으로 보내기</option>{bundle_options_html}</select>'
        f'<button type="button" class="btn sm" onclick="bulkSendToBundle(\'{card_id_esc}\')">보내기</button>'
        if can_send
        else ""
    )
    # 모음에는 「조건 밖」이 구조적으로 존재하지 않는다(§6.13 — added_by="manual"이
    # article_in_condition을 무조건 통과한다). 그래서 이 팝오버에는 담당자가 직접 숨긴
    # 기사만 남고, 이름도 그 사실대로 바뀐다. 안내 문구의 "검색어를 되돌리면" 줄은
    # 통째로 뺀다 — 모음엔 되돌릴 조건이 없어 그대로 두면 거짓말이 된다.
    if bundle_mode:
        hidden_panel_title = "숨긴 기사"
        hidden_panel_note = (
            "복구하면 이 확정본의 목록으로 돌아옵니다. "
            "보낸 원본 쪽에서도 다시 「보낼 수 있는 기사」가 됩니다."
        )
    elif raw_mode:
        # 원본엔 숨기기가 없어 이 팝오버엔 조건 밖 기사만 남는다 — 「직접 숨긴 기사만 복구
        # 버튼이 있어요」는 가리킬 대상이 없는 말이라 뺀다.
        hidden_panel_title = "목록 밖 기사"
        hidden_panel_note = "조건 때문에 빠진 기사예요. 검색어를 되돌리면 저절로 돌아와요."
    else:
        hidden_panel_title = "목록 밖 기사"
        hidden_panel_note = (
            "조건 때문에 빠진 기사는 검색어를 되돌리면 저절로 돌아와요. "
            "직접 숨긴 기사만 복구 버튼이 있어요."
        )

    card_id_attr = html.escape(card["id"])
    xlsx_html = f'<a class="btn ghost sm" href="/adhoc/card/download-excel?id={card_id_attr}">⬇ xlsx</a>'
    if raw_mode:
        # [추가: 2026-09-15] 원본 툴바 — 소제목 배정·추가, 복사·txt는 확정본으로 갔다(복사·txt는
        # 소제목이 붙는 보고서 형식이라 확정본의 몫이다). 로데이터를 통째로 뽑는 xlsx는 남긴다
        # (사용자 결정). 오른쪽 끝의 청록 알약이 이 원본의 확정본으로 가는 길이다.
        # [수정: 2026-09-15] 「불러올 때마다 새 확정본」이라 알약에 원본 시각을 붙인다 — 다시
        # 불러온 직후엔 새 시각의 확정본이 아직 없어 「보내면 생겨요」로 바뀐다.
        _pair_time = window_of(card)["end"]
        if raw_default is not None:
            pair_n = sum(1 for a in raw_default["articles"] if not a.get("hidden"))
            pair_html = (
                f'<a class="pair-link" href="/adhoc/card?id={quote(raw_default["id"])}" '
                f'title="「{html.escape(bundle_label(raw_default))}」 확정본 열기">'
                f'{icon("folder")} {html.escape(bundle_time(raw_default))} 확정본 {pair_n}건 →</a>'
            )
        elif can_send:
            pair_html = (
                f'<span class="pair-link none" title="이 원본에서 기사를 보내면 「{html.escape(raw_default_label)}」 '
                f'확정본이 생겨요 — 「지금까지 불러오기」 뒤에 보내면 새 확정본이 따로 생겨요">'
                f'{icon("folder")} {html.escape(_pair_time)} 확정본 · 보내면 생겨요</span>'
            )
        else:
            pair_html = ""
        toolbar_html = f"""<div class="toolbar">
    {unsent_filter_html}
    {send_all_html}
    <span style="flex:1"></span>
    {xlsx_html}
    {pair_html}
  </div>"""
        bulk_menu_html = _raw_menu("bulkSendRaw(this)")
        bulk_actions_html = (
            f'<span class="send-split" data-id="{card_id_attr}">'
            '<button type="button" class="send-go" data-bundle="__auto__" onclick="bulkSendRaw(this)" '
            'title="고른 기사를 이 원본의 확정본으로 보냅니다 (원본에는 그대로 남아요)">확정본으로 보내기</button>'
            '<span class="more-wrap">'
            '<button type="button" class="send-more" onclick="toggleArticleMenu(this)" '
            'title="다른 확정본으로 보내기" aria-label="다른 확정본으로 보내기">▾</button>'
            f'<span class="more-menu up">{bulk_menu_html}</span></span></span>'
            if bulk_menu_html
            else ""
        )
    else:
        toolbar_html = f"""<div class="toolbar">
    <form method="POST" action="/adhoc/card/classify" style="display:inline">
      <input type="hidden" name="id" value="{card_id_attr}">
      <button type="submit" class="btn ai sm" {"disabled" if unclassified_count == 0 else ""}
        title="{f'미분류 {unclassified_count}건을 배정합니다' if unclassified_count else '배정할 미분류 기사가 없습니다'}">
        {icon("bot")} 소제목 배정{f' ({unclassified_count})' if unclassified_count else ''}</button>
    </form>
    <form method="POST" action="/adhoc/card/add-custom-group" style="display:inline-flex;gap:4px;align-items:center">
      <input type="hidden" name="id" value="{card_id_attr}">
      <input type="text" name="name" placeholder="새 소제목 이름" maxlength="20" style="width:130px;padding:5px 9px;font-size:0.82rem"
        {"disabled" if len(group_names) >= MAX_ADHOC_SUBHEADINGS else ""}>
      <button type="submit" class="btn mute sm" {"disabled" if len(group_names) >= MAX_ADHOC_SUBHEADINGS else ""}
        title="{f'소제목은 최대 {MAX_ADHOC_SUBHEADINGS}개까지 만들 수 있습니다' if len(group_names) >= MAX_ADHOC_SUBHEADINGS else ''}">+ 소제목 추가</button>
    </form>
    {unsent_filter_html}
    <span style="flex:1"></span>
    <button type="button" class="btn ghost sm" onclick="copyWholeCard()">{icon("copy")} 복사</button>
    <a class="btn ghost sm" href="/adhoc/card/download?id={card_id_attr}">{icon("file")} txt로 저장</a>
    {xlsx_html}
  </div>"""
        bulk_actions_html = f"""<select id="bulk-group-select" class="sel-sm">
    <option value="">이동할 소제목 선택</option>
    {"".join(f'<option value="{html.escape(g)}">{html.escape(g)}</option>' for g in group_names)}
  </select>
  <button type="button" class="btn sm" onclick="bulkMove('{card_id_attr}')">옮기기</button>
  {bulk_bundle_html}
  <span class="bulk-div"></span>
  <button type="button" class="btn sm danger" onclick="bulkHide('{card_id_attr}')" title="선택한 기사를 한 번에 숨깁니다 (되돌리기 가능)">{icon("trash")} 숨기기</button>"""

    body = f"""
<div class="card">
  {tabs_html}
  {report_head_html}
  {_render_meta_chips(card)}

  {error_html}
  {warn_html}
  {limit_warn_html}

  {edit_panel_html}

  {toolbar_html}

  {photo_row_html}
  {"".join(sections)}
</div>

{summary_html}

{delete_btn_html}

{f'''<button type="button" class="fab left upper" onclick="post('/adhoc/card/undo', {{id: '{html.escape(card["id"])}'}})"
  title="{html.escape(undo_label)} 되돌리기">{icon("undo")}</button>''' if undo_label else ""}

<div class="fab left" id="hidden-fab" onclick="toggleHiddenPanel()" style="display:{'flex' if hidden_articles else 'none'}">
  {icon("trash")}
  <span class="badge">{len(hidden_articles)}</span>
</div>
<div class="fab-pop" id="hidden-panel">
  <h5>{icon("trash")} {hidden_panel_title} {len(hidden_articles)}건</h5>
  <div style="font-size:.74rem;color:{COLOR_TEXT_MUTED};margin:-4px 0 6px">{hidden_panel_note}</div>
  {hidden_rows}
</div>

<div class="bulk-bar" id="bulk-bar"><div class="bulk-bar-inner">
  <span class="bulk-count" id="bulk-count">0개 선택됨</span>
  {bulk_actions_html}
  <button type="button" class="btn sm clear-btn" onclick="clearBulkSelection()">선택 해제</button>
</div></div>
"""

    copy_full_text = build_adhoc_plain_text(card)
    script = f"""<script>
var ADHOC_PLAIN_TEXT = {__import__("json").dumps(copy_full_text)};

// [추가: 2026-09-02] 스크롤 위치 유지 — 이 화면의 모든 조작은 POST → 303 → GET,
// 즉 브라우저 입장에서 "새 페이지로의 이동"이라 스크롤 복원이 안 걸린다(정기 화면은
// location.reload()라 같은 히스토리 항목이어서 브라우저가 알아서 복원해 준다).
// 그래서 제출 직전 위치를 직접 적어두고 돌아왔을 때 되돌린다. 목록 아래쪽 기사를
// 손볼 때마다 맨 위로 튀던 문제만 없앤다(전체를 다시 그리는 것 자체는 그대로다).
var ADHOC_SCROLL_KEY = 'adhocScroll:' + {__import__("json").dumps(card["id"])};
var ADHOC_HAS_ERROR = {"true" if error else "false"};
var ADHOC_PHOTO_KEY = 'adhocPhotoGather:' + {__import__("json").dumps(card["id"])};
var ADHOC_PHOTO_ICON = {__import__("json").dumps(icon("camera"))};
{_PHOTO_GATHER_JS}
function rememberScroll() {{
  // 시각을 함께 적어둔다 — 제출이 취소되는 경로(예: 수집 시간이 잘못된 "다시 불러오기"는
  // preventDefault로 막힌다)가 있어서, 이동이 실제로 안 일어나면 값만 남는다. 그 값이
  // 나중에 수시 보관함에서 이 카드를 다시 열 때 엉뚱한 위치로 튀게 하면 안 된다.
  try {{ sessionStorage.setItem(ADHOC_SCROLL_KEY, window.scrollY + ':' + Date.now()); }} catch (e) {{}}
}}
// 화면에 직접 적힌 <form>은 submit 이벤트를 타지만, post()/bulkMove()가 즉석에서
// 만들어 부르는 form.submit()은 그 이벤트를 안 띄운다 — 그쪽은 헬퍼 안에서 직접 부른다.
document.addEventListener('submit', rememberScroll, true);
(function () {{
  var y = null;
  try {{
    y = sessionStorage.getItem(ADHOC_SCROLL_KEY);
    sessionStorage.removeItem(ADHOC_SCROLL_KEY);  // 한 번만 쓰고 버린다
  }} catch (e) {{}}
  // 오류 배너는 화면 맨 위에 뜨므로 그때는 복원하지 않는다 — 되돌려 놓으면 방금
  // 무엇이 실패했는지 못 보고 지나친다.
  if (y === null || ADHOC_HAS_ERROR) return;
  var parts = String(y).split(':');
  // POST → 303 → GET 한 바퀴는 아무리 느려도 10초 안에 끝난다. 그보다 오래된 값은
  // 위 주석의 "제출이 취소된" 흔적이므로 쓰지 않는다.
  if (!(Date.now() - (parseInt(parts[1], 10) || 0) < 10000)) return;
  window.scrollTo(0, parseInt(parts[0], 10) || 0);
}})();

{_TIME_INPUT_JS}
attachHmInput('recol-start');
attachHmInput('recol-end');
var recollectForm = document.getElementById('recollect-form');
if (recollectForm) {{
  recollectForm.addEventListener('submit', function (e) {{
    if (!validWindow('recol-start', 'recol-end')) {{
      e.preventDefault();
    }}
  }});
}}

// [추가: 2026-09-15] 헤더 「지금까지 불러오기」 — 검색이 몇 초 걸리는 동안 두 번
// 눌리지 않게 잠그고 「불러오는 중…」으로 바꾼다(submit 이벤트가 이미 났으니 버튼을
// 잠가도 제출은 그대로 간다). 뒤로 가기로 이 화면이 캐시에서 되살아나면 잠긴 채
// 남지 않게 pageshow에서 되돌린다.
var reloadNowForm = document.getElementById('reload-now-form');
if (reloadNowForm) {{
  var reloadNowBtn = reloadNowForm.querySelector('button');
  var reloadNowLabel = reloadNowBtn.querySelector('span').textContent;
  reloadNowForm.addEventListener('submit', function (e) {{
    if (reloadNowBtn.disabled) {{ e.preventDefault(); return; }}
    reloadNowBtn.disabled = true;
    reloadNowBtn.querySelector('span').textContent = '불러오는 중…';
  }});
  window.addEventListener('pageshow', function (e) {{
    if (!e.persisted) return;
    reloadNowBtn.disabled = false;
    reloadNowBtn.querySelector('span').textContent = reloadNowLabel;
  }});
}}

function copyWholeCard() {{
  navigator.clipboard.writeText(ADHOC_PLAIN_TEXT).then(function () {{
    alert('복사했습니다.');
  }}).catch(function () {{ alert('복사에 실패했습니다.'); }});
}}

// [수정: 2026-09-02] 정기 확정본의 copyArticleIcon과 같은 이름·같은 동작 — 아이콘만
// 1초간 ✓로 바뀐다(CSS .a-copy-btn.is-copied). 예전엔 버튼 안 HTML을 통째로 갈아끼워
// 그동안 아이콘 폭이 바뀌며 옆 버튼들이 흔들렸다.
function copyArticleIcon(btn) {{
  navigator.clipboard.writeText(btn.dataset.copyText).then(function () {{
    btn.classList.add('is-copied');
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(function () {{ btn.classList.remove('is-copied'); }}, 1000);
  }}).catch(function () {{ alert('복사에 실패했습니다.'); }});
}}

// [추가: 2026-09-02] ⋯ 더보기 — 한 번에 하나만 열린다(정기 toggleArticleMenu와 동일).
function closeArticleMenus() {{
  document.querySelectorAll('.more-wrap.is-open').forEach(function (w) {{ w.classList.remove('is-open'); }});
}}
function toggleArticleMenu(btn) {{
  var wrap = btn.closest('.more-wrap');
  var wasOpen = wrap.classList.contains('is-open');
  closeArticleMenus();
  if (!wasOpen) {{ wrap.classList.add('is-open'); }}
}}

// ✏️ 기사제목 직접 수정 — 입력칸은 <details> 안(요약 아래)이라, 접혀 있으면 먼저 펼친다.
function openEditSummary(btn) {{
  closeArticleMenus();
  var box = document.getElementById(btn.dataset.target);
  if (!box) return;
  var details = box.closest('details');
  if (details) {{ details.open = true; }}
  box.classList.remove('hidden');
  var input = box.querySelector('input[name=title]');
  if (input) {{ input.focus(); }}
}}
function closeEditSummary(id) {{
  var box = document.getElementById(id);
  if (box) {{ box.classList.add('hidden'); }}
}}

// [추가: 2026-09-02] 게시시각 옆 "N분 전" — 정기 renderRelativeTimes와 같은 계산.
// 서버가 값을 굳혀두면 시간이 지날수록 틀리므로 화면이 열릴 때마다 다시 채운다.
function renderRelativeTimes() {{
  var now = Date.now();
  document.querySelectorAll('.a-time-rel[data-pub-date]').forEach(function (el) {{
    var pub = new Date(el.dataset.pubDate).getTime();
    if (isNaN(pub)) return;
    var minutes = Math.floor((now - pub) / 60000);
    var text;
    if (minutes < 1) text = '방금 전';
    else if (minutes < 60) text = minutes + '분 전';
    else if (minutes < 1440) text = Math.floor(minutes / 60) + '시간 전';
    else text = Math.floor(minutes / 1440) + '일 전';
    el.textContent = text;
  }});
}}
renderRelativeTimes();

// [추가: 2026-09-01] 소제목 헤더의 📋 — 정기 화면 copyArticleIcon과 같은 동작
// (data-copy-text를 클립보드에 넣고 아이콘을 1초간 ✓로). 카드 전체 복사(copyWholeCard)의
// alert을 쓰지 않는 건, 소제목마다 있는 버튼이라 확인창이 번거롭기 때문이다.
function copyGroupText(btn) {{
  navigator.clipboard.writeText(btn.dataset.copyText).then(function () {{
    btn.classList.add('is-copied');
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(function () {{ btn.classList.remove('is-copied'); }}, 1000);
  }}).catch(function () {{ alert('복사에 실패했습니다.'); }});
}}

function post(action, fields) {{
  var form = document.createElement('form');
  form.method = 'POST'; form.action = action;
  Object.keys(fields).forEach(function (key) {{
    var input = document.createElement('input');
    input.type = 'hidden'; input.name = key; input.value = fields[key];
    form.appendChild(input);
  }});
  document.body.appendChild(form);
  rememberScroll();
  form.submit();
}}

// [추가: 2026-08-18] 🏷 라벨 팝오버 — 열고닫기만 JS가 맡는다(붙이기/떼기는 이 화면의
// 다른 모든 조작과 마찬가지로 실제 form 제출 + 페이지 새로고침).
function closeLabelPopovers() {{
  document.querySelectorAll('.lab-pop').forEach(function (p) {{ p.classList.add('hidden'); }});
}}
function toggleLabelPopover(btn) {{
  var pop = btn.parentNode.querySelector('.lab-pop');
  var wasOpen = !pop.classList.contains('hidden');
  closeLabelPopovers();
  if (wasOpen) {{ return; }}
  pop.classList.remove('hidden');
  var attached = Array.prototype.map.call(pop.querySelectorAll('.attached .lab-chip'), function (c) {{
    return c.dataset.label;
  }});
  pop.querySelectorAll('.known-chip').forEach(function (k) {{
    k.classList.toggle('used', attached.indexOf(k.dataset.label) !== -1);
  }});
}}
document.addEventListener('click', function (e) {{
  if (!e.target.closest('.lab-pop-wrap')) {{ closeLabelPopovers(); }}
  if (!e.target.closest('.more-wrap')) {{ closeArticleMenus(); }}
}});
function knownChipClick(el) {{
  var btn = el.closest('.lab-pop-wrap').querySelector('.lab-btn');
  if (el.classList.contains('used')) {{
    post('/adhoc/card/remove-label', {{id: btn.dataset.id, url: btn.dataset.url, label: el.dataset.label}});
  }} else {{
    post('/adhoc/card/add-label', {{
      id: btn.dataset.id, url: btn.dataset.url, label: el.dataset.label,
      outlet: btn.dataset.outlet, title: btn.dataset.title, pubDate: btn.dataset.pubDate, group: btn.dataset.group
    }});
  }}
}}

function renameCard(id, title) {{ post('/adhoc/card/rename', {{id: id, report_title: title}}); }}
// [수정: 2026-09-02] 이 화면의 액션 핸들러는 카드 id·기사 URL·소제목 이름을 onclick
// **인자**로 받지 않고, 누른 요소의 data-* 속성에서 읽는다. 인자로 넘기면 그 값에
// 작은따옴표가 하나만 섞여도(html.escape가 &#x27;로 바꾸고 브라우저가 다시 '로
// 되돌린다) onclick 안의 JS 문자열이 그 자리에서 깨져 **버튼이 조용히 먹통**이 된다 —
// 소제목 이름은 AI가 짓거나 담당자가 직접 입력하는 자유 텍스트고, 기사 URL도 남의
// 사이트 주소라 따옴표가 없다고 장담할 수 없다. 정기 화면(app.renderer)이 data-name을
// 쓰는 이유와 같고, 이 파일의 bulkMove가 이미 쓰던 방식이기도 하다.
function moveArticle(sel) {{
  if (sel.value) post('/adhoc/card/move-article', {{id: sel.dataset.id, url: sel.dataset.url, group: sel.value}});
}}
function hideArticle(btn) {{ post('/adhoc/card/hide', {{id: btn.dataset.id, url: btn.dataset.url}}); }}
function unhideArticle(btn) {{ post('/adhoc/card/unhide', {{id: btn.dataset.id, url: btn.dataset.url}}); }}
function moveGroupOrder(btn, direction) {{
  post('/adhoc/card/move-group-order', {{id: btn.dataset.id, name: btn.dataset.name, direction: direction}});
}}
function removeCustomGroup(btn) {{
  post('/adhoc/card/remove-custom-group', {{id: btn.dataset.id, name: btn.dataset.name}});
}}
// [추가: 2026-09-02] 소제목 통째로 숨기기.
// [수정: 2026-09-16] 확인창을 없앴다 — 정기의 같은 버튼과 같은 근거이자 같은 날 같이 뺐다:
// 되돌릴 자리가 곧바로 화면에 남는다(좌하단 「목록 밖 기사」에서 하나씩, ↩ 한 번이면
// 통째로). 소제목 하나가 통째로 사라지는 건 화면에서 즉시 보이므로 "모르고 지나치는"
// 실수도 안 생긴다. 선택 바의 🗑(bulkHide)는 확인창을 그대로 둔다 — 체크가 여러
// 소제목에 흩어져 있을 수 있어 무엇이 사라지는지 누르기 전에 한눈에 안 보이기 때문.
// 확인창이 들고 있던 이름·건수는 버튼 자신의 title 툴팁이 이어받는다.
function hideGroup(btn) {{
  post('/adhoc/card/hide-group', {{id: btn.dataset.id, name: btn.dataset.name}});
}}
function moveOrder(btn, direction) {{
  post('/adhoc/card/move-order', {{id: btn.dataset.id, url: btn.dataset.url, direction: direction}});
}}

function updateBulkBar() {{
  var checked = document.querySelectorAll('.bulk-chk:checked').length;
  var bar = document.getElementById('bulk-bar');
  bar.classList.toggle('on', checked > 0);
  document.getElementById('bulk-count').textContent = checked + '개 선택됨';
}}
// 뒤로 가기·새로고침 때 브라우저가 체크 상태를 되살려 두는 경우가 있다 — 그때 띠만
// 내려가 있으면 "체크는 돼 있는데 할 일이 안 보이는" 화면이 된다.
window.addEventListener('pageshow', updateBulkBar);
function clearBulkSelection() {{
  document.querySelectorAll('.bulk-chk:checked').forEach(function (el) {{ el.checked = false; }});
  updateBulkBar();
}}
// post()와 같되 같은 이름(urls)의 값을 여러 개 실어 보낸다 — 일괄이동·일괄 보내기 공용.
function postWithUrls(action, fields, urls) {{
  var form = document.createElement('form');
  form.method = 'POST'; form.action = action;
  Object.keys(fields).forEach(function (key) {{
    var input = document.createElement('input');
    input.type = 'hidden'; input.name = key; input.value = fields[key];
    form.appendChild(input);
  }});
  urls.forEach(function (url) {{
    var input = document.createElement('input');
    input.type = 'hidden'; input.name = 'urls'; input.value = url;
    form.appendChild(input);
  }});
  document.body.appendChild(form);
  rememberScroll();
  form.submit();
}}
function checkedUrls() {{
  return Array.prototype.map.call(
    document.querySelectorAll('.bulk-chk:checked'), function (el) {{ return el.dataset.url; }});
}}
function bulkMove(id) {{
  var group = document.getElementById('bulk-group-select').value;
  var urls = checkedUrls();
  if (!group || urls.length === 0) return;
  postWithUrls('/adhoc/card/bulk-move', {{id: id, group: group}}, urls);
}}

// --- 모음으로 보내기 (ADHOC_DESIGN.md §6.13) ---------------------------------
// 셋 다 같은 라우트를 쓴다 — 다른 건 "무엇을 보낼지 고르는 방법"뿐이고, 보내는
// 동작 자체(복사·원본 그대로 유지)는 완전히 같기 때문이다.
function bundleFields(bundleId, extra) {{
  // "+ 새 모음 만들기"를 고르면 이름부터 받는다. 취소하면 null을 돌려줘 호출부가 멈춘다.
  var fields = extra || {{}};
  fields.bundle = bundleId;
  if (bundleId === '__new__') {{
    var name = prompt('새 확정본의 이름을 입력하세요.', fields.bundle_name_hint || '');
    if (!name || !name.trim()) return null;
    fields.bundle_name = name.trim();
  }}
  delete fields.bundle_name_hint;
  return fields;
}}
function sendToBundle(sel) {{
  if (!sel.value) return;
  var fields = bundleFields(sel.value, {{id: sel.dataset.id, urls: sel.dataset.url}});
  if (!fields) {{ sel.selectedIndex = 0; return; }}
  post('/adhoc/card/send-to-bundle', fields);
}}
function bulkSendToBundle(id) {{
  var sel = document.getElementById('bulk-bundle-select');
  var urls = checkedUrls();
  if (!sel || !sel.value || urls.length === 0) return;
  var fields = bundleFields(sel.value, {{id: id}});
  if (!fields) {{ sel.selectedIndex = 0; return; }}
  postWithUrls('/adhoc/card/send-to-bundle', fields, urls);
}}
function sendGroupToBundle(btn) {{
  var name = btn.dataset.name, n = btn.dataset.count;
  // 소제목 이름을 새 모음 이름의 기본값으로 미리 채운다 — 소제목을 통째로 보내면
  // 그 이름이 모음으로 따라가므로(§6.13), 대개 그대로 쓰면 맞다.
  var fields = bundleFields(btn.dataset.bundle,
    {{id: btn.dataset.id, group: name, bundle_name_hint: name}});
  if (!fields) return;
  // 이 블록은 파이썬 f-string이라 JS 줄바꿈 이스케이프는 역슬래시를 두 번 적는다 —
  // 한 번만 적으면 출력에 진짜 줄바꿈이 박혀 JS 문자열이 끊기고, 스크립트 전체가
  // SyntaxError로 죽어 이 화면의 버튼(편집·복사·↑↓ …)이 전부 먹통이 된다.
  if (!confirm('「' + name + '」의 ' + n + '건을 확정본으로 보낼까요?\\n\\n'
      + '원본에는 그대로 남습니다 (복사예요).\\n소제목 이름도 함께 갑니다.')) return;
  post('/adhoc/card/send-to-bundle', fields);
}}
// [추가: 2026-09-15] 로데이터 원본의 「확정본으로」 — 기본 버튼·▾ 메뉴 항목이 같은 함수를
// 쓴다. 보낼 확정본은 누른 버튼의 data-bundle(__auto__ = 이 원본의 확정본, 없으면 새로),
// 카드·기사는 둘을 감싼 .send-split에서 읽는다(onclick 인자에 URL을 싣지 않는 규칙).
function sendRaw(btn) {{
  var split = btn.closest('.send-split');
  var fields = bundleFields(btn.dataset.bundle, {{id: split.dataset.id, urls: split.dataset.url}});
  if (!fields) return;
  closeArticleMenus();
  post('/adhoc/card/send-to-bundle', fields);
}}
// [추가: 2026-09-15] 원본의 🗑 / 「🗑 숨김」 다시 누르기 — 실시간현황처럼 목록에서 빼지 않고
// 표시만 붙이고 뗀다. 확인창은 없다: 기사가 제자리에 남아 바로 되돌릴 수 있어서다.
function rawHide(btn) {{ post('/adhoc/card/raw-hide', {{id: btn.dataset.id, url: btn.dataset.url}}); }}
function rawUnhide(btn) {{ post('/adhoc/card/raw-unhide', {{id: btn.dataset.id, url: btn.dataset.url}}); }}
// 「처리 안 한 N건 전부 확정본으로」 — 무엇을 보낼지는 서버가 다시 센다(all_open).
function sendAllOpen(btn) {{
  if (!confirm('처리 안 한 ' + btn.dataset.count + '건을 「' + btn.dataset.target + '」 확정본으로 보낼까요?\\n\\n'
      + '원본에는 그대로 남아요. 빼려면 확정본에서 🗑를 누르면 돼요.')) return;
  post('/adhoc/card/send-to-bundle', {{id: btn.dataset.id, bundle: '__auto__', all_open: '1'}});
}}
function bulkSendRaw(btn) {{
  var split = btn.closest('.send-split');
  var urls = checkedUrls();
  if (urls.length === 0) return;
  var fields = bundleFields(btn.dataset.bundle, {{id: split.dataset.id}});
  if (!fields) return;
  closeArticleMenus();
  postWithUrls('/adhoc/card/send-to-bundle', fields, urls);
}}
// ☑ 아직 안 보낸 기사만 — 감추기만 하고 순서는 안 건드린다. 소제목 건수는 화면에
// 보이는 것과 어긋나면 안 되므로 여기서 같이 다시 센다(원래 값은 data-total에 보관).
function applyUnsentFilter() {{
  var on = document.getElementById('unsent-only').checked;
  document.body.classList.toggle('unsent-only', on);
  document.querySelectorAll('.group-block').forEach(function (block) {{
    var cnt = block.querySelector('h2.sub .cnt');
    if (!cnt) return;
    if (!cnt.dataset.total) {{
      cnt.dataset.total = String(block.querySelectorAll('.article').length);
    }}
    if (!on) {{ cnt.textContent = cnt.dataset.total + '건'; return; }}
    var left = Array.prototype.filter.call(block.querySelectorAll('.article'), function (a) {{
      return !a.classList.contains('is-sent') && !a.classList.contains('is-raw-hid');
    }}).length;
    cnt.textContent = left + '건 (전체 ' + cnt.dataset.total + ')';
  }});
}}
function deleteCard(id) {{
  // [수정: 2026-09-11] 「되돌릴 수 없어요」는 이제 사실이 아니다 — 지운 카드는 수시 보관함의
  // 제자리 「삭제함」 줄과 맨 아래 「최근 삭제」에서 되살린다. 확인창은 그대로 둔다: 여기선
  // 지운 직후 이 화면을 떠나 보관함으로 가므로, 되살리는 자리가 눈앞에 있지 않다.
  if (confirm('삭제하면 수시 보관함으로 돌아가요. 지운 카드는 거기서 되살릴 수 있어요.\\n삭제할까요?')) post('/adhoc/card/delete', {{id: id}});
}}
function toggleHiddenPanel() {{ document.getElementById('hidden-panel').classList.toggle('is-open'); }}
document.addEventListener('click', function (e) {{
  if (!e.target.closest('#hidden-panel') && !e.target.closest('#hidden-fab')) {{
    document.getElementById('hidden-panel').classList.remove('is-open');
  }}
}});
</script>"""

    # [수정: 2026-09-03] 원본↔확정본을 오갈 길을 상단바에 둔다 — 지금 보고 있는 화면
    # 자신은 빼는 규칙 그대로라, 원본에선 확정본만·확정본에선 원본만 나온다.
    nav = nav_html([
        NAV_HOME,
        NAV_NEW,
        _nav_latest(*(NAV_LATEST_COLLECT if is_bundle(card) else NAV_LATEST_BUNDLE)),
        NAV_ARCHIVE,
    ])
    return base_page(f"수시 모니터링 — {card['report_title']}", body, script, nav=nav)
