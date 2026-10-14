# Design Ref: 사용자 요청(2026-08-05) — 네이버 API의 제목·요약이 이상한 지점에서 잘려 있을 때,
# 기사 줄에 마우스를 올리면 나오는 "🔄 원문에서 다시 가져오기" 버튼으로 그 기사 하나만 고친다.
import json
import threading
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import SUMMARY_OVERRIDES_FILE

# [추가: 2026-10-12] 저장 항목에는 **화면에 입히는 칸**과 **앱이 적어 두는 기록 칸**이
# 섞여 있다. apply_summary_overrides가 항목을 통째로 병합하므로, 이 목록 밖의 칸은
# 기사 dict로 새지 않는다 — 안 그러면 기사마다 article["manual"]·["seen_modified_at"]
# 같은 키가 달라붙어 내보내기·저장 경로로 조용히 퍼진다.
_DISPLAY_FIELDS = ("title", "summary", "outlet")

# [추가: 2026-10-12] 쓰는 자리가 늘어(🔄 한 건·✏️ 한 건·일괄 불러오기) 읽기-수정-쓰기가
# 겹칠 수 있다 — 겹치면 한쪽 수정이 통째로 사라진다. app.curation의 숨김 쓰기와 같은 식으로
# 모듈 잠금으로 직렬화한다.
_lock = threading.Lock()


