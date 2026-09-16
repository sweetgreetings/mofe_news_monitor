# Design Ref: ADHOC_DESIGN.md §5.4 "지난 사안 더보기" — 정기의 지난 기사 더보기가
# 「날짜 → 시간대」인 것과 축이 다르다. 여긴 「사안 → 날짜 → 회차」다: 담당자가 같은
# 사안을 여러 날 추적하며 쌓아온 카드들을 한데 모아 보여준다.
#
# [수정: 2026-08-20] 사안명 검색 + 기간 조회(1일/7일/1개월/전체 빠른 설정 + 시작일~종료일
# 직접 입력)를 추가하면서 "사안 → 회차" 2단이 "사안 → 날짜 → 회차" 3단이 됐다 — 같은
# 사안을 하루에 여러 번 재수집한 경우를 날짜로 한 번 더 묶어야 조회 결과가 읽기 편하다.
# 복사/txt/엑셀은 결과 전체·사안·날짜 세 층 모두에 같은 모양으로 붙는다.
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
    NAV_HOME,
    base_page,
    build_adhoc_excel_rows,
    build_adhoc_plain_text,
    nav_html,
)
from app.config import COLOR_ERROR
from app.excel_export import date_range_label, sanitize_filename_part
from app.icons import icon

_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
_PRESET_UNITS = {"day", "week", "month", "all"}
_PRESET_LABELS = (("day", "1일"), ("week", "7일"), ("month", "1개월"), ("all", "전체"))


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


def _archive_url(q: str, start: str, end: str) -> str:
    params = {}
    if q:
        params["q"] = q
    if start:
        params["start"] = start
    if end:
        params["end"] = end
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


def _group_by_issue_and_date(
    cards: list[dict], known_issue_ids: Optional[set] = None
) -> "OrderedDict[str, OrderedDict[str, list[dict]]]":
    """카드 목록을 사안 → 날짜로 묶는다(만든 시각 최신순).

    [수정: 2026-09-11] 방금 지운 카드(제자리 「삭제함」 줄)가 살아 있는 카드 사이에 섞여
    들어오므로, 입력 순서를 믿지 않고 여기서 한 번 정렬한다.

    known_issue_ids: 사안 정보(issues.json)가 있는 사안 id. 여기 없는 카드는 전부 한
    묶음(_ORPHAN_KEY, 화면의 「이름 없는 사안」)으로 모아 **맨 뒤**에 둔다 — 예전엔
    통째로 건너뛰어 보관함 어디에도 안 보이면서 상단 합계에는 들어가 숫자가 안 맞았고,
    보이지 않으니 지울 방법도 없었다(실측 2026-09-11: 8/21 시험 카드 6개).
    """
    by_issue: "OrderedDict[str, OrderedDict[str, list[dict]]]" = OrderedDict()
    orphans: "OrderedDict[str, list[dict]]" = OrderedDict()
    for c in sorted(cards, key=lambda c: c.get("created_at", ""), reverse=True):
        issue_id = c.get("issue_id")
        if known_issue_ids is not None and issue_id not in known_issue_ids:
            orphans.setdefault(c.get("collect_date", ""), []).append(c)
            continue
        by_date = by_issue.setdefault(issue_id, OrderedDict())
        by_date.setdefault(c.get("collect_date", ""), []).append(c)
    if orphans:
        by_issue[_ORPHAN_KEY] = orphans
    return by_issue


# 사안 정보가 없는 카드들의 묶음 — 이름 칸에 「이름 없는 사안」을 그린다.
_ORPHAN_KEY = "__orphan__"
_ORPHAN_ISSUE = {"id": _ORPHAN_KEY, "name": "", "last_keywords": []}
_ORPHAN_LABEL = "이름 없는 사안"


def _is_gone(c: dict) -> bool:
    """방금 지워 제자리에 「삭제함」 줄로 남는 카드인가(card.list_deleted_cards가 붙인 표식)."""
    return bool(c.get("_trash_name"))


def _live(cards: list[dict]) -> list[dict]:
    return [c for c in cards if not _is_gone(c)]


def _visible_count(c: dict) -> int:
    return sum(1 for a in c["articles"] if not a.get("hidden"))


def _issue_label(issue: dict) -> str:
    return issue.get("name") or _ORPHAN_LABEL


