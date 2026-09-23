# 수시 보관함 — 정기 보관함처럼 「날짜 → 회차」다. 회차 줄은 `HH:MM 기준 · 사안명 · 검색어`,
# 날짜 안은 기준 시각 최신순. 사안명 검색 + 기간 조회는 AND.
# 「보기」 스위치로 묶는 축만 「사안 → 회차」로 바꿀 수 있다(_group_by_issue). 두 보기 모두
# 2단이고 층이 늘지 않는다 — 예전 「사안 → 날짜 → 회차」 3단은 기각이다(_group_by_date 참고).
# 복사/txt/엑셀은 결과 전체·날짜 두 층에 같은 모양으로 붙는다.
#
# 카드 수가 정기처럼 하루 4회씩 쌓이는 게 아니라 필요할 때만 만들어지므로, 정기
# history.html의 EAGER_HISTORY_DAYS + 지연 로딩(fetch) 같은 최적화는 아직 필요 없다 —
# 전부 한 번에 렌더링한다. 나중에 카드가 많아지면 그때 같은 패턴을 가져오면 된다.
# 복사/txt/엑셀도 같은 이유로 전용 엔드포인트 없이, 렌더링 시점에 이미 계산한 텍스트/
# rows를 hidden input에 심어 기존 공용 /download-text·/download-excel로 보낸다
# (app.history_renderer의 회차별 복사·다운로드 버튼과 정확히 같은 패턴 — PRD.md 기능11
# 규칙2 "화면과 내보내기 내용이 항상 일치해야 한다"도 그래야 저절로 지켜진다).
import html
import json
from calendar import monthrange
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import quote, urlencode

from app.adhoc import card
from app.adhoc.renderer import (
    base_page,
    build_adhoc_excel_rows,
    build_adhoc_plain_text,
    page_nav,
)
from app.config import COLOR_ERROR
from app.excel_export import date_range_label, sanitize_filename_part
from app.icons import icon

_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
_PRESET_UNITS = {"day", "week", "month", "all"}
_PRESET_LABELS = (("day", "1일"), ("week", "7일"), ("month", "1개월"), ("all", "전체"))
# 묶는 축과 사안 줄 정렬 — 주소(?view=·?sort=)로만 오간다. **기억하지 않는다**: 화면을
# 열 때는 언제나 날짜별이고, 사안별은 그때그때 눌러서 본다(사용자 결정).
_VIEW_LABELS = (("date", "날짜별"), ("issue", "사안별"))
_SORT_LABELS = (("name", "가나다순"), ("recent", "최근순"))
_VIEWS = {u for u, _ in _VIEW_LABELS}
_ISSUE_SORTS = {u for u, _ in _SORT_LABELS}


def _retention_label() -> str:
    """보관 기간을 화면 문구로 — app.history_renderer._retention_label과 같은 규칙(365일
    -> "1년"처럼 사람이 읽는 단위). [수정: 2026-08-18] "기간 제한 없이"가 실제로는 이제
    거짓말이다 — ADHOC_RETENTION_DAYS(365)를 상수에서 그대로 읽어 붙이므로, 나중에 값이
    또 바뀌어도 화면이 따라간다."""
    from app.config import ADHOC_RETENTION_DAYS

    if ADHOC_RETENTION_DAYS % 365 == 0:
        years = ADHOC_RETENTION_DAYS // 365
        return "1년간" if years == 1 else f"{years}년간"
    if ADHOC_RETENTION_DAYS % 30 == 0:
        return f"{ADHOC_RETENTION_DAYS // 30}개월간"
    return f"{ADHOC_RETENTION_DAYS}일간"


def _format_date_kr_full(date_str: str) -> str:
    """"2026-08-13" -> "8월 13일 (목)" — 날짜 층 헤더용. 사안 안에서 날짜별로 묶다 보니
    (구)_format_date_kr("8/13")만으로는 요일이 안 보여, 재수집으로 하루에 여러 회차가
    쌓였을 때 "무슨 요일에 몰아 모았는지"를 바로 못 읽는 문제가 있었다."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.month}월 {d.day}일 ({_WEEKDAY_KR[d.weekday()]})"


def _format_date_kr_short(date_str: str) -> str:
    """"2026-09-17" -> "9/17(목)" — 사안별 보기의 회차 줄 앞칸용. 날짜별 보기에선 날짜가
    머리줄에 있어 시각만 적지만, 사안 아래에선 줄마다 날짜가 다르다. 세로로 훑는 자리라
    날짜 층 헤더(_format_date_kr_full)보다 짧게 적어 시각 자리가 들쭉날쭉하지 않게 한다."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.month}/{d.day}({_WEEKDAY_KR[d.weekday()]})"


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _preset_range(unit: str, today: str) -> tuple[str, str]:
    """빠른 설정(1일/7일/1개월/전체) 버튼이 가리키는 실제 날짜 범위 — 서버가 "오늘"을
    직접 계산한다(JS로 클라이언트 시계를 흉내내지 않는다: 기기 시계가 어긋나 있어도
    항상 서버 기준 KST 오늘과 일치한다)."""
    d = datetime.strptime(today, "%Y-%m-%d")
    if unit == "day":
        return today, today
    if unit == "week":
        return (d - timedelta(days=6)).strftime("%Y-%m-%d"), today
    if unit == "month":
        last_day = monthrange(d.year, d.month)[1]
        return d.replace(day=1).strftime("%Y-%m-%d"), d.replace(day=last_day).strftime("%Y-%m-%d")
    return "", ""  # "all" — 기간 필터 없음


def _current_preset(start: str, end: str, today: str) -> Optional[str]:
    """지금 start/end가 빠른 설정 버튼 중 하나와 정확히 일치하면 그 이름을, 아니면
    None(=직접 지정)을 돌려준다 — 세그먼트 버튼의 강조 상태와 "직접 지정" 뱃지 표시에 쓴다."""
    if not start and not end:
        return "all"
    for unit in ("day", "week", "month"):
        if _preset_range(unit, today) == (start, end):
            return unit
    return None


def _range_label(start: str, end: str) -> str:
    if not start or not end:
        return "전체 기간"
    if start == end:
        return start
    return f"{start} ~ {end[5:]}"


def _condition_text(q: str, start: str, end: str) -> str:
    label = _range_label(start, end)
    return f"{label} · 사안명 '{q}'" if q else label


