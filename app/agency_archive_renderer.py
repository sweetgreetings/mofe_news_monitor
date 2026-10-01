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
from app.filters import HEADLINE_TAG_RE, headline_kind, looks_like_photo_caption, photo_badge_tip
from app.excel_export import sanitize_filename_part
from app.icons import icon
from app.labels import load_labels
from app.naver_api import PRIORITY_OUTLETS, outlet_display_label
from app.renderer import build_group_copy_texts
from app.search_terms import split_terms
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


def _preset_range(key: str, dates: list) -> tuple:
    """빠른 설정 한 칸이 가리키는 (시작, 끝). 「전체」는 쌓인 가장 오래된 날짜까지."""
    today = datetime.now().strftime("%Y-%m-%d")
    days = dict((k, n) for k, _label, n in _PRESETS).get(key, 30)
    if days is None:
        return (dates[-1] if dates else today), today
    return (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d"), today


def _resolve_range(preset: str, start: str, end: str, dates: list) -> tuple:
    """조회할 (시작, 끝) 날짜 문자열.

    **빠른 설정을 눌렀으면 그게 이긴다** — 날짜 칸은 늘 채워져 나가므로(화면이 지금
    구간을 적어 둔다) 직접 입력을 먼저 보면 빠른 설정 버튼이 아무 일도 못 한다.
    직접 고른 날짜는 빠른 설정 없이 오고, 그때만 그 값을 쓴다. 수시 보관함과 같은 규칙.
    """
    if preset:
        return _preset_range(preset, dates)
    s, e = _parse_date(start), _parse_date(end)
    if s and e:
        return (s, e) if s <= e else (e, s)
    return _preset_range(_DEFAULT_PRESET, dates)


def _current_preset(date_from: str, date_to: str, dates: list) -> Optional[str]:
    """지금 구간과 똑같은 빠른 설정 칸(없으면 None = 직접 지정). 강조할 칸을 고르는 데만 쓴다.

    구간을 값으로 비교하므로, 필터 체크박스로 다시 조회해도 눌러 둔 칸이 그대로 켜져 있다.
    """
    for key, _label, _n in _PRESETS:
        if _preset_range(key, dates) == (date_from, date_to):
            return key
    return None


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
    """`+`로 묶은 조각이 **전부** 든 기사만. 조각 하나하나는 글자 그대로 부분일치다."""
    terms = split_terms(word)
    if not terms:
        return True
    haystack = f"{article.get('title') or ''} {article.get('summary') or ''}".lower()
    return all(t.lower() in haystack for t in terms)


def _highlight(text: str, word: str) -> str:
    """찾은 낱말만 노랑. 이 화면에서 노랑은 **찾은 낱말**을 뜻한다(형광펜은 안 칠한다).

    `+`로 여러 조각을 넣었으면 조각마다 칠한다. 한 자리에서 여러 조각이 걸리면 긴 쪽을
    칠한다 — 짧은 조각이 긴 조각을 가운데서 자르면 안 된다(`가상자산+가상자산 과세`).
    """
    escaped = html.escape(text)
    needles = [html.escape(t).lower() for t in split_terms(word)]
    if not needles:
        return escaped
    low = escaped.lower()
    out, i, n = [], 0, len(escaped)
    while i < n:
        hit = max((x for x in needles if low.startswith(x, i)), key=len, default="")
        if hit:
            out.append(f"<mark>{escaped[i:i + len(hit)]}</mark>")
            i += len(hit)
            continue
        # 걸리는 조각이 없는 자리에서 `&amp;` 같은 실체 참조를 가운데서 쪼개지 않는다.
        if escaped[i] == "&":
            end = escaped.find(";", i, i + 10)
            if end > 0:
                out.append(escaped[i:end + 1])
                i = end + 1
                continue
        out.append(escaped[i])
        i += 1
    return "".join(out)


def _is_major(article: dict) -> bool:
    """고정 주요 언론사 13곳(`PRIORITY_OUTLETS`)인가.

    **설정의 「선택 언론사」(화이트리스트)가 아니다** — 그건 지금 0곳이라 켜면 한 건도
    안 남는다. 이 화면은 찾는 자리라 「무엇이 주요 매체인가」를 담당자에게 되묻지 않는다.
    """
    return (article.get("outlet") or "") in PRIORITY_OUTLETS


def _is_photo(article: dict) -> bool:
    """사진 추정 — 판정은 공용 `looks_like_photo_caption` 그대로고 **원문을 새로 받지만
    않는다**(`fetch_body=False`). 이 화면은 한 번에 수만 건을 훑는데 원문 판정은 90일만
    남으므로, 받으려 들면 화면을 열 때마다 옛 기사 수만 건에 네트워크를 건다.
    이미 적어 둔 판정은 그대로 읽으니 최근 기사는 다른 화면과 답이 같다."""
    return looks_like_photo_caption(article, fetch_body=False)


# 필터 줄. **사진만 「빼고 보기」 방향이다** — 오늘 전체 기사의 다섯 칸은 전부 「~만 보기」인데,
# 여기선 세로선으로 갈라 두어 다른 축으로 읽힌다(사진만 골라 볼 일이 거의 없다).
AGENCY_ARCHIVE_FILTERS = _FILTERS = ("major", "scoop", "flash", "nophoto")


def _tally(article: dict) -> dict:
    """이 기사가 어느 필터에 걸리는가. 건수 표시와 걸러내기가 같은 판정을 쓴다."""
    kind = headline_kind(article.get("title") or "")
    return {
        "major": _is_major(article),
        "scoop": kind == "단독",
        "flash": kind == "속보",
        "photo": _is_photo(article),
    }


def _passes(hits: dict, on: dict) -> bool:
    """서로 AND. **말머리 둘끼리만 OR**(오늘 전체 기사와 같은 규칙)."""
    if on["major"] and not hits["major"]:
        return False
    if (on["scoop"] or on["flash"]) and not (
        (on["scoop"] and hits["scoop"]) or (on["flash"] and hits["flash"])
    ):
        return False
    if on["nophoto"] and hits["photo"]:
        return False
    return True


def _collect(date_from: str, date_to: str, word: str, overrides: dict, on: dict) -> tuple:
    """(날짜 → 기사 목록, 걸린 총 건수, 필터별 건수, 필터 걸기 전 건수). 날짜 최신순.

    **필터는 서버에서 걸고 그다음에 `MAX_ROWS`로 자른다.** 화면에서 숨기는 방식이면
    자른 300건 안에서만 줄어들어 건수가 거짓이 되고, 「지금 화면에 보이는 것」을 담는
    가져가기에도 걸러낸 기사가 실린다.

    필터별 건수는 **낱말·기간까지만 걸러낸 목록** 기준이다(다른 체크와 겹쳐 세지 않는다)
    — 겹쳐 세면 칸을 누를 때마다 옆 칸 숫자가 따라 움직여 무엇을 고르는지 알 수 없다.
    """
    days, total, shown, raw_total = [], 0, 0, 0
    counts = {"major": 0, "scoop": 0, "flash": 0, "photo": 0}
    for date_str in available_dates():
        if not (date_from <= date_str <= date_to):
            continue
        rows = []
        for article in load_day(date_str).get("articles", []):
            merged = _apply_override(article, overrides)
            if not _matches(merged, word):
                continue
            raw_total += 1
            hits = _tally(merged)
            for key, hit in hits.items():
                if hit:
                    counts[key] += 1
            if not _passes(hits, on):
                continue
            total += 1
            if shown < MAX_ROWS:
                merged = dict(merged, _photo=hits["photo"])
                rows.append(merged)
                shown += 1
        if rows:
            days.append((date_str, rows))
    return days, total, counts, raw_total


# 스니펫 — 낱말 앞뒤로 이만큼만 잘라 한 줄로 보여준다. 넘치면 CSS가 한 줄에서 자른다.
_SNIPPET_BEFORE = 28
_SNIPPET_AFTER = 52


def _snippet(summary: str, title: str, word: str) -> str:
    """요약에서 **찾은 낱말 주변만** 한 줄로. 없으면 빈 문자열(그 줄을 안 그린다).

    접힌 행에 이 한 줄이 필요한 이유는 실측이다 — 오늘 255건에서 낱말이 **제목엔 없고
    요약에만** 있는 비율이 낱말마다 15~100%였다(「예산」 74건은 전부). 그냥 접으면
    담당자가 「왜 이게 나왔지」를 알 수 없고, 잘못 걸린 기사(「기획예산처」에 걸린 「예산」)도
    펼쳐 보기 전엔 못 거른다.

    **제목에 이미 있는 낱말은 스니펫을 안 그린다** — 제목이 이미 답이라 한 줄이 중복이다.
    `+`로 여러 조각을 넣었으면 **제목에 없는 조각**을 기준으로 자른다.
    """
    terms = split_terms(word)
    if not (summary and terms):
        return ""
    low_title, low_sum = (title or "").lower(), summary.lower()
    missing = [t for t in terms if t.lower() not in low_title]
    if not missing:
        return ""  # 조각이 전부 제목에 있다 — 제목이 곧 답이다
    at = min((low_sum.find(t.lower()) for t in missing if t.lower() in low_sum), default=-1)
    if at < 0:
        return ""
    lo = max(0, at - _SNIPPET_BEFORE)
    hi = min(len(summary), at + _SNIPPET_AFTER)
    cut = ("…" if lo > 0 else "") + summary[lo:hi] + ("…" if hi < len(summary) else "")
    return f'<div class="snip">{_highlight(cut, word)}</div>'


def _row_html(article: dict, word: str, hidden_urls: set, labels: dict, line_template: str) -> str:
    """기사 한 줄. **요약은 접혀 있고 제목 줄을 누르면 펼쳐진다**(확정본과 같은 규칙 —
    펼친 동안 제목은 `opacity`로만 흐려진다). 원문·복사는 행 오른쪽 끝 아이콘 두 칸이다."""
    url = article.get("url") or ""
    escaped_url = html.escape(url)
    # 한 건 복사는 보고서와 같은 한 줄 + 다음 줄 URL(홈 목록의 `_copy_text`와 같은 모양).
    copy_attr = html.escape(
        line_template.format(outlet=article.get("outlet") or "", title=article.get("title") or url) + "\n" + url
    )
    raw_title = article.get("title") or url
    # 말머리는 배지 없이 제목 글자색으로만(확정본·오늘 전체 기사와 같은 클래스 이름).
    # 앞뒤를 나눠 각자 _highlight를 태워, 찾은 낱말 노랑과 겹쳐도 서로 안 지운다.
    kind = headline_kind(raw_title)
    if kind:
        cut = HEADLINE_TAG_RE.match(raw_title).end()
        cls = "t-scoop" if kind == "단독" else "t-flash"
        title = (
            f'<span class="{cls}">{_highlight(raw_title[:cut], word)}</span>'
            f"{_highlight(raw_title[cut:], word)}"
        )
    else:
        title = _highlight(raw_title, word)
    outlet = article.get("outlet") or ""
    outlet_html = f'<span class="outlet">{outlet_display_label(outlet, url)}</span>' if outlet else ""
    pub = article.get("pub_date") or ""
    meta = [f"<span>{html.escape(pub[11:16])} 게시</span>"] if len(pub) >= 16 else []
    if url in hidden_urls:
        meta.append('<span class="hid">🗑️ 숨김</span>')
    meta += [
        f'<span class="lab">{html.escape(name)}</span>' for name in (labels.get(url, {}).get("labels") or [])
    ]
    summary = article.get("summary") or ""
    # 아이콘 두 칸 — 테두리 상자로 가른다. 맨 아이콘만 두면 ↗와 복사가 같은 실루엣으로
    # 보인다(2026-08-14에 확정본에서 ↗를 뺐던 이유). 휴지통 행과 같은 30×26 규격.
    acts = (
        '<span class="acts">'
        f'<a class="ib" href="{escaped_url}" target="_blank" rel="noopener" title="원문 보기"'
        f' onclick="event.stopPropagation()">{icon("external_link")}</a>'
        f'<button type="button" class="ib" title="복사" data-copy-text="{copy_attr}"'
        f' onclick="copyArchiveRow(event, this)">'
        f'<span class="icon-default">{icon("copy")}</span>'
        f'<span class="icon-done">{icon("check")}</span></button>'
        "</span>"
    )
    # 사진 추정 — 확정본과 같은 값(행 흐리게 + 📷 알약, 마우스를 올리면 또렷해진다).
    # 라벨은 표식이든 추정이든 「📷 사진 추정」 하나이고 차이는 툴팁뿐이다.
    photo = bool(article.get("_photo"))
    badge = (
        f'<span class="photo-badge" title="{html.escape(photo_badge_tip(raw_title, url, fetch_body=False))}">📷 사진 추정</span>'
        if photo
        else ""
    )
    cls = "art is-photo" if photo else "art"
    head = (
        '<div class="title-row">'
        f'<div class="a-title">{outlet_html}{title}{badge}</div>'
        f"{acts}</div>"
        f"{_snippet(summary, article.get('title') or '', word)}"
        f'<div class="a-meta">{"".join(meta)}</div>'
    )
    if not summary:
        return f'<div class="{cls}">{head}</div>'
    return (
        f'<div class="{cls}">'
        f"<details><summary>{head}</summary>"
        f'<div class="a-sum">{_highlight(summary, word)}</div>'
        "</details></div>"
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


# 필터 칸 — 이름·건수 키. 사진만 「빼고」 방향이라 세로선 오른쪽에 혼자 선다.
_FILTER_LABELS = [
    ("major", "주요 언론사만", "major"),
    ("scoop", "[단독]", "scoop"),
    ("flash", "[속보]", "flash"),
    ("nophoto", "사진 추정 제외", "photo"),
]
_FILTER_TIPS = {
    "major": "KBS·MBC·조선·중앙·동아·한경·매경·서울경제·한국일보·머니투데이·이데일리·연합·뉴시스 13곳",
    "scoop": "제목 맨 앞에 [단독]이 붙은 기사만",
    "flash": "제목 맨 앞에 [속보]가 붙은 기사만",
    "nophoto": "사진과 설명 한 문장뿐인 것으로 보이는 기사를 목록에서 뺍니다",
}


def render_agency_archive_page(
    word: str = "",
    preset: str = "",
    start: str = "",
    end: str = "",
    filters: Optional[dict] = None,
) -> str:
    word = (word or "").strip()
    dates = available_dates()
    preset = preset if preset in dict((k, 1) for k, _l, _n in _PRESETS) else ""
    date_from, date_to = _resolve_range(preset, start, end, dates)
    on = {key: bool((filters or {}).get(key)) for key in _FILTERS}

    overrides = load_summary_overrides()
    days, total, counts, raw_total = _collect(date_from, date_to, word, overrides, on)
    hidden_urls = load_hidden_urls()
    labels = load_labels()
    # 한 건 복사는 보고서와 같은 한 줄 형식을 쓴다(정기 설정 `/format`).
    line_template = load_settings().get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)

    # 눌러 둔 칸은 구간을 값으로 대조해 고른다 — 필터를 켜며 다시 조회해도 그대로 켜져 있다.
    current = _current_preset(date_from, date_to, dates)
    preset_html = "".join(
        f'<button type="submit" name="preset" value="{key}" class="{"on" if key == current else ""}">{label}</button>'
        for key, label, _n in _PRESETS
    )
    custom_badge = (
        f'<span class="qsep">직접 지정</span>' if current is None else ""
    )
    filter_html = "".join(
        (
            '<span class="fsep"></span>' if key == "nophoto" else ""
        ) + (
            f'<label title="{html.escape(_FILTER_TIPS[key])}">'
            f'<input type="checkbox" name="{key}" value="1"{" checked" if on[key] else ""}'
            ' onchange="this.form.submit()" />'
            f'{icon("camera") if key == "nophoto" else ""}{html.escape(label)}'
            f'<span class="n">{counts[count_key]}</span></label>'
        )
        for key, label, count_key in _FILTER_LABELS
    )

    if not dates:
        body = (
            '<div class="empty">💤 아직 쌓인 기관 기사가 없어요'
            '<span class="sm">앱이 켜져 있으면 30분마다 저절로 모읍니다. 첫 수집 뒤에 보여요.</span></div>'
        )
    elif not days:
        span = f"{_kr(date_from)} ~ {_kr(date_to)}"
        terms = split_terms(word)
        if len(terms) > 1:
            what = "·".join(f"「{html.escape(t)}」" for t in terms) + "가 다 든 "
            how = "기간을 넓히거나 +로 묶은 낱말을 줄여 보세요."
        elif terms:
            what = f"「{html.escape(terms[0])}」이(가) 든 "
            how = "기간을 넓히거나 낱말을 지워 보세요."
        else:
            what = ""
            how = "기간을 넓혀 보세요."
        picked = [label for key, label, _c in _FILTER_LABELS if on[key]]
        if picked and raw_total:
            # 켜 둔 칸이 실제로 깎아낸 경우다 — 기사는 있으니 「없어요」라고 하면 거짓이고,
            # 기간·낱말을 고치라는 말도 틀린 조언이다. 낱말·기간이 이미 0건이면 아래 문구
            # 그대로 둔다(칸을 꺼도 0건이라 「끄면 나옵니다」가 거짓이 된다).
            body = (
                '<div class="empty">💤 거르기에 걸려 남은 기사가 없어요'
                f'<span class="sm">끄면 {what}기관 기사 {raw_total}건이 나옵니다.<br />'
                f'지금 켜 둔 것: {html.escape(" · ".join(picked))}</span></div>'
            )
        else:
            body = (
                f'<div class="empty">💤 {html.escape(span)}에 {what}기관 기사가 없어요'
                f'<span class="sm">{how}<br />'
                f'이 화면엔 {html.escape("·".join(AGENCY_ARCHIVE_KEYWORDS))}가 든 기사만 쌓입니다.</span></div>'
            )
    else:
        sub = f"{_kr(date_from)} ~ {_kr(date_to)}" + (f" · 「{html.escape(word)}」" if word else "")
        picked = [label for key, label, _c in _FILTER_LABELS if on[key]]
        if picked:
            sub += " · " + html.escape(" · ".join(picked))
        capped = (
            f'<span class="cap">최근 {MAX_ROWS}건만 보입니다 · 낱말이나 기간으로 좁혀 보세요</span>'
            if total > MAX_ROWS
            else ""
        )
        # 날짜 묶음은 접는다 — **오늘만 펼친 채로** 연다(정기 보관함·휴지통과 같은 규칙·
        # 같은 ▸/▾). 한 달을 조회하면 날짜가 수십이라 전부 펼쳐 두면 훑을 수가 없다.
        today = datetime.now().strftime("%Y-%m-%d")
        blocks = []
        for date_str, rows in days:
            today_chip = '<span class="chip-today">오늘</span>' if date_str == today else ""
            blocks.append(
                f'<details class="dblock"{" open" if date_str == today else ""}>'
                f'<summary class="dhead">{_kr(date_str, weekday=True)} {today_chip}'
                f'<span class="dcnt">{len(rows)}건</span></summary>'
                + "".join(_row_html(a, word, hidden_urls, labels, line_template) for a in rows)
                + "</details>"
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
        custom_badge=custom_badge,
        filter_html=filter_html,
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
  /* 단색 SVG 아이콘(app.icons) 공통 크기·색 — 확정본·보관함과 같은 값. */
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
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

  /* 거르기 줄 — 오늘 전체 기사의 특수조건 줄과 같은 모양(보조 줄이라 체크는 --chk-sm).
     사진 칸만 「빼고」 방향이라 세로선으로 갈라 둔다. */
  .frow label {{ display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-sm);
    color: {text}; cursor: pointer; }}
  .frow input[type="checkbox"] {{ flex: none; width: var(--chk-sm); height: var(--chk-sm); margin: 0;
    accent-color: {accent}; cursor: pointer; }}
  .frow .n {{ font-size: var(--fs-xs); color: {text_soft}; }}
  .fsep {{ width: 1px; height: 14px; background: {border}; margin: 0 2px; }}

  /* 사진 추정 — 확정본과 같은 값(행 흐리게 + 알약, 마우스를 올리면 또렷). */
  .art.is-photo {{ opacity: 0.55; }}
  .art.is-photo:hover {{ opacity: 1; }}
  .photo-badge {{ display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: var(--r-pill);
    font-size: var(--fs-xs); font-weight: 600; color: {text_soft}; background: {bg};
    border: 1px solid {border}; white-space: nowrap; }}
  /* 말머리는 배지 없이 제목 글자색(확정본·오늘 전체 기사와 같은 값). */
  .t-scoop {{ color: {scoop_text}; font-weight: 800; }}
  .t-flash {{ color: {flash_text}; font-weight: 600; }}

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
  /* 날짜 묶음 접기 — 정기 보관함·휴지통과 같은 ▸/▾. */
  details.dblock > summary {{ cursor: pointer; list-style: none; }}
  details.dblock > summary::-webkit-details-marker {{ display: none; }}
  details.dblock > summary::before {{ content: "▸"; color: {text_soft}; font-size: 0.8rem;
    font-weight: 400; }}
  details.dblock[open] > summary::before {{ content: "▾"; }}
  details.dblock > summary:hover {{ background: {row_hover}; }}
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
  /* 제목 줄 = 제목(왼쪽) + 아이콘 두 칸(오른쪽 끝) */
  .title-row {{ display: flex; align-items: flex-start; gap: 8px; }}
  .a-title {{ flex: 1 1 auto; min-width: 0; }}
  /* 원문·복사 — 휴지통 행과 같은 30×26 테두리 상자 + 13px 아이콘. **테두리가 핵심이다**:
     맨 아이콘만 두면 ↗와 복사가 같은 실루엣으로 보인다(확정본에서 ↗를 뺐던 이유). */
  .acts {{ flex: none; display: flex; align-items: center; gap: 6px; }}
  .acts .ib {{ flex: none; display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 26px; color: {text_soft}; text-decoration: none; background: {card};
    border: 1px solid {border}; border-radius: var(--r-md); cursor: pointer; padding: 0; }}
  .acts .ib:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
  .acts .ib svg {{ width: 13px; height: 13px; fill: none; stroke: currentColor;
    stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }}
  .acts .ib .icon-done {{ display: none; }}
  .acts .ib.is-copied .icon-default {{ display: none; }}
  .acts .ib.is-copied .icon-done {{ display: inline-flex; color: {ok_text}; }}
  /* 스니펫 — 접힌 채로도 「왜 걸렸는지」가 보이게 낱말 주변만 한 줄. 넘치면 여기서 자른다. */
  .snip {{ margin-top: 2px; font-size: var(--fs-md); color: {text_soft}; line-height: 1.6;
    overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 1;
    -webkit-box-orient: vertical; }}
  /* 요약은 접혀 있다(확정본과 같은 규칙). 펼친 동안 제목은 색이 아니라 밝기로만 흐려진다. */
  .art details > summary {{ cursor: pointer; list-style: none; }}
  .art details > summary::-webkit-details-marker {{ display: none; }}
  .art details[open] .a-title {{ opacity: 0.62; }}
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
        <input type="text" name="q" value="{word}" placeholder="예: 비상경제본부 회의+전기차" />
        <button type="submit" class="btn-go">찾기</button>
      </div>
      <div class="qrow">
        <span class="qlabel">기간</span>
        <span class="preset">{preset_html}</span>
        <input type="date" name="start" value="{date_from}" />
        <span class="qsep">~</span>
        <input type="date" name="end" value="{date_to}" />
        {custom_badge}
      </div>
      <div class="qrow frow">
        <span class="qlabel">거르기</span>
        {filter_html}
      </div>
    </div>
  </form>

  {body_html}
</div>
<script>
/* 다른 화면의 copyArticleIcon과 하는 일은 같지만, 이 버튼은 <summary> 안에 있어
   **누르면 요약이 같이 펼쳐진다** — 그 한 가지 때문에 따로 둔다(공용 함수엔 그 처리가 없다). */
function copyArchiveRow(e, btn) {{
  e.preventDefault(); e.stopPropagation();
  navigator.clipboard.writeText(btn.dataset.copyText || '').then(function () {{
    btn.classList.add('is-copied');
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(function () {{ btn.classList.remove('is-copied'); }}, 1000);
  }}).catch(function () {{ alert('복사에 실패했습니다.'); }});
}}
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
