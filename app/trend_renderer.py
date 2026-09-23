# Design Ref: HISTORY.md "홈 화면 재구성" — 홈 카드 "더보기"가 가는 전용 화면.
"""정책 단어 추이 전용 화면(/trend).

홈 카드(app.landing_renderer._trend_card_html)는 최근 7일 고정 그래프만 보여주고,
단어를 고르는 일·6개월~1년까지 보는 일은 전부 이 화면 하나로 모았다(사용자 결정,
2026-08-27) — 홈은 매 요청마다 다시 그리는 화면이라 조작이 많으면 안 되고, 1년
단위 보고서를 쓸 때만 가끔 여는 화면은 따로 둬도 된다는 판단.

단어 저장소(app.home_trend)는 홈 카드와 이 화면이 공유한다 — 여기서 고른 단어가
홈에도(전부) 즉시 반영된다. 최대 MAX_TREND_WORDS(8)개까지
비교할 수 있다 — 6개 넘으면 선이 엉켜 개별 단어를 정확히 못 읽지만 "확 튀는 게
있으면 보인다"는 목적에는 여전히 쓸모가 있다는 사용자 판단(그래서 표를 그래프와
동등한 자리에 둬 정밀 판독을 받친다).
"""
import html
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode

from app.topnav import plain_nav, topnav_style
from app.config import FONT_STACK, MAX_TREND_WORDS, PALETTE
from app.excel_export import date_range_label
from app.home_trend import load_trend_words
from app.settings import load_settings
from app.trend_chart import HOVER_JS, chart_css, render_chart
from app.trend_data import (
    all_registered_keywords,
    articles_by_date_range,
    build_buckets,
    fill_bucket_status,
    last_complete_day,
    word_hits,
)

_PRESETS = (("7", 7, "7일"), ("30", 30, "1개월"), ("90", 90, "3개월"), ("180", 180, "6개월"), ("365", 365, "1년"))
_UNIT_LABEL = {"day": "일별", "week": "주별", "month": "월별"}


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _preset_range(days: int, today: str) -> tuple:
    """[수정: 2026-09-11] 빠른 설정은 전부 **어제에서 끝난다** — 홈 카드와 같은 규칙이라
    같은 단어가 두 화면에서 같은 구간을 보여준다(app.trend_data.last_complete_day 참고)."""
    end = datetime.strptime(last_complete_day(today), "%Y-%m-%d")
    start = end - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _current_preset(start: str, end: str, today: str):
    for key, days, _ in _PRESETS:
        if _preset_range(days, today) == (start, end):
            return key
    return None


def _trend_url(start: str, end: str) -> str:
    return f"/trend?{urlencode({'start': start, 'end': end})}"


def _resolve_range(query: dict) -> tuple:
    """쿼리스트링(start/end)에서 조회 구간을 정한다. 없거나 형식이 깨졌거나
    시작일이 종료일보다 늦으면 기본값(최근 7일)으로 조용히 되돌아간다 — 잘못된
    URL 하나 때문에 화면이 죽는 것보다는, 늘 보던 기본 구간을 보여주는 편이 낫다.

    [수정: 2026-09-11] 직접 입력한 종료일도 어제를 넘으면 어제로 당긴다 — 빠른 설정만
    어제에서 끝나고 직접 입력은 오늘까지 되면, 같은 화면 안에서 규칙이 두 개가 된다.
    당긴 결과 시작일이 더 늦어지면(예: 오늘~오늘) 위와 같이 기본 구간으로 돌아간다."""
    today = _today_str()
    last_day = last_complete_day(today)
    start, end = query.get("start", [""])[0], query.get("end", [""])[0]
    try:
        if start and end:
            datetime.strptime(end, "%Y-%m-%d")  # 형식 검사 — 깨졌으면 ValueError
            end = min(end, last_day)  # 같은 YYYY-MM-DD 형식이라 문자열 비교가 곧 날짜 비교
            if datetime.strptime(start, "%Y-%m-%d") <= datetime.strptime(end, "%Y-%m-%d"):
                return start, end
    except ValueError:
        pass
    return _preset_range(7, today)


# ══════════════════════════════════════════════════════════════════════════
# 지켜보는 단어 — 칩·입력 폼·등록 검색어에서 고르기
# ══════════════════════════════════════════════════════════════════════════

