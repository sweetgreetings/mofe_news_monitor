# 「🗄️ 기관 기사 찾기」 화면 (`GET /agency-archive`) — 원장(app.agency_archive)을 날짜를 가로질러 찾는다.
#
# 이 화면의 기능은 **찾기 하나**다. 고치는 장치(숨김·소제목·↑↓·라벨 붙이기·발송·형광펜)는
# 없다 — 원장은 책이고 책은 고쳐 쓰지 않는다(CLAUDE.md 「기관 기사 원장」).
#
# URL 키 전역 상태는 **「기사에 관한 사실」만 입힌다**(같은 절의 기준표):
#   - `summary_overrides`(제목·요약) → 입힌다. **표시뿐 아니라 검색 대상에도** 입히는 게
#     핵심이다 — 안 그러면 담당자가 직접 고친 말로 검색했을 때 0건이 나온다.
#   - `labels` → 보여만 준다(붙이는 화면 넷에 이 화면을 더하지 않는다).
#   - `hidden_articles` → **거르지 않는다.** 「그날 무엇이 있었나」가 이 화면의 질문이라
#     보고서에서 뺀 기사도 기록으로 남고, 표시만 「🗑️ 숨김」으로 붙인다.
#
# 입히는 자리는 저장이 아니라 **여기(렌더링 시점) 한 곳**이다. 저장(app.agency_archive)은
# 관측 기록이라 받은 그대로 둔다.
import html
import json
from datetime import datetime, timedelta
from typing import Optional

from app.agency_archive import available_dates, load_day
from app.config import (
    AGENCY_ARCHIVE_KEYWORDS,
    AGENCY_ARCHIVE_TITLE,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    FONT_STACK,
    PALETTE,
)
from app.curation import load_hidden_urls
from app.excel_export import sanitize_filename_part
from app.labels import load_labels
from app.naver_api import outlet_display_label
from app.renderer import build_group_copy_texts
from app.settings import load_settings
from app.summary_overrides import load_summary_overrides
from app.topnav import plain_nav, topnav_style

# 화면에 한 번에 그리는 기사 수 상한. 낱말 없이 기간만 조회하면 한 달이 수천 건이라
# 페이지가 수 MB가 된다. 넘으면 최근 것부터 이만큼만 그리고 머리줄이 그렇다고 말한다
# (내보내기도 「지금 화면에 보이는 것」이라 같은 목록을 쓴다 — CLAUDE.md 엑셀 절).
MAX_ROWS = 300

# 기간 빠른 설정. 「전체」는 쌓인 가장 오래된 날짜까지.
_PRESETS = [("7d", "7일", 7), ("1m", "1개월", 30), ("3m", "3개월", 91), ("1y", "1년", 365), ("all", "전체", None)]
_DEFAULT_PRESET = "1m"