def _archive_url(q: str, start: str, end: str, view: str = "", sort: str = "") -> str:
    params = {}
    if q:
        params["q"] = q
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    if view == "issue":  # 날짜별은 기본값이라 주소에 안 싣는다(공유한 주소가 짧게 남는다).
        params["view"] = view
        if sort == "recent":
            params["sort"] = sort
    query = urlencode(params)
    return f"/adhoc?{query}" if query else "/adhoc"


def _mark_name(name: str, q: str) -> str:
    """사안명 검색어가 실제로 걸린 부분을 노란 배경으로 표시 — 어디가 왜 걸렸는지
    부분일치 결과를 눈으로 바로 확인할 수 있게."""
    if not q:
        return html.escape(name)
    idx = name.lower().find(q.lower())
    if idx < 0:
        return html.escape(name)
    before, matched, after = name[:idx], name[idx : idx + len(q)], name[idx + len(q) :]
    return f'{html.escape(before)}<span class="qmark">{html.escape(matched)}</span>{html.escape(after)}'


def _issue_matches_query(issue: dict, q: str) -> bool:
    return not q or q.lower() in issue["name"].lower()


def _filter_cards(all_cards: list[dict], all_issues: list[dict], q: str, start: str, end: str) -> list[dict]:
    """사안명(부분일치) + 기간(둘 다 AND) — 이미 읽어둔 카드/사안 목록에서 메모리로만
    골라낸다. 파일을 다시 읽지 않으므로 화면 렌더링과 "결과 없음" 안내(전체 기간엔
    있는지 재확인)가 같은 목록을 몇 번이고 값싸게 재사용할 수 있다."""
    cards = all_cards
    if q:
        matching_issue_ids = {i["id"] for i in all_issues if _issue_matches_query(i, q)}
        cards = [c for c in cards if c.get("issue_id") in matching_issue_ids]
    if start and end:
        cards = [c for c in cards if start <= c.get("collect_date", "") <= end]
    return cards


def _run_time(c: dict) -> str:
    """회차 줄의 기준 시각(HH:MM) — 확정본은 머리줄과 같은 `bundle_time`, 옛 원본은 수집 창의
    끝 시각. 모르면 빈 문자열(지어내지 않는다)."""
    if card.is_bundle(c):
        return card.bundle_time(c) or ""
    window = card.window_of(c)
    return (window or {}).get("end", "")


def _group_by_date(cards: list[dict]) -> "OrderedDict[str, list[dict]]":
    """카드 목록을 날짜로 묶는다 — 날짜 최신순, 날짜 안은 기준 시각 최신순(같으면 만든 시각).

    사안으로 먼저 묶지 않는다: 실측(2026-09-22, 사안 16개)에서 여러 날에 걸친 사안은 하나뿐이라
    사안 층은 한 겹 더 펼치게만 했고, 담당자가 예전 이름과 맞추려 사안명에 날짜를 적어 넣었다.
    이어지는 사안은 사안명 검색이 모아 준다. → HISTORY.md 「수시 보관함 — 사안별 묶기」
    방금 지운 카드(제자리 「삭제함」 줄)도 섞여 들어오므로 입력 순서를 믿지 않고 여기서 정렬한다.
    """
    by_date: "OrderedDict[str, list[dict]]" = OrderedDict()
    for c in sorted(cards, key=lambda c: c.get("collect_date", ""), reverse=True):
        by_date.setdefault(c.get("collect_date", ""), []).append(c)
    for d in by_date:
        by_date[d].sort(key=lambda c: (_run_time(c), c.get("created_at", "")), reverse=True)
    return by_date


# 사안 정보(issues.json)가 없는 카드의 사안명 칸 — 숨기지 않고 이 이름으로 그려야 지울 수 있다.
_ORPHAN_LABEL = "이름 없는 사안"
# 처음 열 때 펼쳐 두는 최근 날짜 수(검색 중이 아닐 때).
_OPEN_DATES = 2


def _name_sort_key(name: str) -> tuple:
    """가나다순 — 한글을 먼저, 그 뒤에 영문·숫자. 파이썬 기본 정렬은 ASCII가 한글보다
    앞서서, 그냥 정렬하면 한글 사안 사이에 영문·숫자 사안이 위로 끼어든다."""
    first = name[:1]
    return (0 if "가" <= first <= "힣" else 1, name)


def _group_by_issue(cards: list[dict], issues_by_id: dict, sort: str) -> list[tuple]:
    """카드를 사안으로 묶는다 — 「사안 → 회차」 2단. 사안 안은 날짜·기준 시각 최신순.

    사안 줄 정렬은 화면 칩이 정한다: `name`=가나다순, `recent`=그 사안의 가장 최근 회차가
    늦은 순. 사안 정보가 없는 카드(_ORPHAN_LABEL)는 어느 정렬에서도 맨 뒤다 — 이름이 없어
    가나다순에 낄 자리가 없고, 지우려고 남겨 두는 묶음이라 위로 올라오면 안 된다.
    돌려주는 것은 (사안 id, 사안명, 검색어, 카드들) 줄 목록이다.
    """
    groups: dict = OrderedDict()
    for c in cards:
        issue = issues_by_id.get(c.get("issue_id"))
        key = c["issue_id"] if issue else ""
        groups.setdefault(key, []).append(c)
    for key in groups:
        groups[key].sort(
            key=lambda c: (c.get("collect_date", ""), _run_time(c), c.get("created_at", "")),
            reverse=True,
        )
    keys = list(groups)
    if sort == "name":
        keys.sort(key=lambda k: _name_sort_key(issues_by_id[k]["name"]) if k else (2, ""))
    else:
        keys.sort(
            # 사안 안이 이미 최신순이라 맨 앞 카드가 그 사안의 가장 최근 회차다. 기준 시각까지
            # 같은 사안이 있을 수 있어 만든 시각까지 본다(같은 값이면 줄 순서가 요청마다 흔들린다).
            key=lambda k: (
                groups[k][0].get("collect_date", ""),
                _run_time(groups[k][0]),
                groups[k][0].get("created_at", ""),
            ),
            reverse=True,
        )
        keys.sort(key=lambda k: 0 if k else 1)  # 이름 없는 사안만 맨 뒤(안정 정렬이라 나머지는 그대로)
    rows = []
    for k in keys:
        issue = issues_by_id.get(k)
        name = issue["name"] if issue else _ORPHAN_LABEL
        keywords = " · ".join(issue.get("last_keywords", [])) if issue else ""
        rows.append((k, name, keywords, groups[k]))
    return rows