def _word_card_html(words: list, totals: dict, start: str, end: str, settings: dict) -> str:
    hidden_fields = f'<input type="hidden" name="start" value="{html.escape(start)}">' \
                    f'<input type="hidden" name="end" value="{html.escape(end)}">'

    chips = "".join(
        f'<span class="wchip"><span class="dot" style="background:{PALETTE[f"trend_{i+1}"]}"></span>'
        f'{html.escape(w)}<span class="cnt">{totals.get(w, 0)}건</span>'
        f'<form method="POST" action="/trend/remove-word" style="display:contents">'
        f'{hidden_fields}<input type="hidden" name="word" value="{html.escape(w)}">'
        f'<button type="submit" class="x" title="빼기">×</button></form></span>'
        for i, w in enumerate(words)
    )
    if len(words) < MAX_TREND_WORDS:
        chips += f'<span class="wchip slot-empty">+ {MAX_TREND_WORDS - len(words)}개 더 넣을 수 있어요</span>'

    can_add_more = len(words) < MAX_TREND_WORDS
    registered = all_registered_keywords(settings)

    add_row = ""
    if can_add_more:
        options = "".join(f"<option>{html.escape(w)}</option>" for w in registered if w not in words)
        add_row = (
            '<div class="add-row">'
            '<form method="POST" action="/trend/add-word" style="display:contents">'
            f'{hidden_fields}'
            '<input type="text" name="word" list="trend-word-options" placeholder="단어를 직접 입력" required>'
            f'<datalist id="trend-word-options">{options}</datalist>'
            '<button type="submit">+ 추가</button></form></div>'
        )

    quick_html = ""
    if can_add_more:
        quick = "".join(
            f'<form method="POST" action="/trend/add-word" style="display:inline">{hidden_fields}'
            f'<input type="hidden" name="word" value="{html.escape(w)}">'
            f'<button type="submit" class="qchip">{html.escape(w)}</button></form>'
            for w in registered if w not in words
        )
        if quick:
            quick_html = f'<div class="quick"><div class="qlabel">등록 검색어에서 고르기</div>{quick}</div>'

    return (
        '<div class="card"><h2>🔤 지켜보는 단어 '
        f'<span class="right">{len(words)}/{MAX_TREND_WORDS} · '
        '홈 그래프에는 위에서 3개만 나옵니다</span></h2>'
        f'<div class="word-chips">{chips}</div>{add_row}{quick_html}</div>'
    )


# ══════════════════════════════════════════════════════════════════════════
# 기간 바
# ══════════════════════════════════════════════════════════════════════════

def _period_bar_html(start: str, end: str, unit: str) -> str:
    today = _today_str()
    last_day = last_complete_day(today)  # 날짜 칸이 오늘을 아예 못 고르게 한다
    current = _current_preset(start, end, today)
    on_attr = ' class="on"'
    seg = "".join(
        f'<a href="{_trend_url(*_preset_range(days, today))}"{on_attr if key == current else ""}>{label}</a>'
        for key, days, label in _PRESETS
    )
    return f"""<div class="period-bar">
  <div class="seg">{seg}</div>
  <span class="unit-badge">{_UNIT_LABEL[unit]} 집계</span>
  <span class="sep">|</span>
  <form method="GET" action="/trend" class="range-form">
    <input type="date" name="start" value="{html.escape(start)}" max="{last_day}"> ~
    <input type="date" name="end" value="{html.escape(end)}" max="{last_day}">
    <button type="submit" class="ghost">조회</button>
  </form>
</div>"""


# ══════════════════════════════════════════════════════════════════════════
# 표 + 복사/엑셀
# ══════════════════════════════════════════════════════════════════════════

def _table_and_export_html(buckets: list, series: list, unit: str, start: str, end: str) -> str:
    labels = [b["wd_label"] if unit == "day" else b["label"] for b in buckets]
    header = "<tr><th>단어</th>" + "".join(f"<th>{html.escape(l)}</th>" for l in labels) + "<th>합계</th></tr>"
    body_rows = []
    text_lines = [f"정책 단어 추이 ({start} ~ {end}, {_UNIT_LABEL[unit]} 집계)", "\t".join(["단어"] + labels + ["합계"])]
    for s in series:
        total = sum(v for v in s["values"] if v is not None)
        cells = "".join(f'<td class="na">—</td>' if v is None else f"<td>{v}</td>" for v in s["values"])
        body_rows.append(
            f'<tr><td><span class="wdot" style="background:{s["color"]}"></span>'
            f'{html.escape(s["word"])}</td>{cells}<td class="sum">{total}</td></tr>'
        )
        text_lines.append(
            "\t".join([s["word"]] + ["—" if v is None else str(v) for v in s["values"]] + [str(total)])
        )
    copy_text = "\n".join(text_lines)
    filename_base = date_range_label([start, end])
    rows_for_excel = [
        {"word": s["word"], "values": s["values"], "total": sum(v for v in s["values"] if v is not None)}
        for s in series
    ]

    return f"""<div class="card">
  <h2>📊 {_UNIT_LABEL[unit]} 건수 <span class="right">
    <button type="button" class="ghost" onclick="copyTrendTable(this)"
      data-copy-text="{html.escape(copy_text)}">📋 복사</button>
    <form method="POST" action="/trend/download-excel" style="display:inline">
      <input type="hidden" name="labels" value='{json.dumps(labels, ensure_ascii=False)}'>
      <input type="hidden" name="rows" value='{json.dumps(rows_for_excel, ensure_ascii=False)}'>
      <input type="hidden" name="filename" value="{html.escape(filename_base)}_정책단어추이.xlsx">
      <button type="submit" class="ghost">⬇ 엑셀</button>
    </form>
  </span></h2>
  <div class="table-scroll"><table>{header}{"".join(body_rows)}</table></div>
  <p class="caption">보고서에 붙이는 건 그림이 아니라 숫자라, 그래프와 같은 집계 단위로 표를 같이 둡니다.
  “—”는 그 구간에 저장된 회차가 없다는 뜻(0건과 다름).</p>
</div>"""