def _parse_date(value: str) -> Optional[str]:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def _resolve_range(preset: str, start: str, end: str, dates: list) -> tuple:
    """조회할 (시작, 끝) 날짜 문자열. 직접 입력이 둘 다 올바르면 그쪽이 이긴다."""
    today = datetime.now().strftime("%Y-%m-%d")
    s, e = _parse_date(start), _parse_date(end)
    if s and e:
        return (s, e) if s <= e else (e, s)
    days = dict((key, n) for key, _label, n in _PRESETS).get(preset, 30)
    if days is None:
        return (dates[-1] if dates else today), today
    return (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d"), today


def _apply_override(article: dict, overrides: dict) -> dict:
    """제목·요약에 담당자의 교정을 입힌 사본. **검색 전에** 거친다."""
    override = overrides.get(article.get("url") or "")
    if not override:
        return article
    merged = dict(article)
    if override.get("title"):
        merged["title"] = override["title"]
    if override.get("summary"):
        merged["summary"] = override["summary"]
    return merged


def _matches(article: dict, word: str) -> bool:
    if not word:
        return True
    haystack = f"{article.get('title') or ''} {article.get('summary') or ''}".lower()
    return word.lower() in haystack


def _highlight(text: str, word: str) -> str:
    """찾은 낱말만 노랑. 이 화면에서 노랑은 **찾은 낱말 하나**를 뜻한다(형광펜은 안 칠한다)."""
    escaped = html.escape(text)
    if not word:
        return escaped
    needle = html.escape(word)
    out, low_out, low_needle = [], escaped.lower(), needle.lower()
    i = 0
    while True:
        j = low_out.find(low_needle, i)
        if j < 0:
            out.append(escaped[i:])
            return "".join(out)
        out.append(escaped[i:j])
        out.append(f"<mark>{escaped[j:j + len(needle)]}</mark>")
        i = j + len(needle)


def _collect(date_from: str, date_to: str, word: str, overrides: dict) -> tuple:
    """(날짜 → 기사 목록, 걸린 총 건수). 날짜 최신순, 날짜 안은 저장된 순서(발행 최신순)."""
    days, total = [], 0
    shown = 0
    for date_str in available_dates():
        if not (date_from <= date_str <= date_to):
            continue
        rows = []
        for article in load_day(date_str).get("articles", []):
            merged = _apply_override(article, overrides)
            if not _matches(merged, word):
                continue
            total += 1
            if shown < MAX_ROWS:
                rows.append(merged)
                shown += 1
        if rows:
            days.append((date_str, rows))
    return days, total


def _row_html(article: dict, word: str, hidden_urls: set, labels: dict) -> str:
    url = article.get("url") or ""
    title = _highlight(article.get("title") or url, word)
    outlet = article.get("outlet") or ""
    outlet_html = f'<span class="outlet">{outlet_display_label(outlet, url)}</span>' if outlet else ""
    pub = article.get("pub_date") or ""
    meta = [f"<span>{html.escape(pub[11:16])} 게시</span>"] if len(pub) >= 16 else []
    meta.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener">원문보기</a>')
    if url in hidden_urls:
        meta.append('<span class="hid">🗑️ 숨김</span>')
    chips = "".join(
        f'<span class="lab">{html.escape(name)}</span>' for name in (labels.get(url, {}).get("labels") or [])
    )
    summary = article.get("summary") or ""
    summary_html = f'<div class="a-sum">{_highlight(summary, word)}</div>' if summary else ""
    return (
        '<div class="art">'
        f'<div class="a-title">{outlet_html}<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a></div>'
        f'<div class="a-meta">{"<span>·</span>".join(meta)}{chips}</div>'
        f"{summary_html}"
        "</div>"
    )


def _export_html(days: list, word: str, date_from: str, date_to: str) -> str:
    """복사·텍스트·엑셀 — 다른 화면과 같은 글자 버튼 묶음(.exp), 「지금 화면에 보이는 것」."""
    settings = load_settings()
    line_template = settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)
    flat = [a for _date, rows in days for a in rows]
    if not flat:
        return ""
    group_name = f"기관 기사 {date_from} ~ {date_to}" + (f" · {word}" if word else "")
    copy_text = build_group_copy_texts(
        "",
        [{"name": group_name, "articles": [
            {"outlet": a.get("outlet", ""), "title": a.get("title", ""), "url": a.get("url", "")} for a in flat
        ]}],
        line_template,
        {},
    )[group_name]
    excel_rows = [
        {
            "스크랩일자": date_str,
            "스크랩종료시간": "",
            "발행일 발행시간": (a.get("pub_date") or "").replace("T", " ")[:16],
            "언론사명": a.get("outlet", ""),
            "기사제목": a.get("title", ""),
            "URL": a.get("url", ""),
            "소제목": "",
            "라벨명": "",
        }
        for date_str, rows in days
        for a in rows
    ]
    base = sanitize_filename_part(
        f"{date_from}~{date_to}_기관기사" + (f"_{word}" if word else "")
    )
    attr = html.escape(copy_text)
    return (
        '<span class="exp">'
        f'<button type="button" onclick="copyArchiveText(this)" data-copy-text="{attr}">복사</button>'
        '<form method="POST" action="/download-text" style="display:contents">'
        f'<input type="hidden" name="filename" value="{html.escape(base)}.txt">'
        f'<input type="hidden" name="text" value="{attr}">'
        '<button type="submit">텍스트</button></form>'
        '<form method="POST" action="/download-excel" style="display:contents">'
        f'<input type="hidden" name="filename" value="{html.escape(base)}.xlsx">'
        f'<input type="hidden" name="rows" value="{html.escape(json.dumps(excel_rows, ensure_ascii=False))}">'
        '<button type="submit">엑셀</button></form>'
        "</span>"
    )


