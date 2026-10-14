# Design Ref: archive/TITLE_CHANGE_DESIGN.md §3·§4 — 「제목 다시 불러오기」의 판정 규칙 한 곳.
"""저장된 기사와 방금 받아온 원문을 대조하는 규칙 — 순수 함수만 (저장소·네트워크 없음).

언론사는 기사를 내보낸 뒤에도 제목을 고친다. 기관 요청으로 고치기도 한다. 그런데 앱이 저장한
제목은 수집하던 그 순간의 제목이라 그 뒤로 영영 안 바뀌고, 라벨 보관함처럼 오래 남는 목록에선
그 오차가 그대로 굳는다(실측 2026-10-12, 「부정기사」 5건 중 1건이 이미 바뀌어 있었다 —
`…'쌈짓돈' 현실화` → `…'쌈짓돈' 현실화 우려`).

**모듈이 두 칸으로 갈린다.** 위쪽은 「무엇이 달라졌나」만 답하는 **순수 함수**(저장소·
네트워크 없음)라 그대로 테스트할 수 있고, 아래 `refetch_titles` 하나만 바깥을 쓴다 —
페이지는 app.naver_api가 받아오고 저장은 app.summary_overrides가 한다. 화면은 라벨
보관함·정기 보관함이 맡는다.

판정은 셋이고, 넷째(요약)는 실측으로 기각했다:

- **제목** — 정규화한 뒤 글자 비교(§3.1). 여기가 이 기능의 생사다.
- **발행 시각** — 공짜라 같이 본다. 실측 8건 중 0건이라 거의 안 울린다.
- **원문 수정** — 「수정 시각이 있나」가 아니라 **「담은 뒤에 또 밀렸나」**(§3.2).
- ~~요약~~ — 저장값은 네이버 검색의 「검색어 주변 발췌」이고 원문은 「본문 맨 앞」이라 애초에
  다른 물건이다. 실측 8건 중 5건이 거짓 「바뀜」이었다(§3.3).

LLM을 안 쓴다 — 제목이 같은지는 글자 비교다.
"""
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_KST = timezone(timedelta(hours=9))

# 비교 전에 지우는 것들 —— 아래 normalize_title 주석 참고.
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
# 말줄임표는 글자 하나(…)·점 셋(...)·점 둘(‥)이 섞여 들어온다. 한 모양으로 눕힌다.
_ELLIPSIS_RE = re.compile(r"\.{2,}|‥|…+")
_QUOTES = {
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "–": "-", "—": "-", "−": "-",
    "\xa0": " ",
}
_QUOTE_RE = re.compile("[" + "".join(_QUOTES) + "]")


def normalize_title(value: Optional[str]) -> str:
    """제목을 **비교용으로만** 다듬는다 — 표시·저장에는 쓰지 않는다.

    다듬지 않고 그냥 비교하면 거의 모든 기사에 「바뀜」이 붙고, 그 순간 이 기능은 죽는다
    (모든 줄에 붙는 표시는 아무것도 가르지 못한다). 실제로 다른 것들:

    - 네이버 검색 API가 주는 제목엔 HTML 엔티티가 남는다 — 같은 기사에서 og:title이
      `李대통령 &quot;청렴도 31위…&quot;`, <title>이 `李대통령 "청렴도 31위…"`였다.
    - 검색 API는 `<b>`·`</b>` 강조 태그를 제목에 섞어 보낸다.
    - 따옴표 모양(" / “ / ”), 말줄임표(… / ...), 줄표(- / – / —), 공백 개수, 양끝 공백.

    **말머리는 떼지 않는다** — 언론사가 `[단독]`을 떼면 그건 진짜 변화다.

    규칙을 넓히려면 다시 실측한다(오탐 0 기준 — 사진 추정 필터와 같은 기준).
    """
    text = value or ""
    # 엔티티가 두 겹으로 박힌 경우가 있어(`&amp;quot;`) 더 안 풀릴 때까지 푼다.
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = _TAG_RE.sub("", text)
    text = _QUOTE_RE.sub(lambda m: _QUOTES[m.group(0)], text)
    text = _ELLIPSIS_RE.sub("…", text)
    return _SPACE_RE.sub(" ", text).strip()


def titles_differ(stored: Optional[str], fetched: Optional[str]) -> bool:
    """제목이 실제로 바뀌었나. **받아온 쪽이 비어 있으면 「안 바뀜」이다** — 못 읽은 것을
    「제목이 사라졌다」로 읽으면 멀쩡한 제목을 빈 값으로 덮는다."""
    if not (fetched or "").strip():
        return False
    return normalize_title(stored) != normalize_title(fetched)