def _is_gone(c: dict) -> bool:
    """방금 지워 제자리에 「삭제함」 줄로 남는 카드인가(card.list_deleted_cards가 붙인 표식)."""
    return bool(c.get("_trash_name"))


def _live(cards: list[dict]) -> list[dict]:
    return [c for c in cards if not _is_gone(c)]


def _visible_count(c: dict) -> int:
    return sum(1 for a in c["articles"] if not a.get("hidden"))


def _empty_suggestion(
    all_cards: list[dict], all_issues: list[dict], q: str, start: str, end: str,
    view: str = "date", sort: str = "name",
) -> str:
    """조건에 맞는 결과가 0건일 때 "왜 없는지"와 "한 번에 고치는 길"을 같이 보여준다 —
    그냥 "없습니다"만 보여주면 사안명이 기간 밖에 있는 건지 아예 없는 건지 알 수 없다."""
    if not q:
        return ""
    by_name_only = _filter_cards(all_cards, all_issues, q, "", "")
    if by_name_only:
        dates = sorted(c["collect_date"] for c in by_name_only)
        return (
            f"'{html.escape(q)}'는 이 기간엔 없고 {dates[0]} ~ {dates[-1]}에 "
            f"{len(by_name_only)}건 있습니다. "
            f'<a href="{html.escape(_archive_url(q, "", "", view, sort))}">전체 기간에서 보기 →</a>'
        )
    return (
        f"'{html.escape(q)}'가 든 사안이 없습니다. "
        f'<a href="{html.escape(_archive_url("", start, end, view, sort))}">사안명 조건 지우기 →</a>'
    )


def _seg_links_html(options, current: str, url_fn) -> str:
    """세그먼트 버튼을 링크로 — 기간 빠른 설정(폼 제출)과 같은 모양이지만, 보기·정렬은
    조건을 바꾸는 게 아니라 같은 결과를 다르게 묶는 것이라 GET 링크 하나면 된다."""
    return "".join(
        f'<a class="seg-btn{" on" if key == current else ""}" href="{html.escape(url_fn(key))}">{label}</a>'
        for key, label in options
    )


def _render_filter_bar(q: str, start: str, end: str, today: str, view: str, sort: str) -> str:
    current = _current_preset(start, end, today)
    seg_html = "".join(
        f'<button type="submit" name="preset" value="{unit}" class="seg-btn{" on" if unit == current else ""}">{label}</button>'
        for unit, label in _PRESET_LABELS
    )
    badge_html = (
        f'<span class="badge-custom">직접 지정 · {html.escape(_range_label(start, end))}</span>'
        if current is None
        else ""
    )
    clear_q_html = (
        f'<a class="clr" href="{html.escape(_archive_url("", start, end, view, sort))}">지우기</a>'
        if q
        else ""
    )
    view_seg = _seg_links_html(_VIEW_LABELS, view, lambda k: _archive_url(q, start, end, k, sort))
    sort_html = (
        '<span class="pdiv" aria-hidden="true"></span><span class="jhint">사안 순서</span>'
        f'<div class="seg">{_seg_links_html(_SORT_LABELS, sort, lambda k: _archive_url(q, start, end, "issue", k))}</div>'
        if view == "issue"
        else '<span class="jhint">날짜별은 회차가 언제 쌓였는지, 사안별은 같은 사안이 몇 번인지 봅니다</span>'
    )
    return f"""<div class="period-bar">
  <form method="GET" action="/adhoc">
    <input type="hidden" name="start" value="{html.escape(start)}">
    <input type="hidden" name="end" value="{html.escape(end)}">
    <input type="hidden" name="view" value="{html.escape(view)}">
    <input type="hidden" name="sort" value="{html.escape(sort)}">
    <div class="prow">
      <b class="jlab">사안명</b>
      <input type="search" name="q" value="{html.escape(q)}" placeholder="사안 이름 일부만 넣어도 됩니다 (예: 세수)">
      <button type="submit" class="clr">검색</button>
      {clear_q_html}
      <span class="jhint">비워두면 이 기간의 모든 사안</span>
    </div>
    <div class="prow">
      <b class="jlab">기간</b>
      <div class="seg">{seg_html}</div>
      {badge_html}
      <span class="pdiv" aria-hidden="true"></span>
      <input type="date" name="start" value="{html.escape(start)}" form="adhoc-range-form">
      <span class="tilde">~</span>
      <input type="date" name="end" value="{html.escape(end)}" form="adhoc-range-form">
      <button type="submit" class="go" form="adhoc-range-form">조회</button>
    </div>
    <!-- 묶는 축 — 조건이 아니라 「어떻게 묶어 볼까」라서 링크다. 기억하지 않는다(늘 날짜별로 시작). -->
    <div class="prow">
      <b class="jlab">보기</b>
      <div class="seg">{view_seg}</div>
      {sort_html}
    </div>
  </form>
  <!-- [수정: 2026-09-16] 직접 입력은 기간 줄로 옮기고, 제출은 이 (보이지 않는) 폼이 맡는다 —
       날짜 칸·조회 버튼의 form= 속성이 이 폼을 가리킨다. 위 폼과 나눠 둔 이유는 예전 그대로:
       빠른 설정은 숨은 start/end를, 직접 입력은 입력한 start/end를 보내야 해서다. -->
  <form method="GET" action="/adhoc" id="adhoc-range-form" hidden>
    <input type="hidden" name="q" value="{html.escape(q)}">
    <input type="hidden" name="view" value="{html.escape(view)}">
    <input type="hidden" name="sort" value="{html.escape(sort)}">
  </form>
</div>"""


def build_plain_text_for_cards(cards: list[dict], get_text: Callable[[dict], str] = build_adhoc_plain_text) -> str:
    """여러 카드를 이어붙인 복사/txt 텍스트 — 카드 각각의 build_adhoc_plain_text를 그대로
    이어붙인다(카드 화면과 서식이 어긋나지 않도록, 같은 함수를 재사용)."""
    if not cards:
        return ""
    return "\n\n".join(get_text(c).rstrip("\n") for c in cards) + "\n"