def _empty_suggestion(all_cards: list[dict], all_issues: list[dict], q: str, start: str, end: str) -> str:
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
            f'<a href="{html.escape(_archive_url(q, "", ""))}">전체 기간에서 보기 →</a>'
        )
    return (
        f"'{html.escape(q)}'가 든 사안이 없습니다. "
        f'<a href="{html.escape(_archive_url("", start, end))}">사안명 조건 지우기 →</a>'
    )


def _render_filter_bar(q: str, start: str, end: str, today: str) -> str:
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
        f'<a class="clr" href="{html.escape(_archive_url("", start, end))}">지우기</a>' if q else ""
    )
    return f"""<div class="period-bar">
  <form method="GET" action="/adhoc">
    <input type="hidden" name="start" value="{html.escape(start)}">
    <input type="hidden" name="end" value="{html.escape(end)}">
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
  </form>
  <!-- [수정: 2026-09-16] 직접 입력은 기간 줄로 옮기고, 제출은 이 (보이지 않는) 폼이 맡는다 —
       날짜 칸·조회 버튼의 form= 속성이 이 폼을 가리킨다. 위 폼과 나눠 둔 이유는 예전 그대로:
       빠른 설정은 숨은 start/end를, 직접 입력은 입력한 start/end를 보내야 해서다. -->
  <form method="GET" action="/adhoc" id="adhoc-range-form" hidden>
    <input type="hidden" name="q" value="{html.escape(q)}">
  </form>
</div>"""


def build_plain_text_for_cards(cards: list[dict], get_text: Callable[[dict], str] = build_adhoc_plain_text) -> str:
    """여러 카드를 이어붙인 복사/txt 텍스트 — 카드 각각의 build_adhoc_plain_text를 그대로
    이어붙인다(카드 화면과 서식이 어긋나지 않도록, 같은 함수를 재사용)."""
    if not cards:
        return ""
    return "\n\n".join(get_text(c).rstrip("\n") for c in cards) + "\n"


def _scope_filename(cards: list[dict], scope_name: str, ext: str) -> str:
    dates = [c["collect_date"] for c in cards]
    label = date_range_label(dates) if dates else _today_str()
    safe = sanitize_filename_part(scope_name) if scope_name else "전체"
    return f"{label}_수시모니터링_{safe}.{ext}"


def _export_buttons_html(
    cards: list[dict], scope_name: str, get_text: Callable[[dict], str], size_cls: str = ""
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
        f'<button type="submit">txt</button>'
        f"</form>"
        f'<form method="POST" action="/download-excel" style="display:contents" onclick="event.stopPropagation();">'
        f'<input type="hidden" name="filename" value="{xlsx_filename}">'
        f'<input type="hidden" name="rows" value="{rows_json}">'
        f'<button type="submit" class="xls">xlsx</button>'
        f"</form>"
        f"</span>"
    )


def _window_html(c: dict) -> str:
    # 모음 카드는 수집 시간창이 없다(ADHOC_DESIGN.md §6.13) — 그 자리에 "~"만
    # 남기면 값이 빠진 것처럼 보이므로, 같은 폭의 청록 「모음」 칩으로 대신한다.
    # [수정: 2026-09-15] 확정본이 「지금까지 불러오기」마다 새로 생기면서 같은 날 같은 사안에
    # 여러 개가 선다 — 정기 보관함의 회차 줄처럼 「HH:MM 기준」으로 가른다. 시각이 없는 옛
    # 확정본만 예전 칩 그대로.
    if card.is_bundle(c):
        t = card.bundle_time(c)
        if t:
            return f'<span class="win">{html.escape(t)} 기준</span>'
        return '<span class="win"><span class="bundle-mark">확정본</span></span>'
    window = card.window_of(c)
    return f'<span class="win">{html.escape(window["start"])}~{html.escape(window["end"])}</span>'