def parse_stamp(raw: Optional[str]) -> Optional[datetime]:
    """페이지·저장값의 시각 문자열을 KST 기준 naive datetime으로.

    둘이 모양이 다르다 — 페이지는 `2026-10-07 17:22:19`, 저장값은 ISO
    (`2026-10-07T12:11:13+09:00`). 못 읽으면 None이고, **없는 시각을 지어내지 않는다**
    (app.alert_event.pub_dt와 같은 원칙).
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_KST).replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0)


def pub_differs(stored: Optional[str], fetched: Optional[str]) -> bool:
    """발행 시각이 바뀌었나 — 분 단위로 본다(초는 표기 차이가 난다).

    **한쪽이라도 못 읽으면 「안 바뀜」이다.** 실측 8건 중 0건이라 거의 안 울리는 판정이고,
    울릴 때는 기사를 내렸다 다시 올린 드문 경우다.
    """
    a, b = parse_stamp(stored), parse_stamp(fetched)
    if a is None or b is None:
        return False
    return a != b


def touched_since(seen: Optional[str], fetched: Optional[str]) -> bool:
    """담은 뒤 원문을 **또** 손댔나.

    「수정 시각이 있나」로 쓰면 안 된다 — 실측 8건 **전부**에 수정 시각이 있었다
    (발행 +3분 ~ +6시간 44분). 기사는 거의 다 한 번은 손대므로 그대로 쓰면 모든 줄에
    표시가 붙어 아무것도 못 가른다. 그래서 **처음 확인할 때 적어 둔 값보다 뒤로
    밀렸을 때만** 참이다.

    seen이 없으면(처음 확인하는 기사) 거짓 — 그때는 기준을 적기만 하고 아무 말도 안 한다.
    """
    a, b = parse_stamp(seen), parse_stamp(fetched)
    if a is None or b is None:
        return False
    return b > a


def is_title_protected(entry: Optional[dict]) -> bool:
    """이 기사의 제목을 일괄 불러오기가 **건드리면 안 되나**.

    app.summary_overrides는 🔄(원문에서 가져옴)와 ✏️(직접 적음)를 같은 칸에 담는다.
    구분 없이 덮으면 **담당자가 손으로 고친 제목이 조용히 사라진다**
    (CODING_CONVENTIONS §3 「담당자 작업은 조용히 사라지지 않는다」).

    판정 셋:
      · 저장된 제목이 없다            → 덮을 게 없다(거짓)
      · `manual`에 "title"이 있다      → ✏️로 친 제목이다(참)
      · `refetched_at`이 있다          → 앱이 원문에서 가져온 제목이다(거짓)
      · 그 밖(출처를 모르는 옛 항목)   → **참**. 🔄였다고 증명할 수 없으니 안전한 쪽으로 둔다.
    """
    if not entry or not (entry.get("title") or "").strip():
        return False
    if "title" in (entry.get("manual") or []):
        return True
    return not entry.get("refetched_at")


def compare(
    stored: dict,
    fetched: dict,
    *,
    seen_modified_at: Optional[str] = None,
    override_entry: Optional[dict] = None,
) -> dict:
    """저장된 기사 하나와 방금 받아온 값을 대조한다. **아무것도 저장하지 않는다.**

    stored:  화면이 지금 보여주고 있는 값 — `title`·`pub_date`
    fetched: 원문에서 읽은 값 — `title`·`pub_date`·`modified_at` (못 읽은 건 None)
    seen_modified_at: 이 기사를 **처음 확인했을 때** 적어 둔 수정 시각(§3.2)
    override_entry: app.summary_overrides의 이 URL 항목 — ✏️ 보호 판정용(§4)

    돌려주는 것:
      protected   담당자가 직접 고친 제목이라 건드리지 않았다
      title_changed / old_title / new_title
      pub_changed / new_pub
      touched     제목은 그대로인데 담은 뒤 원문을 또 손댔다
      modified_at 페이지에 찍힌 수정 시각(그대로 — 화면이 적는다)
    """
    protected = is_title_protected(override_entry)
    new_title = (fetched.get("title") or "").strip() or None
    changed = (not protected) and titles_differ(stored.get("title"), new_title)
    modified_at = fetched.get("modified_at")
    return {
        "protected": protected,
        "title_changed": changed,
        "old_title": stored.get("title") or "",
        "new_title": new_title if changed else None,
        "pub_changed": pub_differs(stored.get("pub_date"), fetched.get("pub_date")),
        "new_pub": fetched.get("pub_date"),
        # 제목이 바뀐 행은 그 자체로 알리므로 「원문도 손댔다」를 겹쳐 적지 않는다.
        "touched": (not changed) and touched_since(seen_modified_at, modified_at),
        "modified_at": modified_at,
    }


def summarize(verdicts: list) -> dict:
    """결과 줄에 적을 건수 — `5건 확인 · 제목이 바뀐 기사 1건 · 원문만 손댄 기사 1건`.

    **「확인했다」와 「확인할 수 없다」를 뭉개지 않는다** — 받아오지 못한 기사(네트워크 실패)와
    아예 보지 않은 기사(언론사 자체 도메인)는 `checked`에서 빠지고 각자 칸으로 센다.
    """
    checked = [v for v in verdicts if v is not None]
    return {
        "checked": len(checked),
        "title_changed": sum(1 for v in checked if v["title_changed"]),
        "touched": sum(1 for v in checked if v["touched"]),
        "pub_changed": sum(1 for v in checked if v["pub_changed"]),
        "protected": sum(1 for v in checked if v["protected"]),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 여기부터는 바깥을 쓴다 — 위쪽 순수 함수와 섞지 않는다.
# ──────────────────────────────────────────────────────────────────────────────

# 사진 원문 판정(app.photo_body)과 같은 값. 같은 서버(n.news.naver.com)를 치므로
# 둘이 한꺼번에 돌아도 과하지 않은 선이다.
_FETCH_CONCURRENCY = 4


def refetch_titles(articles: list, *, apply: bool = True) -> dict:
    """여러 기사의 제목을 원문에서 다시 불러온다 — 담당자가 버튼을 눌렀을 때.

    articles: `{"url", "title", "pub_date"}` 목록 — **화면이 지금 보여주고 있는 값**을
    그대로 넘긴다(저장소를 다시 뒤지지 않는다. 화면과 판정이 같은 값을 봐야 한다).

    돌려주는 것:
      rows   `{url: 판정}` — 판정에 `state`가 붙는다
             changed(제목 바뀜) · touched(원문만 손댐) · same · protected(✏️라 안 건드림)
             · skipped(언론사 자체 주소라 확인 안 함) · failed(못 받음)
      counts 결과 줄에 적을 건수 + skipped·failed

    **「확인했다」와 「확인할 수 없다」를 뭉개지 않는다** — 자체 도메인은 애초에 안 보고
    (제목을 믿을 수 없다), 접속 실패는 「안 바뀜」으로 굳히지 않는다(굳히면 그 기사는
    영영 안 걸린다). 둘 다 결과 줄에 제 칸으로 적힌다.

    저장은 **맨 끝에 한 번**이다(app.summary_overrides.apply_title_refetch) — 기사마다
    쓰면 60건에 파일을 120번 쓰고, 겹치면 앞 기사의 수정이 사라진다.

    `apply=False`면 **아무것도 저장하지 않고 보기만 한다** — 뒤에서 도는 「제목 바뀜 알림」
    (app.title_watch)이 쓰는 길이다. 담당자가 안 보는 사이에 제목을 갈아치우면 이미 복사해
    쓴 보고서와 화면이 말없이 어긋나므로, 알림은 알리기만 하고 반영은 담당자가 버튼을
    누를 때(`apply=True`) 한다. 기준점(`seen_modified_at`)도 그때 적힌다.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.naver_api import fetch_for_title_check
    from app.photo_body import is_mirror_url
    from app.summary_overrides import (
        apply_title_refetch, load_summary_overrides, title_check_baseline,
    )

    overrides = load_summary_overrides()
    rows, to_fetch = {}, []
    for article in articles:
        url = (article.get("url") or "").strip()
        if not url or url in rows:
            continue
        if not is_mirror_url(url):
            # 언론사 자체 도메인은 og:title 표기가 매체마다 달라 믿을 수 없다(§7).
            rows[url] = {"state": "skipped"}
            continue
        to_fetch.append(article)

    fetched = {}
    if to_fetch:
        with ThreadPoolExecutor(max_workers=_FETCH_CONCURRENCY) as pool:
            for article, found in zip(
                to_fetch, pool.map(lambda a: fetch_for_title_check(a["url"]), to_fetch)
            ):
                fetched[article["url"]] = found

    writes = {}
    for article in to_fetch:
        url = article["url"]
        found = fetched.get(url)
        if found is None:
            rows[url] = {"state": "failed"}
            continue
        verdict = compare(
            article,
            found,
            seen_modified_at=title_check_baseline(url, overrides),
            override_entry=overrides.get(url),
        )
        verdict["state"] = (
            "protected" if verdict["protected"]
            else "changed" if verdict["title_changed"]
            else "touched" if verdict["touched"]
            else "same"
        )
        rows[url] = verdict
        # 보호된 항목은 제목을 안 적는다. 기준점은 적는다 — 확인은 했기 때문이다.
        writes[url] = {
            "title": verdict["new_title"],
            "modified_at": verdict["modified_at"],
        }

    if apply:
        apply_title_refetch(writes)

    counts = summarize([r for r in rows.values() if "state" in r and r["state"] not in
                        ("skipped", "failed")])
    counts["skipped"] = sum(1 for r in rows.values() if r["state"] == "skipped")
    counts["failed"] = sum(1 for r in rows.values() if r["state"] == "failed")
    return {"rows": rows, "counts": counts}