def _scope_filename(cards: list[dict], scope_name: Optional[str], ext: str) -> str:
    dates = [c["collect_date"] for c in cards]
    label = date_range_label(dates) if dates else _today_str()
    if scope_name is None:  # 날짜 줄 — 정기 보관함 날짜 파일처럼 구분 자리 없이
        return f"{label}_수시모니터링.{ext}"
    safe = sanitize_filename_part(scope_name) if scope_name else "전체"
    return f"{label}_수시모니터링_{safe}.{ext}"


def _export_buttons_html(
    cards: list[dict], scope_name: Optional[str], get_text: Callable[[dict], str], size_cls: str = ""
) -> str:
    """복사/txt/엑셀 — 결과 전체·사안·날짜 세 층에서 이 함수 하나를 그대로 재사용한다.
    렌더링 시점에 이미 계산해둔 텍스트/rows를 hidden input에 심어 기존 공용
    /download-text·/download-excel로 보낸다(app.history_renderer 회차별 버튼과 같은 패턴).
    event.stopPropagation()만 걸고 preventDefault()는 안 쓴다 — 버튼 자체의 기본 동작
    (form 제출)까지 막으면 안 되고, <summary> 안에서 클릭이 토글로 번지는 것만 막으면
    된다(같은 이유로 app.history_renderer._slot_export_actions_html도 이렇게 한다)."""
    if not cards:
        return ""
    copy_text = build_plain_text_for_cards(cards, get_text)
    rows: list[dict] = []
    for c in cards:
        rows.extend(build_adhoc_excel_rows(c))
    copy_attr = html.escape(copy_text)
    rows_json = html.escape(json.dumps(rows, ensure_ascii=False))
    txt_filename = html.escape(_scope_filename(cards, scope_name, "txt"))
    xlsx_filename = html.escape(_scope_filename(cards, scope_name, "xlsx"))
    return (
        f'<span class="exp{size_cls}">'
        f'<button type="button" onclick="event.stopPropagation(); copyArchiveText(this);" '
        f'data-copy-text="{copy_attr}">복사</button>'
        f'<form method="POST" action="/download-text" style="display:contents" onclick="event.stopPropagation();">'
        f'<input type="hidden" name="filename" value="{txt_filename}">'
        f'<input type="hidden" name="text" value="{copy_attr}">'
        f'<button type="submit">텍스트</button>'
        f"</form>"
        f'<form method="POST" action="/download-excel" style="display:contents" onclick="event.stopPropagation();">'
        f'<input type="hidden" name="filename" value="{xlsx_filename}">'
        f'<input type="hidden" name="rows" value="{rows_json}">'
        f'<button type="submit" class="xls">엑셀</button>'
        f"</form>"
        f"</span>"
    )


def _time_html(c: dict) -> str:
    # 확정본이든 옛 원본이든 기준 시각 하나로 적는다 — 보고서 머리줄(`… N시 M분 기준`)과
    # 같은 축이다. 시각이 없는 옛 확정본만 예전 청록 「확정본」 칩.
    t = _run_time(c)
    if t:
        return f'<span class="win">{html.escape(t)} 기준</span>'
    return '<span class="win"><span class="bundle-mark">확정본</span></span>'


def _issue_cells_html(issue: Optional[dict], q: str) -> str:
    """회차 줄의 「사안명 · 검색어」 — 사안 정보가 없으면 「이름 없는 사안」, 검색어는 비운다."""
    if issue is None:
        return f'<span class="iss noname">{_ORPHAN_LABEL}</span><span class="kwv"></span>'
    keywords = " · ".join(issue.get("last_keywords", []))
    return (
        f'<span class="iss">{_mark_name(issue["name"], q)}</span>'
        f'<span class="kwv">{html.escape(keywords)}</span>'
    )


def _date_time_html(c: dict) -> str:
    """사안별 보기의 회차 줄 앞칸 — 「9/17(목) 15:05 기준」. 사안 아래에선 줄마다 날짜가
    다르므로 날짜를 같은 칸에 붙여 적는다(시각 규칙은 _time_html과 같다)."""
    date_str = c.get("collect_date") or ""
    date_label = f"{_format_date_kr_short(date_str)} " if date_str else ""
    t = _run_time(c)
    inner = f"{html.escape(t)} 기준" if t else '<span class="bundle-mark">확정본</span>'
    return f'<span class="win wd">{html.escape(date_label)}{inner}</span>'


def _render_run_row(c: dict, today: str, issue: Optional[dict], q: str = "", view: str = "date") -> str:
    """회차(카드) 한 줄 — 「체크박스 · [시각 기준 · 사안명 · 검색어 · N건 · 열기] · 🗑」.

    체크박스와 🗑를 링크 **밖에** 두는 건 링크 안의 버튼·입력칸은 누르는 순간 링크 이동과
    엉키기 때문이다. 🗑는 확인창 없이 바로 지운다 — 지운 자리에 「삭제함 · 되살리기」가 곧바로
    남는다. 방금 지운 카드(_is_gone)는 링크 대신 그 「삭제함」 줄로 그린다.
    """
    cid = html.escape(c["id"])
    count = _visible_count(c)
    if view == "issue":
        # 사안명·검색어는 머리줄이 이미 말한다 — 그 자리에 날짜를 적는다. 빈 .kwv는 남은
        # 폭을 채워 건수·열기가 날짜별 보기와 같은 자리에 서게 한다.
        cells = _date_time_html(c) + '<span class="kwv"></span>'
    else:
        cells = _time_html(c) + _issue_cells_html(issue, q)
    if _is_gone(c):
        return (
            f'<div class="run-line gone" data-card-id="{cid}">'
            '<span class="pick-sp"></span>'
            f'<span class="run-row">{cells}<span class="n">{count}건</span></span>'
            '<span class="gone-tag">삭제함</span>'
            f'<button type="button" class="restore-btn" data-trash="{html.escape(c["_trash_name"])}" '
            'onclick="restoreCards([this.dataset.trash])">되살리기</button>'
            "</div>"
        )
    is_today = c.get("collect_date") == today
    bundle_attr = ' data-bundle="1"' if card.is_bundle(c) else ""
    return (
        f'<div class="run-line{" is-today" if is_today else ""}" data-card-id="{cid}" '
        f'data-count="{count}"{bundle_attr}>'
        f'<input type="checkbox" class="pick" data-id="{cid}" aria-label="이 회차 고르기" onchange="onPickChange()">'
        f'<a class="run-row" href="/adhoc/card?id={quote(c["id"])}">'
        f'{cells}<span class="n">{count}건</span><span class="go">열기</span></a>'
        f'<button type="button" class="row-del" data-id="{cid}" title="이 회차 삭제" '
        f'onclick="deleteCards([this.dataset.id])">{icon("trash")}</button>'
        "</div>"
    )