def _render_run_row(c: dict, today: str) -> str:
    """회차(카드) 한 줄.

    [수정: 2026-08-25] "보관됨"/"정리 중" 상태 뱃지는 없앴다 — "보관" 버튼이 사라지면서
    카드 status가 언제나 "editing"이라 이 뱃지는 항상 같은 값만 보여주고 있었다
    (CLAUDE.md "사안 목록에서 삭제 vs 보관" 항목 참고).

    [수정: 2026-09-11] 줄이 <a> 하나에서 「체크박스 · 링크 · 🗑」 세 칸이 됐다. 체크박스와
    🗑를 링크 **밖에** 두는 건 링크 안의 버튼·입력칸은 누르는 순간 링크 이동과 엉키기
    때문이다. 🗑는 확인창 없이 바로 지운다 — 지운 자리에 「삭제함 · 되살리기」가 곧바로
    남아 실수를 그 자리에서 되돌린다(확정본의 기사 🗑가 확인창 없이 숨기는 것과 같은 무게).
    방금 지운 카드(_is_gone)는 링크 대신 그 「삭제함」 줄로 그린다.
    """
    cid = html.escape(c["id"])
    count = _visible_count(c)
    if _is_gone(c):
        return (
            f'<div class="run-line gone" data-card-id="{cid}">'
            '<span class="pick-sp"></span>'
            f'<span class="run-row">{_window_html(c)}<span class="n">{count}건</span></span>'
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
        f'{_window_html(c)}<span class="n">{count}건</span><span class="go">열기 →</span></a>'
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


def _render_day_block(
    issue: dict, date_str: str, cards: list[dict], get_text: Callable[[dict], str], today: str, focus_ids: set
) -> str:
    live = _live(cards)
    rows_html = "".join(_render_run_row(c, today) for c in cards)
    if live:
        total_articles = sum(_visible_count(c) for c in live)
        meta_html = f'<span class="dcnt">회차 {len(live)} · {total_articles}건</span>'
        meta_html += _export_buttons_html(live, _issue_label(issue), get_text)
    else:
        meta_html = '<span class="gone-tag">삭제함</span>'
    open_attr = " open" if any(c["id"] in focus_ids for c in cards) else ""
    return f"""<details class="day-block"{open_attr}>
  <summary>{_group_pick_html(live)}<span class="d">{_format_date_kr_full(date_str)}</span>
    {meta_html}</summary>
  {rows_html}
</details>"""


def _render_issue_block(
    issue: dict,
    by_date: "OrderedDict[str, list[dict]]",
    q: str,
    get_text: Callable[[dict], str],
    open_first: bool,
    today: str,
    focus_ids: set,
    outside: int = 0,
) -> str:
    """사안 한 줄 + 그 아래 날짜·회차.

    [추가: 2026-09-15] 줄 끝 🗑 = 「이 사안에 쌓인 회차 모두 삭제」. 회차 줄 🗑(하나, 확인창
    없음)와 달리 **확인창을 한 번 거친다** — 카드 화면에서 기사 🗑는 바로 숨기고 소제목 헤더
    🗑(통째 숨기기)는 이름·건수를 묻는 것과 같은 짝이다. 지우는 건 **지금 화면에 보이는 회차만**
    (기간 필터 밖 회차는 그대로 남는다 — 소제목 통째 숨기기가 보이는 기사만 숨기는 것과 같은
    규칙)이고, 그렇게 남는 회차 수(outside)를 확인창에 적는다. 사안이 통째로 「삭제함」이
    되면 그 줄에 「모두 되살리기」를 둔다(회차 줄마다 되살리기를 누르게 하지 않으려고).
    """
    issue_cards = [c for cards in by_date.values() for c in cards]
    live = _live(issue_cards)
    days_html = "".join(
        _render_day_block(issue, d, cards, get_text, today, focus_ids) for d, cards in by_date.items()
    )
    if issue["id"] == _ORPHAN_KEY:
        # 사안 정보가 없으니 검색어도 모른다 — 지어내지 않고 사실만 적는다.
        name_html = f'<span class="nm noname">{_ORPHAN_LABEL}</span>'
        keywords_str = "사안 정보 없음"
    else:
        name_html = f'<span class="nm">{_mark_name(issue["name"], q)}</span>'
        keywords_str = " · ".join(issue.get("last_keywords", [])) or "-"
    if live:
        total_articles = sum(_visible_count(c) for c in live)
        live_dates = {c.get("collect_date") for c in live}
        meta_html = f'<span class="n">날짜 {len(live_dates)} · 회차 {len(live)} · {total_articles}건</span>'
        meta_html += _export_buttons_html(live, _issue_label(issue), get_text)
        meta_html += (
            f'<button type="button" class="row-del issue-del" title="이 사안의 회차 모두 삭제" '
            f'data-outside="{outside}" '
            f'onclick="event.preventDefault(); event.stopPropagation(); deleteIssue(this);">{icon("trash")}</button>'
        )
    else:
        gone = [c for c in issue_cards if _is_gone(c)]
        restore_all = (
            '<button type="button" class="restore-btn" '
            f'data-trash="{html.escape(json.dumps([c["_trash_name"] for c in gone]))}" '
            'onclick="event.preventDefault(); event.stopPropagation(); '
            'restoreCards(JSON.parse(this.dataset.trash));">모두 되살리기</button>'
            if len(gone) > 1
            else ""
        )
        meta_html = f'<span class="n"><span class="gone-tag">삭제함</span>{restore_all}</span>'
    is_open = open_first or any(c["id"] in focus_ids for c in issue_cards)
    return f"""<details class="issue-block"{' open' if is_open else ''} data-issue-name="{html.escape(_issue_label(issue))}">
  <summary>{_group_pick_html(live)}{name_html}
    <span class="kw">{html.escape(keywords_str)}</span>
    {meta_html}</summary>
  {days_html}
</details>"""


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
) -> str:
    """지난 사안 더보기 — 사안 → 날짜 → 회차 3단(2026-08-20 이전엔 사안 → 회차 2단).
    카드가 하나도 없는 사안은 목록에서 뺀다(사안 자체는 issues.json에 계속 남아
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
        return base_page("수시 모니터링 — 수시 보관함", body, script=page_script, nav=nav_html([NAV_HOME]))

    filter_bar_html = _render_filter_bar(q, start, end, today)
    error_html = (
        f'<p class="fixhint" style="color:{COLOR_ERROR}">{html.escape(range_error)}</p>' if range_error else ""
    )
    filtered = _filter_cards(all_cards, all_issues, q, start, end)
    gone_filtered = _filter_cards(gone_cards, all_issues, q, start, end)

    if not filtered and not gone_filtered:
        suggestion = _empty_suggestion(all_cards, all_issues, q, start, end)
        body = f"""
