# Design Ref: PRD.md 기능1 규칙 19·21, 기능2 규칙 8 — 기사 숨김/되돌리기, 소제목 순서·경계 넘나들기, 소제목 이름 바꾸기
import json
import threading
from datetime import datetime, timedelta
from typing import Optional, Tuple

from app.atomic_write import atomic_write_text
from app.classifier import classify_articles, forced_group_target_names
from app.config import (
    GROUP_LABELS_FILE,
    GROUP_OVERRIDE_RETENTION_DAYS,
    GROUP_OVERRIDES_FILE,
    HIDDEN_ARTICLES_FILE,
    HIDDEN_BATCH_WINDOW_SEC,
    HIDDEN_VIEW_DAYS,
)
from app.llm_classifier import round_key

# [추가: 2026-08-05] hide_article/unhide_article는 파일을 통째로 읽어 고쳐 다시 쓰는
# 방식이라, ThreadingHTTPServer(app.settings_server)가 동시에 여러 요청을 처리하면
# (예: 체크박스로 여러 기사를 한꺼번에 숨기는 bulkHideSelected가 /hide-article을
# 병렬로 여러 번 호출) 두 요청이 같은 "숨기기 전" 상태를 읽어버려 나중에 쓴 쪽이
# 먼저 쓴 쪽의 결과를 덮어써 일부 기사가 숨겨지지 않는 경합이 실제로 발생했다.
# 이 잠금으로 읽기-수정-쓰기 전체를 한 번에 하나씩만 실행되게 한다.
_hidden_lock = threading.Lock()

# [수정: 2026-08-21] group_overrides.json(소제목 강제 배정)의 읽기-수정-쓰기 잠금.
# 예전엔 여기 "마지막으로 훑은 회차 파일 서명"을 캐시해 정리를 건너뛰는 장치가 있었는데,
# 정리 자체가 회차 파일을 안 읽는 방식(나이로만 만료)으로 바뀌면서 함께 없앴다 —
# 자세한 배경은 cleanup_group_overrides 참고.
_overrides_lock = threading.Lock()