def _group_pick_html(live_cards: list[dict]) -> str:
    """사안·날짜 줄의 체크박스 — 「그 아래 회차 전부 고르기」. 살아 있는 회차가 없으면
    (전부 방금 지워 「삭제함」 줄만 남았으면) 고를 게 없으므로 자리만 둔다."""
    if not live_cards:
        return '<span class="pick-sp"></span>'
    return (
        '<input type="checkbox" class="pick pick-group" aria-label="그 아래 회차 전부 고르기" '
        'onclick="event.stopPropagation()" onchange="pickGroup(this)">'
    )


def _render_group_block(
    label: str,
    title_html: str,
    cards: list[dict],
    issues_by_id: dict,
    q: str,
    get_text: Callable[[dict], str],
    today: str,
    is_open: bool,
    view: str = "date",
    unit: str = "날짜",
    scope_name: Optional[str] = None,
    block_cls: str = "",
    mid_html: str = "",
    chip_html: str = "",
) -> str:
    """머리줄 한 줄(카드) + 그 아래 회차 줄들 — 날짜별 보기는 날짜가, 사안별 보기는 사안명이
    머리줄이다(두 보기가 같은 마크업·같은 CSS를 쓴다).

    줄 끝 🗑 = 「이 {unit}의 회차 모두 삭제」, 확인창 한 번(회차 줄 🗑는 확인창 없음). 지우는 건
    지금 화면에 보이는 회차만이다(사안명 검색 중이면 걸린 회차만). 묶음이 통째로 「삭제함」이
    되면 그 줄에 「모두 되살리기」를 둔다.
    """
    live = _live(cards)
    rows_html = "".join(
        _render_run_row(c, today, issues_by_id.get(c.get("issue_id")), q, view) for c in cards
    )
    if live:
        total_articles = sum(_visible_count(c) for c in live)
        meta_html = f'<span class="dcnt">{total_articles}건</span>'
        meta_html += _export_buttons_html(live, scope_name, get_text)
        meta_html += (
            f'<button type="button" class="row-del day-del" title="이 {unit}의 회차 모두 삭제" '
            f'onclick="event.preventDefault(); event.stopPropagation(); deleteDay(this);">{icon("trash")}</button>'
        )
    else:
        gone = [c for c in cards if _is_gone(c)]
        restore_all = (
            '<button type="button" class="restore-btn" '
            f'data-trash="{html.escape(json.dumps([c["_trash_name"] for c in gone]))}" '
            'onclick="event.preventDefault(); event.stopPropagation(); '
            'restoreCards(JSON.parse(this.dataset.trash));">모두 되살리기</button>'
            if len(gone) > 1
            else ""
        )
        meta_html = f'<span class="n"><span class="gone-tag">삭제함</span>{restore_all}</span>'
    return f"""<details class="date-block{block_cls}"{' open' if is_open else ''} data-day-label="{html.escape(label)}" data-unit="{unit}">
  <summary>{_group_pick_html(live)}{title_html}{chip_html}{mid_html}
    {meta_html}</summary>
  <div class="runs">{rows_html}</div>
</details>"""


def _render_date_block(
    date_str: str,
    cards: list[dict],
    issues_by_id: dict,
    q: str,
    get_text: Callable[[dict], str],
    today: str,
    is_open: bool,
) -> str:
    """날짜 한 줄 — 「8월 13일 (목) · N건 · 복사/txt/xlsx · 🗑」."""
    date_label = _format_date_kr_full(date_str)
    return _render_group_block(
        label=date_label,
        title_html=f'<span class="dt">{date_label}</span>',
        cards=cards,
        issues_by_id=issues_by_id,
        q=q,
        get_text=get_text,
        today=today,
        is_open=is_open,
        chip_html='<span class="chip-today">오늘</span>' if date_str == today else "",
    )


def _render_issue_block(
    name: str,
    keywords: str,
    is_orphan: bool,
    cards: list[dict],
    issues_by_id: dict,
    q: str,
    get_text: Callable[[dict], str],
    today: str,
    is_open: bool,
) -> str:
    """사안 한 줄 — 「사안명 · 검색어 · N건 · 복사/txt/xlsx · 🗑」. 오늘 회차가 하나라도
    들어 있으면 날짜 줄과 같은 「오늘」 칩을 단다(최근순 정렬에서 맨 위를 알아보기 쉽게)."""
    name_html = (
        f'<span class="dt noname">{_ORPHAN_LABEL}</span>'
        if is_orphan
        else f'<span class="dt">{_mark_name(name, q)}</span>'
    )
    return _render_group_block(
        label=name,
        title_html=name_html,
        cards=cards,
        issues_by_id=issues_by_id,
        q=q,
        get_text=get_text,
        today=today,
        is_open=is_open,
        view="issue",
        unit="사안",
        scope_name=name,
        block_cls=" issue-block",
        mid_html=f'<span class="ikw">{html.escape(keywords)}</span>',
        chip_html=(
            '<span class="chip-today">오늘</span>'
            if any(c.get("collect_date") == today for c in _live(cards))
            else ""
        ),
    )


def _deleted_when_label(deleted_at: datetime, today: str) -> str:
    """「최근 삭제」 묶음 머리의 지운 시각 — 오늘이면 "오늘 10:12", 아니면 "9월 10일 10:12"."""
    hm = deleted_at.strftime("%H:%M")
    if deleted_at.strftime("%Y-%m-%d") == today:
        return f"오늘 {hm}"
    return f"{deleted_at.month}월 {deleted_at.day}일 {hm}"