def render_agency_archive_page(
    word: str = "", preset: str = "", start: str = "", end: str = ""
) -> str:
    word = (word or "").strip()
    dates = available_dates()
    preset = preset if preset in dict((k, 1) for k, _l, _n in _PRESETS) else _DEFAULT_PRESET
    date_from, date_to = _resolve_range(preset, start, end, dates)
    custom = bool(_parse_date(start) and _parse_date(end))

    overrides = load_summary_overrides()
    days, total = _collect(date_from, date_to, word, overrides)
    hidden_urls = load_hidden_urls()
    labels = load_labels()

    preset_html = "".join(
        f'<button type="submit" name="preset" value="{key}" class="{"on" if (not custom and key == preset) else ""}">{label}</button>'
        for key, label, _n in _PRESETS
    )

    if not dates:
        body = (
            '<div class="empty">💤 아직 쌓인 기관 기사가 없어요'
            '<span class="sm">앱이 켜져 있으면 30분마다 저절로 모읍니다. 첫 수집 뒤에 보여요.</span></div>'
        )
    elif not days:
        span = f"{_kr(date_from)} ~ {_kr(date_to)}"
        what = f"「{html.escape(word)}」이(가) 든 " if word else ""
        body = (
            f'<div class="empty">💤 {html.escape(span)}에 {what}기관 기사가 없어요'
            '<span class="sm">기간을 넓히거나 낱말을 지워 보세요.<br />'
            f'이 화면엔 {html.escape("·".join(AGENCY_ARCHIVE_KEYWORDS))}가 든 기사만 쌓입니다.</span></div>'
        )
    else:
        sub = f"{_kr(date_from)} ~ {_kr(date_to)}" + (f" · 「{html.escape(word)}」" if word else "")
        capped = (
            f'<span class="cap">최근 {MAX_ROWS}건만 보입니다 · 낱말이나 기간으로 좁혀 보세요</span>'
            if total > MAX_ROWS
            else ""
        )
        blocks = []
        for date_str, rows in days:
            today_chip = '<span class="chip-today">오늘</span>' if date_str == datetime.now().strftime("%Y-%m-%d") else ""
            blocks.append(
                f'<div class="dblock"><div class="dhead">{_kr(date_str, weekday=True)} {today_chip}'
                f'<span class="dcnt">{len(rows)}건</span></div>'
                + "".join(_row_html(a, word, hidden_urls, labels) for a in rows)
                + "</div>"
            )
        body = (
            f'<div class="rhead"><span class="n">{total}건</span><span class="sub">{sub}</span>'
            f"{capped}{_export_html(days, word, date_from, date_to)}</div>" + "".join(blocks)
        )

    return _TEMPLATE.format(
        **PALETTE,
        font_stack=FONT_STACK,
        topnav_style=topnav_style(),
        topnav=plain_nav(),
        title=html.escape(AGENCY_ARCHIVE_TITLE),
        scope=html.escape("·".join(AGENCY_ARCHIVE_KEYWORDS)),
        word=html.escape(word),
        date_from=html.escape(date_from),
        date_to=html.escape(date_to),
        preset_html=preset_html,
        body_html=body,
    )


def _kr(date_str: str, weekday: bool = False) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    if weekday:
        return f"{d.month}월 {d.day}일 ({'월화수목금토일'[d.weekday()]})"
    return f"{d.month}월 {d.day}일"