def _recent_days(days: int, now: Optional[datetime] = None) -> set:
    """오늘부터 days일치의 날짜 문자열 집합("YYYY-MM-DD") — days=1이면 오늘 하루."""
    base = now or datetime.now()
    return {(base - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(max(days, 1))}


def _load_records(now: Optional[datetime] = None, days: int = 1) -> list:
    """숨긴 기록을 [{"url":..., "hidden_at":...}, ...] 형태로 읽어온다.

    **days의 기본값이 1(오늘 하루)이라는 것이 이 함수의 안전장치다** — 숨김 판정
    (load_hidden_urls → filter_hidden/is_hidden)이 인자를 안 넘기고 부르므로, 실수로
    빠뜨려도 틀리는 방향이 "덜 숨긴다"가 된다.

    [수정: 2026-07-26] 익일 0시가 지나면 숨김이 풀린다(사용자 요청) — 숨김은 "오늘
    화면만 정리하는" 용도라 다음날까지 끌고 갈 필요가 없다는 판단. app.manual_articles와
    같은 패턴으로, 파일을 그 자리에서 지우진 않고 읽을 때 걸러낸다.

    [수정: 2026-09-04] **"숨김이 작동하는 기간"과 "휴지통에 보이는 기간"을 갈랐다**
    (사용자 요청 — "다음 날 어제 뭘 뺐는지 확인하고 싶다"). 필터는 days=1, 열람만
    HIDDEN_VIEW_DAYS(7일)였다.

    [수정: 2026-09-16] **그 둘을 다시 합쳤다 — 이제 판정도 HIDDEN_VIEW_DAYS다**(사용자
    결정, load_hidden_urls 참고). 자정 해제가 없어져 "휴지통에 있는데 되살릴 수 없는 줄"
    자체가 사라졌다. 이 함수의 days 기본값 1은 그대로 두는데, 이제 안전장치라기보다
    **인자를 반드시 넘기라는 표시**다 — 숨김을 읽는 세 통로(load_hidden_urls /
    load_hidden_records / load_hidden_batches)가 전부 HIDDEN_VIEW_DAYS를 명시한다.

    파일에 실제로 남는 기간도 HIDDEN_VIEW_DAYS다 — hide_article/unhide_articles가 그
    범위로 읽어 그대로 다시 쓰므로, 더 오래된 기록은 다음 숨기기 때 자연히 정리된다.

    옛 형식(평평한 URL 문자열 목록)은 애초에 언제 숨겼는지 알 수 없어 그냥 버려진다
    (오늘 것이라고 확신할 수 없으므로).
    """
    if not HIDDEN_ARTICLES_FILE.exists():
        return []
    try:
        raw = json.loads(HIDDEN_ARTICLES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if raw and isinstance(raw[0], str):
        return []
    allowed = _recent_days(days, now)
    return [record for record in raw if record.get("hidden_at", "")[:10] in allowed]


def _write_records(records: list) -> None:
    atomic_write_text(HIDDEN_ARTICLES_FILE, json.dumps(records, ensure_ascii=False, indent=2))


def load_hidden_urls(now: Optional[datetime] = None) -> set:
    """최근 HIDDEN_VIEW_DAYS일 안에 숨긴 기사의 URL 집합(filter_hidden의 빠른 포함 여부 확인용).

    [수정: 2026-09-16] **오늘 하루에서 7일로 늘렸다**(사용자 결정 — "자정 넘으면 복구를
    못 하게 막혀 있는 거냐"는 물음에서 시작). 예전엔 이 함수만 오늘치를 봐서 자정이 지나면
    숨김이 저절로 풀렸고, 그래서 휴지통에 보이는 지난 날짜 기록은 **되살릴 것이 없는**
    기록이었다(화면도 그 자리에 복구 버튼을 안 줬다). 담당자가 기대한 건 "휴지통에
    있으면 언제든 되살린다"였고, 덤으로 하나가 더 맞아떨어진다 — 정기 보관함이
    filter_hidden을 거치므로, 자정에 숨김이 풀리면 **어제 실제로 발송한 보고서에는 없던
    기사가 오늘 보관함에는 다시 보였다**(2026-09-10에 소제목 순서로 고친 "보관함 텍스트가
    발송본과 다르다"와 같은 종류의 어긋남).

    **열람 기간과 판정 기간을 일부러 같은 상수로 묶어 둔다** — 두 값이 갈리면 "휴지통에는
    보이는데 복구는 안 되는" 줄이 다시 생긴다(그때를 위해 화면은 잠긴 복구 버튼을 그릴 수
    있게 해뒀다, app.settings_server.render_hidden_page).

    **이 기간을 더 늘리지 않는다** — app.undo가 hidden_articles.json을 통째로 20단계
    복사하므로(파일 크기 × 20), 1년치(실측 환산 약 3.6만 건·11MB)면 되돌리기 스택이
    200MB대가 된다. 2026-08-25에 group_overrides를 1년 → 30일로 줄인 것과 같은 이유다.
    """
    return {record["url"] for record in _load_records(now, HIDDEN_VIEW_DAYS)}


def active_hidden_dates(now: Optional[datetime] = None) -> set:
    """지금 **숨김이 실제로 작동 중인** 날짜 문자열 집합 — 화면이 "되살릴 수 있는 줄"을 가르는 기준.

    load_hidden_urls와 같은 창을 봐야 하므로 같은 헬퍼에서 뽑는다(화면이 제 나름대로
    날짜를 계산하면 판정과 조용히 어긋난다).
    """
    return _recent_days(HIDDEN_VIEW_DAYS, now)


def load_hidden_records(now: Optional[datetime] = None, days: int = HIDDEN_VIEW_DAYS) -> list:
    """숨긴 기록을 최근에 숨긴 순서대로(hidden_at 내림차순) 돌려준다 — 기본 7일치.

    "숨긴 기사 관리"(/hidden) 화면 전용이다. **이 목록은 열람용이라 오늘치보다 길다** —
    어제 이전 기록은 이미 숨김이 풀린 상태이므로, 이 값을 숨김 판정에 쓰면 안 된다
    (판정은 load_hidden_urls()/is_hidden() 하나만 거친다).
    """
    return sorted(_load_records(now, days), key=lambda record: record["hidden_at"], reverse=True)


def hide_article(
    url: str,
    now: Optional[datetime] = None,
    outlet: Optional[str] = None,
    title: Optional[str] = None,
    pub_date: Optional[str] = None,
    group: Optional[str] = None,
) -> None:
    """기사 하나를 숨김 처리한다. 원본 회차 JSON은 그대로 두고, 화면에 그릴 때만 제외한다.

    [추가: 2026-07-27] outlet/title/pub_date를 넘기면 숨긴 기록에 같이 저장한다 —
    "다음 회차 초안"(app.preview_renderer)에서 숨긴 기사는 정식 회차로 저장된 적이 없어
    "숨긴 기사 관리" 화면이 기존 방식(정식 회차에서 URL로 역조회, app.settings_server
    ._known_articles_by_url)으로는 언론사·제목을 못 찾아 URL만 보였다 — 숨기는 시점에
    화면에 이미 떠 있는 정보를 그대로 실어 보내면 어디서 숨겼든 항상 보여줄 수 있다.
    생략하면(기존 호출부·정식 회차에서 숨긴 경우) None으로 저장되고, 화면 쪽이 그때는
    기존 방식대로 정식 회차 역조회로 대체한다(하위 호환).

    [추가: 2026-09-02] group(숨길 때 그 기사가 속해 있던 소제목 이름)도 같이 적는다 —
    휴지통이 "한 번에 숨긴 덩어리"에 이름을 붙이는 데만 쓴다(load_hidden_batches).
    숨김 판정에는 **관여하지 않는다**(대조는 URL 하나뿐 — is_hidden 참고). 안 넘어오면
    None으로 남고, 그 묶음은 소제목 이름 없이 "골라서 숨김"으로 표시된다(옛 기록도 동일).
    """
    hide_articles(
        [{"url": url, "outlet": outlet, "title": title, "pub_date": pub_date, "group": group}],
        now=now,
    )


def hide_articles(items, now: Optional[datetime] = None) -> int:
    """여러 기사를 **한 번의 읽기-수정-쓰기로** 숨긴다. 새로 숨긴 건수를 돌려준다.

    items: `{"url", "outlet", "title", "pub_date", "group"}` dict 목록(url 말고는 없어도 된다).
    소제목 통째 숨기기·선택 바 일괄 숨기기가 한 요청에 모아 보낸다 — 예전엔 기사 수만큼
    요청을 나눠 보내, 파일 쓰기·되돌리기 기록·화면 재생성이 기사 수만큼 되풀이됐다.
    hidden_at은 넘긴 순서대로 마이크로초씩 벌린다(같은 시각이면 휴지통 정렬이 뒤집힌다).
    """
    base = now or datetime.now()
    added = 0
    with _hidden_lock:
        # [수정: 2026-09-04] 읽을 때 7일치를 통째로 들고 와 그대로 다시 쓴다 — 예전처럼
        # 오늘 것만 읽어 쓰면, 오늘 첫 숨기기 한 번이 어제 이전 기록을 파일에서 통째로
        # 지워 휴지통의 7일 열람이 성립하지 않는다(파일에 남는 기간 = 여기서 읽는 기간).
        records = _load_records(now, HIDDEN_VIEW_DAYS)
        # [수정: 2026-09-16] 중복 검사가 읽어온 기록 **전체**를 본다. 3일 전 기록도 여전히
        # 살아 있어(숨김 유지 기간 = 열람 기간) 오늘 날짜로 한 건 더 쌓으면 같은 기사가
        # 휴지통에 두 줄로 보이고, 한 줄만 복구해도 안 돌아온다.
        known = {record["url"] for record in records}
        for item in items:
            url = item.get("url")
            if not url or url in known:
                continue
            known.add(url)
            # [수정: 2026-07-26] 초 단위가 아니라 마이크로초까지 남긴다 — 1초 안에 연달아
            # 숨기면 시각이 같아져 정렬(내림차순)이 먼저 숨긴 걸 위로 올린다.
            records.append(
                {
                    "url": url,
                    "hidden_at": (base + timedelta(microseconds=added)).isoformat(),
                    "outlet": item.get("outlet"),
                    "title": item.get("title"),
                    "pub_date": item.get("pub_date"),
                    "group": item.get("group"),
                }
            )
            added += 1
        if added:
            _write_records(records)
    return added


def unhide_articles(urls, now: Optional[datetime] = None) -> int:
    """여러 기사의 숨김을 **한 번의 읽기-수정-쓰기로** 해제한다. 실제로 지운 건수를 돌려준다.

    [추가: 2026-09-02] 휴지통의 "모두 복구"·"선택한 기사 복구"용. 단건 해제를 여러 번
    부르면(unhide_article) 파일을 건수만큼 다시 쓰고, 그때마다 화면 재생성까지 딸려와
    12건 복구에 파일 쓰기가 12번 일어난다 — 숨기기 쪽이 잠금을 둔 이유(경합)와 같은
    문제라 애초에 한 번에 처리한다.
    """
    targets = {url for url in urls if url}
    if not targets:
        return 0
    with _hidden_lock:
        records = _load_records(now, HIDDEN_VIEW_DAYS)
        # [수정: 2026-09-16] 날짜를 안 가리고 그 URL의 기록을 지운다. 2026-09-04의
        # "오늘 기록만 지운다"는 자정에 숨김이 풀리던 시절의 규칙이었다 — 그때 지난 기록은
        # 이미 효력이 없어 지울 이유가 없었고(지우면 "그저께 뭘 뺐더라"만 잃었다), 지금은
        # 그 기록이 곧 그 기사를 숨기고 있는 당사자라 안 지우면 복구가 아무 일도 안 한다.
        kept = [record for record in records if record["url"] not in targets]
        removed = len(records) - len(kept)
        if removed:
            _write_records(kept)
        return removed


def unhide_article(url: str, now: Optional[datetime] = None) -> None:
    """숨김을 해제해 다시 화면에 보이게 한다."""
    unhide_articles([url], now)


def load_hidden_batches(now: Optional[datetime] = None, days: int = HIDDEN_VIEW_DAYS) -> list:
    """숨긴 기록을 "한 번에 숨긴 덩어리"로 묶어 최신순으로 돌려준다.

    [추가: 2026-09-02] 정기는 소제목을 통째로 날리거나(hideGroup) 체크박스로 여러 건을
    한꺼번에 숨기는 일이 흔해, 평평한 목록으로 보여주면 복구 버튼이 수십 개가 되고 그중
    하나를 찾을 수도 되살릴 수도 없다(수시의 "목록 밖 기사"는 카드 하나의 여집합이라
    이 문제가 없었다). 그래서 화면에 그리기 전에 여기서 묶는다.

    묶는 기준은 hidden_at이 HIDDEN_BATCH_WINDOW_SEC(3초) 안인가 하나뿐이다 —
    **묶음의 첫(가장 최신) 기록을 기준점으로 삼고** 그로부터 3초 안의 기록만 같은 묶음에
    넣는다. 앞 기록과의 간격을 이어 붙이는 방식(연쇄)은 바쁜 1분 동안 숨긴 것이 전부
    한 덩어리로 뭉칠 수 있어 쓰지 않는다.

    돌려주는 값(최신순):
        [{"hidden_at": ISO, "date": "YYYY-MM-DD", "group": 소제목이름|None, "records": [기록, ...]}, ...]

    [수정: 2026-09-04] 기본이 HIDDEN_VIEW_DAYS(7일)치이고, date는 화면이 날짜별로 묶고
    "오늘 것만 복구 가능"을 가르는 데 쓴다. 좌하단 팝오버처럼 오늘만 보여줘야 하는 곳은
    days=1을 넘긴다 — 그 배지 숫자는 "지금 내 화면에서 빠진 게 몇 건"이라는 뜻이라
    지난 날짜가 섞이면 거짓말이 된다. 묶음이 날짜를 가로지를 일은 없다(3초 창).

    group은 그 묶음의 기록이 **전부 같은 소제목**일 때만 채운다 — 섞였거나 값이 없으면
    None이고, 화면은 그때 "골라서 숨김"으로 적는다. "소제목 통째로 숨김"이라고는 하지
    않는다: 저장된 값만으로는 그 소제목을 통째로 날린 것인지 그 안에서 몇 건만 골라 숨긴
    것인지 구분할 수 없어, 단정하면 화면이 거짓말을 하게 된다.
    """
    records = load_hidden_records(now, days)
    batches: list = []
    for record in records:
        stamp = _parse_hidden_at(record.get("hidden_at"))
        if (
            batches
            and stamp is not None
            and batches[-1]["_anchor"] is not None
            and (batches[-1]["_anchor"] - stamp).total_seconds() <= HIDDEN_BATCH_WINDOW_SEC
        ):
            batches[-1]["records"].append(record)
            continue
        batches.append({"hidden_at": record.get("hidden_at", ""), "_anchor": stamp, "records": [record]})
    for batch in batches:
        batch.pop("_anchor", None)
        batch["date"] = (batch.get("hidden_at") or "")[:10]
        groups = {(record.get("group") or "") for record in batch["records"]}
        batch["group"] = groups.pop() if len(groups) == 1 else ""
        batch["group"] = batch["group"] or None
    return batches


def _parse_hidden_at(value: Optional[str]) -> Optional[datetime]:
    """hidden_at(ISO 문자열)을 datetime으로. 깨진 값은 None — 그 기록은 혼자 한 묶음이 된다."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _read_override_records() -> dict:
    """저장된 강제 배정 기록을 {url: {"group": 소제목이름, "at": ISO시각|None}}로 읽어온다.

    [수정: 2026-08-21] 값이 소제목 이름 문자열이던 옛 형식({url: name})도 그대로 읽는다
    (at=None으로 취급) — 형식 변경 시 옛 데이터를 버리지 않는다는 규칙
    (CODING_CONVENTIONS.md §7-2). at이 None인 기록은 다음 정리 때 "지금" 시각으로
    도장을 찍어(cleanup_group_overrides) 자연히 새 형식으로 옮겨간다.
    """
    if not GROUP_OVERRIDES_FILE.exists():
        return {}
    try:
        raw = json.loads(GROUP_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    records = {}
    for url, value in raw.items():
        if isinstance(value, str):
            records[url] = {"group": value, "at": None}
        elif isinstance(value, dict) and isinstance(value.get("group"), str):
            at = value.get("at")
            records[url] = {"group": value["group"], "at": at if isinstance(at, str) else None}
    return records


def load_group_overrides() -> dict:
    """소제목 경계를 넘어 수동으로 옮긴 기사의 강제 소제목 기록을 읽어온다.

    {기사 url: 소제목 이름} 형태 — 읽는 쪽(분류·렌더링)은 예나 지금이나 이 모양만 본다.
    파일에 실제로 저장되는 형태(시각 포함)는 _read_override_records가 흡수한다.
    """
    return {url: record["group"] for url, record in _read_override_records().items()}


def _write_override_records(records: dict) -> None:
    atomic_write_text(GROUP_OVERRIDES_FILE, json.dumps(records, ensure_ascii=False, indent=2))


def set_group_override(url: str, group_name: str, now: Optional[datetime] = None) -> None:
    """기사 하나를 특정 소제목에 강제로 배정한다 (PRD.md 기능1 규칙 21).

    [수정: 2026-08-21] 배정한 시각(at)을 함께 남긴다 — 이 기록을 언제 지워도 되는지
    판단하는 유일한 근거다(cleanup_group_overrides). 읽기-수정-쓰기라 잠금으로 감싼다
    (기사별 "다른 소제목" 드롭다운은 화면에서 동시에 여러 번 불릴 수 있다 — 숨김
    기록이 같은 이유로 잠금을 쓴다, CODING_CONVENTIONS.md §7-4).
    """
    with _overrides_lock:
        records = _read_override_records()
        records[url] = {"group": group_name, "at": (now or datetime.now()).isoformat(timespec="seconds")}
        _write_override_records(records)


def clear_group_overrides_for(group_name: str) -> int:
    """소제목 이름 하나를 가리키는 배정 기록을 전부 지운다. 반환값: 지운 개수.

    [추가: 2026-09-03] "+ 새 소제목"으로 **그 이름을 새로 만들 때**만 부른다
    (app.settings_server._handle_add_custom_group). 배정 기록은 URL -> 소제목 **이름**이라,
    같은 이름의 소제목이 다시 생기는 순간 _apply_forced_groups가 과거에 그리로 옮겼던
    기사를 전부 다시 끌어온다 — 담당자가 새로 만든 칸이 빈 적이 없다. 특히 "기타"는
    AI가 catch-all 이름으로 늘 쓰는 데다 담당자도 가장 자주 만드는 이름이라 매번 걸렸다
    (2026-09-03 제보: 09:16에 "기타"로 옮긴 2건이, 그 칸의 원래 기사를 전부 숨겨 칸이
    사라진 뒤 "기타"를 새로 만들자 그대로 되살아났다).

    지우는 건 조용한 삭제가 아니다 — 부르는 쪽이 되돌리기(app.undo)를 먼저 쌓으므로
    ↩ 한 번이면 소제목 생성과 함께 통째로 복구된다.
    """
    with _overrides_lock:
        records = _read_override_records()
        kept = {url: r for url, r in records.items() if r["group"] != group_name}
        removed = len(records) - len(kept)
        if removed:
            _write_override_records(kept)
    return removed


def cleanup_group_overrides(now: Optional[datetime] = None) -> int:
    """배정한 지 GROUP_OVERRIDE_RETENTION_DAYS가 지난 기록만 지운다. 반환값: 지운 개수(로그·확인용).

    [수정: 2026-08-21] 예전엔 "지금 살아있는 기사 URL 집합"을 만들어 거기 없는 기록을
    지웠다(회차 파일 전체 + 초안 캐시 + 예약·담아둔 기사). 그 방식은 **담당자가 방금 한
    소제목 이동을 조용히 지워버릴 수 있다** — 초안에서 옮긴 기사는 회차로 저장되기 전까지
    초안 캐시(data/preview_cache.json)에만 있는데, 그 캐시는 회차가 넘어가거나 검색어가
    바뀌면 통째로 비워지는 임시 파일이다. 그 사이에 정리가 한 번 돌면 배정 기록이 사라지고,
    잠시 뒤 회차가 확정될 때는 이미 기록이 없어서 AI가 처음 잡은 소제목으로 되돌아간다
    (2026-08-21 14:00 회차에서 실제로 2건 발생 — 담당자가 "기타"로 옮긴 기사 2건이
    확정본에서 원래 소제목으로 돌아가 있었다. HISTORY.md 같은 항목 참고).
    "지금 어딘가에 보이는가"는 임시 캐시에 좌우되는 값이라 애초에 삭제 근거가 될 수 없다 —
    담당자가 만든 기록은 나이로만 만료시킨다(CODING_CONVENTIONS.md §3).
    회차 파일을 한 개도 열지 않으므로 매 tick 돌아도 비용이 사실상 0이고, 그래서
    "회차 파일이 안 바뀌었으면 건너뛴다"는 예전의 서명 비교도 필요 없어졌다.

    at이 없는 옛 형식 기록은 지우지 않고 지금 시각으로 도장만 찍는다 — 언제 만든
    기록인지 알 수 없는 것을 "오래됐다"고 단정해 지우면 그게 바로 위 사고와 같은 종류의
    조용한 삭제다. 도장을 찍은 날로부터 GROUP_OVERRIDE_RETENTION_DAYS 뒤에 자연히 만료된다.

    [수정: 2026-08-25] 기준을 RETENTION_DAYS(365, 회차 파일 보관 기간)에서 전용 상수
    GROUP_OVERRIDE_RETENTION_DAYS(30)로 분리했다 — 위 2026-08-21 수정 때 성격이 다른 값을
    그대로 빌려 쓴 것이었다. 이 기록이 지켜야 하는 구간은 "초안에서 옮긴 것이 확정될
    때까지"(몇 시간)뿐이고, 확정되는 순간 배정 결과는 회차 파일의 "group" 필드로
    스냅샷된다(snapshot_group_names). 1년을 들고 있으면 정작 비싼 건 파일 크기가 아니라
    **되돌리기 스택**이다 — app/undo.py가 이 파일을 최대 20단계까지 통째로 복사하므로,
    실측(2026-08-25) 1년치(약 20,600건·3MB) 기준 undo_stack.json이 65MB가 되고 큐레이션
    클릭 1회당 5ms -> 250ms로 뛴다. 30일이면 목적은 그대로 지키면서 그 증폭이 사라진다.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=GROUP_OVERRIDE_RETENTION_DAYS)
    stamp = now.isoformat(timespec="seconds")
    with _overrides_lock:
        records = _read_override_records()
        kept = {}
        removed = 0
        changed = False
        for url, record in records.items():
            if record["at"] is None:
                kept[url] = {**record, "at": stamp}
                changed = True
                continue
            try:
                at = datetime.fromisoformat(record["at"])
            except ValueError:
                # 시각을 못 읽으면 지우지 않고 다시 도장을 찍는다(위와 같은 이유).
                kept[url] = {**record, "at": stamp}
                changed = True
                continue
            if at < cutoff:
                removed += 1
                changed = True
            else:
                kept[url] = record
        if changed:
            _write_override_records(kept)
    return removed


def move_article(
    articles: list,
    keywords: Optional[list],
    url: str,
    direction: str,
    overrides: Optional[dict] = None,
    round_id: Optional[tuple] = None,
) -> Tuple[list, Optional[tuple]]:
    """기사 하나를 **같은 소제목 안에서만** 위/아래로 옮긴다 (PRD.md 기능1 규칙 21).

    그 기사와 이웃의 위치를 원본 리스트에서 맞바꿔 순서만 바꾼다(다른 소제목은 전혀
    건드리지 않는다). 소제목의 맨 위/아래에 닿으면 그대로 멈춘다.

    [수정: 2026-08-14] 예전엔 맨 위/아래에서 한 번 더 누르면 앞/뒤 소제목으로 기사가
    통째로 넘어갔다 — 순서를 다듬다가 한 번 더 눌렀을 뿐인데 분류가 바뀌어버려
    담당자가 의도치 않게 기사를 잃는 사고가 반복됐다. 소제목을 넘는 이동은 "다른
    소제목" 드롭다운(bulk_reassign_group)이 이미 전담하므로, ↑/↓는 순서 전용으로
    역할을 좁혔다. 수시 모니터링(app.adhoc.card.move_article_order)이 처음부터
    이 규칙이었고, 이제 정기도 같아졌다.

    overrides: 지금까지 쌓인 강제 배정 기록. classify_articles에 그대로 반영해 "현재
    화면에 실제로 보이는 소제목 구성" 기준으로 위/아래를 판단한다.

    [수정: 2026-07-27] classify_articles에는 articles를 그대로 넘기지 않고
    filter_hidden(articles)를 넘긴다 — 안 그러면 숨긴 기사가 여전히 같은 소제목의
    "보이지 않는 이웃"으로 남아있어, 화면에는 기사가 1개만 보이는 소제목인데도
    백엔드는 여전히 2개짜리로 보고 "같은 소제목 내 순서 변경"으로 처리해버린다 —
    그 순서 변경 대상이 숨긴(안 보이는) 기사라 화면엔 아무 변화가 없어 "이동이 안 된다"는
    버그가 났다. 소제목 경계 판단(group_index/target_index)은 항상 화면에 보이는
    구성 기준이어야 하고, 실제 순서를 맞바꿀 때는(아래) 원본 articles 전체에서 위치를
    찾으므로 숨긴 기사도 원래 저장 순서 그대로 남는다(삭제되지 않는다).
    반환: (new_articles, new_override).
      - new_override는 항상 None이다 — 소제목을 넘는 이동이 사라졌으므로 남겨둔
        자리일 뿐이다(호출부 두 곳이 아직 이 형태를 기대하므로 형태만 유지한다).
      - 소제목의 맨 위/아래라 더 옮길 곳이 없거나 url을 못 찾으면 (articles, None)을
        그대로 돌려준다.

    [수정: 2026-08-14] allow_llm_call=False를 명시한다 — 이건 "지금 몇 번째 자리인가"를
    알아보려는 읽기용 호출일 뿐이지 소제목을 새로 짓는 순간이 아니다. 예전엔 이 매개변수를
    안 넘겨 기본값(True)을 그대로 물려받았고, 그래서 캐시가 정확히 안 맞아떨어지면(예:
    숨긴 기사 포함 여부가 collect_run과 어긋난 경우) 순서 하나 바꾸려던 호출이 조용히
    진짜 API를 불러 소제목 이름을 통째로 새로 지어버렸다(2026-08-14 09:30 회차 사고).
    """
    groups = classify_articles(
        filter_hidden(articles),
        keywords,
        forced_groups=overrides,
        custom_group_names=forced_group_target_names(),
        allow_llm_call=False,
        round_id=round_id,
    )
    group_index = next(
        (i for i, g in enumerate(groups) if any(a["url"] == url for a in g["articles"])), None
    )
    if group_index is None:
        return articles, None

    group_urls = [a["url"] for a in groups[group_index]["articles"]]
    pos = group_urls.index(url)
    swap_with = pos - 1 if direction == "up" else pos + 1

    if 0 <= swap_with < len(group_urls):
        other_url = group_urls[swap_with]
        index_by_url = {a["url"]: i for i, a in enumerate(articles)}
        i, j = index_by_url[url], index_by_url[other_url]
        new_articles = list(articles)
        new_articles[i], new_articles[j] = new_articles[j], new_articles[i]
        return new_articles, None

    # 소제목의 맨 위/아래 — 경계는 넘지 않는다(다른 소제목으로 옮기려면 드롭다운을 쓴다).
    return articles, None


def bulk_move_articles(
    articles: list,
    keywords: Optional[list],
    urls: list,
    direction: str,
    overrides: Optional[dict] = None,
    round_id: Optional[tuple] = None,
) -> list:
    """체크박스로 선택한 기사 여러 개(3~4개 등)를 같은 소제목 안에서 통째로 한 칸
    위/아래로 옮긴다(하단 "일괄 이동" 바의 ↑/↓, 2026-08-04 추가).

    move_article과 달리 소제목 경계는 넘지 않는다 — 여러 개를 한꺼번에 옮기다 일부만
    다른 소제목으로 넘어가면 "묶음"이라는 개념이 깨지므로, 선택된 기사가 전부 같은
    소제목 안에 있어야 하고(하나라도 다른 소제목이면 아무것도 안 바꾸고 원본 그대로
    돌려준다) 그 소제목의 맨 위/아래에 닿으면 조용히 무시한다(개별 기사 ↑/↓와 달리
    여기선 그게 자연스러운 한계 — 호출하는 쪽 JS가 애초에 버튼을 비활성화해 이 경우가
    거의 안 생기지만, 서버도 한 번 더 확인한다).

    묶음을 한 칸 미는 방법: 선택된 기사를 소제목 안 현재 순서대로 정렬한 뒤, "위로"는
    맨 위부터 아래로, "아래로"는 맨 아래부터 위로 순서대로 바로 이웃과 하나씩 맞바꿔
    나간다 — 이 순서로 해야 묶음 전체가 한 칸 밀린 것과 같은 결과가 된다(거꾸로 하면
    중간에 꼬인다. 예: [A,B*,C*,D*,E]에서 "위로"는 B->C->D 순으로 각자 위 이웃과
    맞바꿔야 [B,C,D,A,E]가 된다).

    [수정: 2026-08-14] move_article과 같은 이유로 allow_llm_call=False를 명시한다 —
    이 호출도 순서 계산용 읽기일 뿐이다.
    """
    groups = classify_articles(
        filter_hidden(articles),
        keywords,
        forced_groups=overrides,
        custom_group_names=forced_group_target_names(),
        allow_llm_call=False,
        round_id=round_id,
    )
    url_set = set(urls)
    group = next((g for g in groups if any(a["url"] in url_set for a in g["articles"])), None)
    if group is None:
        return articles
    group_urls = [a["url"] for a in group["articles"]]
    if not url_set.issubset(set(group_urls)):
        return articles  # 선택 대상이 두 소제목 이상에 걸쳐 있음 — 묶음 이동 불가

    positions = sorted(group_urls.index(u) for u in url_set)
    if direction == "up":
        if positions[0] == 0:
            return articles
        ordered_urls = [group_urls[p] for p in positions]
    else:
        if positions[-1] == len(group_urls) - 1:
            return articles
        ordered_urls = [group_urls[p] for p in reversed(positions)]

    new_articles = list(articles)
    index_by_url = {a["url"]: i for i, a in enumerate(new_articles)}
    current_group_urls = list(group_urls)
    for u in ordered_urls:
        pos = current_group_urls.index(u)
        swap_with = pos - 1 if direction == "up" else pos + 1
        other_url = current_group_urls[swap_with]
        i, j = index_by_url[u], index_by_url[other_url]
        new_articles[i], new_articles[j] = new_articles[j], new_articles[i]
        index_by_url[u], index_by_url[other_url] = j, i
        current_group_urls[pos], current_group_urls[swap_with] = current_group_urls[swap_with], current_group_urls[pos]
    return new_articles


def bulk_reassign_group(urls: list, target_group: str) -> None:
    """체크박스로 선택한 기사 여러 개를 한 번에 target_group으로 옮긴다
    (스크랩 초안의 "선택한 기사 옮기기" 하단 바, 기사별 "다른 소제목으로" 드롭다운).

    move_article과 달리 "이웃과 스왑"이 아니라 "그냥 이 소제목으로 배정"이라 순서
    계산이 필요 없다 — 이미 있는 set_group_override를 URL 개수만큼 반복 호출하면
    끝이고, 그 안에서 몇 번째로 보일지는(입력 순서 유지 원칙) app.classifier
    ._apply_forced_groups가 알아서 정한다.
    """
    for url in urls:
        if url:
            set_group_override(url, target_group)


def load_group_labels(round_id: Optional[tuple] = None) -> dict:
    """이 회차(round_id)에서 사용자가 자동 생성된 소제목 단어(topic word)에 붙인 표시용
    이름을 읽어온다.

    {소제목 단어: 표시할 이름} 형태. 분류 로직 자체(어떤 기사가 어느 그룹인지)는 항상
    원래 소제목 단어로만 판단하고, 이 이름표는 화면에 보여줄 때만 마지막에 적용한다 —
    그래야 소제목 경계 넘나들기(move_article)의 대상 지정도 계속 원래 단어 기준으로
    안정적으로 동작한다.

    [수정: 2026-08-14] 파일 전체가 아니라 round_id로 지정한 회차의 이름표만 읽는다
    (app.llm_classifier.round_key로 파일 안에서 회차별로 나눠 저장한다) — 예전엔
    "이 단어가 나중에 다른 회차에서도 소제목으로 다시 잡히면 같은 이름표가 계속
    적용된다"가 의도된 동작이었는데, 사용자 요청("소제목은 그날그날 다르게 쓰여도
    된다, 이전 회차를 기억할 필요 없다")으로 뒤집혔다. "기타"에 붙인 이름표가 그 뒤
    36개 회차·259건에 잘못 적용됐던 사고(2026-08-14)가 이 방식이면 원천적으로
    불가능해진다 — 다른 회차의 "기타"는 아예 다른 dict를 본다.
    """
    if not GROUP_LABELS_FILE.exists():
        return {}
    try:
        data = json.loads(GROUP_LABELS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    bucket = data.get(round_key(round_id))
    return bucket if isinstance(bucket, dict) else {}


def _write_labels_for_round(round_id: Optional[tuple], labels_for_round: dict) -> None:
    try:
        data = json.loads(GROUP_LABELS_FILE.read_text(encoding="utf-8")) if GROUP_LABELS_FILE.exists() else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[round_key(round_id)] = labels_for_round
    atomic_write_text(GROUP_LABELS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def set_group_label(topic_word: str, label: str, round_id: Optional[tuple] = None) -> None:
    """소제목 단어의 표시 이름을 이 회차(round_id)에만 정한다 (PRD.md 기능2 규칙 8).

    [수정: 2026-08-14] "다른 회차에서도 같은 단어가 잡히면 이름표가 계속 적용된다"는
    옛 동작을 없앴다 — 이제 이름표는 round_id로 지정한 회차 하나에만 산다. 원래
    단어와 똑같은 이름으로 "바꾸면" 이름표를 지운 것으로 본다(되돌리기를 별도 버튼
    없이 자연스럽게 처리) — 이건 그대로다.
    """
    labels = load_group_labels(round_id)
    if label == topic_word:
        labels.pop(topic_word, None)
    else:
        labels[topic_word] = label
    _write_labels_for_round(round_id, labels)


def display_name_in_use(
    display_name: str, active_names: set, exclude_name: Optional[str] = None, round_id: Optional[tuple] = None
) -> bool:
    """이 표시 이름이 지금 같은 화면(같은 회차)에 함께 나타나는 다른 소제목과 겹치는지 확인한다.

    원래 단어(topic word)가 서로 다른 두 소제목이 이름표(label)만 같아지면, 분류상으로는
    별개인데 화면엔 완전히 똑같은 소제목이 두 개로 보인다 — 실제로 "기타"와 "비판 의견"이
    둘 다 "우리부 관련 및 기타"로 이름표가 붙어 발생했던 문제(2026-08-04).

    [수정: 2026-08-04] 처음엔 group_labels.json 전체 이력(지난 회차 포함)을 훑었는데,
    "회차 안에서만 중복이면 문제고, 회차끼리는 같은 이름이어도 상관없다"는 피드백에 따라
    범위를 좁혔다 — active_names는 호출하는 쪽(지금 그 화면)이 현재 DOM에 실제로 그려진
    소제목들의 원래 단어를 모아 넘긴다(app.renderer/app.preview_renderer의 getAllGroups()
    JS와 같은 소스). exclude_name은 지금 이름을 바꾸려는 소제목 자기 자신(원래 단어)을
    검사에서 빼는 용도다.

    [수정: 2026-08-14] round_id로 이제 파일 자체가 회차별로 나뉘므로("지난 회차에서만
    쓰였던 이름표는 검사 대상이 아니다"), 이 함수도 그 회차의 이름표만 읽는다 — 결과는
    같지만(다른 회차 이름표는 애초에 안 보이므로) 근거가 "active_names로 걸러서"가 아니라
    "파일 구조상 다른 회차는 안 보여서"로 바뀐 것이다.
    """
    labels = load_group_labels(round_id)
    for topic_word in active_names:
        if topic_word != exclude_name and display_group_name(topic_word, labels) == display_name:
            return True
    return False


def display_group_name(name: str, labels: dict) -> str:
    """소제목 단어를 화면에 보여줄 이름으로 바꾼다 (이름표가 없으면 원래 단어 그대로)."""
    return labels.get(name, name)


def filter_hidden(articles: list) -> list:
    """숨긴 기사를 제외한 목록을 돌려준다.

    화면 렌더링·복사용 텍스트·키워드 추출·워드클라우드 등 기사 목록을 쓰는 모든 곳이
    이 함수를 거친 뒤의 목록만 사용해야, 숨긴 기사가 어디에도 다시 새어나가지 않는다.

    **대조 기준은 URL 하나뿐이다 — 담당자가 실제로 🗑️를 누른 그 기사만 사라진다.**

    [수정: 2026-08-27] 제목 대조를 뺐다(2026-08-13에 (언론사, 제목)으로 넣고 2026-08-20에
    제목만으로 넓혔던 것을 통째로 되돌림). 그 규칙은 "네이버가 같은 기사를 새 기사 ID로
    재게재하면 옛 URL 숨김이 안 먹는다"는 부활 사고를 막으려던 것인데, 대가로 **담당자가
    누른 적 없는 기사가 조용히 사라지는** 정반대 사고를 낳았다 — 세계일보 기사를 숨겼더니
    제목이 한 글자까지 같은 조선일보 기사가 확정본에서 함께 사라진 건(2026-08-27 제보).

    두 실패의 무게가 대칭이 아니라서 이 방향으로 정했다(사용자 결정):
      - 부활(URL만 볼 때의 실패): 중복이 화면에 **보이고**, 🗑️ 한 번 더 누르면 끝난다.
      - 과잉 숨김(제목까지 볼 때의 실패): **안 보이고**, 왜 없는지 화면 어디에도 안 남아
        저장된 회차 파일을 직접 파봐야 원인을 안다.
    "기사 한 건 한 건이 소중하다 — 제목이 똑같아도 담당자가 다 볼 수 있어야 한다"는 것이
    이 앱의 규칙이고, 자동 판단이 담당자 대신 기사를 치우지 않는다. 부활 위험도 숨김
    기록이 자정에 초기화되므로 하루 안으로 갇힌다. 실측(2026-08-27, 저장된 회차 전체):
    고유 제목 2,515건 중 제목 중복은 38건뿐이고, 그중 20건은 제목만 우연히 겹친 **별개
    기사**였다 — 제목 대조가 구해내는 재게재(18건)보다 잘못 치우는 쪽이 더 많았다.
    배경·수치는 HISTORY.md "제목 대조로 인한 조용한 실종" 참고.

    hide_article이 함께 저장하는 outlet/title은 이제 순수한 **표시용**이다("숨긴 기사
    관리" 화면이 URL만 보여주지 않으려고 쓴다) — 대조에는 관여하지 않는다.
    """
    hidden_urls = load_hidden_urls()
    return [a for a in articles if not is_hidden(a, hidden_urls)]


def is_hidden(article: dict, hidden_urls: set) -> bool:
    """기사 한 건이 숨김 상태인지 — filter_hidden이 쓰는 판정과 같은 식.

    [추가: 2026-08-25] filter_hidden이 안에서만 쓰던 계산을 밖으로 뺐다 — "이 기사가
    숨김 상태인가"를 **거르지 않고 물어보기만 하는** 화면이 생겼기 때문(수시 모니터링의
    📌 담아두기 버튼, 실시간 현황의 "🗑️ 숨김" 표시). 판정이 두 벌로 갈라지면 화면은
    "담을 수 있다"고 말하는데 확정본에서는 filter_hidden이 걸러내는 어긋남이 그대로
    재발하므로(실제로 2026-08-27에 실시간 현황이 그 상태였다), 판정은 반드시 이 함수
    하나만 거친다.

    호출부가 목록을 돌 때마다 파일을 다시 읽지 않도록 URL 집합을 인자로 받는다
    (load_hidden_urls로 한 번 만들어 넘긴다).
    """
    return article["url"] in hidden_urls