<div class="card">
  <h1 class="page-title">수시 보관함</h1>
  <p class="page-sub">사안별로 쌓인 확정본 · {_retention_label()} 보관됩니다</p>
  {filter_bar_html}
  {error_html}
  <p class="result-line">{html.escape(_condition_text(q, start, end))} · <b>결과 없음</b></p>
  {f'<p class="fixhint">{suggestion}</p>' if suggestion else ''}
  <p class="empty">조건에 맞는 수집 결과가 없습니다.</p>
  {recent_html}
</div>"""
        return base_page(
            "수시 모니터링 — 수시 보관함", body, script=page_script, nav=nav_html([NAV_HOME])
        )

    grouped = _group_by_issue_and_date(filtered + gone_filtered, set(issues_by_id))

    # build_adhoc_plain_text(카드)는 결과 전체(top) → 사안 → 날짜 세 층에서 카드별로
    # 최대 3번 재사용되므로, 카드 1건당 한 번만 계산해 세 층이 나눠 쓰게 한다.
    text_cache: dict[str, str] = {}

    def cached_text(c: dict) -> str:
        cid = c["id"]
        if cid not in text_cache:
            text_cache[cid] = build_adhoc_plain_text(c)
        return text_cache[cid]

    focus_ids = deleted_ids | ({restored_id} if restored_id else set())
    # 사안 줄 🗑 확인창의 「이 기간 밖의 회차 N개는 그대로 남아요」 — 사안명 검색은 사안을
    # 통째로 거르므로, 한 사안 안에서 갈리는 건 기간 필터뿐이다.
    total_by_issue: dict[str, int] = {}
    for c in all_cards:
        key = c.get("issue_id") if c.get("issue_id") in issues_by_id else _ORPHAN_KEY
        total_by_issue[key] = total_by_issue.get(key, 0) + 1
    issue_sections = []
    issue_count = 0
    for issue_id, by_date in grouped.items():
        issue = _ORPHAN_ISSUE if issue_id == _ORPHAN_KEY else issues_by_id[issue_id]
        shown = len(_live([c for cards in by_date.values() for c in cards]))
        issue_sections.append(
            _render_issue_block(
                issue,
                by_date,
                q,
                cached_text,
                open_first=(not issue_sections),
                today=today,
                focus_ids=focus_ids,
                outside=max(0, total_by_issue.get(issue_id, 0) - shown),
            )
        )
        if _live([c for cards in by_date.values() for c in cards]):
            issue_count += 1

    total_articles = sum(_visible_count(c) for c in filtered)
    top_export = _export_buttons_html(filtered, q or "전체", cached_text, size_cls=" lg")
    empty_count = sum(1 for c in filtered if _visible_count(c) == 0 and c.get("collect_date") != today)

    body = f"""