_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>
  {topnav_style}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  /* 위 여백 24 + 안쪽 44 — 상단 고정바(54px)를 피하는 값이고, 확정본·초안·오늘 전체 기사·
     정기 보관함·설정·라벨 일곱 화면이 같은 값을 쓴다(화면을 오갈 때 제목이 안 들썩이게). */
  .container {{ max-width: 800px; margin: 24px auto 80px; background: {card}; border: 1px solid {border};
    border-radius: var(--r-lg); padding: 44px 24px 26px; }}
  h1 {{ font-size: var(--fs-xl); color: {header}; margin: 0 0 6px; }}
  .hint {{ color: {text_soft}; font-size: var(--fs-md); margin: 0 0 18px; line-height: 1.7; }}

  .qbar {{ border: 1px solid {border}; border-radius: var(--r-lg); padding: 12px 14px; margin-bottom: 18px;
    background: {bg}; }}
  .qrow {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .qrow + .qrow {{ margin-top: 10px; }}
  .qlabel {{ font-size: var(--fs-sm); color: {text_soft}; font-weight: 600; width: 46px; flex: none; }}
  .qbar input[type="text"], .qbar input[type="date"] {{ height: var(--h-md); border: 1px solid {border};
    border-radius: var(--r-md); padding: 0 10px; font-size: var(--fs-md); font-family: inherit; background: {card};
    color: {text}; }}
  .qbar input[type="text"] {{ flex: 1 1 240px; min-width: 180px; }}
  .qbar input[type="date"] {{ font-size: var(--fs-sm); }}
  .btn-go {{ height: var(--h-md); background: {accent}; color: #FFFFFF; border: none; border-radius: var(--r-md);
    padding: 0 16px; font-size: var(--fs-md); font-weight: 600; font-family: inherit; cursor: pointer; }}
  .preset {{ display: inline-flex; border: 1px solid {border}; border-radius: var(--r-md); overflow: hidden;
    background: {card}; }}
  .preset button {{ border: none; background: transparent; color: {text_soft}; font-size: var(--fs-sm);
    font-family: inherit; padding: 0 11px; height: var(--h-md); cursor: pointer; }}
  .preset button + button {{ border-left: 1px solid {border}; }}
  .preset button.on {{ background: {hover}; color: {header}; font-weight: 700; }}
  .qsep {{ color: {text_soft}; font-size: var(--fs-sm); }}

  .rhead {{ display: flex; align-items: center; gap: 8px; margin: 0 0 6px; flex-wrap: wrap; }}
  .rhead .n {{ font-size: var(--fs-md); color: {header}; font-weight: 700; flex: none; white-space: nowrap; }}
  .rhead .sub {{ font-size: var(--fs-sm); color: {text_soft}; min-width: 0; }}
  .rhead .cap {{ font-size: var(--fs-sm); color: {warn_text}; }}
  .exp {{ display: inline-flex; gap: 2px; flex: none; margin-left: auto; }}
  .exp button {{ border: none; background: transparent; color: {text_soft}; font-weight: 500;
    border-radius: var(--r-md); padding: 4px 8px; font-size: var(--fs-sm); cursor: pointer; font-family: inherit; }}
  .exp button:hover {{ background: transparent; color: {accent}; text-decoration: underline; }}

  .dblock {{ margin-top: 16px; }}
  .dhead {{ display: flex; align-items: center; gap: 8px; font-size: var(--fs-lg); color: {header};
    font-weight: 700; padding: 6px 0; border-bottom: 1px solid {border}; }}
  .dhead .dcnt {{ font-size: var(--fs-sm); color: {text_soft}; font-weight: 500; }}
  .chip-today {{ font-size: var(--fs-xs); font-weight: 700; color: {header}; background: {hover};
    border: 1px solid {accent_border}; border-radius: var(--r-pill); padding: 1px 8px; }}

  .art {{ padding: 4px 8px; margin: 6px 0; border-radius: var(--r-md); }}
  .art:hover {{ background: {row_hover}; }}
  .a-title {{ font-size: var(--fs-base); line-height: 1.55; }}
  .a-title .outlet {{ color: {text_soft}; font-size: 0.86em; font-weight: 500; margin-right: 0.5em; }}
  .a-title a {{ color: {text}; text-decoration: none; }}
  .a-title a:hover {{ text-decoration: underline; }}
  .a-meta {{ margin-top: 2px; font-size: var(--fs-sm); color: {text_soft}; display: flex; align-items: center;
    gap: 6px; flex-wrap: wrap; }}
  .a-meta a {{ color: {text_soft}; text-decoration: none; }}
  .a-meta a:hover {{ color: {accent}; text-decoration: underline; }}
  .a-meta .hid {{ color: {text_soft}; }}
  .a-meta .lab {{ font-size: var(--fs-xs); font-weight: 600; color: {label_text}; background: {label_bg};
    border: 1px solid {label_border}; border-radius: var(--r-pill); padding: 1px 8px; }}
  .a-sum {{ margin-top: 3px; font-size: var(--fs-md); color: {text_soft}; line-height: 1.7; }}
  mark {{ background: {row_new}; color: inherit; padding: 0 1px; border-radius: 2px; }}

  .empty {{ border: 1px dashed {border}; border-radius: var(--r-lg); padding: 28px 20px; text-align: center;
    color: {text_soft}; font-size: var(--fs-lg); }}
  .empty .sm {{ display: block; margin-top: 8px; font-size: var(--fs-md); color: {text_soft}; line-height: 1.8; }}
</style>
</head>
<body>
{topnav}
<div class="container">
  <h1>🗄️ {title}</h1>
  <p class="hint">{scope} 관련 기사를 하루 종일 모아 둡니다. 지우지 않습니다.</p>

  <form method="GET" action="/agency-archive">
    <div class="qbar">
      <div class="qrow">
        <span class="qlabel">낱말</span>
        <input type="text" name="q" value="{word}" placeholder="제목·요약에서 찾을 낱말" />
        <button type="submit" class="btn-go">찾기</button>
      </div>
      <div class="qrow">
        <span class="qlabel">기간</span>
        <span class="preset">{preset_html}</span>
        <input type="date" name="start" value="{date_from}" />
        <span class="qsep">~</span>
        <input type="date" name="end" value="{date_to}" />
      </div>
    </div>
  </form>

  {body_html}
</div>
<script>
function copyArchiveText(btn) {{
  const text = btn.getAttribute('data-copy-text') || '';
  navigator.clipboard.writeText(text).then(function () {{
    const before = btn.textContent;
    btn.textContent = '복사됨';
    setTimeout(function () {{ btn.textContent = before; }}, 1000);
  }});
}}
</script>
</body>
</html>
"""