def _render_recent_deleted(deleted_cards: list[dict], issues_by_id: dict, today: str) -> str:
    """맨 아래 「최근 삭제」 — 제자리 줄(지운 직후 화면)이 아닌, 이전에 지운 카드 전부.

    [추가: 2026-09-11] 수시는 정기와 달리 지운 자리가 목록에 남지 않는다(사안·날짜 줄이
    카드로만 생기므로 비면 줄째 사라진다). 그래서 다시 열었을 때 되살릴 곳을 한 군데로
    모은다. 기본은 접힌 한 줄이고, 한 번에 지운 것(같은 초에 옮긴 것)은 한 묶음으로 보인다.
    기한은 없다 — trash를 사람이 직접 비우기 전까지 여기서 되살릴 수 있다.
    """
    if not deleted_cards:
        return ""
    batches: "OrderedDict[datetime, list[dict]]" = OrderedDict()
    for c in deleted_cards:  # 이미 지운 시각 최신 먼저
        batches.setdefault(c["_deleted_at"], []).append(c)
    parts = []
    for deleted_at, cards in batches.items():
        rows = []
        for c in cards:
            issue = issues_by_id.get(c.get("issue_id"))
            name_html = (
                html.escape(issue["name"]) if issue else f'<i class="noname">{_ORPHAN_LABEL}</i>'
            )
            window = (f"{card.bundle_time(c)} 기준" if card.bundle_time(c) else "확정본") if card.is_bundle(c) else (
                f'{card.window_of(c)["start"]}~{card.window_of(c)["end"]}'
            )
            date_label = _format_date_kr_full(c["collect_date"]) if c.get("collect_date") else ""
            rows.append(
                f'<div class="rrow"><span class="rnm">{name_html}</span>'
                f'<span class="rmeta">{html.escape(date_label)} {html.escape(window)} · {_visible_count(c)}건</span>'
                f'<button type="button" class="restore-btn" data-trash="{html.escape(c["_trash_name"])}" '
                'onclick="restoreCards([this.dataset.trash])">되살리기</button></div>'
            )
        all_btn = (
            '<button type="button" class="restore-btn" '
            f'data-trash="{html.escape(json.dumps([c["_trash_name"] for c in cards]))}" '
            'onclick="restoreCards(JSON.parse(this.dataset.trash))">모두 되살리기</button>'
            if len(cards) > 1
            else ""
        )
        parts.append(
            f'<div class="rbatch"><div class="rbatch-h">{_deleted_when_label(deleted_at, today)}에 삭제 · '
            f"회차 {len(cards)}개{all_btn}</div>{''.join(rows)}</div>"
        )
    return (
        f'<details class="recent-del"><summary>{icon("trash")} 최근 삭제 · 회차 {len(deleted_cards)}개</summary>'
        f'{"".join(parts)}</details>'
    )


def _tidy_controls_html(empty_count: int) -> str:
    """결과 줄 오른쪽 — 평소엔 [선택 삭제], 선택 삭제 중엔 안내·빈 회차 고르기·[완료].

    [빈 회차(0건) 모두 고르기]는 그런 회차가 있을 때만 그린다(📷 사진 추정 전체 선택과
    같은 규칙). 오늘 만든 카드는 세지 않는다 — 방금 만들어 아직 수집 전일 수 있다.
    """
    empty_btn = (
        f'<button type="button" class="pick-empty" onclick="pickEmptyCards()">빈 회차(0건) 모두 고르기 ({empty_count})</button>'
        if empty_count
        else ""
    )
    return (
        '<button type="button" class="tidy-btn normal-only" onclick="setArchiveTidy(true)">선택 삭제</button>'
        f'<span class="tidy-bar tidy-only"><span class="tidy-hint">지울 회차를 고르세요</span>{empty_btn}'
        '<button type="button" class="done-btn" onclick="setArchiveTidy(false)">완료</button></span>'
    )


_SEL_BAR_HTML = f"""<div class="sel-bar" id="arch-selbar">
  <span>회차 <b id="arch-sel-n">0</b>개 · 기사 <b id="arch-sel-a">0</b>건 골랐어요</span><span class="sp"></span>
  <button type="button" class="clr" onclick="clearPicks()">선택 해제</button>
  <button type="button" class="btn danger sm" onclick="deletePicked()">{icon("trash")}<span><span id="arch-sel-n2">0</span>개 삭제</span></button>
</div>"""