# ══════════════════════════════════════════════════════════════════════════
# 페이지 조립
# ══════════════════════════════════════════════════════════════════════════

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>정책 단어 추이</title>
<style>
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  /* [수정: 2026-09-16] 카드 위 여백 60→44px — 60px은 상단 고정바(54px)를 피하려는 값인데 글자 위로
     30px이 비었다. 44px이면 16px 남는다. 확정본·초안·실시간·정기 보관함·설정·수시·정책 단어 추이
     일곱 화면이 같은 값을 써야 화면을 오갈 때 제목이 들썩이지 않는다(수시·추이는 84→68px 형태).
     시안 SUBHEAD_SPACING_MOCKUP.html B안. */
  .container {{ max-width: 900px; margin: 0 auto; padding: 68px 24px 60px; }}
  h1 {{ font-size: var(--fs-xl); color: {header}; margin: 0 0 4px; }}
  .page-sub {{ color: {muted}; font-size: var(--fs-md); margin: 0 0 20px; }}
  .card {{ background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    padding: 18px 20px; margin-bottom: 16px; }}
  .card h2 {{ font-size: var(--fs-base); color: {header}; margin: 0 0 12px;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .card h2 .right {{ margin-left: auto; font-weight: 400; font-size: var(--fs-sm); color: {muted}; }}

{topnav_style}

  button, a.btn {{ background: {accent}; color: #fff; border: none; border-radius: var(--r-md);
    padding: 8px 16px; font-size: var(--fs-md); font-family: inherit; cursor: pointer;
    text-decoration: none; display: inline-flex; align-items: center; justify-content: center; gap: 6px; }}
  button:hover, a.btn:hover {{ background: {header}; }}
  button.ghost {{ background: transparent; color: {accent}; border: 1px solid {accent_border}; }}
  button.ghost:hover {{ background: {hover}; color: {header}; }}
  input[type=text], input[type=date] {{ background: {bg}; color: {text}; border: 1px solid {border};
    border-radius: var(--r-md); padding: 7px 10px; font-size: var(--fs-md); font-family: inherit; }}
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}

  .word-chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
  .wchip {{ display: inline-flex; align-items: center; gap: 7px; padding: 6px 8px 6px 10px;
    border: 1px solid {border}; border-radius: var(--r-pill); background: {card}; font-size: var(--fs-md); }}
  .wchip .dot {{ width: 10px; height: 10px; border-radius: var(--r-circle); flex-shrink: 0; }}
  .wchip .cnt {{ color: {muted}; font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }}
  .wchip .x {{ border: none; background: transparent; color: {muted}; cursor: pointer;
    padding: 0 2px; font-size: 0.95rem; line-height: 1; }}
  .wchip .x:hover {{ background: transparent; color: {error}; }}
  .wchip.slot-empty {{ border-style: dashed; color: #9CA3AF; background: #FCFCFD; }}
  .add-row {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
  .quick {{ margin-top: 12px; padding-top: 12px; border-top: 1px dashed {border}; }}
  .quick .qlabel {{ font-size: var(--fs-sm); color: {muted}; margin-bottom: 7px; }}
  .qchip {{ display: inline-block; border: 1px solid {border}; background: {bg}; color: {text};
    border-radius: var(--r-pill); padding: 4px 11px; font-size: var(--fs-sm); margin: 0 6px 6px 0; cursor: pointer; }}
  .qchip:hover {{ background: {hover}; border-color: {accent}; color: {header}; }}

  .period-bar {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    padding-bottom: 14px; margin-bottom: 6px; border-bottom: 1px solid {border}; }}
  .seg {{ display: inline-flex; border: 1px solid {border}; border-radius: var(--r-md); overflow: hidden; }}
  .seg a {{ background: {card}; color: {text}; text-decoration: none; padding: 7px 14px;
    font-size: var(--fs-md); border-right: 1px solid {border}; }}
  .seg a:last-child {{ border-right: none; }}
  .seg a:hover {{ background: {hover}; }}
  .seg a.on {{ background: {accent}; color: #fff; }}
  .unit-badge {{ font-size: var(--fs-sm); color: {header}; background: {hover}; border: 1px solid {accent_border};
    border-radius: var(--r-pill); padding: 3px 10px; }}
  .period-bar .sep {{ color: #D1D5DB; }}
  .range-form {{ display: inline-flex; align-items: center; gap: 8px; }}

  .table-scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; font-size: var(--fs-md); width: 100%; }}
  th, td {{ border-bottom: 1px solid {border}; padding: 7px 10px; text-align: right;
    white-space: nowrap; font-variant-numeric: tabular-nums; }}
  th:first-child, td:first-child {{ text-align: left; position: sticky; left: 0; background: {card}; z-index: 1; }}
  thead th {{ color: {muted}; font-weight: 600; font-size: var(--fs-sm); background: {card}; }}
  td.sum {{ font-weight: 700; color: {header}; }}
  td.na {{ color: #C7CBD1; }}
  .wdot {{ display: inline-block; width: 9px; height: 9px; border-radius: var(--r-circle); margin-right: 7px; }}
  .caption {{ color: {muted}; font-size: var(--fs-sm); margin-top: 10px; }}
  .empty {{ text-align: center; padding: 26px 10px 24px; color: {muted}; font-size: var(--fs-lg); }}
  {trend_chart_css}
</style>
</head>
<body>
{topnav_html}
<div class="container">
  <h1>📈 정책 단어 추이</h1>
  <p class="page-sub">정기 수집 기사의 제목·요약에 그 단어가 몇 번 등장했는지 · 최대 {max_words}개 단어 비교 · 숨긴 기사 포함</p>
  {word_card_html}
  <div class="card">
    {period_bar_html}
    {chart_html}
  </div>
  {table_html}
</div>
<script>
function copyTrendTable(btn) {{
  navigator.clipboard.writeText(btn.dataset.copyText).then(function () {{
    var orig = btn.textContent;
    btn.textContent = "복사됨";
    setTimeout(function () {{ btn.textContent = orig; }}, 1200);
  }});
}}
{trend_hover_js}
</script>
</body>
</html>
"""


def render_trend_page(query: dict) -> str:
    start, end = _resolve_range(query)
    settings = load_settings()
    words = load_trend_words()

    by_date = articles_by_date_range(start, end)
    buckets, unit = build_buckets(start, end)
    fill_bucket_status(buckets, unit, by_date, _today_str())

    colors = [PALETTE[f"trend_{i+1}"] for i in range(len(words))]
    series = []
    totals = {}
    for word, color in zip(words, colors):
        values = []
        for b in buckets:
            if not b["record"]:
                values.append(None)
                continue
            values.append(sum(word_hits(word, by_date[d] or []) for d in b["dates"]))
        series.append({"word": word, "color": color, "values": values})
        totals[word] = sum(v for v in values if v is not None)

    if series:
        chart_html = (
            f'{render_chart("trend-detail-chart", buckets, series, unit, width=860, height=280)}'
            '<div class="trend-legend">'
            + "".join(
                f'<span class="li"><span class="dot" style="background:{s["color"]}"></span>'
                f'{html.escape(s["word"])}<span class="n">{totals[s["word"]]}건</span></span>'
                for s in series
            )
            + "</div>"
        )
    else:
        chart_html = '<p class="empty">지켜보는 단어를 골라주세요 — 위에서 직접 입력하거나 등록 검색어에서 고를 수 있습니다.</p>'

    table_html = _table_and_export_html(buckets, series, unit, start, end) if series else ""

    return _PAGE_TEMPLATE.format(
        font_stack=FONT_STACK,
        max_words=MAX_TREND_WORDS,
        word_card_html=_word_card_html(words, totals, start, end, settings),
        period_bar_html=_period_bar_html(start, end, unit),
        chart_html=chart_html,
        table_html=table_html,
        trend_chart_css=chart_css(),
        trend_hover_js=HOVER_JS,
        topnav_style=topnav_style(),
        topnav_html=plain_nav(),
        **PALETTE,
    )