<div class="card">
  <h1 class="page-title">수시 보관함</h1>
  <p class="page-sub">사안별로 쌓인 확정본 · {_retention_label()} 보관됩니다</p>
  {filter_bar_html}
  {error_html}
  <div class="result-bar">
    <p class="result-line">{html.escape(_condition_text(q, start, end))} · 사안 <b>{issue_count}개</b> · 회차 <b>{len(filtered)}건</b> · 기사 <b>{total_articles}건</b></p>
    {_tidy_controls_html(empty_count)}
    {top_export}
  </div>
  {_SEL_BAR_HTML}
  {"".join(issue_sections)}
  {recent_html}
</div>"""

    # 보관함은 "홈" 하나만 — 수집 원본·카드로 가는 길은 이 화면 본문(목록/버튼)이
    # 이미 제공하므로 상단바에서 중복으로 안 만든다(사용자 지정).
    return base_page(
        "수시 모니터링 — 수시 보관함",
        body,
        script=page_script,
        nav=nav_html([NAV_HOME]),
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
// 사안·날짜 줄 체크 = 그 아래 회차 전부(/hidden 묶음 체크와 같은 한 방향 — 개별 해제로 머리가 안 풀린다)
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
    var d = cb.closest('details.day-block'); if (d) { d.open = true; }
    var i = cb.closest('details.issue-block'); if (i) { i.open = true; }
  });
  onPickChange();
}
function deletePicked() {
  var lines = pickedLines();
  if (!lines.length) { return; }
  var articles = lines.reduce(function (sum, l) { return sum + Number(l.dataset.count || 0); }, 0);
  var picked = new Set(lines);
  var goneIssues = Array.prototype.filter.call(document.querySelectorAll('details.issue-block'), function (block) {
    var live = block.querySelectorAll('.run-line:not(.gone)');
    return live.length && Array.prototype.every.call(live, function (l) { return picked.has(l); });
  }).map(function (block) { return block.dataset.issueName; });
  var msg = '회차 ' + lines.length + '개를 삭제할까요?\\n기사 ' + articles.toLocaleString() + '건';
  if (goneIssues.length) { msg += ' · 사안 ' + goneIssues.length + '개가 목록에서 사라져요\\n(' + goneIssues.join(', ') + ')'; }
  if (lines.some(function (l) { return l.dataset.bundle; })) {
    msg += '\\n확정본을 지우면 원본의 「✓ …로 보냄」 표시도 함께 풀려요.';
  }
  msg += '\\n\\n지운 자리에서 바로 되살릴 수 있어요.';
  if (!confirm(msg)) { return; }
  deleteCards(lines.map(function (l) { return l.dataset.cardId; }));
}
// 사안 줄 🗑 — 그 사안 아래 지금 보이는 회차 전부. 여러 개라 확인창을 한 번 거친다
// (카드 화면의 소제목 헤더 🗑처럼 이름·건수를 박는다). 기간 밖 회차는 서버가 센 값(data-outside).
function deleteIssue(btn) {
  var block = btn.closest('details.issue-block');
  var lines = Array.prototype.slice.call(block.querySelectorAll('.run-line:not(.gone)'));
  if (!lines.length) { return; }
  var articles = lines.reduce(function (sum, l) { return sum + Number(l.dataset.count || 0); }, 0);
  var days = Array.prototype.filter.call(block.querySelectorAll('details.day-block'), function (d) {
    return d.querySelector('.run-line:not(.gone)');
  }).length;
  var msg = '「' + block.dataset.issueName + '」에 쌓인 회차를 모두 지울까요?\\n날짜 ' + days
    + ' · 회차 ' + lines.length + ' · 기사 ' + articles.toLocaleString() + '건';
  var outside = Number(btn.dataset.outside || 0);
  if (outside) { msg += '\\n이 기간 밖의 회차 ' + outside + '개는 그대로 남아요.'; }
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