def render_archive_page(
    q: str = "",
    start: str = "",
    end: str = "",
    preset: str = "",
    deleted_ids: Optional[list] = None,
    restored_id: str = "",
    view: str = "",
    sort: str = "",
) -> str:
    """수시 보관함 — 날짜 → 회차 2단(회차 줄에 사안명).

    view="issue"면 같은 결과를 사안 → 회차로 묶어 보여준다(회차 줄 앞칸이 날짜+시각).
    sort는 사안별 보기에서만 쓰는 사안 줄 정렬(name=가나다순 / recent=최근순).
    기본은 언제나 날짜별이다 — 고른 보기를 기억하지 않는다(사용자 결정).
    카드가 하나도 없는 사안은 어디에도 안 보인다(사안 자체는 issues.json에 계속 남아
    "원본" 화면의 검색어 자동완성용으로 쓰인다 — ADHOC_DESIGN.md §3.5, 여기 안
    보인다고 사안이 삭제된 게 아니다).

    [추가: 2026-09-11] deleted_ids — 방금 지운 카드 id(삭제 후 되돌아온 주소의 `deleted`).
    그 카드들은 목록에서 빠지는 대신 제자리에 「삭제함 · 되살리기」 줄로 남는다. 주소에
    실려 있을 때만이라, 화면을 다시 열면(조회를 다시 누르는 등) 맨 아래 「최근 삭제」로
    옮겨간다. restored_id — 방금 되살린 카드. 그 줄로 스크롤해 잠깐 칠해 보여준다.
    """
    q = q.strip()
    today = _today_str()
    deleted_ids = set(deleted_ids or [])
    view = view if view in _VIEWS else "date"
    sort = sort if sort in _ISSUE_SORTS else "name"

    if preset in _PRESET_UNITS:
        start, end = _preset_range(preset, today)
    elif bool(start) != bool(end):
        # 한쪽만 채워진 반쪽 상태(URL을 손으로 건드린 경우 등)는 조용히 무필터로 취급.
        start, end = "", ""

    range_error = ""
    if start and end and start > end:
        range_error = f"시작일({start})이 종료일({end})보다 늦어 전체 기간으로 조회했습니다."
        start, end = "", ""

    # [수정: 2026-08-18] 사안마다 list_cards(issue_id=...)를 부르면 그때마다 카드 파일을
    # **전부** 다시 읽는다 — O(사안 수 x 카드 수)다. 실측(사안 40개·카드 365개 13.7MB):
    # 1,415ms. 한 번만 읽고 메모리에서 사안·날짜별로 나누면 38ms(37배). 수시는 보관 기간
    # 제한이 있어도 카드가 꽤 쌓이는 쪽이라 지금부터 잡아둔다(CODING_CONVENTIONS.md §1).
    # [수정: 2026-09-15] 로데이터 수집 원본은 보관함에 안 쌓인다 — 보관함은 정기 보관함처럼
    # 보고서(수집 확정본)가 쌓이는 곳이고, 원본은 새 수집의 「지난 수집」에서 다시 찾는다(수시
    # 4단 흐름). 필드가 없는 옛 원본(소제목·숨김까지 하던 「원본 겸 확정본」)은 그대로 남긴다
    # (사용자 결정). 지운 원본은 「최근 삭제」에 그대로 둬 되살릴 수 있게 한다.
    all_cards = [c for c in card.list_cards() if not card.is_raw(c)]
    trashed = card.list_deleted_cards()
    gone_cards = [c for c in trashed if c["id"] in deleted_ids]
    recent_cards = [c for c in trashed if c["id"] not in deleted_ids]

    # 사안 목록도 카드처럼 한 번만 읽어 이 함수 안에서 재사용한다 — q 매칭
    # (_filter_cards)과 사안 이름 조회(issues_by_id) 둘 다 issues.json을 다시 읽지 않는다.
    all_issues = card.load_issues()
    issues_by_id = {i["id"]: i for i in all_issues}
    recent_html = _render_recent_deleted(recent_cards, issues_by_id, today)
    page_script = _ARCHIVE_SCRIPT.replace("__RESTORED_ID__", json.dumps(restored_id))

    if not all_cards and not gone_cards:
        body = f"""
<div class="card">
  <h1 class="page-title">수시 보관함</h1>
  <p class="empty">아직 보관된 확정본이 없습니다.</p>
  <a class="btn" href="/adhoc/new">{icon("search")} 새 수집 시작</a>
  {recent_html}
</div>"""
        return base_page("수시 모니터링 — 수시 보관함", body, script=page_script, nav=page_nav("archive"))

    sub_label = "사안별로 묶어 본 확정본" if view == "issue" else "날짜별로 쌓인 확정본"
    filter_bar_html = _render_filter_bar(q, start, end, today, view, sort)
    error_html = (
        f'<p class="fixhint" style="color:{COLOR_ERROR}">{html.escape(range_error)}</p>' if range_error else ""
    )
    filtered = _filter_cards(all_cards, all_issues, q, start, end)
    gone_filtered = _filter_cards(gone_cards, all_issues, q, start, end)

    if not filtered and not gone_filtered:
        suggestion = _empty_suggestion(all_cards, all_issues, q, start, end, view, sort)
        body = f"""
<div class="card">
  <h1 class="page-title">수시 보관함</h1>
  <p class="page-sub">{sub_label} · {_retention_label()} 보관됩니다</p>
  {filter_bar_html}
  {error_html}
  <p class="result-line">{html.escape(_condition_text(q, start, end))} · <b>결과 없음</b></p>
  {f'<p class="fixhint">{suggestion}</p>' if suggestion else ''}
  <p class="empty">조건에 맞는 수집 결과가 없습니다.</p>
  {recent_html}
</div>"""
        return base_page(
            "수시 모니터링 — 수시 보관함", body, script=page_script, nav=page_nav("archive")
        )

    shown_cards = filtered + gone_filtered

    # build_adhoc_plain_text(카드)는 결과 전체(top) → 날짜 두 층에서 카드별로 두 번 쓰이므로,
    # 카드 1건당 한 번만 계산해 나눠 쓴다.
    text_cache: dict[str, str] = {}

    def cached_text(c: dict) -> str:
        cid = c["id"]
        if cid not in text_cache:
            text_cache[cid] = build_adhoc_plain_text(c)
        return text_cache[cid]

    focus_ids = deleted_ids | ({restored_id} if restored_id else set())
    # 펼침: 사안명으로 검색했으면 걸린 묶음 전부, 방금 지우거나 되살린 카드가 든 묶음도 전부.
    # 그 밖에는 날짜별만 최근 두 날짜(_OPEN_DATES)를 펼친다 — 사안별은 이름을 훑는 화면이라
    # 처음엔 모두 접어 둔다(가나다순에서 "앞 두 개"는 최근이라는 뜻이 아니다).
    if view == "issue":
        sections = [
            _render_issue_block(
                name,
                keywords,
                not key,
                cards,
                issues_by_id,
                q,
                cached_text,
                today=today,
                is_open=bool(q) or any(c["id"] in focus_ids for c in cards),
            )
            for key, name, keywords, cards in _group_by_issue(shown_cards, issues_by_id, sort)
        ]
    else:
        sections = [
            _render_date_block(
                d,
                cards,
                issues_by_id,
                q,
                cached_text,
                today=today,
                is_open=bool(q) or i < _OPEN_DATES or any(c["id"] in focus_ids for c in cards),
            )
            for i, (d, cards) in enumerate(_group_by_date(shown_cards).items())
        ]

    top_export = _export_buttons_html(filtered, q or "전체", cached_text, size_cls=" lg")
    empty_count = sum(1 for c in filtered if _visible_count(c) == 0 and c.get("collect_date") != today)

    body = f"""
<div class="card">
  <h1 class="page-title">수시 보관함</h1>
  <p class="page-sub">{sub_label} · {_retention_label()} 보관됩니다</p>
  {filter_bar_html}
  {error_html}
  <div class="result-bar">
    <p class="result-line">{html.escape(_condition_text(q, start, end))}</p>
    {_tidy_controls_html(empty_count)}
    {top_export}
  </div>
  {_SEL_BAR_HTML}
  {"".join(sections)}
  {recent_html}
</div>"""

    # 보관함은 "홈" 하나만 — 수집 원본·카드로 가는 길은 이 화면 본문(목록/버튼)이
    # 이미 제공하므로 상단바에서 중복으로 안 만든다(사용자 지정).
    return base_page(
        "수시 모니터링 — 수시 보관함",
        body,
        script=page_script,
        nav=page_nav("archive"),
    )