def load_summary_overrides() -> dict:
    """URL별로 사용자가 원문에서 다시 가져온 제목·요약을 읽어온다. {url: {"title": ..., "summary": ...}}."""
    if not SUMMARY_OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(SUMMARY_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(overrides: dict) -> None:
    atomic_write_text(SUMMARY_OVERRIDES_FILE, json.dumps(overrides, ensure_ascii=False, indent=2))


def set_summary_override(
    url: str,
    title: Optional[str],
    summary: Optional[str],
    outlet: Optional[str] = None,
    *,
    manual: bool = False,
) -> None:
    """원문 페이지에서 다시 가져왔거나 담당자가 직접 친 값을 저장한다. title·summary·outlet
    중 실제로 값이 있는 쪽만 덮어쓰고(하나도 없으면 애초에 호출하는 쪽이 부르지 않는다),
    빈 칸은 기존 저장값(있다면)이나 원래 표시값을 그대로 둔다 — 예: og:title은 없고
    og:description만 있는 페이지면 요약만 고쳐지고 제목은 그대로다.

    outlet(언론사명)은 「기사 정보 직접 수정」 창에서만 들어온다 — 「원문 다시 불러오기」는
    제목·요약만 읽는다. 네이버 미러가 아닌 언론사 자체 도메인 기사는 oid가 없어
    resolve_outlet이 이름을 못 찾고 도메인 문자열(aienews.co.kr)을 그대로 돌려주는데,
    그 값이 보고서 한 줄(`ㅇ (언론사) 제목`)로 나가기 때문에 담당자가 고칠 길이 있어야 한다.
    제목·요약과 같은 칸에 두는 이유는 「이 기사의 언론사가 무엇인가」가 제목과 같은
    **기사에 관한 사실**이어서다(어느 보고서에 넣고 빼는 결정이 아니다).
    """
    overrides = load_summary_overrides()
    entry = overrides.get(url, {})
    written = []
    if title:
        entry["title"] = title
        written.append("title")
    if summary:
        entry["summary"] = summary
        written.append("summary")
    if outlet:
        entry["outlet"] = outlet
        written.append("outlet")

    # [추가: 2026-10-12] 누가 쓴 값인지 적는다(archive/TITLE_CHANGE_DESIGN.md §4).
    # 「제목 다시 불러오기」가 여러 건을 한꺼번에 덮을 때 **담당자가 손으로 친 제목을
    # 조용히 지우지 않으려면** 그 둘을 가릴 수 있어야 한다. 칸 단위로 적는 이유는
    # 하나로 묶으면 언론사명만 ✏️로 고쳐도 제목까지 영영 잠기기 때문이다.
    marked = set(entry.get("manual") or [])
    if manual:
        marked |= set(written)
    else:
        # 🔄를 **그 기사에 직접** 누른 것이라 손으로 친 값보다 이쪽이 최신이다.
        marked -= set(written)
        entry["refetched_at"] = datetime.now().isoformat(timespec="seconds")
    if marked:
        entry["manual"] = sorted(marked)
    else:
        entry.pop("manual", None)

    overrides[url] = entry
    _write(overrides)


def apply_summary_overrides(articles: list) -> list:
    """기사 목록에 저장된 오버라이드를 입혀 돌려준다 — 저장된 키(title/summary/outlet)만
    바꾸고 나머지(url, pub_date 등)는 그대로 둔다. 완성본·초안·오늘 전체 기사·지난 기사
    전부 기사 목록을 확정하는 지점에서 한 번씩 이 함수를 거쳐야, 어느 화면에서 봐도
    같은 제목·요약·언론사가 보인다(전역 저장이므로).

    **입히는 자리가 여기 한 곳이라** 화면·복사·txt·발송·엑셀 「언론사명」이 코드를 더
    안 고쳐도 따라온다 — 저장된 키를 통째로 병합하기 때문이다.
    """
    overrides = load_summary_overrides()
    if not overrides:
        return articles
    result = []
    for article in articles:
        override = overrides.get(article["url"])
        if override:
            article = {
                **article,
                **{k: v for k, v in override.items() if k in _DISPLAY_FIELDS and v},
            }
        result.append(article)
    return result


# [추가: 2026-10-12] 「제목 다시 불러오기」가 "담은 뒤 원문을 또 손댔나"를 판정하려면
# **처음 봤을 때의 기사수정 시각**이 있어야 한다(archive/TITLE_CHANGE_DESIGN.md §3.2) — 실측 8건
# 전부에 수정 시각이 있어서 "값이 있나"로는 못 가르고, 적어 둔 값보다 뒤로 밀렸을 때만
# 뜻이 있다. 같은 항목에 적는 이유는 둘 다 **그 URL이 가리키는 기사에 관한 사실**이라서다
# (새 저장소를 만들면 보관 기간·정리 규칙이 하나 더 생긴다).
#
# 이 칸은 _DISPLAY_FIELDS 밖이라 기사 dict로 새지 않는다.
_SEEN_MODIFIED_KEY = "seen_modified_at"


def title_check_baseline(url: str, overrides: Optional[dict] = None) -> Optional[str]:
    """이 기사를 **마지막으로 확인했을 때** 페이지에 찍혀 있던 기사수정 시각.

    없으면 None — 한 번도 확인한 적이 없다는 뜻이고, 그때는 아무 말도 하지 않고
    기준만 적는다(처음 보는 기사를 "손댔다"고 할 수는 없다).

    화면이 기사를 여럿 그릴 땐 `overrides`에 `load_summary_overrides()` 결과를 넘겨
    파일을 한 번만 읽는다(라벨 보관함이 `load_labels()`를 한 번만 읽는 것과 같은 이유).
    """
    data = load_summary_overrides() if overrides is None else overrides
    return (data.get(url) or {}).get(_SEEN_MODIFIED_KEY) or None


def record_title_check(url: str, modified_at: Optional[str]) -> None:
    """확인한 뒤 기준점을 **지금 본 값으로 옮긴다.**

    **기준을 옮기는 게 맞다** — 안 옮기면 한 번 손댄 기사는 그 뒤 확인할 때마다 영영
    「손댔어요」가 붙어, 애초에 피하려던 "모든 줄에 붙는 표시"가 된다. 대신 담당자가
    그 한 번의 알림을 놓치면 다시 알려주지 않는다 — 제목이 바뀐 경우는 제목 자체가
    고쳐져 남으므로, 놓쳐서 잃는 건 「본문도 손댔다」는 한 줄뿐이다.

    `modified_at`이 없으면(미러가 아니거나 수정된 적 없는 기사) 아무것도 안 적는다 —
    빈 기준점을 적으면 나중에 "확인한 적 있다"와 "수정된 적 없다"가 뒤섞인다.
    """
    if not (modified_at or "").strip():
        return
    overrides = load_summary_overrides()
    entry = overrides.get(url, {})
    if entry.get(_SEEN_MODIFIED_KEY) == modified_at:
        return  # 안 바뀌었으면 파일을 다시 쓰지 않는다
    entry[_SEEN_MODIFIED_KEY] = modified_at
    overrides[url] = entry
    _write(overrides)


def apply_title_refetch(results: dict) -> None:
    """「제목 다시 불러오기」의 결과를 **한 번에** 적는다 — 파일을 한 번 읽고 한 번 쓴다.

    results: `{url: {"title": 새 제목 or None, "modified_at": 기사수정 시각 or None}}`

    여러 건을 돌릴 때 기사마다 set_summary_override·record_title_check를 부르면 60건에
    파일을 120번 읽고 쓴다 — 느린 것보다 **중간에 겹치면 앞 기사의 수정이 사라지는 것**이
    문제다(여러 기사 숨기기를 요청 하나로 묶은 것과 같은 이유).

    제목은 🔄가 가져온 값이므로 `refetched_at`을 적고 그 칸의 ✏️ 표시는 지운다 —
    일괄 경로는 애초에 ✏️ 항목을 건너뛰고 들어오므로(app.title_refetch.is_title_protected),
    여기 도착한 제목은 덮어도 되는 것들뿐이다.
    """
    if not results:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        overrides = load_summary_overrides()
        changed = False
        for url, found in results.items():
            entry = overrides.get(url, {})
            before = dict(entry)
            title = (found.get("title") or "").strip()
            if title:
                entry["title"] = title
                entry["refetched_at"] = now
                marked = [f for f in (entry.get("manual") or []) if f != "title"]
                if marked:
                    entry["manual"] = marked
                else:
                    entry.pop("manual", None)
            modified_at = (found.get("modified_at") or "").strip()
            if modified_at:
                entry[_SEEN_MODIFIED_KEY] = modified_at
            if entry != before:
                overrides[url] = entry
                changed = True
        if changed:
            _write(overrides)