# [추가: 2026-09-11] 삭제·되살리기는 이 화면의 다른 동작처럼 실제 <form> 제출(POST →
# 303 → GET)이다. 되돌아온 주소에 방금 지운 id(deleted)·되살린 id(restored)가 실려 오고,
# 서버가 그 줄이 든 사안·날짜를 펼친 채로 그린다 — 여기서는 그 줄로 스크롤만 한다
# (예전 스크롤 위치를 기억하는 것보다 확실하다: 펼침 상태가 바뀌면 위치도 달라진다).
_ARCHIVE_SCRIPT = """<script>
function copyArchiveText(btn) {
  navigator.clipboard.writeText(btn.dataset.copyText).then(function () {
    var original = btn.textContent;
    btn.textContent = '복사했습니다';
    setTimeout(function () { btn.textContent = original; }, 900);
  }).catch(function () {
    alert('복사에 실패했습니다.');
  });
}
function archivePost(action, name, values) {
  var form = document.createElement('form');
  form.method = 'POST';
  form.action = action;
  values.forEach(function (v) {
    var input = document.createElement('input');
    input.type = 'hidden'; input.name = name; input.value = v;
    form.appendChild(input);
  });
  var back = document.createElement('input');
  back.type = 'hidden'; back.name = 'back'; back.value = location.pathname + location.search;
  form.appendChild(back);
  document.body.appendChild(form);
  form.submit();
}
// 🗑 하나든 선택 삭제든 같은 길 — 확인창은 선택 삭제(deletePicked)에서만 띄운다.
function deleteCards(ids) { if (ids.length) { archivePost('/adhoc/archive/delete', 'id', ids); } }
function restoreCards(names) { if (names.length) { archivePost('/adhoc/archive/restore', 'name', names); } }

function setArchiveTidy(on) {
  document.body.classList.toggle('arch-tidy', on);
  if (!on) { clearPicks(); }
}
function pickedLines() {
  return Array.prototype.filter.call(document.querySelectorAll('.run-line input.pick'), function (cb) { return cb.checked; })
    .map(function (cb) { return cb.closest('.run-line'); });
}
function onPickChange() {
  var lines = pickedLines();
  var articles = lines.reduce(function (sum, l) { return sum + Number(l.dataset.count || 0); }, 0);
  document.getElementById('arch-sel-n').textContent = lines.length;
  document.getElementById('arch-sel-n2').textContent = lines.length;
  document.getElementById('arch-sel-a').textContent = articles.toLocaleString();
  document.getElementById('arch-selbar').classList.toggle('on', lines.length > 0);
}
// 날짜 줄 체크 = 그 아래 회차 전부(/hidden 묶음 체크와 같은 한 방향 — 개별 해제로 머리가 안 풀린다)
function pickGroup(box) {
  var block = box.closest('details');
  block.querySelectorAll('input.pick').forEach(function (cb) { if (cb !== box) { cb.checked = box.checked; } });
  if (box.checked) { block.open = true; block.querySelectorAll('details').forEach(function (d) { d.open = true; }); }
  onPickChange();
}
function clearPicks() {
  document.querySelectorAll('input.pick').forEach(function (cb) { cb.checked = false; });
  onPickChange();
}
function pickEmptyCards() {
  document.querySelectorAll('.run-line[data-count="0"]:not(.is-today) input.pick').forEach(function (cb) {
    cb.checked = true;
    var d = cb.closest('details.date-block'); if (d) { d.open = true; }
  });
  onPickChange();
}
function deletePicked() {
  var lines = pickedLines();
  if (!lines.length) { return; }
  var articles = lines.reduce(function (sum, l) { return sum + Number(l.dataset.count || 0); }, 0);
  var picked = new Set(lines);
  var blocks = document.querySelectorAll('details.date-block');
  var unit = blocks.length ? (blocks[0].dataset.unit || '날짜') : '날짜';
  var goneDays = Array.prototype.filter.call(blocks, function (block) {
    var live = block.querySelectorAll('.run-line:not(.gone)');
    return live.length && Array.prototype.every.call(live, function (l) { return picked.has(l); });
  }).map(function (block) { return block.dataset.dayLabel; });
  var msg = '회차 ' + lines.length + '개를 삭제할까요?\\n기사 ' + articles.toLocaleString() + '건';
  if (goneDays.length) { msg += ' · ' + unit + ' ' + goneDays.length + '개가 목록에서 사라져요\\n(' + goneDays.join(', ') + ')'; }
  if (lines.some(function (l) { return l.dataset.bundle; })) {
    msg += '\\n확정본을 지우면 원본의 「✓ …로 보냄」 표시도 함께 풀려요.';
  }
  msg += '\\n\\n지운 자리에서 바로 되살릴 수 있어요.';
  if (!confirm(msg)) { return; }
  deleteCards(lines.map(function (l) { return l.dataset.cardId; }));
}
// 머리줄 🗑 — 그 날짜(사안별 보기에선 그 사안) 아래 지금 보이는 회차 전부. 확인창 한 번.
function deleteDay(btn) {
  var block = btn.closest('details.date-block');
  var lines = Array.prototype.slice.call(block.querySelectorAll('.run-line:not(.gone)'));
  if (!lines.length) { return; }
  var articles = lines.reduce(function (sum, l) { return sum + Number(l.dataset.count || 0); }, 0);
  var msg = '「' + block.dataset.dayLabel + '」의 회차 ' + lines.length + '개(기사 '
    + articles.toLocaleString() + '건)를 지울까요?';
  if (lines.some(function (l) { return l.dataset.bundle; })) {
    msg += '\\n확정본을 지우면 원본의 「✓ …로 보냄」 표시도 함께 풀려요.';
  }
  msg += '\\n\\n지운 자리에서 바로 되살릴 수 있어요.';
  if (!confirm(msg)) { return; }
  deleteCards(lines.map(function (l) { return l.dataset.cardId; }));
}
// 선택 삭제 중엔 회차 줄을 눌러도 카드가 열리지 않고 체크된다(고르려다 튀어나가지 않게)
document.addEventListener('click', function (e) {
  if (!document.body.classList.contains('arch-tidy')) { return; }
  var link = e.target.closest('a.run-row');
  if (!link) { return; }
  e.preventDefault();
  var cb = link.closest('.run-line').querySelector('input.pick');
  cb.checked = !cb.checked;
  onPickChange();
}, true);
(function () {
  var restored = __RESTORED_ID__;
  var target = document.querySelector('.run-line.gone')
    || (restored && document.querySelector('.run-line[data-card-id="' + restored + '"]'));
  if (!target) { return; }
  target.scrollIntoView({block: 'center'});
  if (!target.classList.contains('gone')) {
    target.classList.add('just-restored');
    setTimeout(function () { target.classList.remove('just-restored'); }, 1500);
  }
})();
</script>"""
